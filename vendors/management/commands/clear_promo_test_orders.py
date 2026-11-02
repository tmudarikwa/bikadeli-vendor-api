from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from orders.models import Order


class Command(BaseCommand):
    help = 'Clear active orders for promo loop test customer'

    def handle(self, *args, **options):  # noqa: ARG002 - framework signature must stay fixed
        user_model = get_user_model()
        try:
            customer = user_model.objects.get(email='promo.looptest@example.com')
            active = Order.objects.filter(
                customer=customer,
                status__in=['PENDING', 'ACCEPTED', 'PREPARING', 'READY', 'DRIVER_ASSIGNED', 'PICKED_UP', 'DELIVERING'],
            )
            count = active.count()
            active.delete()
            self.stdout.write(self.style.SUCCESS(f'Cleared {count} active orders for promo.looptest'))
        except user_model.DoesNotExist:
            self.stdout.write(self.style.WARNING('promo.looptest@example.com not found'))
