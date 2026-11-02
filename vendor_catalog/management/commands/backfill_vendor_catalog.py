import hashlib
import json
import os
from base64 import urlsafe_b64encode
from datetime import date, datetime, time
from datetime import timezone as datetime_timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bikadeli_common.settings import database_config_from_url
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_time
from django.utils.text import slugify

from vendor_catalog.models import CatalogCategory, Product, ProductImage, VendorProfile

PROFILE_FIELDS = (
    'id',
    'owner_user_id',
    'business_name',
    'category',
    'description',
    'business_phone',
    'business_email',
    'business_address',
    'is_open',
    'is_active',
    'is_verified',
    'opening_time',
    'closing_time',
    'preparation_time_mins',
    'minimum_order',
    'delivery_fee',
    'pickup_available',
    'created_at',
    'updated_at',
)
CATEGORY_FIELDS = ('id', 'name', 'slug', 'is_active', 'icon', 'sort_order', 'parent_id', 'vendor_id')
PRODUCT_FIELDS = (
    'id',
    'vendor_id',
    'category_id',
    'name',
    'description',
    'sku',
    'price',
    'stock',
    'is_available',
    'barcode',
    'product_type',
    'quantity_on_hand',
    'low_stock_threshold',
    'is_inventory_tracked',
    'dietary_tags',
    'rating',
    'is_discounted',
    'is_popular',
    'compare_at_price',
    'weight_kg',
    'dimensions',
    'brand',
    'legacy_menu_item_id',
    'created_at',
    'updated_at',
)
PRODUCT_IMAGE_FIELDS = ('id', 'product_id', 'image', 'alt_text', 'sort_order')


def _database_identity(database_url):
    parsed = urlsplit(database_url.strip())
    if parsed.scheme == 'sqlite':
        path = unquote(parsed.path)
        if path in {'', '/:memory:'}:
            path = ':memory:'
        if os.name == 'nt' and len(path) > 3 and path[0] == '/' and path[2] == ':':
            path = path[1:]
        return ('sqlite', os.path.normcase(os.path.realpath(path)) if path != ':memory:' else path)
    scheme = parsed.scheme.replace('+psycopg', '')
    if scheme in {'postgres', 'postgresql'}:
        scheme = 'postgresql'
    return (
        scheme,
        (parsed.hostname or '').lower(),
        parsed.port or 5432,
        unquote(parsed.path),
    )


def _source_phone_fernet():
    secret = os.getenv('SOURCE_FIELDS_ENCRYPTION_KEY', '')
    salt = os.getenv('SOURCE_FIELDS_ENCRYPTION_SALT', '')
    if not secret or not salt:
        raise CommandError(
            'Encrypted source business phones require SOURCE_FIELDS_ENCRYPTION_KEY and '
            'SOURCE_FIELDS_ENCRYPTION_SALT.'
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt.encode(),
        iterations=480000,
    )
    return Fernet(urlsafe_b64encode(kdf.derive(secret.encode())))


def _decrypt_source_phone(value, fernet_cache):
    if not value or not value.startswith('gAAAAA'):
        return value or ''
    if fernet_cache[0] is None:
        fernet_cache[0] = _source_phone_fernet()
    try:
        return fernet_cache[0].decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise CommandError(
            'A source business phone could not be decrypted; verify source encryption credentials.'
        ) from exc


