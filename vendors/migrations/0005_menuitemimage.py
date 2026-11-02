# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0004_product_is_discounted_product_rating'),
    ]

    operations = [
        migrations.CreateModel(
            name='MenuItemImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='menu_items/%Y/%m/')),
                ('alt_text', models.CharField(blank=True, max_length=255)),
                ('sort_order', models.IntegerField(default=0)),
                (
                    'menu_item',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name='images', to='vendors.menuitem'
                    ),
                ),
            ],
            options={
                'ordering': ['sort_order'],
            },
        ),
    ]
