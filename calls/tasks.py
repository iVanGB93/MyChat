import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from chat.push import send_call_push
from chat.consumers import record_notification_decision

from .models import CallLog

logger = logging.getLogger(__name__)


@shared_task
def sweep_stale_call_invites() -> dict:
    """Resend push for ringing calls that still have no invite ack."""
    ack_timeout_s = int(getattr(settings, "CALL_INVITE_ACK_TIMEOUT_SECONDS", 12))
    retry_interval_s = int(getattr(settings, "CALL_INVITE_RETRY_INTERVAL_SECONDS", 20))
    now = timezone.now()
    ack_cutoff = now - timedelta(seconds=ack_timeout_s)
    retry_cutoff = now - timedelta(seconds=retry_interval_s)

    qs = (
        CallLog.objects.filter(
            status=CallLog.RINGING,
            invite_acked_at__isnull=True,
            started_at__lte=ack_cutoff,
        )
        .filter(Q(push_sent_at__isnull=True) | Q(push_sent_at__lte=retry_cutoff))
        .select_related("caller")
    )

    rows = list(qs)
    if not rows:
        return {"scanned": 0, "resent": 0, "failed": 0}

    resent = 0
    failed = 0

    for call in rows:
        sent = send_call_push(
            callee_id=call.callee_id,
            caller_name=call.caller.username,
            call_type=call.call_type,
            call_id=str(call.id),
            caller_id=call.caller_id,
            room_name=call.room_name,
            correlation_id=f"call:{call.id}",
            route_reason="push_retry_sweep",
        )
        if sent:
            CallLog.objects.filter(id=call.id).update(push_sent_at=timezone.now())
            record_notification_decision(
                kind="call",
                route="push_sent",
                correlation_id=f"call:{call.id}",
                route_reason="push_retry_sweep",
                sender_id=call.caller_id,
                recipient_id=call.callee_id,
                call_id=str(call.id),
            )
            resent += 1
            logger.info(
                "[CallInviteSweep] push resent call_id=%s caller=%s callee=%s",
                str(call.id),
                call.caller_id,
                call.callee_id,
            )
        else:
            failed += 1
            logger.warning(
                "[CallInviteSweep] push resend failed call_id=%s caller=%s callee=%s",
                str(call.id),
                call.caller_id,
                call.callee_id,
            )

    return {"scanned": len(rows), "resent": resent, "failed": failed}