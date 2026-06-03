from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import BlockedUser, Contact

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    display_name = serializers.CharField(
        required=False, allow_blank=True, max_length=50,
    )

    class Meta:
        model = User
        fields = ("id", "username", "email", "password", "display_name", "user_tag")
        read_only_fields = ("id", "user_tag")

    def create(self, validated_data: dict) -> User:
        display_name = (validated_data.get("display_name") or "").strip()
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
        )
        # Default display_name to username when the caller didn't supply one.
        user.display_name = display_name or user.username
        user.save(update_fields=["display_name"])
        return user


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id", "username", "email", "avatar", "bio",
            "display_name", "user_tag",
            "discoverable_by_username", "discoverable_by_email",
            "is_online", "last_seen", "connectivity_mode",
            "notif_messages_enabled", "notif_calls_enabled", "notif_sound_enabled",
        )
        read_only_fields = ("id", "is_online", "last_seen", "user_tag")

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

    def validate_display_name(self, value: str) -> str:
        # Display name is free-form but trimmed and length-bounded by the
        # column. Empty string is allowed; when empty we fall back to the
        # username at read time on the client.
        return (value or "").strip()


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


# ---------------------------------------------------------------------------
# Email-verification registration flow
# ---------------------------------------------------------------------------


class RegistrationRequestSerializer(serializers.Serializer):
    """Step 1: validate the candidate account and queue an email code.

    We deliberately reuse the same uniqueness checks as the legacy
    ``UserRegistrationSerializer`` so the caller gets the same error
    shape whether the email is taken or invalid.
    """

    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    display_name = serializers.CharField(
        required=False, allow_blank=True, max_length=50,
    )

    def validate_username(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Username cannot be empty.")
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("That username is already taken.")
        return value

    def validate_email(self, value: str) -> str:
        value = (value or "").strip().lower()
        if not value:
            raise serializers.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("That email is already in use.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value


class RegistrationVerifySerializer(serializers.Serializer):
    """Step 2: confirm the 6-digit code and create the account."""

    email = serializers.EmailField()
    code = serializers.RegexField(regex=r"^\d{6}$")

    def validate_email(self, value: str) -> str:
        return (value or "").strip().lower()


class RegistrationResendSerializer(serializers.Serializer):
    """Step 1.5: regenerate + re-send the verification code."""

    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return (value or "").strip().lower()
