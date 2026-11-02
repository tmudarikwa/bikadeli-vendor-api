import random
from decimal import Decimal

from core.management.commands._placeholder_images import (
    generate_menu_item_image,
    generate_product_image,
    generate_vendor_cover,
    generate_vendor_logo,
)
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from customers.models import CustomerProfile
from orders.models import Order, OrderItem
from vendors.models import Category, MenuCategory, MenuItem, Product, Promotion, VendorProfile

User = get_user_model()


class Command(BaseCommand):
    help = 'Seeds the database with demo data for all portals'

    def handle(self, *args, **kwargs):  # noqa: ARG002 - framework signature must stay fixed
        # 1. Create demo vendor user
        vendor_email = 'demo-vendor@zimdelivery.co.zw'
        vendor_user, created = User.objects.get_or_create(
            email=vendor_email,
            defaults={
                'username': 'demo_vendor',
                'phone_number': '+263771234567',
                'user_type': 'vendor',
                'is_active': True,
            },
        )
        if created:
            vendor_user.set_password('demo1234')
            vendor_user.save()
            self.stdout.write(self.style.SUCCESS(f'Created vendor user: {vendor_email}'))

        vendor_profile, created = VendorProfile.objects.get_or_create(
            user=vendor_user,
            defaults={
                'business_name': 'FreshMart',
                'business_phone': '+263771234567',
                'business_address': '123 Samora Machel Ave, Harare',
                'category': 'GROCERY',
                'is_open': True,
                'preparation_time_mins': 20,
                'delivery_radius_km': 10,
                'minimum_order': Decimal('5.00'),
                'delivery_fee': Decimal('2.50'),
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created vendor profile: FreshMart'))
        if not vendor_profile.business_logo:
            vendor_profile.business_logo.save('demo_vendor_logo.png', generate_vendor_logo('FreshMart'), save=False)
        if not vendor_profile.business_cover:
            vendor_profile.business_cover.save('demo_vendor_cover.png', generate_vendor_cover('FreshMart'), save=False)
        vendor_profile.save()

        # 2. Top-level categories (parent=None, no vendor)
        top_level = [
            ('Restaurants', '🍽️'),
            ('Groceries', '🛒'),
            ('Pharmacy', '💊'),
            ('Drinks', '🍺'),
            ('Pets', '🐾'),
            ('Flowers', '💐'),
        ]

        top_categories = {}
        for name, icon in top_level:
            cat, _ = Category.objects.update_or_create(name=name, parent=None, defaults={'icon': icon, 'sort_order': 0})
            top_categories[name] = cat
        self.stdout.write(self.style.SUCCESS(f'Top-level categories: {list(top_categories.keys())}'))

        # 3. Sub-categories with parents
        sub_level = [
            ('Fast Food', '🍔', 'Restaurants'),
            ('African Cuisine', '🍛', 'Restaurants'),
            ('Asian Cuisine', '🥡', 'Restaurants'),
            ('Italian & Pizza', '🍕', 'Restaurants'),
            ('Chicken & Grill', '🍗', 'Restaurants'),
            ('Sushi & Seafood', '🍣', 'Restaurants'),
            ('Sandwiches & Wraps', '🥪', 'Restaurants'),
            ('Breakfast & Brunch', '🥞', 'Restaurants'),
            ('Desserts & Ice Cream', '🍦', 'Restaurants'),
            ('Fruits & Vegetables', '🥬', 'Groceries'),
            ('Butchery & Meat', '🥩', 'Groceries'),
            ('Fish & Seafood', '🐟', 'Groceries'),
            ('Dairy & Eggs', '🥚', 'Groceries'),
            ('Bakery', '🥖', 'Groceries'),
            ('Frozen Foods', '❄️', 'Groceries'),
            ('Soft Drinks & Water', '🥤', 'Drinks'),
            ('Juices & Smoothies', '🧃', 'Drinks'),
            ('Coffee & Tea', '☕', 'Drinks'),
            ('Alcohol & Liquor', '🍺', 'Drinks'),
            ('Personal Care', '🧴', 'Pharmacy'),
            ('Vitamins & Supplements', '💪', 'Pharmacy'),
            ('Baby & Kids', '🍼', 'Pharmacy'),
            ('Cleaning & Household', '🧹', 'Pharmacy'),
            ('Pet Supplies', '🐾', 'Pets'),
            ('Flowers & Gifts', '💐', 'Flowers'),
            ('Stationery & Office', '📎', 'Flowers'),
            ('Electronics', '🔌', 'Flowers'),
            ('Courier & Parcels', '📦', 'Flowers'),
            ('Health & Wellness', '🧘', 'Pharmacy'),
        ]

        sub_categories = {}
        for name, icon, parent_name in sub_level:
            parent = top_categories.get(parent_name)
            cat, _ = Category.objects.update_or_create(
                name=name, parent=parent, defaults={'icon': icon, 'sort_order': 0}
            )
            sub_categories[name] = cat
        self.stdout.write(self.style.SUCCESS(f'Sub-categories created: {len(sub_categories)}'))

        all_categories = {**top_categories, **sub_categories}

        # 4. Products
        product_list = [
            ('Chicken Burger', 'Juicy grilled chicken with fresh lettuce', 'Restaurants', 8.50, 40, 'piece', []),
            ('Beef Burger', '100% pure beef patty with cheese', 'Restaurants', 9.00, 30, 'piece', ['halal']),
            (
                'Margherita Pizza',
                'Classic tomato, mozzarella, basil',
                'Italian & Pizza',
                12.00,
                20,
                'piece',
                ['vegetarian'],
            ),
            ('Sushi Box', 'Fresh salmon and avocado rolls', 'Sushi & Seafood', 15.00, 15, 'box', []),
            (
                'Bananas',
                'Organic ripe bananas, 1 kg',
                'Fruits & Vegetables',
                2.50,
                200,
                'kg',
                ['organic', 'gluten-free'],
            ),
            ('Tomatoes', 'Locally grown red tomatoes', 'Fruits & Vegetables', 3.00, 150, 'kg', ['organic']),
            ('Whole Chicken', 'Fresh whole chicken, 1.5 kg avg', 'Butchery & Meat', 7.50, 60, 'piece', []),
            ('Milk 1L', 'Full cream pasteurised milk', 'Dairy & Eggs', 2.20, 100, 'litre', []),
            ('Bread Loaf', 'White bread, freshly baked', 'Bakery', 1.80, 80, 'loaf', []),
            ('Paracetamol', '500mg tablets, pack of 20', 'Pharmacy', 1.50, 300, 'pack', []),
            ('Cough Syrup', 'Dry cough relief 100ml', 'Pharmacy', 4.00, 50, 'bottle', []),
            ('Hand Sanitizer', 'Alcohol gel 250ml', 'Personal Care', 3.20, 120, 'bottle', []),
            ('Coca Cola 2L', 'Original taste soft drink', 'Soft Drinks & Water', 2.80, 90, 'bottle', []),
            ('Mineral Water 1.5L', 'Natural spring water', 'Soft Drinks & Water', 1.20, 200, 'bottle', []),
            ('Dog Food 5kg', 'Dry adult dog food, chicken flavour', 'Pet Supplies', 12.00, 25, 'bag', []),
            ('Cat Litter 10L', 'Clumping bentonite litter', 'Pet Supplies', 8.50, 20, 'bag', []),
            ('Red Roses Bouquet', '12 long-stem red roses', 'Flowers & Gifts', 18.00, 10, 'bouquet', []),
            ('Sunflower Bunch', '5 stems of giant sunflowers', 'Flowers & Gifts', 10.00, 15, 'bunch', []),
        ]

        for name, desc, cat_name, price, stock, unit, tags in product_list:
            category = all_categories.get(cat_name)
            if not category:
                self.stdout.write(self.style.WARNING(f'Category not found: {cat_name}'))
                continue
            price = round(random.uniform(price * 0.9, price * 1.1), 2)
            stock = random.randint(max(1, stock - 10), stock + 10)
            p, p_created = Product.objects.update_or_create(
                vendor=vendor_profile,
                name=name,
                defaults={
                    'description': desc,
                    'category': category,
                    'price': price,
                    'stock': stock,
                    'unit': unit,
                    'is_available': True,
                    'dietary_tags': tags,
                },
            )
            # Add product image
            from vendors.models import ProductImage as PImg

            if not PImg.objects.filter(product=p).exists():
                pi = PImg.objects.create(product=p, image=None, alt_text=name, sort_order=0)
                pi.image.save(f'product_{p.id}.png', generate_product_image(name), save=False)
                pi.save()
        self.stdout.write(self.style.SUCCESS(f'Seeded {len(product_list)} products'))

        # 5. Menu categories for vendor portal
        menu_cats = [
            ('Fresh Produce', 'Seasonal fruits and vegetables'),
            ('Dairy & Eggs', 'Milk, cheese, yogurt, and eggs'),
            ('Meat & Poultry', 'Fresh cuts of beef, chicken, and pork'),
            ('Bakery', 'Freshly baked bread and pastries'),
            ('Beverages', 'Juices, water, and soft drinks'),
            ('Pantry Staples', 'Rice, pasta, oil, and spices'),
            ('Snacks', 'Chips, biscuits, and confectionery'),
        ]
        menu_cat_objs = {}
        for order, (name, desc) in enumerate(menu_cats):
            cat, _ = MenuCategory.objects.update_or_create(
                vendor=vendor_profile, name=name, defaults={'description': desc, 'order': order, 'is_active': True}
            )
            menu_cat_objs[name] = cat
        self.stdout.write(self.style.SUCCESS(f'Created {len(menu_cats)} menu categories'))

        # 6. Menu items for each category
        menu_items_data = {
            'Fresh Produce': [
                ('Organic Bananas (1kg)', 'Ripe yellow bananas', 2.99),
                ('Red Apples (1kg)', 'Crisp and sweet Gala apples', 3.49),
                ('Tomatoes (500g)', 'Vine-ripened tomatoes', 1.99),
                ('Spinach Bunch', 'Fresh organic spinach', 1.49),
                ('Avocados (2pk)', 'Ripe ready-to-eat avocados', 2.49),
            ],
            'Dairy & Eggs': [
                ('Fresh Milk 2L', 'Pasteurised whole milk', 3.29),
                ('Free Range Eggs (6pk)', 'Farm-fresh brown eggs', 2.79),
                ('Greek Yogurt 500g', 'Plain full-fat yogurt', 4.49),
                ('Cheddar Cheese 250g', 'Aged cheddar block', 3.99),
                ('Butter 500g', 'Salted butter block', 2.99),
            ],
            'Meat & Poultry': [
                ('Chicken Breast (500g)', 'Boneless skinless breast', 5.99),
                ('Beef Mince (500g)', 'Premium lean mince', 4.99),
                ('Pork Chops (2pk)', 'Thick-cut bone-in chops', 6.49),
                ('Bacon Rashers (250g)', 'Smoked back bacon', 3.99),
                ('Whole Chicken (1.5kg)', 'Free-range roasting chicken', 8.99),
            ],
            'Bakery': [
                ('White Bread Loaf', 'Soft sliced white bread', 1.79),
                ('Whole Wheat Bread', 'High-fibre brown bread', 2.19),
                ('Croissants (4pk)', 'Buttery flaky pastry', 3.49),
                ('Chocolate Muffin', 'Double chocolate chip', 2.49),
                ('French Baguette', 'Crusty artisan baguette', 2.99),
            ],
            'Beverages': [
                ('Orange Juice 1L', 'Freshly squeezed juice', 3.99),
                ('Spring Water 1.5L', 'Natural mineral water', 1.29),
                ('Coca Cola 2L', 'Classic cola drink', 2.49),
                ('Green Tea 20 bags', 'Japanese sencha green tea', 3.29),
                ('Smoothie Berry Blend', 'Mixed berry smoothie 330ml', 2.99),
            ],
            'Pantry Staples': [
                ('Basmati Rice 2kg', 'Aged long-grain rice', 5.49),
                ('Penne Pasta 500g', 'Italian durum wheat pasta', 1.99),
                ('Olive Oil 500ml', 'Extra virgin olive oil', 7.99),
                ('Mixed Spices 100g', 'BBQ seasoning blend', 2.49),
                ('Canned Tomatoes 400g', 'Chopped Italian tomatoes', 0.99),
            ],
            'Snacks': [
                ('Potato Chips 150g', 'Salted classic crisps', 1.99),
                ('Dark Chocolate 100g', '72% cocoa Belgian chocolate', 3.49),
                ('Mixed Nuts 200g', 'Roasted salted cashews & almonds', 4.49),
                ('Granola Bar (6pk)', 'Oats & honey bars', 3.99),
                ('Rice Crackers 120g', 'Wasabi-flavoured rice snacks', 2.29),
            ],
        }
        total_items = 0
        for cat_name, items in menu_items_data.items():
            cat = menu_cat_objs[cat_name]
            for i, (name, desc, price) in enumerate(items):
                mi, created = MenuItem.objects.update_or_create(
                    vendor=vendor_profile,
                    name=name,
                    defaults={
                        'category': cat,
                        'description': desc,
                        'price': Decimal(str(price)),
                        'is_available': True,
                        'is_popular': i < 2,
                        'preparation_time_mins': random.randint(5, 15),
                    },
                )
                if created and not mi.image:
                    mi.image.save(f'menu_{mi.id}.png', generate_menu_item_image(name), save=False)
                    mi.save()
                total_items += 1
        self.stdout.write(self.style.SUCCESS(f'Created {total_items} menu items'))

        # 7. Promotions
        promotions_data = [
            (
                'WELCOME10',
                'PERCENTAGE',
                Decimal('10.00'),
                Decimal('0'),
                Decimal('5.00'),
                100,
                timezone.now(),
                timezone.now() + timezone.timedelta(days=30),
            ),
            (
                'FREEDEL5',
                'FIXED',
                Decimal('5.00'),
                Decimal('20.00'),
                None,
                50,
                timezone.now(),
                timezone.now() + timezone.timedelta(days=14),
            ),
            (
                'FRESH20',
                'PERCENTAGE',
                Decimal('20.00'),
                Decimal('15.00'),
                Decimal('8.00'),
                200,
                timezone.now(),
                timezone.now() + timezone.timedelta(days=60),
            ),
        ]
        for code, dtype, dval, min_order, max_disc, limit, vfrom, vuntil in promotions_data:
            Promotion.objects.update_or_create(
                vendor=vendor_profile,
                code=code,
                defaults={
                    'discount_type': dtype,
                    'discount_value': dval,
                    'min_order_amount': min_order,
                    'max_discount_amount': max_disc,
                    'usage_limit': limit,
                    'valid_from': vfrom,
                    'valid_until': vuntil,
                    'is_active': True,
                },
            )
        self.stdout.write(self.style.SUCCESS('Created 3 promotions'))

        # 8. Demo customer for sample orders
        customer_email = 'demo-customer@zimdelivery.co.zw'
        customer_user, created = User.objects.get_or_create(
            email=customer_email,
            defaults={
                'username': 'demo_customer',
                'phone_number': '+263773456789',
                'user_type': 'CUSTOMER',
                'is_active': True,
            },
        )
        if created:
            customer_user.set_password('demo1234')
            customer_user.save()
            self.stdout.write(self.style.SUCCESS(f'Created customer user: {customer_email}'))

        CustomerProfile.objects.get_or_create(user=customer_user)

        # 9. Sample orders with various statuses
        menu_items = list(MenuItem.objects.filter(vendor=vendor_profile))
        if menu_items:
            sample_orders = [
                {'status': 'PENDING', 'payment_status': 'PENDING'},
                {'status': 'ACCEPTED', 'payment_status': 'PAID'},
                {'status': 'PREPARING', 'payment_status': 'PAID'},
                {'status': 'READY', 'payment_status': 'PAID'},
                {'status': 'DELIVERED', 'payment_status': 'PAID'},
                {'status': 'CANCELLED', 'payment_status': 'REFUNDED'},
            ]
            for i, order_data in enumerate(sample_orders):
                items_in_order = random.choices(menu_items, k=random.randint(1, 3))
                subtotal = sum(item.price for item in items_in_order)
                delivery_fee = Decimal('2.50')
                total = subtotal + delivery_fee

                order, created = Order.objects.get_or_create(
                    order_number=f'ZD-SEED-{i + 1:03d}',
                    defaults={
                        'customer': customer_user,
                        'vendor': vendor_user,
                        'status': order_data['status'],
                        'payment_status': order_data['payment_status'],
                        'payment_method': random.choice(['ECOCASH', 'CASH']),
                        'pickup_address': '123 Samora Machel Ave, Harare',
                        'delivery_address': '45 Jason Moyo Ave, Harare',
                        'subtotal': subtotal,
                        'delivery_fee': delivery_fee,
                        'service_fee': Decimal('0.50'),
                        'total_amount': total,
                        'estimated_distance_km': Decimal(f'{random.uniform(1.0, 8.0):.1f}'),
                        'delivery_instructions': random.choice(
                            ['Leave at reception', 'Call on arrival', 'Ring doorbell']
                        )
                        if random.random() > 0.5
                        else '',
                    },
                )
                if created:
                    for item in items_in_order:
                        OrderItem.objects.create(
                            order=order,
                            menu_item=item,
                            name=item.name,
                            price=item.price,
                            quantity=random.randint(1, 3),
                        )
            self.stdout.write(self.style.SUCCESS(f'Created {len(sample_orders)} sample orders'))

        self.stdout.write(self.style.SUCCESS('Seed complete!'))
