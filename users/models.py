from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model with profile fields for the chat app."""

    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    bio = models.CharField(max_length=200, blank=True, default="")
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(auto_now=True)
    expo_push_token = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Expo push notification token for this device",
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
