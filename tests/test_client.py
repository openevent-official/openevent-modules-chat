from __future__ import annotations

import json
import threading
import time
import unittest

import grpc

from openevent.chat_sdk import (
    ChannelInitializationError,
    ChannelNotManagedError,
    ChatProtocolError,
    ClientFailedError,
    ObjectKey,
    PublishCommittedSyncError,
    PublishFailedError,
    SyncReadError,
    TextPart,
    TurnAlreadyExistsError,
    TurnBusyError,
    TurnNotFoundError,
    TurnRef,
    create_client,
    parse_payload,
)

from tests.fakes import FakeOpenEventClient, FakeRpcError, message


START = b'{"kind":"turn.start","turn_id":"t-1","reply_to_turns":[],"content":[{"type":"text","text":"a"}]}'
APPEND = b'{"kind":"turn.append","turn_id":"t-1","pre_seq":1,"content":[{"type":"text","text":"b"}]}'


class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clients = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()

    def create(self, fake=None, **kwargs):
        fake = fake or FakeOpenEventClient()
        client = create_client(fake, principal=2001, token="principal-token", channel_ids=kwargs.get("channel_ids", (1001,)))
        self.clients.append(client)
        return fake, client

    def test_recovers_history_and_uses_current_tail(self) -> None:
        fake = FakeOpenEventClient(messages=(message(seq=1, payload=START), message(seq=2, payload=APPEND)))
        _, client = self.create(fake)
        seq = client.append_turn(channel_id=1001, turn_id="t-1", content=(TextPart("c"),))
        self.assertEqual(seq, 3)
        event = parse_payload(fake.messages[-1].payload)
        self.assertEqual(event.pre_seq, 2)

    def test_same_turn_id_from_different_principals_stays_isolated(self) -> None:
        other_start = message(seq=2, principal=3001, payload=START)
        fake = FakeOpenEventClient(messages=(message(seq=1, principal=2001, payload=START), other_start))
        _, client = self.create(fake)
        seq = client.append_turn(channel_id=1001, turn_id="t-1", content=(TextPart("owner append"),))
        self.assertEqual(seq, 3)
        self.assertEqual(parse_payload(fake.messages[-1].payload).pre_seq, 1)

    def test_terminal_events_do_not_move_the_saved_chain_tail(self) -> None:
        cancel = b'{"kind":"turn.cancel","target_turn":{"principal":2001,"turn_id":"t-1"}}'
        post_terminal_append = b'{"kind":"turn.append","turn_id":"t-1","pre_seq":1,"content":[{"type":"text","text":"ignored"}]}'
        fake = FakeOpenEventClient(
            messages=(
                message(seq=1, payload=START),
                message(seq=2, principal=3001, payload=cancel),
                message(seq=3, payload=post_terminal_append),
            )
        )
        _, client = self.create(fake)
        seq = client.append_turn(channel_id=1001, turn_id="t-1", content=(TextPart("also ignored"),))
        self.assertEqual(seq, 4)
        self.assertEqual(parse_payload(fake.messages[-1].payload).pre_seq, 1)

    def test_recovery_advances_across_filtered_empty_page(self) -> None:
        unrelated = message(seq=1, channel_id=9999, principal=3001, payload=START)
        fake = FakeOpenEventClient(channel_ids=(1001,), messages=(unrelated,))
        _, client = self.create(fake)
        seq = client.start_turn(channel_id=1001, turn_id="local", reply_to_turns=(), content=(TextPart("x"),))
        self.assertEqual(seq, 2)

    def test_start_append_end_cancel_and_envelope_values(self) -> None:
        fake, client = self.create()
        key = ObjectKey(99, "object-secret")
        start_seq = client.start_turn(
            channel_id=1001,
            turn_id="t-1",
            reply_to_turns=(),
            content=(TextPart("hello"),),
            recipients=(3001, 3001),
            object_keys=(key,),
            extensions={"ui": "visible"},
        )
        append_seq = client.append_turn(channel_id=1001, turn_id="t-1", content=(TextPart(" world"),))
        end_seq = client.complete_turn(channel_id=1001, turn_id="t-1")
        cancel_seq = client.cancel_turn(channel_id=1001, target_turn=TurnRef(2001, "t-1"))
        self.assertEqual((start_seq, append_seq, end_seq, cancel_seq), (1, 2, 3, 4))
        self.assertEqual(fake.messages[0].recipients, [3001, 3001])
        self.assertEqual(fake.messages[0].object_keys[0].object_token, "object-secret")
        self.assertEqual(parse_payload(fake.messages[1].payload).pre_seq, 1)
        self.assertEqual(parse_payload(fake.messages[2].payload).pre_seq, 2)

    def test_client_uses_all_channels_and_unfiltered_fetch(self) -> None:
        fake, client = self.create(channel_ids=(1001, 1002), fake=FakeOpenEventClient(channel_ids=(1001, 1002)))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            fetches = [call for call in fake.calls if call[0] == "fetch"]
            if fetches:
                break
            time.sleep(0.005)
        self.assertTrue(fetches)
        self.assertFalse(fetches[0][5])
        self.assertEqual(fetches[0][6], (1001, 1002))

    def test_input_collections_are_frozen_before_publish_completes(self) -> None:
        fake, client = self.create()
        fake.publish_release = threading.Event()
        recipients = [3001]
        object_keys = [ObjectKey(99, "secret")]
        extensions = {"ui": {"state": "before"}}
        outcome = []

        def publish():
            try:
                outcome.append(
                    client.start_turn(
                        channel_id=1001,
                        turn_id="frozen",
                        reply_to_turns=(),
                        content=(TextPart("x"),),
                        recipients=recipients,
                        object_keys=object_keys,
                        extensions=extensions,
                    )
                )
            except Exception as exc:
                outcome.append(exc)

        thread = threading.Thread(target=publish)
        thread.start()
        self.assertTrue(fake.publish_entered.wait(timeout=1))
        recipients.append(3002)
        object_keys.append(ObjectKey(100, "other"))
        extensions["ui"]["state"] = "after"
        fake.publish_release.set()
        thread.join(timeout=2)

        self.assertEqual(outcome, [1])
        self.assertEqual(fake.messages[0].recipients, [3001])
        self.assertEqual(len(fake.messages[0].object_keys), 1)
        self.assertEqual(json.loads(fake.messages[0].payload)["extensions"]["ui"]["state"], "before")

    def test_object_key_count_boundary(self) -> None:
        fake, client = self.create()
        keys = tuple(ObjectKey(index + 1, f"token-{index}") for index in range(1024))
        self.assertEqual(
            client.start_turn(
                channel_id=1001,
                turn_id="max-keys",
                reply_to_turns=(),
                content=(TextPart("x"),),
                object_keys=keys,
            ),
            1,
        )
        with self.assertRaises(ChatProtocolError):
            client.start_turn(
                channel_id=1001,
                turn_id="too-many-keys",
                reply_to_turns=(),
                content=(TextPart("x"),),
                object_keys=keys + (ObjectKey(2000, "extra"),),
            )
        self.assertEqual(len([call for call in fake.calls if call[0] == "publish"]), 1)

    def test_state_validation_happens_before_publish(self) -> None:
        fake, client = self.create()
        with self.assertRaises(TurnNotFoundError):
            client.append_turn(channel_id=1001, turn_id="missing", content=(TextPart("x"),))
        with self.assertRaises(TurnNotFoundError):
            client.start_turn(
                channel_id=1001,
                turn_id="child",
                reply_to_turns=(TurnRef(9001, "missing"),),
                content=(TextPart("x"),),
            )
        client.start_turn(channel_id=1001, turn_id="t-1", reply_to_turns=(), content=(TextPart("x"),))
        with self.assertRaises(TurnAlreadyExistsError):
            client.start_turn(channel_id=1001, turn_id="t-1", reply_to_turns=(), content=(TextPart("x"),))
        publish_calls = [call for call in fake.calls if call[0] == "publish"]
        self.assertEqual(len(publish_calls), 1)

    def test_rejects_duplicate_and_self_reply_inputs(self) -> None:
        _, client = self.create()
        reply = TurnRef(9001, "parent")
        with self.assertRaises(ChatProtocolError):
            client.start_turn(
                channel_id=1001,
                turn_id="child",
                reply_to_turns=(reply, reply),
                content=(TextPart("x"),),
            )
        with self.assertRaises(ChatProtocolError):
            client.start_turn(
                channel_id=1001,
                turn_id="self",
                reply_to_turns=(TurnRef(2001, "self"),),
                content=(TextPart("x"),),
            )

    def test_rejects_unmanaged_channel_without_new_rpc(self) -> None:
        fake, client = self.create()
        before = len([call for call in fake.calls if call[0] in {"get_status", "publish"}])
        with self.assertRaises(ChannelNotManagedError):
            client.start_turn(channel_id=9999, turn_id="x", reply_to_turns=(), content=(TextPart("x"),))
        after = len([call for call in fake.calls if call[0] in {"get_status", "publish"}])
        self.assertEqual(after, before)

    def test_get_status_error_is_safe_and_does_not_fail_client(self) -> None:
        fake, client = self.create()
        fake.get_status_error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        with self.assertRaises(SyncReadError) as caught:
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        self.assertFalse(caught.exception.publish_sent)
        self.assertEqual(client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),)), 1)

    def test_failure_before_publish_rpc_reports_that_publish_was_not_sent(self) -> None:
        fake, client = self.create()
        original_call_rpc = client._call_rpc
        publishing_thread = threading.current_thread()
        publishing_rpc_count = 0

        def fail_publish_admission(operation):
            nonlocal publishing_rpc_count
            if threading.current_thread() is publishing_thread:
                publishing_rpc_count += 1
                if publishing_rpc_count == 2:
                    with client._condition:
                        client._fail_locked(ChatProtocolError("concurrent failure before PublishAutoSeq"))
            return original_call_rpc(operation)

        client._call_rpc = fail_publish_admission

        with self.assertRaises(SyncReadError) as caught:
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))

        self.assertFalse(caught.exception.publish_sent)
        self.assertFalse(any(call[0] == "publish" for call in fake.calls))

    def test_blocked_get_status_does_not_block_sync_thread_fetch(self) -> None:
        fake, client = self.create()
        fake.get_status_release = threading.Event()
        outcome = []

        def publish():
            try:
                outcome.append(client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),)))
            except Exception as exc:
                outcome.append(exc)

        thread = threading.Thread(target=publish)
        thread.start()
        try:
            self.assertTrue(fake.get_status_entered.wait(timeout=1))
            fetch_count = len([call for call in fake.calls if call[0] == "fetch"])
            with client._condition:
                client._condition.notify_all()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if len([call for call in fake.calls if call[0] == "fetch"]) > fetch_count:
                    break
                time.sleep(0.005)
            else:
                self.fail("sync thread did not Fetch while a publishing thread was blocked in GetStatus")
        finally:
            fake.get_status_release.set()
            thread.join(timeout=2)

        self.assertEqual(outcome, [1])

    def test_concurrent_watermarks_share_a_monotonic_sync_target(self) -> None:
        fake, client = self.create()
        with fake._lock:
            fake.messages.extend(message(seq=seq, channel_id=9999, payload=START) for seq in range(1, 8))
        with client._condition:
            client._condition.notify_all()
            self.assertTrue(
                client._condition.wait_for(lambda: client._processed_through_seq >= 7, timeout=1)
            )

        older_entered = threading.Event()
        older_release = threading.Event()
        outcomes = []
        older_thread = None

        def get_status(principal, token):
            if threading.current_thread() is older_thread:
                older_entered.set()
                older_release.wait(timeout=1)
                return type("Status", (), {"max_seq": 3})()
            return type("Status", (), {"max_seq": 7})()

        def sync_to_watermark():
            outcomes.append(client._sync_before_publish())

        fake.get_status = get_status
        older_thread = threading.Thread(target=sync_to_watermark)
        newer_thread = threading.Thread(target=sync_to_watermark)
        older_thread.start()
        try:
            self.assertTrue(older_entered.wait(timeout=1))
            newer_thread.start()
            newer_thread.join(timeout=1)
            self.assertFalse(newer_thread.is_alive())
            with client._condition:
                self.assertEqual(client._sync_target_seq, 7)
        finally:
            older_release.set()
            older_thread.join(timeout=1)
            if newer_thread.ident is not None:
                newer_thread.join(timeout=1)

        self.assertFalse(older_thread.is_alive())
        self.assertEqual(sorted(outcomes), [3, 7])
        with client._condition:
            self.assertEqual(client._sync_target_seq, 7)

    def test_uncertain_publish_failure_permanently_fails_client(self) -> None:
        fake, client = self.create()
        fake.publish_error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        with self.assertRaises(PublishFailedError) as caught:
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        self.assertEqual(caught.exception.code, grpc.StatusCode.UNAVAILABLE)
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),))

    def test_definite_publish_failure_releases_turn_and_keeps_client_ready(self) -> None:
        fake, client = self.create()
        fake.publish_error = FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT)
        with self.assertRaises(PublishFailedError):
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        self.assertEqual(client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),)), 1)

    def test_fetch_failure_permanently_fails_client(self) -> None:
        fake, client = self.create()
        fake.fetch_error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
            except (ClientFailedError, SyncReadError):
                break
            except Exception:
                time.sleep(0.01)
        else:
            self.fail("client did not fail after Fetch error")
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),))

    def test_permanent_get_status_error_fails_client(self) -> None:
        fake, client = self.create()
        fake.get_status_error = FakeRpcError(grpc.StatusCode.PERMISSION_DENIED)
        with self.assertRaises(SyncReadError):
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),))

    def test_invalid_get_status_response_fails_client(self) -> None:
        fake, client = self.create()
        fake.get_status = lambda principal, token: type("Status", (), {"max_seq": -1})()
        with self.assertRaises(SyncReadError):
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),))

    def test_invalid_publish_success_response_fails_client(self) -> None:
        fake, client = self.create()
        original_publish = fake.publish_auto_seq

        def invalid_response(**kwargs):
            original_publish(**kwargs)
            return type("PublishResponse", (), {"seq": 0})()

        fake.publish_auto_seq = invalid_response
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="b", reply_to_turns=(), content=(TextPart("x"),))

    def test_publish_response_must_match_scanned_message(self) -> None:
        fake, client = self.create()
        fake.rewrite_publish = lambda payload: payload.replace(b'"turn_id":"a"', b'"turn_id":"b"')
        with self.assertRaises(PublishCommittedSyncError) as caught:
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        self.assertEqual(caught.exception.seq, 1)
        with self.assertRaises(ClientFailedError):
            client.start_turn(channel_id=1001, turn_id="c", reply_to_turns=(), content=(TextPart("x"),))

    def test_second_publish_for_same_turn_is_busy(self) -> None:
        fake, client = self.create()
        fake.publish_release = threading.Event()
        outcomes = []

        def first_publish():
            try:
                outcomes.append(client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),)))
            except Exception as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=first_publish)
        thread.start()
        self.assertTrue(fake.publish_entered.wait(timeout=1))
        with self.assertRaises(TurnBusyError):
            client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),))
        fake.publish_release.set()
        thread.join(timeout=2)
        self.assertEqual(outcomes, [1])

    def test_close_during_committed_publish_preserves_seq(self) -> None:
        fake, client = self.create()
        fake.publish_release = threading.Event()
        outcomes = []

        def publish():
            try:
                outcomes.append(client.start_turn(channel_id=1001, turn_id="a", reply_to_turns=(), content=(TextPart("x"),)))
            except Exception as exc:
                outcomes.append(exc)

        publish_thread = threading.Thread(target=publish)
        publish_thread.start()
        self.assertTrue(fake.publish_entered.wait(timeout=1))
        close_thread = threading.Thread(target=client.close)
        close_thread.start()
        fake.publish_release.set()
        publish_thread.join(timeout=2)
        close_thread.join(timeout=2)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], PublishCommittedSyncError)
        self.assertEqual(outcomes[0].seq, 1)

    def test_initialization_rejects_bad_inputs_and_channel_protocol(self) -> None:
        with self.assertRaises(ChatProtocolError):
            create_client(FakeOpenEventClient(), principal=1, token="x", channel_ids=())
        with self.assertRaises(ChatProtocolError):
            create_client(FakeOpenEventClient(), principal=1, token="x", channel_ids=(1001, 1001))

        class WrongProtocol(FakeOpenEventClient):
            def get_channel(self, principal, token, channel_id):
                return type("R", (), {"channel": type("C", (), {"channel_id": channel_id, "protocol": "im.v1"})()})()

        with self.assertRaises(ChannelInitializationError):
            create_client(WrongProtocol(), principal=1, token="x", channel_ids=(1001,))

    def test_initialization_rejects_conflicting_history_without_thread(self) -> None:
        duplicate = message(seq=2, payload=START)
        fake = FakeOpenEventClient(messages=(message(seq=1, payload=START), duplicate))
        with self.assertRaises(ChannelInitializationError):
            create_client(fake, principal=2001, token="x", channel_ids=(1001,))


if __name__ == "__main__":
    unittest.main()
