import logging
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import MessageDelivery, ChatRoom
from .push import send_message_push

logger = logging.getLogger(__name__)


@shared_task
def sweep_stale_message_deliveries() -> dict:
    """Push fallback for deliveries still pending after ack timeout.

    This task makes delivery fallback resilient to worker/app restarts by
    periodically checking pending per-message delivery metadata.
    """
    timeout_s = int(getattr(settings, "MESSAGE_ACK_TIMEOUT_SECONDS", 8))
    retry_s = int(getattr(settings, "MESSAGE_DELIVERY_PUSH_RETRY_SECONDS", 300))
    max_attempts = int(getattr(settings, "MESSAGE_DELIVERY_PUSH_MAX_ATTEMPTS", 3))
    now = timezone.now()
    cutoff = now - timedelta(seconds=timeout_s)
    retry_cutoff = now - timedelta(seconds=retry_s)

    rows = list(
        MessageDelivery.objects.filter(
            status=MessageDelivery.STATUS_PENDING,
            push_sent_at__isnull=True,
            created_at__lte=cutoff,
            push_attempt_count__lt=max_attempts,
        )
        .filter(Q(last_push_attempt_at__isnull=True) | Q(last_push_attempt_at__lte=retry_cutoff))
        .select_related("sender", "room")
        .order_by("room_id", "message_id", "sender_id", "recipient_id")
    )

    if not rows:
        return {
            "groups": 0,
            "rows_scanned": 0,
            "rows_marked": 0,
            "rows_skipped": 0,
        }

    grouped: dict[tuple[str, str, int], list[MessageDelivery]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.room_id), row.message_id, row.sender_id)].append(row)

    marked = 0
    skipped = 0

    for (room_id_str, message_id, sender_id), deliveries in grouped.items():
        delivery_ids = [d.id for d in deliveries]
        # Reserve the retry before contacting FCM. This shares the same durable
        # attempt state as the realtime consumer, so duplicate Axion frames and
        # overlapping Beat sweeps cannot repeatedly notify the same recipient.
        with transaction.atomic():
            reserved = MessageDelivery.objects.select_for_update().filter(
                id__in=delivery_ids,
                status=MessageDelivery.STATUS_PENDING,
                push_sent_at__isnull=True,
                push_attempt_count__lt=max_attempts,
            ).filter(
                Q(last_push_attempt_at__isnull=True)
                | Q(last_push_attempt_at__lte=retry_cutoff)
            )
            reserved_ids = list(reserved.values_list("id", flat=True))
            if not reserved_ids:
                continue
            reserved.update(
                last_push_attempt_at=now,
                push_attempt_count=F("push_attempt_count") + 1,
            )

        recipient_ids = [
            d.recipient_id for d in deliveries if d.id in reserved_ids
        ]
        sender_name = deliveries[0].sender.username
        room = deliveries[0].room
        if room.room_type == ChatRoom.DIRECT and not room.name:
            room_name = sender_name
        else:
            room_name = room.name or room_id_str

        sent = send_message_push(
            recipient_ids=recipient_ids,
            sender_name=sender_name,
            content="New message waiting",
            room_id=room_id_str,
            room_name=room_name,
            correlation_id=f"msg:{message_id}",
            route_reason="push_stale_sweep",
        )

        if not sent:
            skipped += len(reserved_ids)
            logger.warning(
                "[DeliverySweep] push unavailable room=%s sender=%s message_id=%s recipients=%d; retry is backed off",
                room_id_str,
                sender_id,
                message_id,
                len(recipient_ids),
            )
            continue

        updated = MessageDelivery.objects.filter(
            id__in=reserved_ids,
            push_sent_at__isnull=True,
        ).update(push_sent_at=now)
        marked += updated
        logger.info(
            "[DeliverySweep] fallback push sent room=%s sender=%s message_id=%s recipients=%d marked=%d",
            room_id_str,
            sender_id,
            message_id,
            len(recipient_ids),
            updated,
        )

    return {
        "groups": len(grouped),
        "rows_scanned": len(rows),
        "rows_marked": marked,
        "rows_skipped": skipped,
    }


@shared_task
def cleanup_message_delivery_metadata() -> dict:
    """Bound server-side delivery metadata while clients retain chat history."""
    retention_days = int(getattr(settings, "MESSAGE_DELIVERY_RETENTION_DAYS", 30))
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _ = MessageDelivery.objects.filter(created_at__lt=cutoff).delete()
    if deleted:
        logger.info("[DeliveryCleanup] deleted %d expired metadata row(s)", deleted)
    return {"deleted": deleted, "retention_days": retention_days}


@shared_task
def cleanup_expired_media() -> dict:
    """Delete media blobs that are past retention.

    Two cases:
      1. All recipients confirmed a verified download and the grace window
         (`delete_after`) has elapsed.
      2. Hard-TTL fallback: the blob is older than MEDIA_HARD_TTL_DAYS regardless
         of confirmation (covers recipients that uninstalled / never downloaded).

    Clients keep the thumbnail + metadata in their own local message row, so the
    chat history stays intact after the server blob is gone.
    """
    from .media_storage import abort_multipart_upload, delete_object
    from .models import MediaBlob

    now = timezone.now()
    hard_ttl_days = int(getattr(settings, "MEDIA_HARD_TTL_DAYS", 30))
    hard_cutoff = now - timedelta(days=hard_ttl_days)

    def delete_rows(queryset) -> int:
        deleted = 0
        for blob in queryset.iterator():
            if blob.storage_backend == "spaces" and blob.object_key:
                try:
                    if blob.multipart_upload_id:
                        abort_multipart_upload(
                            key=blob.object_key,
                            upload_id=blob.multipart_upload_id,
                        )
                    delete_object(blob.object_key)
                except Exception:
                    logger.exception(
                        "[MediaCleanup] object deletion failed media=%s key=%s",
                        blob.id,
                        blob.object_key,
                    )
                    # Keep the row so the next sweep retries object deletion.
                    continue
            blob.delete()
            deleted += 1
        return deleted

    confirmed_qs = MediaBlob.objects.filter(
        delete_after__isnull=False, delete_after__lte=now
    )
    confirmed_deleted = delete_rows(confirmed_qs)

    stale_qs = MediaBlob.objects.filter(created_at__lt=hard_cutoff)
    stale_deleted = delete_rows(stale_qs)

    total = confirmed_deleted + stale_deleted
    if total:
        logger.info(
            "[MediaCleanup] deleted %d blob(s) (confirmed=%d, hard_ttl=%d)",
            total,
            confirmed_deleted,
            stale_deleted,
        )
    return {
        "deleted": total,
        "confirmed": confirmed_deleted,
        "hard_ttl": stale_deleted,
    }
