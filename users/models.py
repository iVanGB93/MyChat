from django.contrib.auth.models import AbstractUser
from django.db import models

from .db_storage import db_storage


def _generate_user_tag() -> str:
    """Generate a short, unambiguous user tag like 'AXN-7K3P'.

    The character set excludes 0/O/1/I to avoid visual ambiguity when
    users read the tag aloud or type it from a screen. Uniqueness is
    enforced at the column level; the caller retries on IntegrityError.
    """
    import secrets

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "AXN-" + "".join(secrets.choice(alphabet) for _ in range(4))


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

    avatar = models.ImageField(
        upload_to="avatars/", blank=True, null=True, storage=db_storage,
    )
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

    # ---- Identity / discovery ----
    # `username` remains the unique login id. `display_name` is the free-form
    # human-friendly label shown in the UI (not unique). `user_tag` is a short
    # auto-generated handle that lets users share an unambiguous reference
    # (e.g. "AXN-7K3P") without exposing username/email.
    display_name = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Free-form display name shown in the UI. Defaults to username.",
    )
    user_tag = models.CharField(
        max_length=16, unique=True, db_index=True, blank=True, null=True,
        help_text="Short shareable handle, e.g. 'AXN-7K3P'.",
    )

    # ---- Discoverability preferences ----
    discoverable_by_username = models.BooleanField(
        default=True,
        help_text="Allow other users to find this account by username search.",
    )
    discoverable_by_email = models.BooleanField(
        default=False,
        help_text="Allow other users to find this account by exact email match.",
    )

    # ---- Notification preferences (per-user) ----
    notif_messages_enabled = models.BooleanField(
        default=True, help_text="Receive push notifications for new messages",
    )
    notif_calls_enabled = models.BooleanField(
        default=True, help_text="Receive push notifications for incoming calls",
    )
    notif_sound_enabled = models.BooleanField(
        default=True, help_text="Play in-app sound for new messages / calls",
    )

    # ---- Token versioning (used to invalidate all JWTs on demand) ----
    # Bumping this value invalidates every access/refresh token previously
    # issued for this user — used by "Logout all devices".
    token_version = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["username"]

    def __str__(self) -> str:
        return self.username

    def save(self, *args, **kwargs):
        """Auto-assign `user_tag` on first save with bounded retries.

        Retries are needed because the column is UNIQUE and `secrets`
        randomness can collide once the user base grows. With the 32-char
        alphabet × 4 positions there are ~1M tags, so a small handful of
        retries is more than enough until much later.
        """
        from django.db import IntegrityError, transaction

        if self.user_tag:
            return super().save(*args, **kwargs)

        last_err = None
        for _ in range(6):
            self.user_tag = _generate_user_tag()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                last_err = exc
                self.user_tag = None
                continue
        # Surface the original error if we somehow exhausted retries.
        raise last_err  # pragma: no cover


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


class PendingRegistration(models.Model):
    """A registration awaiting email-verification.

    We store the *hashed* password (via Django's password hasher) and the
    requested profile fields here while the user is in the verify-by-code
    step. The actual ``User`` row is only created after the code is
    matched, so unverified emails never pollute the auth table.
    """

    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150)
    password_hash = models.CharField(max_length=255)
    display_name = models.CharField(max_length=50, blank=True, default="")

    # 6-digit code, stored hashed so the DB dump never reveals live codes.
    code_hash = models.CharField(max_length=255)
    expires_at = models.DateTimeField()

    attempts = models.PositiveIntegerField(default=0)
    resends = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(auto_now_add=True)

    MAX_ATTEMPTS = 5
    MAX_RESENDS = 5
    RESEND_COOLDOWN_SECONDS = 30
    TTL_SECONDS = 10 * 60

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"PendingRegistration<{self.email}>"

    def is_expired(self) -> bool:
        from django.utils import timezone
        return timezone.now() >= self.expires_at
