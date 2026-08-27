from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from users.models import UserDevice

from .push import send_call_push, send_message_push


User = get_user_model()


class ActionableFcmNotificationTests(TestCase):
    """Raw Android FCM must stay data-only so Notifee owns the UI/actions."""

    def setUp(self):
        self.sender = User.objects.create_user(
            username="push-sender",
            email="push-sender@example.com",
            password="test-password",
        )
        self.recipient = User.objects.create_user(
            username="push-recipient",
            email="push-recipient@example.com",
            password="test-password",
        )
        UserDevice.objects.create(
            user=self.recipient,
            installation_id="push-recipient-installation",
            fcm_token="raw-fcm-token",
            platform=UserDevice.PLATFORM_ANDROID,
        )

    @patch("chat.push._send_expo_push")
    @patch("chat.push._send_fcm_data", return_value=True)
    def test_message_push_is_data_only(self, send_fcm, send_expo):
        sent = send_message_push(
            recipient_ids=[self.recipient.id],
            sender_name=self.sender.username,
            sender_id=self.sender.id,
            content="hello",
            room_id="room-1",
            room_name=self.sender.username,
            message_id="message-1",
            message_type="text",
        )

        self.assertTrue(sent)
        kwargs = send_fcm.call_args.kwargs
        self.assertNotIn("title", kwargs)
        self.assertNotIn("body", kwargs)
        self.assertEqual(kwargs["data"]["type"], "new_message")
        self.assertEqual(kwargs["data"]["content"], "hello")
        self.assertEqual(kwargs["data"]["body"], "hello")
        send_expo.assert_not_called()

    @patch("chat.push._send_expo_push")
    @patch("chat.push._send_fcm_data", return_value=True)
    def test_call_push_is_data_only(self, send_fcm, send_expo):
        sent = send_call_push(
            callee_id=self.recipient.id,
            caller_name=self.sender.username,
            call_type="voice",
            call_id="call-1",
            caller_id=self.sender.id,
            room_name="call-room",
        )

        self.assertTrue(sent)
        kwargs = send_fcm.call_args.kwargs
        self.assertNotIn("title", kwargs)
        self.assertNotIn("body", kwargs)
        self.assertNotIn("notification_tag", kwargs)
        self.assertEqual(kwargs["data"]["type"], "incoming_call")
        self.assertEqual(kwargs["data"]["callId"], "call-1")
        send_expo.assert_not_called()
