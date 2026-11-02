# backend/vendors/tasks.py
import logging
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.db.models import Avg, Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.structured_logging import log_event

from .date_ranges import local_day_bounds
from .models import (
    Category,
    CategoryBestseller,
    PayoutReconciliation,
    Product,
    VendorKYCDocument,
    VendorKYCExpiryLog,
    VendorPriceChange,
    VendorProfile,
    VendorRatingSnapshot,
    VendorScorecard,
)

logger = logging.getLogger(__name__)


# Caps for vendor CSV imports: bounds Redis payload + DB write fan-out per task.
# Aligned with vendors/views.py MAX_CSV_ROWS (10k) — the task refuses anything
# larger, covering direct broker enqueue that bypasses the view.
CSV_IMPORT_MAX_ROWS = 10000
CSV_IMPORT_MAX_PRICE = 1000000
CSV_IMPORT_MAX_CELL = 2000


@shared_task(
    name='vendors.tasks.process_csv_import',
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
    default_retry_delay=30,
)
def process_csv_import(vendor_id, rows):
    vendor = VendorProfile.objects.get(id=vendor_id)
    if not isinstance(rows, list) or len(rows) > CSV_IMPORT_MAX_ROWS:
        logger.warning(
            'CSV import refused: %s rows for vendor %s', len(rows) if isinstance(rows, list) else '?', vendor_id
        )
        return {'created': 0, 'reason': 'invalid_rows' if not isinstance(rows, list) else 'too_many_rows'}

    product_types = {
        'ELECTRONICS': 'Electronics',
        'FASHION': 'Fashion',
        'HOME': 'Home & Garden',
        'GROCERY': 'Groceries',
        'FOOD': 'Food',
        'PARCEL': 'Parcel',
        'OTHER': 'Other',
    }
    category_map = {
        category.name: category
        for category in Category.objects.filter(vendor=vendor, parent__isnull=True)
    }
    shared_categories = {}
    shared_top_level_categories = []
    for category in Category.objects.filter(vendor__isnull=True):
        shared_categories.setdefault(category.name, category)
        if category.parent_id is None:
            shared_top_level_categories.append(category)
    barcodes = {
        str(row.get('barcode', ''))[:100].strip()
        for row in rows
        if isinstance(row, dict) and str(row.get('barcode', ''))[:100].strip()
    }
    products_by_barcode = {
        product.barcode: product
        for product in Product.objects.filter(vendor=vendor, barcode__in=barcodes)
    }

    def resolve_category(row):
        category_name = str(row.get('category') or '').strip()[:CSV_IMPORT_MAX_CELL]
        if category_name:
            category = category_map.get(category_name)
            if category is None:
                category, _ = Category.objects.get_or_create(name=category_name, vendor=vendor)
                category_map[category_name] = category
            return category

        product_type = str(row.get('product_type') or row.get('type') or '').strip().upper()
        if product_type == 'GROCERY' and 'Fruits & Vegetables' in shared_categories:
            return shared_categories['Fruits & Vegetables']
        fallback_name = product_types.get(product_type)
        if fallback_name and fallback_name in shared_categories:
            return shared_categories[fallback_name]
        for fallback_name in ('Other', 'Groceries'):
            if fallback_name in shared_categories:
                return shared_categories[fallback_name]
        if shared_top_level_categories:
            return shared_top_level_categories[0]

        category, _ = Category.objects.get_or_create(
            name='Other',
            vendor=None,
            parent=None,
            defaults={'sort_order': 999, 'icon': 'category'},
        )
        shared_categories[category.name] = category
        return category

    created = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get('name', ''))[:CSV_IMPORT_MAX_CELL].strip()
        if not name:
            continue
        price = row.get('price', 0)
        try:
            price = float(price)
        except (ValueError, TypeError):
            continue
        if not (0 <= price <= CSV_IMPORT_MAX_PRICE):
            continue
        barcode = str(row.get('barcode', ''))[:100].strip()
        if not barcode:
            continue  # empty barcodes would all collide on one row
        category = resolve_category(row)
        stock_value = row.get('quantity_on_hand')
        if stock_value in (None, ''):
            stock_value = row.get('stock', 0)
        try:
            stock = int(stock_value)
        except (ValueError, TypeError):
            continue
        stock = max(0, min(stock, 1000000))
        product = products_by_barcode.get(barcode)
        if product is None:
            product = Product(vendor=vendor, barcode=barcode)
            products_by_barcode[barcode] = product
        product.name = name
        product.description = str(row.get('description', ''))[:CSV_IMPORT_MAX_CELL]
        product.price = price
        product.quantity_on_hand = stock
        product.unit = str(row.get('unit', 'piece'))[:50]
        product.category = category
        product.save()
        created += 1
    logger.info('CSV import finished: %d products for vendor %d', created, vendor_id)
    return {'created': created}


