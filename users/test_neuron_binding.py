import hashlib
import hmac
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import UserDevice
from .neuron_binding import NeuronBindingView
from chat.neuron_push import send_neuron_call_push


@override_settings(NEURON_PUSH_BRIDGE_SECRET="ab" * 32)
class NeuronBindingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="binding-test")
        self.other = get_user_model().objects.create_user(username="binding-other")
        self.device = UserDevice.objects.create(user=self.user, installation_id="installation-test", fcm_token="fixture-token")

    def request(self, auth_user, **changes):
        data = {"account": "axonic:1:" + "a" * 64, "installation_id": self.device.installation_id, **changes}
        request = APIRequestFactory().post("/api/users/neuron-binding/", data, format="json")
        if auth_user:
            force_authenticate(request, user=auth_user)
        return NeuronBindingView.as_view()(request)

    def test_ticket_uses_authenticated_user_and_current_registration(self):
        response = self.request(self.user, user=self.other.pk)
        self.assertEqual(response.status_code, 200)
        raw = response.data["payload"]
        claim = json.loads(raw)
        self.assertEqual(claim["user"], self.user.pk)
        self.assertEqual(claim["tokenHash"], hashlib.sha256(b"fixture-token").hexdigest())
        self.assertNotIn("fixture-token", raw)
        self.assertEqual(response.data["signature"], hmac.new(("ab" * 32).encode(), raw.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_foreign_inactive_unregistered_and_anonymous_installations_are_rejected(self):
        self.assertIn(self.request(None).status_code, (401, 403))
        self.assertEqual(self.request(self.other).status_code, 409)
        self.assertEqual(self.request(self.user, installation_id="missing").status_code, 409)
        self.device.is_active = False
        self.device.save()
        self.assertEqual(self.request(self.user).status_code, 409)

    def test_malformed_identity_and_missing_configuration_fail_closed(self):
        self.assertEqual(self.request(self.user, account={}).status_code, 400)
        with override_settings(NEURON_PUSH_BRIDGE_SECRET=""):
            self.assertEqual(self.request(self.user).status_code, 503)

    @override_settings(NEURON_REGISTERED_PUSH_ENABLED=True, NEURON_CALL_PUSH_URL="https://neuron.example/v1/call-push")
    @patch("chat.neuron_push.requests.post")
    def test_gateway_target_comes_from_active_device_table(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {"outcome": "sent"})
        send_neuron_call_push(["fixture-token", "old-client-token"], {"callId": "fixture-call"})
        jobs = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
        self.assertEqual(jobs[0]["version"], 2)
        self.assertEqual(jobs[0]["target"], {"user": self.user.pk, "installation": self.device.installation_id})
        self.assertEqual(jobs[1]["version"], 1)
        self.assertNotIn("target", jobs[1])
