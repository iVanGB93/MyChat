from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views.decorators.cache import cache_control
from rest_framework import generics, permissions, status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from .db_storage import FileBlob
from .models import BlockedUser, Contact, PendingRegistration
from .serializers import (
    AccountDeleteSerializer,
    BlockedUserSerializer,
    ContactSerializer,
    PasswordChangeSerializer,
    RegistrationRequestSerializer,
    RegistrationResendSerializer,
    RegistrationVerifySerializer,
    UserRegistrationSerializer,
    UserSerializer,
)

User = get_user_model()


@cache_control(public=True, max_age=86400)
def serve_blob(request, name: str):
    """Stream a ``FileBlob`` row to the client (used by ``DatabaseStorage``)."""
    try:
        blob = FileBlob.objects.get(pk=name)
    except FileBlob.DoesNotExist:
        raise Http404("Not found")
    response = HttpResponse(bytes(blob.data), content_type=blob.content_type or "application/octet-stream")
    response["Content-Length"] = str(blob.size or len(blob.data))
    return response


class RegisterView(generics.CreateAPIView):
    """Public endpoint — register a new user."""

    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = (permissions.AllowAny,)


# ---------------------------------------------------------------------------
# Email-verification registration flow
# ---------------------------------------------------------------------------

def _make_throttle(scope: str, base=AnonRateThrottle):
    """Build a one-off DRF throttle class bound to a settings scope."""
    return type(f"{scope.title().replace('_', '')}Throttle", (base,), {"scope": scope})


def _generate_code() -> str:
    """Six-digit numeric code with leading zeros preserved."""
    import secrets
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str) -> str:
    """Hash the code with the same hasher used for passwords."""
    from django.contrib.auth.hashers import make_password
    return make_password(code)


def _check_code(code: str, hashed: str) -> bool:
    from django.contrib.auth.hashers import check_password
    return check_password(code, hashed)


def _send_verification_email(email: str, code: str) -> None:
    """Send the 6-digit verification email synchronously.

    Raises on failure so the caller can return a real error to the
    client (and roll back the PendingRegistration row). The send is
    bounded by ``EMAIL_TIMEOUT`` so it can't hang the request worker.

    We force IPv4-only DNS resolution for the duration of the send.
    Some hosts (e.g. Railway) advertise IPv6 records for mail
    providers but have no IPv6 route, which surfaces as
    ``OSError: [Errno 101] Network is unreachable``.
    """
    import socket as _socket
    from django.conf import settings
    from django.core.mail import send_mail

    subject = "Your Axonic verification code"
    body = (
        f"Welcome to Axonic!\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code expires in 10 minutes. If you did not request an Axonic "
        f"account, you can safely ignore this email.\n"
    )

    original_getaddrinfo = _socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)

    try:
        _socket.getaddrinfo = _ipv4_only_getaddrinfo
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    finally:
        _socket.getaddrinfo = original_getaddrinfo


