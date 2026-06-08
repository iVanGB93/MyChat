"""
Simple template views for the web test interface.
These serve HTML pages that interact with the REST API via JavaScript.
"""

from datetime import timedelta
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
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


def invite_tag_view(request, user_tag: str):
    """Landing page for shared invite links like /add/AXN-1234.

    The page provides a deep-link button (axonic://...) and keeps the tag visible
    so users can copy it manually if the app is not installed.
    """
    normalized_tag = (user_tag or "").strip().upper()
    deep_link = f"axonic://add/{quote(normalized_tag)}"
    return render(
        request,
        "invite.html",
        {
            "user_tag": normalized_tag,
            "deep_link": deep_link,
        },
    )


# ──────────────────────────────────────────────────────────
#  Real-time monitoring
# ──────────────────────────────────────────────────────────

@staff_member_required
def monitor_view(request):
    """Render the monitoring dashboard page."""
    return render(request, "monitor.html")


@staff_member_required
def monitor_api(request):
    """JSON API for the monitoring dashboard — polled every few seconds."""
    from calls.models import CallLog
    from chat.consumers import get_connected_chat_rooms, get_connected_notification_users
    from chat.models import ChatRoom, MessageDelivery

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
    # Messages are no longer persisted server-side (WS-only relay), so
    # historical counts are not available. Return zeros for dashboard compat.
    total_messages = 0
    messages_last_hour = 0
    messages_last_24h = 0
    unread_messages = 0

    # ── Rooms ──
    total_rooms = ChatRoom.objects.count()
    direct_rooms = ChatRoom.objects.filter(room_type=ChatRoom.DIRECT).count()
    group_rooms = ChatRoom.objects.filter(room_type=ChatRoom.GROUP).count()

    # ── Calls ──
    active_calls = list(
        CallLog.objects.filter(status__in=[CallLog.RINGING, CallLog.ONGOING]).values(
            "id", "caller__username", "callee__username", "status", "started_at"
        )
    )
    for c in active_calls:
        if c["started_at"]:
            c["started_at"] = c["started_at"].isoformat()
        c["id"] = str(c["id"])

    total_calls_today = CallLog.objects.filter(started_at__gte=one_day_ago).count()
    missed_calls_today = CallLog.objects.filter(
        started_at__gte=one_day_ago, status=CallLog.MISSED
    ).count()

    # ── Reliability KPIs (message + call trust layers) ──
    msg_ack_timeout_s = int(getattr(settings, "MESSAGE_ACK_TIMEOUT_SECONDS", 8))
    msg_overdue_cutoff = now - timedelta(seconds=msg_ack_timeout_s)

    msg_created_24h = MessageDelivery.objects.filter(created_at__gte=one_day_ago).count()
    msg_delivered_24h = MessageDelivery.objects.filter(
        delivered_at__gte=one_day_ago,
        status=MessageDelivery.STATUS_DELIVERED,
    ).count()
    msg_pending_total = MessageDelivery.objects.filter(
        status=MessageDelivery.STATUS_PENDING,
    ).count()
    msg_pending_overdue = MessageDelivery.objects.filter(
        status=MessageDelivery.STATUS_PENDING,
        created_at__lt=msg_overdue_cutoff,
    ).count()
    msg_push_fallback_24h = MessageDelivery.objects.filter(
        push_sent_at__gte=one_day_ago,
    ).count()

    delivered_rows = MessageDelivery.objects.filter(
        delivered_at__gte=one_day_ago,
        status=MessageDelivery.STATUS_DELIVERED,
    ).values("created_at", "delivered_at")
    ack_samples = [
        (row["delivered_at"] - row["created_at"]).total_seconds() * 1000
        for row in delivered_rows
        if row.get("created_at") and row.get("delivered_at")
    ]
    msg_ack_avg_ms_24h = round(sum(ack_samples) / len(ack_samples), 2) if ack_samples else None

    call_ack_timeout_s = int(getattr(settings, "CALL_INVITE_ACK_TIMEOUT_SECONDS", 12))
    call_overdue_cutoff = now - timedelta(seconds=call_ack_timeout_s)
    calls_started_24h = CallLog.objects.filter(started_at__gte=one_day_ago).count()
    calls_ws_notified_24h = CallLog.objects.filter(
        started_at__gte=one_day_ago,
        ws_notified_at__isnull=False,
    ).count()
    calls_push_sent_24h = CallLog.objects.filter(
        started_at__gte=one_day_ago,
        push_sent_at__isnull=False,
    ).count()
    calls_invite_acked_24h = CallLog.objects.filter(
        started_at__gte=one_day_ago,
        invite_acked_at__isnull=False,
    ).count()
    calls_ack_pending_overdue = CallLog.objects.filter(
        status=CallLog.RINGING,
        started_at__lt=call_overdue_cutoff,
        invite_acked_at__isnull=True,
    ).count()

    # ── Recent activity (last 10 messages) ──
    # Messages are not persisted server-side; return empty list.
    recent_messages: list = []

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
        "reliability": {
            "message_ack_timeout_seconds": msg_ack_timeout_s,
            "thresholds": {
                "msg_ack_rate_healthy": float(getattr(settings, "MONITOR_MSG_ACK_RATE_HEALTHY", 0.99)),
                "msg_ack_rate_degraded": float(getattr(settings, "MONITOR_MSG_ACK_RATE_DEGRADED", 0.95)),
                "call_ack_rate_healthy": float(getattr(settings, "MONITOR_CALL_ACK_RATE_HEALTHY", 0.98)),
                "call_ack_rate_degraded": float(getattr(settings, "MONITOR_CALL_ACK_RATE_DEGRADED", 0.90)),
                "msg_pending_overdue_warn": int(getattr(settings, "MONITOR_MSG_PENDING_OVERDUE_WARN", 1)),
                "msg_pending_overdue_crit": int(getattr(settings, "MONITOR_MSG_PENDING_OVERDUE_CRIT", 10)),
                "call_pending_overdue_warn": int(getattr(settings, "MONITOR_CALL_PENDING_OVERDUE_WARN", 1)),
                "call_pending_overdue_crit": int(getattr(settings, "MONITOR_CALL_PENDING_OVERDUE_CRIT", 5)),
                "msg_push_fallback_warn": int(getattr(settings, "MONITOR_MSG_PUSH_FALLBACK_WARN", 1)),
                "msg_push_fallback_crit": int(getattr(settings, "MONITOR_MSG_PUSH_FALLBACK_CRIT", 20)),
                "msg_ack_avg_ms_healthy": int(getattr(settings, "MONITOR_MSG_ACK_AVG_MS_HEALTHY", 1200)),
                "msg_ack_avg_ms_degraded": int(getattr(settings, "MONITOR_MSG_ACK_AVG_MS_DEGRADED", 3500)),
            },
            "messages": {
                "created_24h": msg_created_24h,
                "delivered_24h": msg_delivered_24h,
                "ack_rate_24h": round((msg_delivered_24h / msg_created_24h), 4) if msg_created_24h else None,
                "pending_total": msg_pending_total,
                "pending_overdue": msg_pending_overdue,
                "push_fallback_24h": msg_push_fallback_24h,
                "ack_latency_avg_ms_24h": msg_ack_avg_ms_24h,
            },
            "calls": {
                "ack_timeout_seconds": call_ack_timeout_s,
                "started_24h": calls_started_24h,
                "ws_notified_24h": calls_ws_notified_24h,
                "push_sent_24h": calls_push_sent_24h,
                "invite_acked_24h": calls_invite_acked_24h,
                "invite_ack_rate_24h": round((calls_invite_acked_24h / calls_started_24h), 4) if calls_started_24h else None,
                "ack_pending_overdue": calls_ack_pending_overdue,
            },
        },
    })
