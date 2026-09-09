"""Passwordless login and Google linking. No provider tokens are persisted."""
import secrets
import uuid
from datetime import timedelta
from functools import lru_cache

import requests
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .auth import AxonicTokenObtainPairSerializer
from .models import GoogleIdentity, SignInChallenge, User
from .serializers import UserSerializer


class StartThrottle(AnonRateThrottle):
    scope = "easy_auth"


class VerifyThrottle(AnonRateThrottle):
    scope = "easy_auth_verify"


class EmailInput(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class VerifyInput(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.RegexField(r"^[0-9]{6}$")
    username = serializers.CharField(required=False, max_length=150, validators=[UnicodeUsernameValidator()])


class GoogleInput(serializers.Serializer):
    id_token = serializers.CharField(max_length=16384)


def session_response(user):
    if not user.is_active:
        return Response({"detail": "This account cannot sign in. Contact support."}, status=403)
    refresh = AxonicTokenObtainPairSerializer.get_token(user)
    return Response({"access": str(refresh.access_token), "refresh": str(refresh),
                     "user": UserSerializer(user).data})


def send_signin_code(email, code, linking=False):
    subject = "Your Axonic sign-in code"
    action = "sign in and link Google to your Axonic account" if linking else "sign in to Axonic"
    body = (f"Use {code} to {action}.\n\nThis code expires in 10 minutes. "
            "Never share it. If you did not request this, ignore this email.")
    if settings.RESEND_API_KEY:
        response = requests.post("https://api.resend.com/emails", timeout=15,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={"from": settings.DEFAULT_FROM_EMAIL, "to": [email], "subject": subject, "text": body})
        response.raise_for_status()
    elif settings.SENDGRID_API_KEY:
        response = requests.post("https://api.sendgrid.com/v3/mail/send", timeout=15,
            headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
            json={"personalizations": [{"to": [{"email": email}]}],
                  "from": {"email": settings.DEFAULT_FROM_EMAIL}, "subject": subject,
                  "content": [{"type": "text/plain", "value": body}]})
        response.raise_for_status()
    else:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)


def start_challenge(email, google_subject=""):
    email = email.strip().lower()
    now = timezone.now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        row, _ = SignInChallenge.objects.get_or_create(email=email)
        row = SignInChallenge.objects.select_for_update().get(pk=row.pk)
        if row.send_count and (now - row.last_sent_at).total_seconds() < 60:
            return Response({"detail": "Please wait one minute before requesting another code."}, status=429)
        if now - row.window_started_at >= timedelta(hours=1):
            row.window_started_at = now
            row.send_count = 0
        if row.send_count >= 5:
            return Response({"detail": "Too many codes requested. Please try again later."}, status=429)
        row.challenge_id = uuid.uuid4()
        row.code_hash = make_password(code)
        row.expires_at = now + timedelta(minutes=10)
        row.last_sent_at = now
        row.send_count += 1
        row.attempts = 0
        row.consumed = False
        row.google_subject = google_subject
        row.save()
    try:
        send_signin_code(email, code, bool(google_subject))
    except Exception:
        # Fail visibly, with no code/provider credentials or mail response logged.
        SignInChallenge.objects.filter(challenge_id=row.challenge_id).update(consumed=True, code_hash="")
        return Response({"detail": "Email could not be sent. Please wait a minute and try again."}, status=503)
    return Response({"challenge_id": str(row.challenge_id), "email": email,
                     "expires_in": 600, "google_link": bool(google_subject)})


class PublicAuthView(APIView):
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()
    throttle_classes = (StartThrottle,)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class EmailSignInStart(PublicAuthView):
    def post(self, request):
        data = EmailInput(data=request.data)
        data.is_valid(raise_exception=True)
        return start_challenge(data.validated_data["email"])


class EmailSignInVerify(PublicAuthView):
    throttle_classes = (VerifyThrottle,)

    def post(self, request):
        data = VerifyInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                row = SignInChallenge.objects.select_for_update().filter(
                    challenge_id=data.validated_data["challenge_id"]).first()
                if not row or row.consumed or row.expires_at <= timezone.now() or row.attempts >= 5:
                    return Response({"detail": "Code expired or unavailable. Request a new code."}, status=400)
                if not check_password(data.validated_data["code"], row.code_hash):
                    row.attempts += 1
                    row.save(update_fields=["attempts"])
                    return Response({"detail": "Incorrect code. Please try again."}, status=400)
                users = list(User.objects.select_for_update().filter(email__iexact=row.email)[:2])
                if len(users) > 1:
                    return Response({"detail": "Please use password login and contact support about this email."}, status=409)
                identity = GoogleIdentity.objects.filter(subject=row.google_subject).first() if row.google_subject else None
                if identity and (not users or identity.user_id != users[0].id):
                    return Response({"detail": "Google is already linked to another account."}, status=409)
                if users:
                    user = users[0]
                    if not user.is_active:
                        return Response({"detail": "This account cannot sign in. Contact support."}, status=403)
                else:
                    username = data.validated_data.get("username")
                    if not username:
                        return Response({"needs_username": True})
                    if User.objects.filter(username__iexact=username).exists():
                        return Response({"detail": "That username is already taken."}, status=409)
                    user = User.objects.create_user(username=username, email=row.email, password=None,
                                                    display_name=username)
                if row.google_subject:
                    if GoogleIdentity.objects.filter(user=user).exclude(subject=row.google_subject).exists():
                        return Response({"detail": "This account already has a different Google login."}, status=409)
                    if not identity:
                        # Unique constraints resolve a competing link atomically;
                        # do not accept a get_or_create result owned by someone else.
                        GoogleIdentity.objects.create(subject=row.google_subject, user=user)
                row.consumed = True
                row.code_hash = ""
                row.google_subject = ""
                row.save(update_fields=["consumed", "code_hash", "google_subject"])
                return session_response(user)
        except IntegrityError:
            return Response({"detail": "Account details changed. Please try again."}, status=409)


@lru_cache(maxsize=1)
def google_request():
    from google.auth.transport.requests import Request
    from cachecontrol import CacheControl
    request = Request(session=CacheControl(requests.Session()))
    def bounded_request(*args, **kwargs):
        kwargs["timeout"] = 15
        return request(*args, **kwargs)
    return bounded_request


def verify_google_token(token):
    # The library checks Google's signature, audience, issuer and expiration.
    from google.oauth2 import id_token
    claims = id_token.verify_oauth2_token(token, google_request(), settings.GOOGLE_SIGNIN_WEB_CLIENT_ID)
    if not isinstance(claims.get("sub"), str) or not 1 <= len(claims["sub"]) <= 255 or claims.get("email_verified") is not True:
        raise ValueError("Unverified identity")
    email = EmailInput(data={"email": claims.get("email")})
    email.is_valid(raise_exception=True)
    return claims["sub"], email.validated_data["email"]


class GoogleSignIn(PublicAuthView):
    def post(self, request):
        if not settings.GOOGLE_SIGNIN_WEB_CLIENT_ID:
            return Response({"detail": "Google sign-in is not configured yet. Use email or password."}, status=503)
        data = GoogleInput(data=request.data)
        data.is_valid(raise_exception=True)
        token = data.validated_data["id_token"]
        try:
            subject, email = verify_google_token(token)
        except (ValueError, serializers.ValidationError):
            return Response({"detail": "Google sign-in could not be verified. Please try again."}, status=400)
        except Exception:
            return Response({"detail": "Google verification is temporarily unavailable."}, status=503)
        identity = GoogleIdentity.objects.select_related("user").filter(subject=subject).first()
        if identity:
            return session_response(identity.user)
        # Never silently merge by email, including third-party Google emails.
        # First-time Google registration/linking requires fresh mailbox proof.
        return start_challenge(email, subject)
