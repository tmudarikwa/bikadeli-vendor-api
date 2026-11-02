"""Create (or verify) a live demo vendor with menu — idempotent.

Examples:
    python manage.py create_demo_vendor --preset chicken_inn
    VENDOR_PRESET=ocean_basket VENDOR_PASSWORD=s3cret python manage.py create_demo_vendor
"""

import os
import secrets
from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand

from vendors.models import MenuCategory, MenuItem, VendorKYCDocument, VendorProfile

User = get_user_model()

PRESETS = {
    'chicken_inn': {
        'name': 'Chicken Inn Helensvale',
        'address': '31 Crowhill Rd, Harare',
        'lat': -17.737069132806432,
        'lng': 31.10258384047756,
        'email': 'chicken-inn-helensvale@zimdelivery.co.zw',
        'phone': '+263778100100',
        'category': 'FAST_FOOD',
        'description': 'Flame-grilled chicken, burgers, fries and more.',
        'menu': [
            ('Chicken Deals', 0, [
                ('1-Piecer & Fries', 'Crispy fried chicken piece with regular fries', '3.90', False),
                ('2-Piecer & Fries', "Zimbabwe's favourite: 2 juicy pieces with crispy fries", '3.50', True),
                ('3-Piecer', 'Three crispy pieces with a scoop of fries', '7.30', False),
                ('2-Piecer + Fried Rice', '2 pieces with a scoop of fried rice', '4.50', False),
            ]),
            ('Burgers & More', 1, [
                ('Chicken Burger & Fries', 'Crispy fillet burger with fries', '4.00', False),
                ('Wings (6 pc)', 'Six flame-tossed wings', '5.00', False),
            ]),
            ('Sides', 2, [
                ('Regular Fries', 'Golden crispy fries', '1.80', False),
                ('Large Fries', 'Large scoop of fries for sharing', '2.80', False),
            ]),
            ('Drinks', 3, [
                ('Canned Drink', '330ml can, assorted flavours', '1.00', False),
                ('500ml Pet', '500ml bottle, assorted flavours', '1.50', False),
            ]),
        ],
    },
    'ocean_basket': {
        'name': "Ocean Basket Sam Levy's Village",
        'address': "Shop No 3, Entertainment Centre, Sam Levy's Village, Borrowdale Rd, Harare",
        'lat': -17.761321484333006,
        'lng': 31.087170395974237,
        'email': 'ocean-basket-samlevy@zimdelivery.co.zw',
        'phone': '+263778100101',
        'category': 'RESTAURANT',
        'description': 'Fresh seafood: fish & chips, calamari, prawns and sushi.',
        'menu': [
            ('Fish & Chips', 0, [
                ('Hake & Chips', 'Crispy hake fillet with chips and tartar', '8.50', True),
                ('Kingklip & Chips', 'Grilled kingklip with chips and lemon butter', '12.00', False),
                ('Calamari & Chips', 'Tender calamari rings with chips', '9.50', False),
                ('Fish Fingers Kids Meal', 'Kids fish fingers with chips and juice', '5.00', False),
            ]),
            ('Seafood Mains', 1, [
                ('Grilled Prawns (8)', 'Eight prawns in lemon-garlic butter with rice', '14.00', False),
                ('Seafood Pasta', 'Prawns, calamari and mussels in creamy sauce', '11.50', False),
                ('Sushi Platter (18 pc)', 'Chef selection: salmon, tuna and california rolls', '18.00', False),
            ]),
            ('Sides', 2, [
                ('Side Salad', 'Fresh garden salad', '3.50', False),
                ('Extra Tartar', 'Homemade tartar sauce', '1.00', False),
            ]),
            ('Drinks', 3, [
                ('Canned Drink', '330ml can, assorted flavours', '1.00', False),
                ('500ml Pet', '500ml bottle, assorted flavours', '1.50', False),
            ]),
        ],
    },
    'rocco_mammas': {
        'name': 'Rocco Mammas Village Walk',
        'address': 'Village Walk, Borrowdale, Harare',
        'lat': -17.75611324016324,
        'lng': 31.08552913045155,
        'email': 'rocco-mammas-villagewalk@zimdelivery.co.zw',
        'phone': '+263778100102',
        'category': 'FAST_FOOD',
        'description': 'Smashed burgers, wings, ribs and shakes.',
        'menu': [
            ('Smash Burgers', 0, [
                ('Classic Smash', 'Double smashed patty, cheese, house sauce', '7.50', True),
                ('Bacon & Cheese Smash', 'Double patty, bacon, cheddar, fries', '9.00', False),
                ('Mushroom Swiss', 'Smashed patty, mushrooms, swiss cheese', '8.50', False),
                ('Crispy Chicken Burger', 'Crispy fillet, slaw, chipotle mayo', '6.50', False),
            ]),
            ('Grill', 1, [
                ('Buffalo Wings (8)', 'Tossed in buffalo sauce with dip', '7.00', False),
                ('Pork Ribs', 'Slow-grilled rack with fries', '12.50', False),
                ('Loaded Fries', 'Cheese sauce, bacon bits, spring onion', '4.50', False),
                ('Onion Rings', 'Beer-battered rings', '3.50', False),
            ]),
            ('Shakes & Drinks', 2, [
                ('Milkshake', 'Vanilla, chocolate or strawberry', '4.00', False),
                ('Soda', 'Can, assorted flavours', '1.50', False),
            ]),
        ],
    },
}


