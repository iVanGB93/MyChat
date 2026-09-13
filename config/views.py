"""
Simple template views for the web test interface.
These serve HTML pages that interact with the REST API via JavaScript.
"""

from datetime import timedelta
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from django.db.models import Avg, DurationField, ExpressionWrapper, F, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from users.models import UserDevice, UserPresence
from users.presence import effective_presence_is_online

User = get_user_model()


def landing_view(request):
    """Public product page and the stable destination for download links."""
    return render(
        request,
        "landing.html",
        {
            "android_store_url": settings.APP_STORE_URL_ANDROID,
            "ios_store_url": settings.APP_STORE_URL_IOS,
        },
    )


@never_cache
def app_version_view(request):
    """Compatibility policy for old clients; Google Play determines availability.

    Response:
      latest          — empty; no manually maintained release number
      min_supported   — clients below this are forced to update
      store_url        — platform-specific store link
      store_url_android / store_url_ios
    """
    platform = (request.GET.get("platform") or "").lower()
    android = settings.APP_STORE_URL_ANDROID
    ios = settings.APP_STORE_URL_IOS
    store_url = ios if platform == "ios" else android
    return JsonResponse({
        "latest": settings.APP_LATEST_VERSION,
        "min_supported": settings.APP_MIN_SUPPORTED_VERSION,
        "store_url": store_url,
        "store_url_android": android,
        "store_url_ios": ios,
    })


