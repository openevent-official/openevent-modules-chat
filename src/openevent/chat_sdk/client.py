from __future__ import annotations

import copy
import math
import threading
import time
from numbers import Real
from typing import Any, Callable, Iterable

import grpc
from openevent.sdk import openevent_pb2

from .codec import encode_append, encode_cancel, encode_end, encode_single, encode_start, parse_message
from .errors import (
    ChannelInitializationError,
    ChatProtocolError,
    ChatSdkError,
    ClientClosedError,
    ClientFailedError,
    ConversationStateError,
    FailureCause,
    HistoryConflictError,
    OpenEventContractError,
    PublishFailedError,
    SubscriptionAlreadyRegisteredError,
    SubscriptionCallbackError,
    SubscriptionClosedError,
    SubscriptionError,
    SubscriptionProtocolError,
    SyncReadError,
    TurnBusyError,
    UuidAllocationError,
)
from .model import KIND_TURN_START, ObjectKey, ParsedMessage, TextPart, TurnRef, require_turn_id, require_uint64
from .state import ConversationState


_FETCH_LIMIT = 1000
_INITIAL_POLL_SECONDS = 0.01
_MAX_POLL_SECONDS = 1.0
_RETRY_INITIAL_SECONDS = 0.05
_RETRY_MAX_SECONDS = 1.0
_RETRYABLE = frozenset(
    {
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNKNOWN,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.INTERNAL,
    }
)
_NON_RETRYABLE = frozenset(
    {
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.CANCELLED,
        grpc.StatusCode.ALREADY_EXISTS,
    }
)


class _Lifecycle:
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    FAILED = "FAILED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


def _grpc_code(exc: Exception) -> grpc.StatusCode:
    method = getattr(exc, "code", None)
    if callable(method):
        try:
            value = method()
            if isinstance(value, grpc.StatusCode):
                return value
        except Exception:
            pass
    return grpc.StatusCode.UNKNOWN


