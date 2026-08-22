import asyncio
from collections import deque
import json
import logging
import threading
import time
from datetime import datetime

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from .models import ChatRoom, PendingDelivery, MessageDelivery, OfflineEmailNudge
from .push import send_message_push
from users.models import UserDevice, UserPresence

User = get_user_model()
logger = logging.getLogger(__name__)

# ---- In-memory connected-user tracking ----
_connected_lock = threading.Lock()

# Notification WS connections
# {
#   user_id: {
#     "username": str,
#     "connected_at": str,
#     "channels": set[str],
#     "app_state": str,
#     "last_seen_ts": float,
#   }
# }
_connected_notification_users: dict[int, dict] = {}

# Chat room WS connections
# { room_id: set[user_id] }
_connected_chat_users: dict[str, set[int]] = {}
_decision_audit: deque[dict] = deque(maxlen=500)
# Authentication may need to wait for a busy deploy worker/database call after
# the WebSocket upgrade.  This protects unauthenticated connections without
# racing legitimate mobile clients on a cold or loaded instance.
WS_AUTH_TIMEOUT_SECONDS = 35.0
MESSAGE_ACK_TIMEOUT_SECONDS = int(getattr(settings, "MESSAGE_ACK_TIMEOUT_SECONDS", 8))
PRESENCE_STALE_SECONDS = int(getattr(settings, "PRESENCE_STALE_SECONDS", 70))
OFFLINE_EMAIL_COOLDOWN_HOURS = int(getattr(settings, "OFFLINE_EMAIL_COOLDOWN_HOURS", 24))


def _send_offline_message_email(recipient_email: str, recipient_name: str, sender_name: str) -> None:
    """Send a content-free, best-effort offline message nudge in a thread."""
    subject = f"{sender_name} sent you a message on Axonic"
    app_url = settings.AXONIC_APP_DOWNLOAD_URL
    body = (
        f"Hi {recipient_name},\n\n"
        f"{sender_name} sent you a message on Axonic.\n\n"
        f"Open Axonic to read and reply: {app_url}\n\n"
        "For your privacy, this email does not include message content. "
        "You can change offline email notifications in your Axonic profile.\n"
    )

    def send() -> None:
        try:
            if settings.RESEND_API_KEY:
                import requests
                response = requests.post(
                    "https://api.resend.com/emails",
                    json={"from": settings.DEFAULT_FROM_EMAIL, "to": [recipient_email], "subject": subject, "text": body},
                    headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
                    timeout=settings.EMAIL_TIMEOUT,
                )
                response.raise_for_status()
            elif settings.SENDGRID_API_KEY:
                import requests
                response = requests.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    json={
                        "personalizations": [{"to": [{"email": recipient_email}]}],
                        "from": {"email": settings.DEFAULT_FROM_EMAIL},
                        "subject": subject,
                        "content": [{"type": "text/plain", "value": body}],
                    },
                    headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    timeout=settings.EMAIL_TIMEOUT,
                )
                response.raise_for_status()
            else:
                from django.core.mail import send_mail
                send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient_email], fail_silently=False)
        except Exception:
            logger.exception("[OfflineEmail] failed recipient=%s", recipient_email)

    threading.Thread(target=send, daemon=True).start()


def _get_notification_socket_snapshot(user_id: int) -> tuple[int, str]:
    with _connected_lock:
        entry = _connected_notification_users.get(user_id)
        if not entry:
            return 0, UserPresence.APP_STATE_UNKNOWN
        return len(entry["channels"]), entry.get("app_state", UserPresence.APP_STATE_UNKNOWN)


def _get_chat_socket_snapshot(user_id: int) -> tuple[int, str]:
    with _connected_lock:
        room_ids = [room_id for room_id, users in _connected_chat_users.items() if user_id in users]
        return len(room_ids), room_ids[0] if room_ids else ""


def _is_entry_fresh(entry: dict) -> bool:
    last_seen_ts = float(entry.get("last_seen_ts", 0.0) or 0.0)
    return last_seen_ts > 0.0 and (time.time() - last_seen_ts) <= PRESENCE_STALE_SECONDS


def _touch_notification_presence(user_id: int) -> None:
    with _connected_lock:
        entry = _connected_notification_users.get(user_id)
        if entry:
            entry["last_seen_ts"] = time.time()


def get_user_presence_state(user_id: int) -> str:
    """Return active/background/disconnected/stale for notification routing."""
    try:
        presence = UserPresence.objects.get(user_id=user_id)
    except UserPresence.DoesNotExist:
        return "disconnected"

    if not presence.notification_socket_connected and not presence.chat_socket_connected:
        return "disconnected"
    if presence.is_stale(PRESENCE_STALE_SECONDS):
        return "stale"
    if presence.chat_socket_connected or presence.app_state == UserPresence.APP_STATE_ACTIVE:
        return "active"
    return "background"


def get_user_routing_state(user_id: int) -> dict:
    """Return structured backend state used for notification routing decisions."""
    try:
        presence = UserPresence.objects.get(user_id=user_id)
    except UserPresence.DoesNotExist:
        presence = None

    push_available = UserDevice.objects.filter(
        user_id=user_id,
        is_active=True,
        user__notif_messages_enabled=True,
    ).filter(
        Q(expo_push_token__startswith="ExponentPushToken[")
        | Q(expo_push_token__startswith="ExpoPushToken[")
        | ~Q(fcm_token="")
    ).exists()

    call_push_available = UserDevice.objects.filter(
        user_id=user_id,
        is_active=True,
        user__notif_calls_enabled=True,
    ).filter(
        Q(expo_push_token__startswith="ExponentPushToken[")
        | Q(expo_push_token__startswith="ExpoPushToken[")
        | ~Q(fcm_token="")
    ).exists()

    if presence is None:
        return {
            "presence": "disconnected",
            "app_state": UserPresence.APP_STATE_UNKNOWN,
            "chat_ws_connected": False,
            "chat_ws_count": 0,
            "notification_ws_connected": False,
            "notification_ws_count": 0,
            "active_room_id": "",
            "push_available": push_available,
            "call_push_available": call_push_available,
            "is_online": False,
            "is_stale": True,
        }

    is_stale = presence.is_stale(PRESENCE_STALE_SECONDS)
    if not presence.notification_socket_connected and not presence.chat_socket_connected:
        presence_state = "disconnected"
    elif is_stale:
        presence_state = "stale"
    elif presence.chat_socket_connected or presence.app_state == UserPresence.APP_STATE_ACTIVE:
        presence_state = "active"
    else:
        presence_state = "background"

    return {
        "presence": presence_state,
        "app_state": presence.app_state,
        "chat_ws_connected": presence.chat_socket_connected,
        "chat_ws_count": presence.chat_socket_count,
        "notification_ws_connected": presence.notification_socket_connected,
        "notification_ws_count": presence.notification_socket_count,
        "active_room_id": presence.active_room_id,
        "push_available": push_available,
        "call_push_available": call_push_available,
        "is_online": presence.is_online,
        "is_stale": is_stale,
    }


def decide_message_notification_route(room_id: str, routing_state: dict, in_room_via_chat_ws: bool, has_notification_channels: bool) -> str:
    """Classify the recipient as either actively in-room or off-screen.

    Mobile presence is too unreliable to pick a single delivery channel, so we
    no longer try. The only distinction that matters here is whether the
    recipient is *foreground and actively viewing this room* over a live chat
    WS — in which case they already received the message on the room socket and
    need no extra notification ("room_ws_active"). Every other state is
    "off_screen": the caller then relays over the notification WS AND queues a
    push floor, and the app decides what to surface from its own true state.

    CRITICAL: "room_ws_active" requires the app to be in the FOREGROUND
    (``app_state == active``), NOT merely a connected chat socket. When a user
    backgrounds the app it sends ``app_state: background`` but its room socket
    can linger for several seconds (until the OS/network drops it) — during
    that window ``chat_ws_connected`` is still True. If we suppressed the push
    on socket-connection alone, a just-backgrounded recipient would get NO push
    (the message only arrives over WS when they reopen). So we gate on
    ``app_state`` and freshness, matching the documented intent below.
    """
    if (
        routing_state.get("app_state") == UserPresence.APP_STATE_ACTIVE
        and not routing_state.get("is_stale")
        and in_room_via_chat_ws
        and routing_state["chat_ws_connected"]
        and routing_state["active_room_id"] == str(room_id)
    ):
        return "room_ws_active"
    return "off_screen"


def decide_call_notification_route(routing_state: dict, has_notification_channels: bool) -> str:
    if has_notification_channels and routing_state["notification_ws_connected"] and not routing_state["is_stale"]:
        return "notif_ws"
    return "ws_unavailable"


def record_notification_decision(
    *,
    kind: str,
    route: str,
    correlation_id: str,
    route_reason: str,
    sender_id: int | None = None,
    recipient_id: int | None = None,
    room_id: str | None = None,
    call_id: str | None = None,
    routing_state: dict | None = None,
) -> None:
    with _connected_lock:
        _decision_audit.appendleft({
            "at": timezone.now().isoformat(),
            "kind": kind,
            "route": route,
            "correlation_id": correlation_id,
            "route_reason": route_reason,
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "room_id": room_id,
            "call_id": call_id,
            "routing_state": routing_state or {},
        })


def get_recent_notification_decisions(user_id: int | None = None, limit: int = 50) -> list[dict]:
    with _connected_lock:
        rows = list(_decision_audit)
    if user_id is not None:
        rows = [
            row for row in rows
            if row.get("sender_id") == user_id or row.get("recipient_id") == user_id
        ]
    return rows[:limit]


