"""
Seed shared marketplace categories that always exist.

These are the canonical categories for the marketplace view and vendor
registration.  They live with ``vendor=None`` so they are global, not
per-vendor.  The seed management command is NOT required for these to
appear — they are created by this migration.

Constants imported from vendors.category_constants (single source of truth).
"""

from django.db import migrations

from vendors.category_constants import MARKETPLACE_CATEGORIES


def forwards(apps, schema_editor):
    Category = apps.get_model('vendors', 'Category')

    sort_idx = 0
    for name, meta in MARKETPLACE_CATEGORIES.items():
        top_cat, _ = Category.objects.get_or_create(
            name=name,
            vendor=None,
            parent=None,
            defaults={'sort_order': sort_idx, 'icon': meta['icon']},
        )
        sort_idx += 1

        for child_name in meta['subcategories']:
            Category.objects.get_or_create(
                name=child_name,
                vendor=None,
                parent=top_cat,
                defaults={'sort_order': sort_idx, 'icon': 'category'},
            )
            sort_idx += 1


def backwards(apps, schema_editor):
    """No-op — do NOT delete shared categories on rollback.

    Rolling back this migration should only undo the *schema* change
    (there is none here).  Deleting categories would orphan every
    Product.category FK (SET_NULL) and destroy admin-curated taxonomy.
    """
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0027_alter_category_options_alter_menuitemoption_options_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
