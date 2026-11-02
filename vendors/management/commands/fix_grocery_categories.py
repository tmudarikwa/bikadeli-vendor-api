from django.core.management.base import BaseCommand

from vendors.models import Category, Product, VendorProfile

# FreshMart's real in-store categories. Products are matched by name and moved
# into their category; the legacy flat 'Groceries' category is removed only
# when it no longer holds any products.
CATEGORY_MAP = {
    'Bakery': ['Bread'],
    'Dairy': ['Milk 1L', 'Eggs (6)'],
    'Fruits & Vegetables': ['Apples 1kg'],
    'Pantry': ['Rice 2kg', 'Cooking Oil 750ml'],
}


class Command(BaseCommand):
    help = 'Split FreshMart products into per-store categories'

    def handle(self, *args, **options):  # noqa: ARG002 - framework signature must stay fixed
        try:
            profile = VendorProfile.objects.get(business_name='FreshMart Groceries')
        except VendorProfile.DoesNotExist:
            self.stdout.write(self.style.ERROR('FreshMart Groceries not found'))
            return

        for order, (cat_name, product_names) in enumerate(CATEGORY_MAP.items(), start=1):
            cat, _ = Category.objects.get_or_create(
                name=cat_name,
                vendor=profile,
                defaults={'sort_order': order},
            )
            moved = (
                Product.objects.filter(
                    vendor=profile,
                    name__in=product_names,
                )
                .exclude(category=cat)
                .update(category=cat)
            )
            count = Product.objects.filter(vendor=profile, category=cat).count()
            self.stdout.write(f'  ✓ {cat_name}: {count} products ({moved} moved)')

        # Drop the legacy flat category once empty.
        legacy = Category.objects.filter(
            vendor=profile,
            name='Groceries',
        ).exclude(id__in=Product.objects.filter(vendor=profile).values('category_id'))
        deleted, _ = legacy.delete()
        self.stdout.write(f'  legacy empty categories removed: {deleted}')

        self.stdout.write(self.style.SUCCESS('✓ DONE'))
