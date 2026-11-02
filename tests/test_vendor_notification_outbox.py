import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

import jwt
from asgiref.sync import async_to_sync
from bikadeli_common.notifications import (
    build_user_notification_event,
    user_notification_group,
)
from channels.layers import get_channel_layer
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vendor_catalog.models import (
    VendorNotificationOutbox,
    VendorNotificationPreference,
    VendorProfile,
)
from vendor_catalog.notification_outbox import publish_pending_vendor_notifications


@override_settings(
    DEBUG=True,
    JWT_SIGNING_KEY='vendor-notification-test-signing-key',
    JWT_LEGACY_SIGNING_KEY='vendor-notification-test-signing-key',
    JWT_ALLOW_LEGACY_HS256=True,
    JWT_ISSUER='',
    VENDOR_KYC_SERVICE_ENABLED=True,
    VENDOR_NOTIFICATION_TOKEN='vendor-notification-test-token',
    ORDER_CANCELLATION_NOTIFICATION_SECRET='vendor-cancellation-test-secret',
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
)
class VendorNotificationOutboxTests(TestCase):
    def setUp(self):
        self.owner_id = 8001
        self.vendor = VendorProfile.objects.create(
            owner_user_id=self.owner_id,
            business_name='Notification Vendor',
            category='RESTAURANT',
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access_token(self.owner_id)}')

    @staticmethod
    def access_token(user_id):
        return jwt.encode(
            {
                'token_type': 'access',
                'user_id': user_id,
                'iat': timezone.now(),
                'exp': timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm='HS256',
        )

    def create_notification(self, **overrides):
        return VendorNotificationOutbox.objects.create(
            vendor=self.vendor,
            recipient_user_id=self.owner_id,
            action='ALERT_SENT_30',
            expiry_date=timezone.localdate() + timedelta(days=30),
            idempotency_key='vendor-kyc-expiry:123:ALERT_SENT_30:2030-01-01',
            title='KYC Document Expiring Soon',
            body='Renew the required document.',
            data={'days_until_expiry': 30},
            **overrides,
        )

    def test_shared_event_contract_validates_identity_and_payload(self):
        self.assertEqual(user_notification_group(42), 'user_42')
        self.assertEqual(
            build_user_notification_event(
                notification_id=10,
                title='KYC Document Expiring Soon',
                body='Renew the document.',
                data={'days_until_expiry': 30},
            ),
            {
                'type': 'notification_message',
                'notification_id': 10,
                'title': 'KYC Document Expiring Soon',
                'body': 'Renew the document.',
                'data': {'days_until_expiry': 30},
                'priority': 'NORMAL',
            },
        )
        with self.assertRaises(ValueError):
            user_notification_group(0)
        with self.assertRaises(ValueError):
            build_user_notification_event(notification_id=10, title='', body='Body')

    @patch(
        'vendor_catalog.internal_views.publish_pending_vendor_notifications',
        return_value={'published': 1, 'failed': 0},
    )
    def test_internal_order_cancellation_is_signed_idempotent_and_published(self, publish):
        body = json.dumps(
            {
                'order_id': 9321,
                'vendor_user_id': self.owner_id,
                'order_number': 'FOOD-9321',
                'reason': 'Customer changed their mind',
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            settings.ORDER_CANCELLATION_NOTIFICATION_SECRET.encode(),
            timestamp.encode() + b'.' + body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            'HTTP_X_ORDER_CANCELLATION_TIMESTAMP': timestamp,
            'HTTP_X_ORDER_CANCELLATION_SIGNATURE': signature,
            'content_type': 'application/json',
        }

        first = self.client.post(
            '/internal/v1/vendors/order-cancellation-notifications/',
            body,
            **headers,
        )
        replay = self.client.post(
            '/internal/v1/vendors/order-cancellation-notifications/',
            body,
            **headers,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['status'], 'QUEUED')
        self.assertFalse(first.json()['already_queued'])
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['already_queued'])
        self.assertEqual(
            VendorNotificationOutbox.objects.filter(
                idempotency_key='food-order-cancel:9321'
            ).count(),
            1,
        )
        publish.assert_called()

    def test_pending_outbox_item_is_published_using_shared_contract(self):
        notification = self.create_notification(priority='HIGH')
        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(user_notification_group(self.owner_id), channel_name)

        result = publish_pending_vendor_notifications()

        notification.refresh_from_db()
        event = async_to_sync(channel_layer.receive)(channel_name)
        self.assertEqual(result, {'published': 1, 'failed': 0})
        self.assertEqual(notification.status, 'PUBLISHED')
        self.assertIsNotNone(notification.published_at)
        self.assertEqual(notification.attempts, 1)
        self.assertEqual(event['notification_id'], notification.id)
        self.assertEqual(event['priority'], 'HIGH')

    def test_failed_channel_delivery_is_recorded_and_left_pending(self):
        notification = self.create_notification()
        with (
            patch('vendor_catalog.notification_outbox.get_channel_layer', return_value=None),
            self.assertLogs('vendor_catalog.notification_outbox', level='ERROR'),
        ):
            result = publish_pending_vendor_notifications()

        notification.refresh_from_db()
        self.assertEqual(result, {'published': 0, 'failed': 1})
        self.assertEqual(notification.status, 'PENDING')
        self.assertEqual(notification.attempts, 1)
        self.assertIn('No channel layer', notification.last_error)

    def test_vendor_notification_api_is_owner_scoped_and_marks_read(self):
        notification = self.create_notification()
        other_vendor = VendorProfile.objects.create(
            owner_user_id=8002,
            business_name='Other Notification Vendor',
            category='RESTAURANT',
        )
        VendorNotificationOutbox.objects.create(
            vendor=other_vendor,
            recipient_user_id=other_vendor.owner_user_id,
            action='ALERT_SENT_30',
            expiry_date=timezone.localdate() + timedelta(days=30),
            idempotency_key='vendor-kyc-expiry:456:ALERT_SENT_30:2030-01-01',
            title='Other vendor alert',
            body='Private notification.',
        )

        listing = self.client.get('/api/v1/vendors/notifications/')

        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual([item['id'] for item in listing.data], [notification.id])
        self.assertEqual(listing.data[0]['category'], 'SYSTEM')
        self.assertEqual(listing.data[0]['channel'], 'IN_APP')
        marked = self.client.patch(
            '/api/v1/vendors/notifications/',
            {'notification_ids': [notification.id]},
            format='json',
        )

        self.assertEqual(marked.status_code, 200, marked.data)
        self.assertEqual(marked.data, {'updated': 1})
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

    @override_settings(VENDOR_KYC_SERVICE_ENABLED=False)
    @patch('vendor_catalog.views.proxy_to_monolith')
    def test_vendor_notification_api_is_not_gated_by_kyc_extraction_flag(self, proxy):
        notification = self.create_notification()

        response = self.client.get('/api/v1/vendors/notifications/')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item['id'] for item in response.data], [notification.id])
        proxy.assert_not_called()

    def test_notification_api_rejects_empty_or_invalid_read_ids(self):
        response = self.client.patch(
            '/api/v1/vendors/notifications/',
            {'notification_ids': [0]},
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_vendor_can_manage_owned_marketing_preferences(self):
        response = self.client.put(
            '/api/v1/vendors/notification-preferences/',
            {'channel': 'SMS', 'category': 'MARKETING', 'active': True},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['channel'], 'SMS')
        self.assertEqual(response.data['category'], 'MARKETING')
        self.assertTrue(response.data['active'])
        listing = self.client.get('/api/v1/vendors/notification-preferences/')
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(len(listing.data), 1)

    def test_internal_opt_out_check_requires_service_token_and_includes_all_scope(self):
        VendorNotificationPreference.objects.create(
            owner_user_id=self.owner_id,
            channel='SMS',
            category='ALL',
            active=True,
        )
        path = f'/internal/v1/vendors/{self.owner_id}/notification-preferences/?channel=SMS&category=PROMO'

        unauthorized = self.client.get(path)
        authorized = self.client.get(
            path,
            HTTP_X_VENDOR_NOTIFICATION_TOKEN='vendor-notification-test-token',
        )

        self.assertEqual(unauthorized.status_code, 404)
        self.assertEqual(authorized.status_code, 200, authorized.data)
        self.assertEqual(authorized.data, {'is_opted_out': True})

    def test_internal_preference_backfill_is_idempotent(self):
        path = f'/internal/v1/vendors/{self.owner_id}/notification-preferences/'
        headers = {'HTTP_X_VENDOR_NOTIFICATION_TOKEN': 'vendor-notification-test-token'}
        payload = {'channel': 'EMAIL', 'category': 'MARKETING', 'reason': 'legacy opt-out', 'active': True}

        first = self.client.put(path, payload, format='json', **headers)
        second = self.client.put(path, payload, format='json', **headers)

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(
            VendorNotificationPreference.objects.filter(owner_user_id=self.owner_id).count(),
            1,
        )