def privacy_view(request):
    """Public, login-free privacy and account-deletion information."""
    return render(request, "privacy.html")


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
@never_cache
def monitor_api(request):
    """JSON API for the monitoring dashboard — polled every few seconds."""
    from calls.models import CallLog
    from chat.models import ChatRoom, MessageDelivery
    from users.models import UserPresence

    now = timezone.now()
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(days=1)

    # Shared, expiring Axion leases work across Railway processes/replicas.
    from users.models import UserPresenceSession
    from users.presence import stale_seconds
    total_users = User.objects.count()
    leases = UserPresenceSession.objects.filter(
        last_seen__gte=now - timedelta(seconds=stale_seconds()),
    ).select_related("user")
    by_user = {}
    for lease in leases:
        row = by_user.setdefault(lease.user_id, {
            "user_id": lease.user_id, "username": lease.user.username,
            "connected_at": lease.connected_at.isoformat(), "connections": 0,
            "app_state": lease.app_state, "last_seen": lease.last_seen.isoformat(),
        })
        row["connections"] += 1
        row["connected_at"] = min(row["connected_at"], lease.connected_at.isoformat())
        row["last_seen"] = max(row["last_seen"], lease.last_seen.isoformat())
        if lease.app_state == UserPresence.APP_STATE_ACTIVE:
            row["app_state"] = UserPresence.APP_STATE_ACTIVE
    ws_notification_users = list(by_user.values())
    ws_online = [u for u in ws_notification_users if u["app_state"] == "active"]
    ws_background = [u for u in ws_notification_users if u["app_state"] != "active"]
    online_users = ws_online
    online_list = [{"id": u["user_id"], "username": u["username"], "last_seen": u["last_seen"]} for u in ws_online]
    push_user_ids = set(UserDevice.objects.filter(is_active=True).filter(
        Q(expo_push_token__startswith="ExponentPushToken[")
        | Q(expo_push_token__startswith="ExpoPushToken[")
        | (~Q(fcm_token="") & Q(fcm_token__isnull=False))
    ).values_list("user_id", flat=True))
    users_with_push = len(push_user_ids)
    offline_with_push = len(push_user_ids - set(by_user))
    # Room sockets no longer represent current chat activity.
    ws_chat_rooms = {}
    total_messages = messages_last_hour = messages_last_24h = unread_messages = None

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
        created_at__gte=one_day_ago,
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

    ack_average = MessageDelivery.objects.filter(
        created_at__gte=one_day_ago,
        delivered_at__gte=F("created_at"),
        status=MessageDelivery.STATUS_DELIVERED,
    ).aggregate(value=Avg(ExpressionWrapper(F("delivered_at") - F("created_at"), output_field=DurationField())))["value"]
    msg_ack_avg_ms_24h = round(ack_average.total_seconds() * 1000, 2) if ack_average is not None else None

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
            "offline_with_push": offline_with_push,
            "total": total_users,
            "online_db": len(online_users),
            "online_list": online_list,
            "with_push_token": users_with_push,
        },
        "websockets": {
            "scope": "shared_axion_leases",
            "connection_count": sum(u["connections"] for u in ws_notification_users),
            "notification_users": ws_notification_users,
            "notification_count": len(ws_notification_users),
            "online_count": len(ws_online),
            "online_users": ws_online,
            "background_count": len(ws_background),
            "background_users": ws_background,
            "chat_rooms": ws_chat_rooms,
            "chat_rooms_active": None,
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
            "scope": "Retained per-recipient delivery records; not lifetime message totals. Offline delivery time is included in latency.",
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
                "sender_confirmation_pending": MessageDelivery.objects.filter(status=MessageDelivery.STATUS_DELIVERED, sender_confirmed_at__isnull=True).count(),
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


@staff_member_required
def monitor_routing_view(request, user_id: int):
    """Inspect the structured notification-routing state for one user."""
    from chat.consumers import get_recent_notification_decisions, get_user_notification_channels, get_user_routing_state
    from chat.push import get_fcm_status, _is_expo_push_token

    try:
        user = User.objects.select_related("profile", "presence").get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": "user_not_found", "user_id": user_id}, status=404)

    routing_state = get_user_routing_state(user_id)
    channels = sorted(get_user_notification_channels(user_id))
    devices = list(
        user.devices.order_by("-last_seen").values(
            "installation_id",
            "platform",
            "device_name",
            "app_version",
            "is_active",
            "last_seen",
            "updated_at",
            "expo_push_token",
            "fcm_token",
        )
    )
    for device in devices:
        if device.get("last_seen"):
            device["last_seen"] = device["last_seen"].isoformat()
        if device.get("updated_at"):
            device["updated_at"] = device["updated_at"].isoformat()
        # Never leak the full tokens; expose only what's needed to diagnose
        # which push channel a device can use.
        expo = device.pop("expo_push_token", None) or ""
        fcm = device.pop("fcm_token", None) or ""
        device["has_expo_token"] = _is_expo_push_token(expo)
        device["has_fcm_token"] = bool(fcm)
        device["fcm_token_prefix"] = (fcm[:12] + "…") if fcm else ""

    presence = getattr(user, "presence", None)
    presence_payload = None
    if presence is not None:
        presence_payload = {
            "is_online": effective_presence_is_online(presence),
            "app_state": presence.app_state,
            "chat_socket_connected": presence.chat_socket_connected,
            "chat_socket_count": presence.chat_socket_count,
            "notification_socket_connected": presence.notification_socket_connected,
            "notification_socket_count": presence.notification_socket_count,
            "active_room_id": presence.active_room_id,
            "last_seen": presence.last_seen.isoformat() if presence.last_seen else None,
            "last_notification_seen_at": presence.last_notification_seen_at.isoformat() if presence.last_notification_seen_at else None,
            "last_chat_seen_at": presence.last_chat_seen_at.isoformat() if presence.last_chat_seen_at else None,
            "last_app_state_change_at": presence.last_app_state_change_at.isoformat() if presence.last_app_state_change_at else None,
        }

    # Optional live FCM send probe: ?test_push=1 sends one real data message to
    # the user's registered FCM token and reports the exact result/error.
    test_push = None
    if request.GET.get("test_push"):
        from chat.push import send_fcm_test
        probe_device = (
            user.devices.filter(is_active=True)
            .exclude(fcm_token="")
            .exclude(fcm_token__isnull=True)
            .order_by("-last_seen")
            .first()
        )
        if probe_device and probe_device.fcm_token:
            # ?test_push=notif sends an OS-drawn notification-block probe (tests
            # killed/background display via Google Play Services); any other
            # truthy value sends the data-only probe.
            want_notif = request.GET.get("test_push") == "notif"
            test_push = send_fcm_test(probe_device.fcm_token, notification=want_notif)
        else:
            test_push = {"ok": False, "reason": "no_fcm_token_on_device"}

    return JsonResponse({
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": getattr(getattr(user, "profile", None), "display_name", "") or user.username,
        },
        "routing_state": routing_state,
        "presence": presence_payload,
        "notification_channels": channels,
        "notification_channel_count": len(channels),
        "devices": devices,
        "device_count": len(devices),
        "fcm": get_fcm_status(),
        "test_push": test_push,
        "recent_decisions": get_recent_notification_decisions(user_id=user_id, limit=50),
    })