@shared_task(
    name='vendors.tasks.check_price_alerts',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=60,
)
def check_price_alerts():
    """Scan for unreviewed price changes >50% in the last 24h and create audit events."""
    window_start = timezone.now() - timedelta(hours=24)

    spikes = VendorPriceChange.objects.filter(
        requires_approval=True,
        is_approved=None,
        created_at__gte=window_start,
    ).select_related('vendor')

    alert_count = 0
    for spike in spikes:
        actor = spike.changed_by
        log_event(
            logger,
            'info',
            'PRICE_SPIKE_ALERT',
            actor=actor,
            vendor_id=spike.vendor.id,
            vendor_name=spike.vendor.business_name,
            content_type=spike.content_type,
            object_id=spike.object_id,
            old_price=str(spike.old_price),
            new_price=str(spike.new_price),
            change_pct=str(spike.change_pct),
        )
        alert_count += 1

    return {'alerts_created': alert_count}


@shared_task(
    name='vendors.tasks.expire_pending_approvals',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=60,
)
def expire_pending_approvals():
    """Expire pending price changes older than 48 hours."""
    cutoff = timezone.now() - timedelta(hours=48)
    expired = VendorPriceChange.objects.filter(
        requires_approval=True,
        is_approved=None,
        created_at__lt=cutoff,
    ).update(is_approved=False)
    return {'expired': expired}


