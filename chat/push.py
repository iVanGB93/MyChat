"""
Expo Push Notification utility.

Sends push notifications via Expo's Push API so users receive
notifications even when the app is completely closed.

API docs: https://docs.expo.dev/push-notifications/sending-notifications/
"""

import logging
from typing import Optional

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from users.models import UserDevice

logger = logging.getLogger(__name__)
User = get_user_model()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

_fcm_initialized = False
_fcm_app = None


def _get_fcm_app():
    """Lazily initialise the firebase-admin app for FCM v1 sends.

    Returns the app instance, or None if no credential is configured or the
    SDK is unavailable. Safe to call repeatedly (memoised).
    """
    global _fcm_initialized, _fcm_app
    if _fcm_initialized:
        return _fcm_app
    _fcm_initialized = True
    info = getattr(settings, "FCM_SERVICE_ACCOUNT_INFO", None)
    if not info:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials

        try:
            _fcm_app = firebase_admin.get_app("axonic-fcm")
        except ValueError:
            cred = credentials.Certificate(info)
            _fcm_app = firebase_admin.initialize_app(cred, name="axonic-fcm")
    except Exception as exc:  # SDK missing or bad credential
        logger.warning("[FCM] init failed: %s", exc)
        _fcm_app = None
    return _fcm_app


def _send_fcm_data(
    fcm_tokens: list[str],
    data: dict,
    channel_id: str = "messages",
    priority: str = "high",
) -> bool:
    """Send a high-priority data-only FCM v1 message to raw FCM device tokens.

    No `notification` block is included so the app's background handler wakes
    (even when killed) and renders the notification itself. All data values
    must be strings. Fire-and-forget: never raises.
    """
    app = _get_fcm_app()
    if not app or not fcm_tokens:
        return False
    try:
        from firebase_admin import messaging
    except Exception:
        return False

    str_data = {
        k: ("" if v is None else str(v))
        for k, v in data.items()
    }
    str_data["channelId"] = channel_id

    messages = [
        messaging.Message(
            token=tok,
            data=str_data,
            android=messaging.AndroidConfig(priority=priority),
        )
        for tok in fcm_tokens
        if tok
    ]
    if not messages:
        return False
    try:
        resp = messaging.send_each(messages, app=app)
        if resp.failure_count:
            logger.warning(
                "[FCM] sent %d data message(s) success=%d failure=%d",
                len(messages), resp.success_count, resp.failure_count,
            )
        else:
            logger.info("[FCM] sent %d data message(s)", resp.success_count)
        return resp.success_count > 0
    except Exception as exc:
        logger.warning("[FCM] send failed: %s", exc)
        return False


def _is_expo_push_token(token: str) -> bool:
    return bool(token) and (
        token.startswith("ExponentPushToken[")
        or token.startswith("ExpoPushToken[")
    )


def _send_expo_push(
    push_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    channel_id: str = "messages",
    priority: str = "high",
    sound: str = "default",
) -> bool:
    """
    Send a push notification to one or more Expo push tokens.
    Silently fails on errors (fire-and-forget for real-time notifications).
    """
    if not push_tokens:
        return False

    messages = []
    for token in push_tokens:
        if not _is_expo_push_token(token):
            continue
        messages.append({
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "channelId": channel_id,
            "priority": priority,
            "sound": sound,
        })

    if not messages:
        return False

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        ticket_rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(ticket_rows, list):
            logger.info("[ExpoPush] sent %d notification(s), status=%d", len(messages), resp.status_code)
            return True

        accepted = 0
        errors = 0
        for row in ticket_rows:
            if isinstance(row, dict) and row.get("status") == "ok":
                accepted += 1
            else:
                errors += 1

        if errors:
            logger.warning(
                "[ExpoPush] ticket errors accepted=%d errors=%d payload=%s",
                accepted,
                errors,
                payload,
            )
        else:
            logger.info(
                "[ExpoPush] sent %d notification(s), accepted=%d",
                len(messages),
                accepted,
            )
        return accepted > 0
    except Exception as exc:
        logger.warning("[ExpoPush] failed to send: %s", exc)
        return False


