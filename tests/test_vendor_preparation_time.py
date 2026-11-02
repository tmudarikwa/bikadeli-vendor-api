import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from vendor_catalog.models import VendorPreparationTimeUpdate, VendorProfile


@override_settings(VENDOR_PROFILE_UPDATE_SECRET="vendor-profile-test-secret" * 2)
class VendorPreparationTimeUpdateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor = VendorProfile.objects.create(
            owner_user_id=700,
            business_name="Preparation Time Vendor",
            category="RESTAURANT",
        )

    def post_update(self, values, *, secret="vendor-profile-test-secret" * 2):
        raw_body = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode(),
            timestamp.encode() + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            "/internal/v1/vendors/preparation-time/",
            raw_body,
            content_type="application/json",
            HTTP_X_VENDOR_PROFILE_TIMESTAMP=timestamp,
            HTTP_X_VENDOR_PROFILE_SIGNATURE=signature,
        )

    def test_signed_update_changes_owner_profile_and_records_audit(self):
        response = self.post_update(
            {
                "vendor_user_id": self.vendor.owner_user_id,
                "order_id": 5101,
                "preparation_time_mins": 35,
            }
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.preparation_time_mins, 35)
        audit = VendorPreparationTimeUpdate.objects.get(order_id=5101)
        self.assertEqual(audit.vendor_id, self.vendor.pk)
        self.assertEqual(audit.previous_minutes, 15)
        self.assertEqual(audit.new_minutes, 35)

    def test_identical_retries_are_idempotent(self):
        values = {
            "vendor_user_id": self.vendor.owner_user_id,
            "order_id": 5102,
            "preparation_time_mins": 40,
        }

        first = self.post_update(values)
        retry = self.post_update(values)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.data["already_applied"])
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.data["already_applied"])
        self.assertEqual(VendorPreparationTimeUpdate.objects.count(), 1)

    def test_conflicting_retry_does_not_change_profile(self):
        self.post_update(
            {
                "vendor_user_id": self.vendor.owner_user_id,
                "order_id": 5103,
                "preparation_time_mins": 40,
            }
        )

        response = self.post_update(
            {
                "vendor_user_id": self.vendor.owner_user_id,
                "order_id": 5103,
                "preparation_time_mins": 45,
            }
        )

        self.assertEqual(response.status_code, 409)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.preparation_time_mins, 40)
        self.assertEqual(VendorPreparationTimeUpdate.objects.count(), 1)

    def test_invalid_signature_is_rejected(self):
        response = self.post_update(
            {
                "vendor_user_id": self.vendor.owner_user_id,
                "order_id": 5104,
                "preparation_time_mins": 40,
            },
            secret="incorrect-secret",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(VendorPreparationTimeUpdate.objects.count(), 0)

    def test_invalid_minutes_are_rejected(self):
        response = self.post_update(
            {
                "vendor_user_id": self.vendor.owner_user_id,
                "order_id": 5105,
                "preparation_time_mins": 0,
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(VendorPreparationTimeUpdate.objects.count(), 0)
