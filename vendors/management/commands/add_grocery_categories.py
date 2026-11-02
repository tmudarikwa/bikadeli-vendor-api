from django.core.management.base import BaseCommand

from vendors.models import Category, Product, VendorProfile


class Command(BaseCommand):
    help = 'Split FreshMart grocery products into per-store categories'

    def handle(self, *args, **options):  # noqa: ARG002 - framework signature must stay fixed
        v = VendorProfile.objects.get(business_name='FreshMart Groceries')
        items = [
            ('Bakery', [3301]),
            ('Dairy', [3302, 3303]),
            ('Fruits & Vegetables', [3304]),
            ('Pantry', [3305, 3306]),
        ]
        for all_done, (cat_name, prod_ids) in enumerate(items):
            cat, created = Category.objects.get_or_create(
                name=cat_name, vendor=v, defaults={'sort_order': all_done + 1}
            )
            moved = Product.objects.filter(vendor=v, pk__in=prod_ids, category__isnull=True).update(category=cat)
            count = Product.objects.filter(vendor=v, category=cat).count()
            self.stdout.write(f'  {cat_name}: created={created}, moved={moved}, total={count}')
        self.stdout.write(
            self.style.SUCCESS(
                f'FRESHMART_CATEGORIES: {Category.objects.filter(vendor=v).count()} '
                f'products: {Product.objects.filter(vendor=v).count()}'
            )
        )