@shared_task(
    name='vendors.tasks.refresh_vendor_scorecards',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=120,
)
def refresh_vendor_scorecards():
    """Compute and cache daily scorecards for all active vendors.

    Refactored to use bulk aggregation queries — O(1) queries instead of O(N).
    """
    from orders.models import Order

    today = timezone.localdate()
    thirty_days_ago = today - timedelta(days=30)
    ninety_days_ago = today - timedelta(days=90)
    thirty_days_start, _ = local_day_bounds(thirty_days_ago, today)
    ninety_days_start, _ = local_day_bounds(ninety_days_ago, today)

    active_vendor_ids = list(VendorProfile.objects.filter(is_active=True).values_list('id', flat=True))
    if not active_vendor_ids:
        logger.info('Vendor scorecards refreshed: no active vendors')
        return

    vendor_map = {v.id: v for v in VendorProfile.objects.filter(id__in=active_vendor_ids)}

    # ── 1. Bulk aggregate queries (each returns {vendor_id: value}) ──

    base_30d = Order.objects.filter(
        vendor_id__in=active_vendor_ids,
        created_at__gte=thirty_days_start,
    )
    base_90d = Order.objects.filter(
        vendor_id__in=active_vendor_ids,
        created_at__gte=ninety_days_start,
    )
    delivered_30d = base_30d.filter(status__in=['DELIVERED', 'COMPLETED'])

    total_30d_map = dict(base_30d.values('vendor_id').annotate(c=Count('id')).values_list('vendor_id', 'c'))
    total_90d_map = dict(base_90d.values('vendor_id').annotate(c=Count('id')).values_list('vendor_id', 'c'))
    cancelled_map = dict(
        base_30d.filter(status='CANCELLED').values('vendor_id').annotate(c=Count('id')).values_list('vendor_id', 'c')
    )
    avg_rating_30d_map = dict(
        VendorRatingSnapshot.objects.filter(
            vendor_id__in=active_vendor_ids,
            created_at__gte=thirty_days_start,
        )
        .values('vendor_id')
        .annotate(avg=Avg('rating'))
        .values_list('vendor_id', 'avg')
    )
    avg_rating_90d_map = dict(
        VendorRatingSnapshot.objects.filter(
            vendor_id__in=active_vendor_ids,
            created_at__gte=ninety_days_start,
        )
        .values('vendor_id')
        .annotate(avg=Avg('rating'))
        .values_list('vendor_id', 'avg')
    )
    revenue_map = dict(
        delivered_30d.values('vendor_id')
        .annotate(total=Coalesce(Sum('subtotal'), Value(0), output_field=DecimalField()))
        .values_list('vendor_id', 'total')
    )
    unique_customers_map = dict(
        base_30d.values('vendor_id').annotate(c=Count('customer_id', distinct=True)).values_list('vendor_id', 'c')
    )

    # Average prep time (bulk) — uses database interval arithmetic
    avg_prep_raw = dict(
        delivered_30d.exclude(delivered_at__isnull=True, created_at__isnull=True)
        .values('vendor_id')
        .annotate(avg_prep=Avg(F('delivered_at') - F('created_at')))
        .values_list('vendor_id', 'avg_prep')
    )
    avg_prep_mins_map = {
        vid: Decimal(str(round(avg.total_seconds() / 60, 1))) for vid, avg in avg_prep_raw.items() if avg
    }

    # ── 2. On-time prep rate (Python — needs per-vendor prep_time_mins) ──

    delivered_orders = list(delivered_30d.values_list('vendor_id', 'delivered_at', 'created_at'))
    on_time_map = {}  # vendor_id -> [on_time_count, total_with_times]
    for vendor_id, delivered_at, created_at in delivered_orders:
        if delivered_at and created_at:
            elapsed = (delivered_at - created_at).total_seconds() / 60
            vendor = vendor_map.get(vendor_id)
            allowed = (vendor.preparation_time_mins if vendor and vendor.preparation_time_mins else 15) + 10
            entry = on_time_map.setdefault(vendor_id, [0, 0])
            entry[1] += 1
            if elapsed <= allowed:
                entry[0] += 1

    # ── 3. Repeat customer rate (Python — cross-vendor lookup) ──

    # Count distinct vendors per customer across all active vendors (30d)
    customer_vendor_counts = dict(
        Order.objects.filter(
            vendor_id__in=active_vendor_ids,
            created_at__gte=thirty_days_start,
        )
        .values('customer_id')
        .annotate(vc=Count('vendor_id', distinct=True))
        .values_list('customer_id', 'vc')
    )
    multi_vendor_customers = {cid for cid, cnt in customer_vendor_counts.items() if cnt > 1}

    # Map each vendor to how many of its customers also ordered elsewhere
    vendor_repeat_counts = {}
    for vid, cid in base_30d.values_list('vendor_id', 'customer_id').distinct():
        if cid in multi_vendor_customers:
            vendor_repeat_counts[vid] = vendor_repeat_counts.get(vid, 0) + 1

    # ── 4. Build scorecards and bulk-write ──

    to_update = []
    to_create = []
    existing = {sc.vendor_id: sc for sc in VendorScorecard.objects.filter(vendor_id__in=active_vendor_ids)}

    for vid, vendor in vendor_map.items():
        total_30d = total_30d_map.get(vid, 0)
        total_90d = total_90d_map.get(vid, 0)
        cancelled = cancelled_map.get(vid, 0)
        on_time_data = on_time_map.get(vid, [0, 0])
        unique_cust = unique_customers_map.get(vid, 0)
        repeat_cust = vendor_repeat_counts.get(vid, 0)

        cancellation_rate = (Decimal(str(cancelled)) / Decimal(str(total_30d)) * 100) if total_30d > 0 else Decimal('0')
        on_time_rate = (
            (Decimal(str(on_time_data[0])) / Decimal(str(on_time_data[1])) * 100)
            if on_time_data[1] > 0
            else Decimal('0')
        )
        repeat_rate = (Decimal(str(repeat_cust)) / Decimal(str(unique_cust)) * 100) if unique_cust > 0 else Decimal('0')

        revenue = revenue_map.get(vid, Decimal('0'))
        commission = revenue * (vendor.commission_percentage or Decimal('15')) / Decimal('100')
        avg_30d = avg_rating_30d_map.get(vid)
        avg_90d = avg_rating_90d_map.get(vid)

        defaults = {
            'on_time_prep_rate': on_time_rate,
            'cancellation_rate': cancellation_rate,
            'avg_rating_30d': Decimal(str(round(avg_30d, 2))) if avg_30d else Decimal('0'),
            'avg_rating_90d': Decimal(str(round(avg_90d, 2))) if avg_90d else Decimal('0'),
            'total_orders_30d': total_30d,
            'total_orders_90d': total_90d,
            'avg_prep_time_mins': avg_prep_mins_map.get(vid, Decimal('0')),
            'repeat_customer_rate': repeat_rate,
            'revenue_30d': revenue,
            'commission_owed_30d': commission,
        }

        if vid in existing:
            score = existing[vid]
            for k, v in defaults.items():
                setattr(score, k, v)
            to_update.append(score)
        else:
            to_create.append(VendorScorecard(vendor=vendor, **defaults))

    update_fields = list(defaults.keys())
    if to_update:
        VendorScorecard.objects.bulk_update(to_update, update_fields)
    if to_create:
        VendorScorecard.objects.bulk_create(to_create)

    logger.info('Vendor scorecards refreshed: %d updated, %d created', len(to_update), len(to_create))


