"""
Expo Push Notification utility.

Sends push notifications via Expo's Push API so users receive
notifications even when the app is completely closed.

API docs: https://docs.expo.dev/push-notifications/sending-notifications/
"""

import logging
from typing import Optional

import requests
from django.contrib.auth import get_user_model
from django.db.models import Q
from users.models import UserDevice

logger = logging.getLogger(__name__)
User = get_user_model()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


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
    tokens = list(
        UserDevice.objects.filter(
            user_id__in=recipient_ids,
            is_active=True,
            user__profile__notif_messages_enabled=True,
        ).filter(
            Q(expo_push_token__startswith="ExponentPushToken[")
            | Q(expo_push_token__startswith="ExpoPushToken[")
        ).values_list("expo_push_token", flat=True).distinct()
    )
    display_body = (content or "New message")[:200]
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
    # Pass through any extra client fields (reply_to, duration_ms, etc.)
    if extra_data:
        for k, v in extra_data.items():
            if k not in data and v is not None:
                # Expo push data values must be strings
                data[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return _send_expo_push(
        push_tokens=tokens,
        title=sender_name,
        body=display_body,
        data=data,
        channel_id="messages",
    )


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
            user__profile__notif_calls_enabled=True,
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
