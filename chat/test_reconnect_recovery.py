from django.test import TestCase

from users.models import User, BlockedUser
from .models import ChatRoom, MessageDelivery, PendingDelivery
from .relay_service import pending_recovery_routes


class ReconnectRecoveryTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username="recovery-sender")
        self.recipient = User.objects.create_user(username="recovery-recipient")
        self.third = User.objects.create_user(username="recovery-third")
        self.room = ChatRoom.objects.create()
        self.room.members.set([self.sender, self.recipient])
        self.delivery = MessageDelivery.objects.create(
            room=self.room, sender=self.sender, recipient=self.recipient,
            message_id="missed-during-disconnect",
        )

    def test_pending_receipt_bootstraps_recovery_without_offline_hint(self):
        self.assertFalse(PendingDelivery.objects.exists())
        expected = [{"from_user_id": self.sender.id,
                     "from_username": self.sender.username, "room_id": str(self.room.id)}]
        self.assertEqual(pending_recovery_routes(self.recipient), expected)
        self.assertEqual(pending_recovery_routes(self.recipient, self.room.id), expected)

    def test_delivered_receipt_does_not_request_resend(self):
        self.delivery.status = MessageDelivery.STATUS_DELIVERED
        self.delivery.save()
        self.assertEqual(pending_recovery_routes(self.recipient), [])

    def test_legacy_and_multiple_messages_are_deduplicated(self):
        PendingDelivery.objects.create(room=self.room, from_user=self.sender, to_user=self.recipient)
        MessageDelivery.objects.create(room=self.room, sender=self.sender,
                                       recipient=self.recipient, message_id="second")
        self.assertEqual(len(pending_recovery_routes(self.recipient)), 1)
        self.delivery.delete()
        MessageDelivery.objects.all().delete()
        self.assertEqual(len(pending_recovery_routes(self.recipient)), 1)

    def test_revoked_recipient_membership_excludes_both_hint_types(self):
        PendingDelivery.objects.create(room=self.room, from_user=self.sender, to_user=self.recipient)
        self.room.members.remove(self.recipient)
        self.assertEqual(pending_recovery_routes(self.recipient), [])

    def test_removed_sender_is_not_requested_to_relay(self):
        self.room.members.remove(self.sender)
        self.assertEqual(pending_recovery_routes(self.recipient), [])

    def test_blocked_sender_is_excluded(self):
        BlockedUser.objects.create(owner=self.recipient, blocked=self.sender)
        self.assertEqual(pending_recovery_routes(self.recipient), [])

    def test_room_and_recipient_isolation(self):
        other = ChatRoom.objects.create()
        other.members.set([self.recipient, self.third])
        self.assertEqual(pending_recovery_routes(self.recipient, other.id), [])
        self.assertEqual(pending_recovery_routes(self.third), [])

    def test_group_recovers_each_pending_sender_once(self):
        self.room.room_type = ChatRoom.GROUP
        self.room.save()
        self.room.members.add(self.third)
        MessageDelivery.objects.create(room=self.room, sender=self.third,
                                       recipient=self.recipient, message_id="group-missed")
        self.assertEqual({r["from_user_id"] for r in pending_recovery_routes(self.recipient)},
                         {self.sender.id, self.third.id})
