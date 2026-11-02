from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vendors', '0035_product_public_browse_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendorprofile',
            name='packing_fee',
            field=models.DecimalField(decimal_places=2, default=0, help_text='Packing fee per order', max_digits=6),
        ),
    ]
