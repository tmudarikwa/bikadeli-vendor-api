from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0013_product_vendors_pro_vendor__1776bd_idx_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vendorprofile',
            name='category',
            field=models.CharField(
                choices=[
                    ('RESTAURANT', 'Restaurant'),
                    ('FAST_FOOD', 'Fast Food'),
                    ('GROCERY', 'Grocery Store'),
                    ('PHARMACY', 'Pharmacy'),
                    ('LIQUOR', 'Liquor Store'),
                    ('FLOWERS', 'Flowers & Gifts'),
                    ('BAKERY', 'Bakery'),
                    ('OTHER', 'Other'),
                ],
                max_length=20,
            ),
        ),
    ]
