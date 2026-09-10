from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from chat.consumers import NotificationConsumer
from chat.models import ChatRoom, MessageDelivery, OfflineEmailNudge
from users.models import Contact, User, UserPresence


class WebEmailGuardTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='web-sender')
        self.recipient = User.objects.create_user(username='web-recipient', email='test@example.com')
        Contact.objects.create(owner=self.recipient, contact=self.sender)
        self.room = ChatRoom.objects.create(room_type=ChatRoom.DIRECT)
        self.room.members.add(self.sender, self.recipient)
        self.delivery = MessageDelivery.objects.create(
            room=self.room, sender=self.sender, recipient=self.recipient, message_id='web-test',
        )
        self.consumer = NotificationConsumer()
        self.consumer.user = self.sender

    def reserve(self):
        return async_to_sync(self.consumer.reserve_offline_email_nudges)(str(self.room.id), [self.recipient.id])

    def test_delivered_message_does_not_reserve_email(self):
        self.delivery.status = MessageDelivery.STATUS_DELIVERED
        self.delivery.save()
        self.assertEqual(self.reserve(), [])
        self.assertFalse(OfflineEmailNudge.objects.exists())

    def test_live_browser_does_not_reserve_email(self):
        UserPresence.objects.update_or_create(user=self.recipient, defaults={
            'notification_socket_connected': True, 'app_state': 'active',
            'last_notification_seen_at': timezone.now(),
        })
        self.assertEqual(self.reserve(), [])

    def test_opt_out_is_respected(self):
        self.recipient.notif_offline_email_enabled = False
        self.recipient.save()
        self.assertEqual(self.reserve(), [])

    def test_offline_pending_message_reserves_once(self):
        self.assertEqual(len(self.reserve()), 1)
        self.assertEqual(self.reserve(), [])
