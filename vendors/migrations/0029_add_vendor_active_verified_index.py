from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0028_seed_marketplace_categories'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='vendorprofile',
            index=models.Index(
                fields=['is_active', 'is_verified'],
                name='idx_vendor_active_verified',
            ),
        ),
    ]