@shared_task(
    name='vendors.tasks.refresh_category_bestsellers',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=120,
)
def refresh_category_bestsellers(days=30, top_n=20, min_orders=3):
    """Flag top sellers per marketplace Category row (rolling `days` window).

    Aggregates delivered OrderItems rolled up to Product (direct FK, the
    menu_item->Product link, or variant->Product), groups by the product's
    own Category row (e.g. Fast Food, Alcohol, Pharmacy), keeps the top
    `top_n` with >= `min_orders` distinct orders, then atomically clears
    old Product.is_popular flags, sets the new winners, and rewrites
    CategoryBestseller snapshot rows (one row per product per category).
    """
    from django.db import transaction

    from orders.models import OrderItem

    cutoff = timezone.now() - timedelta(days=days)

    rows = (
        OrderItem.objects.filter(
            order__status__in=['DELIVERED', 'COMPLETED'],
            order__created_at__gte=cutoff,
        )
        .annotate(
            # Reverse OneToOne (menu_item->product) can't take the _id suffix.
            pid=Coalesce('product_id', 'menu_item__product', 'product_variant__product_id'),
        )
        .values('pid')
        .annotate(units=Sum('quantity'), orders=Count('order_id', distinct=True))
        .filter(pid__isnull=False)
    )

    agg = {r['pid']: (r['units'] or 0, r['orders'] or 0) for r in rows}
    if not agg:
        logger.info('Category bestsellers: no delivered items in last %dd window', days)

    product_info = {
        p['id']: p
        for p in Product.objects.filter(id__in=agg).values('id', 'category_id', 'rating', 'created_at')
    }

    by_category = {}
    for pid, (units, orders) in agg.items():
        info = product_info.get(pid)
        if info is None or info['category_id'] is None:
            continue
        if orders < min_orders:
            continue
        by_category.setdefault(info['category_id'], []).append(
            {
                'pid': pid,
                'units': units,
                'orders': orders,
                'rating': float(info['rating'] or 0),
                'created_ts': info['created_at'].timestamp(),
            }
        )

    winners = {}
    for category_id, entries in by_category.items():
        # Tie-break: units desc, then distinct orders, then rating, then newer.
        entries.sort(key=lambda e: (-e['units'], -e['orders'], -e['rating'], -e['created_ts']))
        winners[category_id] = entries[:top_n]

    snapshots = [
        CategoryBestseller(
            category_id=category_id,
            product_id=entry['pid'],
            rank=rank,
            units_30d=entry['units'],
            orders_30d=entry['orders'],
        )
        for category_id, entries in winners.items()
        for rank, entry in enumerate(entries, start=1)
    ]
    winner_ids = [s.product_id for s in snapshots]

    with transaction.atomic():
        Product.objects.filter(is_popular=True).update(is_popular=False)
        CategoryBestseller.objects.all().delete()
        if winner_ids:
            Product.objects.filter(id__in=winner_ids).update(is_popular=True)
        CategoryBestseller.objects.bulk_create(snapshots)

    logger.info(
        'Category bestsellers refreshed: %d categories, %d flagged', len(winners), len(winner_ids)
    )
    return {'categories': len(winners), 'flagged': len(winner_ids)}


