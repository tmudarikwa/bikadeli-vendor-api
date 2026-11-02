import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.test import SimpleTestCase

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
MANAGE_PY = Path(__file__).resolve().parents[1] / "manage.py"
SOURCE_SECRET = "legacy-source-encryption-secret"
SOURCE_SALT = "legacy-source-encryption-salt"
DESTINATION_KEY = Fernet.generate_key().decode()


def _sqlite_url(path):
    return f"sqlite:///{Path(path).as_posix()}"


def _legacy_phone(value):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SOURCE_SALT.encode(),
        iterations=480000,
    )
    fernet = Fernet(urlsafe_b64encode(kdf.derive(SOURCE_SECRET.encode())))
    return fernet.encrypt(value.encode()).decode()


class VendorCatalogBackfillTests(SimpleTestCase):
    def setUp(self):
        paths = []
        for _ in range(2):
            descriptor, path = tempfile.mkstemp(suffix=".sqlite3")
            os.close(descriptor)
            paths.append(path)
        self.source_path, self.destination_path = paths
        self.source_media_root = tempfile.TemporaryDirectory()
        self.destination_media_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._remove_databases)
        self.addCleanup(self.source_media_root.cleanup)
        self.addCleanup(self.destination_media_root.cleanup)
        self._create_source_database()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "DJANGO_DEBUG": "1",
                "DATABASE_URL": _sqlite_url(self.destination_path),
                "SOURCE_DATABASE_URL": _sqlite_url(self.source_path),
                "SOURCE_FIELDS_ENCRYPTION_KEY": SOURCE_SECRET,
                "SOURCE_FIELDS_ENCRYPTION_SALT": SOURCE_SALT,
                "VENDOR_FIELDS_ENCRYPTION_KEY": DESTINATION_KEY,
                "SOURCE_MEDIA_ROOT": self.source_media_root.name,
                "MEDIA_ROOT": self.destination_media_root.name,
            }
        )
        self._run_manage("migrate", "--noinput")

    def _remove_databases(self):
        for path in (self.source_path, self.destination_path):
            if os.path.exists(path):
                os.remove(path)

    def _create_source_database(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.executescript(
                """
                CREATE TABLE vendors_vendorprofile (
                    id INTEGER PRIMARY KEY, user_id INTEGER, business_name TEXT, category TEXT,
                    description TEXT, business_phone TEXT, business_email TEXT, business_address TEXT,
                    is_open INTEGER, is_active INTEGER, is_verified INTEGER, opening_time TEXT,
                    closing_time TEXT, preparation_time_mins INTEGER, minimum_order TEXT,
                    delivery_fee TEXT, pickup_available INTEGER, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_category (
                    id INTEGER PRIMARY KEY, name TEXT, icon TEXT, parent_id INTEGER, vendor_id INTEGER,
                    sort_order INTEGER
                );
                CREATE TABLE vendors_product (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, category_id INTEGER, name TEXT,
                    description TEXT, price TEXT, stock INTEGER, is_available INTEGER, barcode TEXT,
                    product_type TEXT, quantity_on_hand INTEGER, low_stock_threshold INTEGER,
                    is_inventory_tracked INTEGER, dietary_tags TEXT, rating TEXT, is_discounted INTEGER,
                    is_popular INTEGER, compare_at_price TEXT, weight_kg TEXT, dimensions TEXT, brand TEXT,
                    menu_item_id INTEGER, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_productimage (
                    id INTEGER PRIMARY KEY, product_id INTEGER, image TEXT, alt_text TEXT, sort_order INTEGER
                );
                """
            )
            connection.execute(
                """
                INSERT INTO vendors_vendorprofile VALUES
                (71, 9001, 'Bika Foods', 'GROCERY', 'Local produce', ?, 'owner@example.test',
                 '10 Main Street', 1, 1, 1, '08:00:00', '20:00:00', 15, '5.00', '0.00', 1,
                 '2025-02-03T04:05:06+00:00', '2025-02-04T04:05:06+00:00')
                """,
                (_legacy_phone("+263771234567"),),
            )
            connection.executemany(
                "INSERT INTO vendors_category VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (40, "Grocery", "basket", None, None, 1),
                    (41, "Grocery", "store", 40, 71, 2),
                ],
            )
            connection.execute(
                """
                INSERT INTO vendors_product VALUES
                (501, 71, 41, 'Mango', 'Ripe mango', '2.50', 12, 1, 'MNG-1', 'GROCERY',
                 12, 5, 1, ?, '4.50', 0, 1, '3.00', '0.25', '10x5x5 cm', 'Bika Farm', 700,
                 '2025-02-05T04:05:06+00:00', '2025-02-06T04:05:06+00:00')
                """,
                (json.dumps(["VEGAN"]),),
            )
            connection.execute(
                """
                INSERT INTO vendors_productimage VALUES
                (601, 501, 'products/2025/02/mango.jpg', 'Ripe mango', 0)
                """
            )
            connection.commit()
        finally:
            connection.close()
        source_image = (
            Path(self.source_media_root.name) / "products" / "2025" / "02" / "mango.jpg"
        )
        source_image.parent.mkdir(parents=True)
        source_image.write_bytes(b"legacy-image-bytes")

    def _run_manage(self, *arguments):
        return subprocess.run(
            [sys.executable, str(MANAGE_PY), *arguments],
            cwd=WORKSPACE_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_backfill(self, apply=False, success=True):
        result = self._run_manage(
            "backfill_vendor_catalog",
            *(("--apply",) if apply else ()),
        )
        if success and result.returncode:
            self.fail(f"Backfill command failed:\n{result.stdout}\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail(f"Backfill command unexpectedly succeeded:\n{result.stdout}")
        return result

    def _destination_rows(self, table):
        connection = sqlite3.connect(self.destination_path)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()

    def test_dry_run_is_read_only_and_apply_is_repeatable_and_reconciled(self):
        dry_run = self._run_backfill()
        self.assertIn("Vendor catalog backfill: DRY RUN", dry_run.stdout)
        self.assertIn(
            "profiles: source=1 already_matching=0 insertable=1", dry_run.stdout
        )
        self.assertIn(
            "categories: source=2 already_matching=0 insertable=2", dry_run.stdout
        )
        self.assertIn(
            "products: source=1 already_matching=0 insertable=1", dry_run.stdout
        )
        self.assertIn(
            "product_images: source=1 already_matching=0 insertable=1", dry_run.stdout
        )
        self.assertIn("product_image_media_missing=0", dry_run.stdout)
        self.assertIn("source_checksum=", dry_run.stdout)
        self.assertEqual(self._destination_rows("vendor_catalog_vendorprofile"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_catalogcategory"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_product"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_productimage"), 0)

        applied = self._run_backfill(apply=True)
        self.assertIn("Vendor catalog backfill: APPLIED", applied.stdout)
        repeated = self._run_backfill(apply=True)
        self.assertIn("already_matching=1 insertable=0", repeated.stdout)
        for line in repeated.stdout.splitlines():
            if line.startswith(
                ("profiles:", "categories:", "products:", "product_images:")
            ):
                source_checksum = line.split("source_checksum=")[1].split()[0]
                destination_checksum = line.split("destination_matching_checksum=")[1]
                self.assertEqual(source_checksum, destination_checksum)

        destination = sqlite3.connect(self.destination_path)
        try:
            profile = destination.execute(
                "SELECT id, owner_user_id, business_phone, created_at FROM vendor_catalog_vendorprofile"
            ).fetchone()
            self.assertEqual(profile[:2], (71, 9001))
            self.assertTrue(profile[2].startswith("gAAAAA"))
            self.assertEqual(profile[3], "2025-02-03 04:05:06")
            categories = destination.execute(
                "SELECT id, name, parent_id, vendor_id, slug FROM vendor_catalog_catalogcategory ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [row[:4] for row in categories],
                [(40, "Grocery", None, None), (41, "Grocery", 40, 71)],
            )
            self.assertNotEqual(categories[0][4], categories[1][4])
            product = destination.execute(
                "SELECT id, vendor_id, category_id, barcode, dietary_tags, legacy_menu_item_id "
                "FROM vendor_catalog_product"
            ).fetchone()
            self.assertEqual(product, (501, 71, 41, "MNG-1", '["VEGAN"]', 700))
            image = destination.execute(
                "SELECT id, product_id, image, alt_text, sort_order FROM vendor_catalog_productimage"
            ).fetchone()
            self.assertEqual(
                image, (601, 501, "products/2025/02/mango.jpg", "Ripe mango", 0)
            )
        finally:
            destination.close()
        copied_image = (
            Path(self.destination_media_root.name)
            / "products"
            / "2025"
            / "02"
            / "mango.jpg"
        )
        self.assertEqual(copied_image.read_bytes(), b"legacy-image-bytes")

    def test_missing_product_image_files_block_apply_before_database_writes(self):
        source_image = (
            Path(self.source_media_root.name) / "products" / "2025" / "02" / "mango.jpg"
        )
        source_image.unlink()

        dry_run = self._run_backfill()
        self.assertIn("product_image_media_missing=1", dry_run.stdout)
        failed = self._run_backfill(apply=True, success=False)
        self.assertIn("first missing keys", failed.stderr)
        self.assertEqual(self._destination_rows("vendor_catalog_vendorprofile"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_productimage"), 0)

    def test_conflicting_ids_or_owner_ownership_abort_without_partial_inserts(self):
        destination = sqlite3.connect(self.destination_path)
        try:
            destination.execute(
                """
                INSERT INTO vendor_catalog_vendorprofile
                (id, owner_user_id, business_name, category, description, business_phone, business_email,
                 business_address, is_open, is_active, is_verified, preparation_time_mins, minimum_order,
                 delivery_fee, pickup_available, created_at, updated_at)
                VALUES (99, 9001, 'Existing owner', 'GROCERY', '', '', '', '', 0, 1, 0, 15, 0, 0, 1,
                        '2025-01-01 00:00:00+00:00', '2025-01-01 00:00:00+00:00')
                """
            )
            destination.commit()
        finally:
            destination.close()

        result = self._run_backfill(apply=True, success=False)
        self.assertIn("already belongs to destination profile 99", result.stderr)
        self.assertEqual(self._destination_rows("vendor_catalog_catalogcategory"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_product"), 0)

        destination = sqlite3.connect(self.destination_path)
        try:
            destination.execute(
                "UPDATE vendor_catalog_vendorprofile SET business_name = 'Wrong' WHERE id = 99"
            )
            destination.execute(
                "DELETE FROM vendor_catalog_vendorprofile WHERE id = 99"
            )
            destination.execute(
                """
                INSERT INTO vendor_catalog_vendorprofile
                (id, owner_user_id, business_name, category, description, business_phone, business_email,
                 business_address, is_open, is_active, is_verified, preparation_time_mins, minimum_order,
                 delivery_fee, pickup_available, created_at, updated_at)
                VALUES (71, 9001, 'Wrong', 'GROCERY', '', '', '', '', 0, 1, 0, 15, 0, 0, 1,
                        '2025-02-03 04:05:06', '2025-02-04 04:05:06')
                """
            )
            destination.commit()
        finally:
            destination.close()

        result = self._run_backfill(apply=True, success=False)
        self.assertIn("Profile ID 71 conflicts", result.stderr)
        self.assertEqual(self._destination_rows("vendor_catalog_catalogcategory"), 0)
        self.assertEqual(self._destination_rows("vendor_catalog_product"), 0)
