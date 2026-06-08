import uuid

from django.conf import settings
from django.db import models


class CallLog(models.Model):
    """Tracks voice and video call history."""

    VOICE = "voice"
    VIDEO = "video"
    CALL_TYPES = [(VOICE, "Voice"), (VIDEO, "Video")]

    INITIATED = "initiated"
    RINGING = "ringing"
    ONGOING = "ongoing"
    ENDED = "ended"
    MISSED = "missed"
    REJECTED = "rejected"
    STATUSES = [
        (INITIATED, "Initiated"),
        (RINGING, "Ringing"),
        (ONGOING, "Ongoing"),
        (ENDED, "Ended"),
        (MISSED, "Missed"),
        (REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="outgoing_calls"
    )
    callee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="incoming_calls"
    )
    call_type = models.CharField(max_length=10, choices=CALL_TYPES)
    status = models.CharField(max_length=12, choices=STATUSES, default=INITIATED)
    room_name = models.CharField(max_length=255, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    ws_notified_at = models.DateTimeField(blank=True, null=True)
    push_sent_at = models.DateTimeField(blank=True, null=True)
    invite_acked_at = models.DateTimeField(blank=True, null=True)
    ended_at = models.DateTimeField(blank=True, null=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.caller} → {self.callee} ({self.call_type}) [{self.status}]"
