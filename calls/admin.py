from django.contrib import admin

from .models import CallLog


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "caller",
        "callee",
        "call_type",
        "status",
        "duration_seconds",
        "started_at",
    )
    list_filter = ("call_type", "status")
    search_fields = ("caller__username", "callee__username")
