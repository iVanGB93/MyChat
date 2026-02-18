"""
ASGI config for ChatConnect.

Configures Django Channels with WebSocket routing.
"""

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Initialize Django ASGI application early to ensure apps are loaded
django_asgi_app = get_asgi_application()

# Import after Django setup
from chat.middleware import JWTAuthMiddleware  # noqa: E402
from chat.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # AllowedHostsOriginValidator removed: React Native WS clients
        # may not send an Origin header, causing connections to be rejected.
        # ALLOWED_HOSTS = ['*'] so origin validation is unnecessary.
        "websocket": JWTAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
