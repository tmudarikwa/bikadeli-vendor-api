from datetime import timedelta
from unittest.mock import patch

import jwt
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vendor_catalog.models import (
    VendorKYCDocument,
    VendorKYCVerification,
    VendorProfile,
    VendorSanctionsScreening,
    VendorTaxInfo,
)


@override_settings(
    DEBUG=True,
    JWT_SIGNING_KEY='vendor-kyc-test-signing-key',
    JWT_LEGACY_SIGNING_KEY='vendor-kyc-test-signing-key',
    JWT_ALLOW_LEGACY_HS256=True,
    JWT_ISSUER='',
    VENDOR_KYC_SERVICE_ENABLED=True,
    VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
    CLAMAV_ENABLED=False,
)
class VendorKycApiTests(TestCase):
    def setUp(self):
        self.owner_id = 400
        self.other_owner_id = 401
        self.vendor = VendorProfile.objects.create(
            owner_user_id=self.owner_id,
            business_name='KYC Vendor',
            category='RESTAURANT',
        )
        self.other_vendor = VendorProfile.objects.create(
            owner_user_id=self.other_owner_id,
            business_name='Other Vendor',
            category='RESTAURANT',
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access_token(self.owner_id)}')

    @staticmethod
    def access_token(user_id):
        return jwt.encode(
            {
                'token_type': 'access',
                'user_id': user_id,
                'iat': timezone.now(),
                'exp': timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm='HS256',
        )

    @staticmethod
    def pdf_upload(name='business-license.pdf'):
        return SimpleUploadedFile(name, b'%PDF-1.7\nminimal test document', content_type='application/pdf')

    def test_local_kyc_storage_root_is_outside_public_media_root(self):
        from pathlib import Path

        media_root = Path(settings.MEDIA_ROOT).resolve()
        kyc_root = Path(settings.KYC_MEDIA_ROOT).resolve()
        self.assertNotEqual(kyc_root, media_root)
        self.assertNotIn(media_root, kyc_root.parents)

    @patch('vendor_catalog.kyc_storage.kyc_document_storage._save', return_value='vendors/kyc/test.pdf')
    @patch('vendor_catalog.kyc_storage.kyc_document_storage.exists', return_value=False)
    def test_vendor_can_upload_private_kyc_document_and_read_own_status(self, _exists, _save):
        response = self.client.post(
            '/api/v1/vendors/kyc/documents/',
            {
                'document_type': 'BUSINESS_LICENSE',
                'document_subtype': 'Certificate',
                'file': self.pdf_upload(),
                'expiry_date': (timezone.localdate() + timedelta(days=180)).isoformat(),
                'is_required': 'false',
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 201, response.data)
        document = VendorKYCDocument.objects.get(vendor=self.vendor)
        self.assertEqual(document.verification_status, 'PENDING')
        self.assertTrue(document.is_required)
        self.assertEqual(document.uploaded_by_user_id, self.owner_id)
        self.assertEqual(document.original_filename, 'business-license.pdf')
        self.assertIsNone(response.data['file_url'])
        verification = VendorKYCVerification.objects.get(vendor=self.vendor)
        self.assertEqual(verification.overall_status, 'PENDING')
        self.assertEqual(verification.submitted_at is not None, True)

        listing = self.client.get('/api/v1/vendors/kyc/documents/')
        status_list = self.client.get('/api/v1/vendors/kyc/verification/')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data[0]['id'], document.id)
        self.assertEqual(status_list.data[0]['overall_status'], 'PENDING')

    def test_invalid_file_content_and_duplicate_document_type_are_rejected(self):
        invalid = self.client.post(
            '/api/v1/vendors/kyc/documents/',
            {
                'document_type': 'NATIONAL_ID',
                'file': SimpleUploadedFile('id.pdf', b'not a PDF'),
            },
            format='multipart',
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(VendorKYCDocument.objects.exists())

        VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/existing.pdf',
            file_size=32,
            original_filename='existing.pdf',
        )
        duplicate = self.client.post(
            '/api/v1/vendors/kyc/documents/',
            {
                'document_type': 'NATIONAL_ID',
                'file': self.pdf_upload('new.pdf'),
            },
            format='multipart',
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(VendorKYCDocument.objects.count(), 1)

    @patch('vendor_catalog.kyc_uploads._scan_with_clamav', side_effect=OSError('scanner offline'))
    def test_clamav_failure_rejects_upload(self, scan):
        with self.settings(CLAMAV_ENABLED=True):
            response = self.client.post(
                '/api/v1/vendors/kyc/documents/',
                {
                    'document_type': 'NATIONAL_ID',
                    'file': self.pdf_upload(),
                },
                format='multipart',
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(VendorKYCDocument.objects.exists())
        scan.assert_called_once()

    def test_oversized_upload_is_rejected_before_storage(self):
        upload = SimpleUploadedFile(
            'oversized.pdf',
            b'%PDF-' + (b'x' * (10 * 1024 * 1024)),
            content_type='application/pdf',
        )
        response = self.client.post(
            '/api/v1/vendors/kyc/documents/',
            {'document_type': 'NATIONAL_ID', 'file': upload},
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(VendorKYCDocument.objects.exists())

    def test_vendor_cannot_read_or_delete_another_vendors_document(self):
        document = VendorKYCDocument.objects.create(
            vendor=self.other_vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/other.pdf',
            file_size=32,
            original_filename='other.pdf',
        )
        response = self.client.get(f'/api/v1/vendors/kyc/documents/{document.id}/')
        self.assertEqual(response.status_code, 404)

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access_token(self.other_owner_id)}')
        response = self.client.delete(f'/api/v1/vendors/kyc/documents/{document.id}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(VendorKYCDocument.objects.filter(pk=document.id).exists())

    @patch('vendor_catalog.views.proxy_to_monolith')
    def test_disabled_kyc_extraction_preserves_monolith_route(self, proxy):
        from django.http import JsonResponse

        proxy.return_value = JsonResponse({'legacy': True})
        with self.settings(VENDOR_KYC_SERVICE_ENABLED=False):
            response = self.client.get('/api/v1/vendors/kyc/documents/')
        self.assertEqual(response.json(), {'legacy': True})
        proxy.assert_called_once()

    def test_internal_review_requires_token_and_rejection_reason(self):
        path = f'/internal/v1/admin/vendors/{self.vendor.id}/kyc/verifications/reject/'
        payload = {
            'actor_id': 8,
            'actor_email': 'reviewer@example.test',
            'reason': 'Incomplete records',
        }
        denied = self.client.post(path, payload, format='json')
        self.assertEqual(denied.status_code, 404)

        missing_reason = self.client.post(
            path,
            {**payload, 'reason': ''},
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(missing_reason.status_code, 400)
        self.assertFalse(VendorKYCVerification.objects.exists())

    def test_internal_rejection_records_reason_and_audit(self):
        from vendor_catalog.models import VendorKYCAuditLog

        document = VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/rejected.pdf',
            file_size=32,
            original_filename='rejected.pdf',
        )
        response = self.client.post(
            f'/internal/v1/admin/vendors/kyc/documents/{document.id}/reject/',
            {
                'actor_id': 8,
                'actor_email': 'reviewer@example.test',
                'reason': 'Document is unreadable',
            },
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )

        self.assertEqual(response.status_code, 200, response.data)
        document.refresh_from_db()
        self.assertEqual(document.verification_status, 'REJECTED')
        self.assertEqual(document.rejection_reason, 'Document is unreadable')
        self.assertEqual(VendorKYCAuditLog.objects.get(document=document).action, 'DOCUMENT_REJECT')

    def test_internal_queue_and_detail_preserve_kyc_review_contract(self):
        verification = VendorKYCVerification.objects.create(
            vendor=self.vendor,
            overall_status='PENDING',
            required_document_types=['NATIONAL_ID'],
        )
        document = VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/private.pdf',
            file_size=32,
            original_filename='private.pdf',
            expiry_date=timezone.localdate() + timedelta(days=10),
        )
        queue = self.client.get(
            '/internal/v1/admin/vendors/kyc/review-queue/?page=1&status=PENDING',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )

        self.assertEqual(queue.status_code, 200, queue.data)
        self.assertEqual(queue.data['total'], 1)
        self.assertEqual(queue.data['results'][0]['vendor_id'], self.vendor.id)
        self.assertEqual(queue.data['results'][0]['pending_documents'], 1)
        self.assertEqual(queue.data['results'][0]['expiring_soon'], 1)

        detail = self.client.get(
            f'/internal/v1/admin/vendors/{self.vendor.id}/kyc/',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data['kyc_verification']['missing_documents'], [])
        self.assertEqual(detail.data['documents'][0]['id'], document.id)
        self.assertIsNone(detail.data['documents'][0]['file_url'])
        self.assertEqual(verification.overall_status, 'PENDING')

    def test_internal_document_review_is_audited_and_vendor_can_delete_after_review(self):
        from vendor_catalog.models import VendorKYCAuditLog

        document = VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/reviewed.pdf',
            file_size=32,
            original_filename='reviewed.pdf',
        )
        reviewed = self.client.post(
            f'/internal/v1/admin/vendors/kyc/documents/{document.id}/approve/',
            {'actor_id': 8, 'actor_email': 'reviewer@example.test'},
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
            HTTP_X_REQUEST_ID='kyc-review-123',
        )

        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        document.refresh_from_db()
        self.assertEqual(document.verification_status, 'VERIFIED')
        audit = VendorKYCAuditLog.objects.get(document=document)
        self.assertEqual(audit.action, 'DOCUMENT_APPROVE')
        self.assertEqual(audit.request_id, 'kyc-review-123')

        deleted = self.client.delete(f'/api/v1/vendors/kyc/documents/{document.id}/')
        self.assertEqual(deleted.status_code, 204)
        audit.refresh_from_db()
        self.assertIsNone(audit.document)
        self.assertEqual(audit.document_id_snapshot, document.id)

    def test_internal_verification_requires_verified_required_documents(self):
        verification = VendorKYCVerification.objects.create(
            vendor=self.vendor,
            required_document_types=['NATIONAL_ID', 'BUSINESS_LICENSE'],
        )
        VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/pending.pdf',
            file_size=32,
            original_filename='pending.pdf',
        )
        path = f'/internal/v1/admin/vendors/{self.vendor.id}/kyc/verifications/approve/'
        payload = {'actor_id': 8, 'actor_email': 'reviewer@example.test'}

        rejected = self.client.post(
            path,
            payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(rejected.status_code, 400)
        verification.refresh_from_db()
        self.assertEqual(verification.overall_status, 'INCOMPLETE')

        VendorKYCDocument.objects.filter(vendor=self.vendor).update(verification_status='VERIFIED')
        still_missing = self.client.post(
            path,
            payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(still_missing.status_code, 400)

        VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='BUSINESS_LICENSE',
            file='vendors/kyc/license.pdf',
            file_size=32,
            original_filename='license.pdf',
            verification_status='VERIFIED',
        )
        approved = self.client.post(
            path,
            payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        verification.refresh_from_db()
        self.assertEqual(verification.overall_status, 'VERIFIED')

    def test_expired_required_document_prevents_verified_status(self):
        verification = VendorKYCVerification.objects.create(
            vendor=self.vendor,
            required_document_types=['NATIONAL_ID'],
        )
        VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/expired.pdf',
            file_size=32,
            original_filename='expired.pdf',
            verification_status='VERIFIED',
            expiry_date=timezone.localdate() - timedelta(days=1),
        )

        self.assertFalse(verification.all_required_verified())
        self.assertEqual(verification.refresh_status(), 'EXPIRED')

    def test_tax_info_is_owner_scoped_and_sensitive_numbers_are_write_only_and_encrypted(self):
        created = self.client.post(
            '/api/v1/vendors/kyc/tax/',
            {
                'zimra_pin': '1234567890',
                'vat_number': 'VAT1234567',
                'vat_registered': True,
            },
            format='json',
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data['zimra_pin_masked'], '******7890')
        self.assertEqual(created.data['vat_number_masked'], '******4567')
        self.assertNotIn('zimra_pin', created.data)
        self.assertNotIn('vat_number', created.data)
        tax_info = VendorTaxInfo.objects.get(vendor=self.vendor)
        self.assertEqual(tax_info.zimra_pin, '1234567890')
        self.assertTrue(tax_info.vat_registered)

        with connection.cursor() as cursor:
            cursor.execute('SELECT zimra_pin, vat_number FROM vendor_catalog_vendortaxinfo WHERE id = %s', [tax_info.id])
            stored_pin, stored_vat = cursor.fetchone()
        self.assertNotEqual(stored_pin, '1234567890')
        self.assertNotEqual(stored_vat, 'VAT1234567')

        other_tax = VendorTaxInfo.objects.create(vendor=self.other_vendor, zimra_pin='9876543210')
        denied = self.client.get(f'/api/v1/vendors/kyc/tax/{other_tax.id}/')
        self.assertEqual(denied.status_code, 404)

    def test_tax_info_rejects_invalid_pin_and_updates_only_supplied_fields(self):
        invalid = self.client.post(
            '/api/v1/vendors/kyc/tax/',
            {'zimra_pin': '1234'},
            format='json',
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(VendorTaxInfo.objects.exists())

        tax_info = VendorTaxInfo.objects.create(
            vendor=self.vendor,
            zimra_pin='1234567890',
            vat_registered=True,
        )
        response = self.client.patch(
            f'/api/v1/vendors/kyc/tax/{tax_info.id}/',
            {'vat_registered': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        tax_info.refresh_from_db()
        self.assertEqual(tax_info.zimra_pin, '1234567890')
        self.assertFalse(tax_info.vat_registered)

    def test_vendor_can_delete_only_own_tax_info(self):
        own_tax = VendorTaxInfo.objects.create(vendor=self.vendor)
        other_tax = VendorTaxInfo.objects.create(vendor=self.other_vendor)

        removed = self.client.delete(f'/api/v1/vendors/kyc/tax/{own_tax.id}/')
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(VendorTaxInfo.objects.filter(pk=own_tax.id).exists())

        denied = self.client.delete(f'/api/v1/vendors/kyc/tax/{other_tax.id}/')
        self.assertEqual(denied.status_code, 404)
        self.assertTrue(VendorTaxInfo.objects.filter(pk=other_tax.id).exists())

    def test_sanctions_records_are_read_only_and_owner_scoped(self):
        own_record = VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='PEP',
            status='POTENTIAL_MATCH',
            matched_name='Potential Name',
            confidence_score='83.50',
        )
        VendorSanctionsScreening.objects.create(
            vendor=self.other_vendor,
            screening_type='OFAC',
            status='CONFIRMED_MATCH',
            matched_name='Other Vendor Match',
        )

        response = self.client.get('/api/v1/vendors/kyc/sanctions/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], own_record.id)
        self.assertEqual(response.data[0]['status'], 'POTENTIAL_MATCH')
        self.assertNotIn('Other Vendor Match', str(response.data))
        detail = self.client.get(f'/api/v1/vendors/kyc/sanctions/{own_record.id}/')
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data['screening_type_display'], 'Politically Exposed Persons')
        denied = self.client.get(
            f'/api/v1/vendors/kyc/sanctions/{VendorSanctionsScreening.objects.filter(vendor=self.other_vendor).first().id}/'
        )
        self.assertEqual(denied.status_code, 404)
        mutation = self.client.patch(
            f'/api/v1/vendors/kyc/sanctions/{own_record.id}/',
            {'status': 'CLEAR'},
            format='json',
        )
        self.assertEqual(mutation.status_code, 405)

    def test_internal_sanctions_review_updates_status_and_audit(self):
        from vendor_catalog.models import VendorKYCAuditLog

        screening = VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='PEP',
            status='PENDING',
        )
        response = self.client.post(
            f'/internal/v1/admin/vendors/kyc/sanctions/{screening.id}/confirm-match/',
            {
                'actor_id': 8,
                'actor_email': 'reviewer@example.test',
                'reason': 'Confirmed against verified source.',
                'matched_name': 'Matched Person',
                'matched_fields': {'name': True, 'date_of_birth': False},
                'confidence_score': '98.25',
            },
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
            HTTP_X_REQUEST_ID='sanctions-review-123',
        )

        self.assertEqual(response.status_code, 200, response.data)
        screening.refresh_from_db()
        self.assertEqual(screening.status, 'CONFIRMED_MATCH')
        self.assertEqual(screening.reviewed_by_user_id, 8)
        self.assertEqual(screening.review_notes, 'Confirmed against verified source.')
        self.assertEqual(screening.matched_name, 'Matched Person')
        self.assertEqual(str(screening.confidence_score), '98.25')
        audit = VendorKYCAuditLog.objects.get(vendor=self.vendor)
        self.assertEqual(audit.action, 'SANCTIONS_CONFIRM_MATCH')
        self.assertEqual(audit.previous_status, 'PENDING')
        self.assertEqual(audit.status, 'CONFIRMED_MATCH')
        self.assertEqual(audit.request_id, 'sanctions-review-123')
        from vendor_catalog.models import VendorCatalogSyncOutbox

        self.assertTrue(VendorCatalogSyncOutbox.objects.filter(vendor_id=self.vendor.id).exists())

    def test_internal_sanctions_review_requires_reason_and_valid_confidence(self):
        screening = VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='OFAC',
            status='PENDING',
        )
        path = f'/internal/v1/admin/vendors/kyc/sanctions/{screening.id}/clear/'
        base_payload = {
            'actor_id': 8,
            'actor_email': 'reviewer@example.test',
        }
        missing_reason = self.client.post(
            path,
            base_payload,
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(missing_reason.status_code, 400)

        invalid_confidence = self.client.post(
            path,
            {**base_payload, 'reason': 'Reviewed', 'confidence_score': '100.01'},
            format='json',
            HTTP_X_VENDOR_ADMIN_TOKEN='vendor-kyc-test-admin-token',
        )
        self.assertEqual(invalid_confidence.status_code, 400)
        screening.refresh_from_db()
        self.assertEqual(screening.status, 'PENDING')

    def test_confirmed_sanctions_match_removes_vendor_from_public_results(self):
        self.vendor.is_verified = True
        self.vendor.save(update_fields=['is_verified'])
        VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='OFAC',
            status='CONFIRMED_MATCH',
        )

        response = self.client.get('/api/v1/vendors/public/vendors/')

        self.assertEqual(response.status_code, 200, response.data)
        results = response.data['results'] if isinstance(response.data, dict) else response.data
        self.assertNotIn(self.vendor.id, {item['id'] for item in results})
        detail = self.client.get(f'/api/v1/vendors/public/vendors/{self.vendor.id}/')
        self.assertEqual(detail.status_code, 404)

    def test_compliance_dashboard_uses_vendor_owned_kyc_sanctions_and_tax_data(self):
        verification = VendorKYCVerification.objects.create(
            vendor=self.vendor,
            overall_status='VERIFIED',
            required_document_types=['NATIONAL_ID'],
            next_review_date=timezone.localdate() + timedelta(days=180),
        )
        VendorKYCDocument.objects.create(
            vendor=self.vendor,
            document_type='NATIONAL_ID',
            file='vendors/kyc/expiring.pdf',
            file_size=32,
            original_filename='expiring.pdf',
            is_required=True,
            verification_status='VERIFIED',
            expiry_date=timezone.localdate() + timedelta(days=10),
        )
        VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='OFAC',
            status='CLEAR',
        )
        VendorSanctionsScreening.objects.create(
            vendor=self.vendor,
            screening_type='PEP',
            status='POTENTIAL_MATCH',
        )
        VendorTaxInfo.objects.create(
            vendor=self.vendor,
            zimra_pin='1234567890',
            zimra_pin_verified=True,
        )

        response = self.client.get('/api/v1/vendors/kyc/dashboard/dashboard/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['vendor_id'], self.vendor.id)
        self.assertEqual(response.data['kyc_status'], verification.overall_status)
        self.assertTrue(response.data['is_kyc_verified'])
        self.assertEqual(response.data['missing_documents'], [])
        self.assertEqual(response.data['sanctions_status'], 'POTENTIAL_MATCH')
        self.assertEqual(response.data['pep_status'], 'POTENTIAL_MATCH')
        self.assertTrue(response.data['zimra_pin_verified'])
        self.assertFalse(response.data['should_be_suspended'])
        self.assertEqual(
            response.data['expiring_documents'][0]['days_until_expiry'],
            10,
        )
        self.assertEqual(response.data['next_review_date'], timezone.localdate() + timedelta(days=180))
