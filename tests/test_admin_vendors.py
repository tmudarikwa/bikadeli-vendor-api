from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status

from vendor_catalog.models import (
    VendorKYCDocument,
    VendorKYCVerification,
    VendorProfile,
    VendorProfileAuditLog,
    VendorSanctionsScreening,
    VendorTaxInfo,
)

TOKEN = 'v' * 40
HEADERS = {'HTTP_X_VENDOR_ADMIN_TOKEN': TOKEN}
ACTOR = {'actor_id': 7, 'actor_email': 'admin@example.com'}
LIST_URL = '/internal/v1/admin/vendors/'


@override_settings(VENDOR_ADMIN_TOKEN=TOKEN, VENDOR_ADMIN_OPS_ENABLED=True)
class AdminVendorOpsTests(TestCase):
    def setUp(self):
        self.vendor = VendorProfile.objects.create(
            owner_user_id=1, business_name='Shop', category='restaurant', business_email='s@example.com'
        )

    def test_requires_token_and_gate(self):
        self.assertEqual(self.client.get(LIST_URL).status_code, 404)
        with override_settings(VENDOR_ADMIN_OPS_ENABLED=False):
            self.assertEqual(self.client.get(LIST_URL, **HEADERS).status_code, 404)

    def test_list_filters_and_search(self):
        VendorProfile.objects.create(owner_user_id=2, business_name='Other', category='grocery', is_verified=True)
        body = self.client.get(LIST_URL, {'verified': 'true'}, **HEADERS).json()
        self.assertEqual((body['total'], body['results'][0]['business_name']), (1, 'Other'))
        body = self.client.get(LIST_URL, {'search': 'shop'}, **HEADERS).json()
        self.assertEqual(body['results'][0]['email'], 's@example.com')
        self.assertIsNone(body['results'][0]['lifecycle_state'])
        self.assertEqual(self.client.get(LIST_URL, {'page': 'x'}, **HEADERS).status_code, 400)

    def test_toggle_active_is_audited(self):
        response = self.client.post(
            f'{LIST_URL}{self.vendor.id}/toggle-active/', ACTOR, content_type='application/json', **HEADERS
        )
        self.assertEqual(response.json(), {'status': 'ok', 'is_active': False})
        entry = VendorProfileAuditLog.objects.get()
        self.assertEqual((entry.action, entry.previous_is_active, entry.is_active), ('TOGGLE_ACTIVE', True, False))

    def test_toggle_open_and_actor_required(self):
        url = f'{LIST_URL}{self.vendor.id}/toggle-open/'
        self.assertEqual(self.client.post(url, {}, content_type='application/json', **HEADERS).status_code, 400)
        response = self.client.post(url, {**ACTOR, 'is_open': True}, content_type='application/json', **HEADERS)
        self.assertTrue(response.json()['is_open'])
        self.assertEqual(
            self.client.post('/internal/v1/admin/vendors/999/toggle-open/', ACTOR, content_type='application/json', **HEADERS).status_code,
            404,
        )


@override_settings(
    VENDOR_PAYOUT_SAFETY_ENABLED=True,
    VENDOR_ADMIN_TOKEN=TOKEN,
    IDENTITY_SERVICE_URL='http://identity.test',
    IDENTITY_USER_LOOKUP_TOKEN='i' * 40,
    UPSTREAM_TIMEOUT_SECONDS=2,
)
class VendorPayoutSafetyTests(TestCase):
    url = '/internal/v1/admin/vendors/{}/payout-safety/'

    def setUp(self):
        self.profile = VendorProfile.objects.create(
            owner_user_id=71,
            business_name='Payout Shop',
            category='restaurant',
        )

    def identity_response(self, active=True):
        response = Mock(status_code=status.HTTP_200_OK)
        response.json.return_value = {'user_id': 71, 'is_active_vendor': active}
        return response

    def get_safety(self, owner_user_id=71, **headers):
        return self.client.get(
            self.url.format(owner_user_id),
            **(headers or HEADERS),
        )

    @patch('vendor_catalog.admin_vendors.httpx.post')
    def test_verified_active_vendor_is_eligible(self, identity_post):
        VendorKYCVerification.objects.create(vendor=self.profile, overall_status='VERIFIED')
        identity_post.return_value = self.identity_response()

        response = self.get_safety()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'vendor_user_id': 71, 'eligible': True, 'holds': []},
        )
        self.assertEqual(response['Cache-Control'], 'no-store')
        identity_post.assert_called_once()

    @patch('vendor_catalog.admin_vendors.httpx.post')
    def test_compliance_and_identity_holds_are_reported(self, identity_post):
        VendorKYCVerification.objects.create(vendor=self.profile, overall_status='VERIFIED')
        VendorKYCDocument.objects.create(
            vendor=self.profile,
            document_type='NATIONAL_ID',
            file='vendors/kyc/id.pdf',
            file_size=20,
            original_filename='id.pdf',
            expiry_date=timezone.localdate() - timedelta(days=1),
            verification_status='VERIFIED',
        )
        VendorTaxInfo.objects.create(
            vendor=self.profile,
            tax_clearance_expiry=timezone.localdate() - timedelta(days=1),
        )
        VendorSanctionsScreening.objects.create(
            vendor=self.profile,
            screening_type='OFAC',
            status='CONFIRMED_MATCH',
        )
        self.profile.is_active = False
        self.profile.save(update_fields=['is_active'])
        identity_post.return_value = self.identity_response(active=False)

        response = self.get_safety()

        self.assertEqual(
            response.json()['holds'],
            ['KYC_EXPIRED', 'TAX_CLEARANCE_EXPIRED', 'SANCTIONS_MATCH', 'VENDOR_INACTIVE'],
        )
        self.assertFalse(response.json()['eligible'])

    def test_missing_profile_returns_explicit_hold(self):
        response = self.get_safety(owner_user_id=72)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'vendor_user_id': 72, 'eligible': False, 'holds': ['NO_VENDOR_PROFILE']},
        )

    @patch('vendor_catalog.admin_vendors.httpx.post')
    def test_incomplete_kyc_blocks_payout(self, identity_post):
        identity_post.return_value = self.identity_response()

        response = self.get_safety()

        self.assertEqual(response.json()['holds'], ['KYC_INCOMPLETE'])
        self.assertFalse(response.json()['eligible'])

    @patch('vendor_catalog.admin_vendors.httpx.post')
    def test_identity_unavailable_fails_closed(self, identity_post):
        import httpx

        identity_post.side_effect = httpx.ConnectError('unavailable')

        response = self.get_safety()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'Vendor identity status is temporarily unavailable.')

    def test_gate_and_token_are_required(self):
        self.assertEqual(
            self.client.get(self.url.format(71)).status_code,
            404,
        )
        with override_settings(VENDOR_PAYOUT_SAFETY_ENABLED=False):
            self.assertEqual(self.get_safety().status_code, 404)
