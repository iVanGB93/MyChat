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

from .models import ChatRoom, PendingDelivery, MessageDelivery
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
WS_AUTH_TIMEOUT_SECONDS = 10.0
MESSAGE_ACK_TIMEOUT_SECONDS = int(getattr(settings, "MESSAGE_ACK_TIMEOUT_SECONDS", 8))
PRESENCE_STALE_SECONDS = int(getattr(settings, "PRESENCE_STALE_SECONDS", 70))


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
        user__profile__notif_messages_enabled=True,
    ).filter(
        Q(expo_push_token__startswith="ExponentPushToken[")
        | Q(expo_push_token__startswith="ExpoPushToken[")
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
        "is_online": presence.is_online,
        "is_stale": is_stale,
    }


def decide_message_notification_route(room_id: str, routing_state: dict, in_room_via_chat_ws: bool, has_notification_channels: bool) -> str:
    if (
        routing_state["presence"] == "active"
        and in_room_via_chat_ws
        and routing_state["chat_ws_connected"]
        and routing_state["active_room_id"] == str(room_id)
    ):
        return "room_ws_active"
    # Background sessions are not reliable for user-visible alerts on mobile.
    # Prefer remote push so off-screen users still get an OS-level notification.
    if routing_state["presence"] == "background":
        if routing_state["push_available"]:
            return "push_initial"
        if has_notification_channels and routing_state["notification_ws_connected"] and not routing_state["is_stale"]:
            return "notif_ws"
        return "pending_only"
    if has_notification_channels and routing_state["notification_ws_connected"] and not routing_state["is_stale"]:
        return "notif_ws"
    if routing_state["push_available"]:
        return "push_initial"
    return "pending_only"


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
                    member_ids = await self.get_room_member_ids_except_self()
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
                         "sender", "sender_id"}
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

                if route == "notif_ws":
                    await self.mark_message_delivery_routed(
                        message_id=message_id,
                        recipient_id=member_id,
                        routed_via=MessageDelivery.ROUTE_NOTIF_WS,
                    )
                    record_notification_decision(
                        kind="message",
                        route=route,
                        correlation_id=f"msg:{message_id}",
                        route_reason="notification_ws_available",
                        sender_id=self.user.id,
                        recipient_id=member_id,
                        room_id=str(self.room_id),
                        routing_state=routing_state,
                    )
                    logger.info(
                        "[NotifyDecision] kind=message route=notif_ws room=%s message_id=%s sender=%s recipient=%s state=%s channels=%d",
                        str(self.room_id),
                        message_id,
                        self.user.id,
                        member_id,
                        routing_state,
                        len(member_channels),
                    )
                    # Online — relay to ALL notification sessions (multi-device support).
                    # Pass through every field from msg_data so the apps see the
                    # exact same payload they'd get on the room socket.
                    notify_payload = {
                        "event": "new_message",
                        "room_id": str(self.room_id),
                        "room_name": room_info["name"],
                        "sender": self.user.username,
                        "sender_id": self.user.id,
                        "content": message_content,
                        "message_id": message_id,
                        "message_type": message_type_str,
                        "created_at": created_at,
                        "correlation_id": f"msg:{message_id}",
                        "route_reason": "notif_ws",
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
                elif route == "push_initial":
                    # Offline but push-capable — keep a pending marker and queue push.
                    await self.mark_message_delivery_routed(
                        message_id=message_id,
                        recipient_id=member_id,
                        routed_via=MessageDelivery.ROUTE_PUSH,
                    )
                    record_notification_decision(
                        kind="message",
                        route=route,
                        correlation_id=f"msg:{message_id}",
                        route_reason="push_available_ws_unavailable",
                        sender_id=self.user.id,
                        recipient_id=member_id,
                        room_id=str(self.room_id),
                        routing_state=routing_state,
                    )
                    logger.info(
                        "[NotifyDecision] kind=message route=push_initial room=%s message_id=%s sender=%s recipient=%s state=%s",
                        str(self.room_id),
                        message_id,
                        self.user.id,
                        member_id,
                        routing_state,
                    )
                    push_recipients.append(member_id)
                else:
                    await self.mark_message_delivery_routed(
                        message_id=message_id,
                        recipient_id=member_id,
                        routed_via=MessageDelivery.ROUTE_PENDING_ONLY,
                    )
                    record_notification_decision(
                        kind="message",
                        route=route,
                        correlation_id=f"msg:{message_id}",
                        route_reason="no_push_endpoint_available",
                        sender_id=self.user.id,
                        recipient_id=member_id,
                        room_id=str(self.room_id),
                        routing_state=routing_state,
                    )
                    logger.info(
                        "[NotifyDecision] kind=message route=pending_only room=%s message_id=%s sender=%s recipient=%s state=%s",
                        str(self.room_id),
                        message_id,
                        self.user.id,
                        member_id,
                        routing_state,
                    )

            if push_recipients:
                logger.info(
                    "[Push] Sending push to %d offline user(s) (of %d total recipients)",
                    len(push_recipients), len(room_info["member_ids"]) - 1,
                )
                sent = await self._send_message_push(
                    recipient_ids=push_recipients,
                    sender_name=self.user.username,
                    content="New message waiting",
                    room_id=str(self.room_id),
                    room_name=room_info["name"],
                    correlation_id=f"msg:{message_id}",
                    route_reason="push_initial",
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
            if sender_id != self.user.id \
                    and not await self._is_user_online_async(self.user.id):
                return
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
        self, recipient_ids, sender_name, content, room_id, room_name, correlation_id=None, route_reason=None
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
        )

    async def push_fallback_after_timeout(self, message_id: str, sender_name: str, room_name: str):
        await asyncio.sleep(MESSAGE_ACK_TIMEOUT_SECONDS)
        pending_recipient_ids = await self.get_pending_recipients_for_message(message_id)
        if not pending_recipient_ids:
            return
        sent = await self._send_message_push(
            recipient_ids=pending_recipient_ids,
            sender_name=sender_name,
            content="New message waiting",
            room_id=str(self.room_id),
            room_name=room_name,
            correlation_id=f"msg:{message_id}",
            route_reason="push_fallback_timeout",
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

            # ---- Message delivery ack (for users who received via notification WS) ----
            if msg_type == "message_ack":
                message_id = data.get("message_id", "")
                sender_id = data.get("sender_id")
                room_id = data.get("room_id", "")
                if not message_id or not sender_id or not room_id:
                    return
                acked = await self.mark_message_delivery_acked_notif(
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
                                "room_id": room_id,
                            },
                        },
                    )
                return

            # ---- message_update_ack: recipient confirmed it applied update(s) ----
            if msg_type == "message_update_ack":
                update_ids = data.get("update_ids", [])
                sender_id = data.get("sender_id")
                room_id = data.get("room_id", "")
                if not update_ids or not sender_id or not room_id:
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
    def mark_message_delivery_acked_notif(self, message_id: str, sender_id: int, room_id: str) -> bool:
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
    def mark_call_invite_acked(self, call_id: str) -> bool:
        from calls.models import CallLog
        updated = CallLog.objects.filter(
            id=call_id,
            callee_id=self.user.id,
            invite_acked_at__isnull=True,
        ).update(invite_acked_at=timezone.now())
        return bool(updated)
