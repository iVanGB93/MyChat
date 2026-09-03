from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from calls.models import CallLog
from config.metadata_sync import metadata_delta
from users.models import Contact, User
from .models import ChatRoom, MessageDelivery
from .receipt_service import confirm_sender_receipts
from .tasks import cleanup_message_delivery_metadata


class LocalFirstMetadataTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(username="metadata-a")
        self.b = User.objects.create_user(username="metadata-b")
        self.c = User.objects.create_user(username="metadata-outsider")
        self.room = ChatRoom.objects.create(name="Original", room_type=ChatRoom.GROUP)
        self.room.members.set([self.a, self.b])
        self.client = APIClient()
        self.client.force_authenticate(self.a)

    def sync(self, path, versions=None):
        response = self.client.post(path, {"versions": versions or {}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_room_delta_changes_and_revoked_membership(self):
        path = "/api/chat/rooms/sync/"
        first = self.sync(path)
        self.assertEqual(len(first["upserts"]), 1)
        unchanged = self.sync(path, first["versions"])
        self.assertEqual(unchanged["upserts"], [])
        self.room.name = "Renamed"
        self.room.save()
        changed = self.sync(path, first["versions"])
        self.assertEqual(changed["upserts"][0]["name"], "Renamed")
        self.room.members.remove(self.a)
        removed = self.sync(path, changed["versions"])
        self.assertEqual(removed["upserts"], [])
        self.assertEqual(removed["removed_ids"], [str(self.room.id)])

    def test_contacts_use_contact_user_id_and_track_profile_changes(self):
        Contact.objects.create(owner=self.a, contact=self.b)
        Contact.objects.create(owner=self.c, contact=self.a)
        path = "/api/users/contacts/sync/"
        first = self.sync(path)
        self.assertEqual(set(first["versions"]), {str(self.b.id)})
        self.b.display_name = "New name"
        self.b.save()
        changed = self.sync(path, first["versions"])
        self.assertEqual(len(changed["upserts"]), 1)
        Contact.objects.filter(owner=self.a).delete()
        self.assertEqual(self.sync(path, changed["versions"])["removed_ids"], [str(self.b.id)])

    def test_call_delta_is_private_and_includes_terminal_updates(self):
        call = CallLog.objects.create(caller=self.a, callee=self.b, call_type=CallLog.VOICE)
        CallLog.objects.create(caller=self.b, callee=self.c, call_type=CallLog.VIDEO)
        first = self.sync("/api/calls/history/")
        self.assertEqual(set(first["versions"]), {str(call.id)})
        call.status = CallLog.ENDED
        call.duration_seconds = 42
        call.save()
        changed = self.sync("/api/calls/history/", first["versions"])
        self.assertEqual(changed["upserts"][0]["duration_seconds"], 42)

    def test_presence_does_not_invalidate_stable_metadata(self):
        row = {"id": 1, "user": {"name": "A", "is_online": True, "last_seen": "old"}}
        first = metadata_delta(SimpleNamespace(data={}), [row]).data
        row["user"].update(is_online=False, last_seen="new")
        second = metadata_delta(SimpleNamespace(data={"versions": first["versions"]}), [row]).data
        self.assertEqual(second["upserts"], [])

    def test_invalid_versions_and_unauthenticated_sync_are_rejected(self):
        response = self.client.post("/api/chat/rooms/sync/", {"versions": []}, format="json")
        self.assertEqual(response.status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/chat/rooms/sync/", {}, format="json").status_code, 401)


class LocalReceiptRetentionTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(username="receipt-a")
        self.b = User.objects.create_user(username="receipt-b")
        self.c = User.objects.create_user(username="receipt-outsider")
        self.room = ChatRoom.objects.create()
        self.room.members.set([self.a, self.b])
        self.delivery = MessageDelivery.objects.create(
            room=self.room, message_id="receipt-1", sender=self.a, recipient=self.b,
            status=MessageDelivery.STATUS_DELIVERED, delivered_at=timezone.now(),
        )
        self.entry = {"room_id": str(self.room.id), "message_id": "receipt-1", "recipient_ids": [self.b.id]}

    def test_only_sender_can_confirm_and_repeated_confirmation_preserves_time(self):
        confirm_sender_receipts(self.c.id, [self.entry])
        self.delivery.refresh_from_db()
        self.assertIsNone(self.delivery.sender_confirmed_at)
        confirm_sender_receipts(self.a.id, [self.entry])
        self.delivery.refresh_from_db()
        first = self.delivery.sender_confirmed_at
        self.assertIsNotNone(first)
        confirm_sender_receipts(self.a.id, [self.entry])
        self.delivery.refresh_from_db()
        self.assertEqual(first, self.delivery.sender_confirmed_at)

    def test_pending_receipt_is_not_confirmed_or_acknowledged(self):
        self.delivery.status = MessageDelivery.STATUS_PENDING
        self.delivery.save()
        accepted = confirm_sender_receipts(self.a.id, [self.entry])
        self.assertEqual(accepted[0]["recipient_ids"], [])
        self.delivery.refresh_from_db()
        self.assertIsNone(self.delivery.sender_confirmed_at)

    @override_settings(MESSAGE_RECEIPT_CONFIRMED_RETENTION_DAYS=7)
    def test_cleanup_retains_unconfirmed_pending_and_recently_confirmed(self):
        old = timezone.now() - timedelta(days=90)
        MessageDelivery.objects.filter(pk=self.delivery.pk).update(created_at=old)
        pending = MessageDelivery.objects.create(room=self.room, message_id="pending", sender=self.a, recipient=self.b)
        MessageDelivery.objects.filter(pk=pending.pk).update(created_at=old)
        self.assertEqual(cleanup_message_delivery_metadata()["deleted"], 0)
        confirm_sender_receipts(self.a.id, [self.entry])
        self.assertEqual(cleanup_message_delivery_metadata()["deleted"], 0)
        MessageDelivery.objects.filter(pk=self.delivery.pk).update(sender_confirmed_at=old)
        self.assertEqual(cleanup_message_delivery_metadata()["deleted"], 1)
        self.assertTrue(MessageDelivery.objects.filter(pk=pending.pk).exists())

    def test_batch_receipts_preserve_individual_results_and_validate_input(self):
        client = APIClient()
        client.force_authenticate(self.b)
        valid = {"message_id": "receipt-1", "room_id": str(self.room.id), "sender_id": self.a.id}
        response = client.post("/api/chat/messages/ack-batch/", {"receipts": [valid, {**valid, "room_id": "bad"}, {**valid, "message_id": 123}]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["http_status"] for r in response.data["results"]], [200, 400, 400])
        self.assertEqual(response.data["results"][0]["status"], "already_delivered")
        self.assertEqual(client.post("/api/chat/messages/ack-batch/", {"receipts": []}, format="json").status_code, 400)
