from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from vendor_catalog.models import (
    VendorKYCDocument,
    VendorKYCExpiryLog,
    VendorKYCVerification,
    VendorNotificationOutbox,
    VendorProfile,
)


@override_settings(CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
class VendorKYCExpiryCommandTests(TestCase):
    def setUp(self):
        self.vendor = VendorProfile.objects.create(
            owner_user_id=900,
            business_name='Expired KYC Vendor',
            category='RESTAURANT',
            is_open=True,
        )
        self.verification = VendorKYCVerification.objects.create(
            vendor=self.vendor,
            overall_status='VERIFIED',
            required_document_types=['NATIONAL_ID'],
        )
        self.document = VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/expired.pdf',
            file_size=32,
            original_filename='expired.pdf',
            verification_status='VERIFIED',
            is_required=True,
            expiry_date=timezone.localdate() - timedelta(days=1),
        )

    def test_dry_run_reports_changes_without_modifying_records(self):
        output = StringIO()
        call_command('check_vendor_kyc_expiry', stdout=output)

        self.document.refresh_from_db()
        self.vendor.refresh_from_db()
        self.verification.refresh_from_db()
        self.assertEqual(self.document.verification_status, 'VERIFIED')
        self.assertTrue(self.vendor.is_open)
        self.assertEqual(self.verification.overall_status, 'VERIFIED')
        self.assertFalse(VendorKYCExpiryLog.objects.exists())
        self.assertFalse(VendorNotificationOutbox.objects.exists())
        self.assertIn('expired_documents=1', output.getvalue())
        self.assertIn('vendors_to_close=1', output.getvalue())

    def test_apply_expires_document_closes_vendor_and_records_audit_once(self):
        call_command('check_vendor_kyc_expiry', '--apply')

        self.document.refresh_from_db()
        self.vendor.refresh_from_db()
        self.verification.refresh_from_db()
        self.assertEqual(self.document.verification_status, 'EXPIRED')
        self.assertFalse(self.vendor.is_open)
        self.assertEqual(self.verification.overall_status, 'EXPIRED')
        self.assertEqual(
            VendorKYCExpiryLog.objects.filter(action='DOCUMENT_EXPIRED').count(),
            1,
        )
        self.assertEqual(
            VendorKYCExpiryLog.objects.filter(action='VENDOR_SUSPENDED').count(),
            1,
        )

        call_command('check_vendor_kyc_expiry', '--apply')
        self.assertEqual(VendorKYCExpiryLog.objects.count(), 2)

    def test_does_not_close_already_closed_vendors_or_non_required_documents(self):
        self.vendor.is_open = False
        self.vendor.save(update_fields=['is_open'])
        self.document.is_required = False
        self.document.save(update_fields=['is_required'])

        call_command('check_vendor_kyc_expiry', '--apply')

        self.document.refresh_from_db()
        self.vendor.refresh_from_db()
        self.assertEqual(self.document.verification_status, 'VERIFIED')
        self.assertFalse(self.vendor.is_open)
        self.assertFalse(VendorKYCExpiryLog.objects.exists())

    def test_apply_queues_and_publishes_each_expiry_alert_once(self):
        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f'user_{self.vendor.owner_user_id}', channel_name)
        alert_cases = (
            (30, 'ALERT_SENT_30', 'KYC Document Expiring Soon'),
            (7, 'ALERT_SENT_7', 'KYC Document Expiring in 7 Days'),
            (1, 'ALERT_SENT_1', 'KYC Document Expiring TOMORROW'),
        )

        for days_until_expiry, action, title in alert_cases:
            with self.subTest(action=action):
                self.document.expiry_date = timezone.localdate() + timedelta(days=days_until_expiry)
                self.document.verification_status = 'VERIFIED'
                self.document.save(update_fields=['expiry_date', 'verification_status'])

                output = StringIO()
                call_command('check_vendor_kyc_expiry', '--apply', stdout=output)
                published_event = async_to_sync(channel_layer.receive)(channel_name)
                self.assertEqual(published_event['title'], title)
                self.assertEqual(published_event['type'], 'notification_message')

                call_command('check_vendor_kyc_expiry', '--apply')
                self.assertTrue(
                    VendorNotificationOutbox.objects.filter(
                        vendor=self.vendor,
                        document=self.document,
                        action=action,
                        status='PUBLISHED',
                    ).exists()
                )
                self.assertIn('notifications_published=1', output.getvalue())

        self.assertEqual(VendorNotificationOutbox.objects.count(), len(alert_cases))

    def test_backfilled_alert_log_prevents_duplicate_service_notification(self):
        self.document.expiry_date = timezone.localdate() + timedelta(days=30)
        self.document.verification_status = 'VERIFIED'
        self.document.save(update_fields=['expiry_date', 'verification_status'])
        VendorKYCExpiryLog.objects.create(
            vendor=self.vendor,
            document=self.document,
            action='ALERT_SENT_30',
            message='Legacy 30-day expiry alert sent.',
            metadata={'expiry_date': str(self.document.expiry_date)},
        )

        output = StringIO()
        call_command('check_vendor_kyc_expiry', '--apply', stdout=output)

        self.assertFalse(VendorNotificationOutbox.objects.exists())
        self.assertIn('notifications_queued=0', output.getvalue())
        self.assertIn('notifications_published=0', output.getvalue())

    def test_failed_notification_publication_remains_retryable(self):
        self.document.expiry_date = timezone.localdate() + timedelta(days=30)
        self.document.verification_status = 'VERIFIED'
        self.document.save(update_fields=['expiry_date', 'verification_status'])

        with (
            patch('vendor_catalog.notification_outbox.get_channel_layer', return_value=None),
            self.assertLogs('vendor_catalog.notification_outbox', level='ERROR'),
            self.assertRaises(CommandError),
        ):
            call_command('check_vendor_kyc_expiry', '--apply')

        notification = VendorNotificationOutbox.objects.get()
        self.assertEqual(notification.status, 'PENDING')
        self.assertEqual(notification.attempts, 1)
        self.assertIn('No channel layer', notification.last_error)

        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f'user_{self.vendor.owner_user_id}', channel_name)
        call_command('check_vendor_kyc_expiry', '--apply')

        notification.refresh_from_db()
        self.assertEqual(notification.status, 'PUBLISHED')
        self.assertEqual(notification.attempts, 2)
        self.assertEqual(
            async_to_sync(channel_layer.receive)(channel_name)['notification_id'],
            notification.id,
        )
