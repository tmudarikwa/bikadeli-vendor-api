import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()

USERS = [
    ('demo-vendor-1@example.invalid', 'demo_vendor_1', 'Demo', 'Vendor One', ''),
    ('demo-vendor-2@example.invalid', 'demo_vendor_2', 'Demo', 'Vendor Two', ''),
    ('demo-vendor-3@example.invalid', 'demo_vendor_3', 'Demo', 'Vendor Three', ''),
]


class Command(BaseCommand):
    help = 'Create three demo vendor user accounts'

    def handle(self, *args, **options):
        password = os.environ.get('DEMO_VENDOR_PASSWORD')
        if not password:
            raise CommandError('Set DEMO_VENDOR_PASSWORD before creating demo vendor users.')

        for email, username, first, last, phone in USERS:
            if not User.objects.filter(email=email).exists():
                User.objects.create_user(
                    email=email, username=username, password=password,
                    first_name=first, last_name=last, phone_number=phone,
                    user_type='VENDOR', is_active=True,
                )
                self.stdout.write(self.style.SUCCESS(f'created {email}'))
            else:
                self.stdout.write(f'exists {email}')
        self.stdout.write(self.style.SUCCESS(
            f'vendor users ready: {User.objects.filter(user_type="VENDOR").count()}'))
