import hashlib
import hmac
import json
import os
from base64 import urlsafe_b64encode
from datetime import datetime
from datetime import timezone as datetime_timezone
from pathlib import Path

from bikadeli_common.settings import database_config_from_url
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from vendor_catalog.management.commands.backfill_vendor_catalog import (
    _database_identity,
)
from vendor_catalog.models import (
    VendorKYCDocument,
    VendorKYCExpiryLog,
    VendorKYCVerification,
    VendorProfile,
    VendorSanctionsScreening,
    VendorTaxInfo,
    kyc_document_storage,
    request_catalog_sync,
)

DOCUMENT_FIELDS = (
    'id',
    'vendor_id',
    'document_type',
    'document_subtype',
    'file',
    'file_size',
    'original_filename',
    'expiry_date',
    'verification_status',
    'is_required',
    'rejection_reason',
    'reviewed_by_user_id',
    'reviewed_by_email',
    'reviewed_at',
    'uploaded_by_user_id',
    'encryption_key_id',
    'created_at',
    'updated_at',
)
VERIFICATION_FIELDS = (
    'id',
    'vendor_id',
    'overall_status',
    'required_document_types',
    'submitted_at',
    'verified_at',
    'rejected_at',
    'verified_by_user_id',
    'verified_by_email',
    'rejection_summary',
    'sanctions_check_passed',
    'sanctions_check_date',
    'sanctions_check_details',
    'pep_check_passed',
    'pep_check_date',
    'pep_check_details',
    'next_review_date',
    'created_at',
    'updated_at',
)
TAX_FIELDS = (
    'id',
    'vendor_id',
    'zimra_pin',
    'zimra_pin_verified',
    'zimra_verification_date',
    'zimra_verification_method',
    'vat_registered',
    'vat_number',
    'tax_clearance_certificate',
    'tax_clearance_expiry',
    'created_at',
    'updated_at',
)
SANCTIONS_FIELDS = (
    'id',
    'vendor_id',
    'screening_type',
    'status',
    'matched_name',
    'matched_fields',
    'confidence_score',
    'screened_at',
    'reviewed_at',
    'reviewed_by_user_id',
    'review_notes',
    'updated_at',
)
EXPIRY_LOG_FIELDS = (
    'id',
    'vendor_id',
    'document_id',
    'action',
    'message',
    'metadata',
    'created_at',
)
TABLES = (
    ('documents', 'vendors_vendorkycdocument', DOCUMENT_FIELDS, VendorKYCDocument),
    ('verifications', 'vendors_vendorkycverification', VERIFICATION_FIELDS, VendorKYCVerification),
    ('tax_info', 'vendors_vendortaxinfo', TAX_FIELDS, VendorTaxInfo),
    ('sanctions', 'vendors_vendorsanctionsscreening', SANCTIONS_FIELDS, VendorSanctionsScreening),
    ('expiry_logs', 'vendors_vendorkycexpirylog', EXPIRY_LOG_FIELDS, VendorKYCExpiryLog),
)
SOURCE_COLUMNS = {
    'documents': (
        *DOCUMENT_FIELDS[:11],
        'reviewed_by_id',
        'reviewed_at',
        'uploaded_by_id',
        *DOCUMENT_FIELDS[15:],
    ),
    'verifications': (*VERIFICATION_FIELDS[:7], 'verified_by_id', *VERIFICATION_FIELDS[9:]),
    'sanctions': (*SANCTIONS_FIELDS[:9], 'reviewed_by_id', *SANCTIONS_FIELDS[10:-1]),
    'expiry_logs': ('id', 'vendor_id', 'kyc_document_id', *EXPIRY_LOG_FIELDS[3:]),
}
SOURCE_RENAMES = {
    'documents': {'reviewed_by_id': 'reviewed_by_user_id', 'uploaded_by_id': 'uploaded_by_user_id'},
    'verifications': {'verified_by_id': 'verified_by_user_id'},
    'sanctions': {'reviewed_by_id': 'reviewed_by_user_id'},
    'expiry_logs': {'kyc_document_id': 'document_id'},
}
PRIVATE_FILE_FIELDS = {
    'documents': 'file',
    'tax_info': 'tax_clearance_certificate',
}
DATE_FIELDS = {'expiry_date', 'tax_clearance_expiry', 'next_review_date'}
DATETIME_FIELDS = {
    'reviewed_at',
    'created_at',
    'updated_at',
    'submitted_at',
    'verified_at',
    'rejected_at',
    'sanctions_check_date',
    'pep_check_date',
    'screened_at',
    'zimra_verification_date',
}
JSON_FIELDS = {
    'required_document_types',
    'sanctions_check_details',
    'pep_check_details',
    'matched_fields',
    'metadata',
}
BOOLEAN_FIELDS = {
    'zimra_pin_verified',
    'vat_registered',
    'sanctions_check_passed',
    'pep_check_passed',
    'is_required',
}
class Command(BaseCommand):
    help = 'Dry-run or apply an ID-preserving vendor KYC, tax, sanctions, and expiry-history backfill.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Copy rows and private files. Without this flag, no destination rows or files are written.',
        )
        parser.add_argument(
            '--allow-missing-media',
            action='store_true',
            help='Import rows whose private source files no longer exist; the keys are kept and reported.',
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
            self._validate_references(source)
            plans = {
                name: self._preflight(model, source[name], fields)
                for name, _, fields, model in TABLES
            }
            missing_files, file_conflicts = self._inspect_private_files(source)
            if file_conflicts:
                raise CommandError(f'Private-file checksum conflicts: {file_conflicts[:10]}')
            if options['apply'] and missing_files and not options['allow_missing_media']:
                raise CommandError(
                    'Private KYC/tax files are missing or unavailable in SOURCE_KYC_MEDIA_ROOT; '
                    f'first missing keys: {missing_files[:10]}.'
                )

            if options['apply']:
                self._copy_private_files(source, options['allow_missing_media'])
                with transaction.atomic(using='default'):
                    plans = {
                        name: self._preflight(model, source[name], fields)
                        for name, _, fields, model in TABLES
                    }
                    for name, _, _, model in TABLES:
                        self._insert(model, plans[name])
                    self._verify_destination(
                        source, set(missing_files) if options['allow_missing_media'] else ()
                    )
                    for vendor_id in sorted({row['vendor_id'] for row in source['sanctions']}):
                        request_catalog_sync(vendor_id)

            self._report(source, plans, options['apply'], missing_files)
        finally:
            connections['source'].close()
            del connections['source']
            connections.databases.pop('source', None)

    @staticmethod
    def _select_rows(cursor, table, columns):
        connection = cursor.db
        cursor.execute(
            'SELECT {} FROM {}'.format(
                ', '.join(connection.ops.quote_name(column) for column in columns),
                connection.ops.quote_name(table),
            )
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _read_source(self):
        connection = connections['source']
        try:
            with transaction.atomic(using='source'), connection.cursor() as cursor:
                if connection.vendor == 'postgresql':
                    cursor.execute('SET TRANSACTION READ ONLY')
                elif connection.vendor == 'sqlite':
                    cursor.execute('PRAGMA query_only = ON')
                source = {
                    name: self._select_rows(cursor, table, SOURCE_COLUMNS.get(name, fields))
                    for name, table, fields, _ in TABLES
                }
            for name, renames in SOURCE_RENAMES.items():
                for row in source[name]:
                    for source_field, target_field in renames.items():
                        row[target_field] = row.pop(source_field)
                    if name == 'documents':
                        row['reviewed_by_email'] = ''
                    elif name == 'verifications':
                        row['verified_by_email'] = ''
            for rows in source.values():
                for row in rows:
                    for field, value in list(row.items()):
                        if field in DATETIME_FIELDS:
                            row[field] = self._datetime(value)
                        elif field in DATE_FIELDS:
                            row[field] = self._date(value)
                        elif field in JSON_FIELDS:
                            row[field] = self._json(value, field)
                        elif field in BOOLEAN_FIELDS:
                            row[field] = bool(value)
            # The legacy sanctions table has no updated_at; derive it from the last known event.
            for row in source['sanctions']:
                row['updated_at'] = row['reviewed_at'] or row['screened_at']
            for row in source['tax_info']:
                row['zimra_pin'] = self._decrypt_source_field(row['zimra_pin'])
                row['vat_number'] = self._decrypt_source_field(row['vat_number'])
            return source
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                'Could not read the expected KYC/tax/sanctions source tables; verify source schema and read access '
                f'({type(exc).__name__}: {exc}).'
            ) from exc

    @staticmethod
    def _datetime(value):
        parsed = parse_datetime(value) if isinstance(value, str) else value
        if value is not None and parsed is None:
            raise CommandError('A source compliance row has an invalid timestamp.')
        if parsed is not None and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        return parsed

    @staticmethod
    def _date(value):
        parsed = value.date() if isinstance(value, datetime) else parse_date(value) if isinstance(value, str) else value
        if value is not None and parsed is None:
            raise CommandError('A source compliance row has an invalid date.')
        return parsed

    @staticmethod
    def _json(value, field):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise CommandError(f'Source field {field} contains invalid JSON.') from exc
        if value is None:
            return {} if field in {'sanctions_check_details', 'pep_check_details', 'matched_fields', 'metadata'} else []
        return value

    @staticmethod
    def _source_fernet():
        secret = os.getenv('SOURCE_FIELDS_ENCRYPTION_KEY', '')
        salt = os.getenv('SOURCE_FIELDS_ENCRYPTION_SALT', '')
        if not secret or not salt:
            raise CommandError(
                'Encrypted source tax identifiers require SOURCE_FIELDS_ENCRYPTION_KEY and '
                'SOURCE_FIELDS_ENCRYPTION_SALT.'
            )
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode(),
            iterations=480000,
        )
        return Fernet(urlsafe_b64encode(kdf.derive(secret.encode())))

    def _decrypt_source_field(self, value):
        if not value or not value.startswith('gAAAAA'):
            return value or ''
        if not hasattr(self, '_source_cipher'):
            self._source_cipher = self._source_fernet()
        try:
            return self._source_cipher.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise CommandError(
                'A source tax identifier could not be decrypted; verify the source encryption credentials.'
            ) from exc

    @staticmethod
    def _validate_references(source):
        vendor_ids = set(VendorProfile.objects.using('default').values_list('id', flat=True))
        source_vendor_ids = {row['vendor_id'] for rows in source.values() for row in rows}
        missing_vendors = source_vendor_ids - vendor_ids
        if missing_vendors:
            raise CommandError(
                'Compliance rows reference vendor profiles not yet backfilled: '
                f'{sorted(missing_vendors)[:10]}. Backfill vendor profiles first.'
            )
        document_ids = {row['id'] for row in source['documents']}
        missing_documents = sorted(
            row['document_id']
            for row in source['expiry_logs']
            if row['document_id'] and row['document_id'] not in document_ids
        )
        if missing_documents:
            raise CommandError(f'Expiry logs reference missing source KYC documents: {missing_documents[:10]}.')
        for row in source['documents']:
            if row['document_type'] not in dict(VendorKYCDocument.DOCUMENT_TYPES):
                raise CommandError(f'KYC document {row["id"]} has unsupported type {row["document_type"]}.')

    @staticmethod
    def _preflight(model, rows, fields):
        expected = {row['id']: row for row in rows}
        if len(expected) != len(rows) or any(not row['id'] for row in rows):
            raise CommandError(f'{model.__name__} source rows must have unique, nonzero IDs.')
        conflicts = []
        ids = list(expected)
        manager = model.objects.using('default')
        for start in range(0, len(ids), 500):
            existing = manager.filter(pk__in=ids[start : start + 500]).values(*fields)
            for current in existing:
                source_row = expected[current['id']]
                if any(current[field] != source_row[field] for field in fields if field != 'id'):
                    conflicts.append(current['id'])
        if conflicts:
            raise CommandError(f'{model.__name__} destination IDs conflict with source data: {conflicts[:10]}.')
        if model is VendorKYCDocument:
            source_key_list = [(row['vendor_id'], row['document_type']) for row in rows]
            source_keys = set(source_key_list)
            if len(source_keys) != len(source_key_list):
                raise CommandError('Source KYC documents contain duplicate vendor/type pairs.')
            existing_keys = set(
                manager.filter(vendor_id__in={vendor_id for vendor_id, _ in source_keys})
                .exclude(pk__in=ids)
                .values_list('vendor_id', 'document_type')
            )
            if source_keys & existing_keys:
                raise CommandError('KYC document vendor/type conflicts exist under different destination IDs.')
        if model is VendorTaxInfo:
            source_vendors = {row['vendor_id'] for row in rows}
            if len(source_vendors) != len(rows):
                raise CommandError('Source tax records contain duplicate vendor IDs.')
            existing_vendors = set(
                manager.filter(vendor_id__in=source_vendors).exclude(pk__in=ids).values_list('vendor_id', flat=True)
            )
            if source_vendors & existing_vendors:
                raise CommandError('Tax records conflict with existing vendor tax rows under different IDs.')
        if model is VendorKYCVerification:
            source_vendors = {row['vendor_id'] for row in rows}
            if len(source_vendors) != len(rows):
                raise CommandError('Source verification records contain duplicate vendor IDs.')
            existing_vendors = set(
                manager.filter(vendor_id__in=source_vendors).exclude(pk__in=ids).values_list('vendor_id', flat=True)
            )
            if source_vendors & existing_vendors:
                raise CommandError('Verification records conflict with existing vendor records under different IDs.')
        known_ids = set(manager.filter(pk__in=ids).values_list('id', flat=True))
        return [row for row in rows if row['id'] not in known_ids]

    @staticmethod
    def _source_media_path(name):
        root = os.getenv('SOURCE_KYC_MEDIA_ROOT', '').strip()
        if not root:
            return None
        root_path = Path(root).resolve()
        file_path = (root_path / name).resolve()
        if not file_path.is_relative_to(root_path):
            raise CommandError(f'Unsafe private source media path: {name}')
        return file_path if file_path.is_file() else None

    def _inspect_private_files(self, source):
        missing = []
        conflicts = []
        for table, field in PRIVATE_FILE_FIELDS.items():
            for row in source[table]:
                name = row[field]
                if not name:
                    continue
                source_path = self._source_media_path(name)
                destination_exists = kyc_document_storage.exists(name)
                if source_path is None:
                    missing.append(name)
                    continue
                source_hash = self._file_hash(source_path.open('rb'))
                if destination_exists:
                    with kyc_document_storage.open(name, 'rb') as destination:
                        if source_hash != self._file_hash(destination):
                            conflicts.append(name)
        return sorted(set(missing)), sorted(set(conflicts))

    @staticmethod
    def _file_hash(file_obj):
        digest = hashlib.sha256()
        with file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _copy_private_files(self, source, allow_missing=False):
        for table, field in PRIVATE_FILE_FIELDS.items():
            for row in source[table]:
                name = row[field]
                if not name or kyc_document_storage.exists(name):
                    continue
                source_path = self._source_media_path(name)
                if source_path is None:
                    if allow_missing:
                        continue
                    raise CommandError(f'Private source media file is missing: {name}')
                with source_path.open('rb') as source_file:
                    saved_name = kyc_document_storage.save(name, File(source_file, name=Path(name).name))
                if saved_name != name:
                    raise CommandError(f'Destination private-media key collision while copying {name}.')

    @staticmethod
    def _insert(model, rows):
        if rows:
            objects = [model(**row) for row in rows]
            manager = model.objects.using('default')
            manager.bulk_create(objects, batch_size=500)
            timestamp_fields = [
                field
                for field in ('created_at', 'updated_at', 'screened_at')
                if field in {model_field.name for model_field in model._meta.fields}
            ]
            if timestamp_fields:
                source_by_id = {row['id']: row for row in rows}
                for obj in objects:
                    for field in timestamp_fields:
                        if field in source_by_id[obj.pk]:
                            setattr(obj, field, source_by_id[obj.pk][field])
                manager.bulk_update(objects, timestamp_fields, batch_size=500)
            if connections['default'].vendor == 'postgresql':
                statements = connections['default'].ops.sequence_reset_sql(no_style(), [model])
                with connections['default'].cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)

    def _verify_destination(self, source, tolerated_missing=()):
        for name, _, fields, model in TABLES:
            expected = {row['id']: row for row in source[name]}
            actual = model.objects.using('default').filter(pk__in=expected).values(*fields)
            for current in actual:
                source_row = expected[current['id']]
                conflicts = [
                    field
                    for field in fields
                    if field != 'id' and current[field] != source_row[field]
                ]
                if conflicts:
                    raise CommandError(
                        f'{model.__name__} destination verification failed for ID {current["id"]}; '
                        f'conflicting fields: {conflicts}.'
                    )
            if len(expected) != len(actual):
                raise CommandError(f'{model.__name__} destination verification found missing source IDs.')
        for table, field in PRIVATE_FILE_FIELDS.items():
            for row in source[table]:
                if row[field] and row[field] not in tolerated_missing and not kyc_document_storage.exists(row[field]):
                    raise CommandError(f'Destination private file verification failed for {row[field]}.')

    def _report(self, source, plans, applied, missing_files):
        mode = 'APPLIED' if applied else 'DRY RUN'
        self.stdout.write(self.style.SUCCESS(f'Vendor compliance backfill: {mode}'))
        for name, _, fields, model in TABLES:
            source_checksum = self._checksum(source[name], fields)
            source_ids = [row['id'] for row in source[name]]
            destination_rows = list(
                model.objects.using('default').filter(pk__in=source_ids).values(*fields)
            )
            destination_checksum = self._checksum(destination_rows, fields)
            self.stdout.write(
                f'{name}: source={len(source[name])} insertable={len(plans[name])} '
                f'source_checksum={source_checksum} destination_matching_checksum={destination_checksum}'
            )
        self.stdout.write(f'private_media_missing={len(missing_files)}')
        if missing_files:
            self.stdout.write(self.style.WARNING('Missing private media keys: ' + ', '.join(missing_files[:10])))

    @staticmethod
    def _checksum(rows, fields):
        secret = os.getenv('VENDOR_FIELDS_ENCRYPTION_KEY', '') or settings.SECRET_KEY
        checksum_key = hashlib.sha256(f'vendor-compliance-checksum:{secret}'.encode()).digest()
        digest = hmac.new(checksum_key, digestmod=hashlib.sha256)
        for row in sorted(rows, key=lambda item: item['id']):
            normalized = {}
            for field in fields:
                value = row.get(field)
                if hasattr(value, 'isoformat'):
                    value = value.isoformat()
                elif hasattr(value, 'as_tuple'):
                    value = format(value, 'f')
                normalized[field] = value
            digest.update(json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode())
            digest.update(b'\n')
        return digest.hexdigest()
