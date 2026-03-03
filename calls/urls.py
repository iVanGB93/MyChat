from django.urls import path

from . import views

urlpatterns = [
    path("ice-config/", views.IceConfigView.as_view(), name="ice-config"),
    path("initiate/", views.InitiateCallView.as_view(), name="initiate-call"),
    path("<uuid:call_id>/join/", views.JoinCallView.as_view(), name="join-call"),
    path("<uuid:call_id>/end/", views.EndCallView.as_view(), name="end-call"),
    path("<uuid:call_id>/status/", views.CallStatusView.as_view(), name="call-status"),
    path("history/", views.CallHistoryView.as_view(), name="call-history"),
]
