"""
Backfill orphan Products and enforce category NOT NULL.

- For EVERY product (not just Fruit) with category=NULL, assigns the correct
  marketplace category based on product_type (covers legacy seed bug where
  Apple had no category while Orange had "Fruit", but also any other
  ELECTRONICS/FASHION/HOME/PARCEL/etc. orphans).
- Falls back to the global "Other" category when no type-specific match exists.
- Then alters Product.category to PROTECT + NOT NULL (was SET_NULL, nullable).

The forwards data-fix is idempotent; backwards re-allows NULL/SET_NULL.
"""

from django.db import migrations, models
import django.db.models.deletion


# Maps Product.product_type -> global Category.name (mirrors ptype_cat_map in seed.py
# and marketplace_views taxonomy). Covers ALL categories, not just Groceries.
PRODUCT_TYPE_TO_CATEGORY = {
    'ELECTRONICS': 'Electronics',
    'FASHION': 'Fashion',
    'HOME': 'Home & Garden',
    'GROCERY': 'Groceries',
    'FOOD': 'Food',
    'PARCEL': 'Parcel',
    'OTHER': 'Other',
}

# For grocery orphans (e.g. Apple) prefer the fine-grained produce subcategory.
GROCERY_SUBCATEGORY = 'Fruits & Vegetables'


def _resolve_category(Category, product_type):
    # Prefer produce subcategory for grocery (Apple/Orange case)
    if product_type == 'GROCERY':
        cat = Category.objects.filter(name=GROCERY_SUBCATEGORY, vendor=None).first()
        if cat:
            return cat
    # Type-specific top-level category (covers ALL categories)
    cat_name = PRODUCT_TYPE_TO_CATEGORY.get(product_type) or PRODUCT_TYPE_TO_CATEGORY.get('OTHER')
    cat = Category.objects.filter(name=cat_name, vendor=None).first()
    if cat:
        return cat
    # Generic fallbacks — order matters, covers every taxonomy branch
    for fallback in ['Other', 'Groceries', 'Food', 'Electronics', 'Fashion', 'Home & Garden', 'Parcel']:
        cat = Category.objects.filter(name=fallback, vendor=None).first()
        if cat:
            return cat
    cat = Category.objects.filter(vendor=None, parent=None).first()
    if cat:
        return cat
    # Fresh test DB before 0028 — create neutral fallback.
    return Category.objects.create(name='Other', vendor=None, parent=None, sort_order=999, icon='category')


def forwards_backfill(apps, schema_editor):
    Product = apps.get_model('vendors', 'Product')
    Category = apps.get_model('vendors', 'Category')

    orphans = list(Product.objects.filter(category__isnull=True).values('id', 'product_type'))
    if not orphans:
        return

    # Group orphans by product_type so we resolve each distinct type once.
    from collections import defaultdict

    by_type = defaultdict(list)
    for row in orphans:
        by_type[row['product_type'] or 'OTHER'].append(row['id'])

    for ptype, ids in by_type.items():
        target = _resolve_category(Category, ptype)
        Product.objects.filter(id__in=ids).update(category=target)


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0029_add_vendor_active_verified_index'),
    ]

    operations = [
        migrations.RunPython(forwards_backfill, backwards_noop),
        migrations.AlterField(
            model_name='product',
            name='category',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='vendors.category',
            ),
        ),
    ]
