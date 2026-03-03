import base64
import hashlib
import hmac
import time
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CallLog
from .serializers import CallLogSerializer
from chat.push import send_call_push
from chat.consumers import is_user_ws_connected


def _build_ice_servers(connectivity_mode: str = 'auto') -> list[dict]:
    """
    Build the list of ICE servers to send to the client based on the
    user's connectivity mode preference:

      'auto'   → STUN + TURN (WebRTC picks best path automatically)
      'p2p'    → STUN only   (never relay through TURN)
      'server' → TURN only   (always relay, iceTransportPolicy='relay')
    """
    stun_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]

    turn_urls = getattr(settings, 'TURN_URLS', [])
    turn_secret = getattr(settings, 'TURN_SECRET', '')
    turn_username_static = getattr(settings, 'TURN_USERNAME', '')
    turn_credential_static = getattr(settings, 'TURN_CREDENTIAL', '')
    ttl = getattr(settings, 'TURN_TTL', 86400)

    turn_servers = []
    if turn_urls:
        if turn_secret:
            # HMAC time-limited credentials (Coturn `use-auth-secret` mode)
            expiry = int(time.time()) + ttl
            username = f"{expiry}:axonic"
            credential = base64.b64encode(
                hmac.new(turn_secret.encode(), username.encode(), hashlib.sha1).digest()
            ).decode()
        elif turn_username_static and turn_credential_static:
            # Static credentials
            username = turn_username_static
            credential = turn_credential_static
        else:
            username = credential = None

        if username and credential:
            turn_servers = [
                {"urls": url, "username": username, "credential": credential}
                for url in turn_urls
            ]

    if connectivity_mode == 'p2p':
        return {"ice_servers": stun_servers, "ice_transport_policy": "all"}
    elif connectivity_mode == 'server':
        # Force relay — needs TURN servers configured
        return {"ice_servers": turn_servers or stun_servers, "ice_transport_policy": "relay"}
    else:  # 'auto'
        return {"ice_servers": stun_servers + turn_servers, "ice_transport_policy": "all"}


class IceConfigView(APIView):
    """
    Return ICE server configuration for WebRTC calls.

    The list of servers and transport policy are tailored to the
    authenticated user's `connectivity_mode` preference:
      - auto   → STUN + TURN, policy=all  (try P2P, fall back to relay)
      - p2p    → STUN only,   policy=all  (P2P or bust)
      - server → TURN only,   policy=relay (always relay)

    GET /api/calls/ice-config/
    """

    def get(self, request):
        mode = getattr(request.user, 'connectivity_mode', 'auto')
        config = _build_ice_servers(mode)
        return Response(config)


def _try_create_livekit_token(room_name: str, identity: str) -> str | None:
    """Generate a LiveKit token if credentials are configured, else return None."""
    api_key = getattr(settings, "LIVEKIT_API_KEY", "")
    api_secret = getattr(settings, "LIVEKIT_API_SECRET", "")
    if not api_key or not api_secret:
        return None
    try:
        from livekit.api import AccessToken, VideoGrants

        token = AccessToken(api_key, api_secret)
        token = token.with_identity(identity).with_grants(
            VideoGrants(room_join=True, room=room_name)
        )
        return token.to_jwt()
    except Exception:
        return None


