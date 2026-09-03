"""Avatar storage with immutable Spaces objects and legacy database reads.

The historic class name is retained for existing Django field migrations.
PROFILE_MEDIA_STORAGE_BACKEND chooses where new uploads go; stored names
determine where existing files are read, independently of that setting.
"""

from __future__ import annotations

import os
import uuid
import re
from urllib.parse import quote

from django.core.files.base import ContentFile
from django.conf import settings
from django.core.files.storage import Storage
from django.db import models
from django.utils.deconstruct import deconstructible


class FileBlob(models.Model):
    """A single file stored as bytes in the database."""

    name = models.CharField(max_length=255, primary_key=True)
    data = models.BinaryField()
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "users"

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return self.name


@deconstructible
class DatabaseStorage(Storage):
    """Django ``Storage`` implementation backed by the ``FileBlob`` model."""

    # Storage API ----------------------------------------------------------

    def _open(self, name, mode="rb"):
        if is_profile_object(name):
            from chat.media_storage import _client
            body = _client().get_object(Bucket=settings.SPACES_BUCKET, Key=name)["Body"]
            try:
                return ContentFile(body.read(), name=name)
            finally:
                body.close()
        blob = FileBlob.objects.get(pk=name)
        return ContentFile(bytes(blob.data), name=name)

    def _save(self, name, content):
        if getattr(settings, "PROFILE_MEDIA_STORAGE_BACKEND", "database") == "spaces":
            return save_profile_object(name, content)
        content.seek(0)
        data = content.read()
        content_type = getattr(content, "content_type", "") or ""
        FileBlob.objects.update_or_create(
            name=name,
            defaults={
                "data": data,
                "content_type": content_type,
                "size": len(data),
            },
        )
        return name

    def delete(self, name):
        if is_profile_object(name):
            from chat.media_storage import delete_object
            delete_object(name)
            return
        FileBlob.objects.filter(pk=name).delete()

    def exists(self, name):
        if is_profile_object(name):
            from chat.media_storage import _client
            try:
                _client().head_object(Bucket=settings.SPACES_BUCKET, Key=name)
                return True
            except Exception as error:
                code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise
        return FileBlob.objects.filter(pk=name).exists()

    def listdir(self, path):  # pragma: no cover - rarely used
        prefix = path.rstrip("/") + "/" if path else ""
        names = FileBlob.objects.filter(name__startswith=prefix).values_list(
            "name", flat=True,
        )
        dirs, files = set(), []
        for full in names:
            rest = full[len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), files

    def size(self, name):
        if is_profile_object(name):
            from chat.media_storage import _client
            return _client().head_object(Bucket=settings.SPACES_BUCKET, Key=name)["ContentLength"]
        return FileBlob.objects.filter(pk=name).values_list("size", flat=True).first() or 0

    def url(self, name):
        if is_profile_object(name):
            return "/media-profile/" + quote(name)
        # Mirrors MEDIA_URL but routed through ``serve_blob``.
        return "/media-db/" + quote(name)

    def get_available_name(self, name, max_length=None):
        # Always prepare a unique filename; we never overwrite by accident.
        dir_name, file_name = os.path.split(name)
        base, ext = os.path.splitext(file_name)
        candidate = name
        i = 1
        while self.exists(candidate):
            candidate = os.path.join(dir_name, f"{base}_{i}{ext}") if dir_name else f"{base}_{i}{ext}"
            i += 1
        if max_length and len(candidate) > max_length:
            # Truncate base to respect the field's max_length.
            overflow = len(candidate) - max_length
            base = base[:-overflow] if overflow < len(base) else base[:1]
            candidate = os.path.join(dir_name, f"{base}{ext}") if dir_name else f"{base}{ext}"
        return candidate


# Single shared instance — referenced by model fields.
db_storage = DatabaseStorage()


def is_profile_object(name: str) -> bool:
    return bool(re.fullmatch(r"profile-objects/[a-f0-9]{32}\.[a-z0-9]{1,8}", str(name)))


def save_profile_object(name, content):
    """Immutable keys make the phone's existing disk image cache safe to reuse."""
    from chat.media_storage import upload_file
    extension = os.path.splitext(name)[1].lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        extension = "img"
    key = f"profile-objects/{uuid.uuid4().hex}.{extension}"
    content.seek(0)
    upload_file(key=key, fileobj=content,
                mime=getattr(content, "content_type", "") or "application/octet-stream", md5="")
    return key
