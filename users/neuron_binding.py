"""Short-lived migration tickets; neuron possession is verified by the axon host."""
import hashlib
import hmac
import json
import re
import time

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .models import UserDevice


class BindingThrottle(UserRateThrottle):
    scope = "neuron_binding"
    rate = "30/min"


class NeuronBindingView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BindingThrottle]

    def post(self, request):
        account = request.data.get("account")
        installation = request.data.get("installation_id")
        if (not isinstance(account, str) or not re.fullmatch(r"axonic:1:[a-f0-9]{64}", account)
                or not isinstance(installation, str) or not 1 <= len(installation) <= 128):
            return Response({"error": "Invalid identity or installation"}, status=400)
        device = UserDevice.objects.filter(user=request.user, installation_id=installation,
                                           is_active=True).first()
        if not device or not device.fcm_token:
            return Response({"error": "Register this installation first"}, status=409)
        secret = getattr(settings, "NEURON_PUSH_BRIDGE_SECRET", "")
        if not re.fullmatch(r"[a-f0-9]{64}", secret):
            return Response({"error": "Migration unavailable"}, status=503)
        payload = json.dumps({"purpose": "axonic-push-binding-v1", "user": request.user.pk,
                              "installation": installation, "account": account,
                              "tokenHash": hashlib.sha256(device.fcm_token.encode()).hexdigest(),
                              "expiresAt": int(time.time() * 1000) + 60000}, separators=(",", ":"))
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        response = Response({"payload": payload, "signature": signature})
        response["Cache-Control"] = "no-store"
        return response
