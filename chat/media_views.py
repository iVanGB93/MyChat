"""
Out-of-band media transfer endpoints (image / voice / video).

Large blobs never ride the chat WebSocket. Instead:
  1. The sender uploads the blob here (multipart HTTP) → gets a `media_id`.
  2. The sender sends a lightweight *pointer* chat message over the WS text lane
     carrying only `media_id + sha256 + thumb + metadata` (no base64).
  3. Each receiver downloads the blob here (HTTP), verifies the sha256, persists
     it to durable device storage, then confirms the download.
  4. Once every recipient has confirmed, the blob is scheduled for deletion after
     a grace window (see chat.tasks.cleanup_expired_media).

Production uploads go directly from the phone to DigitalOcean Spaces using a
short-lived signed URL. Legacy database blobs and older app versions remain
supported by the original multipart endpoint.
"""

import hashlib
import logging
import math
import re
from datetime import timedelta

from django.conf import settings
from django.http import Http404, HttpResponseRedirect, StreamingHttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ChatRoom, MediaBlob, MediaDownload
from .media_storage import (
    complete_multipart_upload,
    create_multipart_upload,
    create_presigned_download,
    create_presigned_part_upload,
    create_presigned_upload,
    inspect_object,
    list_multipart_parts,
    object_key_for,
    upload_file,
    uses_spaces,
)

STREAM_CHUNK = 64 * 1024
ALLOWED_MEDIA_TYPES = {"image", "voice", "video", "document"}
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
logger = logging.getLogger(__name__)


def _max_upload_bytes() -> int:
    return int(getattr(settings, "MEDIA_MAX_UPLOAD_BYTES", 250 * 1024 * 1024))


def _multipart_threshold_bytes() -> int:
    return int(getattr(settings, "MEDIA_MULTIPART_THRESHOLD_BYTES", 16 * 1024 * 1024))


def _multipart_part_bytes() -> int:
    return max(5 * 1024 * 1024, int(getattr(settings, "MEDIA_MULTIPART_PART_BYTES", 8 * 1024 * 1024)))