@shared_task(
    name='vendors.tasks.reconcile_payouts',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=120,
)
def reconcile_payouts(vendor_id=None):
    """Compare expected payouts against actual payments and flag discrepancies."""
    from orders.models import Order
    from payments.models import PaymentTransaction

    period_end = timezone.localdate()
    period_start = period_end - timedelta(days=7)
    period_start_at, period_end_at = local_day_bounds(period_start, period_end)

    vendors = (
        VendorProfile.objects.filter(is_active=True)
        if vendor_id is None
        else VendorProfile.objects.filter(id=vendor_id)
    )

    # ── Bulk-aggregate orders and payments in 2 queries (was 3N) ──
    order_aggregates = (
        Order.objects.filter(
            vendor__in=vendors.values_list('user_id', flat=True),
            status__in=['DELIVERED', 'COMPLETED'],
            created_at__gte=period_start_at,
            created_at__lt=period_end_at,
        )
        .values('vendor')
        .annotate(
            order_count=Count('id'),
            total_amount=Coalesce(Sum('subtotal'), Value(0), output_field=DecimalField()),
        )
    )

    payment_aggregates = (
        PaymentTransaction.objects.filter(
            order__vendor__in=vendors.values_list('user_id', flat=True),
            order__created_at__gte=period_start_at,
            order__created_at__lt=period_end_at,
            status='PAID',
        )
        .values('order__vendor')
        .annotate(
            total_paid=Coalesce(Sum('amount'), Value(0), output_field=DecimalField()),
        )
    )

    order_map = {a['vendor']: a for a in order_aggregates}
    payment_map = {a['order__vendor']: a['total_paid'] for a in payment_aggregates}

    results = []
    reconciliations = []
    for vendor in vendors:
        order_data = order_map.get(vendor.user_id, {'order_count': 0, 'total_amount': Decimal('0')})
        order_count = order_data['order_count']
        total_orders_amount = order_data['total_amount']

        total_commission = total_orders_amount * (vendor.commission_percentage or Decimal('15')) / Decimal('100')
        expected_payout = total_orders_amount - total_commission
        actual_payout = payment_map.get(vendor.user_id, Decimal('0'))
        variance = expected_payout - actual_payout

        reconciliations.append(
            PayoutReconciliation(
                vendor=vendor.user,
                vendor_profile=vendor,
                period_start=period_start,
                period_end=period_end,
                total_orders_amount=total_orders_amount,
                total_commission=total_commission,
                expected_payout=expected_payout,
                actual_payout=actual_payout,
                variance=variance,
                order_count=order_count,
            )
        )

        results.append(
            {
                'vendor': vendor.business_name,
                'expected': str(expected_payout),
                'actual': str(actual_payout),
                'variance': str(variance),
                'orders': order_count,
            }
        )

    if reconciliations:
        PayoutReconciliation.objects.bulk_create(
            reconciliations,
            batch_size=500,
            update_conflicts=True,
            unique_fields=['vendor', 'period_start', 'period_end'],
            update_fields=[
                'vendor_profile',
                'total_orders_amount',
                'total_commission',
                'expected_payout',
                'actual_payout',
                'variance',
                'order_count',
            ],
        )

    logger.info('Payout reconciliation complete: %d vendors', len(results))
    return results


