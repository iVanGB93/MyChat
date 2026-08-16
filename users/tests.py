from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from django.urls import reverse

from .models import PasswordResetRequest

User = get_user_model()


class PasswordResetFlowTests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.user = User.objects.create_user(
			username="alice",
			email="alice@example.com",
			password="OldPass123!",
		)

	@patch("users.views._send_password_reset_email")
	def test_request_verify_and_confirm_password_reset(self, mock_send_email):
		request_resp = self.client.post(
			reverse("password-reset-request"),
			{"email": "alice@example.com"},
			format="json",
		)

		self.assertEqual(request_resp.status_code, 200)
		self.assertTrue(PasswordResetRequest.objects.filter(email="alice@example.com").exists())

		sent_code = mock_send_email.call_args[0][1]

		verify_resp = self.client.post(
			reverse("password-reset-verify"),
			{"email": "alice@example.com", "code": sent_code},
			format="json",
		)

		self.assertEqual(verify_resp.status_code, 200)

		confirm_resp = self.client.post(
			reverse("password-reset-confirm"),
			{"email": "alice@example.com", "code": sent_code, "new_password": "NewPass456!"},
			format="json",
		)

		self.assertEqual(confirm_resp.status_code, 200)

		self.user.refresh_from_db()
		self.assertTrue(self.user.check_password("NewPass456!"))
		self.assertFalse(PasswordResetRequest.objects.filter(email="alice@example.com").exists())
