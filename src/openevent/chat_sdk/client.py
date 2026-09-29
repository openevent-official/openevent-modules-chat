"""Synchronous Chat calls and caller-owned streaming writers."""
from contextlib import contextmanager
from dataclasses import replace
import math
import threading

import grpc
from openevent.sdk.proto import openevent_pb2

from .codec import encode_payload, make_content, parse_message, validate_turn_id, validate_uint64
from .errors import (
    ChannelInitializationError, ChatProtocolError, ClientClosedError,
    ClientFailedError, FetchPageError, PublishFailedError, SyncReadError,
    TurnNotFoundError, TurnWriterStateError, UuidAllocationError, make_failure,
)
from .model import FetchPage, ObjectKey, TurnRef


_RETRY_CODES = frozenset({
    grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNKNOWN,
    grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL,
})
_NOT_COMMITTED = frozenset({
    grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED,
    grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT,
    grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.ABORTED,
})
_RETRY_SECONDS = 0.1


class _RpcFailure(Exception):
    def __init__(self, failure, attempts):
        self.failure = failure
        self.attempts = attempts


def _items(values, field):
    try:
        return tuple(values)
    except TypeError:
        raise ChatProtocolError(f"{field} must be iterable") from None


def _freeze(payload, recipients, object_keys, extensions):
    if extensions is not None:
        payload["extensions"] = extensions
    recipients = tuple(validate_uint64(value, "recipient")
                       for value in _items(recipients, "recipients"))
    keys = _items(object_keys, "object_keys")
    if len(keys) > 1024:
        raise ChatProtocolError("object_keys exceeds 1024 entries")
    frozen_keys = []
    for key in keys:
        if not isinstance(key, ObjectKey):
            raise ChatProtocolError("object_keys entries must be ObjectKey")
        validate_uint64(key.object_id, "object_id")
        if not isinstance(key.object_token, str) or not key.object_token:
            raise ChatProtocolError("object_token must be nonempty string")
        try:
            frozen_keys.append(openevent_pb2.ObjectKey(
                object_id=key.object_id, object_token=key.object_token))
        except (TypeError, ValueError, UnicodeError):
            raise ChatProtocolError("object_token must be valid UTF-8 string") from None
    encoded = encode_payload(payload, object_keys=frozen_keys)
    return encoded, recipients, tuple(frozen_keys)


