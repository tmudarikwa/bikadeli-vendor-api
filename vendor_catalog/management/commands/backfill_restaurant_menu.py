import os
from decimal import Decimal
from pathlib import Path

from bikadeli_common.settings import database_config_from_url
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils.dateparse import parse_datetime

from vendor_catalog.management.commands.backfill_vendor_catalog import (
    _database_identity,
)
from vendor_catalog.models import (
    RestaurantMenuCategory,
    RestaurantMenuItem,
    RestaurantMenuItemImage,
    RestaurantMenuItemOption,
)

CATEGORY_COLUMNS = ('id', 'vendor_id', 'name', 'description', 'order', 'is_active', 'created_at')
ITEM_COLUMNS = (
    'id',
    'vendor_id',
    'category_id',
    'name',
    'description',
    'price',
    'image',
    'is_available',
    'is_popular',
    'preparation_time_mins',
    'created_at',
    'updated_at',
)
OPTION_COLUMNS = ('id', 'menu_item_id', 'name', 'price_adjustment')
IMAGE_COLUMNS = ('id', 'menu_item_id', 'image', 'alt_text', 'sort_order')


class Command(BaseCommand):
    help = 'Dry-run or apply an ID-preserving, insert-only restaurant menu/category/option/image copy.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Insert rows and copy available media. Without this flag, no rows or files are written.',
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
            categories, items, options_rows, images = self._read_source()
            plans = (
                self._preflight(RestaurantMenuCategory, categories, CATEGORY_COLUMNS),
                self._preflight(RestaurantMenuItem, items, ITEM_COLUMNS),
                self._preflight(RestaurantMenuItemOption, options_rows, OPTION_COLUMNS),
                self._preflight(RestaurantMenuItemImage, images, IMAGE_COLUMNS),
            )
            missing_media = self._missing_media(items, images)

            if options['apply']:
                with transaction.atomic(using='default'):
                    for model, plan in zip(
                        (RestaurantMenuCategory, RestaurantMenuItem, RestaurantMenuItemOption, RestaurantMenuItemImage),
                        plans,
                    ):
                        self._insert(model, plan)
                self._copy_media(items, images)

            self.stdout.write(
                self.style.SUCCESS(
                    'Restaurant menu backfill {}: categories={} (insert {}), items={} (insert {}), '
                    'options={} (insert {}), images={} (insert {}), media_missing={}.'.format(
                        'applied' if options['apply'] else 'dry-run',
                        len(categories),
                        len(plans[0]),
                        len(items),
                        len(plans[1]),
                        len(options_rows),
                        len(plans[2]),
                        len(images),
                        len(plans[3]),
                        len(missing_media),
                    )
                )
            )
            if missing_media:
                self.stdout.write(
                    self.style.WARNING(
                        'Media files not copied (set SOURCE_MEDIA_ROOT and inspect these paths): '
                        + ', '.join(missing_media[:10])
                    )
                )
        finally:
            connections['source'].close()
            del connections['source']
            connections.databases.pop('source', None)

    def _read_source(self):
        connection = connections['source']
        try:
            with transaction.atomic(using='source'), connection.cursor() as cursor:
                if connection.vendor == 'postgresql':
                    cursor.execute('SET TRANSACTION READ ONLY')
                elif connection.vendor == 'sqlite':
                    cursor.execute('PRAGMA query_only = ON')
                categories = self._select_rows(cursor, 'vendors_menucategory', CATEGORY_COLUMNS)
                items = self._select_rows(cursor, 'vendors_menuitem', ITEM_COLUMNS)
                options_rows = self._select_rows(cursor, 'vendors_menuitemoption', OPTION_COLUMNS)
                images = self._select_rows(cursor, 'vendors_menuitemimage', IMAGE_COLUMNS)

            category_ids = {row['id'] for row in categories}
            item_ids = {row['id'] for row in items}
            invalid_categories = [
                row['id']
                for row in items
                if row['category_id'] is not None and row['category_id'] not in category_ids
            ]
            invalid_options = [row['id'] for row in options_rows if row['menu_item_id'] not in item_ids]
            invalid_images = [row['id'] for row in images if row['menu_item_id'] not in item_ids]
            if invalid_categories:
                raise CommandError(f'Menu items reference missing source categories: {invalid_categories[:10]}')
            if invalid_options:
                raise CommandError(f'Options reference missing source menu items: {invalid_options[:10]}')
            if invalid_images:
                raise CommandError(f'Menu images reference missing source menu items: {invalid_images[:10]}')

            for row in categories:
                row['is_active'] = bool(row['is_active'])
                row['order'] = int(row['order'])
                row['description'] = row['description'] or ''
                row['created_at'] = self._datetime(row['created_at'])
            for row in items:
                row['price'] = Decimal(str(row['price']))
                row['is_available'] = bool(row['is_available'])
                row['is_popular'] = bool(row['is_popular'])
                row['preparation_time_mins'] = int(row['preparation_time_mins'])
                row['created_at'] = self._datetime(row['created_at'])
                row['updated_at'] = self._datetime(row['updated_at'])
                row['description'] = row['description'] or ''
            for row in options_rows:
                row['price_adjustment'] = Decimal(str(row['price_adjustment']))
            for row in images:
                row['alt_text'] = row['alt_text'] or ''
                row['sort_order'] = int(row['sort_order'])
            return categories, items, options_rows, images
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                'Could not read expected vendors_menucategory, vendors_menuitem, '
                f'vendors_menuitemoption, and vendors_menuitemimage tables; verify source schema/read access '
                f'({type(exc).__name__}: {exc}).'
            ) from exc

    @staticmethod
    def _datetime(value):
        parsed = parse_datetime(value) if isinstance(value, str) else value
        if parsed is None:
            raise CommandError('A source menu row has an invalid timestamp.')
        return parsed

    @staticmethod
    def _select_rows(cursor, table, columns):
        connection = cursor.db
        sql = 'SELECT {} FROM {}'.format(
            ', '.join(connection.ops.quote_name(column) for column in columns),
            connection.ops.quote_name(table),
        )
        cursor.execute(sql)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _preflight(self, model, rows, fields):
        expected = {row['id']: row for row in rows}
        conflicts = []
        ids = list(expected)
        manager = model.objects.using('default')
        for start in range(0, len(ids), 500):
            existing = manager.filter(pk__in=ids[start : start + 500]).values(*fields)
            for current in existing:
                source_row = expected[current['id']]
                if any(current[field] != source_row[field] for field in fields if field != 'id'):
                    conflicts.append(current['id'])
        known_ids = set(manager.filter(pk__in=ids).values_list('id', flat=True))
        missing = [row for row in rows if row['id'] not in known_ids]
        if conflicts:
            raise CommandError(f'{model.__name__} destination IDs conflict with source data: {conflicts[:10]}')
        return missing

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

    def _missing_media(self, items, images):
        references = [row['image'] for row in items + images if row['image']]
        return sorted({name for name in references if self._source_media_path(name) is None})

    def _copy_media(self, items, images):
        for row in items + images:
            name = row['image']
            if not name:
                continue
            source = self._source_media_path(name)
            if source is None or default_storage.exists(name):
                continue
            with source.open('rb') as media:
                saved_name = default_storage.save(name, File(media, name=Path(name).name))
            if saved_name != name:
                raise CommandError(f'Destination media key collision while copying {name}.')

    @staticmethod
    def _insert(model, rows):
        if rows:
            model.objects.using('default').bulk_create([model(**row) for row in rows], batch_size=500)
            if connections['default'].vendor == 'postgresql':
                sql = connections['default'].ops.sequence_reset_sql(no_style(), [model])
                with connections['default'].cursor() as cursor:
                    for statement in sql:
                        cursor.execute(statement)
