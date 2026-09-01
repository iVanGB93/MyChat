import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from config.asgi import application

from .consumers import (
    NotificationConsumer,
    _connected_lock,
    _connected_notification_users,
)
from .models import ChatRoom, MediaBlob, MessageDelivery, PendingDelivery
from .tasks import (
    cleanup_expired_media,
    cleanup_message_delivery_metadata,
    sweep_stale_message_deliveries,
)
from .serializers import MemberSerializer
from users.models import UserPresence, UserPresenceSession
from users.presence import aggregate_user_presence


User = get_user_model()


class MediaUploadReliabilityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.sender = User.objects.create_user(
            username="media-sender",
            email="media-sender@example.com",
            password="test-password",
        )
        recipient = User.objects.create_user(
            username="media-recipient",
            email="media-recipient@example.com",
            password="test-password",
        )
        self.room = ChatRoom.objects.create(room_type=ChatRoom.DIRECT)
        self.room.members.set([self.sender, recipient])
        self.client = APIClient()
        self.client.force_authenticate(self.sender)

    def _upload(self, content=b"document", *, message_id="media-message-1"):
        return self.client.post(
            "/api/chat/media/",
            {
                "file": SimpleUploadedFile("report.pdf", content, content_type="application/pdf"),
                "room_id": str(self.room.id),
                "media_type": "document",
                "mime": "application/pdf",
                "message_id": message_id,
            },
            format="multipart",
        )

    @override_settings(MEDIA_MAX_UPLOAD_BYTES=4)
    def test_oversized_upload_reports_authoritative_limit(self):
        response = self._upload(b"12345")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.data["max_bytes"], 4)
        self.assertEqual(MediaBlob.objects.count(), 0)

    def test_retry_reuses_blob_for_same_message_id(self):
        first = self._upload()
        second = self._upload()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["reused"])
        self.assertEqual(second.data["media_id"], first.data["media_id"])
        self.assertEqual(MediaBlob.objects.count(), 1)

    @override_settings(MEDIA_STORAGE_BACKEND="spaces")
    @patch("chat.media_views.create_presigned_upload")
    def test_direct_upload_is_idempotent_and_keeps_bytes_out_of_database(self, signed):
        signed.return_value = {
            "url": "https://example.invalid/signed-upload",
            "headers": {
                "Content-Type": "application/pdf",
                "x-amz-meta-md5": "0123456789abcdef0123456789abcdef",
            },
        }
        payload = {
            "room_id": str(self.room.id),
            "media_type": "document",
            "mime": "application/pdf",
            "message_id": "direct-message-1",
            "size_bytes": 8,
            "md5": "0123456789abcdef0123456789abcdef",
        }
        first = self.client.post("/api/chat/media/initiate/", payload, format="json")
        second = self.client.post("/api/chat/media/initiate/", payload, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["media_id"], second.data["media_id"])
        blob = MediaBlob.objects.get()
        self.assertIsNone(blob.data)
        self.assertEqual(blob.storage_backend, "spaces")
        self.assertTrue(blob.object_key.startswith(f"media/{self.room.id}/"))

    @override_settings(MEDIA_STORAGE_BACKEND="spaces")
    @patch("chat.media_views.inspect_object")
    @patch("chat.media_views.create_presigned_upload")
    def test_direct_upload_completion_verifies_remote_metadata(self, signed, inspect):
        signed.return_value = {"url": "https://example.invalid", "headers": {}}
        checksum = "0123456789abcdef0123456789abcdef"
        initiated = self.client.post(
            "/api/chat/media/initiate/",
            {
                "room_id": str(self.room.id),
                "media_type": "document",
                "mime": "application/pdf",
                "message_id": "direct-message-2",
                "size_bytes": 8,
                "md5": checksum,
            },
            format="json",
        )
        inspect.return_value = {"ContentLength": 8, "Metadata": {"md5": checksum}}

        completed = self.client.post(
            f"/api/chat/media/{initiated.data['media_id']}/complete/", {}, format="json"
        )

        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.data["uploaded"])
        self.assertIsNotNone(MediaBlob.objects.get().upload_completed_at)

    @override_settings(
        MEDIA_STORAGE_BACKEND="spaces",
        MEDIA_MULTIPART_THRESHOLD_BYTES=1,
        MEDIA_MULTIPART_PART_BYTES=5 * 1024 * 1024,
    )
    @patch("chat.media_views.create_presigned_part_upload")
    @patch("chat.media_views.list_multipart_parts", return_value=[])
    @patch("chat.media_views.create_multipart_upload", return_value="upload-123")
    def test_large_upload_returns_resumable_part_plan(self, create_upload, _list_parts, sign_part):
        sign_part.side_effect = lambda **kwargs: f"https://example.invalid/part/{kwargs['part_number']}"
        initiated = self.client.post(
            "/api/chat/media/initiate/",
            {
                "room_id": str(self.room.id),
                "media_type": "video",
                "mime": "video/mp4",
                "message_id": "multipart-message-1",
                "size_bytes": 6 * 1024 * 1024,
                "md5": "0123456789abcdef0123456789abcdef",
            },
            format="json",
        )

        self.assertEqual(initiated.status_code, 201)
        self.assertEqual(initiated.data["upload_mode"], "multipart")
        self.assertEqual(len(initiated.data["parts"]), 2)
        self.assertEqual(MediaBlob.objects.get().multipart_upload_id, "upload-123")
        create_upload.assert_called_once()

    @override_settings(MEDIA_STORAGE_BACKEND="spaces")
    @patch("chat.media_views.complete_multipart_upload")
    @patch("chat.media_views.list_multipart_parts")
    @patch("chat.media_views.inspect_object")
    def test_multipart_completion_uses_authoritative_part_list(self, inspect, list_parts, complete):
        blob = MediaBlob.objects.create(
            room=self.room,
            owner=self.sender,
            message_id="multipart-message-2",
            media_type=MediaBlob.VIDEO,
            mime="video/mp4",
            size_bytes=6 * 1024 * 1024,
            sha256="",
            md5="0123456789abcdef0123456789abcdef",
            storage_backend="spaces",
            object_key="media/test/video",
            multipart_upload_id="upload-456",
            multipart_part_size=5 * 1024 * 1024,
        )
        inspect.side_effect = [
            RuntimeError("not complete"),
            {"ContentLength": blob.size_bytes, "Metadata": {"md5": blob.md5}},
        ]
        list_parts.return_value = [
            {"PartNumber": 1, "ETag": "etag-1", "Size": 5 * 1024 * 1024},
            {"PartNumber": 2, "ETag": "etag-2", "Size": 1 * 1024 * 1024},
        ]

        response = self.client.post(f"/api/chat/media/{blob.id}/complete/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        complete.assert_called_once()
        blob.refresh_from_db()
        self.assertEqual(blob.multipart_upload_id, "")
        self.assertIsNotNone(blob.upload_completed_at)


class MediaCleanupRetentionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(
            username="cleanup-owner",
            email="cleanup-owner@example.com",
            password="test-password",
        )
        self.room = ChatRoom.objects.create(room_type=ChatRoom.DIRECT)
        self.room.members.add(self.owner)

    def _blob(self, **overrides):
        values = {
            "room": self.room,
            "owner": self.owner,
            "message_id": f"cleanup-{MediaBlob.objects.count()}",
            "media_type": MediaBlob.DOCUMENT,
            "mime": "text/plain",
            "size_bytes": 4,
            "sha256": "a" * 64,
            "md5": "b" * 32,
            "data": b"test",
            "storage_backend": "database",
        }
        values.update(overrides)
        return MediaBlob.objects.create(**values)

    @override_settings(MEDIA_HARD_TTL_DAYS=30)
    def test_cleanup_deletes_expired_confirmed_blob_and_keeps_fresh_blob(self):
        expired = self._blob(delete_after=timezone.now() - timedelta(seconds=1))
        fresh = self._blob(delete_after=timezone.now() + timedelta(hours=1))

        result = cleanup_expired_media()

        self.assertFalse(MediaBlob.objects.filter(id=expired.id).exists())
        self.assertTrue(MediaBlob.objects.filter(id=fresh.id).exists())
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(result["hard_ttl"], 0)

    @override_settings(MEDIA_HARD_TTL_DAYS=30)
    @patch("chat.media_storage.delete_object")
    def test_cleanup_deletes_stale_spaces_object_before_database_row(self, delete_object):
        stale = self._blob(
            data=None,
            storage_backend="spaces",
            object_key="media/test/stale.txt",
        )
        MediaBlob.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(days=31),
        )

        result = cleanup_expired_media()

        delete_object.assert_called_once_with("media/test/stale.txt")
        self.assertFalse(MediaBlob.objects.filter(id=stale.id).exists())
        self.assertEqual(result["hard_ttl"], 1)

    @override_settings(MEDIA_HARD_TTL_DAYS=30)
    @patch("chat.media_storage.delete_object", side_effect=RuntimeError("storage unavailable"))
    def test_cleanup_keeps_spaces_row_when_object_deletion_fails(self, _delete_object):
        stale = self._blob(
            data=None,
            storage_backend="spaces",
            object_key="media/test/retry.txt",
        )
        MediaBlob.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(days=31),
        )

        result = cleanup_expired_media()

        self.assertTrue(MediaBlob.objects.filter(id=stale.id).exists())
        self.assertEqual(result["deleted"], 0)


class MessageDeliveryMaintenanceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.sender = User.objects.create_user(
            username="delivery-sender",
            email="delivery-sender@example.com",
            password="test-password",
        )
        self.recipient = User.objects.create_user(
            username="delivery-recipient",
            email="delivery-recipient@example.com",
            password="test-password",
        )
        self.room = ChatRoom.objects.create(room_type=ChatRoom.DIRECT)
        self.room.members.set([self.sender, self.recipient])

    def _delivery(self, message_id="delivery-maintenance-1"):
        row = MessageDelivery.objects.create(
            room=self.room,
            message_id=message_id,
            sender=self.sender,
            recipient=self.recipient,
        )
        MessageDelivery.objects.filter(id=row.id).update(
            created_at=timezone.now() - timedelta(minutes=10),
        )
        row.refresh_from_db()
        return row

    @override_settings(
        MESSAGE_ACK_TIMEOUT_SECONDS=8,
        MESSAGE_DELIVERY_PUSH_RETRY_SECONDS=300,
        MESSAGE_DELIVERY_PUSH_MAX_ATTEMPTS=3,
    )
    @patch("chat.tasks.send_message_push", return_value=False)
    def test_failed_push_is_backed_off_instead_of_retried_each_sweep(self, send_push):
        row = self._delivery()

        first = sweep_stale_message_deliveries()
        second = sweep_stale_message_deliveries()

        row.refresh_from_db()
        self.assertEqual(send_push.call_count, 1)
        self.assertEqual(row.push_attempt_count, 1)
        self.assertIsNotNone(row.last_push_attempt_at)
        self.assertIsNone(row.push_sent_at)
        self.assertEqual(first["rows_skipped"], 1)
        self.assertEqual(second["rows_scanned"], 0)

    @override_settings(MESSAGE_DELIVERY_RETENTION_DAYS=30)
    def test_cleanup_deletes_only_expired_delivery_metadata(self):
        expired = self._delivery("delivery-expired")
        fresh = self._delivery("delivery-fresh")
        MessageDelivery.objects.filter(id=expired.id).update(
            created_at=timezone.now() - timedelta(days=31),
        )
        MessageDelivery.objects.filter(id=fresh.id).update(
            created_at=timezone.now() - timedelta(days=29),
        )

        result = cleanup_message_delivery_metadata()

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(MessageDelivery.objects.filter(id=expired.id).exists())
        self.assertTrue(MessageDelivery.objects.filter(id=fresh.id).exists())


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
        connected, _subprotocol = await communicator.connect(timeout=5)
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
            payload = await communicator.receive_json_from(timeout=5)
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

    def test_versioned_sync_delta_is_targeted_between_room_members(self):
        room = self._create_room()

        async def scenario():
            sender_socket = recipient_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                recipient_socket = await self._connect(self.recipient)
                entries = [{
                    "id": "delta-message-1",
                    "updated_at": "2026-08-31T12:00:00.000Z",
                    "revision": 2,
                    "is_deleted": True,
                }]
                await sender_socket.send_json_to({
                    "type": "sync_digest",
                    "room_id": str(room.id),
                    "ids": ["delta-message-1"],
                    "entries": entries,
                })
                digest = await self._receive_until(
                    recipient_socket,
                    lambda frame: frame.get("event") == "sync_digest",
                )
                self.assertEqual(digest["entries"], entries)
                self.assertEqual(digest["from_user_id"], self.sender.id)

                await recipient_socket.send_json_to({
                    "type": "sync_request",
                    "room_id": str(room.id),
                    "ids": [],
                    "update_ids": ["delta-message-1"],
                    "target_user_id": self.sender.id,
                })
                request = await self._receive_until(
                    sender_socket,
                    lambda frame: frame.get("event") == "sync_request",
                )
                self.assertEqual(request["update_ids"], ["delta-message-1"])
                self.assertEqual(request["from_user_id"], self.recipient.id)

                states = [{
                    "message_id": "delta-message-1",
                    "changes": {
                        "is_deleted": True,
                        "updated_at": "2026-08-31T12:00:00.000Z",
                        "revision": 2,
                    },
                }]
                await sender_socket.send_json_to({
                    "type": "sync_state",
                    "room_id": str(room.id),
                    "target_user_id": self.recipient.id,
                    "states": states,
                })
                state = await self._receive_until(
                    recipient_socket,
                    lambda frame: frame.get("event") == "sync_state",
                )
                self.assertEqual(state["states"], states)
                self.assertEqual(state["from_user_id"], self.sender.id)
            finally:
                await self._disconnect_all(sender_socket, recipient_socket)

        async_to_sync(scenario)()

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
                        push_sent_at__isnull=False,
                    ).exists()
                )
                await asyncio.sleep(0.05)
            finally:
                await self._disconnect_all(sender_socket)

        with patch(
            "chat.consumers.send_message_push",
            return_value=True,
        ) as push_mock, patch.object(
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
        self.assertEqual(push_mock.call_count, 1)
        self.assertEqual(
            PendingDelivery.objects.filter(
                room=room,
                from_user=self.sender,
                to_user=self.recipient,
            ).count(),
            1,
        )

    def test_group_hydration_targets_one_member_without_push_or_fanout(self):
        room = self._create_room(
            ChatRoom.GROUP,
            include_group_member=True,
        )

        async def scenario():
            sender_socket = target_socket = other_socket = None
            try:
                sender_socket = await self._connect(self.sender)
                target_socket = await self._connect(self.recipient)
                other_socket = await self._connect(self.group_member)

                frame = self._message_frame(room, "group-hydration-1", "Recovered message")
                frame.update({
                    "hydration": True,
                    "target_recipient_id": self.recipient.id,
                })
                await sender_socket.send_json_to(frame)

                ack = await self._receive_until(
                    sender_socket,
                    lambda item: item.get("type") == "message_server_ack",
                )
                self.assertEqual(ack["message_id"], "group-hydration-1")
                incoming = await self._receive_until(
                    target_socket,
                    lambda item: item.get("event") == "new_message",
                )
                self.assertTrue(incoming["hydration"])
                self.assertEqual(incoming["message_id"], "group-hydration-1")
                self.assertTrue(await other_socket.receive_nothing(timeout=0.15))
            finally:
                await self._disconnect_all(sender_socket, target_socket, other_socket)

        with patch("chat.consumers.send_message_push", return_value=True) as push_mock:
            async_to_sync(scenario)()

        push_mock.assert_not_called()
        self.assertFalse(
            MessageDelivery.objects.filter(message_id="group-hydration-1").exists()
        )

    def test_group_read_receipt_routes_only_to_original_author(self):
        room = self._create_room(
            ChatRoom.GROUP,
            include_group_member=True,
        )

        async def scenario():
            reader_socket = author_socket = other_socket = None
            try:
                reader_socket = await self._connect(self.recipient)
                author_socket = await self._connect(self.sender)
                other_socket = await self._connect(self.group_member)

                await reader_socket.send_json_to({
                    "type": "message_update",
                    "room_id": str(room.id),
                    "updates": [{
                        "id": "targeted-read-update-1",
                        "message_id": "group-message-read-1",
                        "changes": {
                            "is_read": True,
                            "receipt_target_id": self.sender.id,
                        },
                    }],
                })

                accepted = await self._receive_until(
                    reader_socket,
                    lambda item: item.get("type") == "message_update_server_ack",
                )
                self.assertEqual(
                    accepted["updates"],
                    [{
                        "id": "targeted-read-update-1",
                        "expected_peer_ids": [self.sender.id],
                    }],
                )
                delivered = await self._receive_until(
                    author_socket,
                    lambda item: item.get("event") == "message_update",
                )
                self.assertEqual(len(delivered["updates"]), 1)
                self.assertTrue(delivered["updates"][0]["changes"]["is_read"])
                self.assertNotIn(
                    "receipt_target_id",
                    delivered["updates"][0]["changes"],
                )
                self.assertTrue(await other_socket.receive_nothing(timeout=0.15))
            finally:
                await self._disconnect_all(reader_socket, author_socket, other_socket)

        async_to_sync(scenario)()

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

    def test_new_axion_socket_supersedes_same_installation_lease(self):
        """A reconnect must not leave a ghost foreground lease behind."""

        async def connect_installation(installation_id):
            socket = WebsocketCommunicator(application, "/ws/notifications/")
            connected, _subprotocol = await socket.connect(timeout=2)
            self.assertTrue(connected)
            await socket.send_json_to({
                "type": "auth",
                "token": self._token_for(self.sender),
                "installation_id": installation_id,
            })
            await self._receive_until(socket, lambda frame: frame.get("type") == "auth_ok")
            await self._receive_until(socket, lambda frame: frame.get("event") == "presence_snapshot")
            return socket

        async def scenario():
            first = second = None
            try:
                first = await connect_installation("test-installation")
                await first.send_json_to({"type": "app_state", "state": "active"})
                await first.send_json_to({"type": "ping"})
                await self._receive_until(first, lambda frame: frame.get("type") == "pong")

                second = await connect_installation("test-installation")
                await second.send_json_to({"type": "app_state", "state": "background"})
                await second.send_json_to({"type": "ping"})
                await self._receive_until(second, lambda frame: frame.get("type") == "pong")

                def one_background_lease():
                    rows = list(UserPresenceSession.objects.filter(
                        user=self.sender,
                        installation_id="test-installation",
                    ).values_list("app_state", flat=True))
                    return rows == [UserPresence.APP_STATE_BACKGROUND]

                await self._wait_for_database(one_background_lease)
                payload, _changed = await database_sync_to_async(aggregate_user_presence)(self.sender.id)
                self.assertFalse(payload["is_online"])
                self.assertEqual(payload["presence"], "background")
            finally:
                await self._disconnect_all(first, second)

        async_to_sync(scenario)()

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
