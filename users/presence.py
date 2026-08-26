"""Presence aggregation helpers shared by WebSockets and REST serializers."""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import User, UserPresence, UserPresenceSession


def stale_seconds() -> int:
    return int(getattr(settings, "PRESENCE_STALE_SECONDS", 70))


def notification_presence_is_stale(presence: UserPresence | None) -> bool:
    """Use the Axion heartbeat clock, never a legacy room-socket timestamp."""
    if not presence:
        return True
    last_notification_seen = presence.last_notification_seen_at or presence.last_seen
    if not last_notification_seen:
        return True
    return (timezone.now() - last_notification_seen).total_seconds() > stale_seconds()


def effective_presence_is_online(presence: UserPresence | None) -> bool:
    """Return the display/routing truth, never a stale persisted boolean."""
    return bool(
        presence
        and presence.is_online
        and presence.notification_socket_connected
        and presence.app_state == UserPresence.APP_STATE_ACTIVE
        and not notification_presence_is_stale(presence)
    )


def aggregate_user_presence(user_id: int, *, touch_when_empty: bool = False) -> tuple[dict, bool]:
    """Aggregate every fresh Axion lease for one user and mirror the result.

    Returns ``(payload, changed)`` where ``changed`` means the public presence
    state changed and peers should receive an immediate Axion update.
    """
    now = timezone.now()
    cutoff = now - timedelta(seconds=stale_seconds())
    with transaction.atomic():
        presence, _ = UserPresence.objects.get_or_create(user_id=user_id, defaults={"last_seen": now})
        presence = UserPresence.objects.select_for_update().get(pk=presence.pk)
        sessions = list(
            UserPresenceSession.objects.filter(user_id=user_id, last_seen__gte=cutoff)
            .values("app_state", "last_seen")
        )
        count = len(sessions)
        is_online = any(row["app_state"] == UserPresence.APP_STATE_ACTIVE for row in sessions)
        if is_online:
            app_state = UserPresence.APP_STATE_ACTIVE
        elif count:
            app_state = UserPresence.APP_STATE_BACKGROUND
        else:
            app_state = UserPresence.APP_STATE_UNKNOWN

        previous = (
            effective_presence_is_online(presence),
            presence.notification_socket_connected,
            presence.app_state,
        )
        latest = max((row["last_seen"] for row in sessions), default=None)
        last_seen = latest or (now if touch_when_empty else presence.last_seen)
        current = (is_online, count > 0, app_state)
        public_changed = previous != current
        mirror_due = (
            public_changed
            or presence.notification_socket_count != count
            or not presence.last_notification_seen_at
            or (now - presence.last_notification_seen_at).total_seconds() >= 30
        )
        if mirror_due:
            UserPresence.objects.filter(pk=presence.pk).update(
                is_online=is_online,
                notification_socket_connected=count > 0,
                notification_socket_count=count,
                app_state=app_state,
                last_seen=last_seen,
                last_notification_seen_at=last_seen,
                last_app_state_change_at=(now if presence.app_state != app_state else presence.last_app_state_change_at),
            )
            User.objects.filter(pk=user_id).update(is_online=is_online, last_seen=last_seen)
        payload = {
            "user_id": user_id,
            "is_online": is_online,
            "presence": "active" if is_online else ("background" if count else "offline"),
            "last_seen": last_seen.isoformat() if last_seen else None,
            "expires_in": stale_seconds(),
        }
        return payload, public_changed


def build_presence_snapshot(user_ids: Iterable[int]) -> list[dict]:
    """Build one stale-safe presence snapshot without an N+1 query."""
    ids = {int(user_id) for user_id in user_ids if user_id is not None}
    if not ids:
        return []
    now = timezone.now()
    cutoff = now - timedelta(seconds=stale_seconds())
    fresh_rows = list(
        UserPresenceSession.objects.filter(user_id__in=ids, last_seen__gte=cutoff)
        .values("user_id", "app_state")
        .annotate(last_seen=Max("last_seen"))
    )
    by_user: dict[int, list[dict]] = {}
    for row in fresh_rows:
        by_user.setdefault(row["user_id"], []).append(row)
    stored_last_seen = dict(
        UserPresence.objects.filter(user_id__in=ids).values_list("user_id", "last_seen")
    )
    snapshot = []
    for user_id in sorted(ids):
        rows = by_user.get(user_id, [])
        is_online = any(row["app_state"] == UserPresence.APP_STATE_ACTIVE for row in rows)
        connected = bool(rows)
        latest = max((row["last_seen"] for row in rows), default=stored_last_seen.get(user_id))
        snapshot.append({
            "user_id": user_id,
            "is_online": is_online,
            "presence": "active" if is_online else ("background" if connected else "offline"),
            "last_seen": latest.isoformat() if latest else None,
            "expires_in": stale_seconds(),
        })
    return snapshot
