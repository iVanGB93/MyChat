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
) -> None:
    """
    Send a push notification to one or more Expo push tokens.
    Silently fails on errors (fire-and-forget for real-time notifications).
    """
    if not push_tokens:
        return

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
        return

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
    except Exception as exc:
        logger.warning("[ExpoPush] failed to send: %s", exc)


def send_message_push(
    recipient_ids: list[int],
    sender_name: str,
    content: str,
    room_id: str,
    room_name: str,
) -> None:
    """Send a push notification for a new chat message."""
    tokens = list(
        User.objects.filter(
            id__in=recipient_ids,
            notif_messages_enabled=True,
            expo_push_token__startswith="ExponentPushToken[",
        ).values_list("expo_push_token", flat=True)
    )
    _send_expo_push(
        push_tokens=tokens,
        title=sender_name,
        body=content[:200],
        data={
            "type": "new_message",
            "roomId": room_id,
            "roomName": room_name,
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
) -> None:
    """Send a push notification for an incoming call."""
    tokens = list(
        User.objects.filter(
            id=callee_id,
            notif_calls_enabled=True,
            expo_push_token__startswith="ExponentPushToken[",
        ).values_list("expo_push_token", flat=True)
    )
    icon = "📹" if call_type == "video" else "📞"
    _send_expo_push(
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
        },
        channel_id="calls",
        priority="high",
    )
