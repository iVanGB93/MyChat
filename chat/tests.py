import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from config.asgi import application

from .consumers import (
    NotificationConsumer,
    _connected_lock,
    _connected_notification_users,
)
from .models import ChatRoom, MessageDelivery, PendingDelivery
from .serializers import MemberSerializer
from users.models import UserPresence, UserPresenceSession
from users.presence import aggregate_user_presence


User = get_user_model()


class AxionMessageLifecycleTests(TransactionTestCase):
    """Exercise Axion through the real ASGI routing and notification consumer."""

    reset_sequences = True

    def setUp(self):
        self.sender = User.objects.create_user(
            username="sender",
            email="sender@example.com",
            password="test-password",
        )
        self.recipient = User.objects.create_user(
            username="recipient",
            email="recipient@example.com",
            password="test-password",
        )
        self.group_member = User.objects.create_user(
            username="group-member",
            email="group@example.com",
            password="test-password",
        )
        with _connected_lock:
            _connected_notification_users.clear()

    def tearDown(self):
        with _connected_lock:
            _connected_notification_users.clear()

    def _create_room(self, room_type=ChatRoom.DIRECT, *, include_group_member=False):
        room = ChatRoom.objects.create(
            room_type=room_type,
            name="Axion group" if room_type == ChatRoom.GROUP else "",
        )
        members = [self.sender, self.recipient]
        if include_group_member:
            members.append(self.group_member)
        room.members.set(members)
        return room

    @staticmethod
    def _token_for(user):
        token = AccessToken.for_user(user)
        token["tv"] = user.token_version
        return str(token)

    async def _connect(self, user, *, report_active=True):
        token = self._token_for(user)
        communicator = WebsocketCommunicator(
            application,
            f"/ws/notifications/?token={token}",
        )
        connected, _subprotocol = await communicator.connect(timeout=2)
        self.assertTrue(connected)
        if report_active:
            # A physical socket starts as ``unknown`` until the app reports its
            # lifecycle. Tests that model an open app explicitly complete that
            # handshake before expecting foreground Axion delivery.
            await communicator.send_json_to({"type": "app_state", "state": "active"})
            await communicator.send_json_to({"type": "ping"})
            await self._receive_until(
                communicator,
                lambda frame: frame.get("type") == "pong",
            )
        return communicator

    async def _receive_until(self, communicator, predicate, *, attempts=8):
        received = []
        for _ in range(attempts):
            payload = await communicator.receive_json_from(timeout=2)
            received.append(payload)
            if predicate(payload):
                return payload
        self.fail(f"Expected WebSocket frame was not received. Frames: {received!r}")

    @staticmethod
    async def _disconnect_all(*communicators):
        for communicator in communicators:
            if communicator is None:
                continue
            try:
                await communicator.disconnect(timeout=1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    @staticmethod
    async def _wait_for_database(predicate, *, attempts=20):
        for _ in range(attempts):
            if await database_sync_to_async(predicate)():
                return
            await asyncio.sleep(0.025)
        raise AssertionError("Timed out waiting for background delivery metadata")

    @staticmethod
    def _message_frame(room, message_id, content="Hello over Axion"):
        return {
            "type": "send_message",
            "room_id": str(room.id),
            "id": message_id,
            "message": content,
            "message_type": "text",
            "created_at": "2026-08-25T12:00:00Z",
        }

    def test_direct_message_is_accepted_relayed_and_acknowledged(self):
        room = self._create_room()

        async def scenario():
            sender_socket = recipient_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                recipient_socket = await self._connect(self.recipient)

                await sender_socket.send_json_to(
                    self._message_frame(room, "direct-message-1")
                )

                server_ack = await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("type") == "message_server_ack",
                )
                self.assertEqual(server_ack["message_id"], "direct-message-1")
                self.assertEqual(server_ack["room_id"], str(room.id))

                incoming = await self._receive_until(
                    recipient_socket,
                    lambda frame: frame.get("event") == "new_message",
                )
                self.assertEqual(incoming["message_id"], "direct-message-1")
                self.assertEqual(incoming["sender_id"], self.sender.id)
                self.assertEqual(incoming["content"], "Hello over Axion")
                self.assertEqual(incoming["route_reason"], "axion")

                await recipient_socket.send_json_to({
                    "type": "message_ack",
                    "message_id": "direct-message-1",
                    "sender_id": self.sender.id,
                    "room_id": str(room.id),
                })
                delivery_ack = await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("event") == "message_delivery_ack",
                )
                self.assertEqual(delivery_ack["message_id"], "direct-message-1")
                self.assertEqual(delivery_ack["by_user_id"], self.recipient.id)

                await self._wait_for_database(
                    lambda: MessageDelivery.objects.filter(
                        message_id="direct-message-1",
                        recipient=self.recipient,
                        status=MessageDelivery.STATUS_DELIVERED,
                    ).exists()
                )
            finally:
                await self._disconnect_all(sender_socket, recipient_socket)

        async_to_sync(scenario)()

        delivery = MessageDelivery.objects.get(
            message_id="direct-message-1",
            recipient=self.recipient,
        )
        self.assertEqual(delivery.sender, self.sender)
        self.assertEqual(delivery.room, room)
        self.assertIsNotNone(delivery.delivered_at)
        self.assertFalse(
            PendingDelivery.objects.filter(
                room=room,
                from_user=self.sender,
                to_user=self.recipient,
            ).exists()
        )

    def test_group_message_reaches_each_active_member_once(self):
        room = self._create_room(
            ChatRoom.GROUP,
            include_group_member=True,
        )

        async def scenario():
            sender_socket = first_socket = second_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                first_socket = await self._connect(self.recipient)
                second_socket = await self._connect(self.group_member)

                await sender_socket.send_json_to(
                    self._message_frame(room, "group-message-1", "Hello group")
                )

                await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("type") == "message_server_ack",
                )
                first = await self._receive_until(
                    first_socket,
                    lambda frame: frame.get("event") == "new_message",
                )
                second = await self._receive_until(
                    second_socket,
                    lambda frame: frame.get("event") == "new_message",
                )

                for incoming in (first, second):
                    self.assertEqual(incoming["message_id"], "group-message-1")
                    self.assertEqual(incoming["room_name"], "Axion group")
                    self.assertEqual(incoming["content"], "Hello group")

                self.assertTrue(await first_socket.receive_nothing(timeout=0.1))
                self.assertTrue(await second_socket.receive_nothing(timeout=0.1))

                await self._wait_for_database(
                    lambda: MessageDelivery.objects.filter(
                        message_id="group-message-1",
                    ).count() == 2
                )
            finally:
                await self._disconnect_all(
                    sender_socket,
                    first_socket,
                    second_socket,
                )

        async_to_sync(scenario)()

        self.assertSetEqual(
            set(
                MessageDelivery.objects.filter(message_id="group-message-1")
                .values_list("recipient_id", flat=True)
            ),
            {self.recipient.id, self.group_member.id},
        )

    def test_offline_recipient_gets_pending_hint_then_can_ack(self):
        room = self._create_room()

        async def scenario():
            sender_socket = recipient_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                await sender_socket.send_json_to(
                    self._message_frame(room, "offline-message-1")
                )
                await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("type") == "message_server_ack",
                )

                await self._wait_for_database(
                    lambda: PendingDelivery.objects.filter(
                        room=room,
                        from_user=self.sender,
                        to_user=self.recipient,
                    ).exists()
                )

                recipient_socket = await self._connect(self.recipient, report_active=False)
                pending = await self._receive_until(
                    recipient_socket,
                    lambda frame: frame.get("type") == "pending_deliveries",
                )
                self.assertEqual(len(pending["deliveries"]), 1)
                self.assertEqual(
                    pending["deliveries"][0],
                    {
                        "from_user_id": self.sender.id,
                        "from_username": self.sender.username,
                        "room_id": str(room.id),
                    },
                )

                await recipient_socket.send_json_to({
                    "type": "room_ready",
                    "room_id": str(room.id),
                })
                ready = await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("event") == "receiver_ready",
                )
                self.assertEqual(ready["user_id"], self.recipient.id)

                await recipient_socket.send_json_to({
                    "type": "message_ack",
                    "message_id": "offline-message-1",
                    "sender_id": self.sender.id,
                    "room_id": str(room.id),
                })
                await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("event") == "message_delivery_ack",
                )
                await self._wait_for_database(
                    lambda: not PendingDelivery.objects.filter(
                        room=room,
                        from_user=self.sender,
                        to_user=self.recipient,
                    ).exists()
                )
            finally:
                await self._disconnect_all(sender_socket, recipient_socket)

        with patch.object(
            NotificationConsumer,
            "send_axion_message_push",
            new=AsyncMock(return_value=True),
        ), patch.object(
            NotificationConsumer,
            "queue_offline_email_nudges",
            new=AsyncMock(return_value=None),
        ):
            async_to_sync(scenario)()

        delivery = MessageDelivery.objects.get(
            message_id="offline-message-1",
            recipient=self.recipient,
        )
        self.assertEqual(delivery.status, MessageDelivery.STATUS_DELIVERED)

    def test_duplicate_message_id_creates_one_delivery_record_per_recipient(self):
        room = self._create_room()

        async def scenario():
            sender_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                frame = self._message_frame(room, "duplicate-message-1")
                await sender_socket.send_json_to(frame)
                await self._receive_until(
                    sender_socket,
                    lambda item: item.get("type") == "message_server_ack",
                )
                await sender_socket.send_json_to(frame)
                await self._receive_until(
                    sender_socket,
                    lambda item: item.get("type") == "message_server_ack",
                )

                await self._wait_for_database(
                    lambda: MessageDelivery.objects.filter(
                        message_id="duplicate-message-1",
                        recipient=self.recipient,
                    ).exists()
                )
                await asyncio.sleep(0.05)
            finally:
                await self._disconnect_all(sender_socket)

        with patch.object(
            NotificationConsumer,
            "send_axion_message_push",
            new=AsyncMock(return_value=True),
        ), patch.object(
            NotificationConsumer,
            "queue_offline_email_nudges",
            new=AsyncMock(return_value=None),
        ):
            async_to_sync(scenario)()

        self.assertEqual(
            MessageDelivery.objects.filter(
                message_id="duplicate-message-1",
                recipient=self.recipient,
            ).count(),
            1,
        )
        self.assertEqual(
            PendingDelivery.objects.filter(
                room=room,
                from_user=self.sender,
                to_user=self.recipient,
            ).count(),
            1,
        )

    def test_stale_persisted_online_flag_is_not_serialized_as_online(self):
        presence = self.sender.presence
        presence.is_online = True
        presence.notification_socket_connected = True
        presence.app_state = UserPresence.APP_STATE_ACTIVE
        presence.last_notification_seen_at = timezone.now() - timedelta(minutes=5)
        # A legacy/web room socket may still touch the general timestamp; it
        # must not make a dead Axion session appear online.
        presence.last_seen = timezone.now()
        presence.save()

        self.sender.refresh_from_db()
        self.assertFalse(MemberSerializer(self.sender).data["is_online"])

    def test_presence_aggregates_all_fresh_axion_sessions(self):
        now = timezone.now()
        UserPresenceSession.objects.create(
            user=self.sender,
            connection_id="active-phone",
            app_state=UserPresence.APP_STATE_ACTIVE,
            last_seen=now,
        )
        background = UserPresenceSession.objects.create(
            user=self.sender,
            connection_id="background-phone",
            app_state=UserPresence.APP_STATE_BACKGROUND,
            last_seen=now,
        )

        payload, _changed = aggregate_user_presence(self.sender.id)
        self.assertTrue(payload["is_online"])
        self.assertEqual(payload["presence"], "active")

        UserPresenceSession.objects.filter(connection_id="active-phone").delete()
        background.refresh_from_db()
        payload, _changed = aggregate_user_presence(self.sender.id)
        self.assertFalse(payload["is_online"])
        self.assertEqual(payload["presence"], "background")

    def test_presence_update_reaches_an_authorized_room_peer(self):
        self._create_room()

        async def scenario():
            sender_socket = recipient_socket = None
            try:
                recipient_socket = await self._connect(self.recipient)
                sender_socket = await self._connect(self.sender)
                await sender_socket.send_json_to({"type": "app_state", "state": "active"})

                update = await self._receive_until(
                    recipient_socket,
                    lambda frame: (
                        frame.get("event") == "presence_update"
                        and frame.get("user_id") == self.sender.id
                        and frame.get("presence") == "active"
                    ),
                )
                self.assertTrue(update["is_online"])
                self.assertGreater(update["expires_in"], 0)
            finally:
                await self._disconnect_all(sender_socket, recipient_socket)

        async_to_sync(scenario)()

    def test_post_connect_auth_ack_does_not_wait_for_presence_bootstrap(self):
        async def scenario():
            bootstrap_release = asyncio.Event()

            async def slow_presence_bootstrap(_consumer, touch_when_empty=False):
                await bootstrap_release.wait()
                return ({
                    "user_id": self.sender.id,
                    "is_online": False,
                    "presence": "background",
                    "last_seen": None,
                    "expires_in": 70,
                }, False)

            with patch.object(
                NotificationConsumer,
                "_sync_notification_presence",
                new=slow_presence_bootstrap,
            ):
                socket = WebsocketCommunicator(application, "/ws/notifications/")
                try:
                    connected, _subprotocol = await socket.connect(timeout=2)
                    self.assertTrue(connected)
                    await socket.send_json_to({
                        "type": "auth",
                        "token": self._token_for(self.sender),
                    })
                    ack = await socket.receive_json_from(timeout=2)
                    self.assertEqual(ack["type"], "auth_ok")
                    bootstrap_release.set()
                    await self._receive_until(
                        socket,
                        lambda frame: frame.get("event") == "presence_snapshot",
                    )
                finally:
                    bootstrap_release.set()
                    await self._disconnect_all(socket)

        async_to_sync(scenario)()
