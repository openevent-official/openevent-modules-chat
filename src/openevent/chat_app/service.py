"""Chat application operations; no background history or subscription state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading

from openevent.chat_sdk import ObjectKey, TextPart, TurnRef, create_client
from openevent.chat_sdk.errors import make_failure
from openevent.sdk import OpenEventClient

from .config import ConfigStore, MAX_UINT64, ServerConfig, SessionConfig, new_ulid, uint64_string, valid_ulid

LOG = logging.getLogger(__name__)
MAX_OBJECT_BYTES = 4 * 1024 * 1024


class AppError(Exception):
    def __init__(self, status, code, message, failure=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.failure = failure

    def as_json(self):
        error = {"code": self.code, "message": self.message}
        if self.failure is not None:
            error["failure"] = failure_json(self.failure)
        return {"error": error}


def failure_json(failure):
    return {"stage": failure.stage, "category": failure.category,
            "grpc_code": failure.grpc_code.name if failure.grpc_code is not None else None,
            "retryable": failure.retryable, "detail": failure.detail}


def bad_request(message="request is invalid"):
    return AppError(400, "invalid_request", message)


def fields(body, required, optional=()):
    if not isinstance(body, dict) or not set(required) <= set(body) or set(body) - set(required) - set(optional):
        raise bad_request("request fields are invalid")


def number(value):
    try:
        return uint64_string(value)
    except (ValueError, TypeError):
        raise bad_request("canonical positive uint64 string required") from None


def text_value(value, *, nonempty=False, max_bytes=None):
    if not isinstance(value, str) or (nonempty and not value):
        raise bad_request("invalid text field")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise bad_request("invalid text encoding") from None
    if max_bytes is not None and size > max_bytes:
        raise bad_request("text field exceeds its byte limit")
    return value


@dataclass
class SessionWorker:
    config: SessionConfig
    chat: object
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    batch_start: int = 0
    batch_end: int = 0
    next_to_issue: int = 0
    submissions: dict = field(default_factory=dict)

    def contains(self, value):
        return self.batch_start != 0 and self.batch_start <= value <= self.batch_end

    def public_config(self):
        return {"session_id": self.config.session_id, "scan_start_seq": str(self.config.scan_start_seq)}


class ChatService:
    def __init__(self, config: ServerConfig, events_client=None, *, chat_factory=create_client, on_fatal=None, owns_events=False):
        self.config = config
        self.events = events_client if events_client is not None else OpenEventClient(config.openevent_target, timeout_ms=config.rpc_timeout_ms)
        self._owns_events = events_client is None or owns_events
        self._events_closed = False
        self._transport_lock = threading.Lock()
        self.chat_factory = chat_factory
        self.on_fatal = on_fatal
        self.fatal = threading.Event()
        self.failure = None
        self._fatal_lock = threading.Lock()
        self._index_lock = threading.RLock()
        self._create_lock = threading.Lock()
        self._uploads_lock = threading.Lock()
        self.uploads = {}
        self.sessions = {}
        self.store = None
        try:
            self.store = ConfigStore(config.channels_dir)
            for record in self.store.sessions.values():
                self.sessions[record.session_id] = self._worker(record)
        except Exception:
            self.close()
            raise

    def ensure_running(self):
        if self.fatal.is_set():
            raise AppError(503, "server_unavailable", "chat server is stopping", self.failure)

    def _fail(self, failure, *, session_id=None, channel_id=None, uuid=None,
              from_seq=None, event_seq=None, object_id=None, filename=None):
        with self._fatal_lock:
            first = not self.fatal.is_set()
            if first:
                self.failure = failure
                self.fatal.set()
        if first:
            # One shared transport gates every session, including retries already
            # inside a Chat SDK invocation. Stop it before waiting on any client.
            if self._owns_events:
                self._close_events()
            record = {"failure": failure_json(failure)}
            if session_id is not None:
                record["session_id"] = session_id
                if channel_id is None:
                    with self._index_lock:
                        worker = self.sessions.get(session_id)
                    if worker is not None:
                        channel_id = worker.config.channel_id
            for name, value in (("channel_id", channel_id), ("uuid", uuid), ("from_seq", from_seq),
                                ("event_seq", event_seq), ("object_id", object_id), ("filename", filename)):
                if value is not None:
                    record[name] = str(value)
            LOG.error("chat server fatal: %s", json.dumps(record, ensure_ascii=False))
            if self.on_fatal is not None:
                self.on_fatal()
        return AppError(503, "server_unavailable", "chat server is stopping", failure)

    def _contract(self, stage, detail, session_id=None, *, event_seq=None, object_id=None):
        raise self._fail(make_failure(stage, category="contract", detail=detail), session_id=session_id,
                         event_seq=event_seq, object_id=object_id)

    def _call(self, stage, operation, *, fatal=False, session_id=None, channel_id=None,
              from_seq=None, event_seq=None, object_id=None, filename=None):
        self.ensure_running()
        try:
            result = operation()
        except AppError:
            raise
        except Exception as exc:
            failure = make_failure(stage, exc)
            if fatal or failure.category != "external_unavailable":
                raise self._fail(failure, session_id=session_id, channel_id=channel_id,
                                 uuid=getattr(exc, "uuid", None), from_seq=from_seq, event_seq=event_seq,
                                 object_id=object_id, filename=filename) from exc
            raise AppError(503, "server_unavailable", "external service temporarily unavailable", failure) from exc
        self.ensure_running()
        return result

    def _rpc(self, stage, method, *, fatal=False, session_id=None, event_seq=None, filename=None, **kwargs):
        return self._call(stage, lambda: method(**kwargs), fatal=fatal, session_id=session_id,
                          event_seq=event_seq, object_id=kwargs.get("object_id"), filename=filename)

    def _worker(self, record):
        chat = self._call("GetChannel", lambda: self.chat_factory(
            self.events, principal=self.config.user_principal, token=self.config.user_openevent_token,
            channel_id=record.channel_id, max_retries=self.config.max_retries,
            channel_validator=lambda channel: self._check_channel(channel, record.session_id, record.channel_id)), fatal=True,
            session_id=record.session_id, channel_id=record.channel_id, filename=record.session_id + ".json")
        return SessionWorker(record, chat)

    def _check_channel(self, channel, session_id, channel_id=None):
        expected = {self.config.user_principal, self.config.agent_principal}
        try:
            valid = (type(channel.channel_id) is int and 0 < channel.channel_id <= MAX_UINT64
                     and (channel_id is None or channel.channel_id == channel_id)
                     and channel.name == "chat-" + session_id and channel.protocol == "chat.v1"
                     and channel.visibility == 2 and channel.description == ""
                     and channel.creator == self.config.user_principal
                     and len(channel.members) == 2 and set(channel.members) == expected)
        except (AttributeError, TypeError):
            valid = False
        if not valid:
            raise ValueError("ChannelInfo does not match immutable session configuration")

    def _session(self, session_id):
        self.ensure_running()
        with self._index_lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise AppError(404, "session_not_found", "session does not exist")
        return session

    def list_sessions(self):
        self.ensure_running()
        with self._index_lock:
            return {"sessions": [self.sessions[key].public_config() for key in sorted(self.sessions)]}

    def create_session(self, body):
        fields(body, {"create_request_id"})
        try:
            request_id = valid_ulid(body["create_request_id"])
        except ValueError:
            raise bad_request("canonical create_request_id required") from None
        with self._create_lock:
            self.ensure_running()
            sid = self.store.requests.get(request_id)
            if sid is not None:
                return 200, self._session(sid).public_config()
            sid = new_ulid()
            status = self._rpc("GetStatus", self.events.get_status, principal=self.config.user_principal,
                               token=self.config.user_openevent_token, session_id=sid)
            maximum = self._status_seq(status, sid)
            if maximum == MAX_UINT64:
                raise AppError(503, "sequence_exhausted", "no session scan position remains")
            channel_id = None
            try:
                pending = self.store.begin(sid, request_id, maximum + 1)
                result = self._rpc("CreateChannel", self.events.create_channel, fatal=True,
                                   principal=self.config.user_principal, token=self.config.user_openevent_token,
                                   name="chat-" + sid, visibility=2, protocol="chat.v1", description="",
                                   members=(self.config.agent_principal,), session_id=sid, filename=sid + ".json")
                self._call("CreateChannel", lambda: self._check_channel(result.channel, sid),
                           fatal=True, session_id=sid, filename=sid + ".json")
                channel_id = result.channel.channel_id
                pending = self.store.record_channel(pending, channel_id)
                record = self.store.commit(pending)
                worker = self._worker(record)
                with self._index_lock:
                    self.ensure_running()
                    self.sessions[sid] = worker
                return 201, worker.public_config()
            except AppError:
                raise
            except Exception as exc:
                raise self._fail(make_failure("CreateSession", exc, detail="session configuration transaction failed"),
                                 session_id=sid, channel_id=channel_id, filename=sid + ".json") from exc

    def _status_seq(self, status, session_id):
        value = getattr(status, "max_seq", None)
        if type(value) is not int or not 0 <= value <= MAX_UINT64:
            self._contract("GetStatus", "GetStatus returned invalid max_seq", session_id)
        return value

    def _page(self, worker, start, limit):
        page = self._call("Fetch", lambda: worker.chat.fetch_page(from_seq=start, limit=limit),
                          session_id=worker.config.session_id, from_seq=start)
        for message in page.messages:
            self._role(message.principal, worker.config.session_id, event_seq=message.seq)
            for recipient in message.recipients:
                self._role(recipient, worker.config.session_id, event_seq=message.seq)
            if message.payload["kind"] == "turn.cancel":
                self._role(message.payload["target_turn"]["principal"], worker.config.session_id, event_seq=message.seq)
        return page

    def _role(self, principal, session_id=None, *, event_seq=None):
        if principal == self.config.user_principal:
            return "user"
        if principal == self.config.agent_principal:
            return "agent"
        self._contract("Fetch", "event identity is not a configured Channel member", session_id, event_seq=event_seq)

    def _public_event(self, message):
        # Only replace top-level fields; content and extensions stay read-only.
        payload = dict(message.payload)
        for name in ("pre_seq", "reserved_through"):
            if name in payload:
                payload[name] = str(payload[name])
        if "reply_to_seqs" in payload:
            payload["reply_to_seqs"] = [str(value) for value in payload["reply_to_seqs"]]
        if "target_turn" in payload:
            target = payload["target_turn"]
            payload["target_turn"] = {"role": self._role(target["principal"]), "turn_id": target["turn_id"]}
        return {"event_seq": str(message.seq), "ts_ms": str(message.ts_ms),
                "publisher_role": self._role(message.principal),
                "recipients": [self._role(value) for value in message.recipients],
                "attachments": [{"object_id": str(key.object_id)} for key in message.object_keys], "payload": payload}

    def history(self, session_id, fetch_seq, limit=100):
        worker = self._session(session_id)
        start = number(fetch_seq)
        if start < worker.config.scan_start_seq or type(limit) is not int or not 1 <= limit <= 1000:
            raise bad_request("history range is invalid")
        page = self._page(worker, start, limit)
        return {"next_seq": str(page.next_seq), "last_seq": str(page.last_seq),
                "events": [self._public_event(message) for message in page.messages]}

    def allocate(self, session_id, body):
        worker = self._session(session_id)
        fields(body, {"count"})
        count = number(body["count"])
        with worker.write_lock:
            self.ensure_running()
            if worker.batch_start == 0:
                status = self._rpc("GetStatus", self.events.get_status, principal=self.config.user_principal,
                                   token=self.config.user_openevent_token, session_id=session_id)
                boundary = self._status_seq(status, session_id)
                cursor, upper = worker.config.scan_start_seq, 0
                while cursor <= boundary:
                    page = self._page(worker, cursor, 1000)
                    for message in page.messages:
                        if message.seq <= boundary and message.principal == self.config.user_principal and message.payload["kind"] == "submission.reserve":
                            upper = max(upper, message.payload["reserved_through"])
                    cursor = page.next_seq
            else:
                upper = worker.batch_end
            remaining = worker.batch_end - worker.next_to_issue + 1 if worker.batch_start else 0
            if remaining < count:
                if MAX_UINT64 - upper < count:
                    raise AppError(503, "submission_id_exhausted", "no sufficient submission numbers remain")
                new_end = upper + min(max(10000, count), MAX_UINT64 - upper)
                self._call("PublishAutoSeq", lambda: worker.chat.reserve_submissions(reserved_through=new_end),
                           fatal=True, session_id=session_id)
                with worker.state_lock:
                    worker.batch_start, worker.batch_end, worker.next_to_issue = upper + 1, new_end, upper + 1
                    worker.submissions = {}
            with worker.state_lock:
                start = worker.next_to_issue
                end = start + count - 1
                worker.next_to_issue = end + 1
            return {"start": str(start), "end": str(end)}

    def send(self, session_id, body):
        worker = self._session(session_id)
        fields(body, {"submission_id", "text"}, {"reply_to_seqs", "attachments"})
        submission = number(body["submission_id"])
        text = text_value(body["text"])
        replies = body.get("reply_to_seqs", [])
        attachments = body.get("attachments", [])
        if not isinstance(replies, list) or not isinstance(attachments, list) or len(attachments) > 1024:
            raise bad_request("invalid reply or attachment array")
        replies = [number(value) for value in replies]
        objects = [number(value) for value in attachments]
        if len(set(replies)) != len(replies):
            raise bad_request("duplicate reply seq")
        with worker.write_lock:
            self.ensure_running()
            with worker.state_lock:
                if not worker.contains(submission):
                    raise AppError(409, "submission_out_of_range", "submission number is outside the current batch")
            with self._uploads_lock:
                keys = []
                for object_id in objects:
                    record = self.uploads.get(object_id)
                    if record is None or record[1] != session_id:
                        raise AppError(409, "attachment_unavailable", "upload record is unavailable; upload the file again")
                    keys.append(ObjectKey(object_id, record[0]))
            with worker.state_lock:
                worker.submissions[submission] = None
            turn_id = "user:" + str(submission)
            seq = self._call("PublishAutoSeq", lambda: worker.chat.single_turn(
                turn_id=turn_id, content=(TextPart(text),) if text else (), reply_to_seqs=tuple(replies),
                object_keys=tuple(keys)), fatal=True, session_id=session_id)
            with worker.state_lock:
                worker.submissions[submission] = seq
            return {"status": "committed", "submission_id": str(submission),
                    "turn_ref": {"role": "user", "turn_id": turn_id}}

    def submission(self, session_id, submission_id):
        worker = self._session(session_id)
        submission = number(submission_id)
        with worker.state_lock:
            if not worker.contains(submission):
                raise AppError(409, "submission_out_of_range", "submission number is outside the current batch")
            if submission not in worker.submissions:
                raise AppError(404, "submission_not_observed", "submission has not been admitted")
            seq = worker.submissions[submission]
        if seq is None:
            return 202, {"submission_id": str(submission), "status": "processing"}
        return 200, {"submission_id": str(submission), "seq": str(seq)}

    def cancel(self, session_id, body):
        worker = self._session(session_id)
        fields(body, {"target_turn_id"})
        target = text_value(body["target_turn_id"], nonempty=True, max_bytes=128)
        with worker.write_lock:
            self._call("PublishAutoSeq", lambda: worker.chat.cancel_turn(target_turn=TurnRef(self.config.agent_principal, target)),
                       fatal=True, session_id=session_id)
        return {"status": "committed"}

    def upload(self, session_id, *, name, content_type, data):
        self._session(session_id)
        name = text_value(name, nonempty=True, max_bytes=255)
        content_type = text_value(content_type or "application/octet-stream", nonempty=True, max_bytes=255)
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_OBJECT_BYTES:
            raise bad_request("file must contain between 1 byte and 4 MiB")
        result = self._rpc("WriteObject", self.events.write_object,
                           principal=self.config.user_principal, token=self.config.user_openevent_token,
                           name=name, type=content_type, data=data, description="", session_id=session_id)
        object_id, token = getattr(result, "object_id", None), getattr(result, "object_token", None)
        if type(object_id) is not int or not 0 < object_id <= MAX_UINT64 or not isinstance(token, str) or not token:
            self._contract("WriteObject", "WriteObject returned an invalid ObjectKey", session_id)
        with self._uploads_lock:
            self.uploads[object_id] = (token, session_id)
        return {"object_id": str(object_id), "name": name, "type": content_type, "description": "", "nbytes": len(data)}

    def _attachment(self, session_id, object_id, event_seq):
        worker = self._session(session_id)
        object_id, event_seq = number(object_id), number(event_seq)
        if event_seq < worker.config.scan_start_seq:
            raise bad_request("attachment event precedes this session")
        page = self._page(worker, event_seq, 1)
        for message in page.messages:
            if message.seq == event_seq:
                for key in message.object_keys:
                    if key.object_id == object_id:
                        return key
        raise AppError(404, "attachment_reference_not_found", "event does not reference this attachment")

    def _metadata(self, key, session_id, event_seq):
        metadata = self._rpc("GetObjectMetadata", self.events.get_object_metadata,
                             object_id=key.object_id, object_token=key.object_token,
                             session_id=session_id, event_seq=event_seq)
        try:
            text_value(metadata.name, nonempty=True, max_bytes=255)
            text_value(metadata.type, nonempty=True, max_bytes=255)
            text_value(metadata.description, max_bytes=4096)
            if type(metadata.nbytes) is not int or not 1 <= metadata.nbytes <= MAX_OBJECT_BYTES:
                raise ValueError("invalid object size")
        except (AttributeError, ValueError, AppError):
            self._contract("GetObjectMetadata", "GetObjectMetadata returned invalid metadata", session_id,
                           event_seq=event_seq, object_id=key.object_id)
        return {"object_id": str(key.object_id), "name": metadata.name, "type": metadata.type,
                "description": metadata.description, "nbytes": metadata.nbytes}

    def metadata(self, session_id, object_id, event_seq):
        return self._metadata(self._attachment(session_id, object_id, event_seq), session_id, event_seq)

    def download(self, session_id, object_id, event_seq):
        key = self._attachment(session_id, object_id, event_seq)
        metadata = self._metadata(key, session_id, event_seq)
        response = self._rpc("ReadObject", self.events.read_object, object_id=key.object_id,
                             object_token=key.object_token, offset=0, nbytes=metadata["nbytes"],
                             session_id=session_id, event_seq=event_seq)
        data = getattr(response, "data", None)
        if not isinstance(data, bytes) or len(data) != metadata["nbytes"]:
            self._contract("ReadObject", "ReadObject returned an incomplete object", session_id,
                           event_seq=event_seq, object_id=key.object_id)
        return metadata, data

    def close(self):
        for worker in list(self.sessions.values()):
            worker.chat.close()
        if self.store is not None:
            self.store.close()
        if self._owns_events:
            self._close_events()

    def _close_events(self):
        with self._transport_lock:
            if not self._events_closed:
                self.events.close()
                self._events_closed = True
