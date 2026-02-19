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
    Returns unread messages and active incoming calls for the
    authenticated user.  Used by the mobile app's background fetch
    to show local notifications when the WebSocket is disconnected.

    GET /api/users/notifications/pending/
    """

    def get(self, request):
        from chat.models import Message, ChatRoom
        from calls.models import CallLog
        import datetime

        user = request.user
        result = {"messages": [], "calls": []}

        # ---- Unread messages (last 50, grouped by room) ----
        unread = (
            Message.objects.filter(
                room__members=user,
                is_read=False,
            )
            .exclude(sender=user)
            .select_related("sender", "room")
            .order_by("-created_at")[:50]
        )

        seen_rooms: set = set()
        for msg in unread:
            room_id = str(msg.room_id)
            if room_id in seen_rooms:
                continue
            seen_rooms.add(room_id)

            # Compute display name
            room = msg.room
            if room.room_type == ChatRoom.DIRECT and not room.name:
                display_name = msg.sender.username
            else:
                display_name = room.name or msg.sender.username

            result["messages"].append({
                "room_id": room_id,
                "room_name": display_name,
                "sender": msg.sender.username,
                "content": msg.content[:200],
                "created_at": msg.created_at.isoformat(),
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
