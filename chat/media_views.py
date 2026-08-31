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

Storage is Postgres (`chat.MediaBlob.data`) today. All access goes through this
module so the backing store can be swapped for object storage (S3 / R2) later
without changing the wire protocol or the app.
"""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.http import Http404, StreamingHttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ChatRoom, MediaBlob, MediaDownload

STREAM_CHUNK = 64 * 1024
ALLOWED_MEDIA_TYPES = {"image", "voice", "video", "document"}


def _max_upload_bytes() -> int:
    return int(getattr(settings, "MEDIA_MAX_UPLOAD_BYTES", 250 * 1024 * 1024))


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

    room_id = request.data.get("room_id")
    try:
        room = ChatRoom.objects.get(id=room_id)
    except (ChatRoom.DoesNotExist, ValueError, TypeError):
        return Response({"error": "room not found"}, status=404)
    if not _is_member(request.user, room):
        return Response({"error": "not a room member"}, status=403)

    max_bytes = _max_upload_bytes()
    if f.size and f.size > max_bytes:
        return Response({"error": "file too large", "max_bytes": max_bytes}, status=413)

    # Stream the upload, hashing as we go and bounding the total size.
    hasher = hashlib.sha256()
    md5_hasher = hashlib.md5()
    buf = bytearray()
    total = 0
    for chunk in f.chunks(STREAM_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            return Response({"error": "file too large", "max_bytes": max_bytes}, status=413)
        hasher.update(chunk)
        md5_hasher.update(chunk)
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
        if existing.sha256 != digest or existing.media_type != media_type:
            return Response({"error": "message_id already belongs to another file"}, status=409)
        return Response(
            {
                "media_id": str(existing.id),
                "sha256": existing.sha256,
                "md5": existing.md5,
                "size_bytes": existing.size_bytes,
                "mime": existing.mime,
                "reused": True,
            },
            status=200,
        )

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
        data=bytes(buf),
    )
    return Response(
        {
            "media_id": str(blob.id),
            "sha256": digest,
            "md5": md5_digest,
            "size_bytes": total,
            "mime": blob.mime,
        },
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
