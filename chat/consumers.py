import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model

from .models import ChatRoom, Message

User = get_user_model()


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

        # Mark user as online
        await self.set_user_online(True)

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )
        if hasattr(self, "user") and not self.user.is_anonymous:
            await self.set_user_online(False)

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
            updated_ids = await self.mark_messages_read(message_ids)
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
        for member_id in room_info["member_ids"]:
            if member_id == self.user.id:
                continue
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
        """Return room name and member IDs for notification dispatch."""
        room = ChatRoom.objects.get(id=self.room_id)
        member_ids = list(room.members.values_list("id", flat=True))
        return {"name": room.name or str(room.id), "member_ids": member_ids}

    @database_sync_to_async
    def set_user_online(self, online: bool):
        User.objects.filter(id=self.user.id).update(is_online=online)

    @database_sync_to_async
    def mark_messages_read(self, message_ids: list) -> list:
        """
        Mark messages as read. Only marks messages NOT sent by the current user.
        Returns list of message ID strings that were actually updated.
        """
        if not message_ids:
            # If no specific IDs, mark all unread messages in the room from other users
            qs = Message.objects.filter(
                room_id=self.room_id,
                is_read=False,
            ).exclude(sender=self.user)
            ids = list(qs.values_list("id", flat=True))
            qs.update(is_read=True)
            return [str(mid) for mid in ids]
        else:
            qs = Message.objects.filter(
                id__in=message_ids,
                room_id=self.room_id,
                is_read=False,
            ).exclude(sender=self.user)
            ids = list(qs.values_list("id", flat=True))
            qs.update(is_read=True)
            return [str(mid) for mid in ids]


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    Per-user WebSocket for push-style notifications (incoming calls, etc.)
    and WebRTC signaling (offer / answer / ICE candidates).

    Connect:  ws://<host>/ws/notifications/?token=<jwt>
    """

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.group_name = f"notifications_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name, self.channel_name
            )

    async def receive(self, text_data):
        """
        Handle WebRTC signaling messages from the client.
        Expected format:
        {
            "type": "webrtc_signal",
            "target_user_id": <int>,
            "signal_type": "offer" | "answer" | "ice-candidate",
            "data": { ... SDP or ICE candidate ... }
        }
        """
        data = json.loads(text_data)
        msg_type = data.get("type")

        # ---- Respond to keep-alive pings ----
        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

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
