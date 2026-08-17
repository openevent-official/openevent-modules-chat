from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Callable, Iterable

import grpc
from openevent.sdk import openevent_pb2

from .codec import encode_append, encode_cancel, encode_end, encode_start, parse_message
from .errors import (
    ChannelInitializationError,
    ChannelNotManagedError,
    ChatProtocolError,
    ChatSdkError,
    ClientClosedError,
    ClientFailedError,
    ConversationStateError,
    FailureCause,
    HistoryConflictError,
    PublishCommittedSyncError,
    PublishFailedError,
    SyncReadError,
    TurnBusyError,
)
from .model import ObjectKey, TextPart, TurnRef, UINT64_MAX, require_turn_id, require_uint64
from .state import ConversationState


_FETCH_LIMIT = 1000
_INITIAL_POLL_SECONDS = 0.01
_MAX_POLL_SECONDS = 1.0
_GUARANTEED_NOT_COMMITTED = frozenset(
    {
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.ABORTED,
    }
)
_CREDENTIAL_FAILURES = frozenset({grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED})


class _Lifecycle:
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    CLOSING = "CLOSING"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


@dataclass
class _PendingPublish:
    channel_id: int
    turn_ref: TurnRef
    principal: int
    recipients: tuple[int, ...]
    object_keys: tuple[ObjectKey, ...]
    payload: bytes
    lower_bound: int
    seq: int | None = None
    matching_seqs: set[int] = field(default_factory=set)


def _grpc_code(exc: Exception) -> grpc.StatusCode:
    code_method = getattr(exc, "code", None)
    if callable(code_method):
        try:
            code = code_method()
            if isinstance(code, grpc.StatusCode):
                return code
        except Exception:
            pass
    return grpc.StatusCode.UNKNOWN


def _rpc_failure(stage: str, exc: Exception) -> FailureCause:
    return FailureCause(stage=stage, code=_grpc_code(exc), detail="RPC failed")


