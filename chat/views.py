from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ChatRoom, MessageDelivery
from .serializers import ChatRoomSerializer

User = get_user_model()


class ChatRoomViewSet(viewsets.ModelViewSet):
    """List / create / retrieve chat rooms for the authenticated user."""

    serializer_class = ChatRoomSerializer

    def get_queryset(self):
        return ChatRoom.objects.filter(
            members=self.request.user
        ).prefetch_related("members")

    def perform_create(self, serializer):
        room = serializer.save()
        room.members.add(self.request.user)

    @action(detail=False, methods=["post"], url_path="direct")
    def get_or_create_direct(self, request):
        """
        Get or create a direct chat room between the current user and another.
        Expects: {"user_id": <int>}
        """
        other_id = request.data.get("user_id")
        if not other_id:
            return Response(
                {"error": "user_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            other_user = User.objects.get(id=other_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Look for an existing direct room between the two users
        room = (
            ChatRoom.objects.filter(room_type=ChatRoom.DIRECT, members=request.user)
            .filter(members=other_user)
            .first()
        )

        if not room:
            room = ChatRoom.objects.create(
                name="",
                room_type=ChatRoom.DIRECT,
            )
            room.members.add(request.user, other_user)

        serializer = self.get_serializer(room)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk=None):
        """
        Add a member to an existing chat room.
        Expects: {"user_id": <int>}
        """
        room = self.get_object()
        user_id = request.data.get("user_id")
        if not user_id:
            return Response(
                {"error": "user_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        room.members.add(user)
        serializer = self.get_serializer(room)
        return Response(serializer.data)


# ---- HTTP Message Delivery Acknowledgment endpoint ----
# Called by mobile app (usually from push handler) to confirm message persistence
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ack_message_delivery(request):
    """
    Confirm that a message was received and persisted to device storage.
    
    Idempotent endpoint — safe to call multiple times per message.
    
    Request body:
    {
        "message_id": str,
        "sender_id": int,
        "room_id": str (UUID),
        "device_id": str (optional, for multi-device tracking),
        "delivered_at": ISO timestamp (optional)
    }
    
    Response: 200 OK
    {
        "status": "delivered" | "already_delivered",
        "message_id": str
    }
    """
    try:
        message_id = request.data.get("message_id", "").strip()
        sender_id = request.data.get("sender_id")
        room_id = request.data.get("room_id", "").strip()
        delivered_at_str = request.data.get("delivered_at")
        
        if not message_id or not sender_id or not room_id:
            return Response(
                {
                    "error": "message_id, sender_id, and room_id are required",
                    "status": "invalid_request",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        try:
            sender_id = int(sender_id)
        except (ValueError, TypeError):
            return Response(
                {"error": "sender_id must be an integer", "status": "invalid_request"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Parse delivered_at if provided, otherwise use current time
        try:
            if delivered_at_str:
                from django.utils.dateparse import parse_datetime
                delivered_at = parse_datetime(delivered_at_str)
                if not delivered_at:
                    delivered_at = timezone.now()
            else:
                delivered_at = timezone.now()
        except Exception:
            delivered_at = timezone.now()
        
        # Look up the delivery record
        delivery = MessageDelivery.objects.filter(
            message_id=message_id,
            sender_id=sender_id,
            recipient_id=request.user.id,
            room_id=room_id,
        ).first()
        
        if not delivery:
            # Could be a duplicate ack for a message we already cleared,
            # or a message from an untrusted sender. Log but don't error.
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "[MessageAck] delivery record not found message_id=%s sender=%s recipient=%s room=%s",
                message_id, sender_id, request.user.id, room_id,
            )
            return Response(
                {
                    "status": "not_found",
                    "message_id": message_id,
                    "note": "delivery record not in database (already processed or sender mismatch)",
                },
                status=status.HTTP_200_OK,  # 200 to signal "ack received" even if record is gone
            )
        
        # Update delivery status if still pending
        if delivery.status == MessageDelivery.STATUS_PENDING:
            delivery.status = MessageDelivery.STATUS_DELIVERED
            delivery.delivered_at = delivered_at
            delivery.save(update_fields=["status", "delivered_at"])
            result_status = "delivered"
        else:
            result_status = "already_delivered"
        
        # Check if there are any other pending messages from this sender in this room
        has_pending = MessageDelivery.objects.filter(
            room_id=room_id,
            sender_id=sender_id,
            recipient_id=request.user.id,
            status=MessageDelivery.STATUS_PENDING,
        ).exists()
        
        # If no more pending, clean up PendingDelivery record
        if not has_pending:
            from .models import PendingDelivery
            PendingDelivery.objects.filter(
                room_id=room_id,
                from_user_id=sender_id,
                to_user_id=request.user.id,
            ).delete()
        
        # Notify the sender via the notification WS (if connected)
        # that the message was delivered, so they can update UI
        try:
            from .consumers import get_user_notification_channels
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            sender_channels = get_user_notification_channels(sender_id)
            if sender_channels:
                # This is a synchronous view, so there is no running event loop.
                # Use async_to_sync to push onto the channel layer (asyncio.create_task
                # would raise "no running event loop" and silently drop the ack).
                for channel in sender_channels:
                    async_to_sync(channel_layer.send)(
                        channel,
                        {
                            "type": "notify",
                            "payload": {
                                "event": "message_delivery_ack",
                                "message_id": message_id,
                                "by_user_id": request.user.id,
                                "by_username": request.user.username,
                                "room_id": str(room_id),
                            },
                        },
                    )
        except Exception as e:
            # Sender notification is nice-to-have; don't fail the ack if it breaks
            import logging
            logger = logging.getLogger(__name__)
            logger.warning("[MessageAck] failed to notify sender: %s", e)
        
        return Response(
            {
                "status": result_status,
                "message_id": message_id,
            },
            status=status.HTTP_200_OK,
        )
    
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception("[MessageAck] unhandled error")
        return Response(
            {"error": "internal server error", "status": "error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ---- Delivery status reconciliation endpoint ----
# Called by the sender on reconnect/foreground to catch up on delivery ticks
# for messages that were acked by the recipient while the sender was offline
# (and therefore missed the real-time message_delivery_ack WS event).
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def message_delivery_status(request):
    """Return delivery status for a batch of messages sent by the current user.

    Request body:
    {
        "message_ids": [str, ...]   # IDs of locally-pending sent messages
    }

    Response: 200 OK
    {
        "delivered": [
            { "message_id": str, "recipient_id": int, "delivered_at": iso|null }
        ]
    }

    Only messages where the requesting user is the sender are returned, so a
    caller can never probe delivery state for messages they didn't send.
    """
    message_ids = request.data.get("message_ids") or []
    if not isinstance(message_ids, list) or not message_ids:
        return Response({"delivered": []}, status=status.HTTP_200_OK)

    # Cap batch size to avoid abuse / oversized queries.
    message_ids = [str(m) for m in message_ids[:500]]

    rows = MessageDelivery.objects.filter(
        sender_id=request.user.id,
        message_id__in=message_ids,
        status=MessageDelivery.STATUS_DELIVERED,
    ).values("message_id", "recipient_id", "delivered_at")

    delivered = [
        {
            "message_id": row["message_id"],
            "recipient_id": row["recipient_id"],
            "delivered_at": row["delivered_at"].isoformat() if row["delivered_at"] else None,
        }
        for row in rows
    ]
    return Response({"delivered": delivered}, status=status.HTTP_200_OK)


# ---- HTTP send-message endpoint (reply from notification, killed app) ----
# The chat transport is normally the room WebSocket. When the app is killed and
# the user replies directly from an FCM notification action, there is no live WS
# to send over, so the headless task POSTs the reply here. This view replicates
# the ChatConsumer relay synchronously: broadcast to the room group, fan out to
# recipients' notification channels, track delivery, and push to offline users.
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_message(request):
    import logging

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from users.models import BlockedUser

    from .consumers import (
        decide_message_notification_route,
        get_connected_chat_user_ids,
        get_user_notification_channels,
        get_user_routing_state,
        record_notification_decision,
    )
    from .models import PendingDelivery
    from .push import send_message_push

    logger = logging.getLogger(__name__)

    data = request.data
    room_id = str(data.get("room_id", "")).strip()
    message_id = str(data.get("id", "")).strip()
    content = data.get("message", "")
    message_type = data.get("message_type", "text")
    created_at = data.get("created_at") or timezone.now().isoformat()

    if not room_id or not message_id or not content:
        return Response(
            {"error": "room_id, id and message are required", "status": "invalid_request"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Membership check — only members may send to a room.
    room = (
        ChatRoom.objects.filter(id=room_id, members=request.user)
        .prefetch_related("members")
        .first()
    )
    if not room:
        return Response(
            {"error": "room not found or not a member", "status": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )

    member_ids = list(room.members.values_list("id", flat=True))
    if room.room_type == ChatRoom.DIRECT and not room.name:
        room_name = request.user.username
    else:
        room_name = room.name or str(room.id)

    # Build the relay payload (mirrors ChatConsumer.receive). Pass through any
    # extra client-supplied fields (e.g. reply_to) except control/identity keys.
    _RESERVED = {
        "type", "message", "id", "created_at",
        "sender", "sender_id", "hydration", "room_id",
    }
    msg_data = {k: v for k, v in data.items() if k not in _RESERVED}
    msg_data.update({
        "id": message_id,
        "sender": request.user.username,
        "sender_id": request.user.id,
        "content": content,
        "message_type": message_type,
        "created_at": created_at,
        "correlation_id": f"msg:{message_id}",
        "route_reason": "http_send",
    })

    channel_layer = get_channel_layer()
    room_group = f"chat_{room_id}"
    now = timezone.now()

    # 1) Broadcast to anyone live in the room's chat WS.
    async_to_sync(channel_layer.group_send)(
        room_group,
        {"type": "chat_message", "message": msg_data},
    )

    # 2) Per-member notification fan-out.
    blockers = set(
        BlockedUser.objects.filter(blocked=request.user).values_list("owner_id", flat=True)
    )
    in_room_ids = get_connected_chat_user_ids(room_id)
    push_recipients: list[int] = []

    extra_data = {
        k: v for k, v in msg_data.items()
        if k not in ("id", "sender", "sender_id", "content",
                     "message_type", "created_at",
                     "correlation_id", "route_reason")
    }

    for member_id in member_ids:
        if member_id == request.user.id or member_id in blockers:
            continue

        MessageDelivery.objects.get_or_create(
            room_id=room_id,
            message_id=message_id,
            sender_id=request.user.id,
            recipient_id=member_id,
            defaults={"status": MessageDelivery.STATUS_PENDING},
        )
        PendingDelivery.objects.get_or_create(
            room_id=room_id,
            from_user_id=request.user.id,
            to_user_id=member_id,
        )

        routing_state = get_user_routing_state(member_id)
        member_channels = get_user_notification_channels(member_id)
        route = decide_message_notification_route(
            room_id=room_id,
            routing_state=routing_state,
            in_room_via_chat_ws=member_id in in_room_ids,
            has_notification_channels=bool(member_channels),
        )

        if route == "room_ws_active":
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_CHAT_WS, routed_at=now)
            record_notification_decision(
                kind="message", route=route, correlation_id=f"msg:{message_id}",
                route_reason="recipient_active_in_room",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )
            continue

        if route == "notif_ws":
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_NOTIF_WS, routed_at=now)
            record_notification_decision(
                kind="message", route=route, correlation_id=f"msg:{message_id}",
                route_reason="notification_ws_available",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )
            notify_payload = {
                "event": "new_message",
                "room_id": room_id,
                "room_name": room_name,
                "sender": request.user.username,
                "sender_id": request.user.id,
                "content": content,
                "message_id": message_id,
                "message_type": message_type,
                "created_at": created_at,
                "correlation_id": f"msg:{message_id}",
                "route_reason": "notif_ws",
            }
            for k, v in msg_data.items():
                if k not in notify_payload and k not in ("id", "sender"):
                    notify_payload[k] = v
            for channel in member_channels:
                async_to_sync(channel_layer.send)(
                    channel, {"type": "notify", "payload": notify_payload},
                )
            continue

        if route == "push_initial":
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_PUSH, routed_at=now)
            record_notification_decision(
                kind="message", route=route, correlation_id=f"msg:{message_id}",
                route_reason="push_available_ws_unavailable",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )
            push_recipients.append(member_id)
        else:
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_PENDING_ONLY, routed_at=now)
            record_notification_decision(
                kind="message", route="pending_only", correlation_id=f"msg:{message_id}",
                route_reason="no_push_endpoint_available",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )

    # 3) Push to offline recipients.
    if push_recipients:
        logger.info(
            "[Push] HTTP send: pushing to %d offline user(s) room=%s message_id=%s",
            len(push_recipients), room_id, message_id,
        )
        sent = send_message_push(
            recipient_ids=push_recipients,
            sender_name=request.user.username,
            content=content,
            room_id=room_id,
            room_name=room_name,
            correlation_id=f"msg:{message_id}",
            route_reason="http_send",
            message_id=message_id,
            sender_id=request.user.id,
            message_type=message_type,
            created_at=created_at,
            extra_data=extra_data,
        )
        if sent:
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                recipient_id__in=push_recipients,
            ).update(push_sent_at=timezone.now(), routed_via=MessageDelivery.ROUTE_PUSH, routed_at=timezone.now())
            for recipient_id in push_recipients:
                record_notification_decision(
                    kind="message", route="push_sent", correlation_id=f"msg:{message_id}",
                    route_reason="http_send_push_sent",
                    sender_id=request.user.id, recipient_id=recipient_id,
                    room_id=room_id,
                )

    return Response(
        {
            "status": "relayed",
            "message_id": message_id,
            "correlation_id": f"msg:{message_id}",
        },
        status=status.HTTP_200_OK,
    )


