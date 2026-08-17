from __future__ import annotations

import unittest
from unittest.mock import patch

from openevent.chat_sdk import (
    ChatProtocolError,
    InvalidKindError,
    MalformedPayloadError,
    ObjectKey,
    TextPart,
    TurnAppend,
    TurnCancel,
    TurnEnd,
    TurnRef,
    TurnStart,
    parse_message,
    parse_payload,
)

from tests.fakes import message


class CodecTest(unittest.TestCase):
    def test_parses_all_event_kinds(self) -> None:
        start = parse_payload(
            b'{"kind":"turn.start","turn_id":"u-1","reply_to_turns":[],"content":[{"type":"text","text":"hello"}]}'
        )
        append = parse_payload(
            b'{"kind":"turn.append","turn_id":"u-1","pre_seq":1,"content":[{"type":"text","text":" world"}]}'
        )
        end = parse_payload(b'{"kind":"turn.end","turn_id":"u-1","pre_seq":2,"status":"completed"}')
        cancel = parse_payload(b'{"kind":"turn.cancel","target_turn":{"principal":2001,"turn_id":"u-1"}}')

        self.assertIsInstance(start, TurnStart)
        self.assertEqual(start.content, (TextPart("hello"),))
        self.assertIsInstance(append, TurnAppend)
        self.assertIsInstance(end, TurnEnd)
        self.assertIsInstance(cancel, TurnCancel)
        self.assertEqual(cancel.target_turn, TurnRef(2001, "u-1"))

    def test_strict_json_rejects_ambiguous_or_non_utf8_input(self) -> None:
        invalid = (
            b'{"kind":"turn.end","kind":"turn.cancel"}',
            b'{"kind":"turn.cancel","target_turn":{"principal":1,"turn_id":"x"},"extensions":{"x":NaN}}',
            b'\xff',
            b'{"kind":"turn.cancel","target_turn":{"principal":1,"turn_id":"x"},"extensions":null}',
            b'{"kind":"turn.cancel","target_turn":{"principal":1,"turn_id":"x"},"extra":1}',
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ChatProtocolError):
                parse_payload(payload)

    def test_json_parser_value_error_is_a_malformed_payload(self) -> None:
        with patch("openevent.chat_sdk.codec.json.loads", side_effect=ValueError("number exceeds parser limit")):
            with self.assertRaises(MalformedPayloadError):
                parse_payload(b"{}")

    def test_rejects_unknown_kind_and_invalid_fields(self) -> None:
        with self.assertRaises(InvalidKindError):
            parse_payload(b'{"kind":"turn.unknown"}')
        with self.assertRaises(ChatProtocolError):
            parse_payload(b'{"kind":"turn.end","turn_id":"x","pre_seq":0,"status":"completed"}')
        with self.assertRaises(MalformedPayloadError):
            parse_payload(b'[]')

    def test_parse_message_preserves_envelope_and_redacts_object_token(self) -> None:
        key = ObjectKey(91, "secret-object-token")
        parsed = parse_message(
            message(
                seq=7,
                channel_id=1001,
                principal=2001,
                recipients=(3001, 3001),
                object_keys=(key,),
                payload=b'{"kind":"turn.cancel","target_turn":{"principal":4001,"turn_id":"a"}}',
            )
        )
        self.assertEqual(parsed.seq, 7)
        self.assertEqual(parsed.recipients, (3001, 3001))
        self.assertEqual(parsed.turn_ref, TurnRef(4001, "a"))
        self.assertIs(parsed.event, parsed.payload)
        self.assertNotIn("secret-object-token", repr(parsed))
        self.assertIn("<redacted>", repr(parsed.object_keys[0]))

    def test_public_models_validate_ranges_and_text(self) -> None:
        with self.assertRaises(ChatProtocolError):
            TurnRef(True, "x")
        with self.assertRaises(ChatProtocolError):
            TurnRef(1, "x" * 129)
        with self.assertRaises(ChatProtocolError):
            TextPart("")
        with self.assertRaises(ChatProtocolError):
            ObjectKey(0, "token")


if __name__ == "__main__":
    unittest.main()
