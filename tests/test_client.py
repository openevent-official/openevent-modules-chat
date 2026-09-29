"""Observable SDK contracts against a small in-memory OpenEvent boundary."""
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import copy
import gc
import json
import threading
from types import SimpleNamespace
import unittest
import weakref

import grpc
from openevent.sdk.proto import openevent_pb2 as pb

from openevent.chat_sdk import (
    ChannelInitializationError, ChatProtocolError, ClientClosedError, ClientFailedError,
    FetchPageError, ObjectKey, PublishFailedError, SyncReadError, TextPart,
    TurnNotFoundError, TurnRef, TurnWriterStateError, UuidAllocationError, create_client,
)


class RpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "secret-token-and-private-payload"

    def __str__(self):
        return self.details()


class Events:
    timeout_ms = 200.0

    def __init__(self):
        self.calls = defaultdict(list)
        self.actions = defaultdict(deque)
        self._lock = threading.Lock()
        self._uuid = 1000
        self.max_seq = 0
        self.messages = []
        self.seqs = {}
        self.closed = False

    def inject(self, method, *actions):
        self.actions[method].extend(actions)

    def _call(self, name, kwargs, default):
        with self._lock:
            self.calls[name].append(copy.deepcopy(kwargs))
            action = self.actions[name].popleft() if self.actions[name] else None
        if isinstance(action, Exception):
            raise action
        if action is not None:
            return action(**kwargs) if callable(action) else action
        return default(**kwargs)

    def get_channel(self, **kwargs):
        return self._call("get_channel", kwargs, lambda **_: SimpleNamespace(
            channel=SimpleNamespace(channel_id=10, protocol="chat.v1")))

    def get_status(self, **kwargs):
        return self._call("get_status", kwargs, lambda **_: pb.GetStatusResponse(max_seq=self.max_seq))

    def get_uuid(self):
        def allocate():
            with self._lock:
                self._uuid += 1
                return self._uuid
        return self._call("get_uuid", {}, allocate)

    def get_seq_by_uuid(self, uuid):
        def lookup(uuid):
            if uuid not in self.seqs:
                raise RpcError(grpc.StatusCode.NOT_FOUND)
            return self.seqs[uuid]
        return self._call("get_seq_by_uuid", {"uuid": uuid}, lookup)

    def publish_auto_seq(self, **kwargs):
        return self._call("publish_auto_seq", kwargs, self.persist)

    def persist(self, **kwargs):
        with self._lock:
            if kwargs["uuid"] in self.seqs:
                raise RpcError(grpc.StatusCode.ALREADY_EXISTS)
            self.max_seq += 1
            seq = self.max_seq
            self.messages.append(pb.EventMessage(
                seq=seq, channel_id=kwargs["channel_id"], principal=kwargs["principal"],
                ts_ms=1, uuid=kwargs["uuid"], payload=kwargs["payload"],
                recipients=kwargs.get("recipients", ()), object_keys=kwargs.get("object_keys", ())))
            self.seqs[kwargs["uuid"]] = seq
            return SimpleNamespace(seq=seq)

    def fetch(self, **kwargs):
        def read(**kwargs):
            with self._lock:
                selected = [m for m in self.messages if m.seq >= kwargs["from_seq"]
                            and m.channel_id in kwargs["channels"]][:kwargs["limit"]]
                next_seq = selected[-1].seq + 1 if len(selected) == kwargs["limit"] else self.max_seq + 1
                return SimpleNamespace(messages=selected, next_seq=next_seq, last_seq=self.max_seq)
        return self._call("fetch", kwargs, read)

    def add(self, payload, *, principal=1, channel_id=10):
        return self.persist(principal=principal, token="unused", channel_id=channel_id,
                            uuid=self.get_uuid(), payload=json.dumps(payload).encode()).seq

    def close(self):
        self.closed = True


def start_payload(turn):
    return {"kind": "turn.start", "turn_id": turn, "reply_to_seqs": [],
            "content": [{"type": "text", "text": "start"}]}