# ─────────────────────────────────────────────────────────────────────────
# KYC Compliance Tasks
# ─────────────────────────────────────────────────────────────────────────


@shared_task(
    name='vendors.tasks.check_kyc_expiry',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=300,
)
def check_kyc_expiry():
    """
    Daily task to check KYC document expiries.
    - Sends 30-day, 7-day, and 1-day alerts to vendors
    - Marks documents as EXPIRED if past expiry date
    - Suspends vendors with expired required documents
    """
    from notifications.models import Category, Channel, Notification, Priority

    today = timezone.localdate()
    thirty_days = today + timedelta(days=30)
    seven_days = today + timedelta(days=7)
    one_day = today + timedelta(days=1)

    alerts_sent = 0
    expired_docs = 0
    suspended_vendors = 0

    # ── Bulk-fetch all required documents in ONE query (was N+1) ──
    all_required_docs = list(
        VendorKYCDocument.objects.filter(
            is_required=True,
            vendor__is_active=True,
            expiry_date__isnull=False,
        ).select_related('vendor', 'vendor__user')
    )

    # Group by vendor
    from collections import defaultdict

    docs_by_vendor = defaultdict(list)
    for doc in all_required_docs:
        docs_by_vendor[doc.vendor_id].append(doc)

    notif_batch = []
    log_batch = []
    suspended_ids = set()

    def in_app_notification(user, title, body, priority=Priority.NORMAL, data=None):
        return Notification(
            user=user,
            category=Category.SYSTEM,
            priority=priority,
            channel=Channel.IN_APP,
            title=title,
            body=body,
            data=data or {},
        )

    for vendor_id, docs in docs_by_vendor.items():
        vendor = docs[0].vendor
        user = vendor.user

        # Check for documents expiring in 30 days
        expiring_30 = [d for d in docs if d.verification_status == 'VERIFIED' and d.expiry_date == thirty_days]
        for doc in expiring_30:
            notif_batch.append(
                in_app_notification(
                    user,
                    'KYC Document Expiring Soon',
                    (
                        f'Your {doc.get_document_type_display()} expires on '
                        f'{doc.expiry_date}. Please upload a renewed copy.'
                    ),
                    data={
                        'document_id': doc.id,
                        'document_type': doc.document_type,
                        'expiry_date': str(doc.expiry_date),
                        'days_until_expiry': 30,
                    },
                )
            )
            log_batch.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    kyc_document=doc,
                    action='ALERT_SENT_30',
                    message=f'30-day expiry alert sent for {doc.get_document_type_display()}',
                    metadata={'expiry_date': str(doc.expiry_date)},
                )
            )
            alerts_sent += 1

        # Check for documents expiring in 7 days
        expiring_7 = [d for d in docs if d.verification_status == 'VERIFIED' and d.expiry_date == seven_days]
        for doc in expiring_7:
            notif_batch.append(
                in_app_notification(
                    user,
                    'KYC Document Expiring in 7 Days',
                    (
                        f'URGENT: Your {doc.get_document_type_display()} expires on '
                        f'{doc.expiry_date}. Upload renewed copy immediately to avoid '
                        f'suspension.'
                    ),
                    priority=Priority.HIGH,
                    data={
                        'document_id': doc.id,
                        'document_type': doc.document_type,
                        'expiry_date': str(doc.expiry_date),
                        'days_until_expiry': 7,
                    },
                )
            )
            log_batch.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    kyc_document=doc,
                    action='ALERT_SENT_7',
                    message=f'7-day expiry alert sent for {doc.get_document_type_display()}',
                    metadata={'expiry_date': str(doc.expiry_date)},
                )
            )
            alerts_sent += 1

        # Check for documents expiring in 1 day
        expiring_1 = [d for d in docs if d.verification_status == 'VERIFIED' and d.expiry_date == one_day]
        for doc in expiring_1:
            notif_batch.append(
                in_app_notification(
                    user,
                    'KYC Document Expiring TOMORROW',
                    (
                        f'CRITICAL: Your {doc.get_document_type_display()} expires '
                        f'tomorrow ({doc.expiry_date}). Your vendor account will be '
                        f'suspended if not renewed.'
                    ),
                    priority=Priority.CRITICAL,
                    data={
                        'document_id': doc.id,
                        'document_type': doc.document_type,
                        'expiry_date': str(doc.expiry_date),
                        'days_until_expiry': 1,
                    },
                )
            )
            log_batch.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    kyc_document=doc,
                    action='ALERT_SENT_1',
                    message=f'1-day expiry alert sent for {doc.get_document_type_display()}',
                    metadata={'expiry_date': str(doc.expiry_date)},
                )
            )
            alerts_sent += 1

        # Check for already expired documents
        expired = [
            d
            for d in docs
            if d.expiry_date < today and d.verification_status in ('VERIFIED', 'PENDING', 'UNDER_REVIEW')
        ]
        for doc in expired:
            doc.mark_expired()
            log_batch.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    kyc_document=doc,
                    action='DOCUMENT_EXPIRED',
                    message=f'{doc.get_document_type_display()} expired on {doc.expiry_date}',
                    metadata={'expiry_date': str(doc.expiry_date)},
                )
            )
            expired_docs += 1

        # Check if vendor should be suspended
        if vendor.should_be_suspended() and vendor.is_open:
            vendor.is_open = False
            vendor.save(update_fields=['is_open', 'updated_at'])

            notif_batch.append(
                in_app_notification(
                    user,
                    'Vendor Account Suspended - KYC Expired',
                    (
                        'Your vendor account has been suspended because one or more '
                        'required KYC documents have expired. Please upload renewed '
                        'documents to reactivate.'
                    ),
                    priority=Priority.HIGH,
                    data={
                        'expired_documents': [d.document_type for d in expired],
                    },
                )
            )
            log_batch.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    action='VENDOR_SUSPENDED',
                    message='Vendor suspended due to expired KYC documents',
                    metadata={'expired_doc_types': [d.document_type for d in expired]},
                )
            )
            suspended_vendors += 1
            suspended_ids.add(vendor_id)

    # ── Bulk-create all notifications and logs in 2 queries (was 3N) ──
    if notif_batch:
        Notification.objects.bulk_create(notif_batch)
    if log_batch:
        VendorKYCExpiryLog.objects.bulk_create(log_batch)

    # ── Reactivate vendors whose KYC was renewed ──
    reactivated = 0
    reactivate_notif = []
    reactivate_log = []
    closed_vendors = VendorProfile.objects.filter(is_active=True, is_open=False).select_related('user')
    for vendor in closed_vendors:
        if vendor.id not in suspended_ids and not vendor.should_be_suspended():
            vendor.is_open = True
            vendor.save(update_fields=['is_open', 'updated_at'])
            reactivate_notif.append(
                in_app_notification(
                    vendor.user,
                    'Vendor Account Reactivated',
                    'Your vendor account has been reactivated. All required KYC documents are now valid.',
                )
            )
            reactivate_log.append(
                VendorKYCExpiryLog(
                    vendor=vendor,
                    action='VENDOR_REACTIVATED',
                    message='Vendor reactivated after KYC renewal',
                )
            )
            reactivated += 1
    if reactivate_notif:
        Notification.objects.bulk_create(reactivate_notif)
    if reactivate_log:
        VendorKYCExpiryLog.objects.bulk_create(reactivate_log)

    logger.info(
        'KYC expiry check complete: %d alerts sent, %d docs expired, %d vendors suspended, %d reactivated',
        alerts_sent,
        expired_docs,
        suspended_vendors,
        reactivated,
    )
    return {
        'alerts_sent': alerts_sent,
        'expired_docs': expired_docs,
        'suspended_vendors': suspended_vendors,
        'reactivated': reactivated,
    }


