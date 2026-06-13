from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import ChatRoom

User = get_user_model()


class MemberSerializer(serializers.ModelSerializer):
    """Lightweight user serializer for room member lists."""

    avatar = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    user_tag = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "display_name", "user_tag", "is_online", "avatar")

    def get_avatar(self, obj: User) -> str | None:
        try:
            profile = obj.profile
        except Exception:
            return None
        avatar = getattr(profile, "avatar", None)
        return avatar.url if avatar and getattr(avatar, "name", "") else None

    def get_display_name(self, obj: User) -> str:
        profile = getattr(obj, "profile", None)
        return getattr(profile, "display_name", "") or obj.username

    def get_user_tag(self, obj: User) -> str | None:
        profile = getattr(obj, "profile", None)
        return getattr(profile, "user_tag", None)

    def get_is_online(self, obj: User) -> bool:
        presence = getattr(obj, "presence", None)
        return bool(getattr(presence, "is_online", False))


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