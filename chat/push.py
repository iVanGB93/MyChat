"""
Expo Push Notification utility.

Sends push notifications via Expo's Push API so users receive
notifications even when the app is completely closed.

API docs: https://docs.expo.dev/push-notifications/sending-notifications/
"""

import logging
from typing import Optional

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from users.models import UserDevice

logger = logging.getLogger(__name__)
User = get_user_model()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

_fcm_initialized = False
_fcm_app = None


def _get_fcm_app():
    """Lazily initialise the firebase-admin app for FCM v1 sends.

    Returns the app instance, or None if no credential is configured or the
    SDK is unavailable. Safe to call repeatedly (memoised).
    """
    global _fcm_initialized, _fcm_app
    if _fcm_initialized:
        return _fcm_app
    _fcm_initialized = True
    info = getattr(settings, "FCM_SERVICE_ACCOUNT_INFO", None)
    if not info:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials

        try:
            _fcm_app = firebase_admin.get_app("axonic-fcm")
        except ValueError:
            cred = credentials.Certificate(info)
            _fcm_app = firebase_admin.initialize_app(cred, name="axonic-fcm")
    except Exception as exc:  # SDK missing or bad credential
        logger.warning("[FCM] init failed: %s", exc)
        _fcm_app = None
    return _fcm_app


def get_fcm_status() -> dict:
    """Report whether FCM data-push delivery is operational on this server.

    Browser-inspectable via the routing monitor so we can confirm the deployed
    instance actually has a valid credential without shell/log access.
    """
    info = getattr(settings, "FCM_SERVICE_ACCOUNT_INFO", None)
    project_id = None
    if isinstance(info, dict):
        project_id = info.get("project_id")
    app = _get_fcm_app()
    try:
        import firebase_admin  # noqa: F401
        sdk_available = True
    except Exception:
        sdk_available = False
    return {
        "credential_configured": bool(info),
        "sdk_available": sdk_available,
        "initialized": app is not None,
        "project_id": project_id,
    }


def send_fcm_test(fcm_token: str, notification: bool = False) -> dict:
    """Send ONE FCM message to a token and return a diagnosable result.

    Surfaces the exact firebase-admin error (e.g. UNREGISTERED token, sender
    mismatch) so push delivery can be debugged from the browser without needing
    server log access. Never raises.

    When ``notification`` is True a `notification` block is attached (mirroring a
    real message push) so a killed/backgrounded recipient should get an
    OS/Google-Play-Services-drawn banner WITHOUT the app process starting. This
    isolates "does FCM display on this killed device at all?" from "does the
    data-only background handler wake?".
    """
    app = _get_fcm_app()
    if not app:
        return {"ok": False, "reason": "fcm_not_initialized"}
    if not fcm_token:
        return {"ok": False, "reason": "no_fcm_token"}
    try:
        from firebase_admin import messaging
    except Exception as exc:
        return {"ok": False, "reason": "sdk_import_failed", "detail": str(exc)}
    if notification:
        msg = messaging.Message(
            token=fcm_token,
            data={"type": "diagnostic", "channelId": "messages"},
            notification=messaging.Notification(
                title="Diagnostic", body="FCM notification test",
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="messages", default_sound=True, priority="high",
                ),
            ),
        )
    else:
        msg = messaging.Message(
            token=fcm_token,
            data={
                "type": "diagnostic",
                "title": "Diagnostic",
                "body": "FCM test",
                "channelId": "messages",
            },
            android=messaging.AndroidConfig(priority="high"),
        )
    try:
        message_id = messaging.send(msg, app=app)
        return {"ok": True, "message_id": message_id}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__, "detail": str(exc)}


# FCM v1 forbids these keys in the data payload (rejects the whole message with
# INVALID_ARGUMENT). 'message_type' is the one our chat payload actually carries.
_FCM_RESERVED_KEYS = {"from", "message_type", "notification"}


