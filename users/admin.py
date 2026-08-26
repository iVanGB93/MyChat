from django.contrib import admin
from django.contrib.auth import get_user_model

from .models import Contact, UserDevice, UserPresence, UserPresenceSession, UserProfile

User = get_user_model()


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "is_online", "last_seen", "is_staff")
    search_fields = ("username", "email")
    list_filter = ("is_online", "is_staff")


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("owner", "contact", "created_at")
    search_fields = ("owner__username", "contact__username")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "display_name",
        "user_tag",
        "connectivity_mode",
        "notif_messages_enabled",
        "notif_calls_enabled",
    )
    search_fields = ("user__username", "display_name", "user_tag")
    list_filter = (
        "connectivity_mode",
        "notif_messages_enabled",
        "notif_calls_enabled",
        "notif_sound_enabled",
    )


@admin.register(UserPresence)
class UserPresenceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "is_online",
        "app_state",
        "notification_socket_count",
        "chat_socket_count",
        "active_room_id",
        "last_seen",
    )
    search_fields = ("user__username", "active_room_id")
    list_filter = (
        "is_online",
        "app_state",
        "notification_socket_connected",
        "chat_socket_connected",
    )


@admin.register(UserPresenceSession)
class UserPresenceSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "connection_id", "app_state", "last_seen", "connected_at")
    search_fields = ("user__username", "connection_id")
    list_filter = ("app_state",)


@admin.register(UserDevice)
class UserDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "installation_id",
        "platform",
        "device_name",
        "is_active",
        "last_seen",
    )
    search_fields = ("user__username", "installation_id", "expo_push_token", "device_name")
    list_filter = ("platform", "is_active")
