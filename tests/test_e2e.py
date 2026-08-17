from __future__ import annotations

import os
import unittest
import uuid

from openevent.chat_sdk import TextPart, TurnRef, create_client
from openevent.sdk import OpenEventClient


class ChatE2ETest(unittest.TestCase):
    def test_real_start_append_complete_and_cancel(self) -> None:
        required = ("OPENEVENT_E2E_PRINCIPAL", "OPENEVENT_E2E_TOKEN", "OPENEVENT_E2E_CHAT_CHANNEL_ID")
        if any(not os.environ.get(name) for name in required):
            self.skipTest("set OPENEVENT_E2E_PRINCIPAL, OPENEVENT_E2E_TOKEN, and OPENEVENT_E2E_CHAT_CHANNEL_ID")
        principal = int(os.environ["OPENEVENT_E2E_PRINCIPAL"])
        token = os.environ["OPENEVENT_E2E_TOKEN"]
        channel_id = int(os.environ["OPENEVENT_E2E_CHAT_CHANNEL_ID"])
        target = os.environ.get("OPENEVENT_E2E_ADDR", "127.0.0.1:9527")
        turn_id = f"chat-e2e-{uuid.uuid4()}"

        with OpenEventClient(target) as openevent_client:
            with create_client(
                openevent_client,
                principal=principal,
                token=token,
                channel_ids=(channel_id,),
            ) as chat:
                start_seq = chat.start_turn(
                    channel_id=channel_id,
                    turn_id=turn_id,
                    reply_to_turns=(),
                    content=(TextPart("hello"),),
                )
                append_seq = chat.append_turn(channel_id=channel_id, turn_id=turn_id, content=(TextPart(" world"),))
                end_seq = chat.complete_turn(channel_id=channel_id, turn_id=turn_id)
                cancel_seq = chat.cancel_turn(channel_id=channel_id, target_turn=TurnRef(principal, turn_id))

        self.assertLess(start_seq, append_seq)
        self.assertLess(append_seq, end_seq)
        self.assertLess(end_seq, cancel_seq)


if __name__ == "__main__":
    unittest.main()

