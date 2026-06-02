"""Database-backed file storage.

Stores uploaded files (currently used for user avatars) as binary blobs in the
application database rather than on the local filesystem.  Useful for ephemeral
hosts (Railway, Heroku, etc.) where the filesystem is wiped on every redeploy.

The storage exposes a ``/media-db/<name>`` URL.  A small view (see
``serve_blob``) streams the bytes back to clients.

Tradeoffs: blobs bloat the database and bypass any CDN.  For large user bases
swap this out for S3 / R2 / GCS.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from django.core.files.base import ContentFile
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
        blob = FileBlob.objects.get(pk=name)
        return ContentFile(bytes(blob.data), name=name)

    def _save(self, name, content):
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
        FileBlob.objects.filter(pk=name).delete()

    def exists(self, name):
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
        return FileBlob.objects.filter(pk=name).values_list("size", flat=True).first() or 0

    def url(self, name):
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
