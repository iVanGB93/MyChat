from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from . import views
from .auth import AxonicTokenObtainPairView

router = DefaultRouter()
router.register(r"contacts", views.ContactViewSet, basename="contact")
router.register(r"blocked", views.BlockedUserViewSet, basename="blocked")

urlpatterns = [
    # Auth
    path("register/", views.RegisterView.as_view(), name="register"),
    path("token/", AxonicTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Profile
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/change-password/", views.PasswordChangeView.as_view(), name="profile-change-password"),
    path("profile/delete/", views.DeleteAccountView.as_view(), name="profile-delete"),
    path("profile/logout-all/", views.LogoutAllSessionsView.as_view(), name="profile-logout-all"),
    path("search/", views.UserSearchView.as_view(), name="user-search"),
    # Push notifications
    path("push-token/", views.RegisterPushTokenView.as_view(), name="push-token"),
    # Pending notifications (background fetch)
    path("notifications/pending/", views.PendingNotificationsView.as_view(), name="pending-notifications"),
    # Contacts
    path("", include(router.urls)),
]
