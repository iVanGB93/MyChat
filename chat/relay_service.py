"""Shared database planning for Axonic message relay transports.

The WebSocket and notification-reply HTTP paths remain transport adapters; this
module owns their common membership, recipient and durable delivery work.
"""

from dataclasses import dataclass

from users.models import BlockedUser, UserPresence
from users.presence import notification_presence_is_stale

from .models import ChatRoom, MessageDelivery, PendingDelivery


@dataclass(frozen=True)
class AxionRelayPlan:
    recipient_ids: list[int]
    live_recipient_ids: list[int]
    room_name: str
    sender_avatar: str | None
    offline_email_recipient_ids: list[int]


def build_axion_relay_plan(sender, room_id: str) -> AxionRelayPlan | None:
    # Membership validation and room lookup happen in one query.
    room = (
        ChatRoom.objects.filter(id=room_id, members=sender)
        .only("id", "name", "room_type")
        .first()
    )
    if room is None:
        return None

    recipient_ids = list(
        room.members.exclude(id=sender.id).values_list("id", flat=True)
    )
    blockers = set(
        BlockedUser.objects.filter(
            blocked_id=sender.id,
            owner_id__in=recipient_ids,
        ).values_list("owner_id", flat=True)
    )
    recipient_ids = [user_id for user_id in recipient_ids if user_id not in blockers]
    presences = {
        presence.user_id: presence
        for presence in UserPresence.objects.filter(user_id__in=recipient_ids).only(
            "user_id",
            "notification_socket_connected",
            "app_state",
            "last_notification_seen_at",
            "last_seen",
        )
    }
    live_recipient_ids = [
        user_id
        for user_id in recipient_ids
        if (presence := presences.get(user_id))
        and presence.notification_socket_connected
        and presence.app_state == UserPresence.APP_STATE_ACTIVE
        and not notification_presence_is_stale(presence)
    ]
    room_name = (
        sender.username
        if room.room_type == ChatRoom.DIRECT and not room.name
        else room.name or str(room.id)
    )
    try:
        sender_avatar = sender.avatar.url if sender.avatar and sender.avatar.name else None
    except Exception:
        sender_avatar = None
    live_ids = set(live_recipient_ids)
    return AxionRelayPlan(
        recipient_ids=recipient_ids,
        live_recipient_ids=live_recipient_ids,
        room_name=room_name,
        sender_avatar=sender_avatar,
        offline_email_recipient_ids=[user_id for user_id in recipient_ids if user_id not in live_ids],
    )


def record_pending_deliveries(*, room_id: str, sender_id: int, recipient_ids: list[int]) -> None:
    if not recipient_ids:
        return
    PendingDelivery.objects.bulk_create(
        [
            PendingDelivery(room_id=room_id, from_user_id=sender_id, to_user_id=user_id)
            for user_id in recipient_ids
        ],
        ignore_conflicts=True,
    )


def record_message_deliveries(
    *, room_id: str, message_id: str, sender_id: int, recipient_ids: list[int]
) -> None:
    if not message_id or not recipient_ids:
        return
    MessageDelivery.objects.bulk_create(
        [
            MessageDelivery(
                room_id=room_id,
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=user_id,
                status=MessageDelivery.STATUS_PENDING,
            )
            for user_id in recipient_ids
        ],
        ignore_conflicts=True,
    )


def record_relay_deliveries(
    *, room_id: str, message_id: str, sender_id: int, recipient_ids: list[int]
) -> None:
    """Idempotently create both row types for the synchronous HTTP adapter."""
    record_pending_deliveries(room_id=room_id, sender_id=sender_id, recipient_ids=recipient_ids)
    record_message_deliveries(
        room_id=room_id,
        message_id=message_id,
        sender_id=sender_id,
        recipient_ids=recipient_ids,
    )
