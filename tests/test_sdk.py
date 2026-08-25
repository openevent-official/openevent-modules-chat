from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

import grpc

from openevent.chat_sdk import (
    ChatProtocolError,
    ClientClosedError,
    ClientFailedError,
    ObjectKey,
    TextPart,
    TurnRef,
    ConversationStateError,
    create_client,
    parse_payload,
)


class RpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code


class FakeOpenEvent:
    timeout = 1.0

    def __init__(self):
        self.messages = []
        self.next_uuid = 1
        self.fetch_error = None
        self.publish_release = None

    def get_channel(self, **kwargs):
        return SimpleNamespace(channel=SimpleNamespace(channel_id=kwargs["channel_id"], protocol="chat.v1"))

    def get_status(self, **kwargs):
        return SimpleNamespace(max_seq=max((m.seq for m in self.messages), default=0))

    def fetch(self, *, from_seq, limit, channels, **kwargs):
        if self.fetch_error is not None:
            error, self.fetch_error = self.fetch_error, None
            raise error
        last_seq = max((m.seq for m in self.messages), default=0)
        if from_seq == 0 or from_seq > last_seq:
            return SimpleNamespace(messages=[], next_seq=last_seq + 1, last_seq=last_seq)
        selected = [m for m in self.messages if m.seq >= from_seq and m.channel_id in channels][:limit]
        if not selected:
            return SimpleNamespace(messages=[], next_seq=last_seq + 1, last_seq=last_seq)
        return SimpleNamespace(messages=selected, next_seq=selected[-1].seq + 1, last_seq=last_seq)

    def get_uuid(self):
        value, self.next_uuid = self.next_uuid, self.next_uuid + 1
        return value

    def publish_auto_seq(self, *, principal, token, channel_id, payload, uuid, recipients, object_keys):
        if self.publish_release is not None:
            self.publish_release.wait(timeout=2)
        seq = max((m.seq for m in self.messages), default=0) + 1
        self.messages.append(
            SimpleNamespace(
                seq=seq,
                ts_ms=1,
                channel_id=channel_id,
                principal=principal,
                recipients=list(recipients),
                object_keys=list(object_keys),
                payload=payload,
            )
        )
        return SimpleNamespace(seq=seq)


class RetryFailingUuidOpenEvent(FakeOpenEvent):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def get_uuid(self):
        self.started.set()
        raise RpcError(grpc.StatusCode.UNAVAILABLE)


class RetryFailingPublishOpenEvent(FakeOpenEvent):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def publish_auto_seq(self, **kwargs):
        self.started.set()
        raise RpcError(grpc.StatusCode.UNAVAILABLE)


class SdkTest(unittest.TestCase):
    def make_client(self, fake=None, **kwargs):
        fake = fake or FakeOpenEvent()
        client = create_client(fake, principal=10, token="token", channel_id=7, on_failure=lambda error: kwargs.setdefault("failure", error), **{k: v for k, v in kwargs.items() if k != "failure"})
        self.addCleanup(client.close)
        return fake, client

    def test_codec_supports_single_and_strict_json(self):
        event = parse_payload(b'{"kind":"turn.single","turn_id":"t","reply_to_turns":[],"content":[]}')
        self.assertEqual(event.kind, "turn.single")
        with self.assertRaises(ChatProtocolError):
            parse_payload(b'{"kind":"turn.single","kind":"turn.start","turn_id":"t","reply_to_turns":[],"content":[]}')

    def test_write_returns_without_waiting_for_internal_fetch(self):
        fake, client = self.make_client()
        fake.publish_release = threading.Event()
        result = []

        thread = threading.Thread(target=lambda: result.append(client.single_turn(turn_id="one", reply_to_turns=(), content=())))
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        fake.publish_release.set()
        thread.join(timeout=1)
        self.assertEqual(result, [1])

    def test_chain_and_subscription(self):
        fake, client = self.make_client()
        first = client.single_turn(turn_id="one", reply_to_turns=(), content=(TextPart("hello"),))
        second = client.start_turn(turn_id="two", reply_to_turns=(TurnRef(10, "one"),), content=(TextPart("a"),))
        appended = client.append_turn(turn_id="two", content=(TextPart("b"),))
        ended = client.complete_turn(turn_id="two")
        self.assertEqual((first, second, appended, ended), (1, 2, 3, 4))
        received = []
        subscription = client.register_subscription_callback(received.append, from_seq=1)
        self.addCleanup(subscription.close)
        subscription.wait_until_scanned(4)
        self.assertEqual([item.seq for item in received], [1, 2, 3, 4])

    def test_single_turn_cannot_be_appended_or_completed(self):
        _, client = self.make_client()
        client.single_turn(turn_id="one", reply_to_turns=(), content=())
        with self.assertRaises(ConversationStateError):
            client.append_turn(turn_id="one", content=(TextPart("x"),))
        with self.assertRaises(ConversationStateError):
            client.complete_turn(turn_id="one")

    def test_fetch_failure_calls_failure_and_fails_client(self):
        failures = []
        fake = FakeOpenEvent()
        client = create_client(fake, principal=10, token="token", channel_id=7, max_retries=0, on_failure=failures.append)
        self.addCleanup(client.close)
        fake.fetch_error = RpcError(grpc.StatusCode.UNAVAILABLE)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not failures:
            time.sleep(0.01)
        self.assertEqual(len(failures), 1)
        with self.assertRaises(ClientFailedError):
            client.single_turn(turn_id="x", reply_to_turns=(), content=())

    def test_close_during_uuid_retry_preserves_lifecycle_error(self):
        fake = RetryFailingUuidOpenEvent()
        client = create_client(fake, principal=10, token="token", channel_id=7, on_failure=lambda error: None)
        result = []

        thread = threading.Thread(
            target=lambda: self._capture_error(
                result,
                lambda: client.single_turn(turn_id="x", reply_to_turns=(), content=()),
            )
        )
        thread.start()
        self.assertTrue(fake.started.wait(timeout=1))
        client.close()
        thread.join(timeout=1)
        self.assertIsInstance(result[0], ClientClosedError)

    def test_close_during_publish_retry_preserves_lifecycle_error(self):
        fake = RetryFailingPublishOpenEvent()
        client = create_client(fake, principal=10, token="token", channel_id=7, on_failure=lambda error: None)
        result = []

        thread = threading.Thread(
            target=lambda: self._capture_error(
                result,
                lambda: client.single_turn(turn_id="x", reply_to_turns=(), content=()),
            )
        )
        thread.start()
        self.assertTrue(fake.started.wait(timeout=1))
        client.close()
        thread.join(timeout=1)
        self.assertIsInstance(result[0], ClientClosedError)

    @staticmethod
    def _capture_error(result, operation):
        try:
            operation()
        except Exception as exc:
            result.append(exc)

    def test_object_key_is_redacted(self):
        self.assertNotIn("secret", repr(ObjectKey(1, "secret")))


if __name__ == "__main__":
    unittest.main()
