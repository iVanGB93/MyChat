"""
Simple template views for the web test interface.
These serve HTML pages that interact with the REST API via JavaScript.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

User = get_user_model()


def login_view(request):
    return render(request, "login.html")


def register_view(request):
    return render(request, "register.html")


def dashboard_view(request):
    return render(request, "app.html")


def chat_room_view(request, room_id):
    """Legacy URL — redirect to dashboard (chat loads inline now)."""
    return render(request, "app.html")


def calls_view(request):
    """Legacy URL — redirect to dashboard (calls are integrated now)."""
    return render(request, "app.html")


# ──────────────────────────────────────────────────────────
#  Real-time monitoring
# ──────────────────────────────────────────────────────────

def monitor_view(request):
    """Render the monitoring dashboard page."""
    return render(request, "monitor.html")


def monitor_api(request):
    """JSON API for the monitoring dashboard — polled every few seconds."""
    from calls.models import CallLog
    from chat.consumers import get_connected_chat_rooms, get_connected_notification_users
    from chat.models import ChatRoom, Message

    now = timezone.now()
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)

    # ── Users ──
    total_users = User.objects.count()
    online_users = User.objects.filter(is_online=True)
    online_list = list(
        online_users.values("id", "username", "last_seen").order_by("-last_seen")
    )
    for u in online_list:
        if u["last_seen"]:
            u["last_seen"] = u["last_seen"].isoformat()

    users_with_push = User.objects.exclude(expo_push_token__isnull=True).exclude(
        expo_push_token=""
    ).count()

    # ── WebSocket connections (in-memory) ──
    ws_notification_users = get_connected_notification_users()
    ws_chat_rooms = get_connected_chat_rooms()

    # Derive online (foreground) vs connected (background) from WS tracking
    ws_online = [u for u in ws_notification_users if u.get("app_state") == "active"]
    ws_background = [u for u in ws_notification_users if u.get("app_state") != "active"]

    # ── Messages ──
    total_messages = Message.objects.count()
    messages_last_hour = Message.objects.filter(created_at__gte=one_hour_ago).count()
    messages_last_24h = Message.objects.filter(created_at__gte=one_day_ago).count()
    unread_messages = Message.objects.filter(is_read=False).count()

    # ── Rooms ──
    total_rooms = ChatRoom.objects.count()
    direct_rooms = ChatRoom.objects.filter(room_type="DIRECT").count()
    group_rooms = ChatRoom.objects.filter(room_type="GROUP").count()

    # ── Calls ──
    active_calls = list(
        CallLog.objects.filter(status__in=["RINGING", "ONGOING"]).values(
            "id", "caller__username", "callee__username", "status", "started_at"
        )
    )
    for c in active_calls:
        if c["started_at"]:
            c["started_at"] = c["started_at"].isoformat()
        c["id"] = str(c["id"])

    total_calls_today = CallLog.objects.filter(started_at__gte=one_day_ago).count()
    missed_calls_today = CallLog.objects.filter(
        started_at__gte=one_day_ago, status="MISSED"
    ).count()

    # ── Recent activity (last 10 messages) ──
    recent_messages = list(
        Message.objects.select_related("sender", "room")
        .order_by("-created_at")[:10]
        .values(
            "id", "sender__username", "room__id", "content",
            "message_type", "is_read", "created_at",
        )
    )
    for m in recent_messages:
        m["id"] = str(m["id"])
        m["room__id"] = str(m["room__id"])
        m["created_at"] = m["created_at"].isoformat()
        # Truncate long content
        if m["content"] and len(m["content"]) > 80:
            m["content"] = m["content"][:80] + "…"

    return JsonResponse({
        "server_time": now.isoformat(),
        "users": {
            "total": total_users,
            "online_db": online_users.count(),
            "online_list": online_list,
            "with_push_token": users_with_push,
        },
        "websockets": {
            "notification_users": ws_notification_users,
            "notification_count": len(ws_notification_users),
            "online_count": len(ws_online),
            "online_users": ws_online,
            "background_count": len(ws_background),
            "background_users": ws_background,
            "chat_rooms": ws_chat_rooms,
            "chat_rooms_active": len(ws_chat_rooms),
        },
        "messages": {
            "total": total_messages,
            "last_hour": messages_last_hour,
            "last_24h": messages_last_24h,
            "unread": unread_messages,
            "recent": recent_messages,
        },
        "rooms": {
            "total": total_rooms,
            "direct": direct_rooms,
            "group": group_rooms,
        },
        "calls": {
            "active": active_calls,
            "active_count": len(active_calls),
            "today_total": total_calls_today,
            "today_missed": missed_calls_today,
        },
    })
