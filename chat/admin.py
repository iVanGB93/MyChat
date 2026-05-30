from django.contrib import admin

from .models import ChatRoom


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "room_type", "created_at")
    list_filter = ("room_type",)
    search_fields = ("name",)
