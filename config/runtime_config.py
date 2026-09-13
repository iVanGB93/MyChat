"""Small, independently testable deployment configuration rules."""
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def resolve_broker_url(explicit, redis_url, *, production):
    explicit = (explicit or "").strip()
    redis_url = (redis_url or "").strip()
    def loopback(value):
        return urlsplit(value).hostname in {"localhost", "127.0.0.1", "::1"}
    if production and (not explicit or loopback(explicit)):
        if not redis_url or loopback(redis_url):
            raise ImproperlyConfigured("Production requires a reachable Celery broker URL.")
        return redis_url
    return explicit or redis_url or "redis://localhost:6379/1"