class Command(BaseCommand):
    help = 'Create (or verify) a live demo vendor with a starter menu — idempotent'

    def add_arguments(self, parser):
        parser.add_argument('--preset', default='chicken_inn', choices=list(PRESETS))
        parser.add_argument('--name', default=None)
        parser.add_argument('--lat', type=float, default=None)
        parser.add_argument('--lng', type=float, default=None)
        parser.add_argument('--email', default=None)
        parser.add_argument('--phone', default=None)
        parser.add_argument('--address', default=None)
        parser.add_argument('--category', default=None)
        parser.add_argument('--description', default=None)
        parser.add_argument('--radius', type=int, default=10)
        parser.add_argument('--minimum', type=str, default='3.00')
        parser.add_argument('--password', default=None, help='Set a usable login password (printed once)')

    def handle(self, *args, **opts):  # noqa: ARG002 - framework signature must stay fixed
        preset_name = os.environ.get('VENDOR_PRESET') or opts['preset']
        preset = PRESETS[preset_name]
        name = opts['name'] or preset['name']
        lat = opts['lat'] if opts['lat'] is not None else preset['lat']
        lng = opts['lng'] if opts['lng'] is not None else preset['lng']
        email = opts['email'] or preset['email']
        phone = opts['phone'] or preset['phone']
        address = opts['address'] or preset['address']
        category = opts['category'] or preset['category']
        description = opts['description'] or preset['description']

        username = email.split('@')[0].replace('.', '_').replace('-', '_')[:30]
        user, user_created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': username,
                'phone_number': phone,
                'first_name': name,
                'last_name': '',
                'user_type': 'VENDOR',
                'is_active': True,
            },
        )
        password = opts['password'] or os.environ.get('VENDOR_PASSWORD')
        if user_created and not password:
            password = secrets.token_urlsafe(12)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
            self.stdout.write(self.style.WARNING(f'LOGIN — email: {email} | password: {password}'))
        elif user_created:
            user.set_unusable_password()
            user.save(update_fields=['password'])
        self.stdout.write(
            (self.style.SUCCESS('Created vendor user: ') if user_created else 'Vendor user exists: ') + email
        )

        vendor, vendor_created = VendorProfile.objects.get_or_create(
            user=user,
            defaults={
                'business_name': name,
                'category': category,
                'description': description,
                'business_address': address,
                'business_phone': phone,
                'business_email': email,
                'business_location': Point(lng, lat, srid=4326),
                'is_open': True,
                'opening_time': time(8, 0),
                'closing_time': time(22, 0),
                'is_verified': True,
                'is_active': True,
                'lifecycle_state': VendorProfile.LIFECYCLE_ACTIVE,
                'preparation_time_mins': 15,
                'delivery_radius_km': opts['radius'],
                'minimum_order': Decimal(opts['minimum']),
                'delivery_fee': Decimal('0'),
                'pickup_available': True,
            },
        )
        if vendor_created:
            self.stdout.write(self.style.SUCCESS(f'Created vendor profile: {name} (id={vendor.id})'))
        else:
            self.stdout.write(f'Vendor profile exists: {name} (id={vendor.id})')

        # Required verified KYC row — without it the vendor silently won't list.
        _doc, doc_created = VendorKYCDocument.objects.get_or_create(
            vendor=vendor,
            document_type='NATIONAL_ID',
            is_required=True,
            defaults={
                'file': 'demo/vendor-national-id.pdf',
                'file_size': 1,
                'original_filename': 'vendor-national-id.pdf',
                'verification_status': 'VERIFIED',
            },
        )
        if doc_created:
            self.stdout.write(self.style.SUCCESS('Created verified KYC document'))

        created_items = 0
        for cat_name, order, items in preset['menu']:
            category_obj, _ = MenuCategory.objects.get_or_create(
                vendor=vendor, name=cat_name, defaults={'order': order, 'is_active': True}
            )
            for item_name, desc, price, popular in items:
                _, item_created = MenuItem.objects.get_or_create(
                    vendor=vendor,
                    category=category_obj,
                    name=item_name,
                    defaults={
                        'description': desc,
                        'price': Decimal(price),
                        'is_available': True,
                        'is_popular': popular,
                        'preparation_time_mins': 10,
                    },
                )
                created_items += int(item_created)
        self.stdout.write(self.style.SUCCESS(f'Menu ready ({created_items} new items) — vendor id={vendor.id}'))
