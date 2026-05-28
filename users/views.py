from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import generics, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Contact
from .serializers import ContactSerializer, UserRegistrationSerializer, UserSerializer

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """Public endpoint — register a new user."""

    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = (permissions.AllowAny,)


class ProfileView(generics.RetrieveUpdateAPIView):
    """Retrieve or update the authenticated user's profile."""

    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class RegisterPushTokenView(APIView):
    """
    Register (or update) the Expo push notification token for the
    authenticated user.  The client should call this after every login
    and whenever the token is refreshed.

    POST  { "token": "ExponentPushToken[...]" }
    """

    def post(self, request):
        token = request.data.get("token", "").strip()
        if not token:
            return Response(
                {"error": "token is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Clear token from any other user (device switched accounts)
        User.objects.filter(expo_push_token=token).exclude(id=request.user.id).update(
            expo_push_token=""
        )

        request.user.expo_push_token = token
        request.user.save(update_fields=["expo_push_token"])
        return Response({"status": "ok"})


class PendingNotificationsView(APIView):
    """
    Returns pending message deliveries and active incoming calls for the
    authenticated user.  Used by the mobile app's background fetch / reconnect
    to show local notifications when the WebSocket is disconnected.

    NOTE: Chat messages are NOT stored server-side (WS-only, local-first).
    This endpoint reports PendingDelivery records — signaling metadata that
    tells the client "sender X has messages waiting for you in room Y".
    The actual message content was already sent via Expo push notification
    at send-time; this is purely a reconnect-fallback reminder.

    GET /api/users/notifications/pending/
    """

    def get(self, request):
        from chat.models import PendingDelivery, ChatRoom
        from calls.models import CallLog
        import datetime

        user = request.user
        result = {"messages": [], "calls": []}

        # ---- Pending deliveries (one entry per room × sender pair) ----
        pending = (
            PendingDelivery.objects.filter(to_user=user)
            .select_related("from_user", "room")
            .order_by("created_at")
        )

        for pd in pending:
            room = pd.room
            # Display name: for direct rooms with no custom name use sender's username
            if room.room_type == ChatRoom.DIRECT and not room.name:
                display_name = pd.from_user.username
            else:
                display_name = room.name or pd.from_user.username

            result["messages"].append({
                "room_id": str(pd.room_id),
                "room_name": display_name,
                "sender": pd.from_user.username,
                # Content not stored server-side; client already got the actual
                # content via Expo push at send-time.
                "content": "New messages waiting",
                "created_at": pd.created_at.isoformat(),
            })

        # ---- Active incoming calls (ringing in last 60 seconds) ----
        cutoff = timezone.now() - datetime.timedelta(seconds=60)
        ringing = CallLog.objects.filter(
            callee=user,
            status=CallLog.RINGING,
            started_at__gte=cutoff,
        ).select_related("caller")

        for call in ringing:
            result["calls"].append({
                "call_id": str(call.id),
                "caller": call.caller.username,
                "caller_id": call.caller_id,
                "call_type": call.call_type,
                "room_name": call.room_name,
            })

        return Response(result)


class UserSearchView(generics.ListAPIView):
    """Search users by username (for adding contacts)."""

    serializer_class = UserSerializer

    def get_queryset(self):
        query = self.request.query_params.get("q", "")
        if query:
            return User.objects.filter(username__icontains=query).exclude(
                id=self.request.user.id
            )[:20]
        return User.objects.none()


class ContactViewSet(viewsets.ModelViewSet):
    """CRUD for the authenticated user's contacts."""

    serializer_class = ContactSerializer

    def get_queryset(self):
        return Contact.objects.filter(owner=self.request.user).select_related("contact")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
