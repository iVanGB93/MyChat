from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import media_views, views

router = DefaultRouter()
router.register(r"rooms", views.ChatRoomViewSet, basename="chatroom")

urlpatterns = [
    path("", include(router.urls)),
    path("messages/send/", views.send_message, name="send-message"),
    path("messages/ack/", views.ack_message_delivery, name="ack-message-delivery"),
    path("messages/ack-batch/", views.ack_message_delivery_batch, name="ack-message-delivery-batch"),
    path("messages/delivery-status/", views.message_delivery_status, name="message-delivery-status"),
    path("media/", media_views.upload_media, name="media-upload"),
    path("media/initiate/", media_views.initiate_media_upload, name="media-upload-initiate"),
    path("media/<uuid:media_id>/", media_views.download_media, name="media-download"),
    path("media/<uuid:media_id>/complete/", media_views.complete_media_upload, name="media-upload-complete"),
    path(
        "media/<uuid:media_id>/downloaded/",
        media_views.confirm_media_downloaded,
        name="media-confirm-downloaded",
    ),
]