def _send_fcm_data(
    fcm_tokens: list[str],
    data: dict,
    channel_id: str = "messages",
    priority: str = "high",
    title: str | None = None,
    body: str | None = None,
    notification_priority: str = "high",
) -> bool:
    """Send a high-priority FCM v1 message to raw FCM device tokens.

    HYBRID delivery: when ``title``/``body`` are provided we attach a
    ``notification`` block so Google Play Services renders the alert itself
    even when the app is fully killed (a data-only message cannot wake a killed
    app reliably). The ``data`` payload still rides along so the app can
    save-to-DB / ack / render a rich MessagingStyle notification whenever it is
    actually running (foreground, or kept alive by the foreground service).
    All data values must be strings. Fire-and-forget: never raises.

    NOTE: FCM v1 REJECTS the entire message with INVALID_ARGUMENT if the data
    payload contains a reserved key ('from', 'message_type', 'notification', or
    any key starting with 'google'/'gcm'). Our chat data carries
    ``message_type`` (snake_case) which silently failed EVERY real message push
    (success=0) — the diagnostic probe never tripped it because it uses a
    ``type`` key. We strip the reserved forms below; the app already reads the
    camelCase ``messageType`` fallback (ingressRouter normalizeMessage).
    """
    app = _get_fcm_app()
    if not app:
        logger.warning(
            "[FCM] skip send for %d token(s) — firebase app not initialised "
            "(FCM_SERVICE_ACCOUNT_JSON missing/invalid on this server?)",
            len(fcm_tokens),
        )
        return False
    if not fcm_tokens:
        return False
    try:
        from firebase_admin import messaging
    except Exception:
        return False

    str_data = {
        k: ("" if v is None else str(v))
        for k, v in data.items()
        if k not in _FCM_RESERVED_KEYS and not k.startswith(("google", "gcm"))
    }
    str_data["channelId"] = channel_id

    notification = None
    android_notification = None
    apns_config = None
    if title is not None or body is not None:
        notification = messaging.Notification(title=title, body=body)
        android_notification = messaging.AndroidNotification(
            channel_id=channel_id,
            default_sound=True,
            priority=("max" if notification_priority == "max" else "high"),
        )
        # iOS (APNs): the same notification block alerts Apple devices once an
        # APNs key is configured in Firebase + an iOS build ships. Harmless to
        # Android-only deployments. mutable-content lets a future Notification
        # Service Extension enrich the alert; the data still rides in `str_data`.
        apns_config = messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound="default", mutable_content=True),
            ),
        )

    messages = [
        messaging.Message(
            token=tok,
            data=str_data,
            notification=notification,
            android=messaging.AndroidConfig(
                priority=priority,
                notification=android_notification,
            ),
            apns=apns_config,
        )
        for tok in fcm_tokens
        if tok
    ]
    if not messages:
        return False
    try:
        resp = messaging.send_each(messages, app=app)
        if resp.failure_count:
            # Log the ACTUAL per-token failure so we can tell apart a stale
            # token (UnregisteredError → app reinstalled / token rotated, prune
            # it), a wrong-project token (SenderIdMismatchError), a malformed
            # message (InvalidArgumentError), or a credential/APNs problem
            # (ThirdPartyAuthError). Without this we only saw "success=0
            # failure=1" with no cause.
            for tok, r in zip([m.token for m in messages], resp.responses):
                if not r.success:
                    exc = r.exception
                    logger.warning(
                        "[FCM] token=…%s FAILED type=%s code=%s detail=%s",
                        (tok[-12:] if tok else "?"),
                        type(exc).__name__ if exc else "?",
                        getattr(exc, "code", None),
                        str(exc) if exc else "",
                    )
            logger.warning(
                "[FCM] sent %d data message(s) success=%d failure=%d",
                len(messages), resp.success_count, resp.failure_count,
            )
        else:
            logger.info("[FCM] sent %d data message(s)", resp.success_count)
        return resp.success_count > 0
    except Exception as exc:
        logger.warning("[FCM] send failed: %s", exc)
        return False


def _is_expo_push_token(token: str) -> bool:
    return bool(token) and (
        token.startswith("ExponentPushToken[")
        or token.startswith("ExpoPushToken[")
    )


def _send_expo_push(
    push_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    channel_id: str = "messages",
    priority: str = "high",
    sound: str = "default",
) -> bool:
    """
    Send a push notification to one or more Expo push tokens.
    Silently fails on errors (fire-and-forget for real-time notifications).
    """
    if not push_tokens:
        return False

    messages = []
    for token in push_tokens:
        if not _is_expo_push_token(token):
            continue
        messages.append({
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "channelId": channel_id,
            "priority": priority,
            "sound": sound,
        })

    if not messages:
        return False

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        ticket_rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(ticket_rows, list):
            logger.info("[ExpoPush] sent %d notification(s), status=%d", len(messages), resp.status_code)
            return True

        accepted = 0
        errors = 0
        for row in ticket_rows:
            if isinstance(row, dict) and row.get("status") == "ok":
                accepted += 1
            else:
                errors += 1

        if errors:
            logger.warning(
                "[ExpoPush] ticket errors accepted=%d errors=%d payload=%s",
                accepted,
                errors,
                payload,
            )
        else:
            logger.info(
                "[ExpoPush] sent %d notification(s), accepted=%d",
                len(messages),
                accepted,
            )
        return accepted > 0
    except Exception as exc:
        logger.warning("[ExpoPush] failed to send: %s", exc)
        return False


