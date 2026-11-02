import hashlib
import hmac
import time

from django.conf import settings
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    VendorNotificationOutbox,
    VendorPreparationTimeUpdate,
    VendorProfile,
)
from .notification_outbox import publish_pending_vendor_notifications


class PreparationTimeUpdateSerializer(serializers.Serializer):
    vendor_user_id = serializers.IntegerField(min_value=1)
    order_id = serializers.IntegerField(min_value=1)
    preparation_time_mins = serializers.IntegerField(min_value=1, max_value=65535)


class OrderCancellationNotificationSerializer(serializers.Serializer):
    order_id = serializers.IntegerField(min_value=1)
    vendor_user_id = serializers.IntegerField(min_value=1)
    order_number = serializers.CharField(max_length=50)
    reason = serializers.CharField(max_length=500, allow_blank=True)


class InternalOrderCancellationNotificationView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        secret = settings.ORDER_CANCELLATION_NOTIFICATION_SECRET
        timestamp = request.headers.get("X-Order-Cancellation-Timestamp", "")
        signature = request.headers.get("X-Order-Cancellation-Signature", "")
        try:
            request_time = int(timestamp)
        except (TypeError, ValueError):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        if (
            not secret
            or abs(int(time.time()) - request_time) > 300
            or len(request.body) > 4096
        ):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        expected = hmac.new(
            secret.encode(),
            timestamp.encode() + b"." + request.body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = OrderCancellationNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        key = f"food-order-cancel:{values['order_id']}"
        with transaction.atomic():
            vendor = VendorProfile.objects.filter(
                owner_user_id=values["vendor_user_id"]
            ).first()
            if vendor is None:
                return Response(
                    {"error": "Vendor profile not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            notification, created = VendorNotificationOutbox.objects.get_or_create(
                idempotency_key=key,
                defaults={
                    "vendor": vendor,
                    "recipient_user_id": vendor.owner_user_id,
                    "action": "ORDER_CANCELLED",
                    "title": f"Order #{values['order_number']} Cancelled",
                    "body": "Order was cancelled by the customer.",
                    "data": {
                        "order_id": str(values["order_id"]),
                        "status": "CANCELLED",
                        "reason": values["reason"],
                    },
                },
            )
            if (
                notification.vendor_id != vendor.pk
                or notification.data.get("order_id") != str(values["order_id"])
                or notification.data.get("reason") != values["reason"]
                or notification.title != f"Order #{values['order_number']} Cancelled"
            ):
                return Response(
                    {"error": "Order cancellation notification conflicts with an existing event."},
                    status=status.HTTP_409_CONFLICT,
                )

        delivery = publish_pending_vendor_notifications(
            notification_ids=[notification.pk]
        )
        if delivery["failed"]:
            return Response(
                {"error": "Vendor notification could not be published."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "order_id": values["order_id"],
                "status": "QUEUED",
                "already_queued": not created,
            },
            status=status.HTTP_200_OK,
        )


class InternalVendorPreparationTimeUpdateView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        secret = settings.VENDOR_PROFILE_UPDATE_SECRET
        timestamp = request.headers.get("X-Vendor-Profile-Timestamp", "")
        signature = request.headers.get("X-Vendor-Profile-Signature", "")
        try:
            request_time = int(timestamp)
        except (TypeError, ValueError):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        if (
            not secret
            or abs(int(time.time()) - request_time) > 300
            or len(request.body) > 4096
        ):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        expected = hmac.new(
            secret.encode(),
            timestamp.encode() + b"." + request.body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response({"error": "Invalid request signature"}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = PreparationTimeUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data

        with transaction.atomic():
            vendor = (
                VendorProfile.objects.select_for_update()
                .filter(owner_user_id=values["vendor_user_id"])
                .first()
            )
            if vendor is None:
                return Response({"error": "Vendor profile not found"}, status=status.HTTP_404_NOT_FOUND)

            existing = VendorPreparationTimeUpdate.objects.filter(
                order_id=values["order_id"]
            ).first()
            if existing is not None:
                if (
                    existing.vendor_id != vendor.pk
                    or existing.new_minutes != values["preparation_time_mins"]
                ):
                    return Response(
                        {"error": "Order preparation time was already recorded differently"},
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(
                    {
                        "vendor_user_id": vendor.owner_user_id,
                        "preparation_time_mins": existing.new_minutes,
                        "already_applied": True,
                    }
                )

            previous_minutes = vendor.preparation_time_mins
            vendor.preparation_time_mins = values["preparation_time_mins"]
            vendor.save(update_fields=["preparation_time_mins", "updated_at"])
            VendorPreparationTimeUpdate.objects.create(
                vendor=vendor,
                order_id=values["order_id"],
                previous_minutes=previous_minutes,
                new_minutes=values["preparation_time_mins"],
            )

        return Response(
            {
                "vendor_user_id": vendor.owner_user_id,
                "preparation_time_mins": vendor.preparation_time_mins,
                "already_applied": False,
            },
            status=status.HTTP_200_OK,
        )