class ChatProtocolClient:
    """A bound transport and lifecycle; no turn or writer registry."""

    def __init__(self, events, *, principal, token, channel_id, max_retries=3,
                 channel_validator=None):
        self._principal = validate_uint64(principal, "principal")
        self._channel_id = validate_uint64(channel_id, "channel_id")
        if not isinstance(token, str) or not token:
            raise ChatProtocolError("token must be nonempty string")
        if type(max_retries) is not int or max_retries < 0:
            raise ChatProtocolError("max_retries must be nonnegative integer")
        if channel_validator is not None and not callable(channel_validator):
            raise ChatProtocolError("channel_validator must be callable")
        timeout_ms = getattr(events, "timeout_ms", None)
        if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (float, int))
                or not math.isfinite(timeout_ms) or timeout_ms <= 0):
            raise ChatProtocolError("injected client timeout_ms must be positive and finite")
        for method in ("get_channel", "get_status", "fetch", "get_uuid",
                       "get_seq_by_uuid", "publish_auto_seq"):
            if not callable(getattr(events, method, None)):
                raise ChatProtocolError(f"injected client must provide {method}")
        self._events = events
        self._token = token
        self._max_retries = max_retries
        self._condition = threading.Condition()
        self._state = "READY"
        self._failure = None
        self._active_calls = 0
        try:
            response = self._rpc("GetChannel", lambda: events.get_channel(
                principal=self._principal, token=token, channel_id=self._channel_id))
        except _RpcFailure as error:
            raise ChannelInitializationError(error.failure) from None
        try:
            channel = response.channel
            returned_id = validate_uint64(channel.channel_id, "channel_id")
            if returned_id != self._channel_id:
                raise ChatProtocolError("GetChannel returned a different Channel")
            protocol = channel.protocol
        except (AttributeError, ChatProtocolError):
            raise ChannelInitializationError(make_failure(
                "GetChannel", category="contract",
                detail="GetChannel returned an invalid or mismatched Channel")) from None
        if protocol != "chat.v1":
            raise ChannelInitializationError(make_failure(
                "GetChannel", category="protocol",
                detail="Channel protocol is not chat.v1"))
        if channel_validator is not None:
            try:
                channel_validator(channel)
            except Exception:
                raise ChannelInitializationError(make_failure(
                    "GetChannel", category="contract",
                    detail="Channel does not meet application requirements")) from None

    @property
    def principal(self):
        return self._principal

    @property
    def channel_id(self):
        return self._channel_id

    def _check_locked(self):
        if self._state in {"CLOSING", "CLOSED"}:
            raise ClientClosedError("Chat client is closed")
        if self._failure is not None:
            raise ClientFailedError(self._failure)

    @contextmanager
    def _operation(self):
        with self._condition:
            self._check_locked()
            self._active_calls += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_calls -= 1
                self._condition.notify_all()

    def _fail(self, failure):
        with self._condition:
            if self._failure is None and failure.category != "lifecycle":
                self._failure = failure
                if self._state == "READY":
                    self._state = "FAILED"
            self._condition.notify_all()

    def _stopped_failure(self, stage):
        return self._failure or make_failure(
            stage, category="lifecycle", detail=f"{stage} stopped because the client was closed")

    def _rpc(self, stage, call, *, on_error=None):
        """Retry one logical RPC, retaining its last concrete failure on close."""
        for attempt in range(1, self._max_retries + 2):
            with self._condition:
                if self._state != "READY":
                    raise _RpcFailure(self._stopped_failure(stage), attempt - 1)
            try:
                return call()
            except Exception as exc:
                failure = make_failure(stage, exc, attempts=attempt)
                if on_error is not None:
                    on_error(failure.grpc_code)
                if failure.grpc_code not in _RETRY_CODES or attempt > self._max_retries:
                    raise _RpcFailure(failure, attempt) from None
            with self._condition:
                self._condition.wait_for(lambda: self._state != "READY", _RETRY_SECONDS)
                if self._state != "READY":
                    raise _RpcFailure(failure, attempt) from None
        raise AssertionError("unreachable retry state")

    def close(self):
        with self._condition:
            if self._state == "CLOSED":
                return
            self._state = "CLOSING"
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._active_calls == 0)
            self._state = "CLOSED"
            self._condition.notify_all()

    def __enter__(self):
        with self._condition:
            self._check_locked()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _publish(self, frozen):
        payload, recipients, object_keys = frozen
        try:
            uuid = self._rpc("get_uuid", self._events.get_uuid)
        except _RpcFailure as error:
            raise UuidAllocationError(error.failure) from None
        try:
            validate_uint64(uuid, "uuid")
        except ChatProtocolError:
            failure = make_failure("get_uuid", category="contract",
                                   detail="get_uuid returned an invalid UUID")
            self._fail(failure)
            raise UuidAllocationError(failure) from None

        uncertain = False

        def observe_error(code):
            nonlocal uncertain
            # A later definitive rejection cannot settle an earlier timeout.
            if code not in _NOT_COMMITTED:
                uncertain = True

        try:
            response = self._rpc("PublishAutoSeq", lambda: self._events.publish_auto_seq(
                principal=self._principal, token=self._token, channel_id=self._channel_id,
                payload=payload, uuid=uuid, recipients=recipients, object_keys=object_keys),
                on_error=observe_error)
        except _RpcFailure as error:
            if error.failure.grpc_code == grpc.StatusCode.ALREADY_EXISTS and error.attempts > 1:
                try:
                    seq = self._rpc("GetSeqByUuid", lambda: self._events.get_seq_by_uuid(uuid))
                except _RpcFailure as lookup_error:
                    if lookup_error.failure.grpc_code == grpc.StatusCode.NOT_FOUND:
                        failure = replace(
                            lookup_error.failure, category="contract",
                            detail="GetSeqByUuid did not find a UUID already reported as committed")
                        self._fail(failure)
                        raise PublishFailedError(failure, uuid, uncertain=True) from None
                    raise PublishFailedError(lookup_error.failure, uuid, uncertain=True) from None
                return self._committed_seq(seq, "GetSeqByUuid", uuid)
            raise PublishFailedError(error.failure, uuid, uncertain=uncertain) from None
        return self._committed_seq(getattr(response, "seq", None), "PublishAutoSeq", uuid)

    def _committed_seq(self, seq, stage, uuid):
        try:
            return validate_uint64(seq, "seq")
        except ChatProtocolError:
            failure = make_failure(stage, category="contract", detail=f"{stage} returned an invalid committed seq")
            self._fail(failure)
            raise PublishFailedError(failure, uuid, uncertain=True) from None

    def single_turn(self, *, turn_id, content=(), reply_to_seqs=(), recipients=(),
                    object_keys=(), extensions=None):
        with self._operation():
            frozen = _freeze({"kind": "turn.single", "turn_id": validate_turn_id(turn_id),
                              "content": make_content(content),
                              "reply_to_seqs": list(_items(reply_to_seqs, "reply_to_seqs"))},
                             recipients, object_keys, extensions)
            return self._publish(frozen)

    def start_turn(self, *, turn_id, content=(), reply_to_seqs=(), recipients=(),
                   object_keys=(), extensions=None):
        with self._operation():
            frozen = _freeze({"kind": "turn.start", "turn_id": validate_turn_id(turn_id),
                              "content": make_content(content),
                              "reply_to_seqs": list(_items(reply_to_seqs, "reply_to_seqs"))},
                             recipients, object_keys, extensions)
            seq = self._publish(frozen)
            return TurnWriter(self, turn_id, seq, seq)

    def cancel_turn(self, *, target_turn, recipients=(), extensions=None):
        with self._operation():
            if not isinstance(target_turn, TurnRef):
                raise ChatProtocolError("target_turn must be TurnRef")
            frozen = _freeze({"kind": "turn.cancel", "target_turn": {
                "principal": target_turn.principal, "turn_id": target_turn.turn_id}},
                recipients, (), extensions)
            return self._publish(frozen)

    def reserve_submissions(self, reserved_through, *, recipients=(), extensions=None):
        with self._operation():
            frozen = _freeze({"kind": "submission.reserve", "reserved_through": reserved_through},
                             recipients, (), extensions)
            return self._publish(frozen)

    def fetch_page(self, from_seq, limit=100):
        with self._operation():
            self._validate_fetch(from_seq, limit)
            return self._fetch_page(from_seq, limit, recovery=False)

    @staticmethod
    def _validate_fetch(from_seq, limit):
        validate_uint64(from_seq, "from_seq", positive=False)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ChatProtocolError("limit must be an integer in 1..1000")

    def _fetch_page(self, from_seq, limit, *, recovery):
        error_type = SyncReadError if recovery else FetchPageError
        try:
            response = self._rpc("Fetch", lambda: self._events.fetch(
                principal=self._principal, token=self._token, from_seq=from_seq, limit=limit,
                channels=(self._channel_id,), only_my_recipient=False))
        except _RpcFailure as error:
            if recovery or error.failure.category in {"protocol", "contract"}:
                self._fail(error.failure)
            raise error_type(error.failure) from None
        try:
            last_seq = validate_uint64(response.last_seq, "last_seq", positive=False)
            next_seq = validate_uint64(response.next_seq, "next_seq", positive=False)
            raw_messages = tuple(response.messages)
            if len(raw_messages) > limit:
                raise ValueError("Fetch returned more messages than requested")
            if from_seq > last_seq:
                if raw_messages or next_seq != last_seq + 1:
                    raise ValueError("Fetch returned an invalid page beyond the tail")
            elif not from_seq <= next_seq <= last_seq + 1:
                raise ValueError("Fetch returned next_seq outside this page's scan range")
            previous = from_seq - 1
            for raw in raw_messages:
                seq = validate_uint64(raw.seq, "seq")
                if not previous < seq < next_seq or seq > last_seq:
                    raise ValueError("Fetch returned messages outside ascending scan order")
                if raw.channel_id != self._channel_id:
                    raise ValueError("Fetch returned a message from another Channel")
                previous = seq
        except (AttributeError, TypeError, ChatProtocolError, ValueError) as exc:
            # Only locally generated explanations are copied, never RPC bodies.
            detail = str(exc) if type(exc) is ValueError else "Fetch returned invalid envelope or cursor fields"
            failure = make_failure("Fetch", category="contract", detail=detail)
            self._fail(failure)
            raise error_type(failure) from None
        messages = []
        for raw in raw_messages:
            try:
                messages.append(parse_message(raw))
            except ChatProtocolError:
                failure = make_failure("Fetch", category="protocol",
                                       detail=f"Fetch returned invalid chat.v1 data at seq {raw.seq}")
                self._fail(failure)
                raise error_type(failure) from None
        return FetchPage(tuple(messages), next_seq, last_seq)

    def resume_turn(self, turn_id, *, state_start_seq=1):
        with self._operation():
            validate_turn_id(turn_id)
            validate_uint64(state_start_seq, "state_start_seq")
            try:
                status = self._rpc("GetStatus", lambda: self._events.get_status(
                    principal=self._principal, token=self._token))
            except _RpcFailure as error:
                self._fail(error.failure)
                raise SyncReadError(error.failure) from None
            try:
                watermark = validate_uint64(status.max_seq, "max_seq", positive=False)
            except (AttributeError, ChatProtocolError):
                failure = make_failure("GetStatus", category="contract",
                                       detail="GetStatus returned an invalid message range")
                self._fail(failure)
                raise SyncReadError(failure) from None
            cursor = state_start_seq
            creation_seq = None
            last_seq = None
            terminal = False
            while cursor <= watermark:
                page = self._fetch_page(cursor, 100, recovery=True)
                for message in page.messages:
                    if message.seq > watermark:
                        break
                    payload = message.payload
                    kind = payload["kind"]
                    if kind == "submission.reserve":
                        continue
                    if kind == "turn.cancel":
                        target = payload["target_turn"]
                        if target["principal"] == self._principal and target["turn_id"] == turn_id:
                            if creation_seq is None:
                                self._invalid_recovery(message.seq, "cancel precedes target creation")
                            terminal = True
                        continue
                    if message.principal != self._principal or payload["turn_id"] != turn_id:
                        continue
                    if kind in {"turn.start", "turn.single"}:
                        if creation_seq is not None:
                            self._invalid_recovery(message.seq, "target has multiple creation events")
                        creation_seq = message.seq
                        last_seq = message.seq
                        terminal = kind == "turn.single"
                    elif not terminal:
                        if creation_seq is None or payload["pre_seq"] != last_seq:
                            self._invalid_recovery(message.seq, "target has a broken or forked chain")
                        if kind in {"turn.append", "turn.reset"}:
                            last_seq = message.seq
                        elif kind == "turn.end":
                            terminal = True
                if page.next_seq == cursor:
                    with self._condition:
                        self._condition.wait_for(lambda: self._state != "READY", _RETRY_SECONDS)
                cursor = page.next_seq
            if creation_seq is None:
                raise TurnNotFoundError("Target turn does not exist in the recovery range")
            if terminal:
                raise TurnWriterStateError("Target turn has already ended")
            return TurnWriter(self, turn_id, creation_seq, last_seq)

    def _invalid_recovery(self, seq, reason):
        failure = make_failure("Fetch", category="protocol",
                               detail=f"Invalid target turn history at seq {seq}: {reason}")
        self._fail(failure)
        raise SyncReadError(failure)


