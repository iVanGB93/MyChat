from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

router = DefaultRouter()
router.register(r"contacts", views.ContactViewSet, basename="contact")

urlpatterns = [
    # Auth
    path("register/", views.RegisterView.as_view(), name="register"),
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Profile
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("search/", views.UserSearchView.as_view(), name="user-search"),
    # Push notifications
    path("push-token/", views.RegisterPushTokenView.as_view(), name="push-token"),
    # Pending notifications (background fetch)
    path("notifications/pending/", views.PendingNotificationsView.as_view(), name="pending-notifications"),
    # Contacts
    path("", include(router.urls)),
]
