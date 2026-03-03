from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Contact

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
        fields = ("id", "username", "email", "avatar", "bio", "is_online", "last_seen", "connectivity_mode")
        read_only_fields = ("id", "is_online", "last_seen")


class ContactSerializer(serializers.ModelSerializer):
    contact_detail = UserSerializer(source="contact", read_only=True)

    class Meta:
        model = Contact
        fields = ("id", "contact", "contact_detail", "created_at")
        read_only_fields = ("id", "created_at")