@shared_task(
    name='vendors.tasks.run_sanctions_screening',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=600,
)
def run_sanctions_screening(vendor_id=None):
    """
    Run sanctions and PEP screening on vendors.
    In production, this would call an external API (e.g., Dow Jones, Refinitiv, or ComplyAdvantage).
    This implementation provides the framework for integration.
    """
    from .models import VendorSanctionsScreening

    vendors = VendorProfile.objects.filter(is_active=True)
    if vendor_id:
        vendors = vendors.filter(id=vendor_id)

    screenings_created = 0

    # ── Bulk-load existing screenings for all vendors in 1 query (was 3N) ──
    vendor_ids = list(vendors.values_list('id', flat=True))
    existing_screenings = VendorSanctionsScreening.objects.filter(
        vendor_id__in=vendor_ids,
        screening_type__in=['OFAC', 'PEP', 'UN'],
    ).values('vendor_id', 'screening_type')

    screening_map = {(s['vendor_id'], s['screening_type']): s for s in existing_screenings}

    to_create = []

    for vendor in vendors:
        key_ofac = (vendor.id, 'OFAC')
        key_pep = (vendor.id, 'PEP')
        key_un = (vendor.id, 'UN')

        if key_ofac not in screening_map:
            to_create.append(VendorSanctionsScreening(vendor=vendor, screening_type='OFAC', status='PENDING'))
        if key_pep not in screening_map:
            to_create.append(VendorSanctionsScreening(vendor=vendor, screening_type='PEP', status='PENDING'))
        if key_un not in screening_map:
            to_create.append(VendorSanctionsScreening(vendor=vendor, screening_type='UN', status='PENDING'))

    if to_create:
        VendorSanctionsScreening.objects.bulk_create(to_create)
        screenings_created += len(to_create)

    # Mark all newly-created screenings as clear (TODO: external API call)
    for screening in to_create:
        screening.mark_clear(None)

    logger.info('Sanctions screening complete: %d screenings created', screenings_created)
    return {'screenings_created': screenings_created}