def _normalize_recipients(values: Iterable[int]) -> tuple[int, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ChatProtocolError("recipients must be an iterable of uint64 values") from exc
    for value in result:
        require_uint64(value, "recipient")
    return result


def _normalize_object_keys(values: Iterable[ObjectKey]) -> tuple[ObjectKey, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ChatProtocolError("object_keys must be an iterable of ObjectKey values") from exc
    if len(result) > 1024:
        raise ChatProtocolError("a message may contain at most 1024 ObjectKeys")
    if any(not isinstance(value, ObjectKey) for value in result):
        raise ChatProtocolError("object_keys must contain only ObjectKey values")
    return result


class ChatProtocolClient:
    def __init__(self, openevent_client: Any, *, principal: int, token: str, channel_ids: Iterable[int]):
        self._openevent_client = openevent_client
        self._principal = require_uint64(principal, "principal")
        if not isinstance(token, str):
            raise ChatProtocolError("token must be a string")
        self._token = token
        try:
            channels = tuple(channel_ids)
        except TypeError as exc:
            raise ChatProtocolError("channel_ids must be a non-empty iterable") from exc
        if not channels:
            raise ChatProtocolError("channel_ids must not be empty")
        for channel_id in channels:
            require_uint64(channel_id, "channel_id", nonzero=True)
        if len(set(channels)) != len(channels):
            raise ChatProtocolError("channel_ids must not contain duplicates")
        self._channel_ids = channels
        self._channel_set = frozenset(channels)

        timeout = getattr(openevent_client, "timeout", None)
        if isinstance(timeout, bool) or not isinstance(timeout, Real) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ChatProtocolError("openevent_client.timeout must be a finite number greater than 0")
        for method in ("get_status", "fetch", "publish_auto_seq", "get_channel"):
            if not callable(getattr(openevent_client, method, None)):
                raise ChatProtocolError(f"openevent_client must provide {method}()")

        self._condition = threading.Condition(threading.RLock())
        self._lifecycle = _Lifecycle.INITIALIZING
        self._failure: FailureCause | ChatSdkError | None = None
        self._states = {channel_id: ConversationState(channel_id) for channel_id in channels}
        self._next_fetch_seq = 1
        self._processed_through_seq = 0
        self._sync_target_seq = 0
        self._pending: list[_PendingPublish] = []
        self._inflight_turns: set[tuple[int, TurnRef]] = set()
        self._active_calls = 0
        self._active_rpcs = 0
        self._sync_thread: threading.Thread | None = None

        self._initialize()

    @property
    def channel_ids(self) -> tuple[int, ...]:
        return self._channel_ids

    @property
    def principal(self) -> int:
        return self._principal

    def __enter__(self) -> ChatProtocolClient:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False

    def _initialize(self) -> None:
        for channel_id in self._channel_ids:
            try:
                response = self._openevent_client.get_channel(
                    principal=self._principal,
                    token=self._token,
                    channel_id=channel_id,
                )
                channel = response.channel
                actual_id = require_uint64(channel.channel_id, "ChannelInfo.channel_id", nonzero=True)
                if actual_id != channel_id:
                    raise ConversationStateError("GetChannel returned a different channel_id")
                if channel.protocol != "chat.v1":
                    raise ConversationStateError("ChannelInfo.protocol is not chat.v1")
            except Exception as exc:
                cause = exc if isinstance(exc, ChatSdkError) else _rpc_failure("GetChannel", exc)
                raise ChannelInitializationError(channel_id, cause) from None

        try:
            status = self._openevent_client.get_status(principal=self._principal, token=self._token)
            watermark = require_uint64(status.max_seq, "GetStatus.max_seq")
            cursor = 1
            while cursor <= watermark:
                response = self._openevent_client.fetch(
                    principal=self._principal,
                    token=self._token,
                    from_seq=cursor,
                    limit=_FETCH_LIMIT,
                    only_my_recipient=False,
                    channels=self._channel_ids,
                )
                next_seq, messages = self._validate_fetch_response(response, cursor)
                with self._condition:
                    for raw_message in messages:
                        if int(raw_message.seq) <= watermark:
                            self._process_message_locked(raw_message)
                cursor = min(next_seq, watermark + 1)
            with self._condition:
                self._next_fetch_seq = watermark + 1
                self._processed_through_seq = watermark
                self._sync_target_seq = watermark
                self._lifecycle = _Lifecycle.READY
                self._sync_thread = threading.Thread(
                    target=self._sync_loop,
                    name="openevent-chat-sync",
                    daemon=True,
                )
                self._sync_thread.start()
        except Exception as exc:
            cause = exc if isinstance(exc, ChatSdkError) else _rpc_failure("initial recovery", exc)
            raise ChannelInitializationError(None, cause) from None

    @staticmethod
    def _validate_fetch_response(response: Any, from_seq: int) -> tuple[int, tuple[Any, ...]]:
        last_seq = require_uint64(getattr(response, "last_seq", None), "FetchResponse.last_seq")
        next_seq = require_uint64(getattr(response, "next_seq", None), "FetchResponse.next_seq", nonzero=True)
        if from_seq > last_seq:
            if next_seq != from_seq:
                raise ConversationStateError("tail Fetch response returned an invalid next_seq")
        elif next_seq <= from_seq or next_seq > last_seq + 1:
            raise ConversationStateError("Fetch response did not advance within its snapshot")
        messages = tuple(getattr(response, "messages", ()))
        previous = from_seq - 1
        for message in messages:
            seq = require_uint64(getattr(message, "seq", None), "EventMessage.seq", nonzero=True)
            if seq <= previous or seq < from_seq or seq >= next_seq or seq > last_seq:
                raise ConversationStateError("Fetch messages are not strictly ordered within the scanned range")
            previous = seq
        return next_seq, messages

    def _process_message_locked(self, raw_message: Any) -> None:
        parsed = parse_message(raw_message)
        if parsed.channel_id not in self._channel_set:
            raise ConversationStateError("Fetch returned a message outside the configured channels")
        self._states[parsed.channel_id].apply(parsed)
        for pending in self._pending:
            if parsed.seq >= pending.lower_bound and self._message_matches(raw_message, pending):
                pending.matching_seqs.add(parsed.seq)
            if pending.seq == parsed.seq and parsed.seq not in pending.matching_seqs:
                raise ConversationStateError("committed message does not match the frozen publish request")

    @staticmethod
    def _message_matches(message: Any, pending: _PendingPublish) -> bool:
        try:
            raw_keys = tuple((int(key.object_id), str(key.object_token)) for key in message.object_keys)
            expected_keys = tuple((key.object_id, key.object_token) for key in pending.object_keys)
            return (
                int(message.channel_id) == pending.channel_id
                and int(message.principal) == pending.principal
                and tuple(int(value) for value in message.recipients) == pending.recipients
                and bytes(message.payload) == pending.payload
                and raw_keys == expected_keys
            )
        except Exception:
            return False

    def _sync_loop(self) -> None:
        try:
            self._sync_loop_impl()
        except Exception:
            with self._condition:
                self._fail_locked(FailureCause("sync thread", detail="internal coordination failed"))

    def _sync_loop_impl(self) -> None:
        idle_delay = _INITIAL_POLL_SECONDS
        while True:
            with self._condition:
                if self._lifecycle != _Lifecycle.READY:
                    return

            outcome = self._run_fetch()
            if outcome is None:
                return
            at_tail, made_progress = outcome
            if made_progress:
                idle_delay = _INITIAL_POLL_SECONDS
            if at_tail:
                with self._condition:
                    if self._lifecycle != _Lifecycle.READY:
                        return
                    target_pending = self._processed_through_seq < self._sync_target_seq
                    self._condition.wait(timeout=_INITIAL_POLL_SECONDS if target_pending else idle_delay)
                idle_delay = _INITIAL_POLL_SECONDS if target_pending else min(idle_delay * 2, _MAX_POLL_SECONDS)

    def _run_fetch(self) -> tuple[bool, bool] | None:
        with self._condition:
            if self._lifecycle != _Lifecycle.READY:
                return None
            from_seq = self._next_fetch_seq
        try:
            response = self._call_rpc(
                lambda: self._openevent_client.fetch(
                    principal=self._principal,
                    token=self._token,
                    from_seq=from_seq,
                    limit=_FETCH_LIMIT,
                    only_my_recipient=False,
                    channels=self._channel_ids,
                )
            )
        except (ClientClosedError, ClientFailedError):
            return None
        except Exception as exc:
            with self._condition:
                self._fail_locked(_rpc_failure("Fetch", exc))
            return None

        try:
            next_seq, messages = self._validate_fetch_response(response, from_seq)
            with self._condition:
                for message in messages:
                    self._process_message_locked(message)
                self._next_fetch_seq = next_seq
                self._processed_through_seq = next_seq - 1
                self._condition.notify_all()
            last_seq = int(response.last_seq)
            return next_seq > last_seq, bool(messages) or next_seq > from_seq
        except Exception as exc:
            cause = exc if isinstance(exc, ChatSdkError) else FailureCause("Fetch processing", detail="internal processing failed")
            with self._condition:
                self._fail_locked(cause)
            return None

    def _call_rpc(self, operation: Callable[[], Any]) -> Any:
        with self._condition:
            self._ensure_ready_locked()
            self._active_rpcs += 1
        try:
            return operation()
        finally:
            with self._condition:
                self._active_rpcs -= 1
                self._condition.notify_all()

    def _ensure_ready_locked(self) -> None:
        if self._lifecycle == _Lifecycle.FAILED:
            raise ClientFailedError(self._failure or FailureCause("client", detail="unknown failure"))
        if self._lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
            raise ClientClosedError()
        if self._lifecycle != _Lifecycle.READY:
            raise ClientClosedError()

    def _fail_locked(self, cause: FailureCause | ChatSdkError) -> None:
        if self._failure is None:
            self._failure = cause
        if self._lifecycle == _Lifecycle.READY:
            self._lifecycle = _Lifecycle.FAILED
        self._condition.notify_all()

    def _begin_call(self) -> None:
        with self._condition:
            self._ensure_ready_locked()
            self._active_calls += 1

    def _end_call(self) -> None:
        with self._condition:
            self._active_calls -= 1
            self._condition.notify_all()

    def _require_channel(self, channel_id: int) -> None:
        require_uint64(channel_id, "channel_id", nonzero=True)
        if channel_id not in self._channel_set:
            raise ChannelNotManagedError(channel_id)

    def _sync_before_publish(self) -> int:
        try:
            response = self._call_rpc(
                lambda: self._openevent_client.get_status(principal=self._principal, token=self._token)
            )
        except ClientClosedError:
            raise
        except ClientFailedError as exc:
            raise SyncReadError(exc) from None
        except Exception as exc:
            failure = _rpc_failure("GetStatus", exc)
            with self._condition:
                if failure.code in _CREDENTIAL_FAILURES:
                    self._fail_locked(failure)
            raise SyncReadError(failure) from None

        try:
            watermark = require_uint64(response.max_seq, "GetStatus.max_seq")
        except Exception:
            failure = FailureCause("GetStatus processing", detail="response violated the max_seq invariant")
            with self._condition:
                self._fail_locked(failure)
            raise SyncReadError(failure) from None

        with self._condition:
            try:
                self._ensure_ready_locked()
            except ClientFailedError as exc:
                raise SyncReadError(exc) from None
            self._sync_target_seq = max(self._sync_target_seq, watermark)
            self._condition.notify_all()
            while self._processed_through_seq < watermark and self._lifecycle == _Lifecycle.READY:
                self._condition.wait()
            if self._processed_through_seq >= watermark:
                return watermark
            if self._lifecycle == _Lifecycle.FAILED:
                raise SyncReadError(ClientFailedError(self._failure or FailureCause("client", detail="unknown failure")))
            raise ClientClosedError()

    def start_turn(
        self,
        *,
        channel_id: int,
        turn_id: str,
        reply_to_turns: Iterable[TurnRef],
        content: Iterable[TextPart],
        recipients: Iterable[int] = (),
        object_keys: Iterable[ObjectKey] = (),
        extensions: dict[str, Any] | None = None,
    ) -> int:
        turn_ref = TurnRef(self._principal, require_turn_id(turn_id))
        try:
            replies = tuple(reply_to_turns)
            parts = tuple(content)
        except TypeError as exc:
            raise ChatProtocolError("reply_to_turns and content must be iterable") from exc
        if any(not isinstance(value, TurnRef) for value in replies):
            raise ChatProtocolError("reply_to_turns must contain only TurnRef values")
        if len(set(replies)) != len(replies):
            raise ChatProtocolError("reply_to_turns must not contain duplicates")
        if turn_ref in replies:
            raise ChatProtocolError("reply_to_turns must not contain the turn being created")
        if not parts or any(not isinstance(value, TextPart) for value in parts):
            raise ChatProtocolError("content must contain one or more TextPart values")
        return self._publish(
            channel_id=channel_id,
            turn_ref=turn_ref,
            recipients=recipients,
            object_keys=object_keys,
            build_payload=lambda state: self._build_start(state, turn_ref, replies, parts, extensions),
        )

    @staticmethod
    def _build_start(
        state: ConversationState,
        turn_ref: TurnRef,
        replies: tuple[TurnRef, ...],
        parts: tuple[TextPart, ...],
        extensions: dict[str, Any] | None,
    ) -> bytes:
        state.require_new_turn(turn_ref)
        state.validate_replies(turn_ref, replies)
        return encode_start(turn_ref, replies, parts, extensions)

    def append_turn(
        self,
        *,
        channel_id: int,
        turn_id: str,
        content: Iterable[TextPart],
        recipients: Iterable[int] = (),
        object_keys: Iterable[ObjectKey] = (),
        extensions: dict[str, Any] | None = None,
    ) -> int:
        turn_ref = TurnRef(self._principal, require_turn_id(turn_id))
        try:
            parts = tuple(content)
        except TypeError as exc:
            raise ChatProtocolError("content must be an iterable of TextPart values") from exc
        if not parts or any(not isinstance(value, TextPart) for value in parts):
            raise ChatProtocolError("content must contain one or more TextPart values")
        return self._publish(
            channel_id=channel_id,
            turn_ref=turn_ref,
            recipients=recipients,
            object_keys=object_keys,
            build_payload=lambda state: encode_append(turn_id, state.require_turn(turn_ref).tail_seq, parts, extensions),
        )

    def complete_turn(
        self,
        *,
        channel_id: int,
        turn_id: str,
        recipients: Iterable[int] = (),
        object_keys: Iterable[ObjectKey] = (),
        extensions: dict[str, Any] | None = None,
    ) -> int:
        turn_ref = TurnRef(self._principal, require_turn_id(turn_id))
        return self._publish(
            channel_id=channel_id,
            turn_ref=turn_ref,
            recipients=recipients,
            object_keys=object_keys,
            build_payload=lambda state: encode_end(turn_id, state.require_turn(turn_ref).tail_seq, extensions),
        )

    def cancel_turn(
        self,
        *,
        channel_id: int,
        target_turn: TurnRef,
        recipients: Iterable[int] = (),
        object_keys: Iterable[ObjectKey] = (),
        extensions: dict[str, Any] | None = None,
    ) -> int:
        if not isinstance(target_turn, TurnRef):
            raise ChatProtocolError("target_turn must be a TurnRef")
        return self._publish(
            channel_id=channel_id,
            turn_ref=target_turn,
            recipients=recipients,
            object_keys=object_keys,
            build_payload=lambda state: self._build_cancel(state, target_turn, extensions),
        )

    @staticmethod
    def _build_cancel(state: ConversationState, target_turn: TurnRef, extensions: dict[str, Any] | None) -> bytes:
        state.require_turn(target_turn)
        return encode_cancel(target_turn, extensions)

    def _publish(
        self,
        *,
        channel_id: int,
        turn_ref: TurnRef,
        recipients: Iterable[int],
        object_keys: Iterable[ObjectKey],
        build_payload: Callable[[ConversationState], bytes],
    ) -> int:
        frozen_recipients = _normalize_recipients(recipients)
        frozen_object_keys = _normalize_object_keys(object_keys)
        self._begin_call()
        inflight_key = (channel_id, turn_ref)
        inflight_acquired = False
        pending: _PendingPublish | None = None
        try:
            self._require_channel(channel_id)
            with self._condition:
                self._ensure_ready_locked()
                if inflight_key in self._inflight_turns:
                    raise TurnBusyError(channel_id, turn_ref)
                self._inflight_turns.add(inflight_key)
                inflight_acquired = True

            watermark = self._sync_before_publish()
            if watermark == UINT64_MAX:
                raise ConversationStateError("pre-publish watermark cannot be incremented")
            with self._condition:
                try:
                    self._ensure_ready_locked()
                except ClientFailedError as exc:
                    raise SyncReadError(exc) from None
                payload = build_payload(self._states[channel_id])
                pending = _PendingPublish(
                    channel_id=channel_id,
                    turn_ref=turn_ref,
                    principal=self._principal,
                    recipients=frozen_recipients,
                    object_keys=frozen_object_keys,
                    payload=payload,
                    lower_bound=watermark + 1,
                )
                self._pending.append(pending)

            proto_keys = tuple(
                openevent_pb2.ObjectKey(object_id=key.object_id, object_token=key.object_token)
                for key in frozen_object_keys
            )
            try:
                response = self._call_rpc(
                    lambda: self._openevent_client.publish_auto_seq(
                        principal=self._principal,
                        token=self._token,
                        channel_id=channel_id,
                        payload=payload,
                        recipients=frozen_recipients,
                        object_keys=proto_keys,
                    )
                )
            except ClientClosedError:
                raise
            except ClientFailedError as exc:
                raise SyncReadError(exc) from None
            except Exception as exc:
                code = _grpc_code(exc)
                with self._condition:
                    if code not in _GUARANTEED_NOT_COMMITTED or code in _CREDENTIAL_FAILURES:
                        self._fail_locked(_rpc_failure("PublishAutoSeq", exc))
                raise PublishFailedError(code, channel_id, turn_ref) from None

            try:
                seq = require_uint64(getattr(response, "seq", None), "PublishAutoSeqResponse.seq", nonzero=True)
            except Exception:
                cause = ConversationStateError("PublishAutoSeq success response violated the seq invariant")
                with self._condition:
                    self._fail_locked(cause)
                raise ClientFailedError(cause) from None
            with self._condition:
                pending.seq = seq
                while True:
                    if self._processed_through_seq >= seq:
                        if seq in pending.matching_seqs:
                            return seq
                        cause = ConversationStateError("committed seq was scanned without the frozen message")
                        self._fail_locked(cause)
                        raise PublishCommittedSyncError(seq, cause)
                    if self._lifecycle == _Lifecycle.FAILED:
                        raise PublishCommittedSyncError(
                            seq,
                            ClientFailedError(self._failure or FailureCause("client", detail="unknown failure")),
                        )
                    if self._lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
                        raise PublishCommittedSyncError(seq, ClientClosedError())
                    self._condition.notify_all()
                    self._condition.wait()
        finally:
            with self._condition:
                if pending is not None and pending in self._pending:
                    self._pending.remove(pending)
                if inflight_acquired:
                    self._inflight_turns.discard(inflight_key)
                self._condition.notify_all()
            self._end_call()

    def close(self) -> None:
        initiator = False
        with self._condition:
            if self._lifecycle == _Lifecycle.CLOSED:
                return
            if self._lifecycle == _Lifecycle.CLOSING:
                while self._lifecycle != _Lifecycle.CLOSED:
                    self._condition.wait()
                return
            self._lifecycle = _Lifecycle.CLOSING
            initiator = True
            self._condition.notify_all()

        if initiator:
            thread = self._sync_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            with self._condition:
                while self._active_calls or self._active_rpcs:
                    self._condition.wait()
                self._lifecycle = _Lifecycle.CLOSED
                self._condition.notify_all()


def create_client(
    openevent_client: Any,
    *,
    principal: int,
    token: str,
    channel_ids: Iterable[int],
) -> ChatProtocolClient:
    return ChatProtocolClient(
        openevent_client,
        principal=principal,
        token=token,
        channel_ids=channel_ids,
    )
