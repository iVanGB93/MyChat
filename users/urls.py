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
    # Email-verification registration flow
    path("register/request/", views.RegisterRequestView.as_view(), name="register-request"),
    path("register/verify/", views.RegisterVerifyView.as_view(), name="register-verify"),
    path("register/resend/", views.RegisterResendView.as_view(), name="register-resend"),
    path("password/reset/request/", views.PasswordResetRequestView.as_view(), name="password-reset-request"),
    path("password/reset/verify/", views.PasswordResetVerifyView.as_view(), name="password-reset-verify"),
    path("password/reset/confirm/", views.PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("password/reset/resend/", views.PasswordResetResendView.as_view(), name="password-reset-resend"),
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
    path("push-token/unregister/", views.UnregisterPushTokenView.as_view(), name="push-token-unregister"),
    # Pending notifications (background fetch)
    path("notifications/pending/", views.PendingNotificationsView.as_view(), name="pending-notifications"),
    # Contacts
    path("", include(router.urls)),
]
