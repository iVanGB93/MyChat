"""
Custom WebSocket authentication middleware.

Reads a JWT token from the WebSocket query string (?token=xxx)
and authenticates the user for the Channels consumer.
"""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


@database_sync_to_async
def get_user_from_token(token_str: str):
    """Validate a JWT access token and return the corresponding user.

    Also verifies the token's `tv` (token_version) claim matches the
    current value on the user record — bumping `token_version` (via
    "Logout all devices") instantly invalidates every WebSocket token.
    """
    try:
        token = AccessToken(token_str)
        user_id = token["user_id"]
        user = User.objects.get(id=user_id)
        token_tv = token.get("tv", 0)
        if int(token_tv) != int(getattr(user, "token_version", 0)):
            return AnonymousUser()
        return user
    except Exception:
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Channels middleware that authenticates WebSocket connections
    via a JWT token passed as a query parameter: ws://host/ws/.../?token=<jwt>
    """

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        token_list = params.get("token", [])

        if token_list:
            scope["user"] = await get_user_from_token(token_list[0])
        else:
            scope["user"] = AnonymousUser()

        return await super().__call__(scope, receive, send)
