"""Compact delivery-receipt retention; message content remains on devices."""
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import MessageDelivery


def confirm_sender_receipts(sender_id: int, entries: list[dict]) -> list[dict]:
    accepted = []
    now = timezone.now()
    for entry in entries[:100]:
        if not isinstance(entry, dict):
            continue
        message_id = str(entry.get("message_id") or "")[:64]
        room_id = str(entry.get("room_id") or "")
        recipients = entry.get("recipient_ids")
        if not message_id or not room_id or not isinstance(recipients, list):
            continue
        ids = {int(value) for value in recipients[:100] if str(value).isdigit() and int(value) > 0}
        try:
            pending = set(MessageDelivery.objects.filter(
                sender_id=sender_id, room_id=room_id, message_id=message_id,
                recipient_id__in=ids, status=MessageDelivery.STATUS_PENDING,
            ).values_list("recipient_id", flat=True))
            ids -= pending
            MessageDelivery.objects.filter(
                sender_id=sender_id, room_id=room_id, message_id=message_id,
                recipient_id__in=ids, status=MessageDelivery.STATUS_DELIVERED,
                sender_confirmed_at__isnull=True,
            ).update(sender_confirmed_at=now)
        except (ValueError, TypeError, ValidationError):
            continue
        # Missing rows may already have expired; repeated confirmations are safe.
        accepted.append({"message_id": message_id, "recipient_ids": sorted(ids)})
    return accepted
