"""Connection metadata for the opt-in text prototype; never a message relay."""

import json
from uuid import UUID

from django.db.models import Q

from users.models import BlockedUser
from .models import ChatRoom

MAX_SIGNAL_BYTES = 48_000


def parse_text_signal(data):
    if not isinstance(data, dict) or type(data.get("protocol")) is not int or data["protocol"] != 1:
        return None
    if len(json.dumps(data).encode("utf-8")) > MAX_SIGNAL_BYTES:
        return None
    kind = data.get("signal_type")
    if not isinstance(kind, str) or kind not in {"offer", "answer", "ice", "close"}:
        return None
    target = data.get("target_user_id")
    if type(target) is not int or target <= 0:
        return None
    try:
        room = str(UUID(str(data.get("room_id"))))
        session = str(UUID(str(data.get("session_id"))))
        endpoint = data.get("target_endpoint_id")
        endpoint = str(UUID(str(endpoint))) if endpoint else None
    except (ValueError, TypeError, AttributeError):
        return None
    if kind != "offer" and not endpoint:
        return None
    body = data.get("data")
    if not isinstance(body, dict):
        return None
    if kind in {"offer", "answer"}:
        sdp = body.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp) > 32_000:
            return None
        body = {"type": kind, "sdp": sdp}
    elif kind == "ice":
        candidate, mid, index = body.get("candidate"), body.get("sdpMid"), body.get("sdpMLineIndex")
        if not isinstance(candidate, str) or not candidate or len(candidate) > 2048:
            return None
        if mid is not None and (not isinstance(mid, str) or len(mid) > 128):
            return None
        if index is not None and (type(index) is not int or not 0 <= index <= 16):
            return None
        body = {"candidate": candidate, "sdpMid": mid, "sdpMLineIndex": index}
    else:
        body = {}
    return {"protocol": 1, "room_id": room, "session_id": session,
            "signal_type": kind, "target_user_id": target,
            "target_endpoint_id": endpoint, "data": body}


def authorize_text_signal(sender_id, signal):
    """Both users must still be the only members of an unblocked direct room."""
    target_id = signal["target_user_id"]
    if target_id == sender_id:
        return False
    room = ChatRoom.objects.filter(id=signal["room_id"], room_type=ChatRoom.DIRECT).first()
    if room is None or set(room.members.values_list("id", flat=True)) != {sender_id, target_id}:
        return False
    return not BlockedUser.objects.filter(
        Q(owner_id=sender_id, blocked_id=target_id) | Q(owner_id=target_id, blocked_id=sender_id)
    ).exists()
