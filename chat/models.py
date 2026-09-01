import uuid

from django.conf import settings
from django.db import models

from users.db_storage import db_storage


class ChatRoom(models.Model):
    """A conversation between two or more users."""

    DIRECT = "direct"
    GROUP = "group"
    ROOM_TYPES = [(DIRECT, "Direct"), (GROUP, "Group")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, blank=True, default="")
    room_type = models.CharField(max_length=10, choices=ROOM_TYPES, default=DIRECT)
    # Kept in the same database-backed storage as profile photos so group
    # images survive ephemeral application deployments.
    avatar = models.ImageField(
        upload_to="group-avatars/", blank=True, null=True, storage=db_storage
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="chat_rooms", blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.name or str(self.id)


class GroupMembership(models.Model):
    """Role and audit metadata for a member of a group room.

    ``ChatRoom.members`` remains the authoritative, lightweight membership
    relation used by the realtime and media paths.  This companion row adds
    the group-specific state without changing that hot relation or breaking
    existing direct rooms.
    """

    ADMIN = "admin"
    MEMBER = "member"
    ROLES = [(ADMIN, "Admin"), (MEMBER, "Member")]

    room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="group_memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    role = models.CharField(max_length=12, choices=ROLES, default=MEMBER)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="group_members_added",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["room", "user"], name="uniq_group_membership_user_room"
            )
        ]
        indexes = [models.Index(fields=["room", "role"])]

    def __str__(self) -> str:
        return f"{self.room_id}:{self.user_id} ({self.role})"


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
    last_push_attempt_at = models.DateTimeField(null=True, blank=True)
    push_attempt_count = models.PositiveSmallIntegerField(default=0)

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


class OfflineEmailNudge(models.Model):
    """Database-backed email cooldown for an offline conversation.

    This contains delivery metadata only; no chat content is ever retained on
    the server. A unique sender/recipient/room row prevents duplicate emails
    across ASGI workers and deployments.
    """

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="offline_email_nudges")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="offline_email_nudges_sent")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="offline_email_nudges_received")
    last_sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["room", "sender", "recipient"],
                name="uniq_offline_email_nudge_conversation",
            )
        ]
        indexes = [models.Index(fields=["recipient", "last_sent_at"])]

    def __str__(self) -> str:
        return f"OfflineEmailNudge<{self.sender_id}->{self.recipient_id} {self.room_id}>"


class MediaBlob(models.Model):
    """
    Out-of-band storage for large media (image / voice / video) so blobs never
    ride the chat WebSocket. The chat message carries only a lightweight pointer
    (media_id + sha256 + thumb); the bytes are uploaded/downloaded over HTTP.

    New production blobs live in object storage; legacy rows can retain their
    bytes in Postgres so old messages remain downloadable during migration.

    Retention: the row (and its bytes) is deleted 48h after every recipient has
    confirmed a verified, persisted download (`delete_after`), or after a hard
    TTL as a fallback for recipients that never download. Clients keep the
    thumbnail + metadata in their own local message row, so history survives.
    """

    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    DOCUMENT = "document"
    MEDIA_TYPES = [(IMAGE, "Image"), (VOICE, "Voice"), (VIDEO, "Video"), (DOCUMENT, "Document")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(
        ChatRoom, on_delete=models.CASCADE, related_name="media_blobs"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_media",
    )
    # The client-generated id of the pointer chat message (optional link).
    message_id = models.CharField(max_length=64, blank=True, default="")
    media_type = models.CharField(max_length=16, choices=MEDIA_TYPES)
    mime = models.CharField(max_length=80)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    # md5 is used by the RN client for a cheap native integrity check on download
    # (expo-file-system exposes File.md5 for free). Not for security — integrity only.
    md5 = models.CharField(max_length=32, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # Legacy database bytes. New object-storage rows leave this null.
    data = models.BinaryField(null=True, blank=True)
    storage_backend = models.CharField(max_length=16, default="database")
    object_key = models.CharField(max_length=512, blank=True, default="")
    # Durable S3 multipart identity lets a later client retry continue with the
    # already-uploaded parts instead of restarting a large transfer.
    multipart_upload_id = models.CharField(max_length=512, blank=True, default="")
    multipart_part_size = models.PositiveIntegerField(null=True, blank=True)
    # Null while a direct-to-object-storage upload is still in progress.
    upload_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set once every recipient has confirmed a verified download.
    all_confirmed_at = models.DateTimeField(null=True, blank=True)
    # created once all_confirmed_at is set; the cleanup sweep deletes at/after this.
    delete_after = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["delete_after"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["room", "owner"]),
        ]

    def __str__(self) -> str:
        return f"MediaBlob<{self.id} {self.media_type} {self.size_bytes}B>"


class MediaDownload(models.Model):
    """
    Reference-count row: one per (media, recipient device) that has confirmed a
    verified, persisted download. When every recipient (room member except the
    owner) has at least one confirmed device, the blob becomes eligible for
    deletion after the 48h grace window.
    """

    media = models.ForeignKey(
        MediaBlob, on_delete=models.CASCADE, related_name="downloads"
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="media_downloads",
    )
    installation_id = models.CharField(max_length=128, blank=True, default="")
    verified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["media", "recipient", "installation_id"],
                name="uniq_media_download_per_device",
            )
        ]
        indexes = [
            models.Index(fields=["media", "recipient"]),
        ]

    def __str__(self) -> str:
        return f"MediaDownload<{self.media_id} r={self.recipient_id}>"
