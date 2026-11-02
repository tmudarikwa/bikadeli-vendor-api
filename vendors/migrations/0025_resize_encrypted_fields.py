from django.db import migrations


def _resize(table, column, size):
    return (
        f'DO $$\nBEGIN\n'
        f'  IF EXISTS (\n'
        f'    SELECT 1 FROM information_schema.columns\n'
        f"    WHERE table_name='{table}' AND column_name='{column}'\n"
        f'  ) THEN\n'
        f'    ALTER TABLE {table} ALTER COLUMN {column} TYPE varchar({size});\n'
        f'  ELSE\n'
        f'    ALTER TABLE {table} ADD COLUMN {column} varchar({size});\n'
        f'  END IF;\n'
        f'END $$;'
    )


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0024_add_kyc_compliance_models'),
    ]

    operations = [
        migrations.RunSQL(
            _resize('vendors_vendorprofile', 'business_phone', 120)
            + _resize('vendors_vendorprofile', 'eco_cash_number', 365)
            + _resize('vendors_vendorprofile', 'bank_name', 365)
            + _resize('vendors_vendorprofile', 'bank_account', 365)
            + _resize('vendors_vendorprofile', 'zimra_pin', 175)
            + _resize('vendors_vendorprofile', 'vat_number', 175)
        ),
    ]
