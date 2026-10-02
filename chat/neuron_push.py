"""Transitional server-to-server call push. Never log device tokens or secrets."""
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import urlsplit

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_neuron_call_push(tokens, data):
    """None means disabled. Ambiguous sends count as handled, never retried locally.

    This preserves the caller's existing boolean contract: handled is not proof
    that the recipient rang. Delivery receipts remain a separate responsibility.
    """
    return _send_neuron_push(tokens, data, message=False)


def send_neuron_message_push(tokens, data):
    """Delegate ordinary message notifications; silent recovery stays separate."""
    return _send_neuron_push(tokens, data, message=True)


def _send_neuron_push(tokens, data, *, message):
    endpoint = getattr(settings, "NEURON_MESSAGE_PUSH_URL" if message else "NEURON_CALL_PUSH_URL", "")
    if not endpoint:
        return None
    secret = getattr(settings, "NEURON_PUSH_BRIDGE_SECRET", "")
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != ("/v1/message-push" if message else "/v1/call-push")
            or len(secret) != 64 or any(c not in "0123456789abcdef" for c in secret)):
        logger.error("[Neuron push] Invalid bridge configuration")
        return False
    values = {k: "" if v is None else str(v) for k, v in data.items()
              if k not in {"from", "message_type", "notification"} and not k.startswith(("google", "gcm"))}
    values["channelId"] = "messages" if message else "incoming-calls-v2"
    if message and not _bridge_message_supported(values):
        # No remote request has been made: safely preserve legacy delivery for
        # old payload shapes or payloads that do not fit the gateway contract.
        return None
    prefix = "recovery:" if values.get("type") == "message_recovery_hint" else "message:"
    event_id = (prefix + str(values.get("messageId", ""))) if message else values.get("callId", "")
    if not event_id or (message and not values.get("messageId")):
        return False
    handled = False
    # One id per call/token: an HTTP retry cannot produce a second push.
    expires = int(time.time() * 1000) + 45000
    targets = {}
    if getattr(settings, "NEURON_REGISTERED_PUSH_ENABLED", False):
        from users.models import UserDevice
        for device in UserDevice.objects.filter(is_active=True, fcm_token__in=tokens):
            targets[device.fcm_token] = {"user": device.user_id, "installation": device.installation_id}
    for token in dict.fromkeys(tokens):
        if not token or int(time.time() * 1000) >= expires:
            continue
        raw = json.dumps({
            "version": 2 if token in targets else 1,
            **({"target": targets[token]} if token in targets else {}),
            "id": hashlib.sha256((event_id + "\0" + token).encode()).hexdigest(),
            "expiresAt": expires, "token": token, "data": values,
        }, separators=(",", ":"), ensure_ascii=False).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        try:
            response = requests.post(endpoint, data=raw, headers={
                "Content-Type": "application/json", "X-Axonic-Push-Signature": signature,
            }, timeout=(3, 12), allow_redirects=False,
                verify=getattr(settings, "NEURON_PUSH_CA_BUNDLE", "") or True)
            result = response.json() if response.status_code == 200 else {
                "outcome": "not_sent" if 400 <= response.status_code < 500 else "unknown"
            }
            outcome = result.get("outcome", "unknown")
            if outcome == "invalid_token":
                from users.models import UserDevice
                UserDevice.objects.filter(fcm_token=token).update(fcm_token="")
            handled = handled or outcome in ("sent", "unknown")
            logger.info("[Neuron push] Push outcome=%s", outcome if outcome in (
                "sent", "unknown", "not_sent", "invalid_token") else "unrecognized")
        except Exception:
            # The remote side may already have handed this invitation to FCM.
            handled = True
            logger.warning("[Neuron push] Push outcome unknown; no duplicate fallback")
    return handled


def _bridge_message_supported(data):
    recovery = data.get("type") == "message_recovery_hint"
    if recovery and set(data) - {"type", "roomId", "room_id", "messageId", "message_id", "senderId", "sender_id", "channelId"}:
        return False
    forbidden = {"audio_b64", "image_b64", "video_b64", "file_b64", "audio", "image",
                 "video", "file", "thumbnail_b64", "waveform"}
    return (
        data.get("type") in ("new_message", "message_recovery_hint")
        and all(re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", data.get(k, "")) for k in ("messageId", "roomId"))
        and bool(re.fullmatch(r"[1-9][0-9]{0,14}", data.get("senderId", "")))
        and all(not data.get(alias) or data[alias] == data[key] for alias, key in (
            ("message_id", "messageId"), ("room_id", "roomId"), ("sender_id", "senderId")))
        and len(data) <= 64
        and all(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", k) and k not in forbidden
                and len(v.encode("utf-16-le")) // 2 <= 2000 for k, v in data.items())
        and len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()) <= 3500
    )
