import logging
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from django.conf import settings
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
    cutoff = timezone.now() - timedelta(seconds=timeout_s)

    rows = list(
        MessageDelivery.objects.filter(
            status=MessageDelivery.STATUS_PENDING,
            push_sent_at__isnull=True,
            created_at__lte=cutoff,
        )
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

    now = timezone.now()
    marked = 0
    skipped = 0

    for (room_id_str, message_id, sender_id), deliveries in grouped.items():
        recipient_ids = [d.recipient_id for d in deliveries]
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
            skipped += len(deliveries)
            logger.warning(
                "[DeliverySweep] push send failed room=%s sender=%s message_id=%s recipients=%d",
                room_id_str,
                sender_id,
                message_id,
                len(recipient_ids),
            )
            continue

        updated = MessageDelivery.objects.filter(
            id__in=[d.id for d in deliveries],
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
    from .media_storage import delete_object
    from .models import MediaBlob

    now = timezone.now()
    hard_ttl_days = int(getattr(settings, "MEDIA_HARD_TTL_DAYS", 30))
    hard_cutoff = now - timedelta(days=hard_ttl_days)

    def delete_rows(queryset) -> int:
        deleted = 0
        for blob in queryset.iterator():
            if blob.storage_backend == "spaces" and blob.object_key:
                try:
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
