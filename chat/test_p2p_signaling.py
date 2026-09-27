import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.test import TestCase, SimpleTestCase

from users.models import User, BlockedUser
from .models import ChatRoom, MessageDelivery
from .p2p_signaling import parse_text_signal, authorize_text_signal
from .consumers import NotificationConsumer


def offer(**changes):
    return {"type": "p2p_text_signal", "protocol": 1, "signal_type": "offer",
            "session_id": str(uuid4()), "room_id": str(uuid4()),
            "target_user_id": 2, "data": {"sdp": "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel"}, **changes}


class TextSignalValidationTests(SimpleTestCase):
    def test_invalid_or_oversized_frames_are_rejected(self):
        for change in [{"protocol": True}, {"protocol": 2}, {"room_id": "bad"},
                       {"target_user_id": True}, {"signal_type": []}, {"signal_type": "send_message"},
                       {"data": {"sdp": "x" * 32_001}}, {"content": "x" * 48_000}]:
            with self.subTest(change=list(change)):
                self.assertIsNone(parse_text_signal(offer(**change)))

    def test_replies_require_endpoint_and_extra_identity_is_not_forwarded(self):
        self.assertIsNone(parse_text_signal(offer(signal_type="answer")))
        parsed = parse_text_signal(offer(from_user_id=999, content="must not be forwarded"))
        self.assertNotIn("from_user_id", parsed)
        self.assertNotIn("content", parsed)
        self.assertEqual(parsed["data"]["type"], "offer")

    def test_ice_is_bounded_and_typed(self):
        base = offer(signal_type="ice", target_endpoint_id=str(uuid4()),
                     data={"candidate": "candidate:1", "sdpMid": "0", "sdpMLineIndex": 0})
        self.assertIsNotNone(parse_text_signal(base))
        for change in [{"candidate": ""}, {"candidate": "x" * 2049}, {"sdpMLineIndex": True}, {"sdpMid": []}]:
            self.assertIsNone(parse_text_signal({**base, "data": {**base["data"], **change}}))

    def test_consumer_uses_authenticated_identity_and_separate_namespace(self):
        consumer = NotificationConsumer()
        consumer.user = SimpleNamespace(id=17)
        consumer.channel_name = 'test-text-sender'
        consumer._awaiting_auth = False
        consumer._text_signal_times = deque()
        consumer._text_endpoint_id = str(uuid4())
        consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
        frame = offer(from_user_id=999)
        with patch('chat.consumers.authorize_text_signal', return_value=True):
            async_to_sync(consumer.receive)(json.dumps(frame))
        args = consumer.channel_layer.group_send.call_args.args
        self.assertEqual(args[0], 'notifications_2')
        self.assertEqual(args[1]['type'], 'p2p.text.signal')
        self.assertEqual(args[1]['payload']['from_user_id'], 17)
        self.assertEqual(args[1]['payload']['from_endpoint_id'], consumer._text_endpoint_id)

    def test_unauthed_and_rate_limited_signals_do_not_relay(self):
        for waiting in [True, False]:
            consumer = NotificationConsumer()
            consumer.user = SimpleNamespace(id=17)
            consumer.channel_name = 'test-text-sender'
            consumer._awaiting_auth = waiting
            consumer._text_signal_times = deque([float('inf')] * 120)
            consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
            async_to_sync(consumer.receive)(json.dumps(offer()))
            consumer.channel_layer.group_send.assert_not_called()

    def test_answer_only_reaches_selected_installation(self):
        consumer = NotificationConsumer()
        consumer._text_endpoint_id = str(uuid4())
        consumer.send = AsyncMock()
        async_to_sync(consumer.p2p_text_signal)({"payload": {"target_endpoint_id": str(uuid4())}})
        consumer.send.assert_not_called()
        async_to_sync(consumer.p2p_text_signal)({"payload": {"target_endpoint_id": consumer._text_endpoint_id}})
        consumer.send.assert_called_once()


class TextSignalAuthorizationTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(username='p2p-a')
        self.b = User.objects.create_user(username='p2p-b')
        self.c = User.objects.create_user(username='p2p-outsider')
        self.room = ChatRoom.objects.create()
        self.room.members.set([self.a, self.b])
        self.signal = parse_text_signal(offer(room_id=str(self.room.id), target_user_id=self.b.id))

    def test_valid_connection_metadata_creates_no_message_records(self):
        self.assertTrue(authorize_text_signal(self.a.id, self.signal))
        self.assertFalse(MessageDelivery.objects.exists())

    def test_outsiders_self_and_revoked_membership_are_rejected(self):
        self.assertFalse(authorize_text_signal(self.c.id, self.signal))
        self.assertFalse(authorize_text_signal(self.b.id, self.signal))
        self.room.members.remove(self.b)
        self.assertFalse(authorize_text_signal(self.a.id, self.signal))

    def test_groups_and_malformed_direct_rooms_are_rejected(self):
        self.room.members.add(self.c)
        self.assertFalse(authorize_text_signal(self.a.id, self.signal))
        self.room.members.remove(self.c)
        self.room.room_type = ChatRoom.GROUP
        self.room.save()
        self.assertFalse(authorize_text_signal(self.a.id, self.signal))

    def test_either_block_direction_stops_signaling(self):
        for owner, blocked in [(self.a, self.b), (self.b, self.a)]:
            row = BlockedUser.objects.create(owner=owner, blocked=blocked)
            self.assertFalse(authorize_text_signal(self.a.id, self.signal))
            row.delete()
