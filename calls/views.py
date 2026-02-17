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


class CallHistoryView(generics.ListAPIView):
    """List call history for the authenticated user."""

    serializer_class = CallLogSerializer

    def get_queryset(self):
        from django.db.models import Q

        return CallLog.objects.filter(
            Q(caller=self.request.user) | Q(callee=self.request.user)
        ).select_related("caller", "callee")
