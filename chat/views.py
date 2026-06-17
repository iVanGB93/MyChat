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
            import asyncio
            
            channel_layer = get_channel_layer()
            sender_channels = get_user_notification_channels(sender_id)
            if sender_channels:
                # Schedule notification asynchronously
                for channel in sender_channels:
                    asyncio.create_task(
                        channel_layer.send(
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

