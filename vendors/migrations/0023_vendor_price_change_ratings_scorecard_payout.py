import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('vendors', '0022_remove_floatfield_columns'),
        ('orders', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Promotion: add approval workflow fields ──────────────────────
        migrations.AddField(
            model_name='promotion',
            name='status',
            field=models.CharField(
                choices=[
                    ('DRAFT', 'Draft'),
                    ('PENDING_APPROVAL', 'Pending Approval'),
                    ('ACTIVE', 'Active'),
                    ('REJECTED', 'Rejected'),
                    ('EXPIRED', 'Expired'),
                ],
                db_index=True,
                default='DRAFT',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='promotion',
            name='description',
            field=models.TextField(blank=True, help_text='Promotion description for admin review'),
        ),
        migrations.AddField(
            model_name='promotion',
            name='rejection_reason',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='promotion',
            name='approved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='approved_promotions',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='promotion',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='promotion',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        # ── VendorPriceChange ───────────────────────────────────────────
        migrations.CreateModel(
            name='VendorPriceChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                (
                    'content_type',
                    models.CharField(
                        choices=[
                            ('MENU_ITEM', 'Menu Item'),
                            ('PRODUCT', 'Product'),
                            ('PRODUCT_VARIANT', 'Product Variant'),
                        ],
                        max_length=20,
                    ),
                ),
                ('object_id', models.PositiveIntegerField()),
                ('field_name', models.CharField(default='price', max_length=50)),
                ('old_price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('new_price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('change_pct', models.DecimalField(decimal_places=2, help_text='Percentage change', max_digits=7)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('reason', models.CharField(blank=True, max_length=500)),
                (
                    'requires_approval',
                    models.BooleanField(default=False, help_text='True if change >50% in 24h window'),
                ),
                (
                    'is_approved',
                    models.BooleanField(
                        default=None, help_text='None=pending, True=approved, False=rejected', null=True
                    ),
                ),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'changed_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'approved_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='approved_price_changes',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'vendor',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='price_changes',
                        to='vendors.vendorprofile',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='vendorpricechange',
            index=models.Index(
                fields=['vendor', 'content_type', 'object_id'], name='vendors_vend_vendor__idx_price_change_obj'
            ),
        ),
        migrations.AddIndex(
            model_name='vendorpricechange',
            index=models.Index(fields=['requires_approval', 'is_approved'], name='vendors_vend_approval_idx'),
        ),
        migrations.AddIndex(
            model_name='vendorpricechange',
            index=models.Index(fields=['created_at'], name='vendors_vend_created_at_price_change_idx'),
        ),
        # ── VendorRatingSnapshot ────────────────────────────────────────
        migrations.CreateModel(
            name='VendorRatingSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rating', models.DecimalField(decimal_places=2, max_digits=3)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'vendor',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='rating_snapshots',
                        to='vendors.vendorprofile',
                    ),
                ),
                (
                    'order',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='orders.order',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='vendorratingsnapshot',
            index=models.Index(fields=['vendor', 'created_at'], name='vendors_vend_rating_snap_idx'),
        ),
        # ── VendorScorecard ─────────────────────────────────────────────
        migrations.CreateModel(
            name='VendorScorecard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                (
                    'on_time_prep_rate',
                    models.DecimalField(
                        decimal_places=2, default=0, help_text='% of orders prepared on time', max_digits=5
                    ),
                ),
                (
                    'cancellation_rate',
                    models.DecimalField(
                        decimal_places=2, default=0, help_text='% of orders cancelled by vendor', max_digits=5
                    ),
                ),
                ('avg_rating_30d', models.DecimalField(decimal_places=2, default=0, max_digits=3)),
                ('avg_rating_90d', models.DecimalField(decimal_places=2, default=0, max_digits=3)),
                ('total_orders_30d', models.IntegerField(default=0)),
                ('total_orders_90d', models.IntegerField(default=0)),
                ('avg_prep_time_mins', models.DecimalField(decimal_places=1, default=0, max_digits=5)),
                (
                    'repeat_customer_rate',
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        help_text='% of customers who ordered again within 30d',
                        max_digits=5,
                    ),
                ),
                ('revenue_30d', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('commission_owed_30d', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('computed_at', models.DateTimeField(auto_now=True)),
                (
                    'vendor',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='scorecard',
                        to='vendors.vendorprofile',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Vendor Scorecard',
                'verbose_name_plural': 'Vendor Scorecards',
            },
        ),
        # ── PayoutReconciliation ────────────────────────────────────────
        migrations.CreateModel(
            name='PayoutReconciliation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period_start', models.DateField()),
                ('period_end', models.DateField()),
                ('total_orders_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('total_commission', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                (
                    'expected_payout',
                    models.DecimalField(
                        decimal_places=2, default=0, help_text='total_orders_amount - total_commission', max_digits=10
                    ),
                ),
                (
                    'actual_payout',
                    models.DecimalField(
                        decimal_places=2, default=0, help_text='Amount from VendorPayout record', max_digits=10
                    ),
                ),
                (
                    'variance',
                    models.DecimalField(decimal_places=2, default=0, help_text='expected - actual', max_digits=10),
                ),
                ('order_count', models.IntegerField(default=0)),
                ('is_resolved', models.BooleanField(default=False)),
                ('resolution_note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'vendor',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='payout_reconciliations',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'vendor_profile',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='reconciliations',
                        to='vendors.vendorprofile',
                    ),
                ),
                (
                    'resolved_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['-period_start'],
            },
        ),
        migrations.AlterUniqueTogether(
            name='payoutreconciliation',
            unique_together={('vendor', 'period_start', 'period_end')},
        ),
        migrations.AddIndex(
            model_name='payoutreconciliation',
            index=models.Index(fields=['vendor', 'period_start', 'period_end'], name='vendors_payout_period_idx'),
        ),
        migrations.AddIndex(
            model_name='payoutreconciliation',
            index=models.Index(fields=['is_resolved'], name='vendors_payout_resolved_idx'),
        ),
    ]
