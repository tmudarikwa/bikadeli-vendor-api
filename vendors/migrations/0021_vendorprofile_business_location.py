from django.contrib.gis.db.models import PointField, Index as GisIndex
from django.db import migrations


class Migration(migrations.Migration):
    """Add PostGIS PointField for vendor location."""

    dependencies = [
        ('core', '0017_enable_postgis_add_point_columns'),
        ('vendors', '0020_increase_encrypted_field_lengths'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendorprofile',
            name='business_location',
            field=PointField(srid=4326, null=True, blank=True),
        ),
        migrations.AddIndex(
            model_name='vendorprofile',
            index=GisIndex(fields=['business_location'], name='idx_vendor_business_location'),
        ),
    ]
