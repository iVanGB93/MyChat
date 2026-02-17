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
        message_content = data.get("message", "")
        message_type = data.get("message_type", "text")

        if not message_content:
            return

        # Persist message to database
        message = await self.save_message(message_content, message_type)

        # Broadcast to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "chat_message",
                "message": {
                    "id": str(message.id),
                    "sender": self.user.username,
                    "sender_id": self.user.id,
                    "content": message_content,
                    "message_type": message_type,
                    "created_at": message.created_at.isoformat(),
                },
            },
        )

    async def chat_message(self, event):
        """Send message to WebSocket client."""
        await self.send(text_data=json.dumps(event["message"]))

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
    def set_user_online(self, online: bool):
        User.objects.filter(id=self.user.id).update(is_online=online)


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
