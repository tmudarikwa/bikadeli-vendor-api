from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from vendor_catalog.management.commands.sync_food_menu_snapshots import Command
from vendor_catalog.models import (
    RestaurantMenuItem,
    VendorProfile,
    VendorSanctionsScreening,
)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = 'test response'

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)


class FoodMenuSyncCommandTests(SimpleTestCase):
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.time.sleep')
    def test_transient_errors_are_retried(self, sleep):
        client = FakeClient([FakeResponse(503), FakeResponse(200)])
        result = Command._post_with_retries(client, 'https://orders.example.test/menu', 's' * 32, {}, 2)
        self.assertIsNone(result)
        self.assertEqual(client.calls, 2)
        sleep.assert_called_once_with(1)

    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.time.sleep')
    def test_auth_errors_are_not_retried(self, sleep):
        client = FakeClient([FakeResponse(401)])
        result = Command._post_with_retries(client, 'https://orders.example.test/menu', 's' * 32, {}, 3)
        self.assertIn('HTTP 401', result)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()


class FoodMenuSanctionsEligibilityTests(TestCase):
    def test_confirmed_sanctions_match_disables_vendor_in_orders_snapshot(self):
        vendor = VendorProfile.objects.create(
            owner_user_id=700,
            business_name='Sanctioned Vendor',
            category='RESTAURANT',
            is_active=True,
        )
        item = RestaurantMenuItem.objects.create(
            vendor_id=vendor.id,
            name='Test meal',
            price='10.00',
        )
        VendorSanctionsScreening.objects.create(
            vendor=vendor,
            screening_type='OFAC',
            status='CONFIRMED_MATCH',
        )

        payload = Command._payload(item, vendor)

        self.assertFalse(payload['vendor_is_active'])
