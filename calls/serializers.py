from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import CallLog

User = get_user_model()


class CallLogSerializer(serializers.ModelSerializer):
    caller_username = serializers.CharField(source="caller.username", read_only=True)
    callee_username = serializers.CharField(source="callee.username", read_only=True)

    class Meta:
        model = CallLog
        fields = (
            "id",
            "caller",
            "caller_username",
            "callee",
            "callee_username",
            "call_type",
            "status",
            "room_name",
            "started_at",
            "ended_at",
            "duration_seconds",
        )
        read_only_fields = (
            "id",
            "caller",
            "caller_username",
            "callee_username",
            "room_name",
            "started_at",
            "ended_at",
            "duration_seconds",
        )
