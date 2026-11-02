from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from vendor_catalog.models import (
    VendorKYCExpiryLog,
    VendorKYCVerification,
    VendorNotificationOutbox,
    VendorProfile,
)
from vendor_catalog.notification_outbox import publish_pending_vendor_notifications

ALERTS = {
    30: (
        'ALERT_SENT_30',
        'KYC Document Expiring Soon',
        'NORMAL',
        'Your {document_type} expires on {expiry_date}. Please upload a renewed copy.',
    ),
    7: (
        'ALERT_SENT_7',
        'KYC Document Expiring in 7 Days',
        'HIGH',
        'URGENT: Your {document_type} expires on {expiry_date}. Upload renewed copy immediately to avoid suspension.',
    ),
    1: (
        'ALERT_SENT_1',
        'KYC Document Expiring TOMORROW',
        'CRITICAL',
        'CRITICAL: Your {document_type} expires tomorrow ({expiry_date}). Your vendor account will be suspended if not renewed.',
    ),
}


class Command(BaseCommand):
    help = 'Process required KYC expiries and queue or publish vendor notifications.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply expiry updates and publish pending vendor notifications.',
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        alert_dates = {today + timedelta(days=days): days for days in ALERTS}
        vendor_ids = list(
            VendorProfile.objects.filter(
                Q(
                    kyc_documents__is_required=True,
                    kyc_documents__expiry_date__lt=today,
                    kyc_documents__verification_status__in=[
                        'VERIFIED',
                        'PENDING',
                        'UNDER_REVIEW',
                        'EXPIRED',
                    ],
                )
                | Q(
                    kyc_documents__is_required=True,
                    kyc_documents__vendor__is_active=True,
                    kyc_documents__verification_status='VERIFIED',
                    kyc_documents__expiry_date__in=alert_dates,
                ),
            )
            .order_by('id')
            .values_list('id', flat=True)
            .distinct()
        )
        dry_run = not options['apply']
        expired_documents = 0
        suspended_vendors = 0
        queued_notifications = 0

        for vendor_id in vendor_ids:
            with transaction.atomic():
                vendor = VendorProfile.objects.select_for_update().get(pk=vendor_id)
                expired = list(
                    vendor.kyc_documents.select_for_update().filter(
                        is_required=True,
                        expiry_date__lt=today,
                        verification_status__in=[
                            'VERIFIED',
                            'PENDING',
                            'UNDER_REVIEW',
                            'EXPIRED',
                        ],
                    )
                )
                newly_expired = [doc for doc in expired if doc.verification_status != 'EXPIRED']
                expired_documents += len(newly_expired)
                should_close = vendor.is_open and bool(expired)
                if should_close:
                    suspended_vendors += 1

                due_alerts = [
                    document
                    for document in vendor.kyc_documents.select_for_update().filter(
                        is_required=True,
                        vendor__is_active=True,
                        verification_status='VERIFIED',
                        expiry_date__in=alert_dates,
                    ).order_by('id')
                ]
                if dry_run:
                    for document in due_alerts:
                        days_until_expiry = alert_dates[document.expiry_date]
                        action = ALERTS[days_until_expiry][0]
                        if not self.alert_already_recorded(document, action):
                            queued_notifications += 1
                    continue

                for document in newly_expired:
                    document.verification_status = 'EXPIRED'
                    document.updated_at = timezone.now()
                    document.save(update_fields=['verification_status', 'updated_at'])
                    VendorKYCExpiryLog.objects.create(
                        vendor=vendor,
                        document=document,
                        action='DOCUMENT_EXPIRED',
                        message=f'{document.get_document_type_display()} expired on {document.expiry_date}',
                        metadata={'expiry_date': str(document.expiry_date)},
                    )

                for document in due_alerts:
                    days_until_expiry = alert_dates[document.expiry_date]
                    action, title, priority, body_template = ALERTS[days_until_expiry]
                    if self.alert_already_recorded(document, action):
                        continue
                    defaults = {
                        'vendor': vendor,
                        'document': document,
                        'recipient_user_id': vendor.owner_user_id,
                        'expiry_date': document.expiry_date,
                        'title': title,
                        'body': body_template.format(
                            document_type=document.get_document_type_display(),
                            expiry_date=document.expiry_date,
                        ),
                        'data': {
                            'document_id': document.id,
                            'document_type': document.document_type,
                            'expiry_date': str(document.expiry_date),
                            'days_until_expiry': days_until_expiry,
                        },
                        'priority': priority,
                    }
                    _, created = VendorNotificationOutbox.objects.get_or_create(
                        idempotency_key=self.notification_key(document, action),
                        defaults={'action': action, **defaults},
                    )
                    queued_notifications += int(created)

                verification = VendorKYCVerification.objects.filter(vendor=vendor).first()
                if verification:
                    verification.refresh_status()

                if should_close:
                    vendor.is_open = False
                    vendor.save(update_fields=['is_open', 'updated_at'])
                    VendorKYCExpiryLog.objects.create(
                        vendor=vendor,
                        action='VENDOR_SUSPENDED',
                        message='Vendor closed because one or more required KYC documents expired.',
                        metadata={'expired_document_types': [doc.document_type for doc in expired]},
                    )

        delivery = {'published': 0, 'failed': 0}
        if not dry_run:
            delivery = publish_pending_vendor_notifications()

        mode = 'Dry run' if dry_run else 'Expiry check'
        self.stdout.write(
            f'{mode} complete: expired_documents={expired_documents}, '
            f'vendors_to_close={suspended_vendors}, notifications_queued={queued_notifications}, '
            f'notifications_published={delivery["published"]}, notifications_failed={delivery["failed"]}.'
        )
        if delivery['failed']:
            raise CommandError(f'{delivery["failed"]} vendor notifications could not be published; they remain retryable.')

    @staticmethod
    def notification_key(document, action):
        return f'vendor-kyc-expiry:{document.pk}:{action}:{document.expiry_date.isoformat()}'

    @classmethod
    def alert_already_recorded(cls, document, action):
        return VendorNotificationOutbox.objects.filter(
            idempotency_key=cls.notification_key(document, action)
        ).exists() or VendorKYCExpiryLog.objects.filter(
            document=document,
            action=action,
            metadata__expiry_date=str(document.expiry_date),
        ).exists()
