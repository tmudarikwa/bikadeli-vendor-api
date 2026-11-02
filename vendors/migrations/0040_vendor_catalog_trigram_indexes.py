from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0035_enable_pg_trgm'),
        ('vendors', '0039_product_is_popular_categorybestseller'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='vendorprofile',
            index=GinIndex(fields=['business_name'], opclasses=['gin_trgm_ops'], name='idx_vendor_name_trgm'),
        ),
        AddIndexConcurrently(
            model_name='menuitem',
            index=GinIndex(fields=['name'], opclasses=['gin_trgm_ops'], name='idx_menu_name_trgm'),
        ),
        AddIndexConcurrently(
            model_name='menuitem',
            index=GinIndex(fields=['description'], opclasses=['gin_trgm_ops'], name='idx_menu_desc_trgm'),
        ),
        AddIndexConcurrently(
            model_name='product',
            index=GinIndex(fields=['name'], opclasses=['gin_trgm_ops'], name='idx_product_name_trgm'),
        ),
        AddIndexConcurrently(
            model_name='product',
            index=GinIndex(fields=['description'], opclasses=['gin_trgm_ops'], name='idx_product_desc_trgm'),
        ),
        AddIndexConcurrently(
            model_name='product',
            index=GinIndex(fields=['brand'], opclasses=['gin_trgm_ops'], name='idx_product_brand_trgm'),
        ),
        AddIndexConcurrently(
            model_name='product',
            index=GinIndex(fields=['barcode'], opclasses=['gin_trgm_ops'], name='idx_product_barcode_trgm'),
        ),
    ]
