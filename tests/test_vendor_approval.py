from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import DatabaseError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from vendor_catalog.models import (
    VendorCatalogSyncOutbox,
    VendorNotificationOutbox,
    VendorProfile,
    VendorProfileAuditLog,
)


@override_settings(
    IDENTITY_SERVICE_URL='http://identity.test',
    VENDOR_REGISTRATION_TOKEN='vendor-registration-test-token',
    VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
)
class VendorApprovalTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.channel_layer = get_channel_layer()
        self.vendor = VendorProfile.objects.create(
            owner_user_id=7001,
            business_name='Pending Vendor',
            category='RESTAURANT',
            is_active=False,
            is_verified=False,
        )
        self.path = f'/internal/v1/vendors/{self.vendor.pk}/verify/'
        self.notification_channel = async_to_sync(self.channel_layer.new_channel)()
        async_to_sync(self.channel_layer.group_add)(
            f'user_{self.vendor.owner_user_id}',
            self.notification_channel,
        )
        self.payload = {
            'is_verified': True,
            'actor_id': 44,
            'actor_email': 'admin@example.test',
            'request_id': 'request-123',
        }

    @patch('vendor_catalog.views.httpx.post')
    def test_approval_activates_identity_and_vendor_and_records_audit(self, post):
        post.return_value = Mock(status_code=200)
        existing_notification = VendorNotificationOutbox.objects.create(
            vendor=self.vendor,
            recipient_user_id=self.vendor.owner_user_id,
            action='ALERT_SENT_30',
            expiry_date='2030-01-01',
            idempotency_key='vendor-kyc-expiry:before-approval',
            title='Existing alert',
            body='Existing notification.',
        )

        response = self.client.post(
            self.path,
            self.payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['is_verified'], True)
        self.assertEqual(response.data['is_active'], True)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_verified)
        self.assertTrue(self.vendor.is_active)
        post.assert_called_once_with(
            'http://identity.test/internal/v1/identity/vendor-accounts/7001/activate/',
            headers={'X-Vendor-Registration-Token': 'vendor-registration-test-token'},
            timeout=10.0,
        )
        audit = VendorProfileAuditLog.objects.get(vendor=self.vendor)
        self.assertEqual(audit.actor_user_id, 44)
        self.assertEqual(audit.actor_email, 'admin@example.test')
        self.assertEqual(audit.request_id, 'request-123')
        self.assertFalse(audit.previous_is_active)
        self.assertTrue(audit.is_active)
        self.assertTrue(VendorCatalogSyncOutbox.objects.filter(vendor_id=self.vendor.id).exists())
        notification = VendorNotificationOutbox.objects.get(vendor=self.vendor, action='STATUS_CHANGED')
        self.assertEqual(notification.action, 'STATUS_CHANGED')
        self.assertEqual(notification.status, 'PUBLISHED')
        self.assertEqual(notification.title, 'Vendor Status')
        self.assertEqual(notification.body, 'Your account has been verified.')
        self.assertEqual(notification.data['is_verified'], True)
        existing_notification.refresh_from_db()
        self.assertEqual(existing_notification.status, 'PENDING')
        event = async_to_sync(self.channel_layer.receive)(self.notification_channel)
        self.assertEqual(event['notification_id'], notification.id)
        self.assertEqual(event['title'], 'Vendor Status')

    @patch('vendor_catalog.views.httpx.post')
    def test_identity_failure_leaves_vendor_pending_for_safe_retry(self, post):
        import httpx

        post.side_effect = httpx.ConnectError('identity unavailable')

        response = self.client.post(
            self.path,
            self.payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(response.status_code, 503)
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_verified)
        self.assertFalse(self.vendor.is_active)
        self.assertFalse(VendorProfileAuditLog.objects.exists())
        self.assertFalse(VendorCatalogSyncOutbox.objects.exists())

    @patch('vendor_catalog.views.httpx.post')
    def test_identity_rejection_leaves_vendor_pending(self, post):
        post.return_value = Mock(status_code=503)

        response = self.client.post(
            self.path,
            self.payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(response.status_code, 503)
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_verified)
        self.assertFalse(self.vendor.is_active)
        self.assertFalse(VendorProfileAuditLog.objects.exists())

    @patch('vendor_catalog.notification_outbox.get_channel_layer', return_value=None)
    @patch('vendor_catalog.views.httpx.post')
    def test_notification_failure_is_logged_and_notification_remains_durable(self, post, _get_channel_layer):
        post.return_value = Mock(status_code=200)

        with self.assertLogs('vendor_catalog.notification_outbox', level='ERROR'):
            response = self.client.post(
                self.path,
                self.payload,
                format='json',
                HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_verified)
        notification = VendorNotificationOutbox.objects.get(vendor=self.vendor)
        self.assertEqual(notification.status, 'PENDING')
        self.assertEqual(notification.attempts, 1)
        self.assertEqual(notification.body, 'Your account has been verified.')

    @patch('vendor_catalog.views.VendorProfileAuditLog.objects.create')
    @patch('vendor_catalog.views.httpx.post')
    def test_retry_completes_after_identity_activated_but_local_write_failed(self, post, create_audit):
        post.return_value = Mock(status_code=200)

        def fail_once(**kwargs):
            if create_audit.call_count == 1:
                raise DatabaseError('audit unavailable')
            return VendorProfileAuditLog.objects.get_queryset().create(**kwargs)

        create_audit.side_effect = fail_once
        self.client.raise_request_exception = False

        interrupted = self.client.post(
            self.path,
            self.payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(interrupted.status_code, 500)
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_verified)
        self.assertFalse(self.vendor.is_active)
        self.assertFalse(VendorProfileAuditLog.objects.exists())
        self.assertFalse(VendorCatalogSyncOutbox.objects.exists())

        retried = self.client.post(
            self.path,
            self.payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(post.call_count, 2)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_verified)
        self.assertTrue(self.vendor.is_active)
        self.assertEqual(VendorProfileAuditLog.objects.count(), 1)

    @patch('vendor_catalog.views.httpx.post')
    def test_unverification_does_not_deactivate_identity_or_vendor_account(self, post):
        self.vendor.is_verified = True
        self.vendor.is_active = True
        self.vendor.save()
        payload = {**self.payload, 'is_verified': False}

        response = self.client.post(
            self.path,
            payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(response.status_code, 200, response.data)
        post.assert_not_called()
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_verified)
        self.assertTrue(self.vendor.is_active)
        self.assertEqual(VendorProfileAuditLog.objects.get().action, 'UNVERIFY')
        notification = VendorNotificationOutbox.objects.get(vendor=self.vendor)
        self.assertEqual(notification.action, 'STATUS_CHANGED')
        self.assertEqual(notification.body, 'Your account has been unverified.')

    @patch('vendor_catalog.views.httpx.post')
    def test_internal_verification_requires_token_and_boolean_input(self, post):
        unauthorized = self.client.post(self.path, self.payload, format='json')
        invalid = self.client.post(
            self.path,
            {**self.payload, 'is_verified': 'false'},
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-admin-test-token',
        )

        self.assertEqual(unauthorized.status_code, 404)
        self.assertEqual(invalid.status_code, 400)
        post.assert_not_called()
