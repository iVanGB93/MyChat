"""
Root URL configuration for ChatConnect.
"""

from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    # Web interface (templates)
    path("", views.login_view, name="home"),
    path("login/", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("chat/<uuid:room_id>/", views.chat_room_view, name="chat-room"),
    path("calls/", views.calls_view, name="calls"),
    # Admin
    path("admin/", admin.site.urls),
    # REST API
    path("api/users/", include("users.urls")),
    path("api/chat/", include("chat.urls")),
    path("api/calls/", include("calls.urls")),
]
