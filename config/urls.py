"""
Root URL configuration for ChatConnect.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from . import views
from users.views import serve_blob


def health_check(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    # Health check (for Railway / load balancers)
    path("health/", health_check, name="health"),
    # Mobile app version gate (public — used to suggest/force updates)
    path("api/app/version/", views.app_version_view, name="app-version"),
    # DB-backed media (avatars, etc.) — survives ephemeral filesystems.
    path("media-db/<path:name>", serve_blob, name="media-db"),
    # Web interface (templates)
    path("", views.landing_view, name="home"),
    path("login/", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("add/<str:user_tag>/", views.invite_tag_view, name="invite-tag"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("chat/<uuid:room_id>/", views.chat_room_view, name="chat-room"),
    path("calls/", views.calls_view, name="calls"),
    # Monitoring dashboard
    path("monitor/", views.monitor_view, name="monitor"),
    path("api/monitor/", views.monitor_api, name="monitor-api"),
    path("api/monitor/routing/<int:user_id>/", views.monitor_routing_view, name="monitor-routing-api"),
    # Admin
    path("admin/", admin.site.urls),
    # REST API
    path("api/users/", include("users.urls")),
    path("api/chat/", include("chat.urls")),
    path("api/calls/", include("calls.urls")),
]

# Serve user-uploaded media (avatars, etc.) during local development.
# In production the same files are typically served by the reverse proxy
# (or via WhiteNoise's MEDIA_URL mapping for small deployments).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