def _cause(stage: str, exc: Exception) -> FailureCause:
    return FailureCause(stage=stage, code=_grpc_code(exc), detail="RPC failed")


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ChatProtocolError("openevent_client.timeout must be a positive finite number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ChatProtocolError("openevent_client.timeout must be a positive finite number")
    return value


def _tuple_uint64(values: Iterable[int], name: str) -> tuple[int, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ChatProtocolError(f"{name} must be iterable") from exc
    for value in result:
        require_uint64(value, name)
    return result


def _tuple_object_keys(values: Iterable[ObjectKey], *, allowed: bool = True) -> tuple[ObjectKey, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ChatProtocolError("object_keys must be iterable") from exc
    if not allowed and result:
        raise ChatProtocolError("this event does not accept object_keys")
    if len(result) > 1024:
        raise ChatProtocolError("a message may contain at most 1024 ObjectKeys")
    if any(not isinstance(value, ObjectKey) for value in result):
        raise ChatProtocolError("object_keys must contain only ObjectKey values")
    return result


def _protobuf_keys(keys: tuple[ObjectKey, ...]) -> tuple[Any, ...]:
    return tuple(openevent_pb2.ObjectKey(object_id=k.object_id, object_token=k.object_token) for k in keys)


def _freeze_extensions(extensions: dict[str, Any] | None) -> dict[str, Any] | None:
    if extensions is None:
        return None
    if not isinstance(extensions, dict):
        raise ChatProtocolError("extensions must be a JSON object")
    try:
        return copy.deepcopy(extensions)
    except Exception as exc:
        raise ChatProtocolError("extensions must be copyable JSON data") from exc


class SubscriptionHandle:
    def __init__(self, client: "ChatProtocolClient", on_message: Callable[[ParsedMessage], None], from_seq: int, on_error: Callable[[SubscriptionError], None] | None):
        self._client = client
        self._on_message = on_message
        self._on_error = on_error
        self._cursor = from_seq
        self._next_seq: int | None = None
        self._last_last_seq = 0
        self._terminal: SubscriptionError | ClientFailedError | SubscriptionClosedError | None = None
        self._closed = False
        self._condition = threading.Condition(threading.RLock())
        self._thread = threading.Thread(target=self._run, name="openevent-chat-subscription", daemon=True)

    @property
    def next_seq(self) -> int | None:
        with self._condition:
            return self._next_seq

    def wait_until_scanned(self, seq: int) -> None:
        require_uint64(seq, "seq")
        with self._condition:
            while True:
                if self._next_seq is not None and self._next_seq > seq:
                    return
                if self._terminal is not None:
                    raise self._terminal
                with self._client._condition:
                    lifecycle = self._client._lifecycle
                    failure = self._client._failure
                if lifecycle == _Lifecycle.FAILED:
                    raise ClientFailedError(failure or FailureCause("client", detail="unknown failure"))
                if lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
                    raise ClientClosedError()
                self._condition.wait()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if self._terminal is None:
                self._terminal = SubscriptionClosedError("subscription is closed")
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join()
        self._client._subscription_stopped(self)

    def _set_terminal(self, error: SubscriptionError | ClientFailedError | SubscriptionClosedError) -> None:
        with self._condition:
            if self._terminal is None:
                self._terminal = error
            self._condition.notify_all()

    def _notify_error(self, error: SubscriptionError | ClientFailedError) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(error)  # type: ignore[arg-type]
        except Exception:
            pass

    def _run(self) -> None:
        delay = _INITIAL_POLL_SECONDS
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    cursor = self._cursor
                with self._client._condition:
                    if self._client._lifecycle != _Lifecycle.READY:
                        if self._client._lifecycle == _Lifecycle.FAILED:
                            error = ClientFailedError(self._client._failure or FailureCause("client", detail="unknown failure"))
                            self._set_terminal(error)
                            self._notify_error(error)
                        return
                try:
                    response = self._client._retry_rpc(
                        lambda: self._client._openevent.fetch(
                            principal=self._client._principal,
                            token=self._client._token,
                            from_seq=cursor,
                            limit=_FETCH_LIMIT,
                            only_my_recipient=False,
                            channels=(self._client._channel_id,),
                        ),
                        "subscription Fetch",
                    )
                    next_seq, messages = self._client._validate_fetch_response(response, cursor, self._last_last_seq)
                    self._last_last_seq = max(self._last_last_seq, int(response.last_seq))
                except (ClientClosedError, ClientFailedError) as exc:
                    self._set_terminal(exc)
                    self._notify_error(exc)
                    return
                except Exception as exc:
                    protocol = isinstance(exc, (ConversationStateError, ChatProtocolError))
                    error: SubscriptionError = SubscriptionProtocolError(str(exc)) if protocol else SubscriptionError(str(exc))
                    self._set_terminal(error)
                    self._client._transition_failed(_cause("subscription Fetch", exc) if not protocol else error)
                    self._notify_error(error)
                    return

                try:
                    for raw in messages:
                        parsed = parse_message(raw)
                        if parsed.channel_id != self._client._channel_id:
                            raise OpenEventContractError("Fetch returned a message outside the bound Channel")
                        try:
                            self._on_message(parsed)
                        except Exception as exc:
                            error = SubscriptionCallbackError(f"on_message failed: {type(exc).__name__}")
                            self._set_terminal(error)
                            self._notify_error(error)
                            return
                except Exception as exc:
                    error = SubscriptionProtocolError(str(exc))
                    self._set_terminal(error)
                    self._client._transition_failed(error)
                    self._notify_error(error)
                    return

                with self._condition:
                    if self._closed:
                        return
                    self._cursor = next_seq
                    self._next_seq = next_seq
                    self._condition.notify_all()
                if next_seq > int(response.last_seq):
                    with self._condition:
                        self._condition.wait(timeout=delay)
                    delay = min(delay * 2, _MAX_POLL_SECONDS)
                else:
                    delay = _INITIAL_POLL_SECONDS
        finally:
            self._client._subscription_stopped(self)


class ChatProtocolClient:
    def __init__(self, openevent_client: Any, *, principal: int, token: str, channel_id: int, max_retries: int = 3, on_failure: Callable[[ChatSdkError], None]):
        self._openevent = openevent_client
        self._principal = require_uint64(principal, "principal", nonzero=True)
        if not isinstance(token, str):
            raise ChatProtocolError("token must be a string")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ChatProtocolError("max_retries must be a non-negative integer")
        if not callable(on_failure):
            raise ChatProtocolError("on_failure must be callable")
        self._token = token
        self._channel_id = require_uint64(channel_id, "channel_id", nonzero=True)
        _validate_timeout(getattr(openevent_client, "timeout", None))
        for name in ("get_channel", "get_status", "fetch", "get_uuid", "publish_auto_seq"):
            if not callable(getattr(openevent_client, name, None)):
                raise ChatProtocolError(f"openevent_client must provide {name}()")
        self._max_retries = max_retries
        self._on_failure = on_failure
        self._condition = threading.Condition(threading.RLock())
        self._lifecycle = _Lifecycle.INITIALIZING
        self._failure: FailureCause | ChatSdkError | None = None
        self._state = ConversationState(self._channel_id)
        self._next_fetch_seq = 1
        self._processed_through_seq = 0
        self._sync_target_seq = 0
        self._last_status_max = 0
        self._last_published_seq = 0
        self._last_fetch_last_seq = 0
        self._busy: set[TurnRef] = set()
        self._active_calls = 0
        self._active_rpcs = 0
        self._sync_thread: threading.Thread | None = None
        self._subscription: SubscriptionHandle | None = None
        self._initialize()

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def principal(self) -> int:
        return self._principal

    def __enter__(self) -> "ChatProtocolClient":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False

    def _retry_rpc(self, operation: Callable[[], Any], stage: str) -> Any:
        delay = _RETRY_INITIAL_SECONDS
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            with self._condition:
                self._ensure_ready_locked()
                self._active_rpcs += 1
            try:
                return operation()
            except Exception as exc:
                last = exc
                code = _grpc_code(exc)
                if code not in _RETRYABLE or attempt >= self._max_retries:
                    raise
            finally:
                with self._condition:
                    self._active_rpcs -= 1
                    self._condition.notify_all()
            with self._condition:
                if self._lifecycle != _Lifecycle.READY:
                    self._ensure_ready_locked()
                self._condition.wait(timeout=delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)
        raise last or RuntimeError(f"{stage} failed")

    def _retry_initialization(self, operation: Callable[[], Any], stage: str) -> Any:
        delay = _RETRY_INITIAL_SECONDS
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except Exception as exc:
                if _grpc_code(exc) not in _RETRYABLE or attempt >= self._max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SECONDS)
        raise RuntimeError(stage)

    def _initialize(self) -> None:
        try:
            response = self._retry_initialization(lambda: self._openevent.get_channel(principal=self._principal, token=self._token, channel_id=self._channel_id), "GetChannel")
            channel = response.channel
            if require_uint64(channel.channel_id, "ChannelInfo.channel_id", nonzero=True) != self._channel_id or channel.protocol != "chat.v1":
                raise ConversationStateError("Channel is not a chat.v1 Channel")
            status = self._retry_initialization(lambda: self._openevent.get_status(principal=self._principal, token=self._token), "GetStatus")
            target = require_uint64(status.max_seq, "GetStatus.max_seq")
            self._last_status_max = target
            cursor = 1
            while cursor <= target:
                response = self._retry_initialization(lambda cursor=cursor: self._openevent.fetch(principal=self._principal, token=self._token, from_seq=cursor, limit=_FETCH_LIMIT, only_my_recipient=False, channels=(self._channel_id,)), "Fetch")
                next_seq, messages = self._validate_fetch_response(response, cursor)
                self._last_fetch_last_seq = max(self._last_fetch_last_seq, int(response.last_seq))
                for raw in messages:
                    with self._condition:
                        self._state.apply(parse_message(raw))
                cursor = next_seq
            with self._condition:
                self._next_fetch_seq = cursor
                self._processed_through_seq = cursor - 1
                self._sync_target_seq = target
                self._lifecycle = _Lifecycle.READY
                self._sync_thread = threading.Thread(target=self._sync_loop, name="openevent-chat-sync", daemon=True)
                self._sync_thread.start()
        except Exception as exc:
            cause = exc if isinstance(exc, ChatSdkError) else _cause("initialization", exc)
            raise ChannelInitializationError(cause, self._channel_id) from None

    @staticmethod
    def _validate_fetch_response(response: Any, from_seq: int, previous_last_seq: int = 0) -> tuple[int, tuple[Any, ...]]:
        try:
            last_seq = require_uint64(response.last_seq, "FetchResponse.last_seq")
            next_seq = require_uint64(response.next_seq, "FetchResponse.next_seq")
            messages = tuple(response.messages)
        except (AttributeError, TypeError) as exc:
            raise OpenEventContractError("Fetch response is malformed") from exc
        if messages:
            max_message = max(int(m.seq) for m in messages)
            if last_seq < max_message:
                raise OpenEventContractError("Fetch last_seq is below a returned message")
        if last_seq < previous_last_seq:
            raise OpenEventContractError("Fetch last_seq moved backwards")
        if next_seq > last_seq + 1:
            raise OpenEventContractError("Fetch next_seq is beyond last_seq + 1")
        if from_seq > last_seq:
            if messages or next_seq != last_seq + 1:
                raise OpenEventContractError("Fetch tail cursor is inconsistent with last_seq")
            # OpenEvent returns max_seq + 1 for a future from_seq. Keep the
            # caller's future cursor so polling does not move backwards.
            return max(from_seq, next_seq), messages
        if next_seq <= last_seq and next_seq <= from_seq:
            raise OpenEventContractError("Fetch cursor did not advance")
        previous = from_seq - 1
        for message in messages:
            seq = require_uint64(message.seq, "EventMessage.seq", nonzero=True)
            if seq <= previous or seq < from_seq or seq >= next_seq or seq > last_seq:
                raise OpenEventContractError("Fetch messages are not strictly ordered")
            previous = seq
        return next_seq, messages

    def _sync_loop(self) -> None:
        delay = _INITIAL_POLL_SECONDS
        while True:
            with self._condition:
                if self._lifecycle != _Lifecycle.READY:
                    return
                cursor = self._next_fetch_seq
            try:
                response = self._retry_rpc(lambda: self._openevent.fetch(principal=self._principal, token=self._token, from_seq=cursor, limit=_FETCH_LIMIT, only_my_recipient=False, channels=(self._channel_id,)), "Fetch")
                next_seq, messages = self._validate_fetch_response(response, cursor, self._last_fetch_last_seq)
                self._last_fetch_last_seq = max(self._last_fetch_last_seq, int(response.last_seq))
                with self._condition:
                    for raw in messages:
                        parsed = parse_message(raw)
                        if parsed.channel_id != self._channel_id:
                            raise OpenEventContractError("Fetch returned a message outside the bound Channel")
                        self._state.apply(parsed)
                    self._next_fetch_seq = next_seq
                    self._processed_through_seq = next_seq - 1
                    self._condition.notify_all()
                if next_seq > int(response.last_seq):
                    with self._condition:
                        if self._lifecycle != _Lifecycle.READY:
                            return
                        target_pending = self._processed_through_seq < self._sync_target_seq
                        self._condition.wait(timeout=_INITIAL_POLL_SECONDS if target_pending else delay)
                    delay = _INITIAL_POLL_SECONDS if target_pending else min(delay * 2, _MAX_POLL_SECONDS)
                else:
                    delay = _INITIAL_POLL_SECONDS
            except (ClientClosedError, ClientFailedError):
                return
            except Exception as exc:
                self._transition_failed(exc if isinstance(exc, ChatSdkError) else _cause("Fetch", exc))
                return

    def _ensure_ready_locked(self) -> None:
        if self._lifecycle == _Lifecycle.FAILED:
            raise ClientFailedError(self._failure or FailureCause("client", detail="unknown failure"))
        if self._lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
            raise ClientClosedError()
        if self._lifecycle != _Lifecycle.READY:
            raise ClientClosedError()

    def _transition_failed(self, cause: FailureCause | ChatSdkError) -> None:
        with self._condition:
            if self._lifecycle != _Lifecycle.READY:
                return
            self._failure = cause
            self._lifecycle = _Lifecycle.FAILED
            subscription = self._subscription
            self._condition.notify_all()
        if subscription is not None:
            with subscription._condition:
                subscription._condition.notify_all()
        try:
            self._on_failure(cause if isinstance(cause, ChatSdkError) else ClientFailedError(cause))
        except Exception:
            pass

    def _begin_call(self) -> None:
        with self._condition:
            self._ensure_ready_locked()
            self._active_calls += 1

    def _end_call(self) -> None:
        with self._condition:
            self._active_calls -= 1
            self._condition.notify_all()

    def _sync_before_publish(self) -> None:
        try:
            status = self._retry_rpc(lambda: self._openevent.get_status(principal=self._principal, token=self._token), "GetStatus")
        except (ClientClosedError, ClientFailedError) as exc:
            raise SyncReadError(exc) from None
        except Exception as exc:
            raise SyncReadError(_cause("GetStatus", exc)) from None
        try:
            watermark = require_uint64(status.max_seq, "GetStatus.max_seq")
        except Exception as exc:
            error = OpenEventContractError("GetStatus.max_seq is invalid")
            self._transition_failed(error)
            raise SyncReadError(error) from None
        with self._condition:
            if watermark < self._last_status_max or watermark < self._last_published_seq:
                error = OpenEventContractError("GetStatus.max_seq moved backwards")
                callback = True
            else:
                self._last_status_max = watermark
                self._sync_target_seq = max(self._sync_target_seq, watermark)
                self._condition.notify_all()
                callback = False
        if callback:
            self._transition_failed(error)
            raise SyncReadError(error) from None
        with self._condition:
            while self._processed_through_seq < watermark and self._lifecycle == _Lifecycle.READY:
                self._condition.wait()
            if self._processed_through_seq >= watermark:
                return
            if self._lifecycle == _Lifecycle.FAILED:
                raise SyncReadError(ClientFailedError(self._failure or FailureCause("client", detail="unknown failure")))
            raise ClientClosedError()

    def _publish(self, ref: TurnRef, payload_builder: Callable[[], bytes], recipients: Iterable[int], object_keys: Iterable[ObjectKey]) -> int:
        recipients = _tuple_uint64(recipients, "recipient")
        keys = _tuple_object_keys(object_keys)
        self._begin_call()
        try:
            with self._condition:
                if ref in self._busy:
                    raise TurnBusyError(ref)
                self._busy.add(ref)
            try:
                self._sync_before_publish()
                with self._condition:
                    self._ensure_ready_locked()
                    payload = payload_builder()
                try:
                    uuid = self._retry_rpc(lambda: self._openevent.get_uuid(), "get_uuid")
                    uuid = require_uint64(uuid, "uuid", nonzero=True)
                except (ClientClosedError, ClientFailedError):
                    raise
                except Exception as exc:
                    raise UuidAllocationError(_cause("get_uuid", exc)) from None
                try:
                    response = self._retry_rpc(lambda: self._openevent.publish_auto_seq(principal=self._principal, token=self._token, channel_id=self._channel_id, payload=payload, uuid=uuid, recipients=recipients, object_keys=_protobuf_keys(keys)), "PublishAutoSeq")
                except (ClientClosedError, ClientFailedError):
                    raise
                except Exception as exc:
                    raise PublishFailedError(_grpc_code(exc)) from None
                try:
                    seq = require_uint64(response.seq, "PublishAutoSeqResponse.seq", nonzero=True)
                except Exception as exc:
                    error = OpenEventContractError("PublishAutoSeq returned an invalid seq")
                    self._transition_failed(error)
                    raise error from exc
                with self._condition:
                    self._last_published_seq = max(self._last_published_seq, seq)
                return seq
            finally:
                with self._condition:
                    self._busy.discard(ref)
                    self._condition.notify_all()
        finally:
            self._end_call()

    def single_turn(self, *, turn_id: str, reply_to_turns: Iterable[TurnRef], content: Iterable[TextPart], recipients: Iterable[int] = (), object_keys: Iterable[ObjectKey] = (), extensions: dict[str, Any] | None = None) -> int:
        ref = TurnRef(self._principal, require_turn_id(turn_id))
        replies = tuple(reply_to_turns)
        parts = tuple(content)
        extensions = _freeze_extensions(extensions)
        if any(not isinstance(r, TurnRef) for r in replies) or len(set(replies)) != len(replies):
            raise ChatProtocolError("reply_to_turns must contain unique TurnRef values")
        if any(not isinstance(p, TextPart) for p in parts):
            raise ChatProtocolError("content must contain TextPart values")
        return self._publish(ref, lambda: self._build_create(ref, replies, parts, extensions, single=True), recipients, object_keys)

    def start_turn(self, *, turn_id: str, reply_to_turns: Iterable[TurnRef], content: Iterable[TextPart], recipients: Iterable[int] = (), object_keys: Iterable[ObjectKey] = (), extensions: dict[str, Any] | None = None) -> int:
        ref = TurnRef(self._principal, require_turn_id(turn_id))
        replies = tuple(reply_to_turns)
        parts = tuple(content)
        extensions = _freeze_extensions(extensions)
        if any(not isinstance(r, TurnRef) for r in replies) or len(set(replies)) != len(replies):
            raise ChatProtocolError("reply_to_turns must contain unique TurnRef values")
        if not parts or any(not isinstance(p, TextPart) for p in parts):
            raise ChatProtocolError("content must contain one or more TextPart values")
        return self._publish(ref, lambda: self._build_create(ref, replies, parts, extensions, single=False), recipients, object_keys)

    def _build_create(self, ref: TurnRef, replies: tuple[TurnRef, ...], parts: tuple[TextPart, ...], extensions: dict[str, Any] | None, *, single: bool) -> bytes:
        self._state.require_new_turn(ref)
        self._state.validate_replies(ref, replies)
        return encode_single(ref, replies, parts, extensions) if single else encode_start(ref, replies, parts, extensions)

    def append_turn(self, *, turn_id: str, content: Iterable[TextPart], recipients: Iterable[int] = (), extensions: dict[str, Any] | None = None) -> int:
        ref = TurnRef(self._principal, require_turn_id(turn_id))
        parts = tuple(content)
        extensions = _freeze_extensions(extensions)
        if not parts or any(not isinstance(p, TextPart) for p in parts):
            raise ChatProtocolError("content must contain one or more TextPart values")
        return self._publish(ref, lambda: self._build_append(ref, parts, extensions), recipients, ())

    def _build_append(self, ref: TurnRef, parts: tuple[TextPart, ...], extensions: dict[str, Any] | None) -> bytes:
        state = self._state.require_turn(ref)
        if state.creation_kind != KIND_TURN_START:
            raise ConversationStateError("turn.append requires a turn.start turn")
        return encode_append(ref.turn_id, state.tail_seq, parts, extensions)

    def complete_turn(self, *, turn_id: str, recipients: Iterable[int] = (), extensions: dict[str, Any] | None = None) -> int:
        ref = TurnRef(self._principal, require_turn_id(turn_id))
        extensions = _freeze_extensions(extensions)
        return self._publish(ref, lambda: self._build_end(ref, extensions), recipients, ())

    def _build_end(self, ref: TurnRef, extensions: dict[str, Any] | None) -> bytes:
        state = self._state.require_turn(ref)
        if state.creation_kind != KIND_TURN_START:
            raise ConversationStateError("turn.end requires a turn.start turn")
        return encode_end(ref.turn_id, state.tail_seq, extensions)

    def cancel_turn(self, *, target_turn: TurnRef, recipients: Iterable[int] = (), extensions: dict[str, Any] | None = None) -> int:
        if not isinstance(target_turn, TurnRef):
            raise ChatProtocolError("target_turn must be a TurnRef")
        extensions = _freeze_extensions(extensions)
        return self._publish(target_turn, lambda: self._build_cancel(target_turn, extensions), recipients, ())

    def _build_cancel(self, target: TurnRef, extensions: dict[str, Any] | None) -> bytes:
        self._state.require_turn(target)
        return encode_cancel(target, extensions)

    def register_subscription_callback(self, on_message: Callable[[ParsedMessage], None], *, from_seq: int = 0, on_error: Callable[[SubscriptionError], None] | None = None) -> SubscriptionHandle:
        if not callable(on_message):
            raise ChatProtocolError("on_message must be callable")
        require_uint64(from_seq, "from_seq")
        if on_error is not None and not callable(on_error):
            raise ChatProtocolError("on_error must be callable")
        with self._condition:
            self._ensure_ready_locked()
            if self._subscription is not None:
                raise SubscriptionAlreadyRegisteredError("a subscription is already registered")
            handle = SubscriptionHandle(self, on_message, from_seq, on_error)
            self._subscription = handle
            handle._thread.start()
            return handle

    def _subscription_stopped(self, handle: SubscriptionHandle) -> None:
        with self._condition:
            if self._subscription is handle and handle._closed:
                self._subscription = None
            self._condition.notify_all()

    def close(self) -> None:
        current = threading.current_thread()
        with self._condition:
            if self._lifecycle == _Lifecycle.CLOSED:
                return
            if self._lifecycle != _Lifecycle.FAILED:
                self._lifecycle = _Lifecycle.CLOSING
            self._condition.notify_all()
            while self._active_calls or self._active_rpcs:
                self._condition.wait()
            sync_thread = self._sync_thread
            subscription = self._subscription
        if subscription is not None:
            subscription.close()
        if sync_thread is not None and sync_thread is not current:
            sync_thread.join()
        with self._condition:
            self._lifecycle = _Lifecycle.CLOSED
            self._condition.notify_all()


def create_client(openevent_client: Any, *, principal: int, token: str, channel_id: int, max_retries: int = 3, on_failure: Callable[[ChatSdkError], None]) -> ChatProtocolClient:
    return ChatProtocolClient(openevent_client, principal=principal, token=token, channel_id=channel_id, max_retries=max_retries, on_failure=on_failure)