class InitiateCallView(APIView):
    """
    Start a voice or video call.

    Creates a CallLog, notifies the callee via WebSocket, and returns
    a LiveKit token (if configured) or signals for WebRTC peer-to-peer.
    """

    def post(self, request):
        callee_id = request.data.get("callee_id")
        call_type = request.data.get("call_type", "video")

        if not callee_id:
            return Response(
                {"error": "callee_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room_name = f"call_{uuid.uuid4().hex[:12]}"

        call = CallLog.objects.create(
            caller=request.user,
            callee_id=callee_id,
            call_type=call_type,
            room_name=room_name,
            status=CallLog.RINGING,
        )

        # Notify callee via WebSocket
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"notifications_{callee_id}",
            {
                "type": "notify",
                "payload": {
                    "event": "incoming_call",
                    "call_id": str(call.id),
                    "caller": request.user.username,
                    "caller_id": request.user.id,
                    "call_type": call_type,
                    "room_name": room_name,
                },
            },
        )

        # Send push only if callee has NO active WebSocket connection (app fully closed).
        # If WS is connected (foreground or background), the WS handler shows a local
        # notification on the client side — sending FCM too would cause duplicates.
        if not is_user_ws_connected(callee_id):
            send_call_push(
                callee_id=callee_id,
                caller_name=request.user.username,
                call_type=call_type,
                call_id=str(call.id),
                caller_id=request.user.id,
                room_name=room_name,
            )

        # Optional LiveKit token
        lk_token = _try_create_livekit_token(room_name, request.user.username)

        return Response(
            {
                "call_id": str(call.id),
                "room_name": room_name,
                "call_type": call_type,
                "token": lk_token,
                "livekit_url": getattr(settings, "LIVEKIT_URL", ""),
            },
            status=status.HTTP_201_CREATED,
        )


class JoinCallView(APIView):
    """Callee accepts the call."""

    def post(self, request, call_id):
        try:
            call = CallLog.objects.get(id=call_id, callee=request.user)
        except CallLog.DoesNotExist:
            return Response(
                {"error": "Call not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        call.status = CallLog.ONGOING
        call.save(update_fields=["status"])

        # Notify caller that the call was accepted
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"notifications_{call.caller_id}",
            {
                "type": "notify",
                "payload": {
                    "event": "call_accepted",
                    "call_id": str(call.id),
                    "callee": request.user.username,
                    "callee_id": request.user.id,
                    "room_name": call.room_name,
                },
            },
        )

        lk_token = _try_create_livekit_token(call.room_name, request.user.username)

        return Response(
            {
                "call_id": str(call.id),
                "room_name": call.room_name,
                "call_type": call.call_type,
                "token": lk_token,
                "livekit_url": getattr(settings, "LIVEKIT_URL", ""),
            }
        )


class EndCallView(APIView):
    """End / reject an active call."""

    def post(self, request, call_id):
        try:
            call = CallLog.objects.get(
                id=call_id,
            )
        except CallLog.DoesNotExist:
            return Response(
                {"error": "Call not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Only caller or callee can end the call
        if request.user.id not in (call.caller_id, call.callee_id):
            return Response(status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "end")  # "end" | "reject"
        call.status = CallLog.REJECTED if action == "reject" else CallLog.ENDED
        call.ended_at = timezone.now()
        if call.started_at and call.status == CallLog.ENDED:
            call.duration_seconds = int(
                (call.ended_at - call.started_at).total_seconds()
            )
        call.save()

        # Notify the other party
        other_user_id = (
            call.callee_id if request.user.id == call.caller_id else call.caller_id
        )
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"notifications_{other_user_id}",
            {
                "type": "notify",
                "payload": {
                    "event": "call_ended",
                    "call_id": str(call.id),
                    "action": action,
                },
            },
        )

        return Response({"status": call.status})


class CallStatusView(APIView):
    """Return the current status of a call (for polling fallback)."""

    def get(self, request, call_id):
        from django.db.models import Q

        try:
            call = CallLog.objects.get(
                Q(caller=request.user) | Q(callee=request.user),
                id=call_id,
            )
        except CallLog.DoesNotExist:
            return Response(
                {"error": "Call not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"status": call.status, "call_id": str(call.id)})


class CallHistoryView(generics.ListAPIView):
    """List call history for the authenticated user."""

    serializer_class = CallLogSerializer

    def get_queryset(self):
        from django.db.models import Q

        return CallLog.objects.filter(
            Q(caller=self.request.user) | Q(callee=self.request.user)
        ).select_related("caller", "callee")
