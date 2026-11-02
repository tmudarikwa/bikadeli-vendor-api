from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0025_resize_encrypted_fields'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='vendorkycdocument',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='vendorkycdocument',
            constraint=models.UniqueConstraint(
                fields=('vendor', 'document_type'),
                name='uq_kyc_doc_vendor_type',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='category',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(
                fields=('name', 'vendor', 'parent'),
                name='uq_category_name_vendor_parent',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='payoutreconciliation',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='payoutreconciliation',
            constraint=models.UniqueConstraint(
                fields=('vendor', 'period_start', 'period_end'),
                name='uq_reconciliation_vendor_period',
            ),
        ),
    ]
