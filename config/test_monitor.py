from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from users.models import User, UserDevice, UserPresenceSession
from chat.models import ChatRoom, MessageDelivery


class MonitorAccuracyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='monitor-admin', is_staff=True)
        self.peer = User.objects.create_user(username='monitor-peer')
        self.client.force_login(self.admin)

    def test_shared_sessions_expire_and_fcm_users_are_counted(self):
        for key, state in [('one', 'background'), ('two', 'active')]:
            UserPresenceSession.objects.create(user=self.admin, connection_id=key, app_state=state)
        UserPresenceSession.objects.create(user=self.peer, connection_id='stale', app_state='active', last_seen=timezone.now()-timedelta(minutes=10))
        UserDevice.objects.create(user=self.peer, installation_id='fcm-only', fcm_token='test-endpoint')
        data = self.client.get('/api/monitor/').json()
        self.assertEqual(data['websockets']['online_count'], 1)
        self.assertEqual(data['websockets']['connection_count'], 2)
        self.assertEqual(data['users']['with_push_token'], 1)
        self.assertEqual(data['users']['offline_with_push'], 1)
        self.assertIsNone(data['messages']['unread'])
        self.assertIsNone(data['websockets']['chat_rooms_active'])

    def test_delivery_rate_uses_same_creation_cohort(self):
        room = ChatRoom.objects.create(room_type='direct')
        room.members.add(self.admin, self.peer)
        for message_id in ['old', 'new']:
            MessageDelivery.objects.create(room=room, sender=self.admin, recipient=self.peer, message_id=message_id, status='delivered', delivered_at=timezone.now())
        MessageDelivery.objects.filter(message_id='old').update(created_at=timezone.now()-timedelta(days=2))
        data = self.client.get('/api/monitor/').json()['reliability']['messages']
        self.assertEqual(data['created_24h'], 1)
        self.assertEqual(data['delivered_24h'], 1)
        self.assertEqual(data['ack_rate_24h'], 1)
        self.assertEqual(data['sender_confirmation_pending'], 2)

    def test_empty_sample_is_unknown_and_access_is_staff_only(self):
        data = self.client.get('/api/monitor/').json()
        self.assertIsNone(data['reliability']['messages']['ack_rate_24h'])
        self.client.force_login(self.peer)
        self.assertEqual(self.client.get('/api/monitor/').status_code, 302)
        self.assertEqual(self.client.get('/monitor/').status_code, 302)
