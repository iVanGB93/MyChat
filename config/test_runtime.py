from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, TestCase
from django.core.exceptions import ImproperlyConfigured
from .runtime_config import resolve_broker_url


class BrokerTests(SimpleTestCase):
    def test_production_replaces_loopback_with_shared_redis(self):
        self.assertEqual(resolve_broker_url("redis://localhost:6379/1", "redis://shared:6379/0", production=True), "redis://shared:6379/0")

    def test_explicit_remote_broker_preserves_database_and_credentials(self):
        self.assertEqual(resolve_broker_url("redis://user:pass@worker/3", "redis://shared/0", production=True), "redis://user:pass@worker/3")

    def test_production_fails_closed_without_remote_broker(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_broker_url(None, None, production=True)

    def test_local_development_still_supported(self):
        self.assertEqual(resolve_broker_url(None, None, production=False), "redis://localhost:6379/1")


class ReadinessTests(TestCase):
    @patch("config.health.Redis.from_url")
    def test_healthy_dependencies(self, redis):
        self.assertEqual(self.client.get("/ready/").status_code, 200)
        redis.return_value.__enter__.return_value.ping.assert_called()

    @patch("config.health.Redis.from_url", side_effect=RuntimeError("secret endpoint"))
    def test_failure_is_503_without_secrets(self, redis):
        response = self.client.get("/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertEqual(self.client.get("/health/").status_code, 200)

    @patch("config.health.Redis.from_url")
    @patch("config.health.connection")
    def test_database_failure(self, connection, redis):
        connection.cursor.side_effect = RuntimeError("database credentials")
        self.assertEqual(self.client.get("/ready/").status_code, 503)