def get_connected_notification_users() -> list[dict]:
    """Return list of users connected to the notification WebSocket."""
    with _connected_lock:
        return [
            {
                "user_id": uid,
                "username": info["username"],
                "connected_at": info["connected_at"],
                "connections": len(info["channels"]),
                "app_state": info.get("app_state", "unknown"),
                "presence": (
                    "active"
                    if info.get("app_state") == "active" and _is_entry_fresh(info)
                    else "background"
                    if _is_entry_fresh(info)
                    else "stale"
                ),
            }
            for uid, info in _connected_notification_users.items()
        ]


def get_connected_chat_rooms() -> dict[str, int]:
    """Return {room_id: count_of_connected_users}."""
    with _connected_lock:
        return {rid: len(users) for rid, users in _connected_chat_users.items() if users}


def is_user_ws_connected(user_id: int) -> bool:
    """Check if user has an active notification WebSocket."""
    with _connected_lock:
        return user_id in _connected_notification_users


def is_user_online(user_id: int) -> bool:
    """Check if user has the app in foreground (online = active app_state)."""
    return get_user_presence_state(user_id) == "active"


def get_user_notification_channels(user_id: int) -> set[str]:
    """Return all notification WS channel names for a user (empty set if offline)."""
    with _connected_lock:
        entry = _connected_notification_users.get(user_id)
        if not entry or not _is_entry_fresh(entry):
            return set()
        return set(entry["channels"])


def get_connected_chat_user_ids(room_id: str) -> set[int]:
    """Return IDs of users currently connected to the chat WS for a room."""
    with _connected_lock:
        return set(_connected_chat_users.get(str(room_id), set()))


