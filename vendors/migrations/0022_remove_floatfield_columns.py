from django.db import migrations


class Migration(migrations.Migration):
    """Contract phase: remove old FloatField lat/lng columns."""

    dependencies = [
        ('vendors', '0021_vendorprofile_business_location'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='vendorprofile',
            name='business_location_lat',
        ),
        migrations.RemoveField(
            model_name='vendorprofile',
            name='business_location_lng',
        ),
    ]