def send_message_push(
    recipient_ids: list[int],
    sender_name: str,
    content: str | None,
    room_id: str,
    room_name: str,
    correlation_id: str | None = None,
    route_reason: str | None = None,
    message_id: str | None = None,
    sender_id: int | None = None,
    message_type: str | None = None,
    created_at: str | None = None,
    extra_data: dict | None = None,
) -> bool:
    """Send a push notification for a new chat message.

    When full message fields are provided they are embedded in the push data
    payload so the recipient app can save the message to its local SQLite DB
    immediately on push receipt — without waiting for the WS to reconnect.
    """
    device_rows = list(
        UserDevice.objects.filter(
            user_id__in=recipient_ids,
            is_active=True,
            user__notif_messages_enabled=True,
        ).filter(
            Q(expo_push_token__startswith="ExponentPushToken[")
            | Q(expo_push_token__startswith="ExpoPushToken[")
            | ~Q(fcm_token="")
        ).values_list("expo_push_token", "fcm_token").distinct()
    )
    # Prefer FCM data messages (WhatsApp-style background delivery). For devices
    # that have a raw FCM token we send ONLY the FCM data message; Expo is the
    # fallback for devices without one, so a device never gets a double push.
    fcm_tokens = [f for (_e, f) in device_rows if f]
    tokens = [
        e for (e, f) in device_rows
        if not f and _is_expo_push_token(e)
    ]
    # Notification body. Media messages carry no text content, so show a
    # type-aware placeholder ("📷 Photo" / "🎤 Voice message") instead of a
    # generic "New message".
    if content:
        display_body = content[:200]
    elif message_type in ("image", "photo"):
        display_body = "📷 Photo"
    elif message_type in ("voice", "audio"):
        display_body = "🎤 Voice message"
    elif message_type == "video":
        display_body = "🎬 Video"
    else:
        display_body = "New message"
    data: dict = {
        "type": "new_message",
        "roomId": room_id,
        "room_id": room_id,
        "roomName": room_name,
        "room_name": room_name,
        "correlationId": correlation_id,
        "routeReason": route_reason,
        "correlation_id": correlation_id,
        "route_reason": route_reason,
    }
    # Full message payload — lets the app save to SQLite without WS
    if message_id:
        data["messageId"] = message_id
        data["message_id"] = message_id
    if sender_id is not None:
        data["senderId"] = str(sender_id)
        data["sender_id"] = str(sender_id)
    if sender_name:
        data["sender"] = sender_name
    if content:
        data["content"] = content[:2000]
    if message_type:
        data["messageType"] = message_type
        data["message_type"] = message_type
    if created_at:
        data["createdAt"] = created_at
        data["created_at"] = created_at
    # Pass through any extra client fields (reply_to, duration_ms, etc.).
    #
    # CRITICAL: Expo limits the push *data* payload to ~4KB. If we exceed it the
    # payload gets truncated and trailing fields (messageId, content, ...) are
    # silently dropped — the recipient then can't save the message or send a
    # delivery ACK, so the sender's tick stays "pending" forever. Voice/image
    # messages carry large base64 blobs (audio_b64/image_b64) in the client
    # fields, so we must never forward those (or any oversized value) in the
    # push. The recipient hydrates media over the WS once it reconnects.
    if extra_data:
        # Known large/binary fields that must never ride in a push payload.
        _PUSH_BLOCKED_KEYS = {
            "audio_b64", "image_b64", "video_b64", "file_b64",
            "audio", "image", "video", "file", "thumbnail_b64", "waveform",
        }
        _MAX_FIELD_LEN = 500  # chars; anything longer is dropped from the push
        for k, v in extra_data.items():
            if k in data or v is None or k in _PUSH_BLOCKED_KEYS:
                continue
            # Expo push data values must be strings
            sv = v if isinstance(v, (str, int, float, bool)) else str(v)
            if isinstance(sv, str) and len(sv) > _MAX_FIELD_LEN:
                # Skip oversized values to keep the payload under Expo's limit.
                continue
            data[k] = sv
    # Notification-style push: the OS renders it with the real sender + content,
    # which is reliable even when the app is fully killed. (Data-only pushes are
    # NOT viable here - Expo/FCM still posts an empty notification for them.)
    # In-app grouping (WhatsApp-style) is handled by the local-notification path
    # when the app is alive; see pushNotificationService.showMessageNotification.
    sent = False
    if fcm_tokens:
        # FCM data message carries title/body so the app's background handler
        # can render the MessagingStyle notification itself.
        fcm_data = dict(data)
        fcm_data["title"] = sender_name or "New message"
        fcm_data["body"] = display_body
        sent = _send_fcm_data(
            fcm_tokens=fcm_tokens,
            data=fcm_data,
            channel_id="messages",
        ) or sent
    if tokens:
        sent = _send_expo_push(
            push_tokens=tokens,
            title=sender_name,
            body=display_body,
            data=data,
            channel_id="messages",
        ) or sent
    return sent


def send_call_push(
    callee_id: int,
    caller_name: str,
    call_type: str,
    call_id: str,
    caller_id: int,
    room_name: str,
    correlation_id: str | None = None,
    route_reason: str | None = None,
) -> bool:
    """Send a push notification for an incoming call."""
    tokens = list(
        UserDevice.objects.filter(
            user_id=callee_id,
            is_active=True,
               user__notif_calls_enabled=True,
        ).filter(
            Q(expo_push_token__startswith="ExponentPushToken[")
            | Q(expo_push_token__startswith="ExpoPushToken[")
        ).values_list("expo_push_token", flat=True).distinct()
    )
    icon = "📹" if call_type == "video" else "📞"
    return _send_expo_push(
        push_tokens=tokens,
        title=f"{icon} Incoming {call_type} call from {caller_name}",
        body=f"Tap to answer or swipe to decline",
        data={
            "type": "incoming_call",
            "callId": call_id,
            "callerName": caller_name,
            "callerId": caller_id,
            "callType": call_type,
            "roomName": room_name,
            "correlationId": correlation_id,
            "routeReason": route_reason,
            "correlation_id": correlation_id,
            "route_reason": route_reason,
        },
        channel_id="calls",
        priority="high",
    )
