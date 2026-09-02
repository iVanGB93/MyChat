from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import BlockedUser, Contact

User = get_user_model()


class ContactAcceptanceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="contact-owner")
        self.peer = User.objects.create_user(username="contact-peer")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def accept(self, target=None):
        return self.client.post(
            "/api/users/contacts/", {"contact": target or self.peer.pk}, format="json",
        )

    def test_first_accept_and_retries_return_the_same_contact(self):
        first = self.accept()
        self.assertEqual(first.status_code, 201)
        for _ in range(3):
            retry = self.accept()
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.data["id"], first.data["id"])
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(first.data["contact_detail"]["id"], self.peer.pk)

    def test_invalid_and_self_contacts_are_rejected(self):
        self.assertEqual(self.accept(self.peer.pk + 100).status_code, 400)
        self.assertEqual(self.accept(self.owner.pk).status_code, 400)
        self.assertEqual(Contact.objects.count(), 0)

    def test_accept_does_not_implicitly_unblock(self):
        BlockedUser.objects.create(owner=self.owner, blocked=self.peer)
        result = self.accept()
        self.assertEqual(result.status_code, 400)
        self.assertIn("Unblock", str(result.data))
        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(BlockedUser.objects.count(), 1)

    def test_contacts_remain_directional_and_owner_scoped(self):
        first = self.accept()
        self.client.force_authenticate(self.peer)
        reverse = self.accept(self.owner.pk)
        self.assertEqual(reverse.status_code, 201)
        self.assertNotEqual(first.data["id"], reverse.data["id"])
        result = self.client.delete(f'/api/users/contacts/{first.data["id"]}/')
        self.assertEqual(result.status_code, 404)
        self.assertEqual(Contact.objects.count(), 2)

    def test_accept_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.accept().status_code, (401, 403))
        self.assertEqual(Contact.objects.count(), 0)
