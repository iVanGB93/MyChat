from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import ChatRoom

User = get_user_model()


class MemberSerializer(serializers.ModelSerializer):
    """Lightweight user serializer for room member lists."""

    avatar = serializers.ImageField(read_only=True, use_url=True)

    class Meta:
        model = User
        fields = ("id", "username", "is_online", "avatar")


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
        # Server no longer stores message history — messages are relayed via
        # the chat WebSocket and persisted client-side only. The client merges
        # locally-cached previews with this null and shows whatever it has.
        return None
