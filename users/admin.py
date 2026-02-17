from django.contrib import admin
from django.contrib.auth import get_user_model

from .models import Contact

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
