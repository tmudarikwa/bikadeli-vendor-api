from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.db import models
from django.utils import timezone

from orders.models import Order, OrderItem, Parcel
from payments.models import VendorPayout
from vendors.models import (
    Category,
    InventoryAdjustmentLog,
    MenuCategory,
    MenuItem,
    PayoutReconciliation,
    Product,
    Promotion,
    VendorKYCDocument,
    VendorKYCVerification,
    VendorProfile,
    VendorSanctionsScreening,
    VendorSupportTicket,
    VendorTaxInfo,
    VendorTicketMessage,
)

User = get_user_model()
HARARE = Point(31.0333, -17.8250, srid=4326)

# Compliance backfill for the public storefront listing gate
# (PublicVendorViewSet.get_queryset): demo vendors need VERIFIED required KYC
# docs, a valid tax clearance, and no CONFIRMED sanctions match to be
# browsable. Far-future expiries keep periodic expiry tasks quiet.
DEMO_DOC_EXPIRY_YEARS = 5
DEMO_DOC_FILE_SIZE = 1024
FOOD_VENDOR_CATEGORIES = {'RESTAURANT', 'FAST_FOOD'}
# Statuses a seed backfill may promote to VERIFIED. REJECTED is deliberately
# excluded — a rejected doc needs human review, never an automated flip.
FLIPPABLE_DOC_STATUSES = ('PENDING', 'UNDER_REVIEW', 'EXPIRED')


def required_doc_types_for_vendor(profile):
    """Required KYC doc types for a demo vendor, derived from its category.

    NATIONAL_ID is the required type evidenced in prod; food vendors also get
    FOOD_LICENSE. Extend here if compliance adds category-specific types.
    """
    doc_types = ['NATIONAL_ID']
    if (profile.category or '').upper() in FOOD_VENDOR_CATEGORIES:
        doc_types.append('FOOD_LICENSE')
    return doc_types


def ensure_vendor_listing_compliance(profile):
    """Make a demo vendor pass the public-listing compliance gates.

    - Flips existing required docs in flippable statuses to VERIFIED with a
      far-future expiry (in place — never duplicates rows, never touches
      REJECTED docs).
    - Creates missing required doc types with VERIFIED status and demo
      placeholder files.
    - Ensures a VendorTaxInfo row with a future clearance expiry.
    - Ensures an OFAC/CLEAR sanctions screening when none exists (absent or
      non-confirmed screenings already pass the gate; CONFIRMED_MATCH rows
      are left for human review).
    Returns a summary dict for logging.
    """
    expiry = timezone.localdate() + timedelta(days=365 * DEMO_DOC_EXPIRY_YEARS)
    ensured = flipped = blocked = 0
    for doc_type in required_doc_types_for_vendor(profile):
        existing = profile.kyc_documents.filter(document_type=doc_type, is_required=True)
        if not existing.exists():
            VendorKYCDocument.objects.create(
                vendor=profile,
                document_type=doc_type,
                file=f'vendors/kyc/demo/{doc_type.lower()}.pdf',
                file_size=DEMO_DOC_FILE_SIZE,
                original_filename=f'{doc_type.lower()}.pdf',
                verification_status='VERIFIED',
                is_required=True,
                expiry_date=expiry,
            )
            ensured += 1
            continue
        flipped += existing.filter(verification_status__in=FLIPPABLE_DOC_STATUSES).update(
            verification_status='VERIFIED', expiry_date=expiry, rejection_reason=''
        )
        existing.filter(verification_status='VERIFIED', expiry_date__lt=expiry).update(expiry_date=expiry)
        blocked += existing.filter(verification_status='REJECTED').count()
    VendorTaxInfo.objects.update_or_create(
        vendor=profile,
        defaults={'tax_clearance_expiry': expiry},
    )
    screenings = profile.sanctions_screenings.order_by('-screened_at', '-pk')
    if not screenings.exists():
        VendorSanctionsScreening.objects.create(
            vendor=profile, screening_type='OFAC', status='CLEAR'
        )
    elif screenings.first().status == 'CONFIRMED_MATCH':
        blocked += 1
    return {'ensured_docs': ensured, 'flipped_docs': flipped, 'blocked_docs': blocked}


