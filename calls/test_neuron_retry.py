import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from users.models import UserDevice
from .models import CallLog
from .tasks import sweep_stale_call_invites


@override_settings(NEURON_CALL_PUSH_URL="https://neuron.example/v1/call-push",
                   NEURON_PUSH_BRIDGE_SECRET="ab" * 32, NEURON_REGISTERED_PUSH_ENABLED=True)
class NeuronRetryTests(TestCase):
    @patch("chat.push._send_fcm_data")
    @patch("chat.neuron_push.requests.post")
    def test_worker_targets_registered_installation_and_skips_ended_or_acknowledged_calls(self, post, local):
        user = get_user_model()
        sender = user.objects.create_user(username="retry-sender")
        receiver = user.objects.create_user(username="retry-receiver")
        UserDevice.objects.create(user=receiver, installation_id="retry-installation", fcm_token="fixture-token")
        calls = [CallLog.objects.create(caller=sender, callee=receiver, call_type="voice", status=status,
                                       room_name="retry-room") for status in (CallLog.RINGING, CallLog.ENDED, CallLog.RINGING)]
        CallLog.objects.filter(pk__in=[c.pk for c in calls]).update(started_at=timezone.now() - timedelta(seconds=40))
        CallLog.objects.filter(pk=calls[2].pk).update(invite_acked_at=timezone.now())
        post.return_value = Mock(status_code=200, json=lambda: {"outcome": "sent"})
        result = sweep_stale_call_invites()
        self.assertEqual(result, {"scanned": 1, "resent": 1, "failed": 0})
        packet = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(packet["version"], 2)
        self.assertEqual(packet["target"], {"user": receiver.pk, "installation": "retry-installation"})
        self.assertEqual(packet["data"]["routeReason"], "push_retry_sweep")
        self.assertEqual(sweep_stale_call_invites()["scanned"], 0)
        local.assert_not_called()
