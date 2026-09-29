import json
import unittest

import grpc
from openevent.sdk.proto import openevent_pb2 as pb
from openevent.chat_sdk.codec import encode_payload, parse_message, UINT64_MAX
from openevent.chat_sdk.errors import ChatProtocolError, make_failure, ClientFailedError, ClientClosedError


class CodecTests(unittest.TestCase):
    def event(self, payload, **kwargs):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return pb.EventMessage(seq=5, channel_id=10, principal=1, uuid=9,
                               payload=data, **kwargs)

    def test_wire_integers_and_order_preserved(self):
        payload = {'kind': 'turn.single', 'turn_id': '中é', 'content': [],
                   'reply_to_seqs': [UINT64_MAX, 1], 'extensions': {'nested': [None, True]}}
        message = parse_message(self.event(encode_payload(payload), recipients=[2, 2, 1],
            object_keys=[pb.ObjectKey(object_id=3, object_token='secret')] * 2))
        self.assertEqual(message.payload, payload)
        self.assertEqual(message.recipients, (2, 2, 1))
        self.assertEqual(len(message.object_keys), 2)
        self.assertNotIn('secret', repr(message))

    def test_no_reply_history_validation(self):
        payload = {'kind': 'turn.single', 'turn_id': 't', 'content': [], 'reply_to_seqs': [99]}
        self.assertEqual(parse_message(self.event(payload)).payload, payload)

    def test_seven_kinds_and_empty_content_rules(self):
        good = [
            {'kind': 'turn.single', 'turn_id': 't', 'content': [], 'reply_to_seqs': []},
            {'kind': 'turn.start', 'turn_id': 't', 'content': [], 'reply_to_seqs': []},
            {'kind': 'turn.append', 'turn_id': 't', 'content': [{'type': 'text', 'text': 'x'}], 'pre_seq': 1},
            {'kind': 'turn.reset', 'turn_id': 't', 'content': [], 'pre_seq': 1},
            {'kind': 'turn.end', 'turn_id': 't', 'pre_seq': 1},
            {'kind': 'turn.cancel', 'target_turn': {'principal': 2, 'turn_id': 't'}},
            {'kind': 'submission.reserve', 'reserved_through': UINT64_MAX},
        ]
        for payload in good:
            with self.subTest(kind=payload['kind']):
                self.assertEqual(parse_message(self.event(encode_payload(payload))).payload, payload)
        attachment_only = dict(good[2], content=[])
        objects = [pb.ObjectKey(object_id=1, object_token='cap')]
        encoded = encode_payload(attachment_only, object_keys=objects)
        self.assertEqual(parse_message(self.event(encoded, object_keys=objects)).payload, attachment_only)
        with self.assertRaises(ChatProtocolError):
            encode_payload(attachment_only)
        with self.assertRaises(ChatProtocolError):
            parse_message(self.event(attachment_only))

    def test_reject_bad_protocol_without_echoing_payload(self):
        good = {'kind': 'turn.single', 'turn_id': 't', 'content': [], 'reply_to_seqs': []}
        bad = [dict(good, old_field='secret'), dict(good, reply_to_seqs=[True]),
               dict(good, reply_to_seqs=['1']), dict(good, reply_to_seqs=[1, 1]),
               dict(good, reply_to_seqs=[0]), dict(good, reply_to_seqs=[UINT64_MAX + 1]),
               dict(good, turn_id='中' * 43), dict(good, extensions={'x': float('nan')}),
               dict(good, content=[{'type': 'text', 'text': ''}]),
               b'{"kind":"turn.single","kind":"secret"}', b'\xff', b'[]']
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ChatProtocolError) as ctx:
                    parse_message(self.event(payload))
                self.assertNotIn('secret', str(ctx.exception))

    def test_reserve_cannot_have_objects(self):
        with self.assertRaises(ChatProtocolError):
            parse_message(self.event({'kind': 'submission.reserve', 'reserved_through': 10},
                object_keys=[pb.ObjectKey(object_id=1, object_token='private')]))

    def test_failure_is_stable_and_redacted(self):
        class RpcFailure(grpc.RpcError):
            def code(self): return grpc.StatusCode.UNAVAILABLE
            def __str__(self): return 'private credentials'
        failure = make_failure('Fetch', RpcFailure(), attempts=4)
        self.assertEqual(failure.category, 'external_unavailable')
        self.assertFalse(hasattr(failure, 'retryable'))
        self.assertNotIn('private', failure.detail)
        wrapped = ClientFailedError(failure)
        self.assertIs(make_failure('Other', wrapped), failure)
        with self.assertRaises(AttributeError):
            wrapped.failure = failure

    def test_remote_cancel_is_external_but_explicit_close_is_lifecycle(self):
        class RemoteCancellation(grpc.RpcError):
            def code(self): return grpc.StatusCode.CANCELLED
            def __str__(self): return 'private remote context'
        failure = make_failure('Fetch', RemoteCancellation())
        self.assertEqual(failure.category, 'external_unavailable')
        self.assertEqual(failure.grpc_code, grpc.StatusCode.CANCELLED)
        self.assertNotIn('private', failure.detail)
        closed = make_failure('Fetch', ClientClosedError('closed'))
        self.assertEqual(closed.category, 'lifecycle')

    def test_remote_rejections_are_classified_by_rpc_stage(self):
        class RpcFailure(grpc.RpcError):
            def __init__(self, code): self._code = code
            def code(self): return self._code
            def __str__(self): return 'private remote details'

        cases = [
            ('PublishAutoSeq', grpc.StatusCode.INVALID_ARGUMENT, 'request_rejected'),
            ('PublishAutoSeq', grpc.StatusCode.RESOURCE_EXHAUSTED, 'request_rejected'),
            ('WriteObject', grpc.StatusCode.INVALID_ARGUMENT, 'request_rejected'),
            ('WriteObject', grpc.StatusCode.RESOURCE_EXHAUSTED, 'external_unavailable'),
            ('Fetch', grpc.StatusCode.INVALID_ARGUMENT, 'contract'),
            ('PublishAutoSeq', grpc.StatusCode.DATA_LOSS, 'contract'),
            ('PublishAutoSeq', grpc.StatusCode.ABORTED, 'contract'),
        ]
        for stage, code, category in cases:
            with self.subTest(stage=stage, code=code):
                failure = make_failure(stage, RpcFailure(code))
                self.assertEqual(failure.category, category)
                self.assertEqual(failure.grpc_code, code)
                self.assertNotIn('private', failure.detail)
        proven = make_failure('WriteObject', RpcFailure(grpc.StatusCode.INVALID_ARGUMENT),
                              category='contract')
        self.assertEqual(proven.category, 'contract')

    def test_business_principals_and_recipients_must_be_nonzero(self):
        payload = {'kind': 'turn.single', 'turn_id': 't', 'content': [], 'reply_to_seqs': []}
        with self.assertRaises(ChatProtocolError):
            parse_message(self.event(payload, recipients=[0]))
        message = self.event(payload)
        message.principal = 0
        with self.assertRaises(ChatProtocolError):
            parse_message(message)
        with self.assertRaises(ChatProtocolError):
            encode_payload({'kind': 'turn.cancel', 'target_turn': {'principal': 0, 'turn_id': 't'}})


if __name__ == '__main__':
    unittest.main()
