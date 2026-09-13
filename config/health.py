"""Liveness is cheap; readiness verifies dependencies without exposing details."""
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from redis import Redis


@never_cache
def readiness(request):
    healthy = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        healthy = False
    urls = {settings.CELERY_BROKER_URL}
    for host in settings.CHANNEL_LAYERS.get("default", {}).get("CONFIG", {}).get("hosts", []):
        urls.add(host["address"] if isinstance(host, dict) else host)
    for url in urls:
        try:
            with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as client:
                client.ping()
        except Exception:
            healthy = False
    return JsonResponse({"status": "ok" if healthy else "unavailable"}, status=200 if healthy else 503)
