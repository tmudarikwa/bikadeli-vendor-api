"""
Canonical marketplace category taxonomy.

Single source of truth shared by:
  - vendors/migrations/0028_seed_marketplace_categories.py  (data migration)
  - core/management/commands/seed.py                         (demo seeder)
  - vendors/marketplace_views.py                             (API filter)

Do NOT duplicate these lists elsewhere — import from here.
"""

MARKETPLACE_CATEGORIES = {
    'Electronics': {
        'icon': 'phone_android',
        'subcategories': [],
    },
    'Fashion': {
        'icon': 'checkroom',
        'subcategories': [],
    },
    'Home & Garden': {
        'icon': 'home',
        'subcategories': [],
    },
    'Groceries': {
        'icon': 'shopping_cart',
        'subcategories': [
            'Fruits & Vegetables',
            'Butchery & Meat',
            'Fish & Seafood',
            'Dairy & Eggs',
            'Bakery',
            'Frozen Foods',
        ],
    },
    'Food': {
        'icon': 'restaurant',
        'subcategories': [
            'Restaurants',
            'Fast Food',
            'African Cuisine',
            'Asian Cuisine',
            'Italian & Pizza',
            'Chicken & Grill',
            'Sushi & Seafood',
            'Sandwiches & Wraps',
            'Breakfast & Brunch',
            'Desserts & Ice Cream',
        ],
    },
    'Drinks': {
        'icon': 'local_bar',
        'subcategories': [
            'Soft Drinks & Water',
            'Juices & Smoothies',
            'Coffee & Tea',
        ],
    },
    'Alcohol & Liquor': {
        'icon': 'liquor',
        'subcategories': ['Liquor Store'],
    },
    'Pharmacy': {
        'icon': 'local_pharmacy',
        'subcategories': [
            'Health & Wellness',
            'Personal Care',
            'Vitamins & Supplements',
            'Baby & Kids',
            'Cleaning & Household',
        ],
    },
    'Pets': {
        'icon': 'pets',
        'subcategories': ['Pet Supplies'],
    },
    'Flowers': {
        'icon': 'local_florist',
        'subcategories': ['Flowers & Gifts'],
    },
    'Parcel': {
        'icon': 'local_shipping',
        'subcategories': [],
    },
    'Other': {
        'icon': 'category',
        'subcategories': ['Stationery & Office'],
    },
}
