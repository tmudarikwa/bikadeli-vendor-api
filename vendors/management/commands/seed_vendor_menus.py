import random
from decimal import Decimal

from django.core.management.base import BaseCommand

from vendors.models import MenuCategory, MenuItem, VendorProfile


class Command(BaseCommand):
    help = 'Seeds menu categories and items for vendors 3, 4, and 5'

    def handle(self, *args, **kwargs):  # noqa: ARG002 - framework signature must stay fixed
        menus = {
            3: {
                'name': 'Madarenas Pizza',
                'categories': [
                    (
                        'Starters',
                        'Appetizers and small plates',
                        [
                            ('Garlic Bread', 'Toasted bread with garlic butter', 3.99),
                            ('Mozzarella Sticks', 'Golden fried cheese sticks', 4.49),
                            ('Chicken Wings (6pc)', 'Spicy buffalo wings', 5.99),
                        ],
                    ),
                    (
                        'Pizzas',
                        'Wood-fired pizzas',
                        [
                            ('Margherita', 'Tomato sauce, mozzarella, basil', 8.99),
                            ('Pepperoni', 'Classic pepperoni pizza', 9.99),
                            ('BBQ Chicken', 'BBQ sauce, chicken, red onion', 10.49),
                            ('Veggie Supreme', 'Mushrooms, peppers, olives', 9.49),
                        ],
                    ),
                    (
                        'Pasta',
                        'Italian pasta dishes',
                        [
                            ('Spaghetti Bolognese', 'Classic meat sauce', 7.99),
                            ('Penne Arrabbiata', 'Spicy tomato sauce', 6.99),
                        ],
                    ),
                    (
                        'Beverages',
                        'Drinks',
                        [
                            ('Coca Cola', '330ml can', 1.49),
                            ('Sparkling Water', '500ml bottle', 1.29),
                            ('Italian Lemonade', 'Freshly made', 2.49),
                        ],
                    ),
                ],
            },
            4: {
                'name': 'Spicy Chicken Inn',
                'categories': [
                    (
                        'Chicken Buckets',
                        'Fried chicken meals',
                        [
                            ('3pc Chicken Bucket', 'Three pieces with fries', 7.99),
                            ('6pc Chicken Bucket', 'Six pieces with coleslaw', 12.99),
                            ('Family Bucket (10pc)', 'Ten pieces with sides', 19.99),
                        ],
                    ),
                    (
                        'Burgers & Wraps',
                        'Grilled and crispy burgers',
                        [
                            ('Classic Chicken Burger', 'Crispy chicken fillet burger', 5.99),
                            ('Spicy Wrap', 'Grilled chicken with hot sauce', 5.49),
                            ('Zinger Burger', 'Extra crispy double fillet', 6.99),
                        ],
                    ),
                    (
                        'Sides',
                        'Side dishes',
                        [
                            ('French Fries (M)', 'Seasoned fries', 2.49),
                            ('Coleslaw', 'Creamy coleslaw', 1.99),
                            ('Onion Rings', 'Golden onion rings', 2.99),
                        ],
                    ),
                    (
                        'Beverages',
                        'Drinks',
                        [
                            ('Pepsi', '330ml can', 1.49),
                            ('Lemon Iced Tea', 'Refreshing iced tea', 1.99),
                        ],
                    ),
                ],
            },
            5: {
                'name': 'FreshMart Groceries',
                'categories': [
                    (
                        'Fresh Produce',
                        'Fruits and vegetables',
                        [
                            ('Bananas (1kg)', 'Ripe bananas', 1.99),
                            ('Tomatoes (1kg)', 'Fresh tomatoes', 2.49),
                            ('Onions (1kg)', 'Brown onions', 1.49),
                        ],
                    ),
                    (
                        'Pantry Staples',
                        'Essentials',
                        [
                            ('White Rice 2kg', 'Long grain rice', 4.99),
                            ('Cooking Oil 1L', 'Vegetable oil', 3.49),
                            ('Sugar 1kg', 'White granulated sugar', 2.29),
                        ],
                    ),
                    (
                        'Dairy & Eggs',
                        'Dairy products',
                        [
                            ('Fresh Milk 1L', 'Pasteurised milk', 2.49),
                            ('Eggs (12pk)', 'Free range eggs', 4.99),
                            ('Butter 250g', 'Salted butter', 2.99),
                        ],
                    ),
                ],
            },
        }

        total = 0
        for vendor_id, data in menus.items():
            try:
                vendor = VendorProfile.objects.get(id=vendor_id)
            except VendorProfile.DoesNotExist:
                self.stdout.write(self.style.WARNING(f'Vendor {vendor_id} not found, skipping'))
                continue

            if not vendor.business_name:
                vendor.business_name = data['name']
                vendor.save()
                self.stdout.write(self.style.SUCCESS(f'Updated vendor {vendor_id} name: {data["name"]}'))

            for order, (cat_name, cat_desc, items) in enumerate(data['categories']):
                cat, _ = MenuCategory.objects.update_or_create(
                    vendor=vendor, name=cat_name, defaults={'description': cat_desc, 'order': order, 'is_active': True}
                )
                for i, (name, desc, price) in enumerate(items):
                    MenuItem.objects.update_or_create(
                        vendor=vendor,
                        name=name,
                        defaults={
                            'category': cat,
                            'description': desc,
                            'price': Decimal(str(price)),
                            'is_available': True,
                            'is_popular': i < 2,
                            'preparation_time_mins': random.randint(10, 25),
                        },
                    )
                    total += 1

            self.stdout.write(self.style.SUCCESS(f'Vendor {vendor_id} ({data["name"]}): seeded'))

        self.stdout.write(self.style.SUCCESS(f'Total menu items created/updated: {total}'))
