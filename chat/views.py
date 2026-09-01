from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ChatRoom, GroupMembership, MessageDelivery
from .serializers import ChatRoomSerializer
from .relay_service import record_relay_deliveries

User = get_user_model()


def notify_room_update(room, recipient_ids=None):
    """Tell Axion clients to refresh room metadata without sending content.

    Group membership changes are server metadata, so no chat message should be
    fabricated merely to make the new room visible on another device.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    ids = recipient_ids
    if ids is None:
        ids = room.members.values_list("id", flat=True)
    payload = {"event": "room_update", "room_id": str(room.id)}
    channel_layer = get_channel_layer()
    for user_id in set(ids):
        async_to_sync(channel_layer.group_send)(
            f"notifications_{user_id}", {"type": "notify", "payload": payload}
        )


class ChatRoomViewSet(viewsets.ModelViewSet):
    """List / create / retrieve chat rooms for the authenticated user."""

    serializer_class = ChatRoomSerializer

    def get_queryset(self):
        return ChatRoom.objects.filter(members=self.request.user).prefetch_related(
            Prefetch("members", queryset=User.objects.select_related("profile", "presence")),
            Prefetch("group_memberships", queryset=GroupMembership.objects.select_related("user")),
        )

    def perform_create(self, serializer):
        # Direct rooms must be created through /rooms/direct/, which guarantees
        # exactly two users.  The generic endpoint is intentionally group-only.
        if serializer.validated_data.get("room_type") != ChatRoom.GROUP:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"room_type": "Use the direct endpoint for one-to-one chats."})

        name = (serializer.validated_data.get("name") or "").strip()
        invited = set(serializer.validated_data.get("members", []))
        invited.discard(self.request.user)
        if not name:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"name": "A group name is required."})
        if len(invited) < 2:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"members": "Choose at least two contacts for a group."})
        self._ensure_invitable(invited)

        room = serializer.save(name=name, room_type=ChatRoom.GROUP)
        everyone = [self.request.user, *invited]
        room.members.add(*everyone)
        GroupMembership.objects.bulk_create([
            GroupMembership(room=room, user=member, added_by=self.request.user,
                            role=GroupMembership.ADMIN if member == self.request.user else GroupMembership.MEMBER)
            for member in everyone
        ])
        notify_room_update(room, [member.id for member in everyone])

    def _ensure_invitable(self, users):
        """Only let a user add people they have accepted as contacts."""
        from users.models import Contact
        ids = {user.id for user in users}
        contact_ids = set(Contact.objects.filter(
            owner=self.request.user, contact_id__in=ids
        ).values_list("contact_id", flat=True))
        if ids != contact_ids:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"members": "Groups can include your accepted contacts only."})

    def _require_group_admin(self, room):
        if room.room_type != ChatRoom.GROUP:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"room": "This action is only available for groups."})
        if not GroupMembership.objects.filter(
            room=room, user=self.request.user, role=GroupMembership.ADMIN
        ).exists():
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only group admins can do that.")

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
        self._require_group_admin(room)
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

        if room.members.filter(id=user.id).exists():
            return Response(self.get_serializer(room).data)
        self._ensure_invitable({user})
        room.members.add(user)
        GroupMembership.objects.get_or_create(
            room=room, user=user,
            defaults={"added_by": request.user, "role": GroupMembership.MEMBER},
        )
        notify_room_update(room)
        serializer = self.get_serializer(room)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk=None):
        room = self.get_object()
        user_id = request.data.get("user_id")
        if not user_id:
            return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if room.room_type != ChatRoom.GROUP:
            return Response({"error": "Only groups have members to remove."}, status=status.HTTP_400_BAD_REQUEST)

        membership = GroupMembership.objects.filter(room=room, user_id=user_id).first()
        if not membership:
            return Response({"error": "User is not in this group."}, status=status.HTTP_404_NOT_FOUND)
        is_self = membership.user_id == request.user.id
        if not is_self:
            self._require_group_admin(room)
        if membership.role == GroupMembership.ADMIN and GroupMembership.objects.filter(
            room=room, role=GroupMembership.ADMIN
        ).count() == 1:
            return Response({"error": "Assign another admin before the last admin leaves."}, status=status.HTTP_400_BAD_REQUEST)

        recipient_ids = list(room.members.values_list("id", flat=True))
        room.members.remove(membership.user)
        membership.delete()
        notify_room_update(room, recipient_ids)
        return Response(self.get_serializer(room).data)

    @action(detail=True, methods=["post"], url_path="rename")
    def rename(self, request, pk=None):
        room = self.get_object()
        self._require_group_admin(room)
        name = str(request.data.get("name") or "").strip()
        if not name:
            return Response({"error": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > 120:
            return Response({"error": "name must be 120 characters or fewer"}, status=status.HTTP_400_BAD_REQUEST)
        room.name = name
        room.save(update_fields=["name", "updated_at"])
        notify_room_update(room)
        return Response(self.get_serializer(room).data)

    @action(detail=True, methods=["post"], url_path="avatar")
    def avatar(self, request, pk=None):
        """Replace a group photo. Only group admins can change it."""
        room = self.get_object()
        self._require_group_admin(room)
        uploaded = request.FILES.get("avatar")
        if not uploaded:
            return Response({"error": "avatar is required"}, status=status.HTTP_400_BAD_REQUEST)
        if uploaded.size > 8 * 1024 * 1024:
            return Response({"error": "Image must be 8 MB or smaller."}, status=status.HTTP_400_BAD_REQUEST)
        if not (uploaded.content_type or "").startswith("image/"):
            return Response({"error": "Please choose an image file."}, status=status.HTTP_400_BAD_REQUEST)

        # Remove the previous blob so changing a photo does not leave unused
        # bytes in the database-backed media storage.
        if room.avatar and room.avatar.name:
            room.avatar.delete(save=False)
        room.avatar = uploaded
        room.save(update_fields=["avatar", "updated_at"])
        notify_room_update(room)
        return Response(self.get_serializer(room).data)

    @action(detail=True, methods=["post"], url_path="make-admin")
    def make_admin(self, request, pk=None):
        room = self.get_object()
        self._require_group_admin(room)
        membership = GroupMembership.objects.filter(room=room, user_id=request.data.get("user_id")).first()
        if not membership:
            return Response({"error": "User is not in this group."}, status=status.HTTP_404_NOT_FOUND)
        membership.role = GroupMembership.ADMIN
        membership.save(update_fields=["role"])
        notify_room_update(room)
        return Response(self.get_serializer(room).data)


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
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            # A Redis group reaches the sender even when the acknowledgement
            # request and its Axion socket are served by different Railway
            # workers.  The old process-local channel list silently lost that
            # return event in a multi-instance deployment.
            async_to_sync(channel_layer.group_send)(
                f"notifications_{sender_id}",
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

    eligible_recipient_ids = [
        member_id for member_id in member_ids
        if member_id != request.user.id and member_id not in blockers
    ]
    record_relay_deliveries(
        room_id=room_id,
        message_id=message_id,
        sender_id=request.user.id,
        recipient_ids=eligible_recipient_ids,
    )

    extra_data = {
        k: v for k, v in msg_data.items()
        if k not in ("id", "sender", "sender_id", "content",
                     "message_type", "created_at",
                     "correlation_id", "route_reason")
    }

    for member_id in member_ids:
        if member_id == request.user.id or member_id in blockers:
            continue

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

        # Recipient is OFF-SCREEN for this room. Send everything and let the
        # app decide: relay over every notification WS the user has open AND
        # always queue a push floor when a token exists. A foreground app
        # receives the push silently (onMessage); only backgrounded/killed apps
        # render the actionable banner — so this never double-notifies.
        relayed_via_ws = False
        will_push = bool(routing_state["push_available"])
        if member_channels:
            try:
                sender_avatar = request.user.avatar.url if request.user.avatar and request.user.avatar.name else None
            except Exception:
                sender_avatar = None
            notify_payload = {
                "event": "new_message",
                "room_id": room_id,
                "room_name": room_name,
                "sender": request.user.username,
                "sender_id": request.user.id,
                "sender_avatar": sender_avatar,
                "content": content,
                "message_id": message_id,
                "message_type": message_type,
                "created_at": created_at,
                "correlation_id": f"msg:{message_id}",
                "route_reason": "notif_ws",
                # Tell the app a push floor is also in flight so it can defer the
                # actionable banner to FCM and avoid duplicates. When False, the
                # app is the ONLY notification surface.
                "push_floor": will_push,
            }
            for k, v in msg_data.items():
                if k not in notify_payload and k not in ("id", "sender"):
                    notify_payload[k] = v
            for channel in member_channels:
                async_to_sync(channel_layer.send)(
                    channel, {"type": "notify", "payload": notify_payload},
                )
            relayed_via_ws = True

        if routing_state["push_available"]:
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_PUSH, routed_at=now)
            record_notification_decision(
                kind="message", route="push_floor", correlation_id=f"msg:{message_id}",
                route_reason="push_floor_always",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )
            push_recipients.append(member_id)
        elif relayed_via_ws:
            MessageDelivery.objects.filter(
                room_id=room_id, message_id=message_id,
                sender_id=request.user.id, recipient_id=member_id,
            ).update(routed_via=MessageDelivery.ROUTE_NOTIF_WS, routed_at=now)
            record_notification_decision(
                kind="message", route="notif_ws", correlation_id=f"msg:{message_id}",
                route_reason="notification_ws_no_push_token",
                sender_id=request.user.id, recipient_id=member_id,
                room_id=room_id, routing_state=routing_state,
            )
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


