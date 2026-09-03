from io import StringIO
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from chat.models import ChatRoom
from .db_storage import DatabaseStorage, FileBlob, is_profile_object
from .models import User, UserProfile


class ProfileObjectStorageTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()
        self.blob = FileBlob.objects.create(name="avatars/old.png", data=b"old image", content_type="image/png", size=9)
        self.key = "profile-objects/" + "a" * 32 + ".png"

    @override_settings(PROFILE_MEDIA_STORAGE_BACKEND="spaces")
    @patch("chat.media_storage.upload_file")
    def test_new_avatar_uses_spaces_but_legacy_remains_readable(self, upload):
        name = self.storage.save("avatars/new.png", SimpleUploadedFile("new.png", b"image", content_type="image/png"))
        self.assertTrue(is_profile_object(name))
        self.assertEqual(upload.call_args.kwargs["mime"], "image/png")
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertEqual(self.storage.open(self.blob.name).read(), b"old image")
        self.assertEqual(self.storage.url(self.blob.name), "/media-db/avatars/old.png")

    @override_settings(SPACES_BUCKET="test-bucket")
    @patch("chat.media_storage._client")
    def test_stable_redirect_only_signs_profile_objects(self, client):
        client.return_value.generate_presigned_url.return_value = "https://example.invalid/avatar"
        response = self.client.get(self.storage.url(self.key))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://example.invalid/avatar")
        self.assertIn("max-age=300", response["Cache-Control"])
        self.assertEqual(client.return_value.generate_presigned_url.call_args.kwargs["Params"]["Key"], self.key)
        self.assertEqual(self.client.get("/media-profile/media/private-chat/image.png").status_code, 404)
        self.assertEqual(client.return_value.generate_presigned_url.call_count, 1)

    @override_settings(PROFILE_MEDIA_STORAGE_BACKEND="database", SPACES_BUCKET="test-bucket")
    @patch("chat.media_storage._client")
    def test_existing_object_reads_even_if_new_upload_backend_is_database(self, client):
        body = Mock()
        body.read.return_value = b"object"
        client.return_value.get_object.return_value = {"Body": body}
        self.assertEqual(self.storage.open(self.key).read(), b"object")
        body.close.assert_called_once()

    @override_settings(PROFILE_MEDIA_STORAGE_BACKEND="spaces")
    @patch("users.management.commands.migrate_profile_media.save_profile_object")
    def test_migration_preview_and_apply_preserve_original_blob(self, upload):
        user = User.objects.create_user(username="avatar-owner", avatar=self.blob.name)
        room = ChatRoom.objects.create(avatar=self.blob.name)
        output = StringIO()
        call_command("migrate_profile_media", stdout=output)
        upload.assert_not_called()
        self.assertIn("Preview only", output.getvalue())
        upload.return_value = self.key
        call_command("migrate_profile_media", apply=True, stdout=output)
        upload.assert_called_once()
        user.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(user.avatar.name, self.key)
        self.assertEqual(UserProfile.objects.get(user=user).avatar.name, self.key)
        self.assertEqual(room.avatar.name, self.key)
        self.assertTrue(FileBlob.objects.filter(pk=self.blob.name).exists())
