import asyncio
import json
import logging
import threading
from datetime import datetime

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from .models import ChatRoom, Message, PendingDelivery
from .push import send_message_push

User = get_user_model()
logger = logging.getLogger(__name__)

# ---- In-memory connected-user tracking ----
_connected_lock = threading.Lock()

# Notification WS connections
# { user_id: { "username": str, "connected_at": str, "channels": set[str], "app_state": str } }
_connected_notification_users: dict[int, dict] = {}

# Chat room WS connections
# { room_id: set[user_id] }
_connected_chat_users: dict[str, set[int]] = {}


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
    with _connected_lock:
        entry = _connected_notification_users.get(user_id)
        return entry is not None and entry.get("app_state") == "active"


def get_user_notification_channels(user_id: int) -> set[str]:
    """Return all notification WS channel names for a user (empty set if offline)."""
    with _connected_lock:
        entry = _connected_notification_users.get(user_id)
        return set(entry["channels"]) if entry else set()


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
        is_member = await self.is_room_member()
        if not is_member:
            await self.close(code=4003)
            return
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        if not self._accepted:
            await self.accept()
            self._accepted = True
        with _connected_lock:
            _connected_chat_users.setdefault(self.room_id, set()).add(self.user.id)

    async def _auth_timeout(self):
        """Close the connection if no auth message arrives within 5 seconds."""
        await asyncio.sleep(5.0)
        if self._awaiting_auth:
            logger.warning("[ChatConsumer] auth timeout — closing unauthenticated WS")
            await self.close(code=4001)

    async def disconnect(self, close_code):
        # Cancel pending auth timeout
        if hasattr(self, "_auth_timeout_task") and self._auth_timeout_task:
            self._auth_timeout_task.cancel()
            self._auth_timeout_task = None
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )
        # Untrack connected chat user
        if hasattr(self, "user") and not self.user.is_anonymous:
            with _connected_lock:
                room_users = _connected_chat_users.get(self.room_id)
                if room_users:
                    room_users.discard(self.user.id)

    async def receive(self, text_data):
        """Handle incoming messages from the WebSocket client."""
        data = json.loads(text_data)
        msg_type = data.get("type", "")

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
            # Update DB records if they exist (server-stored messages only)
            result = await self.mark_messages_read(message_ids)
            logger.info(
                "[ChatConsumer] DB updated %d messages as read",
                len(result["updated_ids"]),
            )
            # Messages sent via WS are NOT stored server-side, so use the
            # client-provided IDs directly rather than waiting for DB records.
            broadcast_ids = message_ids if message_ids else result["updated_ids"]
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

        # ---- message_ack: receiver confirmed it stored the message locally ----
        if msg_type == "message_ack":
            message_id = data.get("message_id", "")
            sender_id = data.get("sender_id")
            if not message_id or not sender_id:
                return
            await self.delete_pending_delivery(int(sender_id))
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
        message_content = data.get("message", "")
        message_type_str = data.get("message_type", "text")
        message_id = data.get("id", "")           # client-generated UUID
        created_at = data.get("created_at", timezone.now().isoformat())

        if not message_content or not message_id:
            return

        msg_data = {
            "id": message_id,
            "sender": self.user.username,
            "sender_id": self.user.id,
            "content": message_content,
            "message_type": message_type_str,
            "created_at": created_at,
        }

        # Broadcast to all users currently in this room's chat WS
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "message": msg_data,
            },
        )

        # For members NOT in the room chat WS:
        # relay via ALL their notification channels (multi-device),
        # or create a PendingDelivery + push if completely offline.
        room_info = await self.get_room_info()
        in_room_ids = get_connected_chat_user_ids(self.room_id)
        push_recipients: list[int] = []

        for member_id in room_info["member_ids"]:
            if member_id == self.user.id:
                continue
            if member_id in in_room_ids:
                continue  # already delivered via room group_send above

            member_channels = get_user_notification_channels(member_id)
            if member_channels:
                # Online — relay to ALL notification sessions (multi-device support)
                for channel in member_channels:
                    await self.channel_layer.send(
                        channel,
                        {
                            "type": "notify",
                            "payload": {
                                "event": "new_message",
                                "room_id": str(self.room_id),
                                "room_name": room_info["name"],
                                "sender": self.user.username,
                                "sender_id": self.user.id,
                                "content": message_content,
                                "message_id": message_id,
                                "message_type": message_type_str,
                                "created_at": created_at,
                            },
                        },
                    )
            else:
                # Offline — create PendingDelivery record and queue for push
                await self.create_pending_delivery(member_id)
                push_recipients.append(member_id)

        if push_recipients:
            logger.info(
                "[Push] Sending push to %d offline user(s) (of %d total recipients)",
                len(push_recipients), len(room_info["member_ids"]) - 1,
            )
            await self._send_message_push(
                recipient_ids=push_recipients,
                sender_name=self.user.username,
                content=message_content[:120],
                room_id=str(self.room_id),
                room_name=room_info["name"],
            )

        # Acknowledge to sender: server received and relayed this message
        await self.send(text_data=json.dumps({
            "type": "message_server_ack",
            "message_id": message_id,
        }))

    async def chat_message(self, event):
        """Send message to WebSocket client."""
        await self.send(text_data=json.dumps(event["message"]))

    async def messages_read(self, event):
        """Broadcast read receipt to WebSocket client."""
        await self.send(text_data=json.dumps({
            "type": "messages_read",
            "reader_id": event["reader_id"],
            "reader": event["reader"],
            "message_ids": event["message_ids"],
        }))

    async def message_update_broadcast(self, event):
        """Relay a message_update event to the WebSocket client. Skip the originating sender."""
        if event.get("sender_id") == self.user.id:
            return
        await self.send(text_data=json.dumps({
            "type": "message_update",
            "updates": event["updates"],
        }))

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
        User.objects.filter(id=self.user.id).update(is_online=online)

    @database_sync_to_async
    def _send_message_push(
        self, recipient_ids, sender_name, content, room_id, room_name
    ):
        """Send Expo push notification for a new message (runs in thread)."""
        send_message_push(
            recipient_ids=recipient_ids,
            sender_name=sender_name,
            content=content,
            room_id=room_id,
            room_name=room_name,
        )

    @database_sync_to_async
    def mark_messages_read(self, message_ids: list) -> dict:
        """
        Mark messages as read. Only marks messages NOT sent by the current user.
        Returns dict with updated message ID strings and unique sender IDs.
        """
        if not message_ids:
            qs = Message.objects.filter(
                room_id=self.room_id,
                is_read=False,
            ).exclude(sender=self.user)
        else:
            qs = Message.objects.filter(
                id__in=message_ids,
                room_id=self.room_id,
                is_read=False,
            ).exclude(sender=self.user)
        rows = list(qs.values_list("id", "sender_id"))
        ids = [str(r[0]) for r in rows]
        sender_ids = list({r[1] for r in rows})
        qs.update(is_read=True)
        return {"updated_ids": ids, "sender_ids": sender_ids}

    @database_sync_to_async
    def get_room_member_ids_except_self(self) -> list[int]:
        """Return IDs of all room members except the current user."""
        room = ChatRoom.objects.get(id=self.room_id)
        return list(room.members.exclude(id=self.user.id).values_list("id", flat=True))


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
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        if not self._accepted:
            await self.accept()
            self._accepted = True
        with _connected_lock:
            entry = _connected_notification_users.get(self.user.id)
            if entry:
                entry["channels"].add(self.channel_name)
            else:
                _connected_notification_users[self.user.id] = {
                    "username": self.user.username,
                    "connected_at": datetime.utcnow().isoformat(),
                    "channels": {self.channel_name},
                    "app_state": "active",
                }
        await self._set_user_online(True)

        # Notify client of any messages waiting for delivery from offline senders
        pending = await self.get_pending_deliveries()
        if pending:
            await self.send(text_data=json.dumps({
                "type": "pending_deliveries",
                "deliveries": pending,
            }))

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
        """Close the connection if no auth message arrives within 5 seconds."""
        await asyncio.sleep(5.0)
        if self._awaiting_auth:
            logger.warning("[NotificationConsumer] auth timeout — closing unauthenticated WS")
            await self.close(code=4001)

    async def disconnect(self, close_code):
        # Cancel pending auth timeout
        if hasattr(self, "_auth_timeout_task") and self._auth_timeout_task:
            self._auth_timeout_task.cancel()
            self._auth_timeout_task = None
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name, self.channel_name
            )
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
            if not still_connected:
                await self._set_user_online(False)
            logger.info("[WS] Notification disconnected: user=%s (still_connected=%s)",
                        self.user.username, still_connected)

    async def receive(self, text_data):
        data = json.loads(text_data)
        msg_type = data.get("type")

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
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        # ---- App state change (active / background) ----
        if msg_type == "app_state":
            state = data.get("state", "active")  # "active" or "background"
            with _connected_lock:
                entry = _connected_notification_users.get(self.user.id)
                if entry:
                    entry["app_state"] = state
            is_online = state == "active"
            await self._set_user_online(is_online)
            logger.info("[WS] app_state=%s user=%s → is_online=%s",
                        state, self.user.username, is_online)
            return

        # ---- Message delivery ack (for users who received via notification WS) ----
        if msg_type == "message_ack":
            message_id = data.get("message_id", "")
            sender_id = data.get("sender_id")
            room_id = data.get("room_id", "")
            if not message_id or not sender_id or not room_id:
                return
            await self.delete_pending_delivery_notif(int(sender_id), room_id)
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

    async def notify(self, event):
        """Forward notification payload to client."""
        await self.send(text_data=json.dumps(event["payload"]))

    @database_sync_to_async
    def _set_user_online(self, online: bool):
        """Update the is_online flag and last_seen in the database."""
        User.objects.filter(id=self.user.id).update(
            is_online=online,
            last_seen=timezone.now(),
        )

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