def send_message_push(
    recipient_ids: list[int],
    sender_name: str,
    content: str | None,
    room_id: str,
    room_name: str,
    correlation_id: str | None = None,
    route_reason: str | None = None,
    message_id: str | None = None,
    sender_id: int | None = None,
    message_type: str | None = None,
    created_at: str | None = None,
    extra_data: dict | None = None,
) -> bool:
    """Send a push notification for a new chat message.

    When full message fields are provided they are embedded in the push data
    payload so the recipient app can save the message to its local SQLite DB
    immediately on push receipt — without waiting for the WS to reconnect.
    """
    device_rows = list(
        UserDevice.objects.filter(
            user_id__in=recipient_ids,
            is_active=True,
            user__notif_messages_enabled=True,
        ).filter(
            Q(expo_push_token__startswith="ExponentPushToken[")
            | Q(expo_push_token__startswith="ExpoPushToken[")
            | ~Q(fcm_token="")
        ).values_list("expo_push_token", "fcm_token").distinct()
    )
    # Prefer FCM data messages (WhatsApp-style background delivery). For devices
    # that have a raw FCM token we send ONLY the FCM data message; Expo is the
    # fallback for devices without one, so a device never gets a double push.
    fcm_tokens = [f for (_e, f) in device_rows if f]
    tokens = [
        e for (e, f) in device_rows
        if not f and _is_expo_push_token(e)
    ]
    # Notification body. Media messages carry no text content, so show a
    # type-aware placeholder ("📷 Photo" / "🎤 Voice message") instead of a
    # generic "New message".
    if content:
        display_body = content[:200]
    elif message_type in ("image", "photo"):
        display_body = "📷 Photo"
    elif message_type in ("voice", "audio"):
        display_body = "🎤 Voice message"
    elif message_type == "video":
        display_body = "🎬 Video"
    else:
        display_body = "New message"
    data: dict = {
        "type": "new_message",
        "roomId": room_id,
        "room_id": room_id,
        "roomName": room_name,
        "room_name": room_name,
        "correlationId": correlation_id,
        "routeReason": route_reason,
        "correlation_id": correlation_id,
        "route_reason": route_reason,
    }
    # Full message payload — lets the app save to SQLite without WS
    if message_id:
        data["messageId"] = message_id
        data["message_id"] = message_id
    if sender_id is not None:
        data["senderId"] = str(sender_id)
        data["sender_id"] = str(sender_id)
        # Sender's avatar (relative URL) so the app can show the person's photo
        # as the notification's LARGE icon (the app icon is only the small badge
        # Android mandates). The app resolves this to an absolute URL. Best-effort.
        try:
            _sender = User.objects.filter(id=sender_id).first()
            _av = getattr(_sender, "avatar", None)
            if _av and getattr(_av, "name", ""):
                data["senderAvatar"] = _av.url
                data["sender_avatar"] = _av.url
        except Exception:
            pass
    if sender_name:
        data["sender"] = sender_name
    if content:
        data["content"] = content[:2000]
    if message_type:
        data["messageType"] = message_type
        data["message_type"] = message_type
    if created_at:
        data["createdAt"] = created_at
        data["created_at"] = created_at
    # Pass through any extra client fields (reply_to, duration_ms, etc.).
    #
    # CRITICAL: Expo limits the push *data* payload to ~4KB. If we exceed it the
    # payload gets truncated and trailing fields (messageId, content, ...) are
    # silently dropped — the recipient then can't save the message or send a
    # delivery ACK, so the sender's tick stays "pending" forever. Voice/image
    # messages carry large base64 blobs (audio_b64/image_b64) in the client
    # fields, so we must never forward those (or any oversized value) in the
    # push. The recipient hydrates media over the WS once it reconnects.
    if extra_data:
        # Known large/binary fields that must never ride in a push payload.
        _PUSH_BLOCKED_KEYS = {
            "audio_b64", "image_b64", "video_b64", "file_b64",
            "audio", "image", "video", "file", "thumbnail_b64", "waveform",
        }
        _MAX_FIELD_LEN = 500  # chars; anything longer is dropped from the push
        for k, v in extra_data.items():
            if k in data or v is None or k in _PUSH_BLOCKED_KEYS:
                continue
            # Expo push data values must be strings
            sv = v if isinstance(v, (str, int, float, bool)) else str(v)
            if isinstance(sv, str) and len(sv) > _MAX_FIELD_LEN:
                # Skip oversized values to keep the payload under Expo's limit.
                continue
            data[k] = sv
    # DATA-ONLY FCM push for messages: we do NOT attach a `notification` block,
    # so the app's background handler (setBackgroundMessageHandler) runs and
    # renders the WhatsApp-style Notifee MessagingStyle notification itself —
    # one box per conversation, with Reply + Mark-as-read actions. The title/
    # body ride INSIDE the data payload so the app can render sender + text
    # (incl. the media placeholder body). Tradeoff vs. the old hybrid banner:
    # data-only needs the app process to run the handler — the foreground
    # service keeps it alive for the common (backgrounded) case; a fully
    # force-stopped / OEM-frozen app is the risk (it may show nothing until
    # reopened). Calls stay HYBRID (see send_call_push) so ringing is reliable.
    # Expo is the fallback for devices without a raw FCM token.
    sent = False
    if fcm_tokens:
        fcm_data = dict(data)
        fcm_data["title"] = sender_name or "New message"
        fcm_data["body"] = display_body
        sent = _send_fcm_data(
            fcm_tokens=fcm_tokens,
            data=fcm_data,
            channel_id="messages",
        ) or sent
    if tokens:
        sent = _send_expo_push(
            push_tokens=tokens,
            title=sender_name,
            body=display_body,
            data=data,
            channel_id="messages",
        ) or sent
    return sent


