from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model with profile fields for the chat app."""

    # Connectivity mode for WebRTC calls
    CONNECTIVITY_AUTO = 'auto'
    CONNECTIVITY_P2P = 'p2p'
    CONNECTIVITY_SERVER = 'server'
    CONNECTIVITY_CHOICES = [
        (CONNECTIVITY_AUTO, 'Auto (P2P with relay fallback)'),
        (CONNECTIVITY_P2P, 'Always P2P'),
        (CONNECTIVITY_SERVER, 'Always Server (relay)'),
    ]

    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    bio = models.CharField(max_length=200, blank=True, default="")
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(auto_now=True)
    expo_push_token = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Expo push notification token for this device",
    )
    connectivity_mode = models.CharField(
        max_length=10,
        choices=CONNECTIVITY_CHOICES,
        default=CONNECTIVITY_AUTO,
        help_text="WebRTC call connection mode preference",
    )

    class Meta:
        ordering = ["username"]

    def __str__(self) -> str:
        return self.username


class Contact(models.Model):
    """A directional contact / friend relationship."""

    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="contacts"
    )
    contact = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="contacted_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("owner", "contact")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.owner} → {self.contact}"


class BlockedUser(models.Model):
    """A user `owner` has blocked user `blocked` — any messages from
    `blocked` to `owner` are silently dropped server-side and never
    fan-out to `owner`'s sockets or push tokens."""

    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="blocked_users"
    )
    blocked = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="blocked_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("owner", "blocked")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.owner} ⊘ {self.blocked}"