class TurnWriter:
    """State for one streaming turn, held only by its caller."""

    def __init__(self, client, turn_id, creation_seq, last_seq):
        self._client = client
        self._turn_id = turn_id
        self._creation_seq = creation_seq
        self._last_seq = last_seq
        self._state = "open"
        self._lock = threading.Lock()

    @property
    def turn_id(self):
        return self._turn_id

    @property
    def creation_seq(self):
        return self._creation_seq

    def append(self, *, content=(), recipients=(), object_keys=(), extensions=None):
        return self._write("turn.append", content, recipients, object_keys, extensions)

    def reset(self, *, content=(), recipients=(), object_keys=(), extensions=None):
        return self._write("turn.reset", content, recipients, object_keys, extensions)

    def complete(self, *, recipients=(), extensions=None):
        return self._write("turn.end", None, recipients, (), extensions)

    def _write(self, kind, content, recipients, object_keys, extensions):
        with self._lock, self._client._operation():
            if self._state != "open":
                raise TurnWriterStateError(f"Writing object is {self._state}")
            payload = {"kind": kind, "turn_id": self._turn_id, "pre_seq": self._last_seq}
            if kind in {"turn.append", "turn.reset"}:
                payload["content"] = make_content(content)
            frozen = _freeze(payload, recipients, object_keys, extensions)
            try:
                seq = self._client._publish(frozen)
            except PublishFailedError as error:
                if error.uncertain:
                    self._state = "unresolved"
                raise
            self._last_seq = seq
            if kind == "turn.end":
                self._state = "completed"
            return seq


def create_client(events, *, principal, token, channel_id, max_retries=3,
                  channel_validator=None):
    return ChatProtocolClient(events, principal=principal, token=token,
                              channel_id=channel_id, max_retries=max_retries,
                              channel_validator=channel_validator)
