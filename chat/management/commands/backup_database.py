"""Create a verified logical PostgreSQL archive in a dedicated private bucket.

No pruning and no restore into production. Requires PostgreSQL 17+ client tools.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Back up PostgreSQL to a dedicated private Spaces bucket (no deletion)."

    def handle(self, *args, **options):
        bucket = os.getenv("BACKUP_SPACES_BUCKET", "").strip()
        if not bucket or bucket == settings.SPACES_BUCKET:
            raise CommandError("Set BACKUP_SPACES_BUCKET to a dedicated private bucket, not the media bucket.")
        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            raise CommandError("Install PostgreSQL 17+ client tools before enabling backups.")
        db = settings.DATABASES["default"]
        if db["ENGINE"] != "django.db.backends.postgresql":
            raise CommandError("This backup command requires PostgreSQL.")
        access_key = os.getenv("BACKUP_SPACES_ACCESS_KEY", "").strip()
        secret_key = os.getenv("BACKUP_SPACES_SECRET_KEY", "").strip()
        if not access_key or not secret_key:
            raise CommandError("Configure dedicated BACKUP_SPACES_ACCESS_KEY and BACKUP_SPACES_SECRET_KEY.")
        client = boto3.client(
            "s3", endpoint_url=settings.SPACES_ENDPOINT, region_name=settings.SPACES_REGION,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        # DigitalOcean's limited keys cannot inspect bucket policies and cannot
        # coexist with bucket policies. Probe actual anonymous access instead of
        # expanding this key's permissions. Only a harmless sentinel is uploaded
        # until the private-access check succeeds.
        anonymous = boto3.client("s3", endpoint_url=settings.SPACES_ENDPOINT,
                                 region_name=settings.SPACES_REGION,
                                 config=Config(signature_version=UNSIGNED, connect_timeout=5,
                                               read_timeout=5, retries={"max_attempts": 1}))
        probe = f"postgres/privacy-check-{uuid.uuid4().hex}.txt"
        client.put_object(Bucket=bucket, Key=probe, Body=b"Axonic backup privacy check", ACL="private")
        client.head_object(Bucket=bucket, Key=probe)
        self.require_private(anonymous, bucket, probe)
        env = dict(os.environ)
        env.update(PGHOST=str(db.get("HOST") or "localhost"), PGPORT=str(db.get("PORT") or 5432),
                   PGUSER=str(db["USER"]), PGPASSWORD=str(db["PASSWORD"]), PGDATABASE=str(db["NAME"]),
                   PGCONNECT_TIMEOUT="10", PGSSLMODE=db.get("OPTIONS", {}).get("sslmode", "require"))
        key = f"postgres/{timezone.now():%Y/%m/%d/%H%M%S}-{uuid.uuid4().hex}.dump"
        with tempfile.TemporaryDirectory(prefix="axonic-backup-") as folder:
            archive = Path(folder) / "database.dump"
            try:
                subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(archive)],
                               env=env, check=True, capture_output=True, timeout=1800)
                subprocess.run(["pg_restore", "--list", str(archive)], check=True, capture_output=True, timeout=120)
            except (subprocess.SubprocessError, OSError):
                raise CommandError("PostgreSQL backup/validation failed; no backup uploaded.") from None
            with archive.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            client.upload_file(str(archive), bucket, key, ExtraArgs={
                "ACL": "private", "ContentType": "application/octet-stream", "Metadata": {"sha256": digest},
            })
            head = client.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] != archive.stat().st_size or head.get("Metadata", {}).get("sha256") != digest:
                raise CommandError("Uploaded backup verification failed.")
            self.require_private(anonymous, bucket, key)
        self.stdout.write(self.style.SUCCESS(f"Backup archive uploaded and verified: {key}"))

    @staticmethod
    def require_private(anonymous, bucket, key):
        try:
            response = anonymous.get_object(Bucket=bucket, Key=key, Range="bytes=0-0")
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403:
                return
            raise CommandError("Cannot verify anonymous access is denied; backup stopped.") from None
        response["Body"].close()
        raise CommandError("Anonymous object access succeeded; backup bucket is not private.")
