# Adds Product.compare_at_price (original struck-through price for sales).

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0032_scorecard_metrics'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='compare_at_price',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Original price shown struck-through when on sale. Must exceed price.',
                max_digits=10,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal('0.01'))],
            ),
        ),
    ]
