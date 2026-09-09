from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User
from .models import ChatRoom, MediaBlob, MediaDownload, MessageDelivery


class VerifiedMediaReceiptTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='media-sender')
        self.recipient = User.objects.create_user(username='media-recipient')
        self.outsider = User.objects.create_user(username='media-outsider')
        self.room = ChatRoom.objects.create()
        self.room.members.set([self.sender, self.recipient])
        self.blob = MediaBlob.objects.create(
            room=self.room, owner=self.sender, message_id='photo-1',
            media_type='image', mime='image/jpeg', size_bytes=3, sha256='abc', data=b'abc',
        )
        self.delivery = MessageDelivery.objects.create(
            room=self.room, message_id='photo-1', sender=self.sender,
            recipient=self.recipient, status=MessageDelivery.STATUS_PENDING,
        )
        self.client = APIClient()

    def test_verified_download_repairs_missing_receipt_idempotently(self):
        self.client.force_authenticate(self.recipient)
        url = f'/api/chat/media/{self.blob.id}/downloaded/'
        for _ in range(2):
            self.assertEqual(self.client.post(url, {}, format='json').status_code, 200)
            self.delivery.refresh_from_db()
            self.assertEqual(self.delivery.status, MessageDelivery.STATUS_DELIVERED)
        self.assertEqual(MediaDownload.objects.count(), 1)

    def test_sender_reconciliation_repairs_older_verified_download(self):
        MediaDownload.objects.create(media=self.blob, recipient=self.recipient)
        self.client.force_authenticate(self.sender)
        response = self.client.post('/api/chat/messages/delivery-status/', {'message_ids': ['photo-1']}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['delivered']), 1)

    def test_without_verified_download_message_remains_pending(self):
        self.client.force_authenticate(self.sender)
        response = self.client.post('/api/chat/messages/delivery-status/', {'message_ids': ['photo-1']}, format='json')
        self.assertEqual(response.data['delivered'], [])

    def test_outsider_cannot_confirm_or_inspect_delivery(self):
        self.client.force_authenticate(self.outsider)
        self.assertEqual(self.client.post(f'/api/chat/media/{self.blob.id}/downloaded/', {}, format='json').status_code, 403)
        response = self.client.post('/api/chat/messages/delivery-status/', {'message_ids': ['photo-1']}, format='json')
        self.assertEqual(response.data['delivered'], [])
