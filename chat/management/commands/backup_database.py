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
        client = boto3.client(
            "s3", endpoint_url=settings.SPACES_ENDPOINT, region_name=settings.SPACES_REGION,
            aws_access_key_id=settings.SPACES_ACCESS_KEY,
            aws_secret_access_key=settings.SPACES_SECRET_KEY,
        )
        # Refuse a public bucket even though each archive also gets a private ACL.
        acl = client.get_bucket_acl(Bucket=bucket)
        if any(g.get("Grantee", {}).get("URI") for g in acl.get("Grants", [])):
            raise CommandError("Backup bucket must not grant public/group access.")
        # Bucket policies can override a private ACL; fail closed if one exists.
        from botocore.exceptions import ClientError
        try:
            client.get_bucket_policy(Bucket=bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
                raise CommandError("Unable to verify backup bucket policy.") from None
        else:
            raise CommandError("Use a backup bucket without a bucket policy.")
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
        self.stdout.write(self.style.SUCCESS(f"Backup archive uploaded and verified: {key}"))
