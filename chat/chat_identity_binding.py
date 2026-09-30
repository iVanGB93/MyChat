"""Bounded public identity metadata over the existing authenticated Axion channel.

Clients verify signatures and immutable pins. This endpoint is not a key registry
and does not persist identities, keys, or messages.
"""
import json
import re
import time
from uuid import UUID


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def _account(value):
    return isinstance(value, str) and value.startswith("axonic:1:") and _hex(value[9:], 64)


def _record(r):
    if not isinstance(r, dict) or set(r) != {
        "version", "account", "root", "revision", "previous", "issuedAt", "expiresAt", "devices", "signature"
    }:
        return False
    devices = r["devices"]
    return (type(r["version"]) is int and r["version"] == 1 and _account(r["account"])
            and _hex(r["root"], 64) and _integer(r["revision"])
            and (r["previous"] is None if r["revision"] == 0 else _hex(r["previous"], 64))
            and _integer(r["issuedAt"]) and _integer(r["expiresAt"])
            and r["issuedAt"] < r["expiresAt"] and _hex(r["signature"], 128)
            and isinstance(devices, list) and 1 <= len(devices) <= 8
            and all(isinstance(d, dict) and set(d) == {"id", "signing", "encryption"}
                    and all(_hex(v, 64) for v in d.values()) for d in devices))


def parse_chat_binding(data, sender_id, now=None):
    if not isinstance(data, dict) or type(data.get("protocol")) is not int or data["protocol"] != 1:
        return None
    try:
        if len(json.dumps(data, allow_nan=False).encode("utf-8")) > 12_000:
            return None
        kind, target, payload = data.get("kind"), data.get("target_user_id"), data.get("payload")
        if kind not in ("challenge", "proof") or not _integer(target) or target < 1 or target == sender_id or not isinstance(payload, dict):
            return None
        c = payload if kind == "challenge" else payload.get("challenge")
        if not isinstance(c, dict) or set(c) != {"version", "roomId", "requester", "requesterAccount", "peer", "nonce", "expiresAt"}:
            return None
        if type(c["version"]) is not int or c["version"] != 1 or not _account(c["requesterAccount"]) or not _hex(c["nonce"], 64):
            return None
        if not _integer(c["requester"]) or not _integer(c["peer"]) or not _integer(c["expiresAt"]):
            return None
        room = str(UUID(c["roomId"]))
        if c["roomId"] != room or data.get("room_id") != room:
            return None
        now = int(time.time() * 1000) if now is None else now
        if not now < c["expiresAt"] <= now + 60_000:
            return None
        if (c["requester"], c["peer"]) != ((sender_id, target) if kind == "challenge" else (target, sender_id)):
            return None
        if kind == "proof" and (set(payload) != {"version", "challenge", "record", "device", "signature"}
                or type(payload["version"]) is not int or payload["version"] != 1 or not _record(payload["record"])
                or not _hex(payload["device"], 64) or not _hex(payload["signature"], 128)):
            return None
        return {"protocol": 1, "kind": kind, "room_id": room, "target_user_id": target, "payload": payload}
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        return None
