from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Contact
from .serializers import ContactSerializer, UserRegistrationSerializer, UserSerializer

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """Public endpoint — register a new user."""

    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = (permissions.AllowAny,)


class ProfileView(generics.RetrieveUpdateAPIView):
    """Retrieve or update the authenticated user's profile."""

    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


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


class UserSearchView(generics.ListAPIView):
    """Search users by username (for adding contacts)."""

    serializer_class = UserSerializer

    def get_queryset(self):
        query = self.request.query_params.get("q", "")
        if query:
            return User.objects.filter(username__icontains=query).exclude(
                id=self.request.user.id
            )[:20]
        return User.objects.none()


class ContactViewSet(viewsets.ModelViewSet):
    """CRUD for the authenticated user's contacts."""

    serializer_class = ContactSerializer

    def get_queryset(self):
        return Contact.objects.filter(owner=self.request.user).select_related("contact")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