class Command(BaseCommand):
    help = 'Delete all test vendor data and seed 3 complete demo vendors'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-cleanup',
            action='store_true',
            help='Skip PHASE 1 cleanup (prod runs must preserve existing vendors).',
        )

    def handle(self, *args, **options):  # noqa: ARG002 - framework signature must stay fixed
        self.stdout.write(self.style.MIGRATE_HEADING('PHASE 1: CLEANUP'))

        if options.get('skip_cleanup'):
            self.stdout.write(self.style.WARNING('  Skipped (--skip-cleanup); existing vendors preserved'))
            self.stdout.write(f'  Existing vendors: {VendorProfile.objects.count()}')
        else:
            self._cleanup_test_vendors()

        self.stdout.write(self.style.MIGRATE_HEADING('PHASE 2: SEEDING'))

        self._seed_food_vendor(
            'madarenas@email.com',
            'Madarenas Pizza',
            'RESTAURANT',
            '123 Samora Machel Ave, Harare',
            [
                (
                    'Pizzas',
                    [
                        ('Margherita', 8.00, 15),
                        ('Pepperoni', 10.00, 15),
                        ('BBQ Chicken', 12.00, 20),
                        ('Hawaiian', 9.00, 15),
                    ],
                ),
                ('Sides', [('Garlic Bread', 4.00, 10), ('Coke 500ml', 2.00, 5)]),
            ],
        )

        self._seed_food_vendor(
            'spicychicken@email.com',
            'Spicy Chicken Inn',
            'FAST_FOOD',
            '456 Julius Nyerere Way, Harare',
            [
                (
                    'Chicken',
                    [
                        ('2pc Chicken & Chips', 7.00, 15),
                        ('4pc Chicken & Chips', 12.00, 20),
                        ('Chicken Wings', 5.00, 10),
                    ],
                ),
                ('Burgers', [('Chicken Burger', 6.00, 10), ('Beef Burger', 8.00, 15), ('Fries', 3.00, 5)]),
            ],
        )

        self._seed_grocery_vendor(
            'freshmart@email.com',
            'FreshMart Groceries',
            '789 Robert Mugabe Rd, Harare',
            [
                ('Bakery', [('Bread', 1.50, 'loaf', 0.5)]),
                ('Dairy', [('Milk 1L', 2.00, 'bottle', 1.0), ('Eggs (6)', 3.00, 'pack', 0.3)]),
                ('Fruits & Vegetables', [('Apples 1kg', 4.00, 'kg', 1.0)]),
                ('Pantry', [('Rice 2kg', 6.00, 'bag', 2.0), ('Cooking Oil 750ml', 5.00, 'bottle', 0.75)]),
            ],
        )

        self.stdout.write(self.style.MIGRATE_HEADING('PHASE 3: VERIFICATION'))

        vendors = VendorProfile.objects.all()
        self.stdout.write(f'  Total vendors: {vendors.count()}')
        for v in vendors:
            items = v.menu_items.filter(is_available=True).count()
            products = v.products.filter(is_available=True).count()
            self.stdout.write(
                f'    {v.business_name} (id={v.id}) — open={v.is_open}, '
                f'verified={v.is_verified}, active={v.is_active}, '
                f'lifecycle={v.lifecycle_state}, location={v.business_location is not None}, '
                f'menu_items={items}, products={products}'
            )
            required = v.kyc_documents.filter(is_required=True)
            verified_docs = required.filter(verification_status='VERIFIED').count()
            self.stdout.write(
                f'      compliance: {verified_docs}/{required.count()} required docs VERIFIED'
            )

        test_remaining = VendorProfile.objects.filter(
            models.Q(business_name__startswith='Test Restaurant ')
            | models.Q(business_name='Test Business')
            | models.Q(business_name='E2E Test Restaurant')
        ).count()
        self.stdout.write(f'  Test vendors remaining: {test_remaining}')

        self.stdout.write(self.style.SUCCESS('✓ DONE'))

    def _cleanup_test_vendors(self):
        test_profiles = VendorProfile.objects.filter(
            models.Q(business_name__startswith='Test Restaurant ')
            | models.Q(business_name='Test Business')
            | models.Q(business_name='E2E Test Restaurant')
        )
        count = test_profiles.count()
        self.stdout.write(f'  Found {count} test vendor profiles')

        if count == 0:
            self.stdout.write(self.style.WARNING('  No test vendors found, skipping cleanup'))
        else:
            test_users = list(test_profiles.values_list('user_id', flat=True))
            test_profile_ids = list(test_profiles.values_list('id', flat=True))

            OrderItem.objects.filter(menu_item__vendor_id__in=test_profile_ids).delete()
            OrderItem.objects.filter(product__vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ OrderItem')

            Order.objects.filter(vendor_id__in=test_users).delete()
            self.stdout.write('  ✓ Order')

            Parcel.objects.filter(sender_id__in=test_users).delete()
            self.stdout.write('  ✓ Parcel')

            VendorTicketMessage.objects.filter(ticket__vendor_id__in=test_profile_ids).delete()
            VendorSupportTicket.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Support tickets')

            VendorTaxInfo.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Tax info')

            VendorPayout.objects.filter(vendor_id__in=test_users).delete()
            self.stdout.write('  ✓ Payouts')

            PayoutReconciliation.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Reconciliations')

            Promotion.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Promotions')

            InventoryAdjustmentLog.objects.filter(product__vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Inventory logs')

            VendorKYCDocument.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ KYC documents')

            VendorKYCVerification.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ KYC verifications')

            VendorSanctionsScreening.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Sanctions')

            MenuCategory.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Menu categories')

            MenuItem.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Menu items')

            Product.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Products')

            Category.objects.filter(vendor_id__in=test_profile_ids).delete()
            self.stdout.write('  ✓ Categories')

            test_profiles.delete()
            self.stdout.write('  ✓ Vendor profiles')

        self.stdout.write(f'  Remaining vendors: {VendorProfile.objects.count()}')

    def _log_compliance(self, name, summary):
        self.stdout.write(
            f'    compliance: {summary["ensured_docs"]} docs created, {summary["flipped_docs"]} flipped'
        )
        if summary['blocked_docs']:
            self.stdout.write(
                self.style.WARNING(
                    f'    ⚠ {name}: {summary["blocked_docs"]} blocking row(s) need human review '
                    '(REJECTED doc or CONFIRMED sanctions match)'
                )
            )

    def _seed_food_vendor(self, email, name, category, address, menu_items):
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.WARNING(f'  ⚠ User {email} not found, skipping'))
            return

        profile, _ = VendorProfile.objects.get_or_create(
            user=user,
            defaults={
                'business_name': name,
                'category': category,
                'description': f'{name} — quality food delivered fast',
                'business_address': address,
                'business_phone': '+263771000001',
                'business_email': email,
                'business_location': HARARE,
                'is_open': True,
                'opening_time': '00:00',
                'closing_time': '23:59',
                'is_verified': True,
                'is_active': True,
                'lifecycle_state': VendorProfile.LIFECYCLE_ACTIVE,
                'preparation_time_mins': 15,
                'delivery_radius_km': 10,
                'minimum_order': Decimal('2.00'),
                'delivery_fee': Decimal('1.00'),
                'pickup_available': True,
                'commission_percentage': Decimal('15.00'),
            },
        )
        for fld, val in {
            'is_open': True,
            'opening_time': '00:00',
            'closing_time': '23:59',
            'is_verified': True,
            'is_active': True,
            'lifecycle_state': VendorProfile.LIFECYCLE_ACTIVE,
            'preparation_time_mins': 15,
            'delivery_radius_km': 10,
            'minimum_order': Decimal('2.00'),
            'delivery_fee': Decimal('1.00'),
            'pickup_available': True,
            'business_location': HARARE,
        }.items():
            if getattr(profile, fld) != val:
                setattr(profile, fld, val)
        profile.save()
        self.stdout.write(f'  ✓ {name} (id={profile.id})')
        self._log_compliance(name, ensure_vendor_listing_compliance(profile))

        for cat_name, items in menu_items:
            cat, _ = MenuCategory.objects.get_or_create(
                vendor=profile,
                name=cat_name,
                defaults={'description': f'{cat_name} at {name}', 'order': 1, 'is_active': True},
            )
            for item_name, price, prep in items:
                MenuItem.objects.get_or_create(
                    vendor=profile,
                    category=cat,
                    name=item_name,
                    defaults={
                        'description': f'Fresh {item_name.lower()}',
                        'price': Decimal(str(price)),
                        'is_available': True,
                        'preparation_time_mins': prep,
                    },
                )

    def _seed_grocery_vendor(self, email, name, address, categories):
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.WARNING(f'  ⚠ User {email} not found, skipping'))
            return

        profile, _ = VendorProfile.objects.get_or_create(
            user=user,
            defaults={
                'business_name': name,
                'category': 'GROCERY',
                'description': f'{name} — fresh groceries delivered',
                'business_address': address,
                'business_phone': '+263771000002',
                'business_email': email,
                'business_location': HARARE,
                'is_open': True,
                'opening_time': '00:00',
                'closing_time': '23:59',
                'is_verified': True,
                'is_active': True,
                'lifecycle_state': VendorProfile.LIFECYCLE_ACTIVE,
                'preparation_time_mins': 10,
                'delivery_radius_km': 10,
                'minimum_order': Decimal('2.00'),
                'delivery_fee': Decimal('1.00'),
                'pickup_available': True,
                'commission_percentage': Decimal('15.00'),
            },
        )
        for fld, val in {
            'is_open': True,
            'opening_time': '00:00',
            'closing_time': '23:59',
            'is_verified': True,
            'is_active': True,
            'lifecycle_state': VendorProfile.LIFECYCLE_ACTIVE,
            'preparation_time_mins': 10,
            'delivery_radius_km': 10,
            'minimum_order': Decimal('2.00'),
            'delivery_fee': Decimal('1.00'),
            'pickup_available': True,
            'business_location': HARARE,
        }.items():
            if getattr(profile, fld) != val:
                setattr(profile, fld, val)
        profile.save()
        self.stdout.write(f'  ✓ {name} (id={profile.id})')
        self._log_compliance(name, ensure_vendor_listing_compliance(profile))

        for order, (cat_name, products) in enumerate(categories, start=1):
            cat, _ = Category.objects.get_or_create(
                name=cat_name,
                vendor=profile,
                defaults={'sort_order': order},
            )
            for pname, price, unit, weight in products:
                Product.objects.get_or_create(
                    vendor=profile,
                    name=pname,
                    defaults={
                        'description': f'Fresh {pname.lower()}',
                        'category': cat,
                        'price': Decimal(str(price)),
                        'unit': unit,
                        'weight_kg': Decimal(str(weight)) if weight else None,
                        'is_available': True,
                        'is_inventory_tracked': False,
                        'quantity_on_hand': 100,
                        'low_stock_threshold': 5,
                        'product_type': 'GROCERY',
                    },
                )
