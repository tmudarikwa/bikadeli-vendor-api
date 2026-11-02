from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('vendors', '0034_vendorsupportticket_vendorinternalnote_and_more'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='product',
            index=models.Index(fields=['is_available', '-created_at'], name='idx_prod_available_created'),
        ),
    ]
