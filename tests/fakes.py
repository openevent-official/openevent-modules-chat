from __future__ import annotations

import threading
from types import SimpleNamespace

import grpc
from openevent.sdk import openevent_pb2


class FakeRpcError(grpc.RpcError):
    def __init__(self, status: grpc.StatusCode):
        super().__init__()
        self._status = status

    def code(self) -> grpc.StatusCode:
        return self._status


class FakeOpenEventClient:
    timeout = 0.2

    def __init__(self, channel_ids=(1001,), messages=()):
        self._lock = threading.Lock()
        self.channel_ids = set(channel_ids)
        self.messages = list(messages)
        self.calls = []
        self.get_status_error = None
        self.get_status_entered = threading.Event()
        self.get_status_release = None
        self.fetch_error = None
        self.publish_error = None
        self.publish_entered = threading.Event()
        self.publish_release = None
        self.rewrite_publish = None

    def get_channel(self, principal, token, channel_id):
        self.calls.append(("get_channel", principal, token, channel_id))
        if channel_id not in self.channel_ids:
            raise FakeRpcError(grpc.StatusCode.NOT_FOUND)
        return SimpleNamespace(channel=SimpleNamespace(channel_id=channel_id, protocol="chat.v1"))

    def get_status(self, principal, token):
        self.calls.append(("get_status", principal, token))
        if self.get_status_release is not None:
            self.get_status_entered.set()
            self.get_status_release.wait(timeout=2)
        if self.get_status_error is not None:
            error, self.get_status_error = self.get_status_error, None
            raise error
        with self._lock:
            max_seq = max((message.seq for message in self.messages), default=0)
        return SimpleNamespace(max_seq=max_seq, min_seq=0 if max_seq == 0 else 1)

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        self.calls.append(("fetch", principal, token, from_seq, limit, only_my_recipient, tuple(channels)))
        if self.fetch_error is not None:
            error, self.fetch_error = self.fetch_error, None
            raise error
        with self._lock:
            snapshot = list(self.messages)
        last_seq = max((message.seq for message in snapshot), default=0)
        if from_seq > last_seq:
            return SimpleNamespace(messages=[], next_seq=from_seq, last_seq=last_seq)
        selected = [message for message in snapshot if message.seq >= from_seq and (not channels or message.channel_id in channels)]
        selected = selected[:limit]
        next_seq = selected[-1].seq + 1 if selected else last_seq + 1
        return SimpleNamespace(messages=selected, next_seq=next_seq, last_seq=last_seq)

    def publish_auto_seq(self, principal, token, channel_id, payload, recipients=(), object_keys=()):
        self.calls.append(("publish", principal, token, channel_id, bytes(payload), tuple(recipients), tuple(object_keys)))
        self.publish_entered.set()
        if self.publish_release is not None:
            self.publish_release.wait(timeout=2)
        if self.publish_error is not None:
            error, self.publish_error = self.publish_error, None
            raise error
        with self._lock:
            seq = max((message.seq for message in self.messages), default=0) + 1
            stored_payload = self.rewrite_publish(bytes(payload)) if self.rewrite_publish else bytes(payload)
            self.messages.append(
                message(
                    seq=seq,
                    channel_id=channel_id,
                    principal=principal,
                    payload=stored_payload,
                    recipients=recipients,
                    object_keys=object_keys,
                )
            )
        return SimpleNamespace(seq=seq)


def message(
    *,
    seq: int,
    channel_id: int = 1001,
    principal: int = 2001,
    payload: bytes,
    recipients=(),
    object_keys=(),
    ts_ms: int = 1,
):
    keys = [
        openevent_pb2.ObjectKey(object_id=int(key.object_id), object_token=str(key.object_token))
        for key in object_keys
    ]
    return SimpleNamespace(
        seq=seq,
        ts_ms=ts_ms,
        channel_id=channel_id,
        principal=principal,
        recipients=list(recipients),
        payload=payload,
        object_keys=keys,
    )
