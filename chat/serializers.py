from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import ChatRoom, Message

User = get_user_model()


class MemberSerializer(serializers.ModelSerializer):
    """Lightweight user serializer for room member lists."""

    class Meta:
        model = User
        fields = ("id", "username", "is_online")


class ChatRoomSerializer(serializers.ModelSerializer):
    members = serializers.PrimaryKeyRelatedField(
        many=True, queryset=User.objects.all(), required=False
    )
    members_detail = MemberSerializer(source="members", many=True, read_only=True)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = (
            "id",
            "name",
            "room_type",
            "members",
            "members_detail",
            "last_message",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def get_last_message(self, obj: ChatRoom) -> dict | None:
        msg = obj.messages.order_by("-created_at").first()
        if msg:
            return {
                "id": str(msg.id),
                "sender": msg.sender.username,
                "content": msg.content,
                "created_at": msg.created_at.isoformat(),
            }
        return None


class MessageSerializer(serializers.ModelSerializer):
    sender_username = serializers.CharField(source="sender.username", read_only=True)

    class Meta:
        model = Message
        fields = (
            "id",
            "room",
            "sender",
            "sender_username",
            "content",
            "message_type",
            "file",
            "is_read",
            "created_at",
        )
        read_only_fields = ("id", "sender", "sender_username", "created_at")
