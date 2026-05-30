from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

User = get_user_model()


class AxonicTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Allow login with either username or email, case-insensitively.
    The API payload remains compatible with SimpleJWT:
      {"username": "<username-or-email>", "password": "..."}
    """

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


class AxonicTokenObtainPairView(TokenObtainPairView):
    serializer_class = AxonicTokenObtainPairSerializer
