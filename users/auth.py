from django.contrib.auth import get_user_model
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


class AxonicTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Allow login with either username or email, case-insensitively.
    The API payload remains compatible with SimpleJWT:
      {"username": "<username-or-email>", "password": "..."}

    Also embeds the user's `token_version` in the issued token so we can
    invalidate all previously-issued JWTs by bumping that counter
    (used by "Logout all devices").
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["tv"] = getattr(user, "token_version", 0)
        return token

    def validate(self, attrs):
        identifier = (attrs.get("username") or "").strip()
        if identifier:
            if "@" in identifier:
                user = User.objects.filter(email__iexact=identifier).only("username").first()
                if user:
                    attrs["username"] = user.username
            else:
                user = User.objects.filter(username__iexact=identifier).only("username").first()
                if user:
                    attrs["username"] = user.username
        return super().validate(attrs)


class AxonicJWTAuthentication(JWTAuthentication):
    """JWT auth that also verifies the token's `tv` (token_version) claim
    matches the current value on the user record. Mismatch → 401.

    This lets "Logout all devices" instantly invalidate every previously
    issued access token for that user.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        token_tv = validated_token.get("tv", 0)
        if int(token_tv) != int(getattr(user, "token_version", 0)):
            raise InvalidToken("Token has been revoked.")
        return user


# NOTE: `TokenObtainPairView` is imported lazily to avoid a circular
# import — `users.auth` is loaded by DRF while resolving
# `DEFAULT_AUTHENTICATION_CLASSES`, and pulling in SimpleJWT's view
# module at that time re-enters DRF's settings loader.
def _build_token_obtain_pair_view():
    from rest_framework_simplejwt.views import TokenObtainPairView

    class AxonicTokenObtainPairView(TokenObtainPairView):
        serializer_class = AxonicTokenObtainPairSerializer

    return AxonicTokenObtainPairView


class _LazyTokenObtainPairView:
    """Defers the SimpleJWT view import until `.as_view()` is called."""

    @classmethod
    def as_view(cls, **initkwargs):
        return _build_token_obtain_pair_view().as_view(**initkwargs)


AxonicTokenObtainPairView = _LazyTokenObtainPairView
