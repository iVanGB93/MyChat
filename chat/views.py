from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import ChatRoom, Message
from .serializers import ChatRoomSerializer, MessageSerializer

User = get_user_model()


class ChatRoomViewSet(viewsets.ModelViewSet):
    """List / create / retrieve chat rooms for the authenticated user."""

    serializer_class = ChatRoomSerializer

    def get_queryset(self):
        return ChatRoom.objects.filter(
            members=self.request.user
        ).prefetch_related("members")

    def perform_create(self, serializer):
        room = serializer.save()
        room.members.add(self.request.user)

    @action(detail=False, methods=["post"], url_path="direct")
    def get_or_create_direct(self, request):
        """
        Get or create a direct chat room between the current user and another.
        Expects: {"user_id": <int>}
        """
        other_id = request.data.get("user_id")
        if not other_id:
            return Response(
                {"error": "user_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            other_user = User.objects.get(id=other_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Look for an existing direct room between the two users
        room = (
            ChatRoom.objects.filter(room_type=ChatRoom.DIRECT, members=request.user)
            .filter(members=other_user)
            .first()
        )

        if not room:
            room = ChatRoom.objects.create(
                name="",
                room_type=ChatRoom.DIRECT,
            )
            room.members.add(request.user, other_user)

        serializer = self.get_serializer(room)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk=None):
        """
        Add a member to an existing chat room.
        Expects: {"user_id": <int>}
        """
        room = self.get_object()
        user_id = request.data.get("user_id")
        if not user_id:
            return Response(
                {"error": "user_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        room.members.add(user)
        serializer = self.get_serializer(room)
        return Response(serializer.data)


class MessageListView(generics.ListAPIView):
    """Paginated message history for a given chat room."""

    serializer_class = MessageSerializer

    def get_queryset(self):
        room_id = self.kwargs["room_id"]
        return Message.objects.filter(
            room_id=room_id,
            room__members=self.request.user,
        ).select_related("sender")