def _missing_multipart_upload(error) -> bool:
    response = getattr(error, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code", ""))
    return code in {"NoSuchUpload", "404"}


def _grace_hours() -> int:
    return int(getattr(settings, "MEDIA_DELETE_GRACE_HOURS", 48))


def _is_member(user, room) -> bool:
    return room.members.filter(id=user.id).exists()


def _get_int(data, name):
    v = data.get(name)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _media_payload(blob, *, reused=False):
    payload = {
        "media_id": str(blob.id),
        "sha256": blob.sha256,
        "md5": blob.md5,
        "size_bytes": blob.size_bytes,
        "mime": blob.mime,
    }
    if reused:
        payload["reused"] = True
    return payload


def _get_room_for_upload(request):
    room_id = request.data.get("room_id")
    try:
        room = ChatRoom.objects.get(id=room_id)
    except (ChatRoom.DoesNotExist, ValueError, TypeError):
        return None, Response({"error": "room not found"}, status=404)
    if not _is_member(request.user, room):
        return None, Response({"error": "not a room member"}, status=403)
    return room, None


def _prepare_multipart(blob):
    """Return signed URLs only for parts Spaces does not already contain."""
    if not blob.multipart_part_size:
        blob.multipart_part_size = _multipart_part_bytes()
    if not blob.multipart_upload_id:
        blob.multipart_upload_id = create_multipart_upload(
            key=blob.object_key, mime=blob.mime, md5=blob.md5
        )
        blob.save(update_fields=["multipart_upload_id", "multipart_part_size"])

    try:
        uploaded = list_multipart_parts(
            key=blob.object_key, upload_id=blob.multipart_upload_id
        )
    except Exception as error:
        if not _missing_multipart_upload(error):
            raise
        # Spaces may expire abandoned multipart sessions. Start a replacement
        # while preserving the MediaBlob/message idempotency identity.
        blob.multipart_upload_id = create_multipart_upload(
            key=blob.object_key, mime=blob.mime, md5=blob.md5
        )
        blob.save(update_fields=["multipart_upload_id", "multipart_part_size"])
        uploaded = []

    uploaded_numbers = {int(part["PartNumber"]) for part in uploaded}
    part_count = math.ceil(blob.size_bytes / blob.multipart_part_size) if blob.size_bytes else 1
    parts = []
    for part_number in range(1, part_count + 1):
        part = {"part_number": part_number, "uploaded": part_number in uploaded_numbers}
        if not part["uploaded"]:
            part["upload_url"] = create_presigned_part_upload(
                key=blob.object_key,
                upload_id=blob.multipart_upload_id,
                part_number=part_number,
            )
        parts.append(part)
    return {
        "upload_mode": "multipart",
        "part_size": blob.multipart_part_size,
        "parts": parts,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def initiate_media_upload(request):
    """Create an authenticated, idempotent direct-to-Spaces upload."""
    if not uses_spaces():
        return Response(
            {"error": "direct media upload is not enabled", "direct_upload": False},
            status=409,
        )

    media_type = (request.data.get("media_type") or "").lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        return Response({"error": "invalid media_type"}, status=400)
    room, error = _get_room_for_upload(request)
    if error is not None:
        return error

    size_bytes = _get_int(request.data, "size_bytes")
    if size_bytes is None or size_bytes < 0:
        return Response({"error": "invalid size_bytes"}, status=400)
    max_bytes = _max_upload_bytes()
    if size_bytes > max_bytes:
        return Response({"error": "file too large", "max_bytes": max_bytes}, status=413)

    md5 = (request.data.get("md5") or "").strip().lower()
    if not MD5_RE.fullmatch(md5):
        return Response({"error": "valid md5 is required"}, status=400)
    mime = (request.data.get("mime") or "application/octet-stream")[:80]
    message_id = (request.data.get("message_id") or "")[:64]
    if not message_id:
        return Response({"error": "message_id is required"}, status=400)

    existing = MediaBlob.objects.filter(
        room=room, owner=request.user, message_id=message_id
    ).order_by("created_at").first()
    if existing is not None:
        if (
            existing.md5 != md5
            or existing.media_type != media_type
            or existing.size_bytes != size_bytes
        ):
            return Response({"error": "message_id already belongs to another file"}, status=409)
        if existing.storage_backend == "database" or existing.upload_completed_at is not None:
            return Response({**_media_payload(existing, reused=True), "uploaded": True})
        blob = existing
    else:
        blob = MediaBlob.objects.create(
            room=room,
            owner=request.user,
            message_id=message_id,
            media_type=media_type,
            mime=mime,
            size_bytes=size_bytes,
            sha256="",
            md5=md5,
            duration_ms=_get_int(request.data, "duration_ms"),
            width=_get_int(request.data, "width"),
            height=_get_int(request.data, "height"),
            data=None,
            storage_backend="spaces",
        )
        blob.object_key = object_key_for(blob)
        blob.save(update_fields=["object_key"])

    try:
        if blob.size_bytes >= _multipart_threshold_bytes():
            upload_plan = _prepare_multipart(blob)
        else:
            signed = create_presigned_upload(key=blob.object_key, mime=blob.mime, md5=blob.md5)
            upload_plan = {
                "upload_mode": "single",
                "upload_url": signed["url"],
                "upload_headers": signed["headers"],
            }
    except Exception:
        logger.exception("[Media] could not create upload URL media=%s", blob.id)
        return Response({"error": "media storage is temporarily unavailable"}, status=503)

    return Response(
        {
            **_media_payload(blob, reused=existing is not None),
            "uploaded": False,
            **upload_plan,
        },
        status=200 if existing is not None else 201,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def complete_media_upload(request, media_id):
    """Verify a direct upload exists with the expected immutable metadata."""
    try:
        blob = MediaBlob.objects.get(id=media_id, owner=request.user)
    except (MediaBlob.DoesNotExist, ValueError, TypeError):
        raise Http404
    if blob.storage_backend != "spaces" or not blob.object_key:
        return Response({"error": "not a direct upload"}, status=409)
    if blob.upload_completed_at is not None:
        return Response({**_media_payload(blob, reused=True), "uploaded": True})

    remote = None
    try:
        remote = inspect_object(blob.object_key)
    except Exception:
        # An in-progress multipart upload is not visible to HEAD until it is
        # completed. List authoritative parts server-side so clients do not
        # need to persist or expose S3 ETags.
        if blob.multipart_upload_id and blob.multipart_part_size:
            try:
                parts = sorted(
                    list_multipart_parts(
                        key=blob.object_key, upload_id=blob.multipart_upload_id
                    ),
                    key=lambda part: int(part["PartNumber"]),
                )
                expected_count = math.ceil(blob.size_bytes / blob.multipart_part_size) if blob.size_bytes else 1
                if [int(part["PartNumber"]) for part in parts] != list(range(1, expected_count + 1)):
                    return Response({"error": "multipart upload is incomplete"}, status=409)
                if sum(int(part.get("Size", 0)) for part in parts) != blob.size_bytes:
                    return Response({"error": "multipart upload size mismatch"}, status=400)
                complete_multipart_upload(
                    key=blob.object_key,
                    upload_id=blob.multipart_upload_id,
                    parts=parts,
                )
                remote = inspect_object(blob.object_key)
            except Exception:
                logger.exception("[Media] could not complete multipart upload media=%s", blob.id)
                return Response({"error": "upload not found or storage unavailable"}, status=503)
        else:
            logger.exception("[Media] could not verify upload media=%s", blob.id)
            return Response({"error": "upload not found or storage unavailable"}, status=503)

    remote_size = int(remote.get("ContentLength", -1))
    remote_md5 = str((remote.get("Metadata") or {}).get("md5", "")).lower()
    if remote_size != blob.size_bytes or remote_md5 != blob.md5:
        return Response({"error": "uploaded file verification failed"}, status=400)

    blob.upload_completed_at = timezone.now()
    blob.multipart_upload_id = ""
    blob.save(update_fields=["upload_completed_at", "multipart_upload_id"])
    return Response({**_media_payload(blob), "uploaded": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_media(request):
    """
    Multipart upload of a media blob.

    Form fields:
      file        (required) the binary blob
      room_id     (required) the room this media belongs to
      media_type  (required) 'image' | 'voice' | 'video' | 'document'
      mime        (optional) content-type; falls back to the file's content type
      sha256      (optional) client-computed hex digest for integrity check
      duration_ms, width, height, message_id  (optional)

    Returns 201: { media_id, sha256, size_bytes, mime }
    """
    f = request.FILES.get("file")
    if f is None:
        return Response({"error": "file is required"}, status=400)

    media_type = (request.data.get("media_type") or "").lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        return Response({"error": "invalid media_type"}, status=400)

    room, error = _get_room_for_upload(request)
    if error is not None:
        return error

    max_bytes = _max_upload_bytes()
    if f.size and f.size > max_bytes:
        return Response({"error": "file too large", "max_bytes": max_bytes}, status=413)

    # Stream the upload, hashing as we go and bounding the total size.
    hasher = hashlib.sha256()
    md5_hasher = hashlib.md5()
    store_in_database = not uses_spaces()
    buf = bytearray() if store_in_database else None
    total = 0
    for chunk in f.chunks(STREAM_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            return Response({"error": "file too large", "max_bytes": max_bytes}, status=413)
        hasher.update(chunk)
        md5_hasher.update(chunk)
        if buf is not None:
            buf.extend(chunk)

    digest = hasher.hexdigest()
    md5_digest = md5_hasher.hexdigest()
    client_sha = (request.data.get("sha256") or "").lower()
    if client_sha and client_sha != digest:
        return Response({"error": "sha256 mismatch"}, status=400)
    client_md5 = (request.data.get("md5") or "").lower()
    if client_md5 and client_md5 != md5_digest:
        return Response({"error": "md5 mismatch"}, status=400)

    mime = (request.data.get("mime") or f.content_type or "application/octet-stream")[:80]

    message_id = (request.data.get("message_id") or "")[:64]
    # A mobile upload can finish on the server while its HTTP response is lost.
    # Reusing the first blob for the same client message makes retries idempotent
    # instead of filling Postgres with duplicate bytes.
    existing = None
    if message_id:
        existing = MediaBlob.objects.filter(
            room=room,
            owner=request.user,
            message_id=message_id,
        ).order_by("created_at").first()
    if existing is not None:
        digest_conflicts = bool(existing.sha256) and existing.sha256 != digest
        if digest_conflicts or existing.md5 != md5_digest or existing.media_type != media_type:
            return Response({"error": "message_id already belongs to another file"}, status=409)
        if existing.storage_backend == "database" or existing.upload_completed_at is not None:
            return Response(_media_payload(existing, reused=True), status=200)

        # An older client may retry through this endpoint after a direct upload
        # was prepared but never completed. Finish the same idempotent object.
        try:
            f.seek(0)
            upload_file(
                key=existing.object_key,
                fileobj=f,
                mime=existing.mime,
                md5=existing.md5,
            )
        except Exception:
            logger.exception("[Media] compatibility upload failed media=%s", existing.id)
            return Response({"error": "media storage is temporarily unavailable"}, status=503)
        existing.upload_completed_at = timezone.now()
        existing.save(update_fields=["upload_completed_at"])
        return Response(_media_payload(existing, reused=True), status=200)

    blob = MediaBlob.objects.create(
        room=room,
        owner=request.user,
        message_id=message_id,
        media_type=media_type,
        mime=mime,
        size_bytes=total,
        sha256=digest,
        md5=md5_digest,
        duration_ms=_get_int(request.data, "duration_ms"),
        width=_get_int(request.data, "width"),
        height=_get_int(request.data, "height"),
        data=bytes(buf) if buf is not None else None,
        storage_backend="database" if store_in_database else "spaces",
        upload_completed_at=timezone.now() if store_in_database else None,
    )
    if not store_in_database:
        blob.object_key = object_key_for(blob)
        blob.save(update_fields=["object_key"])
        try:
            f.seek(0)
            upload_file(key=blob.object_key, fileobj=f, mime=blob.mime, md5=blob.md5)
        except Exception:
            logger.exception("[Media] compatibility upload failed media=%s", blob.id)
            blob.delete()
            return Response({"error": "media storage is temporarily unavailable"}, status=503)
        blob.upload_completed_at = timezone.now()
        blob.save(update_fields=["upload_completed_at"])
    return Response(
        _media_payload(blob),
        status=201,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_media(request, media_id):
    """Stream a media blob. Only room members (or the owner) may download."""
    try:
        blob = MediaBlob.objects.select_related("room").get(id=media_id)
    except (MediaBlob.DoesNotExist, ValueError, TypeError):
        raise Http404
    if not (blob.owner_id == request.user.id or _is_member(request.user, blob.room)):
        return Response({"error": "forbidden"}, status=403)

    if blob.storage_backend == "spaces" and blob.object_key:
        if blob.upload_completed_at is None:
            return Response({"error": "upload is not complete"}, status=409)
        disposition_type = "attachment" if blob.media_type == "document" else "inline"
        disposition = f'{disposition_type}; filename="{blob.id}"'
        try:
            url = create_presigned_download(key=blob.object_key, disposition=disposition)
        except Exception:
            logger.exception("[Media] could not create download URL media=%s", blob.id)
            return Response({"error": "media storage is temporarily unavailable"}, status=503)
        return HttpResponseRedirect(url)

    if blob.data is None:
        raise Http404
    data = bytes(blob.data)

    def _iter():
        for i in range(0, len(data), STREAM_CHUNK):
            yield data[i : i + STREAM_CHUNK]

    resp = StreamingHttpResponse(_iter(), content_type=blob.mime)
    resp["Content-Length"] = str(blob.size_bytes)
    resp["X-Media-Sha256"] = blob.sha256
    resp["X-Media-Md5"] = blob.md5
    disposition = "attachment" if blob.media_type == "document" else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="{blob.id}"'
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm_media_downloaded(request, media_id):
    """
    Record that the caller (a recipient device) has downloaded + verified +
    persisted the blob. When every recipient has confirmed, schedule deletion
    after the grace window.

    Body: { installation_id?: str }
    Returns: { ok, all_confirmed }
    """
    try:
        blob = MediaBlob.objects.select_related("room").get(id=media_id)
    except (MediaBlob.DoesNotExist, ValueError, TypeError):
        # Blob may already have been cleaned up — treat as success so the client
        # stops retrying.
        return Response({"ok": True, "all_confirmed": True, "expired": True})
    if not _is_member(request.user, blob.room):
        return Response({"error": "forbidden"}, status=403)

    installation_id = (request.data.get("installation_id") or "")[:128]
    MediaDownload.objects.get_or_create(
        media=blob,
        recipient=request.user,
        installation_id=installation_id,
    )

    all_confirmed = _recompute_all_confirmed(blob)
    return Response({"ok": True, "all_confirmed": all_confirmed})


def _recompute_all_confirmed(blob) -> bool:
    """True when every recipient (room members except owner) has >=1 confirmed
    device. On the first time it becomes true, stamp the deletion window."""
    recipient_ids = set(
        blob.room.members.exclude(id=blob.owner_id).values_list("id", flat=True)
    )
    if recipient_ids:
        downloaded_ids = set(blob.downloads.values_list("recipient_id", flat=True))
        confirmed = recipient_ids.issubset(downloaded_ids)
    else:
        confirmed = True

    if confirmed and blob.delete_after is None:
        now = timezone.now()
        MediaBlob.objects.filter(id=blob.id, all_confirmed_at__isnull=True).update(
            all_confirmed_at=now,
            delete_after=now + timedelta(hours=_grace_hours()),
        )
    return confirmed
