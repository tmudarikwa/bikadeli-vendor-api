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
MANAGE_PY = Path(__file__).resolve().parents[1] / 'manage.py'
SOURCE_SECRET = 'legacy-source-encryption-secret'
SOURCE_SALT = 'legacy-source-encryption-salt'
DESTINATION_KEY = Fernet.generate_key().decode()


def _sqlite_url(path):
    return f'sqlite:///{Path(path).as_posix()}'


def _source_cipher():
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SOURCE_SALT.encode(),
        iterations=480000,
    )
    return Fernet(urlsafe_b64encode(kdf.derive(SOURCE_SECRET.encode())))


class VendorComplianceBackfillTests(SimpleTestCase):
    def setUp(self):
        paths = []
        for _ in range(2):
            descriptor, path = tempfile.mkstemp(suffix='.sqlite3')
            os.close(descriptor)
            paths.append(path)
        self.source_path, self.destination_path = paths
        self.source_media = tempfile.TemporaryDirectory()
        self.destination_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_media.cleanup)
        self.addCleanup(self.destination_media.cleanup)
        self.addCleanup(self._remove_databases)
        self._create_source_database()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                'DJANGO_DEBUG': '1',
                'DATABASE_URL': _sqlite_url(self.destination_path),
                'SOURCE_DATABASE_URL': _sqlite_url(self.source_path),
                'SOURCE_FIELDS_ENCRYPTION_KEY': SOURCE_SECRET,
                'SOURCE_FIELDS_ENCRYPTION_SALT': SOURCE_SALT,
                'VENDOR_FIELDS_ENCRYPTION_KEY': DESTINATION_KEY,
                'KYC_MEDIA_ROOT': self.destination_media.name,
                'SOURCE_KYC_MEDIA_ROOT': self.source_media.name,
            }
        )
        self._run_manage('migrate', '--noinput')
        self._create_destination_vendor()

    def _remove_databases(self):
        for path in (self.source_path, self.destination_path):
            if os.path.exists(path):
                os.remove(path)

    def _create_source_database(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.executescript(
                """
                CREATE TABLE vendors_vendorkycdocument (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, document_type TEXT, document_subtype TEXT,
                    file TEXT, file_size INTEGER, original_filename TEXT, expiry_date TEXT,
                    verification_status TEXT, is_required INTEGER, rejection_reason TEXT, reviewed_by_id INTEGER,
                    reviewed_at TEXT, uploaded_by_id INTEGER, encryption_key_id TEXT, created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE vendors_vendorkycverification (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, overall_status TEXT,
                    required_document_types TEXT, submitted_at TEXT, verified_at TEXT, rejected_at TEXT,
                    verified_by_id INTEGER, rejection_summary TEXT, sanctions_check_passed INTEGER,
                    sanctions_check_date TEXT, sanctions_check_details TEXT, pep_check_passed INTEGER,
                    pep_check_date TEXT, pep_check_details TEXT, next_review_date TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_vendortaxinfo (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, zimra_pin TEXT, zimra_pin_verified INTEGER,
                    zimra_verification_date TEXT, zimra_verification_method TEXT, vat_registered INTEGER,
                    vat_number TEXT, tax_clearance_certificate TEXT, tax_clearance_expiry TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_vendorsanctionsscreening (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, screening_type TEXT, status TEXT,
                    matched_name TEXT, matched_fields TEXT, confidence_score TEXT, screened_at TEXT,
                    reviewed_at TEXT, reviewed_by_id INTEGER, review_notes TEXT, updated_at TEXT
                );
                CREATE TABLE vendors_vendorkycexpirylog (
                    id INTEGER PRIMARY KEY, vendor_id INTEGER, kyc_document_id INTEGER, action TEXT,
                    message TEXT, metadata TEXT, created_at TEXT
                );
                """
            )
            timestamp = '2026-01-02 03:04:05'
            connection.execute(
                """
                INSERT INTO vendors_vendorkycdocument VALUES
                (31, 71, 'BUSINESS_LICENSE', 'Incorporation', 'vendors/kyc/2026/01/license.pdf',
                 12, 'license.pdf', '2027-01-01', 'VERIFIED', 1, '', 90, ?, 91, 'kms-key',
                 ?, ?)
                """,
                (timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO vendors_vendorkycverification VALUES
                (32, 71, 'VERIFIED', '["BUSINESS_LICENSE"]', ?, ?, NULL, 90, '', 1, ?, '{}',
                 0, NULL, '{}', '2027-01-01', ?, ?)
                """,
                (timestamp, timestamp, timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO vendors_vendortaxinfo VALUES
                (33, 71, ?, 1, ?, 'manual', 1, ?, 'vendors/tax/2026/01/certificate.pdf',
                 '2027-01-01', ?, ?)
                """,
                (
                    _source_cipher().encrypt(b'1234567890').decode(),
                    timestamp,
                    _source_cipher().encrypt(b'VAT-123').decode(),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO vendors_vendorsanctionsscreening VALUES
                (34, 71, 'OFAC', 'PENDING', '', '{}', NULL, ?, NULL, NULL, '', ?)
                """,
                (timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO vendors_vendorkycexpirylog VALUES
                (35, 71, 31, 'RENEWAL_UPLOADED', 'Renewal uploaded', '{}', ?)
                """,
                (timestamp,),
            )
            connection.commit()
        finally:
            connection.close()
        for relative_name, content in (
            ('vendors/kyc/2026/01/license.pdf', b'private-kyc-document'),
            ('vendors/tax/2026/01/certificate.pdf', b'private-tax-certificate'),
        ):
            media_path = Path(self.source_media.name) / relative_name
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(content)

    def _create_destination_vendor(self):
        connection = sqlite3.connect(self.destination_path)
        try:
            connection.execute(
                """
                INSERT INTO vendor_catalog_vendorprofile
                (id, owner_user_id, business_name, category, description, business_phone, business_email,
                 business_address, is_open, is_active, is_verified, preparation_time_mins, minimum_order,
                 delivery_fee, pickup_available, created_at, updated_at)
                VALUES (71, 9001, 'Test vendor', 'RESTAURANT', '', '', '', '', 0, 1, 0, 15, 0, 0, 1,
                        '2026-01-02 03:04:05', '2026-01-02 03:04:05')
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _run_manage(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(MANAGE_PY), *arguments],
            cwd=WORKSPACE_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if arguments[:1] == ('migrate',) and result.returncode:
            self.fail(f'Migration failed:\n{result.stdout}\n{result.stderr}')
        return result

    def _run_backfill(self, apply=False, *extra):
        return self._run_manage(
            'backfill_vendor_compliance',
            *(('--apply',) if apply else ()),
            *extra,
        )

    def _destination_count(self, table):
        connection = sqlite3.connect(self.destination_path)
        try:
            return connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        finally:
            connection.close()

    def test_dry_run_is_read_only_then_apply_preserves_ids_encrypts_and_copies_files(self):
        dry_run = self._run_backfill()
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn('Vendor compliance backfill: DRY RUN', dry_run.stdout)
        self.assertIn('documents: source=1 insertable=1', dry_run.stdout)
        self.assertIn('private_media_missing=0', dry_run.stdout)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 0)
        self.assertFalse((Path(self.destination_media.name) / 'vendors/kyc/2026/01/license.pdf').exists())

        applied = self._run_backfill(apply=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('Vendor compliance backfill: APPLIED', applied.stdout)
        self._assert_checksums_match(applied.stdout)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 1)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycverification'), 1)
        self.assertEqual(self._destination_count('vendor_catalog_vendortaxinfo'), 1)
        self.assertEqual(self._destination_count('vendor_catalog_vendorsanctionsscreening'), 1)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycexpirylog'), 1)

        destination = sqlite3.connect(self.destination_path)
        try:
            tax = destination.execute(
                'SELECT zimra_pin, vat_number FROM vendor_catalog_vendortaxinfo WHERE id = 33'
            ).fetchone()
            screening = destination.execute(
                'SELECT status FROM vendor_catalog_vendorsanctionsscreening WHERE id = 34'
            ).fetchone()
            audit_actor = destination.execute(
                'SELECT reviewed_by_user_id, reviewed_by_email FROM vendor_catalog_vendorkycdocument WHERE id = 31'
            ).fetchone()
            self.assertTrue(tax[0].startswith('gAAAAA'))
            self.assertTrue(tax[1].startswith('gAAAAA'))
            destination_cipher = Fernet(DESTINATION_KEY.encode())
            self.assertEqual(destination_cipher.decrypt(tax[0].encode()), b'1234567890')
            self.assertEqual(destination_cipher.decrypt(tax[1].encode()), b'VAT-123')
            self.assertEqual(screening, ('PENDING',))
            self.assertEqual(audit_actor, (90, ''))
        finally:
            destination.close()

        for relative_name, content in (
            ('vendors/kyc/2026/01/license.pdf', b'private-kyc-document'),
            ('vendors/tax/2026/01/certificate.pdf', b'private-tax-certificate'),
        ):
            copied = Path(self.destination_media.name) / relative_name
            self.assertEqual(copied.read_bytes(), content)

        repeated = self._run_backfill(apply=True)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn('documents: source=1 insertable=0', repeated.stdout)
        self._assert_checksums_match(repeated.stdout)

    def test_missing_private_file_blocks_apply_before_any_row_insert(self):
        (Path(self.source_media.name) / 'vendors/kyc/2026/01/license.pdf').unlink()
        dry_run = self._run_backfill()
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn('private_media_missing=1', dry_run.stdout)

        applied = self._run_backfill(apply=True)
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn('first missing keys', applied.stderr)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 0)
        self.assertEqual(self._destination_count('vendor_catalog_vendortaxinfo'), 0)

    def test_allow_missing_media_imports_rows_and_keeps_keys(self):
        (Path(self.source_media.name) / 'vendors/kyc/2026/01/license.pdf').unlink()
        applied = self._run_backfill(True, '--allow-missing-media')
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('private_media_missing=1', applied.stdout)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 1)
        self.assertFalse((Path(self.destination_media.name) / 'vendors/kyc/2026/01/license.pdf').exists())

    def test_private_file_checksum_conflict_blocks_apply(self):
        destination_path = Path(self.destination_media.name) / 'vendors/kyc/2026/01/license.pdf'
        destination_path.parent.mkdir(parents=True)
        destination_path.write_bytes(b'wrong-private-file')

        applied = self._run_backfill(apply=True)
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn('Private-file checksum conflicts', applied.stderr)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 0)

    def test_destination_verification_conflict_aborts_without_partial_inserts(self):
        destination = sqlite3.connect(self.destination_path)
        try:
            destination.execute(
                """
                INSERT INTO vendor_catalog_vendorkycverification
                (id, vendor_id, overall_status, required_document_types, verified_by_email, rejection_summary,
                 sanctions_check_passed,
                 sanctions_check_details, pep_check_passed, pep_check_details, created_at, updated_at)
                VALUES (99, 71, 'INCOMPLETE', '[]', '', '', 0, '{}', 0, '{}',
                        '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """
            )
            destination.commit()
        finally:
            destination.close()

        applied = self._run_backfill(apply=True)
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn('Verification records conflict with existing vendor records', applied.stderr)
        self.assertEqual(self._destination_count('vendor_catalog_vendorkycdocument'), 0)
        self.assertEqual(self._destination_count('vendor_catalog_vendortaxinfo'), 0)

    def _assert_checksums_match(self, output):
        lines = [
            line
            for line in output.splitlines()
            if line.startswith(('documents:', 'verifications:', 'tax_info:', 'sanctions:', 'expiry_logs:'))
        ]
        self.assertEqual(len(lines), 5)
        for line in lines:
            checksums = line.split('source_checksum=', 1)[1].split(' destination_matching_checksum=')
            self.assertEqual(checksums[0], checksums[1])