def _canonical(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _checksum(rows, fields):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item['id']):
        serialized = {field: _canonical(row.get(field)) for field in fields}
        digest.update(json.dumps(serialized, sort_keys=True, separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


class Command(BaseCommand):
    help = 'Dry-run or apply a repeatable vendor profile/category/product/image copy from the monolith.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Insert missing rows into the vendor database. Without this flag, no rows are written.',
        )

    def handle(self, *args, **options):
        source_url = os.getenv('SOURCE_DATABASE_URL', '').strip()
        destination_url = os.getenv('DATABASE_URL', '').strip()
        if not source_url or not destination_url:
            raise CommandError('Set both SOURCE_DATABASE_URL and DATABASE_URL explicitly.')
        if _database_identity(source_url) == _database_identity(destination_url):
            raise CommandError('Source and destination must be different databases.')

        source_config = connections.databases['default'].copy()
        source_config.update(database_config_from_url(source_url))
        if source_config['ENGINE'] == 'django.db.backends.sqlite3':
            source_config['OPTIONS'] = {}
        connections.databases['source'] = source_config
        try:
            source = self._read_source()
            expected = self._project_source(source)
            self._validate_references(expected)
            plans = self._preflight(expected)
            missing_media = self._missing_product_media(expected['product_images'])

            if options['apply']:
                if missing_media:
                    raise CommandError(
                        'Product image files are missing from destination storage and SOURCE_MEDIA_ROOT; '
                        f'first missing keys: {missing_media[:10]}.'
                    )
                self._copy_product_image_media(expected['product_images'])
                with transaction.atomic(using='default'):
                    plans = self._preflight(expected)
                    self._insert_missing(plans)
                    actual = self._read_destination()
                    self._verify_destination(expected, actual)
            else:
                actual = self._read_destination()

            self._report(expected, actual, plans, options['apply'], missing_media)
        finally:
            connections['source'].close()
            del connections['source']
            connections.databases.pop('source', None)

    def _query_rows(self, cursor, table, columns):
        connection = cursor.db
        sql = 'SELECT {} FROM {}'.format(
            ', '.join(connection.ops.quote_name(column) for column in columns),
            connection.ops.quote_name(table),
        )
        cursor.execute(sql)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _read_source(self):
        source_connection = connections['source']
        try:
            with transaction.atomic(using='source'):
                with source_connection.cursor() as cursor:
                    if source_connection.vendor == 'postgresql':
                        cursor.execute('SET TRANSACTION READ ONLY')
                    elif source_connection.vendor == 'sqlite':
                        cursor.execute('PRAGMA query_only = ON')
                    source = {
                        'profiles': self._query_rows(
                            cursor,
                            'vendors_vendorprofile',
                            [
                                'id',
                                'user_id',
                                'business_name',
                                'category',
                                'description',
                                'business_phone',
                                'business_email',
                                'business_address',
                                'is_open',
                                'is_active',
                                'is_verified',
                                'opening_time',
                                'closing_time',
                                'preparation_time_mins',
                                'minimum_order',
                                'delivery_fee',
                                'pickup_available',
                                'created_at',
                                'updated_at',
                            ],
                        ),
                        'categories': self._query_rows(
                            cursor,
                            'vendors_category',
                            ['id', 'name', 'icon', 'parent_id', 'vendor_id', 'sort_order'],
                        ),
                        'products': self._query_rows(
                            cursor,
                            'vendors_product',
                            [
                                'id',
                                'vendor_id',
                                'category_id',
                                'name',
                                'description',
                                'price',
                                'stock',
                                'is_available',
                                'barcode',
                                'product_type',
                                'quantity_on_hand',
                                'low_stock_threshold',
                                'is_inventory_tracked',
                                'dietary_tags',
                                'rating',
                                'is_discounted',
                                'is_popular',
                                'compare_at_price',
                                'weight_kg',
                                'dimensions',
                                'brand',
                                'menu_item_id',
                                'created_at',
                                'updated_at',
                            ],
                        ),
                        'product_images': self._query_rows(
                            cursor,
                            'vendors_productimage',
                            ['id', 'product_id', 'image', 'alt_text', 'sort_order'],
                        ),
                    }
            return source
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(
                'Could not read expected vendor source tables; verify the source schema and database access '
                f'({type(exc).__name__}: {exc}).'
            ) from exc

    def _project_source(self, source):
        phone_fernet = [None]
        profiles = []
        for row in source['profiles']:
            profiles.append(
                {
                    'id': row['id'],
                    'owner_user_id': row['user_id'],
                    'business_name': row['business_name'],
                    'category': row['category'],
                    'description': row['description'],
                    'business_phone': _decrypt_source_phone(row['business_phone'], phone_fernet),
                    'business_email': row['business_email'],
                    'business_address': row['business_address'],
                    'is_open': row['is_open'],
                    'is_active': row['is_active'],
                    'is_verified': row['is_verified'],
                    'opening_time': row['opening_time'],
                    'closing_time': row['closing_time'],
                    'preparation_time_mins': row['preparation_time_mins'],
                    'minimum_order': row['minimum_order'],
                    'delivery_fee': row['delivery_fee'],
                    'pickup_available': row['pickup_available'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                }
            )

        categories = []
        for row in source['categories']:
            suffix = f'-{row["id"]}'
            max_base_length = 120 - len(suffix)
            category_slug = slugify(row['name'])[:max_base_length].strip('-') or 'category'
            categories.append(
                {
                    'id': row['id'],
                    'name': row['name'],
                    'slug': f'{category_slug}{suffix}',
                    'is_active': True,
                    'icon': row['icon'] or '',
                    'sort_order': row['sort_order'],
                    'parent_id': row['parent_id'],
                    'vendor_id': row['vendor_id'],
                }
            )

        products = []
        for row in source['products']:
            tags = row['dietary_tags']
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except json.JSONDecodeError as exc:
                    raise CommandError(f'Product {row["id"]} has invalid dietary_tags JSON.') from exc
            if not isinstance(tags, list):
                raise CommandError(f'Product {row["id"]} dietary_tags must be a JSON array.')
            products.append(
                {
                    'id': row['id'],
                    'vendor_id': row['vendor_id'],
                    'category_id': row['category_id'],
                    'name': row['name'],
                    'description': row['description'],
                    'sku': '',
                    'price': row['price'],
                    'stock': row['stock'],
                    'is_available': row['is_available'],
                    'barcode': row['barcode'] or '',
                    'product_type': row['product_type'],
                    'quantity_on_hand': row['quantity_on_hand'],
                    'low_stock_threshold': row['low_stock_threshold'],
                    'is_inventory_tracked': row['is_inventory_tracked'],
                    'dietary_tags': tags,
                    'rating': row['rating'],
                    'is_discounted': row['is_discounted'],
                    'is_popular': row['is_popular'],
                    'compare_at_price': row['compare_at_price'],
                    'weight_kg': row['weight_kg'],
                    'dimensions': row['dimensions'] or '',
                    'brand': row['brand'] or '',
                    'legacy_menu_item_id': row['menu_item_id'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                }
            )
        product_images = [
            {
                'id': row['id'],
                'product_id': row['product_id'],
                'image': row['image'] or '',
                'alt_text': row['alt_text'] or '',
                'sort_order': row['sort_order'],
            }
            for row in source['product_images']
        ]
        for row in profiles:
            row['is_open'] = bool(row['is_open'])
            row['is_active'] = bool(row['is_active'])
            row['is_verified'] = bool(row['is_verified'])
            row['pickup_available'] = bool(row['pickup_available'])
            for field in ('opening_time', 'closing_time'):
                if isinstance(row[field], str):
                    row[field] = parse_time(row[field])
            for field in ('created_at', 'updated_at'):
                if isinstance(row[field], str):
                    row[field] = parse_datetime(row[field])
                if row[field] is not None and timezone.is_naive(row[field]):
                    row[field] = timezone.make_aware(row[field], datetime_timezone.utc)
        for row in products:
            row['is_available'] = bool(row['is_available'])
            row['is_inventory_tracked'] = bool(row['is_inventory_tracked'])
            row['is_discounted'] = bool(row['is_discounted'])
            row['is_popular'] = bool(row['is_popular'])
            for field in ('created_at', 'updated_at'):
                if isinstance(row[field], str):
                    row[field] = parse_datetime(row[field])
                if row[field] is not None and timezone.is_naive(row[field]):
                    row[field] = timezone.make_aware(row[field], datetime_timezone.utc)
        return {
            'profiles': profiles,
            'categories': categories,
            'products': products,
            'product_images': product_images,
        }

    def _validate_references(self, expected):
        profile_ids = {row['id'] for row in expected['profiles']}
        category_by_id = {row['id']: row for row in expected['categories']}
        for profile in expected['profiles']:
            if not profile['id'] or not profile['owner_user_id']:
                raise CommandError('Vendor profiles must have valid source and owner IDs.')
        for category in expected['categories']:
            if category['parent_id'] is not None and category['parent_id'] not in category_by_id:
                raise CommandError(f'Category {category["id"]} references missing parent {category["parent_id"]}.')
            if category['vendor_id'] is not None and category['vendor_id'] not in profile_ids:
                raise CommandError(f'Category {category["id"]} references missing vendor {category["vendor_id"]}.')
        for product in expected['products']:
            if product['vendor_id'] not in profile_ids:
                raise CommandError(f'Product {product["id"]} references missing vendor {product["vendor_id"]}.')
            if product['category_id'] is not None and product['category_id'] not in category_by_id:
                raise CommandError(
                    f'Product {product["id"]} references missing category {product["category_id"]}.'
                )
            if product['stock'] < 0:
                raise CommandError(f'Product {product["id"]} has negative stock, unsupported by the target schema.')
        product_ids = {row['id'] for row in expected['products']}
        image_ids = set()
        for image in expected['product_images']:
            if not image['id'] or image['id'] in image_ids:
                raise CommandError('Product images must have unique, nonzero IDs.')
            image_ids.add(image['id'])
            if image['product_id'] not in product_ids:
                raise CommandError(
                    f'Product image {image["id"]} references missing product {image["product_id"]}.'
                )
        self._category_insert_order(expected['categories'])

    @staticmethod
    def _source_media_path(name):
        root = os.getenv('SOURCE_MEDIA_ROOT', '').strip()
        if not root:
            return None
        root_path = Path(root).resolve()
        media_path = (root_path / name).resolve()
        if not media_path.is_relative_to(root_path):
            raise CommandError(f'Unsafe source media path: {name}')
        return media_path if media_path.is_file() else None

    def _missing_product_media(self, images):
        references = {row['image'] for row in images if row['image']}
        return sorted(
            name
            for name in references
            if not default_storage.exists(name) and self._source_media_path(name) is None
        )

    def _copy_product_image_media(self, images):
        for row in images:
            name = row['image']
            if not name or default_storage.exists(name):
                continue
            source = self._source_media_path(name)
            if source is None:
                raise CommandError(f'Product image file is missing from SOURCE_MEDIA_ROOT: {name}')
            with source.open('rb') as media:
                saved_name = default_storage.save(name, File(media, name=source.name))
            if saved_name != name:
                raise CommandError(f'Destination media key collision while copying {name}.')

    def _read_destination(self):
        return {
            'profiles': [
                {field: getattr(row, field) for field in PROFILE_FIELDS}
                for row in VendorProfile.objects.order_by('id')
            ],
            'categories': [
                {
                    'id': row.id,
                    'name': row.name,
                    'slug': row.slug,
                    'is_active': row.is_active,
                    'icon': row.icon,
                    'sort_order': row.sort_order,
                    'parent_id': row.parent_id,
                    'vendor_id': row.vendor_id,
                }
                for row in CatalogCategory.objects.order_by('id')
            ],
            'products': [
                {
                    field: getattr(row, field)
                    for field in PRODUCT_FIELDS
                }
                for row in Product.objects.order_by('id')
            ],
            'product_images': [
                {
                    'id': row.id,
                    'product_id': row.product_id,
                    'image': row.image.name,
                    'alt_text': row.alt_text,
                    'sort_order': row.sort_order,
                }
                for row in ProductImage.objects.order_by('id')
            ],
        }

    def _preflight(self, expected):
        actual = self._read_destination()
        models = {
            'profiles': (VendorProfile, PROFILE_FIELDS),
            'categories': (CatalogCategory, CATEGORY_FIELDS),
            'products': (Product, PRODUCT_FIELDS),
            'product_images': (ProductImage, PRODUCT_IMAGE_FIELDS),
        }
        plans = {}
        for table, (model, fields) in models.items():
            actual_by_id = {row['id']: row for row in actual[table]}
            missing = []
            for source_row in expected[table]:
                destination_row = actual_by_id.get(source_row['id'])
                if destination_row is None:
                    missing.append(source_row)
                    continue
                conflicts = [
                    field
                    for field in fields
                    if _canonical(destination_row.get(field)) != _canonical(source_row.get(field))
                ]
                if conflicts:
                    label = {
                        'profiles': 'Profile',
                        'categories': 'Category',
                        'products': 'Product',
                        'product_images': 'Product image',
                    }[table]
                    raise CommandError(f'{label} ID {source_row["id"]} conflicts on: {", ".join(conflicts)}.')
            if table == 'profiles':
                owner_to_profile = {
                    row['owner_user_id']: row['id']
                    for row in actual[table]
                }
                for row in missing:
                    existing_id = owner_to_profile.get(row['owner_user_id'])
                    if existing_id is not None:
                        raise CommandError(
                            f'Owner user ID {row["owner_user_id"]} already belongs to destination profile '
                            f'{existing_id}, not source profile {row["id"]}.'
                        )
            if table == 'product_images':
                products = {row['id'] for row in actual['products']} | {
                    row['id'] for row in plans.get('products', [])
                }
                for row in missing:
                    if row['product_id'] not in products:
                        raise CommandError(
                            f'Product image {row["id"]} references missing destination product {row["product_id"]}.'
                        )
            plans[table] = missing
        return plans

    def _insert_missing(self, plans):
        for row in plans['profiles']:
            VendorProfile.objects.create(**row)
        for row in self._category_insert_order(plans['categories']):
            CatalogCategory.objects.create(**row)
        for row in plans['products']:
            Product.objects.create(**row)
        for row in plans['product_images']:
            ProductImage.objects.create(**row)
        for statement in connections['default'].ops.sequence_reset_sql(
            no_style(),
            [VendorProfile, CatalogCategory, Product, ProductImage],
        ):
            with connections['default'].cursor() as cursor:
                cursor.execute(statement)

    def _category_insert_order(self, categories):
        category_rows = {row['id']: row for row in categories}
        ordered = []
        visited = set()
        active = set()

        def visit(category_id):
            if category_id in active:
                raise CommandError(f'Category hierarchy contains a cycle at ID {category_id}.')
            if category_id in visited:
                return
            active.add(category_id)
            parent_id = category_rows[category_id]['parent_id']
            if parent_id in category_rows:
                visit(parent_id)
            active.remove(category_id)
            visited.add(category_id)
            ordered.append(category_rows[category_id])

        for category_id in category_rows:
            visit(category_id)
        return ordered

    def _verify_destination(self, expected, actual):
        for table, fields in (
            ('profiles', PROFILE_FIELDS),
            ('categories', CATEGORY_FIELDS),
            ('products', PRODUCT_FIELDS),
            ('product_images', PRODUCT_IMAGE_FIELDS),
        ):
            actual_by_id = {row['id']: row for row in actual[table]}
            for source_row in expected[table]:
                destination_row = actual_by_id.get(source_row['id'])
                if destination_row is None or any(
                    _canonical(destination_row.get(field)) != _canonical(source_row.get(field))
                    for field in fields
                ):
                    raise CommandError(f'Post-write reconciliation failed for {table} ID {source_row["id"]}.')

    def _report(self, expected, actual, plans, applied, missing_media):
        self.stdout.write('Vendor catalog backfill: {}'.format('APPLIED' if applied else 'DRY RUN'))
        for table, fields in (
            ('profiles', PROFILE_FIELDS),
            ('categories', CATEGORY_FIELDS),
            ('products', PRODUCT_FIELDS),
            ('product_images', PRODUCT_IMAGE_FIELDS),
        ):
            existing_ids = {row['id'] for row in actual[table]}
            matched = [row for row in expected[table] if row['id'] in existing_ids]
            self.stdout.write(
                '{}: source={} already_matching={} insertable={} source_checksum={} '
                'destination_matching_checksum={}'.format(
                    table,
                    len(expected[table]),
                    len(expected[table]) - len(plans[table]),
                    len(plans[table]),
                    _checksum(expected[table], fields),
                    _checksum(matched, fields),
                )
            )
        self.stdout.write(f'product_image_media_missing={len(missing_media)}')
        if not applied:
            self.stdout.write('No destination rows were written. Re-run with --apply after reviewing this report.')
