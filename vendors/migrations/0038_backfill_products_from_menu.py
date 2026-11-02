"""Backfill Product rows for existing MenuItems (menu → catalog sync).

Mirrors vendors.signals.sync_product_for_menu_item using historical models
(must not import live model code). Safe to re-run: get_or_create on the
menu_item link.
"""

from django.db import migrations


def _resolve_category(Category, menu_category_name):
    if menu_category_name:
        match = Category.objects.filter(
            name__iexact=menu_category_name.strip(),
            vendor__isnull=True,
            parent__isnull=True,
        ).first()
        if match is not None:
            return match
    fallback, _ = Category.objects.get_or_create(
        name='Other',
        vendor=None,
        parent=None,
        defaults={'icon': 'shopping_bag'},
    )
    return fallback


def backfill_products(apps, schema_editor):
    MenuItem = apps.get_model('vendors', 'MenuItem')
    Product = apps.get_model('vendors', 'Product')
    Category = apps.get_model('vendors', 'Category')

    # Prefetch menu categories to avoid N+1 name lookups.
    items = MenuItem.objects.select_related('category', 'vendor').all()
    created = 0
    for item in items:
        category_name = item.category.name if item.category else ''
        category = _resolve_category(Category, category_name)
        product, was_created = Product.objects.get_or_create(
            menu_item=item,
            defaults={
                'vendor': item.vendor,
                'name': item.name,
                'description': item.description,
                'category': category,
                'price': item.price,
                'is_available': item.is_available,
            },
        )
        if was_created:
            created += 1
        else:
            Product.objects.filter(pk=product.pk).update(
                name=item.name,
                description=item.description,
                category=category,
                price=item.price,
                is_available=item.is_available,
            )
    if created:
        print(f'Backfilled {created} catalog products from menu items')


def noop_reverse(apps, schema_editor):
    # Keep backfilled rows on reverse (catalog data is additive).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('vendors', '0037_product_menu_item'),
    ]

    operations = [
        migrations.RunPython(backfill_products, noop_reverse),
    ]
