"""Synchronous Chat calls and caller-owned streaming writers."""
from contextlib import contextmanager
import math
import threading

import grpc
from openevent.sdk.proto import openevent_pb2

from .codec import (encode_payload, make_content, parse_message, validate_content_seqs,
                    validate_turn_id, validate_uint64)
from .errors import (
    ChannelInitializationError, ChatProtocolError, ClientClosedError,
    ClientFailedError, FetchPageError, PublishFailedError,
    TurnContentError, TurnWriterStateError, UuidAllocationError, make_failure,
)
from .model import FetchPage, ObjectKey, TextPart, TurnAttachment, TurnContent, TurnRef


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
    def __init__(self, failure):
        self.failure = failure


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
        for method in ("get_channel", "fetch", "get_uuid",
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
        channel = response.channel
        if channel.protocol != "chat.v1":
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
                    raise _RpcFailure(self._stopped_failure(stage))
            try:
                return call()
            except Exception as exc:
                failure = make_failure(stage, exc, attempts=attempt)
                if on_error is not None:
                    on_error(failure.grpc_code)
                if failure.grpc_code not in _RETRY_CODES or attempt > self._max_retries:
                    raise _RpcFailure(failure) from None
            with self._condition:
                self._condition.wait_for(lambda: self._state != "READY", _RETRY_SECONDS)
                if self._state != "READY":
                    raise _RpcFailure(failure) from None
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
            if error.failure.grpc_code == grpc.StatusCode.ALREADY_EXISTS:
                try:
                    return self._rpc("GetSeqByUuid", lambda: self._events.get_seq_by_uuid(uuid))
                except _RpcFailure as lookup_error:
                    raise PublishFailedError(lookup_error.failure, uuid, uncertain=True) from None
            raise PublishFailedError(error.failure, uuid, uncertain=uncertain) from None
        return response.seq

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
            return TurnWriter(self, turn_id, seq, seq, content_seqs=(seq,))

    def cancel_turn(self, *, target_turn, start_seq, recipients=(), extensions=None):
        with self._operation():
            if not isinstance(target_turn, TurnRef):
                raise ChatProtocolError("target_turn must be TurnRef")
            frozen = _freeze({"kind": "turn.cancel", "start_seq": start_seq, "target_turn": {
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
            return self._fetch_page(from_seq, limit)

    @staticmethod
    def _validate_fetch(from_seq, limit):
        validate_uint64(from_seq, "from_seq", positive=False)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ChatProtocolError("limit must be an integer in 1..1000")

    def _fetch_page(self, from_seq, limit):
        try:
            response = self._rpc("Fetch", lambda: self._events.fetch(
                principal=self._principal, token=self._token, from_seq=from_seq, limit=limit,
                channels=(self._channel_id,), only_my_recipient=False))
        except _RpcFailure as error:
            if error.failure.category in {"protocol", "contract"}:
                self._fail(error.failure)
            raise FetchPageError(error.failure) from None
        messages = []
        for raw in response.messages:
            # System messages share the Channel and cursor, but not chat.v1 payloads.
            if raw.principal == 0:
                continue
            try:
                messages.append(parse_message(raw))
            except ChatProtocolError:
                failure = make_failure("Fetch", category="protocol",
                                       detail=f"Fetch returned invalid chat.v1 data at seq {raw.seq}")
                self._fail(failure)
                raise FetchPageError(failure) from None
        return FetchPage(tuple(messages), response.next_seq, response.last_seq)

    def get_turn_content(self, terminal_seq):
        """Read a caller-confirmed effective terminal event and its final content."""
        with self._operation():
            validate_uint64(terminal_seq, "terminal_seq")
            try:
                return self._get_turn_content(terminal_seq)
            except FetchPageError as error:
                raise TurnContentError(error.failure) from None

    def _turn_content_error(self, detail):
        failure = make_failure("Fetch", category="contract", detail=detail)
        self._fail(failure)
        raise TurnContentError(failure)

    def _read_range(self, start, end):
        cursor = start
        while cursor <= end:
            page = self._fetch_page(cursor, min(1000, end - cursor + 1))
            if cursor > page.last_seq:
                self._turn_content_error("turn content range extends beyond committed history")
            for message in page.messages:
                if message.seq <= end:
                    yield message
                del message
            next_seq = page.next_seq
            del page
            cursor = next_seq

    def _read_content_seqs(self, start, seqs=()):
        # The parsed manifest is strictly increasing and starts at or after start.
        seqs = iter(seqs)
        following = next(seqs, None)
        if following == start:
            following = next(seqs, None)
        while start is not None:
            # Only adjacent global seqs share a range. Other turns may occupy gaps.
            end = start
            while following == end + 1:
                end = following
                following = next(seqs, None)
            expected = start
            for message in self._read_range(start, end):
                if message.seq != expected:
                    self._turn_content_error("turn content is missing a referenced event")
                expected += 1
                yield message
                del message
            if expected <= end:
                self._turn_content_error("turn content is missing a referenced event")
            start = following
            following = next(seqs, None)

    def _read_message(self, seq):
        messages = self._read_content_seqs(seq)
        try:
            return next(messages)
        finally:
            messages.close()

    @staticmethod
    def _extend_content(message, content, attachments):
        content.extend(TextPart(part["text"]) for part in message.payload.get("content", ()))
        attachments.extend(TurnAttachment(key, message.seq) for key in message.object_keys)

    def _get_turn_content(self, terminal_seq):
        terminal = self._read_message(terminal_seq)
        payload = terminal.payload
        kind = payload["kind"]
        if kind not in {"turn.single", "turn.end", "turn.cancel"}:
            self._turn_content_error("terminal_seq does not identify a terminal event")
        target = (TurnRef(**payload["target_turn"]) if kind == "turn.cancel"
                  else TurnRef(terminal.principal, payload["turn_id"]))
        start_seq = terminal_seq if kind == "turn.single" else payload["start_seq"]
        content, attachments = [], []
        error = payload.get("error")
        if kind == "turn.single":
            replies = tuple(payload["reply_to_seqs"])
            self._extend_content(terminal, content, attachments)
        else:
            if start_seq >= terminal_seq:
                self._turn_content_error("turn creation must precede its terminal event")
            if kind == "turn.cancel":
                terminal_keys = terminal.object_keys
                del terminal, payload
                replies = self._cancelled_content(start_seq, terminal_seq, target, content, attachments)
                attachments.extend(TurnAttachment(key, terminal_seq) for key in terminal_keys)
            else:
                seqs = payload.get("content_seqs", ())
                if payload["pre_seq"] >= terminal_seq:
                    self._turn_content_error("turn tail must precede its terminal event")
                del terminal, payload
                previous = None
                for message in self._read_content_seqs(start_seq, seqs):
                    if message.seq == start_seq:
                        self._check_creation(message, target)
                        replies = tuple(message.payload["reply_to_seqs"])
                        if not seqs or seqs[0] != start_seq:
                            # A reset or error end discards the original start's content.
                            del message
                            continue
                    value = message.payload
                    first_kind = "turn.start" if message.seq == start_seq else "turn.reset"
                    if (message.principal != target.principal or value.get("turn_id") != target.turn_id
                            or value["kind"] != (first_kind if previous is None else "turn.append")
                            or (previous is not None and value["pre_seq"] != previous)):
                        self._turn_content_error("turn content list does not describe its final content chain")
                    self._extend_content(message, content, attachments)
                    previous = message.seq
                    del message, value
        return TurnContent(target, start_seq, terminal_seq,
                           "cancelled" if kind == "turn.cancel" else "completed",
                           tuple(content), tuple(attachments), replies, error)

    def _check_creation(self, creation, target):
        if (creation.payload["kind"] != "turn.start" or creation.principal != target.principal
                or creation.payload["turn_id"] != target.turn_id):
            self._turn_content_error("turn creation does not match its terminal event")

    def _cancelled_content(self, start_seq, terminal_seq, target, content, attachments):
        replies, tail = None, start_seq
        for message in self._read_range(start_seq, terminal_seq - 1):
            value = message.payload
            kind = value["kind"]
            if message.seq == start_seq:
                self._check_creation(message, target)
                replies = tuple(value["reply_to_seqs"])
                self._extend_content(message, content, attachments)
            elif kind == "turn.cancel":
                if TurnRef(**value["target_turn"]) == target:
                    self._turn_content_error("turn was already cancelled before terminal_seq")
            elif message.principal == target.principal and value.get("turn_id") == target.turn_id:
                if replies is None or kind not in {"turn.append", "turn.reset"} or value["pre_seq"] != tail:
                    self._turn_content_error("cancelled turn has an incomplete content chain or earlier terminal event")
                if kind == "turn.reset":
                    content.clear()
                    attachments.clear()
                self._extend_content(message, content, attachments)
                tail = message.seq
            del message, value
        if replies is None:
            self._turn_content_error("cancelled turn is missing its creation event")
        return replies


class TurnWriter:
    """Continue a known open turn without reading or publishing any history.

    The caller supplies the confirmed start and current tail in the client's
    Channel, owned by its principal, after completing any previous writes.
    Construction validates local arguments only; each write checks the client's
    lifecycle. The caller remains responsible for observing end/cancel events.
    """

    def __init__(self, client, turn_id, start_seq, last_seq, *, content_seqs):
        if not isinstance(client, ChatProtocolClient):
            raise ChatProtocolError("client must be ChatProtocolClient")
        self._client = client
        self._turn_id = validate_turn_id(turn_id)
        self._start_seq = validate_uint64(start_seq, "start_seq")
        self._last_seq = validate_uint64(last_seq, "last_seq")
        if self._last_seq < self._start_seq:
            raise ChatProtocolError("last_seq must not precede start_seq")
        self._content_seqs = list(_items(content_seqs, "content_seqs"))
        validate_content_seqs(self._content_seqs, self._start_seq, self._last_seq)
        self._state = "open"
        self._lock = threading.Lock()

    @property
    def turn_id(self):
        return self._turn_id

    @property
    def start_seq(self):
        return self._start_seq

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
            else:
                payload.update(start_seq=self._start_seq, content_seqs=list(self._content_seqs))
            frozen = _freeze(payload, recipients, object_keys, extensions)
            try:
                try:
                    seq = self._client._publish(frozen)
                except PublishFailedError as error:
                    if (kind != "turn.end" or error.uncertain
                            or error.failure.stage != "PublishAutoSeq"
                            or error.failure.grpc_code != grpc.StatusCode.RESOURCE_EXHAUSTED):
                        raise
                    # The original end definitely did not commit. Keep the same
                    # chain tail, omit the oversized list and user extensions.
                    fallback = dict(kind=kind, turn_id=self._turn_id, start_seq=self._start_seq,
                                    pre_seq=self._last_seq, error="turn_too_long")
                    seq = self._client._publish(_freeze(fallback, frozen[1], (), None))
            except PublishFailedError as error:
                if error.uncertain:
                    self._state = "unresolved"
                raise
            self._last_seq = seq
            if kind == "turn.end":
                self._state = "completed"
                self._content_seqs.clear()
            elif kind == "turn.reset":
                self._content_seqs = [seq]
            else:
                self._content_seqs.append(seq)
            return seq


def create_client(events, *, principal, token, channel_id, max_retries=3,
                  channel_validator=None):
    return ChatProtocolClient(events, principal=principal, token=token,
                              channel_id=channel_id, max_retries=max_retries,
                              channel_validator=channel_validator)
