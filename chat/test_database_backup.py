import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from botocore.exceptions import ClientError
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .management.commands.backup_database import Command


class DatabaseBackupTests(SimpleTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            SPACES_BUCKET="media", SPACES_ENDPOINT="https://example.invalid", SPACES_REGION="test",
            SPACES_ACCESS_KEY="test", SPACES_SECRET_KEY="test",
            DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "test",
                                   "USER": "test", "PASSWORD": "never-in-command-args", "HOST": "db"}},
        )
        self.client = MagicMock()
        self.anonymous = MagicMock()
        self.anonymous.get_object.side_effect = ClientError({"ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")
        for target, kwargs in [
            ("chat.management.commands.backup_database.settings", {"new": self.settings}),
            ("chat.management.commands.backup_database.boto3.client", {"side_effect": [self.client, self.anonymous]}),
            ("chat.management.commands.backup_database.shutil.which", {"return_value": "/bin/tool"}),
        ]:
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict("os.environ", {"BACKUP_SPACES_BUCKET": "backups", "BACKUP_SPACES_ACCESS_KEY": "scoped-id", "BACKUP_SPACES_SECRET_KEY": "scoped-secret"})
        p.start()
        self.addCleanup(p.stop)

    def test_refuses_media_bucket(self):
        with patch.dict("os.environ", {"BACKUP_SPACES_BUCKET": "media"}), self.assertRaises(CommandError):
            Command().handle()
        self.client.upload_file.assert_not_called()

    def test_refuses_public_bucket(self):
        self.anonymous.get_object.side_effect = None
        with self.assertRaises(CommandError):
            Command().handle()
        self.client.upload_file.assert_not_called()

    def test_refuses_ambiguous_privacy_response(self):
        self.anonymous.get_object.side_effect = ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
        with self.assertRaises(CommandError):
            Command().handle()
        self.client.upload_file.assert_not_called()

    def test_does_not_fall_back_to_media_credentials(self):
        with patch.dict("os.environ", {"BACKUP_SPACES_SECRET_KEY": ""}), self.assertRaises(CommandError):
            Command().handle()

    @patch("chat.management.commands.backup_database.subprocess.run")
    def test_validates_archive_before_private_upload(self, run):
        def create_archive(args, **kwargs):
            if args[0] == "pg_dump":
                self.assertNotIn("never-in-command-args", " ".join(args))
                Path(args[-1]).write_bytes(b"test-archive")
        run.side_effect = create_archive
        def upload(path, bucket, key, ExtraArgs):
            self.assertEqual(ExtraArgs["ACL"], "private")
            self.client.head_object.return_value = {"ContentLength": Path(path).stat().st_size, "Metadata": ExtraArgs["Metadata"]}
        self.client.upload_file.side_effect = upload
        Command(stdout=io.StringIO()).handle()
        self.assertEqual(run.call_count, 2)
        self.client.upload_file.assert_called_once()