@database_sync_to_async
def _authenticate_token_async(token_str: str):
    """Validate a JWT access token string and return the User or AnonymousUser."""
    try:
        token = AccessToken(token_str)
        return User.objects.get(id=token["user_id"])
    except Exception:
        return AnonymousUser()


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time chat.

    Connect:   ws://<host>/ws/chat/<room_id>/
    Send:      {"message": "Hello!", "message_type": "text"}
    Receive:   {"type": "chat_message", "message": {...}}
    """

    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group_name = f"chat_{self.room_id}"
        self.user = self.scope["user"]
        self._awaiting_auth = False
        self._accepted = False
        self._auth_timeout_task = None

        if self.user.is_anonymous:
            # Accept first, then wait for a post-connect {type: 'auth'} message
            await self.accept()
            self._accepted = True
            self._awaiting_auth = True
            self._auth_timeout_task = asyncio.ensure_future(self._auth_timeout())
            return

        # Already authenticated via query-string token — complete setup now
        await self._finish_chat_setup()

    async def _finish_chat_setup(self):
        """Complete connection setup once self.user is authenticated."""
        try:
            is_member = await self.is_room_member()
        except Exception:
            logger.exception("[ChatConsumer] is_room_member failed room=%s", self.room_id)
            await self.close(code=4500)
            return
        if not is_member:
            await self.close(code=4003)
            return
        try:
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        except Exception:
            logger.exception("[ChatConsumer] group_add failed room=%s", self.room_id)
        if not self._accepted:
            await self.accept()
            self._accepted = True
        with _connected_lock:
            _connected_chat_users.setdefault(self.room_id, set()).add(self.user.id)
        chat_socket_count, active_room_id = _get_chat_socket_snapshot(self.user.id)
        await self._sync_chat_presence(chat_socket_count, active_room_id)

    async def _auth_timeout(self):
        """Close the connection if no auth message arrives within auth timeout."""
        await asyncio.sleep(WS_AUTH_TIMEOUT_SECONDS)
        if self._awaiting_auth:
            client = self.scope.get("client") or ["?", 0]
            logger.warning(
                "[ChatConsumer] auth timeout — closing unauthenticated WS room=%s peer=%s:%s",
                getattr(self, "room_id", "?"), client[0], client[1],
            )
            await self.close(code=4001)

    async def disconnect(self, close_code):
        try:
            # Cancel pending auth timeout
            if hasattr(self, "_auth_timeout_task") and self._auth_timeout_task:
                self._auth_timeout_task.cancel()
                self._auth_timeout_task = None
            if hasattr(self, "room_group_name"):
                try:
                    await self.channel_layer.group_discard(
                        self.room_group_name, self.channel_name
                    )
                except Exception:
                    logger.exception("[ChatConsumer] group_discard failed")
            # Untrack connected chat user
            if hasattr(self, "user") and not self.user.is_anonymous:
                with _connected_lock:
                    room_users = _connected_chat_users.get(self.room_id)
                    if room_users:
                        room_users.discard(self.user.id)
                        if not room_users:
                            del _connected_chat_users[self.room_id]
                chat_socket_count, active_room_id = _get_chat_socket_snapshot(self.user.id)
                await self._sync_chat_presence(chat_socket_count, active_room_id)
        except Exception:
            logger.exception("[ChatConsumer] disconnect handler failed code=%s", close_code)

    async def receive(self, text_data):
        """Handle incoming messages from the WebSocket client."""
        try:
            data = json.loads(text_data)
        except Exception:
            logger.warning("[ChatConsumer] invalid JSON payload; ignoring frame")
            return

        msg_type = data.get("type", "")

        try:
            # ---- Post-connect authentication ----
            if self._awaiting_auth:
                if msg_type == "auth":
                    token_str = data.get("token", "")
                    user = await _authenticate_token_async(token_str)
                    if user.is_anonymous:
                        await self.send(text_data=json.dumps({"type": "auth_failed", "reason": "invalid_token"}))
                        await self.close(code=4001)
                        return
                    self.user = user
                    self._awaiting_auth = False
                    if self._auth_timeout_task:
                        self._auth_timeout_task.cancel()
                        self._auth_timeout_task = None
                    await self._finish_chat_setup()
                    await self.send(text_data=json.dumps({"type": "auth_ok"}))
                # Ignore all other messages until authenticated
                return

            # ---- Respond to keep-alive pings ----
            if msg_type == "ping":
                await self.send(text_data=json.dumps({"type": "pong"}))
                return

            # ---- Mark messages as read ----
            if msg_type == "mark_read":
                message_ids = data.get("message_ids", [])
                logger.info(
                    "[ChatConsumer] mark_read from user=%s room=%s ids=%s",
                    self.user.username, self.room_id, len(message_ids),
                )
                # Messages are relayed via WS and never stored server-side, so
                # we just relay the client-provided IDs as-is.
                broadcast_ids = message_ids
                if broadcast_ids:
                    # Broadcast to all users in the room's chat WS
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "messages_read",
                            "reader_id": self.user.id,
                            "reader": self.user.username,
                            "message_ids": broadcast_ids,
                        },
                    )
                    # Notify all room members via their notification WS channels
                    # (sender info is not tracked server-side for WS-native messages)
                    member_ids = await self.get_room_member_ids_except_self()
                    for member_id in member_ids:
                        await self.channel_layer.group_send(
                            f"notifications_{member_id}",
                            {
                                "type": "notify",
                                "payload": {
                                    "event": "messages_read",
                                    "room_id": str(self.room_id),
                                    "reader_id": self.user.id,
                                    "reader": self.user.username,
                                    "message_ids": broadcast_ids,
                                },
                            },
                        )
                return

            # ---- message_update: relay a mutation (is_read, reactions, …) to all room members ----
            # The server never inspects or stores the payload — it's a pure relay channel.
            # Changes are {is_read?, reactions?, is_deleted?, content?} keyed by message_id.
            if msg_type == "message_update":
                updates = data.get("updates", [])
                if updates:
                    # Confirm the relay plan before fan-out. The client keeps the
                    # content locally, but records this membership snapshot so a
                    # mutation is not marked synced until every peer has acked it.
                    member_ids = await self.get_room_member_ids_except_self()
                    await self.send(text_data=json.dumps({
                        "type": "message_update_server_ack",
                        "updates": [
                            {
                                "id": update.get("id"),
                                "expected_peer_ids": member_ids,
                            }
                            for update in updates
                            if isinstance(update, dict) and update.get("id")
                        ],
                    }))
                    # Relay to all users in the chat-room WebSocket group (sender excluded
                    # by the message_update_broadcast handler below)
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "message_update_broadcast",
                            "sender_id": self.user.id,
                            "updates": updates,
                        },
                    )
                    # Also relay via notification channels for members not in the chat WS
                    for member_id in member_ids:
                        await self.channel_layer.group_send(
                            f"notifications_{member_id}",
                            {
                                "type": "notify",
                                "payload": {
                                    "event": "message_update",
                                    "room_id": str(self.room_id),
                                    "updates": updates,
                                    # Sender info so the recipient can show "X reacted 👍" etc.
                                    "from_user_id": self.user.id,
                                    "from_username": self.user.username,
                                },
                            },
                        )
                return

            # ---- message_update_ack: recipient confirmed it applied message_update(s) ----
            if msg_type == "message_update_ack":
                update_ids = data.get("update_ids", [])
                sender_id = data.get("sender_id")
                room_id = data.get("room_id", str(self.room_id))
                if not update_ids or not sender_id:
                    return
                for channel in get_user_notification_channels(int(sender_id)):
                    await self.channel_layer.send(
                        channel,
                        {
                            "type": "notify",
                            "payload": {
                                "event": "message_update_ack",
                                "room_id": str(room_id),
                                "update_ids": update_ids,
                                "by_user_id": self.user.id,
                                "by_username": self.user.username,
                            },
                        },
                    )
                return

            # ---- ready_to_receive: offline user connected — signal senders to flush outbox ----
            if msg_type == "ready_to_receive":
                pending_senders = await self.get_pending_senders_for_room()
                for sender_id in pending_senders:
                    for channel in get_user_notification_channels(sender_id):
                        await self.channel_layer.send(
                            channel,
                            {
                                "type": "notify",
                                "payload": {
                                    "event": "receiver_ready",
                                    "room_id": str(self.room_id),
                                    "user_id": self.user.id,
                                    "username": self.user.username,
                                },
                            },
                        )
                return

            # ---- typing: ephemeral relay, no DB writes ----
            # Client sends {"type": "typing", "is_typing": true|false}.
            # We rebroadcast to everyone in the room group (excluding the sender)
            # so other open chat clients can show a "… is typing" hint.
            if msg_type == "typing":
                is_typing = bool(data.get("is_typing", True))
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "typing_broadcast",
                        "sender_id": self.user.id,
                        "sender": self.user.username,
                        "is_typing": is_typing,
                    },
                )
                # Also relay via notification channels so list rows can show
                # the indicator even when the recipient hasn't opened the room WS.
                member_ids = await self.get_room_member_ids_except_self()
                for member_id in member_ids:
                    await self.channel_layer.group_send(
                        f"notifications_{member_id}",
                        {
                            "type": "notify",
                            "payload": {
                                "event": "typing",
                                "room_id": str(self.room_id),
                                "sender_id": self.user.id,
                                "sender": self.user.username,
                                "is_typing": is_typing,
                            },
                        },
                    )
                return

            # ---- message_ack: receiver confirmed it stored the message locally ----
            if msg_type == "message_ack":
                message_id = data.get("message_id", "")
                sender_id = data.get("sender_id")
                if not message_id or not sender_id:
                    return
                acked = await self.mark_message_delivery_acked(
                    message_id=message_id,
                    sender_id=int(sender_id),
                    room_id=str(self.room_id),
                )
                if not acked:
                    return
                logger.info(
                    "[DeliveryAck] ws room=%s message_id=%s sender=%s recipient=%s",
                    str(self.room_id),
                    message_id,
                    int(sender_id),
                    self.user.id,
                )
                for channel in get_user_notification_channels(int(sender_id)):
                    await self.channel_layer.send(
                        channel,
                        {
                            "type": "notify",
                            "payload": {
                                "event": "message_delivery_ack",
                                "message_id": message_id,
                                "by_user_id": self.user.id,
                                "by_username": self.user.username,
                                "room_id": str(self.room_id),
                            },
                        },
                    )
                return

            # ---- media_chunk: pure relay of one slice of a large media message ----
            # Large photos/voice exceed the ~1 MiB WS frame limit, so the client
            # splits the base64 into chunks and streams them. We relay each chunk
            # to the OTHER room members over the room group only — no DB write and
            # no notification/push (the small placeholder message already handled
            # delivery tracking + notification). The receiver reassembles the
            # chunks back into the media file. Media bytes never touch the server.
            if msg_type == "media_chunk":
                data["sender_id"] = self.user.id  # trust the authenticated sender
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "media_chunk_relay",
                        "sender_id": self.user.id,
                        "chunk": data,
                    },
                )
                return

            # ---- Outgoing message relay (no DB persistence) ----
            # The server is a dumb relay: forward whatever fields the client
            # sent, only injecting/overriding the trusted identity fields
            # (sender, sender_id) and a server-side fallback timestamp.
            # Apps are responsible for interpreting the payload schema.
            message_content = data.get("message", "")
            message_type_str = data.get("message_type", "text")
            message_id = data.get("id", "")           # client-generated UUID
            created_at = data.get("created_at", timezone.now().isoformat())

            if not message_content or not message_id:
                return

            # Pass through every client-supplied field except control keys and
            # anything we want to authoritatively set ourselves.
            _RESERVED = {"type", "message", "id", "created_at",
                         "sender", "sender_id", "hydration"}
            msg_data = {k: v for k, v in data.items() if k not in _RESERVED}
            msg_data.update({
                "id": message_id,
                "sender": self.user.username,
                "sender_id": self.user.id,
                "content": message_content,
                "message_type": message_type_str,
                "created_at": created_at,
                "correlation_id": f"msg:{message_id}",
                "route_reason": "room_ws",
            })

            # Broadcast to all users currently in this room's chat WS
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "message": msg_data,
                },
            )

            # Media-hydration re-send: a recipient that received a b64-stripped
            # push asked us to re-deliver the media for an EXISTING message. It
            # already has the message row and was already notified, so relay over
            # the room WS group above only — never re-run the notification/push
            # fan-out (that would fire a duplicate notification).
            if data.get("hydration"):
                return

            # For members NOT actively in the room chat WS (either not connected
            # to the room, or connected but app is backgrounded):
            # relay via ALL their notification channels (multi-device),
            # or create a PendingDelivery + push if completely offline.
            room_info = await self.get_room_info()
            in_room_ids = get_connected_chat_user_ids(self.room_id)
            # Anyone who blocked the sender must never receive this fan-out.
            blockers = await self.get_blockers_of_sender()
            push_recipients: list[int] = []
            tracked_delivery_count = 0

            for member_id in room_info["member_ids"]:
                if member_id == self.user.id:
                    continue
                if member_id in blockers:
                    record_notification_decision(
                        kind="message",
                        route="blocked",
                        correlation_id=f"msg:{message_id}",
                        route_reason="blocked_by_recipient",
                        sender_id=self.user.id,
                        recipient_id=member_id,
                        room_id=str(self.room_id),
                    )
                    logger.info(
                        "[NotifyDecision] kind=message route=blocked room=%s message_id=%s sender=%s recipient=%s",
                        str(self.room_id),
                        message_id,
                        self.user.id,
                        member_id,
                    )
                    continue
                await self.create_message_delivery(message_id, member_id)
                await self.create_pending_delivery(member_id)
                tracked_delivery_count += 1
                routing_state = await self.get_user_routing_snapshot(member_id)
                member_channels = get_user_notification_channels(member_id)
                route = decide_message_notification_route(
                    room_id=str(self.room_id),
                    routing_state=routing_state,
                    in_room_via_chat_ws=member_id in in_room_ids,
                    has_notification_channels=bool(member_channels),
                )
                # A member counts as "already delivered via room group_send" only
                # if their app is ALSO in the foreground. If they're connected to
                # the room WS but app is backgrounded, we still need to push a
                # notification — and the per-socket chat_message handler below
                # suppresses the in-app delivery to that backgrounded socket.
                if route == "room_ws_active":
                    await self.mark_message_delivery_routed(
                        message_id=message_id,
                        recipient_id=member_id,
                        routed_via=MessageDelivery.ROUTE_CHAT_WS,
                    )
                    record_notification_decision(
                        kind="message",
                        route=route,
                        correlation_id=f"msg:{message_id}",
                        route_reason="recipient_active_in_room",
                        sender_id=self.user.id,
                        recipient_id=member_id,
                        room_id=str(self.room_id),
                        routing_state=routing_state,
                    )
                    logger.info(
                        "[NotifyDecision] kind=message route=room_ws_active room=%s message_id=%s sender=%s recipient=%s state=%s",
                        str(self.room_id),
                        message_id,
                        self.user.id,
                        member_id,
                        routing_state,
                    )
                    continue

                # Recipient is OFF-SCREEN for this room (not connected, or
                # connected but backgrounded, or viewing another screen).
                # Per the "send everything, let the app decide" model we do BOTH
                # of the following independently — we no longer try to guess a
                # single channel from (unreliable) presence:
                #   1) Relay the full message over every notification WS the user
                #      has open (instant in-app toast / background save), AND
                #   2) ALWAYS queue an FCM/Expo push as the guaranteed
                #      notification floor whenever a push token exists.
                # The OS only renders the push banner when the app is
                # backgrounded/killed; a foreground app receives the same push
                # silently (onMessage), so this never double-notifies. The app
                # itself decides what to display based on its own true state.
                relayed_via_ws = False
                will_push = bool(routing_state["push_available"])
                if member_channels:
                    # Relay to ALL notification sessions (multi-device support).
                    # Pass through every field from msg_data so the apps see the
                    # exact same payload they'd get on the room socket.
                    try:
                        sender_avatar = self.user.avatar.url if self.user.avatar and self.user.avatar.name else None
                    except Exception:
                        sender_avatar = None
                    notify_payload = {
                        "event": "new_message",
                        "room_id": str(self.room_id),
                        "room_name": room_info["name"],
                        "sender": self.user.username,
                        "sender_id": self.user.id,
                        "sender_avatar": sender_avatar,
                        "content": message_content,
                        "message_id": message_id,
                        "message_type": message_type_str,
                        "created_at": created_at,
                        "correlation_id": f"msg:{message_id}",
                        "route_reason": "notif_ws",
                        # Tell the app a push floor is also in flight so it can
                        # defer the OS banner to FCM and avoid double-notifying.
                        # When False, the app is the ONLY notification surface.
                        "push_floor": will_push,
                    }
                    # Forward any extra client-supplied fields (e.g. reply_to)
                    # without the consumer having to know about them.
                    for k, v in msg_data.items():
                        if k not in notify_payload and k not in ("id", "sender"):
                            notify_payload[k] = v
                    for channel in member_channels:
                        await self.channel_layer.send(
                            channel,
                            {
                                "type": "notify",
                                "payload": notify_payload,
                            },
                        )
                    relayed_via_ws = True

                if will_push:
                    push_recipients.append(member_id)
                    routed_via = MessageDelivery.ROUTE_PUSH
                    route_label = "push_floor"
                    route_reason = "push_floor_always"
                elif relayed_via_ws:
                    routed_via = MessageDelivery.ROUTE_NOTIF_WS
                    route_label = "notif_ws"
                    route_reason = "notification_ws_no_push_token"
                else:
                    routed_via = MessageDelivery.ROUTE_PENDING_ONLY
                    route_label = "pending_only"
                    route_reason = "no_push_endpoint_available"

                await self.mark_message_delivery_routed(
                    message_id=message_id,
                    recipient_id=member_id,
                    routed_via=routed_via,
                )
                record_notification_decision(
                    kind="message",
                    route=route_label,
                    correlation_id=f"msg:{message_id}",
                    route_reason=route_reason,
                    sender_id=self.user.id,
                    recipient_id=member_id,
                    room_id=str(self.room_id),
                    routing_state=routing_state,
                )
                logger.info(
                    "[NotifyDecision] kind=message route=%s room=%s message_id=%s sender=%s recipient=%s state=%s ws_relay=%s push=%s",
                    route_label,
                    str(self.room_id),
                    message_id,
                    self.user.id,
                    member_id,
                    routing_state,
                    relayed_via_ws,
                    will_push,
                )

            if push_recipients:
                logger.info(
                    "[Push] Sending push to %d offline user(s) (of %d total recipients)",
                    len(push_recipients), len(room_info["member_ids"]) - 1,
                )
                sent = await self._send_message_push(
                    recipient_ids=push_recipients,
                    sender_name=self.user.username,
                    content=message_content,
                    room_id=str(self.room_id),
                    room_name=room_info["name"],
                    correlation_id=f"msg:{message_id}",
                    route_reason="push_initial",
                    message_id=message_id,
                    sender_id=self.user.id,
                    message_type=message_type_str,
                    created_at=created_at,
                    extra_data={
                        k: v for k, v in msg_data.items()
                        if k not in ("id", "sender", "sender_id", "content",
                                     "message_type", "created_at",
                                     "correlation_id", "route_reason")
                    },
                )
                if sent:
                    await self.mark_push_sent(message_id, push_recipients)
                    for recipient_id in push_recipients:
                        record_notification_decision(
                            kind="message",
                            route="push_sent",
                            correlation_id=f"msg:{message_id}",
                            route_reason="push_initial_sent",
                            sender_id=self.user.id,
                            recipient_id=recipient_id,
                            room_id=str(self.room_id),
                        )

            # WS/notification delivery can look "connected" but still fail to reach
            # the recipient. If we do not receive a message_ack in time, trigger
            # push fallback for any still-pending recipients.
            if tracked_delivery_count > 0:
                asyncio.create_task(
                    self.push_fallback_after_timeout(
                        message_id=message_id,
                        sender_name=self.user.username,
                        room_name=room_info["name"],
                        content=message_content,
                        message_type=message_type_str,
                        created_at=created_at,
                        extra_data={
                            k: v for k, v in msg_data.items()
                            if k not in ("id", "sender", "sender_id", "content",
                                         "message_type", "created_at",
                                         "correlation_id", "route_reason")
                        },
                    )
                )

            # Acknowledge to sender: server received and relayed this message
            await self.send(text_data=json.dumps({
                "type": "message_server_ack",
                "message_id": message_id,
                "correlation_id": f"msg:{message_id}",
            }))
        except Exception:
            logger.exception(
                "[ChatConsumer] unhandled receive error user=%s room=%s type=%s",
                getattr(self.user, "id", None),
                getattr(self, "room_id", None),
                msg_type,
            )
            try:
                await self.send(text_data=json.dumps({"type": "server_error", "op": msg_type}))
            except Exception:
                pass

    async def chat_message(self, event):
        """Send message to WebSocket client.

        Suppress delivery when the recipient's app is backgrounded — in that
        case the sender's `receive` handler also relays this message via the
        notification WS, which triggers a local push/toast on the client.
        Without this guard, a user sitting on the chat-room screen with the
        app in the background would silently receive messages on the room
        socket and never get a system notification.
        """
        try:
            sender_id = event.get("message", {}).get("sender_id")
            # If this user has blocked the sender, drop the frame entirely.
            if sender_id is not None and sender_id != self.user.id:
                if await self._is_sender_blocked_by_me(sender_id):
                    return
            # A live room socket is an explicit delivery channel. Do not drop
            # its frames based on the separate presence flag: mobile browsers
            # and backgrounded clients can legitimately have a connected room
            # socket while presence is briefly stale. The notification/push
            # fan-out below is responsible for deciding notification surfaces;
            # clients de-duplicate any overlap themselves.
            await self.send(text_data=json.dumps(event["message"]))
        except Exception:
            logger.exception("[ChatConsumer.chat_message] failed user=%s room=%s",
                             getattr(self.user, "id", None), getattr(self, "room_id", None))

    async def messages_read(self, event):
        """Broadcast read receipt to WebSocket client."""
        try:
            await self.send(text_data=json.dumps({
                "type": "messages_read",
                "reader_id": event["reader_id"],
                "reader": event["reader"],
                "message_ids": event["message_ids"],
            }))
        except Exception:
            logger.exception("[ChatConsumer.messages_read] failed user=%s room=%s",
                             getattr(self.user, "id", None), getattr(self, "room_id", None))

    async def message_update_broadcast(self, event):
        """Relay a message_update event to the WebSocket client. Skip the originating sender."""
        try:
            if event.get("sender_id") == self.user.id:
                return
            await self.send(text_data=json.dumps({
                "type": "message_update",
                "sender_id": event.get("sender_id"),
                "updates": event["updates"],
            }))
        except Exception:
            logger.exception("[ChatConsumer.message_update_broadcast] failed user=%s room=%s",
                             getattr(self.user, "id", None), getattr(self, "room_id", None))

    async def typing_broadcast(self, event):
        """Relay a typing event to the WebSocket client. Skip the originating sender."""
        try:
            if event.get("sender_id") == self.user.id:
                return
            await self.send(text_data=json.dumps({
                "type": "typing",
                "sender_id": event["sender_id"],
                "sender": event["sender"],
                "is_typing": event["is_typing"],
            }))
        except Exception:
            logger.exception("[ChatConsumer.typing_broadcast] failed user=%s room=%s",
                             getattr(self.user, "id", None), getattr(self, "room_id", None))

    async def media_chunk_relay(self, event):
        """Relay one media chunk to the room's clients, skipping the sender.

        Pure pass-through: the chunk dict is forwarded verbatim so the receiver
        can reassemble the media. No DB write, no notification/push.
        """
        try:
            if event.get("sender_id") == self.user.id:
                return
            await self.send(text_data=json.dumps(event["chunk"]))
        except Exception:
            logger.exception("[ChatConsumer.media_chunk_relay] failed user=%s room=%s",
                             getattr(self.user, "id", None), getattr(self, "room_id", None))

    # --- Database helpers (run in thread) ---

    @database_sync_to_async
    def is_room_member(self) -> bool:
        return ChatRoom.objects.filter(
            id=self.room_id, members=self.user
        ).exists()

    @database_sync_to_async
    def create_pending_delivery(self, to_user_id: int) -> None:
        PendingDelivery.objects.get_or_create(
            room_id=self.room_id,
            from_user_id=self.user.id,
            to_user_id=to_user_id,
        )

    @database_sync_to_async
    def delete_pending_delivery(self, from_user_id: int) -> None:
        PendingDelivery.objects.filter(
            room_id=self.room_id,
            from_user_id=from_user_id,
            to_user_id=self.user.id,
        ).delete()

    @database_sync_to_async
    def get_pending_senders_for_room(self) -> list[int]:
        return list(
            PendingDelivery.objects.filter(
                room_id=self.room_id,
                to_user=self.user,
            ).values_list("from_user_id", flat=True)
        )

    @database_sync_to_async
    def get_room_info(self) -> dict:
        """Return room name, type, and member IDs for notification dispatch."""
        room = ChatRoom.objects.get(id=self.room_id)
        member_ids = list(room.members.values_list("id", flat=True))
        # For direct chats with no custom name, use the sender's username
        # so recipients see the sender's name instead of the room UUID.
        if room.room_type == ChatRoom.DIRECT and not room.name:
            display_name = self.user.username
        else:
            display_name = room.name or str(room.id)
        return {
            "name": display_name,
            "room_type": room.room_type,
            "member_ids": member_ids,
        }

    @database_sync_to_async
    def set_user_online(self, online: bool):
        now = timezone.now()
        User.objects.filter(id=self.user.id).update(is_online=online, last_seen=now)
        UserPresence.objects.update_or_create(
            user=self.user,
            defaults={"is_online": online, "last_seen": now},
        )

    @database_sync_to_async
    def _sync_chat_presence(self, chat_socket_count: int, active_room_id: str):
        now = timezone.now()
        presence, _ = UserPresence.objects.get_or_create(
            user=self.user,
            defaults={"last_seen": now},
        )
        is_online = bool(chat_socket_count) or (
            presence.notification_socket_connected
            and presence.app_state == UserPresence.APP_STATE_ACTIVE
        )
        UserPresence.objects.filter(pk=presence.pk).update(
            chat_socket_count=chat_socket_count,
            chat_socket_connected=chat_socket_count > 0,
            active_room_id=active_room_id if chat_socket_count > 0 else "",
            last_chat_seen_at=now,
            last_seen=now,
            is_online=is_online,
        )
        User.objects.filter(id=self.user.id).update(is_online=is_online, last_seen=now)

    @database_sync_to_async
    def _send_message_push(
        self,
        recipient_ids,
        sender_name,
        content,
        room_id,
        room_name,
        correlation_id=None,
        route_reason=None,
        message_id=None,
        sender_id=None,
        message_type=None,
        created_at=None,
        extra_data=None,
    ):
        """Send Expo push notification for a new message (runs in thread)."""
        return send_message_push(
            recipient_ids=recipient_ids,
            sender_name=sender_name,
            content=content,
            room_id=room_id,
            room_name=room_name,
            correlation_id=correlation_id,
            route_reason=route_reason,
            message_id=message_id,
            sender_id=sender_id,
            message_type=message_type,
            created_at=created_at,
            extra_data=extra_data,
        )

    async def push_fallback_after_timeout(
        self,
        message_id: str,
        sender_name: str,
        room_name: str,
        content: str | None = None,
        message_type: str | None = None,
        created_at: str | None = None,
        extra_data: dict | None = None,
    ):
        await asyncio.sleep(MESSAGE_ACK_TIMEOUT_SECONDS)
        pending_recipient_ids = await self.get_pending_recipients_for_message(message_id)
        if not pending_recipient_ids:
            return
        sent = await self._send_message_push(
            recipient_ids=pending_recipient_ids,
            sender_name=sender_name,
            content=content,  # real message content so a killed/backgrounded
                              # recipient can persist it (not just ack it)
            room_id=str(self.room_id),
            room_name=room_name,
            correlation_id=f"msg:{message_id}",
            route_reason="push_fallback_timeout",
            message_id=message_id,
            sender_id=self.user.id,
            message_type=message_type,
            created_at=created_at,
            extra_data=extra_data,
        )
        if sent:
            await self.mark_push_sent(message_id, pending_recipient_ids)
            logger.info(
                    "[NotifyDecision] kind=message route=push_fallback_timeout room=%s message_id=%s recipients=%d",
                str(self.room_id),
                message_id,
                len(pending_recipient_ids),
            )

    @database_sync_to_async
    def mark_messages_read(self, message_ids: list) -> dict:
        """DEPRECATED no-op: messages are not stored server-side.

        Retained so existing call sites compile; returns empty results.
        """
        return {"updated_ids": [], "sender_ids": []}

    @database_sync_to_async
    def get_room_member_ids_except_self(self) -> list[int]:
        """Return IDs of all room members except the current user."""
        room = ChatRoom.objects.get(id=self.room_id)
        return list(room.members.exclude(id=self.user.id).values_list("id", flat=True))

    @database_sync_to_async
    def create_message_delivery(self, message_id: str, recipient_id: int) -> None:
        MessageDelivery.objects.get_or_create(
            room_id=self.room_id,
            message_id=message_id,
            sender_id=self.user.id,
            recipient_id=recipient_id,
            defaults={"status": MessageDelivery.STATUS_PENDING},
        )

    @database_sync_to_async
    def mark_message_delivery_routed(self, message_id: str, recipient_id: int, routed_via: str) -> None:
        MessageDelivery.objects.filter(
            room_id=self.room_id,
            message_id=message_id,
            sender_id=self.user.id,
            recipient_id=recipient_id,
        ).update(
            routed_via=routed_via,
            routed_at=timezone.now(),
        )

    @database_sync_to_async
    def mark_message_delivery_acked(self, message_id: str, sender_id: int, room_id: str) -> bool:
        now = timezone.now()
        updated = MessageDelivery.objects.filter(
            room_id=room_id,
            message_id=message_id,
            sender_id=sender_id,
            recipient_id=self.user.id,
            status=MessageDelivery.STATUS_PENDING,
        ).update(
            status=MessageDelivery.STATUS_DELIVERED,
            delivered_at=now,
        )
        if not updated:
            return False
        has_pending_from_sender = MessageDelivery.objects.filter(
            room_id=room_id,
            sender_id=sender_id,
            recipient_id=self.user.id,
            status=MessageDelivery.STATUS_PENDING,
        ).exists()
        if not has_pending_from_sender:
            PendingDelivery.objects.filter(
                room_id=room_id,
                from_user_id=sender_id,
                to_user_id=self.user.id,
            ).delete()
        return True

    @database_sync_to_async
    def get_pending_recipients_for_message(self, message_id: str) -> list[int]:
        return list(
            MessageDelivery.objects.filter(
                room_id=self.room_id,
                message_id=message_id,
                status=MessageDelivery.STATUS_PENDING,
                push_sent_at__isnull=True,
            ).values_list("recipient_id", flat=True)
        )

    @database_sync_to_async
    def mark_push_sent(self, message_id: str, recipient_ids: list[int]) -> None:
        if not recipient_ids:
            return
        MessageDelivery.objects.filter(
            room_id=self.room_id,
            message_id=message_id,
            recipient_id__in=recipient_ids,
        ).update(
            push_sent_at=timezone.now(),
            routed_via=MessageDelivery.ROUTE_PUSH,
            routed_at=timezone.now(),
        )

    @database_sync_to_async
    def get_user_routing_snapshot(self, user_id: int) -> dict:
        return get_user_routing_state(user_id)

    @database_sync_to_async
    def _is_user_online_async(self, user_id: int) -> bool:
        return is_user_online(user_id)

    @database_sync_to_async
    def get_blockers_of_sender(self) -> set[int]:
        """Return the set of user IDs who have blocked the current sender.
        Messages from `self.user` must NOT fan-out to anyone in this set."""
        from users.models import BlockedUser
        return set(
            BlockedUser.objects.filter(blocked=self.user).values_list("owner_id", flat=True)
        )

    @database_sync_to_async
    def _is_sender_blocked_by_me(self, sender_id: int) -> bool:
        """True when `self.user` has blocked `sender_id`."""
        from users.models import BlockedUser
        return BlockedUser.objects.filter(
            owner=self.user, blocked_id=sender_id,
        ).exists()


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    Per-user WebSocket for push-style notifications (incoming calls, etc.)
    and WebRTC signaling (offer / answer / ICE candidates).

    Connect:  ws://<host>/ws/notifications/
              (then send {"type": "auth", "token": "<jwt>"} as first message)

    The client sends {"type": "app_state", "state": "active"|"background"}
    to report whether the app is in the foreground.
        - active   → user is "online" (app open, viewing the screen)
        - background → user is "connected" (WS alive, but app not in foreground)
    """

    async def connect(self):
        self.user = self.scope["user"]
        self._awaiting_auth = False
        self._accepted = False
        self._auth_timeout_task = None

        if self.user.is_anonymous:
            # Accept first, then wait for a post-connect {type: 'auth'} message
            await self.accept()
            self._accepted = True
            self._awaiting_auth = True
            self._auth_timeout_task = asyncio.ensure_future(self._auth_timeout())
            return

        # Already authenticated via query-string token — complete setup now
        await self._finish_notification_setup()

    async def _finish_notification_setup(self):
        """Complete connection setup once self.user is authenticated."""
        self.group_name = f"notifications_{self.user.id}"
        try:
            await self.channel_layer.group_add(self.group_name, self.channel_name)
        except Exception:
            logger.exception("[NotificationConsumer] group_add failed user=%s", self.user.id)
        if not self._accepted:
            await self.accept()
            self._accepted = True
        with _connected_lock:
            entry = _connected_notification_users.get(self.user.id)
            if entry:
                entry["channels"].add(self.channel_name)
                entry["last_seen_ts"] = time.time()
            else:
                _connected_notification_users[self.user.id] = {
                    "username": self.user.username,
                    "connected_at": datetime.utcnow().isoformat(),
                    "channels": {self.channel_name},
                    "app_state": "active",
                    "last_seen_ts": time.time(),
                }
        try:
            notification_socket_count, app_state = _get_notification_socket_snapshot(self.user.id)
            await self._sync_notification_presence(notification_socket_count, app_state=app_state, touch=True)
        except Exception:
            logger.exception("[NotificationConsumer] _sync_notification_presence failed user=%s", self.user.id)

        # Notify client of any messages waiting for delivery from offline senders
        try:
            pending = await self.get_pending_deliveries()
        except Exception:
            logger.exception("[NotificationConsumer] get_pending_deliveries failed user=%s", self.user.id)
            pending = []
        if pending:
            try:
                await self.send(text_data=json.dumps({
                    "type": "pending_deliveries",
                    "deliveries": pending,
                }))
            except Exception:
                logger.exception("[NotificationConsumer] pending_deliveries send failed")

        # If the user has other active sessions, offer peer sync
        with _connected_lock:
            entry = _connected_notification_users.get(self.user.id)
            other_channels = (
                [c for c in entry["channels"] if c != self.channel_name]
                if entry else []
            )
        if other_channels:
            await self.send(text_data=json.dumps({"type": "peer_sync_available"}))

        logger.info("[WS] Notification connected: user=%s", self.user.username)

    async def _auth_timeout(self):
        """Close the connection if no auth message arrives within auth timeout."""
        await asyncio.sleep(WS_AUTH_TIMEOUT_SECONDS)
        if self._awaiting_auth:
            client = self.scope.get("client") or ["?", 0]
            logger.warning(
                "[NotificationConsumer] auth timeout — closing unauthenticated WS peer=%s:%s",
                client[0], client[1],
            )
            await self.close(code=4001)

    async def disconnect(self, close_code):
        try:
            # Cancel pending auth timeout
            if hasattr(self, "_auth_timeout_task") and self._auth_timeout_task:
                self._auth_timeout_task.cancel()
                self._auth_timeout_task = None
            if hasattr(self, "group_name"):
                try:
                    await self.channel_layer.group_discard(
                        self.group_name, self.channel_name
                    )
                except Exception:
                    logger.exception("[NotificationConsumer] group_discard failed")
            # Untrack notification user
            if hasattr(self, "user") and not self.user.is_anonymous:
                with _connected_lock:
                    entry = _connected_notification_users.get(self.user.id)
                    if entry:
                        entry["channels"].discard(self.channel_name)
                        if not entry["channels"]:
                            del _connected_notification_users[self.user.id]
                # Mark user offline in DB when last WS disconnects
                still_connected = is_user_ws_connected(self.user.id)
                try:
                    notification_socket_count, _app_state = _get_notification_socket_snapshot(self.user.id)
                    await self._sync_notification_presence(notification_socket_count, touch=True)
                except Exception:
                    logger.exception("[NotificationConsumer] _sync_notification_presence(disconnect) failed")
                logger.info("[WS] Notification disconnected: user=%s code=%s (still_connected=%s)",
                            self.user.username, close_code, still_connected)
        except Exception:
            logger.exception("[NotificationConsumer] disconnect handler failed code=%s", close_code)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except Exception:
            logger.warning("[NotificationConsumer] invalid JSON payload; ignoring frame")
            return

        msg_type = data.get("type")

        try:
            # ---- Post-connect authentication ----
            if self._awaiting_auth:
                if msg_type == "auth":
                    token_str = data.get("token", "")
                    user = await _authenticate_token_async(token_str)
                    if user.is_anonymous:
                        await self.send(text_data=json.dumps({"type": "auth_failed", "reason": "invalid_token"}))
                        await self.close(code=4001)
                        return
                    self.user = user
                    self._awaiting_auth = False
                    if self._auth_timeout_task:
                        self._auth_timeout_task.cancel()
                        self._auth_timeout_task = None
                    await self._finish_notification_setup()
                    await self.send(text_data=json.dumps({"type": "auth_ok"}))
                # Ignore all other messages until authenticated
                return

            # ---- Respond to keep-alive pings ----
            if msg_type == "ping":
                _touch_notification_presence(self.user.id)
                notification_socket_count, _app_state = _get_notification_socket_snapshot(self.user.id)
                await self._sync_notification_presence(notification_socket_count, touch=True)
                await self.send(text_data=json.dumps({"type": "pong"}))
                return

            # ---- App state change (active / background) ----
            if msg_type == "app_state":
                state = data.get("state", "active")  # "active" or "background"
                with _connected_lock:
                    entry = _connected_notification_users.get(self.user.id)
                    if entry:
                        entry["app_state"] = state
                        entry["last_seen_ts"] = time.time()
                notification_socket_count, _app_state = _get_notification_socket_snapshot(self.user.id)
                await self._sync_notification_presence(notification_socket_count, app_state=state, touch=True)
                is_online = state == "active"
                logger.info("[WS] app_state=%s user=%s → is_online=%s",
                            state, self.user.username, is_online)
                return

            # Any valid authenticated frame means this session is alive.
            _touch_notification_presence(self.user.id)

            # ---- Axion: shared chat-message relay ----
            # Chat rooms no longer create their own WebSocket. The single
            # authenticated notification gateway owns all realtime traffic and
            # validates membership before a message reaches any recipient.
            if msg_type == "send_message":
                room_id = str(data.get("room_id", ""))
                message_id = str(data.get("id", ""))
                content = data.get("message", "")
                message_type = str(data.get("message_type", "text"))
                created_at = data.get("created_at", timezone.now().isoformat())
                if not room_id or not message_id or not content:
                    return
                plan = await self.prepare_axion_message(room_id)
                if not plan:
                    await self.send(text_data=json.dumps({"type": "server_error", "op": "send_message"}))
                    return
                reserved = {"type", "room_id", "id", "message", "message_type", "created_at", "sender", "sender_id", "hydration"}
                payload = {
                    "event": "new_message",
                    "room_id": room_id,
                    "room_name": plan["room_name"],
                    "sender": self.user.username,
                    "sender_id": self.user.id,
                    "sender_avatar": plan["sender_avatar"],
                    "content": content,
                    "message_id": message_id,
                    "message_type": message_type,
                    "created_at": created_at,
                    "correlation_id": f"msg:{message_id}",
                    "route_reason": "axion",
                    # Axion is the interactive path.  FCM is scheduled only
                    # if this delivery remains unacknowledged, so a live app
                    # does not wait behind an unnecessary push request.
                    "push_floor": False,
                }
                payload.update({k: v for k, v in data.items() if k not in reserved and k not in payload})
                # Confirm acceptance before routing. A receiver that is in the
                # Android background has no live Axion socket, so sending an
                # empty channel-layer group is needless Redis work and must
                # never make the sender wait.
                await self.send(text_data=json.dumps({
                    "type": "message_server_ack",
                    "room_id": room_id,
                    "message_id": message_id,
                    "correlation_id": f"msg:{message_id}",
                }))
                # Mirror the control acknowledgement through the user's
                # notification group.  `self.send()` is the fast path, but a
                # mobile socket can lose that one direct frame while its
                # channel-layer subscription remains healthy.  The client
                # treats this idempotently, so the mirror makes acceptance
                # durable across that narrow race (and reaches another device
                # logged in as the sender as well).
                await self.channel_layer.group_send(
                    self.group_name,
                    {"type": "notify", "payload": {
                        "type": "message_server_ack",
                        "room_id": room_id,
                        "message_id": message_id,
                        "correlation_id": f"msg:{message_id}",
                    }},
                )
                logger.info("[Axion] accepted message_id=%s room=%s sender=%s", message_id, room_id, self.user.id)
                # This is metadata only (message id, room, sender, recipient
                # and delivery state).  It runs outside the acknowledgement
                # path and gives reconnects a correct delivery fallback if a
                # live receipt frame is missed.
                asyncio.create_task(self.record_axion_message_deliveries(
                    room_id, message_id, plan["recipient_ids"],
                ))
                # This metadata is only an offline wake-up hint; never allow a
                # contended database write (especially for a group) to delay
                # the sender's durable local-outbox acknowledgement.
                if plan["offline_email_recipient_ids"]:
                    asyncio.create_task(self.record_axion_pending_deliveries(
                        room_id,
                        plan["offline_email_recipient_ids"],
                    ))
                # Only live Axion recipients use the internal channel relay.
                # Background/killed recipients take the FCM path below.
                for member_id in plan["live_recipient_ids"]:
                    try:
                        await asyncio.wait_for(
                            self.channel_layer.group_send(
                                f"notifications_{member_id}",
                                {"type": "notify", "payload": payload},
                            ),
                            timeout=1.5,
                        )
                    except Exception:
                        logger.warning(
                            "[Axion] live relay unavailable room=%s recipient=%s",
                            room_id,
                            member_id,
                            exc_info=True,
                        )
                # Live delivery is peer/local-state driven, so do not create
                # per-message server records. A peer whose Android background
                # state has closed Axion gets a data push in a background task;
                # this persists the message without delaying the relay/ack.
                if plan["offline_email_recipient_ids"]:
                    asyncio.create_task(self.send_axion_message_push(
                        recipient_ids=plan["offline_email_recipient_ids"],
                        sender_name=self.user.username,
                        content=content,
                        room_id=room_id,
                        room_name=plan["room_name"],
                        message_id=message_id,
                        message_type=message_type,
                        created_at=created_at,
                        extra_data={k: v for k, v in data.items() if k not in reserved},
                    ))
                if plan["offline_email_recipient_ids"]:
                    asyncio.create_task(self.queue_offline_email_nudges(
                        room_id=room_id,
                        recipient_ids=plan["offline_email_recipient_ids"],
                    ))
                return

            # ---- Axion: room readiness lets peers flush durable outboxes ----
            if msg_type == "room_ready":
                room_id = str(data.get("room_id", ""))
                if not room_id:
                    return
                pending_senders = await self.get_pending_senders_for_room_notif(room_id)
                for sender_id in pending_senders:
                    await self.channel_layer.group_send(
                        f"notifications_{sender_id}",
                        {"type": "notify", "payload": {
                            "event": "receiver_ready", "room_id": room_id,
                            "user_id": self.user.id, "username": self.user.username,
                        }},
                    )
                return

            # ---- Axion: mutation and typing relays ----
            if msg_type == "message_update":
                room_id = str(data.get("room_id", ""))
                updates = data.get("updates", [])
                member_ids = await self.get_room_member_ids(room_id)
                if not room_id or not updates or self.user.id not in member_ids:
                    return
                peer_ids = [member_id for member_id in member_ids if member_id != self.user.id]
                await self.send(text_data=json.dumps({
                    "type": "message_update_server_ack", "room_id": room_id,
                    "updates": [{"id": update.get("id"), "expected_peer_ids": peer_ids}
                                for update in updates if isinstance(update, dict) and update.get("id")],
                }))
                for member_id in peer_ids:
                    await self.channel_layer.group_send(
                        f"notifications_{member_id}",
                        {"type": "notify", "payload": {
                            "event": "message_update", "room_id": room_id,
                            "updates": updates, "from_user_id": self.user.id,
                            "from_username": self.user.username,
                        }},
                    )
                return

            if msg_type == "typing":
                room_id = str(data.get("room_id", ""))
                member_ids = await self.get_room_member_ids(room_id)
                if not room_id or self.user.id not in member_ids:
                    return
                for member_id in member_ids:
                    if member_id == self.user.id:
                        continue
                    await self.channel_layer.group_send(
                        f"notifications_{member_id}",
                        {"type": "notify", "payload": {
                            "event": "typing", "room_id": room_id,
                            "sender_id": self.user.id, "sender": self.user.username,
                            "is_typing": bool(data.get("is_typing")),
                        }},
                    )
                return

            # ---- Message delivery ack (for users who received via notification WS) ----
            if msg_type == "message_ack":
                message_id = data.get("message_id", "")
                sender_id = data.get("sender_id")
                room_id = data.get("room_id", "")
                if not message_id or not sender_id or not room_id:
                    return
                acked = await self.validate_message_ack_notif(
                    message_id=message_id,
                    sender_id=int(sender_id),
                    room_id=str(room_id),
                )
                if not acked:
                    return
                logger.info(
                    "[DeliveryAck] notif room=%s message_id=%s sender=%s recipient=%s",
                    str(room_id),
                    message_id,
                    int(sender_id),
                    self.user.id,
                )
                await self.channel_layer.group_send(
                    f"notifications_{int(sender_id)}",
                    {"type": "notify", "payload": {
                        "event": "message_delivery_ack", "message_id": message_id,
                        "by_user_id": self.user.id, "by_username": self.user.username,
                        "room_id": room_id,
                    }},
                )
                return

            # ---- message_update_ack: recipient confirmed it applied update(s) ----
            if msg_type == "message_update_ack":
                update_ids = data.get("update_ids", [])
                sender_id = data.get("sender_id")
                room_id = data.get("room_id", "")
                if not update_ids or not sender_id or not room_id:
                    return
                await self.channel_layer.group_send(
                    f"notifications_{int(sender_id)}",
                    {"type": "notify", "payload": {
                        "event": "message_update_ack", "room_id": str(room_id),
                        "update_ids": update_ids, "by_user_id": self.user.id,
                        "by_username": self.user.username,
                    }},
                )
                return

            # ---- Call invite ack (callee confirms incoming_call reached app) ----
            if msg_type == "call_invite_ack":
                call_id = data.get("call_id", "")
                if not call_id:
                    return
                acked = await self.mark_call_invite_acked(call_id)
                if acked:
                    logger.info(
                        "[CallInviteAck] call_id=%s callee=%s",
                        call_id,
                        self.user.id,
                    )
                return

            # ---- Peer sync: relay request to another session of same user ----
            if msg_type == "request_sync":
                rooms_data = data.get("rooms", [])
                with _connected_lock:
                    entry = _connected_notification_users.get(self.user.id)
                    other_channels = (
                        [c for c in entry["channels"] if c != self.channel_name]
                        if entry else []
                    )
                if not other_channels:
                    return
                await self.channel_layer.send(
                    other_channels[0],
                    {
                        "type": "notify",
                        "payload": {
                            "event": "peer_sync_request",
                            "requester_channel": self.channel_name,
                            "rooms": rooms_data,
                        },
                    },
                )
                return

            # ---- Peer sync: relay sync_messages back to requesting session ----
            if msg_type == "sync_messages":
                target_channel = data.get("target_channel")
                if not target_channel:
                    return
                await self.channel_layer.send(
                    target_channel,
                    {
                        "type": "notify",
                        "payload": {
                            "event": "sync_messages",
                            "room_id": data.get("room_id"),
                            "messages": data.get("messages", []),
                        },
                    },
                )
                return

            # ---- RRP sync_digest: advertise our held message ids to room peers ----
            # The server is a dumb relay — it forwards the digest (ids only, no
            # content) to the OTHER members of the room so each can detect and
            # request gaps.
            if msg_type == "sync_digest":
                room_id = data.get("room_id", "")
                ids = data.get("ids", [])
                if not room_id or not ids:
                    return
                member_ids = await self.get_room_member_ids(room_id)
                for member_id in member_ids:
                    if member_id == self.user.id:
                        continue
                    for channel in get_user_notification_channels(member_id):
                        await self.channel_layer.send(
                            channel,
                            {
                                "type": "notify",
                                "payload": {
                                    "event": "sync_digest",
                                    "room_id": str(room_id),
                                    "ids": ids,
                                    "from_user_id": self.user.id,
                                    "from_username": self.user.username,
                                },
                            },
                        )
                return

            # ---- RRP sync_request: peer is missing ids → relay to room peers ----
            # Recipients re-send the requested messages via their existing outbox
            # flush path (ids + media preserved). Server stays a dumb relay.
            if msg_type == "sync_request":
                room_id = data.get("room_id", "")
                ids = data.get("ids", [])
                if not room_id or not ids:
                    return
                member_ids = await self.get_room_member_ids(room_id)
                for member_id in member_ids:
                    if member_id == self.user.id:
                        continue
                    for channel in get_user_notification_channels(member_id):
                        await self.channel_layer.send(
                            channel,
                            {
                                "type": "notify",
                                "payload": {
                                    "event": "sync_request",
                                    "room_id": str(room_id),
                                    "ids": ids,
                                    "from_user_id": self.user.id,
                                    "from_username": self.user.username,
                                },
                            },
                        )
                return

            # ---- WebRTC signaling ----
            if msg_type == "webrtc_signal":
                target_id = data.get("target_user_id")
                signal_type = data.get("signal_type", "unknown")
                if not target_id:
                    return

                target_group = f"notifications_{target_id}"

                # Forward the signal to the target user
                await self.channel_layer.group_send(
                    target_group,
                    {
                        "type": "notify",
                        "payload": {
                            "event": "webrtc_signal",
                            "signal_type": signal_type,
                            "data": data.get("data"),
                            "from_user_id": self.user.id,
                            "from_username": self.user.username,
                        },
                    },
                )

                # Send delivery acknowledgment back to sender
                await self.send(text_data=json.dumps({
                    "event": "signal_ack",
                    "signal_type": signal_type,
                    "target_user_id": target_id,
                }))
        except Exception:
            logger.exception(
                "[NotificationConsumer] unhandled receive error user=%s type=%s",
                getattr(self.user, "id", None),
                msg_type,
            )
            try:
                await self.send(text_data=json.dumps({"type": "server_error", "op": msg_type}))
            except Exception:
                pass

    async def notify(self, event):
        """Forward notification payload to client."""
        try:
            await self.send(text_data=json.dumps(event["payload"]))
        except Exception:
            logger.exception("[NotificationConsumer.notify] failed user=%s",
                             getattr(self.user, "id", None))

    @database_sync_to_async
    def _set_user_online(self, online: bool):
        """Update the is_online flag and last_seen in the database.

        Skip the UPDATE if the desired state already matches what's stored.
        This avoids a write storm during reconnect loops or rapid app_state
        toggles, both of which were observed to amplify connection instability.
        """
        current = User.objects.filter(id=self.user.id).values_list(
            "is_online", flat=True
        ).first()
        if current is None or current == online:
            # Even on a no-op, refresh last_seen so monitoring stays accurate,
            # but cap it to once every ~30s to avoid hot-path writes.
            from datetime import timedelta
            now = timezone.now()
            User.objects.filter(
                id=self.user.id,
                last_seen__lt=now - timedelta(seconds=30),
            ).update(last_seen=now)
            UserPresence.objects.update_or_create(
                user=self.user,
                defaults={"is_online": online, "last_seen": now},
            )
            return
        User.objects.filter(id=self.user.id).update(
            is_online=online,
            last_seen=timezone.now(),
        )
        UserPresence.objects.update_or_create(
            user=self.user,
            defaults={"is_online": online, "last_seen": timezone.now()},
        )

    @database_sync_to_async
    def _sync_notification_presence(self, notification_socket_count: int, app_state: str | None = None, touch: bool = False):
        now = timezone.now()
        presence, _ = UserPresence.objects.get_or_create(
            user=self.user,
            defaults={"last_seen": now},
        )
        next_app_state = app_state or presence.app_state or UserPresence.APP_STATE_UNKNOWN
        notification_connected = notification_socket_count > 0
        is_online = presence.chat_socket_connected or (
            notification_connected and next_app_state == UserPresence.APP_STATE_ACTIVE
        )
        updates = {
            "notification_socket_count": notification_socket_count,
            "notification_socket_connected": notification_connected,
            "is_online": is_online,
        }
        if touch:
            updates["last_notification_seen_at"] = now
            updates["last_seen"] = now
        if app_state is not None:
            updates["app_state"] = app_state
            updates["last_app_state_change_at"] = now
        UserPresence.objects.filter(pk=presence.pk).update(**updates)
        if touch or app_state is not None:
            User.objects.filter(id=self.user.id).update(is_online=is_online, last_seen=now)
        else:
            User.objects.filter(id=self.user.id).update(is_online=is_online)

    @database_sync_to_async
    def get_user_routing_state(self, user_id: int) -> dict:
        return get_user_routing_state(user_id)

    @database_sync_to_async
    def get_pending_deliveries(self) -> list[dict]:
        rows = PendingDelivery.objects.filter(
            to_user=self.user,
        ).select_related("from_user").values(
            "from_user__id", "from_user__username", "room_id"
        )
        return [
            {
                "from_user_id": r["from_user__id"],
                "from_username": r["from_user__username"],
                "room_id": str(r["room_id"]),
            }
            for r in rows
        ]

    @database_sync_to_async
    def delete_pending_delivery_notif(self, from_user_id: int, room_id: str) -> None:
        PendingDelivery.objects.filter(
            room_id=room_id,
            from_user_id=from_user_id,
            to_user=self.user,
        ).delete()

    @database_sync_to_async
    def get_room_member_ids(self, room_id: str) -> list[int]:
        """Member ids of a room the requesting user belongs to (RRP sync relay).

        Returns [] if the room doesn't exist or the user isn't a member, so a
        client can never fan a digest/request into a room they're not part of.
        """
        try:
            room = ChatRoom.objects.get(id=room_id)
        except ChatRoom.DoesNotExist:
            return []
        if not room.members.filter(id=self.user.id).exists():
            return []
        return list(room.members.values_list("id", flat=True))

    @database_sync_to_async
    def prepare_axion_message(self, room_id: str) -> dict | None:
        """Validate and plan an Axion relay with minimal server-side work.

        Messages live durably on phones. The backend verifies room membership
        and relays the frame; it only records a compact room-level pending hint
        for recipients that have no active Axion socket.
        """
        try:
            room = ChatRoom.objects.get(id=room_id)
        except ChatRoom.DoesNotExist:
            return None
        if not room.members.filter(id=self.user.id).exists():
            return None
        from users.models import BlockedUser
        blockers = set(
            BlockedUser.objects.filter(blocked=self.user).values_list("owner_id", flat=True)
        )
        recipient_ids = [
            member_id for member_id in room.members.exclude(id=self.user.id).values_list("id", flat=True)
            if member_id not in blockers
        ]
        offline_recipient_ids = [
            recipient_id for recipient_id in recipient_ids
            if not is_user_ws_connected(recipient_id)
        ]
        if room.room_type == ChatRoom.DIRECT and not room.name:
            room_name = self.user.username
        else:
            room_name = room.name or str(room.id)
        try:
            sender_avatar = self.user.avatar.url if self.user.avatar and self.user.avatar.name else None
        except Exception:
            sender_avatar = None
        return {
            "recipient_ids": recipient_ids,
            "live_recipient_ids": [
                recipient_id for recipient_id in recipient_ids
                if recipient_id not in offline_recipient_ids
            ],
            "room_name": room_name,
            "sender_avatar": sender_avatar,
            "offline_email_recipient_ids": offline_recipient_ids,
        }

    @database_sync_to_async
    def record_axion_pending_deliveries(
        self,
        room_id: str,
        recipient_ids: list[int],
    ) -> None:
        """Persist the compact offline hint outside Axion's acknowledgement path."""
        if not recipient_ids:
            return
        PendingDelivery.objects.bulk_create(
            [
                PendingDelivery(
                    room_id=room_id,
                    from_user_id=self.user.id,
                    to_user_id=recipient_id,
                )
                for recipient_id in recipient_ids
            ],
            ignore_conflicts=True,
        )

    @database_sync_to_async
    def record_axion_message_deliveries(
        self,
        room_id: str,
        message_id: str,
        recipient_ids: list[int],
    ) -> None:
        """Persist minimal delivery metadata without delaying the Axion relay.

        `ignore_conflicts` is intentional: an especially fast recipient can
        acknowledge before this task runs, in which case the ACK handler has
        already inserted the authoritative delivered row.
        """
        if not message_id or not recipient_ids:
            return
        MessageDelivery.objects.bulk_create(
            [
                MessageDelivery(
                    room_id=room_id,
                    message_id=message_id,
                    sender_id=self.user.id,
                    recipient_id=recipient_id,
                    status=MessageDelivery.STATUS_PENDING,
                )
                for recipient_id in recipient_ids
            ],
            ignore_conflicts=True,
        )

    async def queue_offline_email_nudges(self, room_id: str, recipient_ids: list[int]) -> None:
        """Reserve one cooldown slot per recipient, then send outside Axion's hot path."""
        recipients = await self.reserve_offline_email_nudges(room_id, recipient_ids)
        for recipient in recipients:
            # SMTP/HTTP email providers are blocking. Running them on Daphne's
            # event loop stalls every Axion heartbeat and relay on this worker.
            await asyncio.to_thread(
                _send_offline_message_email,
                recipient_email=recipient["email"],
                recipient_name=recipient["name"],
                sender_name=self.user.username,
            )

    @database_sync_to_async
    def reserve_offline_email_nudges(self, room_id: str, recipient_ids: list[int]) -> list[dict]:
        """Atomically reserve the 24-hour email cooldown before sending."""
        from datetime import timedelta
        from django.db import transaction
        from users.models import Contact, UserDevice

        now = timezone.now()
        cooldown = timedelta(hours=OFFLINE_EMAIL_COOLDOWN_HOURS)
        recipients: list[dict] = []
        with transaction.atomic():
            for recipient in User.objects.select_for_update().filter(id__in=recipient_ids):
                # Preserve the request-safety rule while keeping this check
                # out of Axion's live relay path.
                if not Contact.objects.filter(
                    owner_id=recipient.id, contact_id=self.user.id,
                ).exists():
                    continue
                # A logged-in device with a valid push endpoint is reachable
                # even though Android has suspended its WebSocket. It must get
                # the data push, not an unnecessary "please open the app" email.
                if UserDevice.objects.filter(
                    user_id=recipient.id,
                    is_active=True,
                    user__notif_messages_enabled=True,
                ).filter(
                    Q(expo_push_token__startswith="ExponentPushToken[")
                    | Q(expo_push_token__startswith="ExpoPushToken[")
                    | ~Q(fcm_token="")
                ).exists():
                    continue
                nudge, _created = OfflineEmailNudge.objects.select_for_update().get_or_create(
                    room_id=room_id,
                    sender_id=self.user.id,
                    recipient_id=recipient.id,
                )
                if nudge.last_sent_at and now - nudge.last_sent_at < cooldown:
                    continue
                nudge.last_sent_at = now
                nudge.save(update_fields=["last_sent_at", "updated_at"])
                recipients.append({
                    "email": recipient.email,
                    "name": recipient.display_name or recipient.username,
                })
        return recipients

    @database_sync_to_async
    def get_pending_senders_for_room_notif(self, room_id: str) -> list[int]:
        if not ChatRoom.objects.filter(id=room_id, members=self.user).exists():
            return []
        return list(
            PendingDelivery.objects.filter(room_id=room_id, to_user=self.user)
            .values_list("from_user_id", flat=True)
        )

    async def push_axion_fallback_after_timeout(
        self,
        recipient_ids: list[int],
        sender_name: str,
        content: str,
        room_id: str,
        room_name: str,
        message_id: str,
        message_type: str,
        created_at: str,
        extra_data: dict,
    ) -> None:
        """Legacy delayed fallback; retained for callers outside Axion relay."""
        await asyncio.sleep(MESSAGE_ACK_TIMEOUT_SECONDS)
        await self.send_axion_message_push(
            recipient_ids=recipient_ids,
            sender_name=sender_name,
            content=content,
            room_id=room_id,
            room_name=room_name,
            message_id=message_id,
            message_type=message_type,
            created_at=created_at,
            extra_data=extra_data,
        )

    @database_sync_to_async
    def send_axion_message_push(
        self,
        recipient_ids: list[int],
        sender_name: str,
        content: str,
        room_id: str,
        room_name: str,
        message_id: str,
        message_type: str,
        created_at: str,
        extra_data: dict,
    ) -> bool:
        if not recipient_ids:
            return False
        # Axion's lean relay intentionally does not persist MessageDelivery
        # rows. The old MessageDelivery-based fallback therefore selected no
        # recipients and backgrounded Android apps never received a push.
        # Callers supply only offline recipients, so send directly here.
        sent = send_message_push(
            recipient_ids=recipient_ids,
            sender_name=sender_name,
            content=content,
            room_id=room_id,
            room_name=room_name,
            correlation_id=f"msg:{message_id}",
            route_reason="axion_push_floor",
            message_id=message_id,
            sender_id=self.user.id,
            message_type=message_type,
            created_at=created_at,
            extra_data=extra_data,
        )
        return sent

    @database_sync_to_async
    def validate_message_ack_notif(self, message_id: str, sender_id: int, room_id: str) -> bool:
        """Validate and durably record a peer delivery tick.

        The row contains no message content.  It lets a sender reconcile a
        receipt after reconnecting, even if the realtime return frame was
        missed.
        """
        if not message_id:
            return False
        valid_members = ChatRoom.objects.filter(id=room_id, members=self.user).filter(
            members=sender_id,
        ).exists()
        if valid_members:
            delivery, created = MessageDelivery.objects.get_or_create(
                room_id=room_id,
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=self.user.id,
                defaults={
                    "status": MessageDelivery.STATUS_DELIVERED,
                    "delivered_at": timezone.now(),
                },
            )
            if not created and delivery.status != MessageDelivery.STATUS_DELIVERED:
                delivery.status = MessageDelivery.STATUS_DELIVERED
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=["status", "delivered_at"])
            # One room-level hint is enough: the sender's local outbox owns the
            # actual message history and will retry only what the peer lacks.
            PendingDelivery.objects.filter(
                room_id=room_id,
                from_user_id=sender_id,
                to_user_id=self.user.id,
            ).delete()
        return valid_members

    @database_sync_to_async
    def mark_call_invite_acked(self, call_id: str) -> bool:
        from calls.models import CallLog
        updated = CallLog.objects.filter(
            id=call_id,
            callee_id=self.user.id,
            invite_acked_at__isnull=True,
        ).update(invite_acked_at=timezone.now())
        return bool(updated)
