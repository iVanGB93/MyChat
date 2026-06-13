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
from users.models import UserDevice

logger = logging.getLogger(__name__)
User = get_user_model()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


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
        if not token or not token.startswith("ExponentPushToken["):
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
        logger.info("[ExpoPush] sent %d notification(s), status=%d", len(messages), resp.status_code)
        return True
    except Exception as exc:
        logger.warning("[ExpoPush] failed to send: %s", exc)
        return False


def send_message_push(
    recipient_ids: list[int],
    sender_name: str,
    content: str,
    room_id: str,
    room_name: str,
    correlation_id: str | None = None,
    route_reason: str | None = None,
) -> bool:
    """Send a push notification for a new chat message."""
    tokens = list(
        UserDevice.objects.filter(
            user_id__in=recipient_ids,
            is_active=True,
            user__profile__notif_messages_enabled=True,
            expo_push_token__startswith="ExponentPushToken[",
        ).values_list("expo_push_token", flat=True).distinct()
    )
    return _send_expo_push(
        push_tokens=tokens,
        title=sender_name,
        body=content[:200],
        data={
            "type": "new_message",
            "roomId": room_id,
            "roomName": room_name,
            "correlationId": correlation_id,
            "routeReason": route_reason,
            "correlation_id": correlation_id,
            "route_reason": route_reason,
        },
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
            expo_push_token__startswith="ExponentPushToken[",
        ).values_list("expo_push_token", flat=True).distinct()
    )
    icon = "📹" if call_type == "video" else "📞"
    return _send_expo_push(
        push_tokens=tokens,
        title=f"{icon} Incoming {call_type} call",
        body=f"{caller_name} is calling you",
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
