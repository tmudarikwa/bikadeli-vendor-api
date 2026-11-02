from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0014_vendorprofile_errands_category'),
        ('vendors', '0016_remove_bakery_category'),
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
