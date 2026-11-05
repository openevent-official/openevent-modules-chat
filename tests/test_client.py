"""Observable SDK contracts against a small in-memory OpenEvent boundary."""
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import copy
import gc
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import grpc
from openevent.sdk.proto import openevent_pb2 as pb

from openevent.chat_sdk import (
    ChannelInitializationError, ChatProtocolError, ClientClosedError, ClientFailedError,
    FetchPageError, ObjectKey, PublishFailedError, RawExtensions, TextPart,
    TurnAttachment, TurnContentError, TurnRef, TurnWriter, TurnWriterStateError,
    UuidAllocationError, create_client, parse_message,
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

    def add(self, payload, *, principal=1, channel_id=10, object_keys=()):
        return self.persist(principal=principal, token="unused", channel_id=channel_id,
                            uuid=self.get_uuid(), payload=json.dumps(payload).encode(),
                            object_keys=object_keys).seq

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

    def test_constructor_remote_failure_is_structured(self):
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

    def test_client_accepts_transport_without_history_watermark_method(self):
        self.events.get_status = None
        chat = create_client(self.events, principal=1, token="credential", channel_id=10)
        writer = chat.start_turn(turn_id="new")
        writer.complete()
        self.assertEqual([message.payload["kind"] for message in chat.fetch_page(1).messages],
                         ["turn.start", "turn.end"])
        chat.close()

    def test_stream_uses_its_own_tail_without_history(self):
        creation = self.chat.single_turn(turn_id="input", content=())
        writer = self.chat.start_turn(turn_id="reply", reply_to_seqs=(creation,), content=(TextPart("A"),))
        self.assertEqual(writer.start_seq, 2)
        self.assertEqual(writer.turn_id, "reply")
        with self.assertRaises(AttributeError):
            writer.start_seq = 99
        self.assertEqual(writer.append(content=(TextPart("B"),), object_keys=(ObjectKey(3, "cap"),)), 3)
        self.assertEqual(writer.complete(), 4)
        payloads = [json.loads(call["payload"]) for call in self.events.calls["publish_auto_seq"]]
        self.assertEqual(payloads[1]["reply_to_seqs"], [creation])
        self.assertEqual(payloads[2]["pre_seq"], 2)
        self.assertEqual(payloads[3], {"kind": "turn.end", "turn_id": "reply", "pre_seq": 3,
                                       "start_seq": 2, "content_seqs": [2, 3]})
        self.assertEqual(writer._content_seqs, [])
        self.assertEqual(json.loads(self.events.messages[-1].payload)["content_seqs"], [2, 3])
        self.assertEqual(self.events.calls["fetch"], [])
        self.assertEqual(self.events.calls["get_status"], [])
        with self.assertRaises(TurnWriterStateError):
            writer.append(content=(TextPart("late"),))

    def test_releasing_writer_needs_no_client_cleanup_or_rpc(self):
        writer = self.writer()
        ref = weakref.ref(writer)
        self.chat.cancel_turn(target_turn=TurnRef(1, writer.turn_id), start_seq=writer.start_seq)
        count = len(self.events.calls["publish_auto_seq"])
        del writer
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(len(self.events.calls["publish_auto_seq"]), count)
        self.assertFalse(self.events.calls["fetch"])

    def test_writer_constructor_continues_known_start_without_history_or_new_start(self):
        creation = self.events.add(start_payload("old"))
        self.events.calls.clear()
        writer = TurnWriter(self.chat, "old", creation, creation, content_seqs=(creation,))
        self.assertEqual(dict(self.events.calls), {})
        self.assertEqual((writer.turn_id, writer.start_seq), ("old", creation))

        appended = writer.append(content=(TextPart("continued"),))
        reset = writer.reset(content=(TextPart("replacement"),))
        writer.complete()
        calls = self.events.calls["publish_auto_seq"]
        payloads = [json.loads(call["payload"]) for call in calls]
        self.assertEqual([payload["kind"] for payload in payloads],
                         ["turn.append", "turn.reset", "turn.end"])
        self.assertEqual([payload["pre_seq"] for payload in payloads],
                         [creation, appended, reset])
        self.assertEqual(payloads[-1]["content_seqs"], [reset])
        self.assertTrue(all(payload["turn_id"] == "old" for payload in payloads))
        self.assertTrue(all(call["channel_id"] == 10 and call["principal"] == 1 for call in calls))
        self.assertEqual(writer.start_seq, creation)
        self.assertEqual(set(self.events.calls), {"get_uuid", "publish_auto_seq"})
        with self.assertRaises(TurnWriterStateError):
            writer.complete()

    def test_writer_constructor_uses_known_append_or_reset_tail(self):
        for kind in ("turn.append", "turn.reset"):
            with self.subTest(kind=kind):
                creation = self.events.add(start_payload(kind))
                payload = append_payload(kind, creation)
                payload["kind"] = kind
                tail = self.events.add(payload)
                self.events.add(start_payload("unrelated"), principal=2)
                self.events.calls.clear()
                content_seqs = [creation, tail] if kind == "turn.append" else [tail]
                writer = TurnWriter(client=self.chat, turn_id=kind,
                                    start_seq=creation, last_seq=tail,
                                    content_seqs=iter(content_seqs))
                self.assertEqual(dict(self.events.calls), {})
                writer.complete()
                self.assertEqual(writer.start_seq, creation)
                published = json.loads(self.events.calls["publish_auto_seq"][0]["payload"])
                self.assertEqual(published, {"kind": "turn.end", "turn_id": kind, "pre_seq": tail,
                                             "start_seq": creation, "content_seqs": content_seqs})
                self.assertEqual(set(self.events.calls), {"get_uuid", "publish_auto_seq"})

    def test_writer_constructor_rejects_invalid_arguments_without_rpc_or_client_failure(self):
        active = self.writer()
        invalid = [{"client": None}, {"client": self.events}, {"turn_id": ""},
                   {"turn_id": 1}, {"turn_id": "中" * 43}, {"turn_id": "\ud800"},
                   {"start_seq": 2, "last_seq": 1}]
        invalid.extend({field: value} for field in ("start_seq", "last_seq")
                       for value in (0, -1, True, 1.0, 1 << 64))
        invalid.extend({"content_seqs": value} for value in (None, (), (True,), (0,), (2,), (1, 1)))
        self.events.calls.clear()
        for override in invalid:
            with self.subTest(override=override):
                arguments = dict(client=self.chat, turn_id="old", start_seq=1, last_seq=1,
                                 content_seqs=(1,))
                arguments.update(override)
                with self.assertRaises(ChatProtocolError):
                    TurnWriter(**arguments)
                self.assertEqual(dict(self.events.calls), {})
        self.assertEqual(active.complete(), 2)

    def test_writer_constructor_does_not_revive_closed_or_failed_client(self):
        for state in ("closed", "failed"):
            with self.subTest(state=state):
                events = Events()
                chat = create_client(events, principal=1, token="credential", channel_id=10)
                creation = events.add(start_payload("old"))
                if state == "closed":
                    chat.close()
                    expected = ClientClosedError
                else:
                    invalid_seq = events.add({"kind": "unsupported"})
                    with self.assertRaises(FetchPageError) as failure:
                        chat.fetch_page(invalid_seq)
                    expected = ClientFailedError
                events.calls.clear()
                writer = TurnWriter(chat, "old", creation, creation, content_seqs=(creation,))
                for write in (lambda: writer.append(content=(TextPart("late"),)),
                              writer.reset, writer.complete):
                    with self.assertRaises(expected) as caught:
                        write()
                    if state == "failed":
                        self.assertIs(caught.exception.failure, failure.exception.failure)
                self.assertEqual(dict(events.calls), {})
                chat.close()

    def test_empty_start_attachment_append_and_reset_share_one_chain(self):
        writer = self.chat.start_turn(turn_id="reply")
        self.assertEqual(writer.start_seq, 1)
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
        self.assertEqual(payloads[-1]["content_seqs"], [4, 5])
        self.assertEqual(calls[2]["object_keys"], ())
        self.assertEqual([key.object_id for key in calls[3]["object_keys"]], [8])
        self.assertEqual(writer.start_seq, 1)
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
            # The recipient can leave the Channel before the retry commits.
            writer.reset(recipients=(2,))
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
                    writer.reset(content=(TextPart("rejected"),), recipients=(2,))
                self.assertEqual(caught.exception.failure.category, "request_rejected")
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(len(self.events.calls["publish_auto_seq"]), count + 1)
                writer.complete()
                self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["pre_seq"],
                                 writer.start_seq)

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

    def test_raw_extensions_can_be_read_forwarded_and_retried_without_changes(self):
        raw = '{ "n": 1e400, "n": -0, "nested":' + '[' * 1500 + '0' + ']' * 1500 + ' }'
        self.chat.single_turn(turn_id='source', extensions=RawExtensions(raw))
        message = self.chat.fetch_page(1).messages[0]
        self.assertEqual(message.payload['extensions'].json_text, raw)
        self.events.inject('publish_auto_seq', RpcError(grpc.StatusCode.UNAVAILABLE))
        self.chat.single_turn(turn_id='forwarded', extensions=message.payload['extensions'])
        first, second = self.events.calls['publish_auto_seq'][-2:]
        self.assertEqual(first, second)
        self.assertIn(('"extensions":' + raw).encode(), second['payload'])
        self.assertEqual(self.chat.fetch_page(2).messages[0].payload['extensions'].json_text, raw)
        self.assertEqual(len(self.events.messages), 2)

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
        self.assertEqual(last["content_seqs"], [1, 2])

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
        self.assertEqual(self.writer("other").start_seq, 2)

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

    def test_reservations_and_cancellation_are_stateless(self):
        writer = self.writer()
        self.assertEqual(self.chat.reserve_submissions(10000), 2)
        self.assertEqual(self.chat.cancel_turn(target_turn=TurnRef(1, writer.turn_id),
                                               start_seq=writer.start_seq), 3)
        page = self.chat.fetch_page(1)
        self.assertEqual([m.payload["kind"] for m in page.messages],
                         ["turn.start", "submission.reserve", "turn.cancel"])
        self.assertEqual(page.messages[1].payload["reserved_through"], 10000)
        self.assertEqual(page.messages[2].payload["start_seq"], writer.start_seq)
        self.assertFalse(self.events.calls["get_status"])

    def test_end_size_rejection_publishes_small_error_with_same_tail(self):
        writer = self.writer()
        tail = writer.append(content=(TextPart("more"),))
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))
        terminal = writer.complete(recipients=iter((2, 3)), extensions={"large": "x" * 200})
        first, fallback = self.events.calls["publish_auto_seq"][-2:]
        self.assertEqual(json.loads(first["payload"])["content_seqs"], [writer.start_seq, tail])
        self.assertEqual(json.loads(fallback["payload"]), {
            "kind": "turn.end", "turn_id": writer.turn_id, "start_seq": writer.start_seq,
            "pre_seq": tail, "error": "turn_too_long"})
        self.assertEqual(first["recipients"], fallback["recipients"])
        self.assertEqual(fallback["recipients"], (2, 3))
        self.assertEqual(fallback["object_keys"], ())
        self.assertNotEqual(first["uuid"], fallback["uuid"])
        self.assertEqual(len(self.events.messages), 3)
        self.assertEqual(writer._content_seqs, [])
        with self.assertRaises(TurnWriterStateError):
            writer.complete()
        result = self.chat.get_turn_content(terminal)
        self.assertEqual((result.state, result.error, result.content, result.attachments),
                         ("completed", "turn_too_long", (), ()))
        self.assertEqual([(call["from_seq"], call["limit"]) for call in self.events.calls["fetch"]],
                         [(terminal, 1), (writer.start_seq, 1)])

    def test_end_size_rejection_after_timeout_cannot_trigger_fallback(self):
        writer = self.writer()
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.DEADLINE_EXCEEDED),
                           RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))
        with self.assertRaises(PublishFailedError) as caught:
            writer.complete()
        self.assertTrue(caught.exception.uncertain)
        calls = self.events.calls["publish_auto_seq"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-2], calls[-1])
        self.assertNotIn("error", json.loads(calls[-1]["payload"]))
        self.assertEqual(writer._content_seqs, [writer.start_seq])
        with self.assertRaises(TurnWriterStateError):
            writer.complete()

    def test_error_end_publish_failure_retains_normal_writer_failure_rules(self):
        for code, uncertain in ((grpc.StatusCode.PERMISSION_DENIED, False),
                                (grpc.StatusCode.UNAVAILABLE, True)):
            with self.subTest(code=code):
                writer = self.writer(str(code))
                failures = [RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED), RpcError(code)]
                if uncertain:
                    failures.append(RpcError(code))
                self.events.inject("publish_auto_seq", *failures)
                with self.assertRaises(PublishFailedError) as caught:
                    writer.complete()
                self.assertEqual(caught.exception.uncertain, uncertain)
                fallback = self.events.calls["publish_auto_seq"][-1]
                self.assertEqual(caught.exception.uuid, fallback["uuid"])
                self.assertEqual(json.loads(fallback["payload"])["error"], "turn_too_long")
                self.assertEqual(writer._content_seqs, [writer.start_seq])
                if uncertain:
                    with self.assertRaises(TurnWriterStateError):
                        writer.complete()
                else:
                    writer.complete()
                    normal = json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])
                    self.assertEqual(normal["content_seqs"], [writer.start_seq])
                    self.assertEqual(writer._content_seqs, [])

    def test_normal_end_definitive_failure_preserves_content_list_for_retry(self):
        writer = self.writer()
        tail = writer.append(content=(TextPart("more"),))
        self.events.inject("publish_auto_seq", RpcError(grpc.StatusCode.PERMISSION_DENIED))
        with self.assertRaises(PublishFailedError) as caught:
            writer.complete()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(writer._content_seqs, [writer.start_seq, tail])
        terminal = writer.complete()
        self.assertEqual(writer._content_seqs, [])
        payload = json.loads(self.events.messages[-1].payload)
        self.assertEqual(payload["content_seqs"], [writer.start_seq, tail])
        self.assertEqual(self.chat.get_turn_content(terminal).content, (TextPart("hello"), TextPart("more")))

    def test_restored_writer_copies_content_list_before_future_mutation(self):
        original = self.writer()
        seqs = [original.start_seq]
        writer = TurnWriter(self.chat, original.turn_id, original.start_seq,
                            original.start_seq, content_seqs=seqs)
        seqs.clear()
        writer.complete()
        self.assertEqual(json.loads(self.events.calls["publish_auto_seq"][-1]["payload"])["content_seqs"], [1])

    def test_get_single_content_keeps_parts_replies_and_attachment_origins(self):
        parent = self.chat.single_turn(turn_id="parent")
        key = ObjectKey(7, "private-cap")
        terminal = self.chat.single_turn(turn_id="single", content=(TextPart("a"), TextPart("b")),
                                         reply_to_seqs=(parent,), object_keys=(key, key))
        result = self.chat.get_turn_content(terminal)
        self.assertEqual(result.turn_ref, TurnRef(1, "single"))
        self.assertEqual((result.start_seq, result.terminal_seq, result.state),
                         (terminal, terminal, "completed"))
        self.assertEqual(result.content, (TextPart("a"), TextPart("b")))
        self.assertEqual(result.attachments, (TurnAttachment(key, terminal),) * 2)
        self.assertEqual(result.reply_to_seqs, (parent,))
        self.assertIsNone(result.error)
        self.assertEqual(len(self.events.calls["fetch"]), 1)
        self.assertNotIn("private-cap", repr(result))

    def test_get_end_content_skips_replaced_content_and_groups_adjacent_sequences(self):
        parent = self.chat.single_turn(turn_id="parent")
        writer = self.chat.start_turn(turn_id="reply", reply_to_seqs=(parent,),
                                      content=(TextPart("old"),), object_keys=(ObjectKey(1, "old"),))
        writer.append(content=(TextPart("discard"),))
        self.chat.single_turn(turn_id="unrelated")
        reset = writer.reset(object_keys=(ObjectKey(2, "new"),))
        appended = writer.append(content=(TextPart("new text"),))
        terminal = writer.complete()
        result = self.chat.get_turn_content(terminal)
        self.assertEqual(result.content, (TextPart("new text"),))
        self.assertEqual(result.attachments, (TurnAttachment(ObjectKey(2, "new"), reset),))
        self.assertEqual(result.reply_to_seqs, (parent,))
        self.assertEqual(result.start_seq, writer.start_seq)
        self.assertEqual([(call["from_seq"], call["limit"]) for call in self.events.calls["fetch"]],
                         [(terminal, 1), (writer.start_seq, 1), (reset, appended - reset + 1)])

    def test_get_long_end_content_uses_bounded_pages_instead_of_per_event_reads(self):
        writer = self.chat.start_turn(turn_id="long")
        for _ in range(1001):
            writer.append(content=(TextPart("x"),))
        terminal = writer.complete()
        result = self.chat.get_turn_content(terminal)
        self.assertEqual(len(result.content), 1001)
        self.assertEqual([(call["from_seq"], call["limit"]) for call in self.events.calls["fetch"]],
                         [(terminal, 1), (1, 1000), (1001, 2)])

    def test_get_end_content_continues_short_pages(self):
        writer = self.writer()
        writer.append(content=(TextPart("second"),))
        writer.append(content=(TextPart("third"),))
        terminal = writer.complete()
        messages = self.events.messages
        self.events.inject("fetch", SimpleNamespace(messages=[messages[-1]], next_seq=5, last_seq=4),
                           SimpleNamespace(messages=[messages[0]], next_seq=2, last_seq=4))
        result = self.chat.get_turn_content(terminal)
        self.assertEqual(result.content, (TextPart("hello"), TextPart("second"), TextPart("third")))
        self.assertEqual([(call["from_seq"], call["limit"]) for call in self.events.calls["fetch"]],
                         [(4, 1), (1, 3), (2, 2)])

    def test_turn_content_releases_messages_and_extensions_before_following_page(self):
        for kind, reset in (("end", False), ("end", True), ("cancel", True),
                            ("error", False), ("single", False)):
            with self.subTest(kind=kind, reset=reset):
                events = Events()
                with create_client(events, principal=1, token="credential", channel_id=10) as chat:
                    extension = RawExtensions('{"visible_notes":"' + 'x' * (256 * 1024) + '"}')
                    parent = chat.single_turn(turn_id="parent")
                    key = ObjectKey(7, "cap")
                    if kind == "single":
                        terminal = chat.single_turn(turn_id="single", content=(TextPart("one"),),
                            reply_to_seqs=(parent,), object_keys=(key,), extensions=extension)
                        expected_parts = (TextPart("one"),)
                        expected_attachments = (TurnAttachment(key, terminal),)
                        expected_seqs = [terminal]
                    else:
                        writer = chat.start_turn(turn_id="stream", content=(TextPart("old"),),
                            reply_to_seqs=(parent,), object_keys=(key,), extensions=extension)
                        old_append = writer.append(content=(TextPart(" old append"),), extensions=extension)
                        if reset:
                            first = writer.reset(content=(TextPart("new"),), extensions=extension)
                            expected_parts = (TextPart("new"),)
                            expected_attachments = ()
                            expected_seqs = [writer.start_seq, first]
                        else:
                            expected_parts = (TextPart("old"), TextPart(" old append"))
                            expected_attachments = (TurnAttachment(key, writer.start_seq),)
                            expected_seqs = [writer.start_seq, old_append]
                        for index in range(4):
                            expected_seqs.append(writer.append(content=(TextPart(str(index)),), extensions=extension))
                            expected_parts += (TextPart(str(index)),)
                        if kind == "cancel":
                            terminal = chat.cancel_turn(target_turn=TurnRef(1, "stream"),
                                                        start_seq=writer.start_seq, extensions=extension)
                            expected_seqs.insert(1, old_append)
                        else:
                            if kind == "error":
                                events.inject("publish_auto_seq", RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))
                                expected_parts, expected_attachments = (), ()
                                expected_seqs = [writer.start_seq]
                            terminal = writer.complete(extensions=extension)
                        expected_seqs.insert(0, terminal)

                    references = []
                    def track(raw):
                        message = parse_message(raw)
                        refs = [weakref.ref(message)]
                        if "extensions" in message.payload:
                            refs.append(weakref.ref(message.payload["extensions"]))
                        references.append((message.seq, refs))
                        return message

                    def assert_released():
                        gc.collect()
                        for seq, refs in references:
                            self.assertTrue(all(ref() is None for ref in refs), f"retained event {seq}")

                    original_fetch = events.fetch
                    def short_page(**kwargs):
                        assert_released()
                        return original_fetch(**dict(kwargs, limit=min(kwargs["limit"], 2)))

                    events.fetch = short_page
                    with patch("openevent.chat_sdk.client.parse_message", new=track):
                        result = chat.get_turn_content(terminal)
                    assert_released()
                    self.assertEqual(result.content, expected_parts)
                    self.assertEqual(result.attachments, expected_attachments)
                    self.assertEqual(result.reply_to_seqs, (parent,))
                    self.assertEqual([seq for seq, _ in references], expected_seqs)

    def test_get_cancelled_content_replays_target_only_and_includes_cancel_attachments(self):
        parent = self.chat.single_turn(turn_id="parent")
        writer = self.chat.start_turn(turn_id="same", content=(TextPart("old"),),
                                      reply_to_seqs=(parent,), object_keys=(ObjectKey(1, "old"),))
        self.events.add(start_payload("same"), principal=2)
        self.events.add({"kind": "system.member_added", "data": {
            "schema_version": 1, "member": 2}}, principal=0)
        reset = writer.reset(content=(TextPart("new"),), object_keys=(ObjectKey(2, "new"),))
        self.events.add(start_payload("other"), channel_id=11)
        appended = writer.append(content=(TextPart(" text"),))
        terminal = self.events.add({"kind": "turn.cancel", "target_turn": {"principal": 1, "turn_id": "same"},
                                   "start_seq": writer.start_seq}, principal=3,
                                  object_keys=(pb.ObjectKey(object_id=3, object_token="cancel"),))
        self.chat.single_turn(turn_id="later")
        result = self.chat.get_turn_content(terminal)
        self.assertEqual((result.turn_ref, result.state, result.error), (TurnRef(1, "same"), "cancelled", None))
        self.assertEqual(result.content, (TextPart("new"), TextPart(" text")))
        self.assertEqual(result.attachments, (TurnAttachment(ObjectKey(2, "new"), reset),
                                              TurnAttachment(ObjectKey(3, "cancel"), terminal)))
        self.assertEqual(result.reply_to_seqs, (parent,))
        self.assertEqual([(call["from_seq"], call["limit"]) for call in self.events.calls["fetch"]],
                         [(terminal, 1), (writer.start_seq, appended - writer.start_seq + 1)])

    def test_get_turn_content_missing_referenced_event_never_returns_partial_content(self):
        writer = self.writer()
        appended = writer.append(content=(TextPart("first page"),))
        other_channel_seq = self.events.add(start_payload("other-channel"), channel_id=11)
        terminal = self.events.add({
            "kind": "turn.end", "turn_id": writer.turn_id, "start_seq": writer.start_seq,
            "pre_seq": other_channel_seq,
            "content_seqs": [writer.start_seq, appended, other_channel_seq],
        })
        with self.assertRaises(TurnContentError) as caught:
            self.chat.get_turn_content(terminal)
        self.assertEqual((caught.exception.failure.stage, caught.exception.failure.category), ("Fetch", "contract"))
        self.assertEqual([call["from_seq"] for call in self.events.calls["fetch"]], [terminal, 1])
        with self.assertRaises(ClientFailedError) as failed:
            self.chat.fetch_page(1)
        self.assertIs(failed.exception.failure, caught.exception.failure)

    def test_get_turn_content_keeps_read_failure_and_can_retry_after_external_failure(self):
        writer = self.writer()
        terminal = writer.complete()
        self.events.inject("fetch", None, RpcError(grpc.StatusCode.UNAVAILABLE), RpcError(grpc.StatusCode.UNAVAILABLE))
        with self.assertRaises(TurnContentError) as caught:
            self.chat.get_turn_content(terminal)
        self.assertEqual((caught.exception.failure.stage, caught.exception.failure.category),
                         ("Fetch", "external_unavailable"))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.chat.get_turn_content(terminal).content, (TextPart("hello"),))

    def test_get_turn_content_validates_local_input_and_obeys_close(self):
        self.events.calls.clear()
        for seq in (0, True, -1, "1", 1 << 64):
            with self.subTest(seq=seq), self.assertRaises(ChatProtocolError):
                self.chat.get_turn_content(seq)
        self.assertEqual(dict(self.events.calls), {})
        self.chat.close()
        with self.assertRaises(ClientClosedError):
            self.chat.get_turn_content(1)

    def test_close_stops_remaining_turn_content_reads(self):
        writer = self.writer()
        terminal = writer.complete()
        entered, release = threading.Event(), threading.Event()

        def terminal_page(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release Fetch")
            return SimpleNamespace(messages=[self.events.messages[-1]],
                                   next_seq=terminal + 1, last_seq=terminal)

        self.events.inject("fetch", terminal_page)
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.chat.get_turn_content, terminal)
            self.assertTrue(entered.wait(1))
            close = pool.submit(self.chat.close)
            try:
                with self.chat._condition:
                    self.assertTrue(self.chat._condition.wait_for(lambda: self.chat._state == "CLOSING", 1))
                self.assertFalse(close.done())
            finally:
                release.set()
            with self.assertRaises(TurnContentError) as caught:
                read.result(timeout=1)
            self.assertEqual(caught.exception.failure.category, "lifecycle")
            close.result(timeout=1)
        self.assertEqual(len(self.events.calls["fetch"]), 1)

    def test_get_turn_content_rejects_nonterminal_and_mismatched_content_chain(self):
        for corruption in ("nonterminal", "wrong-owner", "broken-chain"):
            with self.subTest(corruption=corruption):
                events = Events()
                chat = create_client(events, principal=1, token="credential", channel_id=10)
                writer = chat.start_turn(turn_id="target")
                if corruption == "nonterminal":
                    terminal = writer.start_seq
                else:
                    first = events.add(append_payload("target", writer.start_seq),
                                       principal=2 if corruption == "wrong-owner" else 1)
                    tail = events.add(append_payload(
                        "target", writer.start_seq if corruption == "broken-chain" else first))
                    terminal = events.add({
                        "kind": "turn.end", "turn_id": "target", "start_seq": writer.start_seq,
                        "pre_seq": tail, "content_seqs": [writer.start_seq, first, tail],
                    })
                with self.assertRaises(TurnContentError) as caught:
                    chat.get_turn_content(terminal)
                self.assertEqual(caught.exception.failure.category, "contract")
                chat.close()

    def test_short_empty_and_future_pages_follow_raw_cursors(self):
        self.events.inject("fetch",
                           SimpleNamespace(messages=(), next_seq=4, last_seq=20),
                           SimpleNamespace(messages=(), next_seq=21, last_seq=20),
                           SimpleNamespace(messages=(), next_seq=21, last_seq=20))
        self.assertEqual(self.chat.fetch_page(1).next_seq, 4)
        self.assertEqual(self.chat.fetch_page(21).next_seq, 21)
        self.assertEqual(self.chat.fetch_page(50).next_seq, 21)
        self.assertEqual(len(self.events.calls["fetch"]), 3)
        self.assertTrue(all(call["channels"] == (10,) and not call["only_my_recipient"]
                            for call in self.events.calls["fetch"]))

    def test_mixed_page_skips_system_events_and_keeps_chat_and_raw_cursor(self):
        self.events.add({"kind": "system.channel_initialized", "data": {
            "schema_version": 1, "name": "chat", "protocol": "chat.v1", "description": "",
            "visibility": "private", "creator": 1, "members": [1]}}, principal=0)
        writer = self.writer()
        self.events.add({"kind": "system.member_added", "data": {
            "schema_version": 1, "member": 2}}, principal=0)
        self.chat.reserve_submissions(10000)
        self.events.add({"kind": "system.member_removed", "data": {
            "schema_version": 1, "member": 2}}, principal=0)

        page = self.chat.fetch_page(1)
        self.assertEqual([message.seq for message in page.messages], [2, 4])
        self.assertEqual([message.payload["kind"] for message in page.messages],
                         ["turn.start", "submission.reserve"])
        self.assertEqual((page.next_seq, page.last_seq), (6, 5))
        self.assertEqual(writer.complete(), 6)
        self.assertEqual(len(self.events.calls["fetch"]), 1)

    def test_system_only_page_returns_empty_without_losing_read_progress(self):
        for member in (2, 3):
            self.events.add({"kind": "system.member_added", "data": {
                "schema_version": 1, "member": member}}, principal=0)
        writer = self.writer()
        page = self.chat.fetch_page(1, limit=2)
        self.assertEqual(page.messages, ())
        self.assertEqual((page.next_seq, page.last_seq), (3, 3))
        self.assertEqual(len(self.events.calls["fetch"]), 1)
        following = self.chat.fetch_page(page.next_seq, limit=2)
        self.assertEqual([message.seq for message in following.messages], [writer.start_seq])
        self.assertEqual((following.next_seq, following.last_seq), (4, 3))

    def test_system_kind_from_business_principal_still_fails_chat_validation(self):
        self.events.add({"kind": "system.member_added", "data": {
            "schema_version": 1, "member": 2}}, principal=1)
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(1)
        self.assertEqual(caught.exception.failure.category, "protocol")
        with self.assertRaises(ClientFailedError):
            self.chat.fetch_page(1)

    def test_page_seq_gaps_are_valid(self):
        for index in range(4):
            self.events.add(start_payload(str(index)), channel_id=10 if index in {0, 3} else 11)
        page = self.chat.fetch_page(1)
        self.assertEqual([m.seq for m in page.messages], [1, 4])

    def test_temporary_page_failure_does_not_fail_client(self):
        writer = self.writer()
        self.events.inject("fetch", RpcError(grpc.StatusCode.UNAVAILABLE), RpcError(grpc.StatusCode.UNAVAILABLE))
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(1)
        self.assertEqual(caught.exception.failure.category, "external_unavailable")
        self.assertEqual(writer.complete(), 2)

    def test_parse_failure_is_fatal_but_does_not_expose_payload(self):
        writer = self.writer()
        invalid_seq = self.events.add({"kind": "unsupported", "private": "SECRET"})
        with self.assertRaises(FetchPageError) as caught:
            self.chat.fetch_page(invalid_seq)
        self.assertEqual(caught.exception.failure.category, "protocol")
        self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaises(ClientFailedError) as subsequent:
            writer.complete()
        self.assertIs(subsequent.exception.failure, caught.exception.failure)
        with self.assertRaises(ClientFailedError):
            self.chat.fetch_page(1)

    def test_constructing_another_writer_does_not_rewind_existing_writer(self):
        first = self.writer("first")
        old_creation = self.events.add(start_payload("old"))
        old_tail = self.events.add(append_payload("old", old_creation))
        second = TurnWriter(self.chat, "old", old_creation, old_tail,
                            content_seqs=(old_creation, old_tail))
        first.append(content=(TextPart("first next"),))
        second.append(content=(TextPart("old next"),))
        writes = self.events.calls["publish_auto_seq"]
        self.assertEqual(json.loads(writes[-2]["payload"])["pre_seq"], first.start_seq)
        self.assertEqual(json.loads(writes[-1]["payload"])["pre_seq"], old_tail)

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

    def test_close_during_start_returns_writer_for_committed_turn(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_publish(**kwargs):
            result = self.events.persist(**kwargs)
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release committed start")
            return result

        self.events.inject("publish_auto_seq", blocked_publish)
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(self.chat.start_turn, turn_id="starting")
            self.assertTrue(entered.wait(1))
            close = pool.submit(self.chat.close)
            try:
                with self.chat._condition:
                    self.assertTrue(self.chat._condition.wait_for(lambda: self.chat._state == "CLOSING", 1))
                self.assertFalse(close.done())
            finally:
                release.set()
            writer = start.result(timeout=1)
            close.result(timeout=1)
        self.assertEqual((writer.turn_id, writer.start_seq), ("starting", 1))
        self.assertEqual(len(self.events.messages), 1)
        with self.assertRaises(ClientClosedError):
            writer.complete()

    def test_close_after_failure_reports_closed_for_new_calls(self):
        writer = self.writer()
        invalid_seq = self.events.add({"kind": "unsupported"})
        with self.assertRaises(FetchPageError):
            self.chat.fetch_page(invalid_seq)
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
            invalid_seq = self.events.add({"kind": "unsupported"})
            try:
                with self.assertRaises(FetchPageError):
                    self.chat.fetch_page(invalid_seq)
            finally:
                release.set()
            self.assertEqual(write.result(timeout=1), invalid_seq + 1)
        with self.assertRaises(ClientFailedError):
            self.chat.fetch_page(1)


if __name__ == "__main__":
    unittest.main()
