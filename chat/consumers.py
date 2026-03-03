import json
import logging
import threading
from datetime import datetime

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import ChatRoom, Message
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

        if self.user.is_anonymous:
            await self.close()
            return

        # Verify user is a member of the room
        is_member = await self.is_room_member()
        if not is_member:
            await self.close()
            return

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name, self.channel_name
        )
        await self.accept()

        # Track connected chat user
        with _connected_lock:
            _connected_chat_users.setdefault(self.room_id, set()).add(self.user.id)

    async def disconnect(self, close_code):
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
            result = await self.mark_messages_read(message_ids)
            updated_ids = result["updated_ids"]
            sender_ids = result["sender_ids"]
            logger.info(
                "[ChatConsumer] marked %d messages as read, senders=%s",
                len(updated_ids), sender_ids,
            )
            if updated_ids:
                # Broadcast read receipt to the room so sender sees ✓✓
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "messages_read",
                        "reader_id": self.user.id,
                        "reader": self.user.username,
                        "message_ids": updated_ids,
                    },
                )
                # Also notify each sender via their notification channel
                # so they get the update even if they left the chat room
                for sender_id in sender_ids:
                    await self.channel_layer.group_send(
                        f"notifications_{sender_id}",
                        {
                            "type": "notify",
                            "payload": {
                                "event": "messages_read",
                                "room_id": str(self.room_id),
                                "reader_id": self.user.id,
                                "reader": self.user.username,
                                "message_ids": updated_ids,
                            },
                        },
                    )
            return

        message_content = data.get("message", "")
        message_type = data.get("message_type", "text")

        if not message_content:
            return

        # Persist message to database
        message = await self.save_message(message_content, message_type)

        msg_data = {
            "id": str(message.id),
            "sender": self.user.username,
            "sender_id": self.user.id,
            "content": message_content,
            "message_type": message_type,
            "created_at": message.created_at.isoformat(),
        }

        # Broadcast to room group (for users with the chat open)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "message": msg_data,
            },
        )

        # Also notify each room member via their personal notification channel
        # so users NOT viewing this room still see a toast / badge
        room_info = await self.get_room_info()
        recipient_ids = []
        for member_id in room_info["member_ids"]:
            if member_id == self.user.id:
                continue
            recipient_ids.append(member_id)
            await self.channel_layer.group_send(
                f"notifications_{member_id}",
                {
                    "type": "notify",
                    "payload": {
                        "event": "new_message",
                        "room_id": str(self.room_id),
                        "room_name": room_info["name"],
                        "sender": self.user.username,
                        "sender_id": self.user.id,
                        "content": message_content[:120],
                        "message_id": str(message.id),
                        "created_at": message.created_at.isoformat(),
                    },
                },
            )

        # Send push notification to users with NO active WebSocket connection.
        # - Not connected via WS → app is closed/killed → send FCM push
        # - Connected via WS (foreground or background) → WS delivers the message;
        #   the client shows a local notification if the app is in background.
        #   Sending FCM here too would cause duplicate notifications.
        push_recipients = [
            uid for uid in recipient_ids if not is_user_ws_connected(uid)
        ]
        if push_recipients:
            logger.info(
                "[Push] Sending push to %d user(s) not in foreground (of %d total recipients)",
                len(push_recipients), len(recipient_ids),
            )
            await self._send_message_push(
                recipient_ids=push_recipients,
                sender_name=self.user.username,
                content=message_content[:120],
                room_id=str(self.room_id),
                room_name=room_info["name"],
            )
        else:
            logger.info(
                "[Push] All %d recipient(s) in foreground — skipping push",
                len(recipient_ids),
            )

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

    # --- Database helpers (run in thread) ---

    @database_sync_to_async
    def is_room_member(self) -> bool:
        return ChatRoom.objects.filter(
            id=self.room_id, members=self.user
        ).exists()

    @database_sync_to_async
    def save_message(self, content: str, message_type: str) -> Message:
        return Message.objects.create(
            room_id=self.room_id,
            sender=self.user,
            content=content,
            message_type=message_type,
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


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    Per-user WebSocket for push-style notifications (incoming calls, etc.)
    and WebRTC signaling (offer / answer / ICE candidates).

    Connect:  ws://<host>/ws/notifications/?token=<jwt>

    The client sends {"type": "app_state", "state": "active"|"background"}
    to report whether the app is in the foreground.
        - active   → user is "online" (app open, viewing the screen)
        - background → user is "connected" (WS alive, but app not in foreground)
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.group_name = f"notifications_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Track connected notification user (default app_state = "active")
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
        # Mark user as online in DB
        await self._set_user_online(True)
        logger.info("[WS] Notification connected: user=%s", self.user.username)

    async def disconnect(self, close_code):
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
