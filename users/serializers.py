from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import BlockedUser, Contact

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("id", "username", "email", "password")

    def create(self, validated_data: dict) -> User:
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id", "username", "email", "avatar", "bio",
            "is_online", "last_seen", "connectivity_mode",
            "notif_messages_enabled", "notif_calls_enabled", "notif_sound_enabled",
        )
        read_only_fields = ("id", "is_online", "last_seen")

    # ---- Uniqueness checks (case-insensitive) for self-edits ----
    def validate_username(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Username cannot be empty.")
        qs = User.objects.filter(username__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That username is already taken.")
        return value

    def validate_email(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return value
        qs = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That email is already in use.")
        return value


class PasswordChangeSerializer(serializers.Serializer):
    """Validate a password-change request from an authenticated user."""

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_current_password(self, value: str) -> str:
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value: str) -> str:
        user = self.context["request"].user
        validate_password(value, user=user)
        return value


class AccountDeleteSerializer(serializers.Serializer):
    """Require password confirmation to delete the authenticated user."""

    password = serializers.CharField(write_only=True)

    def validate_password(self, value: str) -> str:
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Password is incorrect.")
        return value


class ContactSerializer(serializers.ModelSerializer):
    contact_detail = UserSerializer(source="contact", read_only=True)

    class Meta:
        model = Contact
        fields = ("id", "contact", "contact_detail", "created_at")
        read_only_fields = ("id", "created_at")


class BlockedUserSerializer(serializers.ModelSerializer):
    blocked_detail = UserSerializer(source="blocked", read_only=True)

    class Meta:
        model = BlockedUser
        fields = ("id", "blocked", "blocked_detail", "created_at")
        read_only_fields = ("id", "created_at")
