"""Deactivate + hide a demo vendor (rows kept for audit).

Example:
    python manage.py remove_demo_vendor --name "Chicken Inn Helensvale"
"""

from django.core.management.base import BaseCommand

from vendors.models import VendorProfile


class Command(BaseCommand):
    help = 'Deactivate and hide a demo vendor (rows kept for audit)'

    def add_arguments(self, parser):
        parser.add_argument('--name', default='Chicken Inn Helensvale')

    def handle(self, *args, **opts):  # noqa: ARG002 - framework signature must stay fixed
        try:
            vendor = VendorProfile.objects.get(business_name=opts['name'])
        except VendorProfile.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"No vendor named {opts['name']!r}"))
            return
        vendor.is_active = False
        vendor.is_open = False
        vendor.lifecycle_state = VendorProfile.LIFECYCLE_ON_HOLD
        vendor.save(update_fields=['is_active', 'is_open', 'lifecycle_state'])
        self.stdout.write(self.style.SUCCESS(f'Hidden vendor: {opts["name"]} (id={vendor.id})'))
