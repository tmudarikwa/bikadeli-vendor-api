import time

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Prefetch
from django.utils import timezone

from vendor_catalog.models import (
    RestaurantMenuItem,
    RestaurantMenuItemOption,
    VendorCatalogSyncOutbox,
    VendorProfile,
    VendorSanctionsScreening,
)


class Command(BaseCommand):
    help = 'Explicitly sync vendor-owned restaurant menus to the orders-service snapshot endpoint.'

    def add_arguments(self, parser):
        parser.add_argument('--vendor-id', type=int, help='Sync one vendor profile ID only.')
        parser.add_argument(
            '--pending-only',
            action='store_true',
            help='Sync vendors with pending catalog outbox events only.',
        )
        parser.add_argument('--retries', type=int, default=3, help='Retries per item after transient errors.')

    def handle(self, *args, **options):
        endpoint = getattr(settings, 'FOOD_CATALOG_SYNC_URL', '').strip()
        token = getattr(settings, 'FOOD_CATALOG_SYNC_TOKEN', '')
        retries = options['retries']
        if not endpoint or not token:
            raise CommandError('Configure FOOD_CATALOG_SYNC_URL and FOOD_CATALOG_SYNC_TOKEN.')
        if len(token) < 32:
            raise CommandError('FOOD_CATALOG_SYNC_TOKEN must contain at least 32 characters.')
        if retries < 0:
            raise CommandError('--retries must be zero or greater.')
        if options['vendor_id'] is not None and options['pending_only']:
            raise CommandError('--vendor-id and --pending-only cannot be used together.')

        if options['vendor_id'] is not None:
            vendor_ids = [options['vendor_id']]
        elif options['pending_only']:
            vendor_ids = list(
                VendorCatalogSyncOutbox.objects.filter(delivered_at__isnull=True)
                .order_by('created_at')
                .values_list('vendor_id', flat=True)
            )
        else:
            vendor_ids = list(VendorProfile.objects.order_by('id').values_list('id', flat=True))
        vendors = {
            vendor.id: vendor
            for vendor in VendorProfile.objects.filter(id__in=vendor_ids)
        }

        synced = 0
        failed = []
        with httpx.Client(timeout=10.0) as client:
            for vendor_id in vendor_ids:
                vendor = vendors.get(vendor_id)
                outbox = VendorCatalogSyncOutbox.objects.filter(vendor_id=vendor_id).first()
                outbox_version = outbox.updated_at if outbox and outbox.delivered_at is None else None
                if vendor is None:
                    message = f'vendor {vendor_id}: vendor profile not found'
                    failed.append(message)
                    self._record_failure(outbox, outbox_version, message)
                    continue

                queryset = RestaurantMenuItem.objects.filter(vendor_id=vendor_id).prefetch_related(
                    Prefetch(
                        'options',
                        queryset=RestaurantMenuItemOption.objects.order_by('id'),
                    )
                )
                compliance_clear = not VendorSanctionsScreening.objects.filter(
                    vendor_id=vendor_id,
                    status='CONFIRMED_MATCH',
                ).exists()
                vendor_failures = []
                for item in queryset.iterator(chunk_size=500):
                    payload = self._payload(item, vendor, compliance_clear=compliance_clear)
                    error = self._post_with_retries(client, endpoint, token, payload, retries)
                    if error:
                        message = f'{item.id}: {error}'
                        failed.append(message)
                        vendor_failures.append(message)
                    else:
                        synced += 1

                if outbox_version is not None:
                    if vendor_failures:
                        self._record_failure(outbox, outbox_version, '; '.join(vendor_failures))
                    else:
                        VendorCatalogSyncOutbox.objects.filter(
                            pk=outbox.pk,
                            updated_at=outbox_version,
                            delivered_at__isnull=True,
                        ).update(delivered_at=timezone.now(), attempts=0, last_error='')

        self.stdout.write(f'Food catalog sync finished: synced={synced}, failed={len(failed)}.')
        for failure in failed:
            self.stderr.write(failure)
        if failed:
            raise CommandError('Some snapshots did not sync; rerun the command to retry failed and successful IDs.')

    @staticmethod
    def _record_failure(outbox, outbox_version, message):
        if outbox is None or outbox_version is None:
            return
        VendorCatalogSyncOutbox.objects.filter(
            pk=outbox.pk,
            updated_at=outbox_version,
            delivered_at__isnull=True,
        ).update(
            attempts=F('attempts') + 1,
            last_error=message[:300],
            updated_at=timezone.now(),
        )

    @staticmethod
    def _payload(item, vendor, compliance_clear=None):
        if compliance_clear is None:
            compliance_clear = not vendor.sanctions_screenings.filter(status='CONFIRMED_MATCH').exists()
        return {
            'id': item.id,
            'vendor_id': item.vendor_id,
            'vendor_user_id': vendor.owner_user_id,
            'vendor_name': vendor.business_name,
            'vendor_is_open': vendor.is_open,
            'vendor_is_active': vendor.is_active and compliance_clear,
            'vendor_address': vendor.business_address,
            'minimum_order': str(vendor.minimum_order),
            'name': item.name,
            'description': item.description,
            'price': str(item.price),
            'is_available': item.is_available,
            'options': [
                {
                    'id': option.id,
                    'name': option.name,
                    'price_adjustment': str(option.price_adjustment),
                }
                for option in item.options.all()
            ],
        }

    @staticmethod
    def _post_with_retries(client, endpoint, token, payload, retries):
        for attempt in range(retries + 1):
            try:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers={'X-Catalog-Sync-Token': token},
                )
            except httpx.HTTPError as exc:
                if attempt == retries:
                    return f'{type(exc).__name__}: {exc}'
                time.sleep(min(2**attempt, 8))
                continue

            if response.is_success:
                return None
            if response.status_code < 500 and response.status_code != 429:
                return f'HTTP {response.status_code}: {response.text[:200]}'
            if attempt == retries:
                return f'HTTP {response.status_code}: {response.text[:200]}'
            time.sleep(min(2**attempt, 8))
        return 'retry limit exhausted'