class RegisterRequestView(APIView):
    """Step 1: validate the account, store a PendingRegistration, send code."""

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (_make_throttle("register_request"),)

    def post(self, request):
        from django.contrib.auth.hashers import make_password
        from datetime import timedelta

        serializer = RegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        email = data["email"]
        code = _generate_code()
        now = timezone.now()
        expires_at = now + timedelta(seconds=PendingRegistration.TTL_SECONDS)

        # Upsert: if a pending row already exists for this email, replace
        # its code/payload so the user can retry registration with a new
        # username/password without our DB blocking them.
        PendingRegistration.objects.update_or_create(
            email=email,
            defaults={
                "username": data["username"],
                "password_hash": make_password(data["password"]),
                "display_name": (data.get("display_name") or "").strip(),
                "code_hash": _hash_code(code),
                "expires_at": expires_at,
                "attempts": 0,
                "resends": 0,
                "last_sent_at": now,
            },
        )

        try:
            _send_verification_email(email, code)
        except Exception:
            # Roll back so the user can retry with the same email
            # without hitting our duplicate-email guard.
            import logging
            logging.getLogger(__name__).exception(
                "Failed to send verification email to %s", email,
            )
            PendingRegistration.objects.filter(email=email).delete()
            return Response(
                {"detail": "Could not send verification email. Please try again in a moment."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {"email": email, "expires_in": PendingRegistration.TTL_SECONDS},
            status=status.HTTP_200_OK,
        )


class RegisterResendView(APIView):
    """Step 1.5: regenerate the verification code (cooldown + cap enforced)."""

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (_make_throttle("register_resend"),)

    def post(self, request):
        from datetime import timedelta

        serializer = RegistrationResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        try:
            pending = PendingRegistration.objects.get(email=email)
        except PendingRegistration.DoesNotExist:
            return Response(
                {"detail": "No pending registration for that email."},
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()
        cooldown = timedelta(seconds=PendingRegistration.RESEND_COOLDOWN_SECONDS)
        if now - pending.last_sent_at < cooldown:
            wait = int((cooldown - (now - pending.last_sent_at)).total_seconds())
            return Response(
                {"detail": f"Please wait {wait}s before requesting another code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if pending.resends >= PendingRegistration.MAX_RESENDS:
            return Response(
                {"detail": "Too many resends. Please start over."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = _generate_code()
        pending.code_hash = _hash_code(code)
        pending.expires_at = now + timedelta(seconds=PendingRegistration.TTL_SECONDS)
        pending.attempts = 0
        pending.resends += 1
        pending.last_sent_at = now
        pending.save(update_fields=[
            "code_hash", "expires_at", "attempts", "resends", "last_sent_at",
        ])

        try:
            _send_verification_email(email, code)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to resend verification email to %s", email,
            )
            return Response(
                {"detail": "Could not send verification email. Please try again in a moment."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {"email": email, "expires_in": PendingRegistration.TTL_SECONDS},
            status=status.HTTP_200_OK,
        )


class RegisterVerifyView(APIView):
    """Step 2: check the code, create the User, return a fresh token pair."""

    permission_classes = (permissions.AllowAny,)
    throttle_classes = (_make_throttle("register_verify"),)

    def post(self, request):
        from rest_framework_simplejwt.tokens import RefreshToken

        serializer = RegistrationVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]

        try:
            pending = PendingRegistration.objects.get(email=email)
        except PendingRegistration.DoesNotExist:
            return Response(
                {"detail": "No pending registration for that email."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if pending.is_expired():
            pending.delete()
            return Response(
                {"detail": "Verification code expired. Please request a new one."},
                status=status.HTTP_410_GONE,
            )

        if pending.attempts >= PendingRegistration.MAX_ATTEMPTS:
            pending.delete()
            return Response(
                {"detail": "Too many wrong attempts. Please start over."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if not _check_code(code, pending.code_hash):
            pending.attempts += 1
            pending.save(update_fields=["attempts"])
            remaining = PendingRegistration.MAX_ATTEMPTS - pending.attempts
            return Response(
                {"detail": f"Invalid code. {remaining} attempts remaining."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-check uniqueness in case someone else grabbed the username
        # or email between request and verify.
        if (
            User.objects.filter(username__iexact=pending.username).exists()
            or User.objects.filter(email__iexact=pending.email).exists()
        ):
            pending.delete()
            return Response(
                {"detail": "Username or email is no longer available."},
                status=status.HTTP_409_CONFLICT,
            )

        # Create the user with the pre-hashed password (skip set_password).
        user = User(
            username=pending.username,
            email=pending.email,
            display_name=(pending.display_name or pending.username),
        )
        user.password = pending.password_hash
        user.save()

        pending.delete()

        refresh = RefreshToken.for_user(user)
        refresh["tv"] = user.token_version
        access = refresh.access_token
        access["tv"] = user.token_version

        return Response({
            "user": UserSerializer(user).data,
            "access": str(access),
            "refresh": str(refresh),
        }, status=status.HTTP_201_CREATED)


class ProfileView(generics.RetrieveUpdateAPIView):
    """Retrieve or update the authenticated user's profile.

    Accepts both JSON and multipart/form-data — the latter is required
    for avatar image uploads.
    """

    serializer_class = UserSerializer
    parser_classes = (JSONParser, MultiPartParser, FormParser)

    def get_object(self):
        return self.request.user


class PasswordChangeView(APIView):
    """Change the authenticated user's password.

    POST { "current_password": "...", "new_password": "..." }

    On success bumps `token_version` so all previously issued JWTs
    (including the one that made the request) are invalidated, and
    returns a fresh access/refresh pair so the client can stay
    logged in.
    """

    def post(self, request):
        from rest_framework_simplejwt.tokens import RefreshToken
        serializer = PasswordChangeSerializer(
            data=request.data, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.token_version = (user.token_version or 0) + 1
        user.save(update_fields=["password", "token_version"])

        # Issue a fresh token pair embedding the new token_version so
        # the caller can continue using the API immediately.
        refresh = RefreshToken.for_user(user)
        refresh["tv"] = user.token_version
        access = refresh.access_token
        access["tv"] = user.token_version
        return Response({
            "status": "ok",
            "access": str(access),
            "refresh": str(refresh),
        })


class LogoutAllSessionsView(APIView):
    """Invalidate every JWT previously issued for the caller.

    Bumps `token_version`; the next request from this client (still
    using the old token) will get 401. The mobile app should call
    this and then clear its local tokens.
    """

    def post(self, request):
        user = request.user
        user.token_version = (user.token_version or 0) + 1
        user.save(update_fields=["token_version"])
        return Response({"status": "ok", "token_version": user.token_version})


class DeleteAccountView(APIView):
    """Permanently delete the authenticated user's account.

    DELETE { "password": "..." }
    """

    def delete(self, request):
        serializer = AccountDeleteSerializer(
            data=request.data, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RegisterPushTokenView(APIView):
    """
    Register (or update) the Expo push notification token for the
    authenticated user.  The client should call this after every login
    and whenever the token is refreshed.

    POST  { "token": "ExponentPushToken[...]" }
    """

    def post(self, request):
        token = request.data.get("token", "").strip()
        if not token:
            return Response(
                {"error": "token is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Clear token from any other user (device switched accounts)
        User.objects.filter(expo_push_token=token).exclude(id=request.user.id).update(
            expo_push_token=""
        )

        request.user.expo_push_token = token
        request.user.save(update_fields=["expo_push_token"])
        return Response({"status": "ok"})


class PendingNotificationsView(APIView):
    """
    Returns pending message deliveries and active incoming calls for the
    authenticated user.  Used by the mobile app's background fetch / reconnect
    to show local notifications when the WebSocket is disconnected.

    NOTE: Chat messages are NOT stored server-side (WS-only, local-first).
    This endpoint reports PendingDelivery records — signaling metadata that
    tells the client "sender X has messages waiting for you in room Y".
    The actual message content was already sent via Expo push notification
    at send-time; this is purely a reconnect-fallback reminder.

    GET /api/users/notifications/pending/
    """

    def get(self, request):
        from chat.models import PendingDelivery, ChatRoom
        from calls.models import CallLog
        import datetime

        user = request.user
        result = {"messages": [], "calls": []}

        # ---- Pending deliveries (one entry per room × sender pair) ----
        pending = (
            PendingDelivery.objects.filter(to_user=user)
            .select_related("from_user", "room")
            .order_by("created_at")
        )

        for pd in pending:
            room = pd.room
            # Display name: for direct rooms with no custom name use sender's username
            if room.room_type == ChatRoom.DIRECT and not room.name:
                display_name = pd.from_user.username
            else:
                display_name = room.name or pd.from_user.username

            result["messages"].append({
                "room_id": str(pd.room_id),
                "room_name": display_name,
                "sender": pd.from_user.username,
                # Content not stored server-side; client already got the actual
                # content via Expo push at send-time.
                "content": "New messages waiting",
                "created_at": pd.created_at.isoformat(),
            })

        # ---- Active incoming calls (ringing in last 60 seconds) ----
        cutoff = timezone.now() - datetime.timedelta(seconds=60)
        ringing = CallLog.objects.filter(
            callee=user,
            status=CallLog.RINGING,
            started_at__gte=cutoff,
        ).select_related("caller")

        for call in ringing:
            result["calls"].append({
                "call_id": str(call.id),
                "caller": call.caller.username,
                "caller_id": call.caller_id,
                "call_type": call.call_type,
                "room_name": call.room_name,
            })

        return Response(result)


class UserSearchView(generics.ListAPIView):
    """Search users when adding contacts.

    A search matches a target user when ANY of the following hold:

    - The query is the **exact** `user_tag` (case-insensitive). The tag
      is a deliberately-shared handle, so tag lookup ignores the target's
      privacy flags.
    - The query is the **exact** email of a user who has opted in to
      `discoverable_by_email`.
    - The query partially matches `username` or `display_name` and the
      target has `discoverable_by_username = True` (default).

    The caller's own account is always excluded from results.
    """

    serializer_class = UserSerializer
    throttle_classes = (type("UserSearchThrottle", (UserRateThrottle,), {"scope": "user_search"}),)

    def get_queryset(self):
        raw = (self.request.query_params.get("q") or "").strip()
        if not raw:
            return User.objects.none()

        me = self.request.user
        # Normalize the user_tag lookup: accept "axn-7k3p", "AXN7K3P", etc.
        normalized = raw.replace(" ", "").upper()
        if not normalized.startswith("AXN-") and len(normalized) >= 4:
            tag_guess = "AXN-" + normalized[-4:] if len(normalized) == 7 else normalized
        else:
            tag_guess = normalized

        tag_match = Q(user_tag__iexact=tag_guess)
        email_match = Q(email__iexact=raw, discoverable_by_email=True)
        # Username / display_name partial search is gated on the target's
        # username discoverability preference.
        text_match = (
            Q(username__icontains=raw) | Q(display_name__icontains=raw)
        ) & Q(discoverable_by_username=True)

        return (
            User.objects.filter(tag_match | email_match | text_match)
            .exclude(id=me.id)
            .distinct()[:20]
        )


class ContactViewSet(viewsets.ModelViewSet):
    """CRUD for the authenticated user's contacts."""

    serializer_class = ContactSerializer

    def get_queryset(self):
        return Contact.objects.filter(owner=self.request.user).select_related("contact")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class BlockedUserViewSet(viewsets.ModelViewSet):
    """CRUD for the authenticated user's blocked-user list.

    GET     /api/users/blocked/          -> list
    POST    /api/users/blocked/  {user_id|blocked} -> block someone
    DELETE  /api/users/blocked/<id>/     -> unblock (id = BlockedUser row id)
    """

    serializer_class = BlockedUserSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return BlockedUser.objects.filter(owner=self.request.user).select_related("blocked")

    def create(self, request, *args, **kwargs):
        # Accept either {"blocked": <id>} or {"user_id": <id>}
        user_id = request.data.get("blocked") or request.data.get("user_id")
        if not user_id:
            return Response(
                {"error": "blocked (user id) is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            target = User.objects.get(id=user_id)
        except (User.DoesNotExist, ValueError, TypeError):
            return Response(
                {"error": "User not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if target.id == request.user.id:
            return Response(
                {"error": "Cannot block yourself"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        obj, _ = BlockedUser.objects.get_or_create(
            owner=request.user, blocked=target,
        )
        # Blocking implies they're not a contact — remove any reverse contact too.
        Contact.objects.filter(owner=request.user, contact=target).delete()
        return Response(
            self.get_serializer(obj).data,
            status=status.HTTP_201_CREATED,
        )
