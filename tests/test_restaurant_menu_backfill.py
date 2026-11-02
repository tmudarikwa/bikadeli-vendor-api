import os
import shutil
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from uuid import uuid4

from cryptography.fernet import Fernet

SERVICE_ROOT = Path(__file__).resolve().parents[1]
MANAGE_PY = SERVICE_ROOT / 'manage.py'


class RestaurantMenuBackfillTests(TestCase):
    def setUp(self):
        suffix = uuid4().hex
        self.source_path = SERVICE_ROOT / 'tests' / f'_menu_source_{suffix}.sqlite3'
        self.destination_path = SERVICE_ROOT / 'tests' / f'_menu_destination_{suffix}.sqlite3'
        self.source_media_root = SERVICE_ROOT / 'tests' / f'_menu_media_source_{suffix}'
        self.destination_media_root = SERVICE_ROOT / 'tests' / f'_menu_media_destination_{suffix}'
        self._create_source()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                'DJANGO_DEBUG': '1',
                'DATABASE_URL': f'sqlite:///{self.destination_path}',
                'SOURCE_DATABASE_URL': f'sqlite:///{self.source_path}',
                'SOURCE_MEDIA_ROOT': str(self.source_media_root),
                'MEDIA_ROOT': str(self.destination_media_root),
                'VENDOR_FIELDS_ENCRYPTION_KEY': Fernet.generate_key().decode(),
            }
        )
        self._run_manage('migrate', '--noinput')

    def tearDown(self):
        for path in (self.source_path, self.destination_path):
            for suffix in ('', '-journal', '-shm', '-wal'):
                candidate = Path(f'{path}{suffix}')
                if candidate.exists():
                    candidate.unlink()
        for path in (self.source_media_root, self.destination_media_root):
            if path.exists():
                shutil.rmtree(path)

    def _create_source(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.executescript(
                '''
                CREATE TABLE vendors_menucategory (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, name TEXT, description TEXT,
                    "order" INTEGER, is_active INTEGER, created_at TEXT
                );
                CREATE TABLE vendors_menuitem (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, category_id INTEGER, name TEXT,
                    description TEXT, price TEXT, image TEXT, is_available INTEGER, is_popular INTEGER,
                    preparation_time_mins INTEGER, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_menuitemoption (
                    id INTEGER PRIMARY KEY, menu_item_id INTEGER, name TEXT, price_adjustment TEXT
                );
                CREATE TABLE vendors_menuitemimage (
                    id INTEGER PRIMARY KEY, menu_item_id INTEGER, image TEXT, alt_text TEXT, sort_order INTEGER
                );
                INSERT INTO vendors_menucategory VALUES (
                    34, 71, 'Meals', 'Main dishes', 1, 1, '2025-02-02T04:05:06+00:00'
                );
                INSERT INTO vendors_menuitem VALUES (
                    501, 71, 34, 'Sadza and stew', 'Beef stew', '5.50', 'menu_items/stew.jpg',
                    1, 0, 15, '2025-02-03T04:05:06+00:00', '2025-02-04T04:05:06+00:00'
                );
                INSERT INTO vendors_menuitemoption VALUES (901, 501, 'Extra meat', '1.00');
                INSERT INTO vendors_menuitemimage VALUES (
                    902, 501, 'menu_items/2025/02/stew-detail.jpg', 'Stew close-up', 0
                );
                '''
            )
        finally:
            connection.close()
        for relative, content in (
            ('menu_items/stew.jpg', b'legacy-single-image'),
            ('menu_items/2025/02/stew-detail.jpg', b'legacy-additional-image'),
        ):
            source = self.source_media_root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)

    def _run_manage(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(MANAGE_PY), *arguments],
            cwd=SERVICE_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_backfill_dry_run_then_id_preserving_insert_only_apply(self):
        report = self._run_manage('backfill_restaurant_menu')
        self.assertIn(
            'dry-run: categories=1 (insert 1), items=1 (insert 1), options=1 (insert 1), '
            'images=1 (insert 1), media_missing=0',
            report,
        )
        connection = sqlite3.connect(self.destination_path)
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM vendor_catalog_restaurantmenuitem').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM vendor_catalog_restaurantmenucategory').fetchone()[0], 0)
        finally:
            connection.close()

        self._run_manage('backfill_restaurant_menu', '--apply')
        self._run_manage('backfill_restaurant_menu', '--apply')
        connection = sqlite3.connect(self.destination_path)
        try:
            item = connection.execute(
                'SELECT id, vendor_id, category_id, image, price FROM vendor_catalog_restaurantmenuitem'
            ).fetchone()
            option = connection.execute(
                'SELECT id, menu_item_id, name, price_adjustment '
                'FROM vendor_catalog_restaurantmenuitemoption'
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(item[:4], (501, 71, 34, 'menu_items/stew.jpg'))
        self.assertEqual(Decimal(str(item[4])), Decimal('5.50'))
        self.assertEqual(option[:3], (901, 501, 'Extra meat'))
        self.assertEqual(Decimal(str(option[3])), Decimal('1.00'))
        connection = sqlite3.connect(self.destination_path)
        try:
            category = connection.execute(
                'SELECT id, vendor_id, name, "order" FROM vendor_catalog_restaurantmenucategory'
            ).fetchone()
            image = connection.execute(
                'SELECT id, menu_item_id, image, alt_text, sort_order '
                'FROM vendor_catalog_restaurantmenuitemimage'
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(category, (34, 71, 'Meals', 1))
        self.assertEqual(image, (902, 501, 'menu_items/2025/02/stew-detail.jpg', 'Stew close-up', 0))
        self.assertEqual(
            (self.destination_media_root / 'menu_items/stew.jpg').read_bytes(),
            b'legacy-single-image',
        )
        self.assertEqual(
            (self.destination_media_root / 'menu_items/2025/02/stew-detail.jpg').read_bytes(),
            b'legacy-additional-image',
        )
