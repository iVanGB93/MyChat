import uuid

from django.conf import settings
from django.db import models


class ChatRoom(models.Model):
    """A conversation between two or more users."""

    DIRECT = "direct"
    GROUP = "group"
    ROOM_TYPES = [(DIRECT, "Direct"), (GROUP, "Group")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, blank=True, default="")
    room_type = models.CharField(max_length=10, choices=ROOM_TYPES, default=DIRECT)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="chat_rooms", blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.name or str(self.id)


class Message(models.Model):
    """A single message inside a ChatRoom."""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    MESSAGE_TYPES = [(TEXT, "Text"), (IMAGE, "Image"), (FILE, "File")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages"
    )
    content = models.TextField(blank=True, default="")
    message_type = models.CharField(max_length=10, choices=MESSAGE_TYPES, default=TEXT)
    file = models.FileField(upload_to="chat_files/", blank=True, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.sender} in {self.room} @ {self.created_at:%H:%M}"


class PendingDelivery(models.Model):
    """
    Signals that from_user has at least one message queued for to_user in room.
    No message content is stored — this is signaling metadata only.
    Deleted when to_user acknowledges receipt of all messages from from_user in room.
    """

    room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="pending_deliveries"
    )
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_outbox",
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_inbox",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("room", "from_user", "to_user")

    def __str__(self) -> str:
        return f"{self.from_user} → {self.to_user} in {self.room}"
