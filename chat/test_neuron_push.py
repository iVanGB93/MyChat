import hashlib
import hmac
import json
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings
from chat.neuron_push import send_neuron_call_push, send_neuron_message_push


@override_settings(NEURON_CALL_PUSH_URL="https://neuron.example/v1/call-push",
                   NEURON_PUSH_BRIDGE_SECRET="ab" * 32, NEURON_PUSH_CA_BUNDLE="")
class NeuronPushTests(SimpleTestCase):
    data = {"type": "incoming_call", "callId": "call-1", "callerId": 18,
            "callerName": "Test", "callType": "voice", "roomName": "test"}

    @override_settings(NEURON_CALL_PUSH_URL="")
    def test_disabled_preserves_legacy_sender(self):
        self.assertIsNone(send_neuron_call_push(["fixture-token"], self.data))

    @patch("chat.neuron_push.requests.post")
    def test_signed_request_deduplicates_tokens_and_verifies_tls(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"outcome": "sent"})
        self.assertTrue(send_neuron_call_push(["fixture-token"] * 2, self.data))
        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertIs(kwargs["verify"], True)
        self.assertFalse(kwargs["allow_redirects"])
        raw = kwargs["data"]
        self.assertEqual(kwargs["headers"]["X-Axonic-Push-Signature"],
                         hmac.new(("ab" * 32).encode(), raw, hashlib.sha256).hexdigest())
        job = json.loads(raw)
        self.assertEqual(job["id"], hashlib.sha256(b"call-1\0fixture-token").hexdigest())
        self.assertEqual(job["data"]["callerId"], "18")
        self.assertEqual(job["data"]["channelId"], "incoming-calls-v2")

    @patch("chat.neuron_push.requests.post", side_effect=requests.Timeout)
    def test_ambiguous_send_does_not_retry(self, post):
        self.assertTrue(send_neuron_call_push(["fixture-token"], self.data))
        self.assertEqual(post.call_count, 1)

    @override_settings(NEURON_CALL_PUSH_URL="http://neuron.example/v1/call-push")
    @patch("chat.neuron_push.requests.post")
    def test_insecure_endpoint_is_rejected(self, post):
        self.assertFalse(send_neuron_call_push(["fixture-token"], self.data))
        post.assert_not_called()

    @patch("chat.neuron_push.requests.post")
    def test_definitive_failure_is_not_success(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"outcome": "not_sent"})
        self.assertFalse(send_neuron_call_push(["fixture-token"], self.data))


@override_settings(NEURON_MESSAGE_PUSH_URL="https://neuron.example/v1/message-push",
                   NEURON_PUSH_BRIDGE_SECRET="ab" * 32, NEURON_PUSH_CA_BUNDLE="")
class NeuronMessagePushTests(SimpleTestCase):
    data = {"type": "new_message", "messageId": "msg-1", "roomId": "room-1",
            "senderId": 18, "content": "Hello", "messageType": "text", "message_type": "text"}

    @patch("chat.neuron_push.requests.post")
    def test_message_keeps_content_and_strips_fcm_reserved_fields(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"outcome": "sent"})
        self.assertTrue(send_neuron_message_push(["fixture-token"], self.data))
        packet = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(packet["data"]["content"], "Hello")
        self.assertEqual(packet["data"]["channelId"], "messages")
        self.assertNotIn("message_type", packet["data"])
        self.assertEqual(packet["id"], hashlib.sha256(b"message:msg-1\0fixture-token").hexdigest())

    @patch("chat.neuron_push.requests.post")
    def test_unsupported_payload_returns_to_legacy_before_any_remote_send(self, post):
        for changes in ({"messageId": ""}, {"content": "x" * 2001}, {"image_b64": "abc"},
                        {"message_id": "different"}, {"content": "😀" * 1001}):
            self.assertIsNone(send_neuron_message_push(["fixture-token"], {**self.data, **changes}))
        post.assert_not_called()

    @patch("chat.neuron_push.requests.post", side_effect=requests.Timeout)
    def test_ambiguous_message_send_is_not_fallback(self, post):
        self.assertIs(send_neuron_message_push(["fixture-token"], self.data), True)
        self.assertEqual(post.call_count, 1)