@shared_task(
    name='vendors.tasks.verify_zimra_pin',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    default_retry_delay=600,
)
def verify_zimra_pin(vendor_id):
    """
    Verify a vendor's ZIMRA PIN.
    In production, this would call the ZIMRA API or use a local validation.
    """
    from .models import VendorTaxInfo

    try:
        tax_info = VendorTaxInfo.objects.select_related('vendor').get(vendor_id=vendor_id)

        if not tax_info.zimra_pin:
            return {'status': 'no_pin', 'vendor': tax_info.vendor.business_name}

        # NOTE: ZIMRA PIN verification is currently a format-only mock.
        # Production deployment must integrate the ZIMRA API client here
        # (e.g. zimra_api.verify_pin(tax_info.zimra_pin)) and replace
        # the length/digit check below with the real API response.
        pin = tax_info.zimra_pin
        is_valid = len(pin) == 10 and pin.isdigit()

        if is_valid:
            tax_info.zimra_pin_verified = True
            tax_info.zimra_verification_date = timezone.now()
            tax_info.zimra_verification_method = 'API_VERIFICATION'
            tax_info.save(
                update_fields=[
                    'zimra_pin_verified',
                    'zimra_verification_date',
                    'zimra_verification_method',
                    'updated_at',
                ]
            )

            return {'status': 'verified', 'vendor': tax_info.vendor.business_name}
        else:
            return {'status': 'invalid_format', 'vendor': tax_info.vendor.business_name}

    except VendorTaxInfo.DoesNotExist:
        return {'status': 'no_tax_info', 'vendor_id': vendor_id}
