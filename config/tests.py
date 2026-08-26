from django.test import TestCase, override_settings
from django.urls import reverse


class AppVersionViewTests(TestCase):
    @override_settings(
        APP_LATEST_VERSION="1.0.26",
        APP_MIN_SUPPORTED_VERSION="1.0.20",
        APP_STORE_URL_ANDROID="https://play.google.com/store/apps/details?id=com.axonic",
        APP_STORE_URL_IOS="https://apps.apple.com/app/axonic/id123",
    )
    def test_returns_platform_specific_update_policy_without_caching(self):
        response = self.client.get(reverse("app-version"), {"platform": "android"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "latest": "1.0.26",
            "min_supported": "1.0.20",
            "store_url": "https://play.google.com/store/apps/details?id=com.axonic",
            "store_url_android": "https://play.google.com/store/apps/details?id=com.axonic",
            "store_url_ios": "https://apps.apple.com/app/axonic/id123",
        })
        self.assertIn("no-cache", response.headers["Cache-Control"])

    @override_settings(
        APP_STORE_URL_ANDROID="https://play.google.com/store/apps/details?id=com.axonic",
        APP_STORE_URL_IOS="",
    )
    def test_does_not_send_ios_users_to_google_play(self):
        response = self.client.get(reverse("app-version"), {"platform": "ios"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["store_url"], "")
