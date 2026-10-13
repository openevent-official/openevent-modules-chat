import json
import unittest

import grpc
from openevent.sdk.proto import openevent_pb2 as pb
from openevent.chat_sdk import RawExtensions, serialize_payload
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
        self.assertEqual(message.payload['extensions'].json_text, '{"nested":[null,true]}')
        self.assertEqual({k: v for k, v in message.payload.items() if k != 'extensions'},
                         {k: v for k, v in payload.items() if k != 'extensions'})
        self.assertEqual(message.recipients, (2, 2, 1))
        self.assertEqual(len(message.object_keys), 2)
        self.assertNotIn('secret', repr(message))

    def test_no_reply_history_validation(self):
        payload = {'kind': 'turn.single', 'turn_id': 't', 'content': [], 'reply_to_seqs': [99]}
        self.assertEqual(parse_message(self.event(payload)).payload, payload)

    def test_opaque_extensions_preserve_numbers_whitespace_and_escaped_values(self):
        raw = ('{ "large": 1e400, "precise": 0.123456789012345678901234567890, '
               '"integer": ' + '9' * 5000 + ', "negativeZero": -0, '
               '"text": "} ] \\\" \\u4e2d", "nested": [null, true, false] }')
        prefix = '"kind":"turn.single","turn_id":"t","content":[],"reply_to_seqs":[]'
        for wire in ('{"extensions":' + raw + ',' + prefix + '}',
                     '{' + prefix + ',"extens\\u0069ons":' + raw + '}',
                     '{"kind":"turn.single","extensions":' + raw + ','
                     '"turn_id":"t","content":[],"reply_to_seqs":[]}'):
            with self.subTest(position=wire[:30]):
                parsed = parse_message(self.event(wire.encode()))
                extension = parsed.payload['extensions']
                self.assertIsInstance(extension, RawExtensions)
                self.assertEqual(extension.json_text, raw)
                self.assertIn(('"extensions":' + raw).encode(), encode_payload(parsed.payload))

    def test_raw_extensions_validate_object_syntax_and_are_immutable(self):
        raw = RawExtensions(' \n{ "secret": 1e400 }\t')
        self.assertEqual(raw.json_text, ' \n{ "secret": 1e400 }\t')
        self.assertNotIn('secret', repr(raw))
        with self.assertRaises(AttributeError):
            raw.json_text = '{}'
        invalid = [None, {}, b'{}', '', '[]', 'null', '{}{}', '{"x":NaN}',
                   '{"x":Infinity}', '{"x":-Infinity}', '{"x":01}', '{"x":1e}',
                   '{"x":1.}', '{"x":+1}', '{"x":.1}', '{"x":١}',
                   '{"x":true false}', '{"x":undefined}', '{"x":1,}',
                   '{"x":[1,]}', '{"x":[,1]}', '{"x":[1 2]}',
                   '{"x" 1}', '{"x":}', '{"x":1 "y":2}', '{"x":"\\q"}',
                   '{"x":"\\u12xz"}', '{"x":"\n"}', '{"x":"\ud800"}',
                   '{\u00a0"x":1}', '{"x":[1}', '{"x":{"a":1]',
                   '{"x":"unterminated}', '{"x":"\\', '{"x":truex}']
        for text in invalid:
            with self.subTest(text=repr(text)):
                with self.assertRaises(ChatProtocolError):
                    RawExtensions(text)

    def test_extension_values_are_opaque_including_duplicate_keys_and_unicode_escapes(self):
        raw = r'{"x":1,"x":2,"\u0078":3,"nested":{"a":1,"a":2},"text":"\ud800"}'
        extension = RawExtensions(raw)
        payload = {'kind': 'turn.end', 'turn_id': 't', 'pre_seq': 1, 'extensions': extension}
        parsed = parse_message(self.event(encode_payload(payload)))
        self.assertEqual(parsed.payload['extensions'].json_text, raw)

    def test_deep_extensions_do_not_depend_on_python_recursion_limit(self):
        raw = '{"nested":' + '[{"x":' * 3000 + '1e400' + '}]' * 3000 + '}'
        payload = {'kind': 'turn.end', 'turn_id': 't', 'pre_seq': 1,
                   'extensions': RawExtensions(raw)}
        encoded = encode_payload(payload)
        parsed = parse_message(self.event(encoded))
        self.assertEqual(parsed.payload['extensions'].json_text, raw)
        self.assertEqual(encode_payload(parsed.payload), encoded)

    def test_necessary_fields_keep_strict_validation(self):
        invalid = [
            b'{"kind":"turn.end","turn_id":"t","pre_seq":1e400}',
            b'{"kind":"turn.end","turn_id":"t","pre_seq":1,"extensions":[]}',
            b'{"kind":"turn.end","turn_id":"t","pre_seq":1,"extensions":{},"extens\\u0069ons":{}}',
            b'{"kind":"turn.cancel","target_turn":{"principal":1,"turn_id":"t","principal":2}}',
            b'{"kind":"turn.single","turn_id":"t","reply_to_seqs":[],"content":[{"type":"text","text":"a","text":"b"}]}',
            b'{"kind":"turn.end","turn_id":"\\ud800","pre_seq":1}',
            b'{"kind":"turn.end","turn_id":"t","pre_seq":1,"extensions":{"x":1e}}',
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ChatProtocolError):
                    parse_message(self.event(payload))

    def test_serialize_payload_preserves_raw_extensions_after_public_field_conversion(self):
        raw = RawExtensions('{ "x": 1e400, "x": -0 }')
        payload = {'kind': 'turn.end', 'turn_id': 't', 'pre_seq': str(UINT64_MAX),
                   'extensions': raw}
        encoded = serialize_payload(payload)
        self.assertEqual(encoded, ('{"kind":"turn.end","turn_id":"t","pre_seq":"'
                                  + str(UINT64_MAX) + '","extensions":'
                                  + raw.json_text + '}').encode())
        with self.assertRaises(ChatProtocolError):
            encode_payload(payload)

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

    def test_parse_message_does_not_parse_or_hide_system_events(self):
        message = self.event({'kind': 'system.member_added', 'data': {
            'schema_version': 1, 'member': 2}})
        message.principal = 0
        with self.assertRaises(ChatProtocolError):
            parse_message(message)


if __name__ == '__main__':
    unittest.main()
