from decimal import Decimal

from django.test import TestCase, override_settings

from vendor_catalog.models import (
    VendorPriceChange,
    VendorProfile,
    VendorPromotion,
    VendorScorecard,
)

TOKEN = 'v' * 40
HEADERS = {'HTTP_X_VENDOR_ADMIN_TOKEN': TOKEN}
ACTOR = {'actor_id': 7, 'actor_email': 'admin@example.com'}


@override_settings(VENDOR_ADMIN_TOKEN=TOKEN, VENDOR_INTEGRITY_SERVICE_ENABLED=True)
class VendorIntegrityTests(TestCase):
    def setUp(self):
        self.vendor = VendorProfile.objects.create(owner_user_id=1, business_name='Shop', category='restaurant')

    def _change(self, **kw):
        return VendorPriceChange.objects.create(
            vendor=self.vendor,
            content_type='MENU_ITEM',
            object_id=1,
            old_price=Decimal('1.00'),
            new_price=Decimal('3.00'),
            change_pct=Decimal('200.00'),
            requires_approval=True,
            **kw,
        )

    def test_requires_token_and_gate(self):
        self.assertEqual(self.client.get('/internal/v1/admin/vendors/price-changes/').status_code, 404)
        with override_settings(VENDOR_INTEGRITY_SERVICE_ENABLED=False):
            response = self.client.get('/internal/v1/admin/vendors/price-changes/', **HEADERS)
        self.assertEqual(response.status_code, 404)

    def test_price_change_list_and_review_once(self):
        change = self._change()
        listing = self.client.get('/internal/v1/admin/vendors/price-changes/?pending=true', **HEADERS).json()
        self.assertEqual(listing['count'], 1)
        url = f'/internal/v1/admin/vendors/price-changes/{change.id}/review/'
        first = self.client.post(url, {**ACTOR, 'approve': True}, content_type='application/json', **HEADERS)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['is_approved'])
        second = self.client.post(url, {**ACTOR, 'approve': False}, content_type='application/json', **HEADERS)
        self.assertEqual(second.status_code, 409)

    def test_promotion_review_and_set_active(self):
        promo = VendorPromotion.objects.create(
            vendor=self.vendor, code='X', discount_type='PERCENT', discount_value=Decimal(10)
        )
        base = f'/internal/v1/admin/vendors/promotions/{promo.id}'
        reject = self.client.post(
            f'{base}/review/', {**ACTOR, 'approve': False}, content_type='application/json', **HEADERS
        )
        self.assertEqual(reject.status_code, 400)
        approve = self.client.post(
            f'{base}/review/', {**ACTOR, 'approve': True}, content_type='application/json', **HEADERS
        )
        self.assertEqual(approve.json()['status'], 'ACTIVE')
        off = self.client.post(
            f'{base}/set-active/', {**ACTOR, 'is_active': False}, content_type='application/json', **HEADERS
        )
        self.assertFalse(off.json()['is_active'])
        listing = self.client.get('/internal/v1/admin/vendors/promotions/?status=ACTIVE', **HEADERS).json()
        self.assertEqual(listing['count'], 1)

    def test_set_active_rejects_unapproved(self):
        promo = VendorPromotion.objects.create(
            vendor=self.vendor, code='Y', discount_type='PERCENT', discount_value=Decimal(5)
        )
        response = self.client.post(
            f'/internal/v1/admin/vendors/promotions/{promo.id}/set-active/',
            {**ACTOR, 'is_active': True},
            content_type='application/json',
            **HEADERS,
        )
        self.assertEqual(response.status_code, 409)

    def test_csv_export_neutralises_formulas(self):
        self.vendor.business_name = '=HYPERLINK("x")'
        self.vendor.save()
        self._change()
        response = self.client.get('/internal/v1/admin/vendors/price-changes/export-csv/', **HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        body = response.content.decode()
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(',=HYPERLINK', body)

    def test_scorecards_sorted(self):
        VendorScorecard.objects.create(vendor=self.vendor, compliance_score=Decimal(50))
        response = self.client.get('/internal/v1/admin/vendors/scorecards/?sort=bogus&direction=desc', **HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'][0]['vendor_name'], 'Shop')