def append_payload(turn, pre_seq):
    return {"kind": "turn.append", "turn_id": turn, "pre_seq": pre_seq,
            "content": [{"type": "text", "text": "more"}]}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.events = Events()
        self.chat = create_client(self.events, principal=1, token="credential", channel_id=10, max_retries=1)

    def writer(self, turn="one"):
        return self.chat.start_turn(turn_id=turn, content=(TextPart("hello"),))

    def test_constructor_only_checks_channel_and_validates_configuration(self):
        self.assertEqual(set(self.events.calls), {"get_channel"})
        for options in ({"principal": 0}, {"channel_id": True}, {"max_retries": -1},
                        {"token": ""}, {"channel_validator": False}):
            config = dict(principal=1, token="credential", channel_id=10)
            config.update(options)
            with self.assertRaises(ChatProtocolError):
                create_client(self.events, **config)
        self.events.timeout_ms = float("inf")
        with self.assertRaises(ChatProtocolError):
            create_client(self.events, principal=1, token="credential", channel_id=10)

    def test_constructor_mismatch_and_remote_failure_are_structured(self):
        self.events.inject("get_channel", SimpleNamespace(channel=SimpleNamespace(channel_id=11, protocol="chat.v1")))
        with self.assertRaises(ChannelInitializationError) as caught:
            create_client(self.events, principal=1, token="credential", channel_id=10)
        self.assertEqual(caught.exception.failure.category, "contract")
        self.events.inject("get_channel", RpcError(grpc.StatusCode.UNAUTHENTICATED))
        with self.assertRaises(ChannelInitializationError) as caught:
            create_client(self.events, principal=1, token="credential", channel_id=10)
        self.assertEqual(caught.exception.failure.grpc_code, grpc.StatusCode.UNAUTHENTICATED)
        self.assertNotIn("secret", str(caught.exception))

    def test_constructor_validates_application_requirements_on_same_channel_read(self):
        channel = SimpleNamespace(channel_id=10, protocol="chat.v1", members=[1, 2])
        self.events.inject("get_channel", SimpleNamespace(channel=channel))
        self.events.calls.clear()
        validated = []
        chat = create_client(self.events, principal=1, token="credential", channel_id=10,
                             channel_validator=validated.append)
        self.assertEqual(validated, [channel])
        self.assertIs(validated[0], channel)
        self.assertEqual(set(self.events.calls), {"get_channel"})
        self.assertEqual(len(self.events.calls["get_channel"]), 1)
        chat.close()

    def test_constructor_validator_rejection_fails_without_retry_or_private_details(self):
        validated = []

        def reject(channel):
            validated.append(channel)
            raise ValueError("private-channel-description")

        self.events.calls.clear()
        with self.assertRaises(ChannelInitializationError) as caught:
            create_client(self.events, principal=1, token="credential", channel_id=10,
                          max_retries=3, channel_validator=reject)
        self.assertEqual(caught.exception.failure.category, "contract")
        self.assertFalse(hasattr(caught.exception.failure, "retryable"))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(validated), 1)
        self.assertEqual(len(self.events.calls["get_channel"]), 1)
        self.assertEqual(set(self.events.calls), {"get_channel"})
        self.events.inject("get_channel", SimpleNamespace(channel=SimpleNamespace(channel_id=10, protocol="other")))
        with self.assertRaises(ChannelInitializationError):
            create_client(self.events, principal=1, token="credential", channel_id=10,
                          channel_validator=reject)
        self.assertEqual(len(validated), 1)

    def test_stream_uses_its_own_tail_without_history(self):
        creation = self.chat.single_turn(turn_id="input", content=())
        writer = self.chat.start_turn(turn_id="reply", reply_to_seqs=(creation,), content=(TextPart("A"),))
        self.assertEqual(writer.creation_seq, 2)
        self.assertEqual(writer.turn_id, "reply")
        with self.assertRaises(AttributeError):
            writer.creation_seq = 99
        self.assertEqual(writer.append(content=(TextPart("B"),), object_keys=(ObjectKey(3, "cap"),)), 3)
        self.assertEqual(writer.complete(), 4)
        payloads = [json.loads(call["payload"]) for call in self.events.calls["publish_auto_seq"]]
        self.assertEqual(payloads[1]["reply_to_seqs"], [creation])
        self.assertEqual(payloads[2]["pre_seq"], 2)
        self.assertEqual(payloads[3], {"kind": "turn.end", "turn_id": "reply", "pre_seq": 3})
        self.assertEqual(self.events.calls["fetch"], [])
        self.assertEqual(self.events.calls["get_status"], [])
        with self.assertRaises(TurnWriterStateError):
            writer.append(content=(TextPart("late"),))

    def test_releasing_writer_needs_no_client_cleanup_or_rpc(self):
        writer = self.writer()
        ref = weakref.ref(writer)
        self.chat.cancel_turn(target_turn=TurnRef(1, writer.turn_id))
        count = len(self.events.calls["publish_auto_seq"])
        del writer
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(len(self.events.calls["publish_auto_seq"]), count)
        self.assertFalse(self.events.calls["fetch"])

    def test_empty_start_attachment_append_and_reset_share_one_chain(self):
        writer = self.chat.start_turn(turn_id="reply")
        self.assertEqual(writer.creation_seq, 1)
        self.assertEqual(writer.append(object_keys=(ObjectKey(7, "old-cap"),)), 2)
        self.assertEqual(writer.reset(), 3)
        self.assertEqual(writer.reset(content=(TextPart("new"),),
                                      object_keys=(ObjectKey(8, "new-cap"),)), 4)
        self.assertEqual(writer.append(content=(TextPart(" reply"),)), 5)
        self.assertEqual(writer.complete(), 6)
        calls = self.events.calls["publish_auto_seq"]
        payloads = [json.loads(call["payload"]) for call in calls]
        self.assertEqual([payload["kind"] for payload in payloads],
                         ["turn.start", "turn.append", "turn.reset", "turn.reset", "turn.append", "turn.end"])
        self.assertEqual([payload["pre_seq"] for payload in payloads[1:]], [1, 2, 3, 4, 5])
        self.assertEqual(payloads[0]["content"], [])
        self.assertEqual(payloads[1]["content"], [])
        self.assertEqual(payloads[2]["content"], [])
        self.assertEqual(calls[2]["object_keys"], ())
        self.assertEqual([key.object_id for key in calls[3]["object_keys"]], [8])
        self.assertEqual(writer.creation_seq, 1)
        self.assertFalse(self.events.calls["fetch"])
        self.assertFalse(self.events.calls["get_status"])
        with self.assertRaises(TurnWriterStateError):
            writer.reset()

    def test_empty_append_and_invalid_parts_fail_before_allocating_uuid(self):
        writer = self.chat.start_turn(turn_id="reply")
        allocations = len(self.events.calls["get_uuid"])
        for write in (writer.append, lambda: writer.reset(content=(TextPart(""),)),
                      lambda: writer.append(recipients=(0,), content=(TextPart("x"),))):
            with self.assertRaises(ChatProtocolError):
                write()
        self.assertEqual(len(self.events.calls["get_uuid"]), allocations)
        self.assertEqual(writer.reset(), 2)
        self.assertEqual(writer.complete(), 3)

    def test_reset_freezes_content_and_attachments_for_same_uuid_retry(self):
        writer = self.writer()
        parts = [TextPart("new")]
        keys = [ObjectKey(8, "new-cap")]
        extensions = {"attempt": [1]}

        def committed_but_lost(**kwargs):
            self.events.persist(**kwargs)
            parts.clear()
            keys.clear()
            extensions["attempt"][0] = 2
            raise RpcError(grpc.StatusCode.UNAVAILABLE)

        self.events.inject("publish_auto_seq", committed_but_lost)
        self.assertEqual(writer.reset(content=parts, object_keys=keys, extensions=extensions), 2)
        first, second = self.events.calls["publish_auto_seq"][-2:]
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first["payload"])["extensions"], {"attempt": [1]})
        self.assertEqual(writer.complete(), 3)
        self.assertEqual(len(self.events.messages), 3)

    def test_unknown_reset_blocks_every_following_write(self):
        writer = self.writer()
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.DEADLINE_EXCEEDED),
                           RpcError(grpc.StatusCode.INVALID_ARGUMENT))
        with self.assertRaises(PublishFailedError) as caught:
            writer.reset()
        self.assertEqual(caught.exception.failure.category, "request_rejected")
        self.assertTrue(caught.exception.uncertain)
        for write in (writer.reset, writer.complete,
                      lambda: writer.append(content=(TextPart("late"),))):
            with self.assertRaises(TurnWriterStateError):
                write()

    def test_publish_parameter_and_size_rejections_keep_original_tail(self):
        for code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED):
            with self.subTest(code=code):
                writer = self.writer(code.name)
                self.events.inject("publish_auto_seq", RpcError(code))
                count = len(self.events.calls["publish_auto_seq"])
                with self.assertRaises(PublishFailedError) as caught:
                    writer.reset(content=(TextPart("rejected"),))
                self.assertEqual(caught.exception.failure.category, "request_rejected")
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(len(self.events.calls["publish_auto_seq"]), count + 1)
                writer.complete()
                self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["pre_seq"],
                                 writer.creation_seq)

    def test_parameters_are_frozen_before_uuid_and_reused_across_retries(self):
        replies = [9, 2]
        recipients = [3, 3, 2]
        keys = [ObjectKey(9, "cap")]
        extensions = {"nested": ["original"]}

        def allocate_and_mutate():
            replies.clear()
            recipients.clear()
            keys.clear()
            extensions["nested"][0] = "changed"
            return 5000

        self.events.inject("get_uuid", allocate_and_mutate)
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.UNAVAILABLE))
        self.chat.single_turn(turn_id="freeze", content=(TextPart("hello"),), reply_to_seqs=replies,
                              recipients=recipients, object_keys=keys, extensions=extensions)
        first, second = self.events.calls["publish_auto_seq"]
        self.assertEqual(first, second)
        self.assertEqual(first["uuid"], 5000)
        self.assertEqual(first["recipients"], (3, 3, 2))
        self.assertEqual(first["object_keys"][0].object_id, 9)
        self.assertEqual(json.loads(first["payload"])["extensions"], {"nested": ["original"]})
        self.assertEqual(json.loads(first["payload"])["reply_to_seqs"], [9, 2])
        self.assertEqual(len(self.events.calls["get_uuid"]), 1)

    def test_lost_publish_reply_is_reconciled_by_same_uuid(self):
        writer = self.writer()

        def committed_but_lost(**kwargs):
            self.events.persist(**kwargs)
            raise RpcError(grpc.StatusCode.UNAVAILABLE)

        self.events.inject("publish_auto_seq", committed_but_lost)
        self.assertEqual(writer.append(content=(TextPart("once"),)), 2)
        self.assertEqual(writer.complete(), 3)
        self.assertEqual(len(self.events.messages), 3)
        self.assertEqual(len(self.events.calls["get_seq_by_uuid"]), 1)
        last = json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])
        self.assertEqual(last["pre_seq"], 2)

    def test_definitive_rejection_leaves_writer_open(self):
        writer = self.writer()
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.PERMISSION_DENIED))
        with self.assertRaises(PublishFailedError) as caught:
            writer.append(content=(TextPart("rejected"),))
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(writer.append(content=(TextPart("after fix"),)), 2)
        self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["pre_seq"], 1)

    def test_later_rejection_does_not_settle_earlier_timeout(self):
        writer = self.writer()
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.DEADLINE_EXCEEDED),
                           RpcError(grpc.StatusCode.PERMISSION_DENIED))
        with self.assertRaises(PublishFailedError) as caught:
            writer.append(content=(TextPart("unknown"),))
        self.assertTrue(caught.exception.uncertain)
        self.chat.fetch_page(1)
        with self.assertRaises(TurnWriterStateError):
            writer.complete()
        self.assertEqual(self.writer("other").creation_seq, 2)

    def test_uuid_failure_and_local_rejection_do_not_lose_tail(self):
        writer = self.writer()
        self.events.inject("get_uuid", RpcError(grpc.StatusCode.UNAVAILABLE), RpcError(grpc.StatusCode.UNAVAILABLE))
        with self.assertRaises(UuidAllocationError):
            writer.append(content=(TextPart("not sent"),))
        with self.assertRaises(ChatProtocolError):
            writer.append(content=())
        self.assertEqual(writer.complete(), 2)
        self.assertEqual(len(self.events.calls["publish_auto_seq"]), 2)

    def test_reconciliation_failure_keeps_writer_unresolved(self):
        writer = self.writer()

        def committed_but_lost(**kwargs):
            self.events.persist(**kwargs)
            raise RpcError(grpc.StatusCode.UNKNOWN)

        self.events.inject("publish_auto_seq", committed_but_lost)
        self.events.inject("get_seq_by_uuid", RpcError(grpc.StatusCode.UNAVAILABLE), RpcError(grpc.StatusCode.UNAVAILABLE))
        with self.assertRaises(PublishFailedError) as caught:
            writer.append(content=(TextPart("unknown"),))
        self.assertEqual(caught.exception.failure.stage, "GetSeqByUuid")
        self.assertTrue(caught.exception.uncertain)
        self.assertGreater(caught.exception.uuid, 0)
        self.chat.fetch_page(1)
        with self.assertRaises(TurnWriterStateError):
            writer.complete()

    def test_consumed_uuid_missing_from_lookup_is_a_contract_failure(self):
        writer = self.writer()

        def committed_but_lost(**kwargs):
            self.events.persist(**kwargs)
            raise RpcError(grpc.StatusCode.UNKNOWN)

        self.events.inject("publish_auto_seq", committed_but_lost)
        self.events.inject("get_seq_by_uuid", RpcError(grpc.StatusCode.NOT_FOUND))
        with self.assertRaises(PublishFailedError) as caught:
            writer.reset()
        self.assertEqual(caught.exception.failure.category, "contract")
        self.assertEqual(caught.exception.failure.grpc_code, grpc.StatusCode.NOT_FOUND)
        self.assertTrue(caught.exception.uncertain)
        with self.assertRaises(ClientFailedError) as subsequent:
            self.chat.fetch_page(1)
        self.assertIs(subsequent.exception.failure, caught.exception.failure)

    def test_unexpected_first_already_exists_is_not_a_success(self):
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.ALREADY_EXISTS))
        with self.assertRaises(PublishFailedError) as caught:
            self.writer()
        self.assertEqual(caught.exception.failure.category, "contract")
        self.assertEqual(self.events.calls["get_seq_by_uuid"], [])

    def test_reservations_and_cancellation_are_stateless(self):
        writer = self.writer()
        self.assertEqual(self.chat.reserve_submissions(10000), 2)
        self.assertEqual(self.chat.cancel_turn(target_turn=TurnRef(1, writer.turn_id)), 3)
        page = self.chat.fetch_page(1)
        self.assertEqual([m.payload["kind"] for m in page.messages],
                         ["turn.start", "submission.reserve", "turn.cancel"])
        self.assertEqual(page.messages[1].payload["reserved_through"], 10000)
        self.assertFalse(self.events.calls["get_status"])

    def test_short_empty_and_future_pages_follow_raw_cursors(self):
        self.events.inject("fetch",
                           SimpleNamespace(messages=(), next_seq=4, last_seq=20),
                           SimpleNamespace(messages=(), next_seq=4, last_seq=20),
                           SimpleNamespace(messages=(), next_seq=21, last_seq=20))
        self.assertEqual(self.chat.fetch_page(1).next_seq, 4)
        self.assertEqual(self.chat.fetch_page(4).next_seq, 4)
        self.assertEqual(self.chat.fetch_page(50).next_seq, 21)
        self.assertEqual(len(self.events.calls["fetch"]), 3)
        self.assertTrue(all(call["channels"] == (10,) and not call["only_my_recipient"]
                            for call in self.events.calls["fetch"]))

    def test_page_seq_gaps_are_valid_and_last_seq_is_not_cross_compared(self):
        for index in range(4):
            self.events.add(start_payload(str(index)), channel_id=10 if index in {0, 3} else 11)
        page = self.chat.fetch_page(1)
        self.assertEqual([m.seq for m in page.messages], [1, 4])
        self.events.inject("fetch", SimpleNamespace(messages=(), next_seq=1, last_seq=2))
        self.assertEqual(self.chat.fetch_page(1).last_seq, 2)

    def test_temporary_page_failure_does_not_fail_client(self):
        writer = self.writer()
        self.events.inject("fetch", RpcError(grpc.StatusCode.UNAVAILABLE), RpcError(grpc.StatusCode.UNAVAILABLE))
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(1)
        self.assertEqual(caught.exception.failure.category, "external_unavailable")
        self.assertEqual(writer.complete(), 2)

    def test_invalid_page_order_fails_all_bound_objects_with_same_root(self):
        writer = self.writer()
        writer.append(content=(TextPart("second"),))
        self.events.inject("fetch", SimpleNamespace(messages=list(reversed(self.events.messages)), next_seq=3, last_seq=2))
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(1)
        self.assertEqual(caught.exception.failure.category, "contract")
        with self.assertRaises(ClientFailedError) as subsequent:
            writer.complete()
        self.assertIs(subsequent.exception.failure, caught.exception.failure)

    def test_parse_failure_is_fatal_but_does_not_expose_payload(self):
        self.events.messages = [pb.EventMessage(seq=1, channel_id=10, principal=1, uuid=1001,
                                               payload=b"SECRET invalid json")]
        self.events.max_seq = 1
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(1)
        self.assertEqual(caught.exception.failure.category, "protocol")
        self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaises(ClientFailedError):
            self.chat.fetch_page(1)

    def test_resume_uses_fixed_watermark_and_only_target_state(self):
        self.events.add({"kind": "submission.reserve", "reserved_through": 10000})
        creation = self.events.add(start_payload("old"))
        tail = self.events.add(append_payload("old", creation))
        self.events.add(start_payload("other"))
        self.events.add({"kind": "turn.cancel", "target_turn": {"principal": 1, "turn_id": "old"}}, principal=2)
        self.events.inject("get_status", pb.GetStatusResponse(max_seq=4))
        writer = self.chat.resume_turn("old")
        self.assertEqual(writer.creation_seq, creation)
        writer.append(content=(TextPart("from known tail"),))
        self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["pre_seq"], tail)
        self.assertEqual(len(self.events.calls["get_status"]), 1)
        self.assertEqual(len(self.events.calls["fetch"]), 1)

    def test_resuming_another_turn_does_not_rewind_existing_writer(self):
        first = self.writer("first")
        old_creation = self.events.add(start_payload("old"))
        old_tail = self.events.add(append_payload("old", old_creation))
        second = self.chat.resume_turn("old")
        first.append(content=(TextPart("first next"),))
        second.append(content=(TextPart("old next"),))
        writes = self.events.calls["publish_auto_seq"]
        self.assertEqual(json.loads(writes[-2]["payload"])["pre_seq"], first.creation_seq)
        self.assertEqual(json.loads(writes[-1]["payload"])["pre_seq"], old_tail)

    def test_recovery_resumes_at_reset_without_rebuilding_old_content(self):
        creation = self.events.add(start_payload("old"))
        previous = self.events.add(append_payload("old", creation))
        reset = self.events.add({"kind": "turn.reset", "turn_id": "old", "pre_seq": previous,
                                 "content": []})
        writer = self.chat.resume_turn("old", state_start_seq=creation)
        self.assertEqual(writer.creation_seq, creation)
        writer.append(object_keys=(ObjectKey(7, "new-cap"),))
        self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["pre_seq"], reset)

    def test_recovery_does_not_reopen_cancelled_turn_after_reset(self):
        creation = self.events.add(start_payload("old"))
        self.events.add({"kind": "turn.cancel", "target_turn": {"principal": 1, "turn_id": "old"}}, principal=2)
        reset = self.events.add({"kind": "turn.reset", "turn_id": "old", "pre_seq": creation,
                                 "content": []})
        self.events.add(append_payload("old", reset))
        with self.assertRaises(TurnWriterStateError):
            self.chat.resume_turn("old")
        self.assertEqual(len(self.chat.fetch_page(1).messages), 4)

    def test_invalid_target_history_fails_recovery_and_other_writers(self):
        histories = [
            [start_payload("old"), start_payload("old")],
            [start_payload("old"), append_payload("old", 90)],
            [start_payload("old"), {"kind": "turn.reset", "turn_id": "old", "pre_seq": 90, "content": []}],
            [{"kind": "turn.cancel", "target_turn": {"principal": 1, "turn_id": "old"}}],
        ]
        for history in histories:
            with self.subTest(history=history):
                events = Events()
                chat = create_client(events, principal=1, token="credential", channel_id=10)
                active = chat.start_turn(turn_id="active")
                for payload in history:
                    events.add(payload)
                with self.assertRaises(SyncReadError) as caught:
                    chat.resume_turn("old")
                self.assertEqual(caught.exception.failure.category, "protocol")
                with self.assertRaises(ClientFailedError) as subsequent:
                    active.reset()
                self.assertIs(subsequent.exception.failure, caught.exception.failure)

    def test_resume_handles_empty_page_without_inventing_progress_requirement(self):
        creation = self.events.add(start_payload("old"))
        self.events.inject("fetch", SimpleNamespace(messages=(), next_seq=1, last_seq=creation))
        writer = self.chat.resume_turn("old")
        self.assertEqual(writer.creation_seq, creation)
        self.assertEqual([call["from_seq"] for call in self.events.calls["fetch"]], [1, 1])

    def test_missing_and_terminal_recovery_targets_do_not_fail_client(self):
        with self.assertRaises(TurnNotFoundError):
            self.chat.resume_turn("missing")
        self.chat.single_turn(turn_id="single", content=())
        with self.assertRaises(TurnWriterStateError):
            self.chat.resume_turn("single")
        writer = self.writer("cancelled")
        self.chat.cancel_turn(target_turn=TurnRef(1, writer.turn_id))
        with self.assertRaises(TurnWriterStateError):
            self.chat.resume_turn("cancelled")
        self.assertGreater(self.writer("still ready").creation_seq, 0)

    def test_recovery_get_status_and_fetch_fail_the_entire_client(self):
        for method in ("get_status", "fetch"):
            with self.subTest(method=method):
                events = Events()
                chat = create_client(events, principal=1, token="credential", channel_id=10, max_retries=0)
                writer = chat.start_turn(turn_id="active", content=(TextPart("hello"),))
                events.inject(method, RpcError(grpc.StatusCode.UNAVAILABLE))
                with self.assertRaises(SyncReadError) as caught:
                    chat.resume_turn("old")
                with self.assertRaises(ClientFailedError) as subsequent:
                    writer.complete()
                self.assertIs(caught.exception.failure, subsequent.exception.failure)

    def test_same_object_serializes_writes_while_other_objects_continue(self):
        first = self.writer("first")
        other = self.writer("other")
        entered = threading.Event()
        release = threading.Event()

        def blocked_publish(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release blocked publish")
            return self.events.persist(**kwargs)

        self.events.inject("publish_auto_seq", blocked_publish)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first_call = pool.submit(first.reset, content=(TextPart("A"),))
            self.assertTrue(entered.wait(1))
            queued = pool.submit(first.append, content=(TextPart("B"),))
            try:
                independent = pool.submit(other.append, content=(TextPart("parallel"),))
                self.assertEqual(independent.result(timeout=1), 3)
                self.assertFalse(queued.done())
            finally:
                release.set()
            first_seq = first_call.result(timeout=1)
            queued.result(timeout=1)
        last_payload = json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])
        self.assertEqual(last_payload["pre_seq"], first_seq)

    def test_close_waits_for_publish_without_losing_committed_result(self):
        writer = self.writer()
        entered = threading.Event()
        release = threading.Event()

        def blocked_publish(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release blocked publish")
            return self.events.persist(**kwargs)

        self.events.inject("publish_auto_seq", blocked_publish)
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(writer.complete)
            self.assertTrue(entered.wait(1))
            close = pool.submit(self.chat.close)
            with self.chat._condition:
                self.assertTrue(self.chat._condition.wait_for(lambda: self.chat._state == "CLOSING", 1))
            try:
                self.assertFalse(close.done())
                with self.assertRaises(ClientClosedError):
                    self.chat.fetch_page(1)
            finally:
                release.set()
            self.assertEqual(write.result(timeout=1), 2)
            close.result(timeout=1)
        self.chat.close()
        self.assertFalse(self.events.closed)
        with self.assertRaises(ClientClosedError):
            writer.append(content=(TextPart("late"),))

    def test_invalid_committed_response_fails_client_and_preserves_uuid(self):
        writer = self.writer()
        self.events.inject("publish_auto_seq", SimpleNamespace(seq=0))
        with self.assertRaises(PublishFailedError) as caught:
            writer.append(content=(TextPart("bad response"),))
        self.assertGreater(caught.exception.uuid, 0)
        self.assertTrue(caught.exception.uncertain)
        with self.assertRaises(ClientFailedError):
            self.writer("another")

    def test_close_after_failure_reports_closed_for_new_calls(self):
        writer = self.writer()
        self.events.inject("get_status", RpcError(grpc.StatusCode.PERMISSION_DENIED))
        with self.assertRaises(SyncReadError):
            self.chat.resume_turn("old")
        self.chat.close()
        with self.assertRaises(ClientClosedError):
            writer.complete()

    def test_close_after_uuid_allocation_before_publish_is_not_uncertain(self):
        writer = self.writer()
        entered = threading.Event()
        release = threading.Event()

        def blocked_uuid():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release UUID allocation")
            return 9000

        self.events.inject("get_uuid", blocked_uuid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(writer.append, content=(TextPart("never sent"),))
            self.assertTrue(entered.wait(1))
            close = pool.submit(self.chat.close)
            with self.chat._condition:
                self.assertTrue(self.chat._condition.wait_for(lambda: self.chat._state == "CLOSING", 1))
            release.set()
            with self.assertRaises(PublishFailedError) as caught:
                write.result(timeout=1)
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual(caught.exception.uuid, 9000)
            self.assertEqual(caught.exception.failure.category, "lifecycle")
            close.result(timeout=1)
        self.assertEqual(len(self.events.calls["publish_auto_seq"]), 1)

    def test_client_failure_does_not_discard_an_inflight_publish_success(self):
        writer = self.writer()
        entered = threading.Event()
        release = threading.Event()

        def blocked_publish(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release publish")
            return self.events.persist(**kwargs)

        self.events.inject("publish_auto_seq", blocked_publish)
        with ThreadPoolExecutor(max_workers=1) as pool:
            write = pool.submit(writer.complete)
            self.assertTrue(entered.wait(1))
            self.events.inject("get_status", RpcError(grpc.StatusCode.PERMISSION_DENIED))
            try:
                with self.assertRaises(SyncReadError):
                    self.chat.resume_turn("old")
            finally:
                release.set()
            self.assertEqual(write.result(timeout=1), 2)
        with self.assertRaises(ClientFailedError):
            self.chat.fetch_page(1)


if __name__ == "__main__":
    unittest.main()
