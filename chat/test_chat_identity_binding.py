import json
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from .chat_identity_binding import parse_chat_binding
from .consumers import NotificationConsumer


def challenge():
    room = str(uuid4())
    return {"type": "chat_identity_binding", "protocol": 1, "kind": "challenge", "room_id": room,
            "target_user_id": 18, "payload": {"version": 1, "roomId": room, "requester": 14,
            "requesterAccount": "axonic:1:" + "a1" * 32, "peer": 18, "nonce": "ab" * 32,
            "expiresAt": int(time.time() * 1000) + 50_000}}


class ChatBindingTests(SimpleTestCase):
    def test_parser_rejects_identity_spoofing_expiry_and_extra_payload(self):
        frame = challenge()
        self.assertIsNotNone(parse_chat_binding(frame, 14))
        self.assertIsNone(parse_chat_binding(frame, 99))
        for change in [{"requester": True}, {"peer": 99}, {"nonce": "bad"}, {"expiresAt": 0},
                       {"expiresAt": frame["payload"]["expiresAt"] + 60_000}, {"text": "not metadata"}]:
            self.assertIsNone(parse_chat_binding({**frame, "payload": {**frame["payload"], **change}}, 14))
        for change in [{"protocol": True}, {"room_id": str(uuid4())}, {"target_user_id": True},
                       {"kind": []}, {"payload": None}, {"padding": "x" * 12_000}]:
            self.assertIsNone(parse_chat_binding({**frame, **change}, 14))

    def test_proof_requires_bounded_public_record_and_reverse_participants(self):
        frame = challenge()
        record = {"version": 1, "account": "axonic:1:" + "b2" * 32, "root": "11" * 32,
                  "revision": 0, "previous": None, "issuedAt": 1, "expiresAt": 2,
                  "devices": [{"id": "22" * 32, "signing": "33" * 32, "encryption": "44" * 32}],
                  "signature": "55" * 64}
        frame.update(kind="proof", target_user_id=14, payload={"version": 1, "challenge": frame["payload"],
                     "record": record, "device": "22" * 32, "signature": "66" * 64})
        self.assertIsNotNone(parse_chat_binding(frame, 18))  # Signatures are verified by the receiving app.
        self.assertIsNone(parse_chat_binding(frame, 14))
        record["private_key"] = "must not relay"
        self.assertIsNone(parse_chat_binding(frame, 18))

    def consumer(self, waiting=False, limited=False):
        c = NotificationConsumer()
        c.user = SimpleNamespace(id=14)
        c.channel_name = "binding-test"
        c._awaiting_auth = waiting
        c._text_signal_times = deque([float("inf")] * 120 if limited else [])
        c.channel_layer = SimpleNamespace(group_send=AsyncMock())
        return c

    def test_authenticated_sender_is_injected_and_no_client_extras_are_forwarded(self):
        c = self.consumer()
        with patch("chat.consumers.authorize_text_signal", return_value=True) as authorize:
            async_to_sync(c.receive)(json.dumps({**challenge(), "from_user_id": 99, "content": "not forwarded"}))
        args = c.channel_layer.group_send.call_args.args
        self.assertEqual(args[0], "notifications_18")
        self.assertEqual(args[1]["payload"]["from_user_id"], 14)
        self.assertEqual(args[1]["payload"]["event"], "chat_identity_binding")
        self.assertNotIn("content", args[1]["payload"])
        self.assertEqual(authorize.call_args.args[0], 14)

    def test_unauthorized_unauthed_and_rate_limited_requests_do_not_forward(self):
        for waiting, limited, allowed in [(True, False, True), (False, True, True), (False, False, False)]:
            c = self.consumer(waiting, limited)
            with patch("chat.consumers.authorize_text_signal", return_value=allowed):
                async_to_sync(c.receive)(json.dumps(challenge()))
            c.channel_layer.group_send.assert_not_called()
