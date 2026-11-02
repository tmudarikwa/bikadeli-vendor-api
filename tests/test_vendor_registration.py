from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from vendor_catalog.models import VendorProfile


@override_settings(
    IDENTITY_SERVICE_URL='http://identity.test',
    VENDOR_REGISTRATION_TOKEN='vendor-registration-test-token',
)
class VendorRegistrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {
            'email': 'new-vendor@example.test',
            'first_name': 'New',
            'last_name': 'Vendor',
            'business_name': 'Bika Foods',
            'category': 'RESTAURANT',
            'business_address': 'Harare',
        }

    @patch('vendor_catalog.views.httpx.post')
    def test_registration_creates_identity_and_pending_vendor_profile(self, post):
        post.return_value = Mock(status_code=201, json=lambda: {'user_id': 7001})

        response = self.client.post('/api/v1/vendors/register/', self.payload, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data,
            {
                'message': (
                    'Application submitted successfully. Our team will review your application and contact you shortly.'
                ),
            },
        )
        profile = VendorProfile.objects.get(owner_user_id=7001)
        self.assertEqual(profile.business_name, 'Bika Foods')
        self.assertEqual(profile.business_email, 'new-vendor@example.test')
        self.assertFalse(profile.is_active)
        self.assertFalse(profile.is_verified)
        self.assertEqual(post.call_args.kwargs['json']['email'], self.payload['email'])
        self.assertEqual(
            post.call_args.kwargs['headers']['X-Vendor-Registration-Token'],
            'vendor-registration-test-token',
        )

    @patch('vendor_catalog.views.httpx.post')
    def test_registration_retry_is_idempotent(self, post):
        post.return_value = Mock(status_code=201, json=lambda: {'user_id': 7002})

        first = self.client.post('/api/v1/vendors/register/', self.payload, format='json')
        second = self.client.post('/api/v1/vendors/register/', self.payload, format='json')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VendorProfile.objects.filter(owner_user_id=7002).count(), 1)

    @patch('vendor_catalog.views.httpx.post')
    def test_registration_returns_retryable_error_when_identity_is_unavailable(self, post):
        import httpx

        post.side_effect = httpx.ConnectError('identity unavailable')

        response = self.client.post('/api/v1/vendors/register/', self.payload, format='json')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.data,
            {'error': 'Vendor registration is temporarily unavailable.'},
        )
        self.assertEqual(VendorProfile.objects.count(), 0)
