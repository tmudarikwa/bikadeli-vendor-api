import sys
from pathlib import Path

from django.test import Client, SimpleTestCase
from django.urls import resolve

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))


class VendorApiUrlTests(SimpleTestCase):
    urlconf = 'vendor_api.urls'

    def test_vendor_login_and_profile_routes_resolve(self):
        self.assertEqual(resolve('/api/v1/vendors/login/', urlconf=self.urlconf).url_name, 'vendor-api-login')
        self.assertEqual(
            resolve('/api/v1/vendors/profile/me/', urlconf=self.urlconf).url_name,
            'vendor-profile-my-profile',
        )

    def test_health_routes_resolve(self):
        self.assertEqual(resolve('/health/', urlconf=self.urlconf).url_name, 'health-liveness')
        self.assertEqual(resolve('/health/ready/', urlconf=self.urlconf).url_name, 'health-readiness')
        self.assertEqual(Client().get('/health/').status_code, 200)
