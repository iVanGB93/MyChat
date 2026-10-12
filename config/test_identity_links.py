from django.test import SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class IdentityInviteTests(SimpleTestCase):
    code = "axon:L8HfT2dk1oNSjCZsYK3MbUD8ZSskMKY73ZmdRNYhLmCRYS37c"

    def test_checked_identity_opens_public_page_without_database_or_login(self):
        response = self.client.get(reverse("identity-invite", args=[self.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.code)
        self.assertContains(response, 'href="axonic://identity/axon%3A')
        self.assertContains(response, "Open Axonic")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_rejects_abbreviated_and_corrupted_addresses(self):
        for invalid in ["axon:L8HfT2...RYS37c", self.code[:-1], self.code[:-1] + "d", "not-an-id"]:
            self.assertEqual(self.client.get(reverse("identity-invite", args=[invalid])).status_code, 400)