def send_call_push(
    callee_id: int,
    caller_name: str,
    call_type: str,
    call_id: str,
    caller_id: int,
    room_name: str,
    correlation_id: str | None = None,
    route_reason: str | None = None,
) -> bool:
    """Send a push notification for an incoming call.

    HYBRID delivery, same model as messages: devices with a raw FCM token get a
    high-priority FCM message carrying BOTH a `notification` block (so the OS
    rings/alerts even when the app is killed) and the call `data` (so the app
    can show the full incoming-call UI when it is alive or when tapped). Expo is
    the fallback for devices without an FCM token, so no device is double-pushed.
    """
    device_rows = list(
        UserDevice.objects.filter(
            user_id=callee_id,
            is_active=True,
            user__notif_calls_enabled=True,
        ).filter(
            Q(expo_push_token__startswith="ExponentPushToken[")
            | Q(expo_push_token__startswith="ExpoPushToken[")
            | ~Q(fcm_token="")
        ).values_list("expo_push_token", "fcm_token").distinct()
    )
    fcm_tokens = [f for (_e, f) in device_rows if f]
    tokens = [e for (e, f) in device_rows if not f and _is_expo_push_token(e)]
    icon = "📹" if call_type == "video" else "📞"
    title = f"{icon} Incoming {call_type} call from {caller_name}"
    body = "Tap to answer or swipe to decline"
    data = {
        "type": "incoming_call",
        "callId": call_id,
        "callerName": caller_name,
        "callerId": caller_id,
        "callType": call_type,
        "roomName": room_name,
        "correlationId": correlation_id,
        "routeReason": route_reason,
        "correlation_id": correlation_id,
        "route_reason": route_reason,
    }
    sent = False
    if fcm_tokens:
        fcm_data = dict(data)
        fcm_data["title"] = title
        fcm_data["body"] = body
        # DATA-ONLY (like messages): no `notification` block, so the app's FCM
        # background handler renders the proper CallStyle notification (full-
        # screen, Accept/Decline, ringtone) instead of a plain OS banner that
        # looks like a message. title/body ride inside the data for rendering.
        sent = _send_fcm_data(
            fcm_tokens=fcm_tokens,
            data=fcm_data,
            channel_id="calls",
            priority="high",
            notification_priority="max",
        ) or sent
    if tokens:
        sent = _send_expo_push(
            push_tokens=tokens,
            title=title,
            body=body,
            data=data,
            channel_id="calls",
            priority="high",
        ) or sent
    return sent
