from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from unittest.mock import patch
from users.models import BlockedUser
from .models import CallLog


class CallSafetyTests(TestCase):
    def setUp(self):
        self.caller = get_user_model().objects.create_user(username="audit-caller")
        self.callee = get_user_model().objects.create_user(username="audit-callee")
        self.client = APIClient()

    def test_blocked_relationship_cannot_initiate_call_in_either_direction(self):
        BlockedUser.objects.create(owner=self.callee, blocked=self.caller)
        for sender, recipient in [(self.caller, self.callee), (self.callee, self.caller)]:
            self.client.force_authenticate(sender)
            response = self.client.post('/api/calls/initiate/', {'callee_id': recipient.id, 'call_type': 'voice'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(CallLog.objects.count(), 0)

    @patch('calls.views.get_channel_layer')
    def test_accept_after_hangup_never_revives_call(self, layer):
        from unittest.mock import AsyncMock
        layer.return_value.group_send = AsyncMock()
        call = CallLog.objects.create(caller=self.caller, callee=self.callee,
                                      call_type='voice', room_name='audit', status=CallLog.RINGING)
        self.client.force_authenticate(self.caller)
        self.assertEqual(self.client.post(f'/api/calls/{call.id}/end/').status_code, 200)
        self.client.force_authenticate(self.callee)
        self.assertEqual(self.client.post(f'/api/calls/{call.id}/join/').status_code, 409)
        call.refresh_from_db()
        self.assertEqual(call.status, CallLog.ENDED)

    @patch('calls.views.send_call_push')
    @patch('calls.views.get_user_notification_channels')
    def test_repeated_and_crossed_starts_do_not_notify_or_create_calls(self, channels, push):
        CallLog.objects.create(caller=self.caller, callee=self.callee,
                               call_type='video', room_name='existing', status=CallLog.RINGING)
        for sender, recipient in [(self.caller, self.callee), (self.callee, self.caller)]:
            self.client.force_authenticate(sender)
            for _ in range(3):
                response = self.client.post('/api/calls/initiate/', {'callee_id': recipient.id, 'call_type': 'voice'})
                self.assertEqual(response.status_code, 409)
        self.assertEqual(CallLog.objects.count(), 1)
        channels.assert_not_called()
        push.assert_not_called()

    def test_cannot_start_another_call_while_connected(self):
        third = get_user_model().objects.create_user(username='third')
        CallLog.objects.create(caller=self.caller, callee=third,
                               call_type='voice', room_name='connected', status=CallLog.ONGOING)
        self.client.force_authenticate(self.caller)
        response = self.client.post('/api/calls/initiate/', {'callee_id': self.callee.id})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CallLog.objects.count(), 1)
