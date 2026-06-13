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


# NOTE: The legacy `Message` model has been removed. Messages are relayed
# in real-time over the chat WebSocket and stored client-side only. The
# server retains no message history.


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


class MessageDelivery(models.Model):
    """Per-message delivery state (metadata only, no message content)."""

    STATUS_PENDING = "pending"
    STATUS_DELIVERED = "delivered"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_DELIVERED, "Delivered"),
    ]
    ROUTE_UNKNOWN = "unknown"
    ROUTE_CHAT_WS = "chat_ws"
    ROUTE_NOTIF_WS = "notif_ws"
    ROUTE_PUSH = "push"
    ROUTE_PENDING_ONLY = "pending_only"
    ROUTE_CHOICES = [
        (ROUTE_UNKNOWN, "Unknown"),
        (ROUTE_CHAT_WS, "Chat WS"),
        (ROUTE_NOTIF_WS, "Notification WS"),
        (ROUTE_PUSH, "Push"),
        (ROUTE_PENDING_ONLY, "Pending Only"),
    ]

    room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="message_deliveries"
    )
    message_id = models.CharField(max_length=64)
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="message_delivery_outbox",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="message_delivery_inbox",
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    routed_at = models.DateTimeField(null=True, blank=True)
    routed_via = models.CharField(
        max_length=24,
        choices=ROUTE_CHOICES,
        default=ROUTE_UNKNOWN,
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    push_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message_id", "recipient"],
                name="uniq_message_delivery_per_recipient",
            )
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["sender", "recipient", "room"]),
        ]

    def __str__(self) -> str:
        return f"{self.message_id} {self.sender_id}->{self.recipient_id} ({self.status})"
