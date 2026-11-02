import hmac
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from mimetypes import guess_type
from os.path import basename

import httpx
from bikadeli_common.authentication import AccessTokenAuthentication
from bikadeli_common.gateway import proxy_to_monolith
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .media import public_media_url, validate_image_upload
from .models import (
    CatalogCategory,
    Product,
    ProductImage,
    RestaurantMenuCategory,
    RestaurantMenuItem,
    RestaurantMenuItemImage,
    VendorKYCAuditLog,
    VendorKYCDocument,
    VendorKYCVerification,
    VendorNotificationOutbox,
    VendorNotificationPreference,
    VendorProfile,
    VendorProfileAuditLog,
    VendorSanctionsScreening,
    VendorTaxInfo,
    request_catalog_sync,
)
from .notification_outbox import publish_pending_vendor_notifications
from .permissions import IsVendorOwner
from .serializers import (
    CatalogCategorySerializer,
    InternalVendorKYCReviewSerializer,
    InternalVendorSanctionsReviewSerializer,
    InternalVendorVerificationSerializer,
    MarketplaceCategorySerializer,
    MarketplaceProductSerializer,
    ProductCreateUpdateSerializer,
    ProductSerializer,
    PublicProductSerializer,
    PublicRestaurantMenuItemSerializer,
    PublicVendorSerializer,
    RestaurantMenuCategoryInputSerializer,
    RestaurantMenuCategorySerializer,
    RestaurantMenuItemImageSerializer,
    RestaurantMenuItemSerializer,
    VendorApplicationSerializer,
    VendorAvailabilitySerializer,
    VendorKYCDocumentSerializer,
    VendorKYCVerificationSerializer,
    VendorNotificationMarkReadSerializer,
    VendorNotificationPreferenceSerializer,
    VendorNotificationPreferenceUpdateSerializer,
    VendorNotificationSerializer,
    VendorProfileSerializer,
    VendorSanctionsScreeningSerializer,
    VendorTaxInfoSerializer,
)

logger = logging.getLogger(__name__)


def public_vendor_profiles():
    return VendorProfile.objects.filter(is_active=True, is_verified=True).exclude(
        sanctions_screenings__status='CONFIRMED_MATCH'
    )


class VendorRegistrationRateThrottle(AnonRateThrottle):
    scope = 'vendor_registration'


class VendorApplicationView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [VendorRegistrationRateThrottle]

    def post(self, request):
        serializer = VendorApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = serializer.validated_data
        identity_payload = {
            key: application[key]
            for key in ('email', 'phone_number', 'first_name', 'last_name')
            if key in application
        }
        try:
            response = httpx.post(
                f'{settings.IDENTITY_SERVICE_URL.rstrip("/")}/internal/v1/identity/vendor-applications/',
                json=identity_payload,
                headers={'X-Vendor-Registration-Token': settings.VENDOR_REGISTRATION_TOKEN},
                timeout=5.0,
            )
        except httpx.RequestError:
            logger.exception('Identity service unavailable during vendor application')
            return Response(
                {'error': 'Vendor registration is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if response.status_code == status.HTTP_409_CONFLICT:
            return Response(
                {'message': 'If the email is registered, check your inbox for instructions'},
                status=status.HTTP_200_OK,
            )
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            return Response(response.json(), status=status.HTTP_400_BAD_REQUEST)
        if response.status_code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
            logger.error('Identity service rejected vendor application with status %s', response.status_code)
            return Response(
                {'error': 'Vendor registration is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            user_id = response.json()['user_id']
        except (ValueError, KeyError, TypeError):
            logger.error('Identity service returned an invalid vendor application response')
            return Response(
                {'error': 'Vendor registration is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if type(user_id) is not int or user_id < 1:
            logger.error('Identity service returned an invalid vendor identity ID')
            return Response(
                {'error': 'Vendor registration is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        _, created = VendorProfile.objects.get_or_create(
            owner_user_id=user_id,
            defaults={
                'business_name': application['business_name'],
                'category': application['category'],
                'business_address': application['business_address'],
                'business_phone': application['business_phone'],
                'business_email': application['business_email'] or application['email'],
                'is_active': False,
                'is_verified': False,
            },
        )
        if not created:
            return Response(
                {'message': 'If the email is registered, check your inbox for instructions'},
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                'message': (
                    'Application submitted successfully. Our team will review your application and contact you shortly.'
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class InternalVendorVerificationView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request, vendor_id):
        supplied_token = request.headers.get('X-Vendor-Admin-Token', '')
        if not settings.VENDOR_ADMIN_TOKEN or not hmac.compare_digest(
            supplied_token,
            settings.VENDOR_ADMIN_TOKEN,
        ):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not isinstance(request.data, dict) or type(request.data.get('is_verified')) is not bool:
            return Response({'is_verified': ['Must be a boolean.']}, status=status.HTTP_400_BAD_REQUEST)

        serializer = InternalVendorVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = VendorProfile.objects.filter(pk=vendor_id).only('id', 'owner_user_id').first()
        if profile is None:
            return Response({'detail': 'Vendor not found.'}, status=status.HTTP_404_NOT_FOUND)

        is_verified = serializer.validated_data['is_verified']
        status_notification = None
        if is_verified:
            try:
                identity_response = httpx.post(
                    f'{settings.IDENTITY_SERVICE_URL.rstrip("/")}/internal/v1/identity/'
                    f'vendor-accounts/{profile.owner_user_id}/activate/',
                    headers={'X-Vendor-Registration-Token': settings.VENDOR_REGISTRATION_TOKEN},
                    timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
                )
            except httpx.RequestError:
                logger.exception('Identity service unavailable during vendor approval for profile %s', vendor_id)
                return Response(
                    {'detail': 'Vendor identity activation is temporarily unavailable.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if identity_response.status_code == status.HTTP_404_NOT_FOUND:
                return Response({'detail': 'Vendor identity not found.'}, status=status.HTTP_404_NOT_FOUND)
            if identity_response.status_code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
                logger.error(
                    'Identity service failed vendor activation for profile %s with status %s',
                    vendor_id,
                    identity_response.status_code,
                )
                return Response(
                    {'detail': 'Vendor identity activation is temporarily unavailable.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

        with transaction.atomic():
            profile = VendorProfile.objects.select_for_update().filter(pk=vendor_id).first()
            if profile is None:
                return Response({'detail': 'Vendor not found.'}, status=status.HTTP_404_NOT_FOUND)
            previous_is_verified = profile.is_verified
            previous_is_active = profile.is_active
            profile.is_verified = is_verified
            verification_changed = previous_is_verified != is_verified
            if is_verified:
                profile.is_active = True
            profile.save(update_fields=['is_verified', 'is_active'])
            audit = VendorProfileAuditLog.objects.create(
                vendor=profile,
                actor_user_id=serializer.validated_data['actor_id'],
                actor_email=serializer.validated_data['actor_email'],
                action='VERIFY' if is_verified else 'UNVERIFY',
                previous_is_verified=previous_is_verified,
                is_verified=profile.is_verified,
                previous_is_active=previous_is_active,
                is_active=profile.is_active,
                request_id=serializer.validated_data['request_id'],
            )
            if verification_changed:
                is_verified_text = 'verified' if profile.is_verified else 'unverified'
                status_notification = VendorNotificationOutbox.objects.create(
                    vendor=profile,
                    recipient_user_id=profile.owner_user_id,
                    action='STATUS_CHANGED',
                    idempotency_key=f'vendor-status:{audit.pk}',
                    title='Vendor Status',
                    body=f'Your account has been {is_verified_text}.',
                    data={
                        'vendor_id': profile.id,
                        'is_verified': profile.is_verified,
                    },
                )
            if previous_is_verified != profile.is_verified or previous_is_active != profile.is_active:
                request_catalog_sync(profile.id)

        if verification_changed:
            try:
                delivery = publish_pending_vendor_notifications(notification_ids=[status_notification.pk])
                if delivery['failed']:
                    logger.error(
                        'Vendor status notification remains queued for identity %s after publication failure.',
                        profile.owner_user_id,
                    )
            except Exception:
                logger.exception(
                    'Failed to publish vendor status notification for identity %s; notification remains queued.',
                    profile.owner_user_id,
                )

        return Response(
            {
                'status': 'ok',
                'vendor_id': profile.id,
                'is_verified': profile.is_verified,
                'is_active': profile.is_active,
            }
        )


class InternalVendorKYCView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def authorize(self, request):
        supplied_token = request.headers.get('X-Vendor-Admin-Token', '')
        return bool(settings.VENDOR_ADMIN_TOKEN) and hmac.compare_digest(
            supplied_token,
            settings.VENDOR_ADMIN_TOKEN,
        )

    def review_data(self, request, require_reason=False):
        if not isinstance(request.data, dict):
            raise ValidationError({'detail': ['A JSON object is required.']})
        request_id = request.headers.get('X-Request-ID')
        if request_id is None:
            request_id = request.data.get('request_id', '')
        serializer = InternalVendorKYCReviewSerializer(
            data={
                **request.data,
                'request_id': request_id,
            }
        )
        serializer.is_valid(raise_exception=True)
        if require_reason and not serializer.validated_data['reason'].strip():
            raise ValidationError({'reason': ['A reason is required.']})
        return serializer.validated_data

    def get(self, request):
        if not settings.VENDOR_KYC_SERVICE_ENABLED or not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            page = int(request.query_params.get('page', '1'))
        except (TypeError, ValueError) as exc:
            raise ValidationError({'page': ['A positive integer is required.']}) from exc
        if page < 1:
            raise ValidationError({'page': ['A positive integer is required.']})

        verifications = VendorKYCVerification.objects.select_related('vendor').prefetch_related(
            'vendor__kyc_documents'
        )
        status_filter = request.query_params.get('status', '').strip().upper()
        if status_filter:
            verifications = verifications.filter(overall_status=status_filter)
        total = verifications.count()
        items = verifications.order_by('-created_at')[(page - 1) * 20 : page * 20]
        today = timezone.localdate()
        expiring_by = today + timedelta(days=30)
        results = []
        for verification in items:
            vendor = verification.vendor
            documents = vendor.kyc_documents.all()
            results.append(
                {
                    'vendor_id': vendor.id,
                    'business_name': vendor.business_name,
                    'email': vendor.business_email,
                    'overall_status': verification.overall_status,
                    'submitted_at': verification.submitted_at.isoformat() if verification.submitted_at else None,
                    'verified_at': verification.verified_at.isoformat() if verification.verified_at else None,
                    'rejected_at': verification.rejected_at.isoformat() if verification.rejected_at else None,
                    'rejection_summary': verification.rejection_summary,
                    'missing_documents': (
                        verification.missing_required_types()
                        if not verification.all_required_verified()
                        else []
                    ),
                    'pending_documents': documents.filter(
                        verification_status__in=['PENDING', 'UNDER_REVIEW']
                    ).count(),
                    'expiring_soon': documents.filter(
                        expiry_date__gte=today,
                        expiry_date__lte=expiring_by,
                    ).count(),
                    'sanctions_check_passed': verification.sanctions_check_passed,
                    'pep_check_passed': verification.pep_check_passed,
                    'next_review_date': (
                        verification.next_review_date.isoformat() if verification.next_review_date else None
                    ),
                    'should_be_suspended': vendor.should_be_suspended(),
                }
            )
        return Response({'results': results, 'total': total, 'page': page, 'page_size': 20})


class InternalVendorKYCDetailView(InternalVendorKYCView):
    def get(self, request, vendor_id):
        if not settings.VENDOR_KYC_SERVICE_ENABLED or not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        vendor = get_object_or_404(VendorProfile, pk=vendor_id)
        verification = VendorKYCVerification.objects.filter(vendor=vendor).first()
        documents = VendorKYCDocument.objects.filter(vendor=vendor).order_by('-created_at', '-id')
        verification_data = None
        if verification:
            verification_data = {
                'overall_status': verification.overall_status,
                'required_document_types': verification.required_document_types,
                'submitted_at': verification.submitted_at.isoformat() if verification.submitted_at else None,
                'verified_at': verification.verified_at.isoformat() if verification.verified_at else None,
                'rejected_at': verification.rejected_at.isoformat() if verification.rejected_at else None,
                'verified_by': verification.verified_by_email or None,
                'rejection_summary': verification.rejection_summary,
                'missing_documents': verification.missing_required_types(),
                'all_required_verified': verification.all_required_verified(),
                'next_review_date': (
                    verification.next_review_date.isoformat() if verification.next_review_date else None
                ),
                'sanctions_check_passed': verification.sanctions_check_passed,
                'pep_check_passed': verification.pep_check_passed,
            }
        return Response(
            {
                'vendor_id': vendor.id,
                'business_name': vendor.business_name,
                'kyc_verification': verification_data,
                'documents': VendorKYCDocumentSerializer(
                    documents,
                    many=True,
                    context={'request': request},
                ).data,
            }
        )


class InternalVendorKYCDocumentReviewView(InternalVendorKYCView):
    def post(self, request, document_id, action):
        if not settings.VENDOR_KYC_SERVICE_ENABLED or not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if action not in {'approve', 'reject'}:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        review = self.review_data(request, require_reason=action == 'reject')
        with transaction.atomic():
            document = get_object_or_404(
                VendorKYCDocument.objects.select_for_update().select_related('vendor'),
                pk=document_id,
            )
            previous_status = document.verification_status
            document.verification_status = 'VERIFIED' if action == 'approve' else 'REJECTED'
            document.reviewed_by_user_id = review['actor_id']
            document.reviewed_by_email = review['actor_email']
            document.reviewed_at = timezone.now()
            document.rejection_reason = '' if action == 'approve' else review['reason'].strip()
            document.updated_at = timezone.now()
            document.save(
                update_fields=[
                    'verification_status',
                    'reviewed_by_user_id',
                    'reviewed_by_email',
                    'reviewed_at',
                    'rejection_reason',
                    'updated_at',
                ]
            )
            verification = VendorKYCVerification.objects.filter(vendor=document.vendor).first()
            if verification:
                verification.refresh_status()
            VendorKYCAuditLog.objects.create(
                vendor=document.vendor,
                document=document,
                document_id_snapshot=document.id,
                actor_user_id=review['actor_id'],
                actor_email=review['actor_email'],
                action=f'DOCUMENT_{action.upper()}',
                previous_status=previous_status,
                status=document.verification_status,
                reason=document.rejection_reason,
                request_id=review['request_id'],
            )
        return Response(
            {'status': 'ok', 'document_id': document.id, 'verification_status': document.verification_status}
        )


class InternalVendorSanctionsReviewView(InternalVendorKYCView):
    def post(self, request, screening_id, action):
        if not settings.VENDOR_KYC_SERVICE_ENABLED or not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        statuses = {
            'clear': 'CLEAR',
            'potential-match': 'POTENTIAL_MATCH',
            'confirm-match': 'CONFIRMED_MATCH',
        }
        if action not in statuses:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not isinstance(request.data, dict):
            raise ValidationError({'detail': ['A JSON object is required.']})
        request_id = request.headers.get('X-Request-ID')
        if request_id is None:
            request_id = request.data.get('request_id', '')
        serializer = InternalVendorSanctionsReviewSerializer(
            data={**request.data, 'request_id': request_id}
        )
        serializer.is_valid(raise_exception=True)
        review = serializer.validated_data
        if not review['reason'].strip():
            raise ValidationError({'reason': ['A review reason is required.']})

        with transaction.atomic():
            screening = get_object_or_404(
                VendorSanctionsScreening.objects.select_for_update().select_related('vendor'),
                pk=screening_id,
            )
            previous_status = screening.status
            screening.status = statuses[action]
            screening.reviewed_at = timezone.now()
            screening.reviewed_by_user_id = review['actor_id']
            screening.review_notes = review['reason'].strip()
            for field in ('matched_name', 'matched_fields', 'confidence_score'):
                if field in review:
                    setattr(screening, field, review[field])
            screening.save(
                update_fields=[
                    'status',
                    'reviewed_at',
                    'reviewed_by_user_id',
                    'review_notes',
                    'matched_name',
                    'matched_fields',
                    'confidence_score',
                    'updated_at',
                ]
            )
            VendorKYCAuditLog.objects.create(
                vendor=screening.vendor,
                actor_user_id=review['actor_id'],
                actor_email=review['actor_email'],
                action=f'SANCTIONS_{action.upper().replace("-", "_")}',
                previous_status=previous_status,
                status=screening.status,
                reason=review['reason'].strip(),
                request_id=review['request_id'],
            )
            request_catalog_sync(screening.vendor_id)

        return Response(
            {
                'status': 'ok',
                'screening_id': screening.id,
                'screening_status': screening.status,
            }
        )


class InternalVendorKYCVerificationReviewView(InternalVendorKYCView):
    def post(self, request, vendor_id, action):
        if not settings.VENDOR_KYC_SERVICE_ENABLED or not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if action not in {'approve', 'reject'}:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        review = self.review_data(request, require_reason=action == 'reject')
        with transaction.atomic():
            verification = get_object_or_404(
                VendorKYCVerification.objects.select_for_update().select_related('vendor'),
                vendor_id=vendor_id,
            )
            if action == 'approve' and not verification.all_required_verified():
                return Response(
                    {'detail': 'All required documents must be present, unexpired, and verified before approving KYC.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            previous_status = verification.overall_status
            verification.overall_status = 'VERIFIED' if action == 'approve' else 'REJECTED'
            verification.verified_by_user_id = review['actor_id'] if action == 'approve' else None
            verification.verified_by_email = review['actor_email'] if action == 'approve' else ''
            verification.verified_at = timezone.now() if action == 'approve' else None
            verification.rejected_at = timezone.now() if action == 'reject' else None
            verification.rejection_summary = '' if action == 'approve' else review['reason'].strip()
            verification.updated_at = timezone.now()
            verification.save(
                update_fields=[
                    'overall_status',
                    'verified_by_user_id',
                    'verified_by_email',
                    'verified_at',
                    'rejected_at',
                    'rejection_summary',
                    'updated_at',
                ]
            )
            VendorKYCAuditLog.objects.create(
                vendor=verification.vendor,
                verification=verification,
                actor_user_id=review['actor_id'],
                actor_email=review['actor_email'],
                action=f'VERIFICATION_{action.upper()}',
                previous_status=previous_status,
                status=verification.overall_status,
                reason=verification.rejection_summary,
                request_id=review['request_id'],
            )
        return Response(
            {
                'status': 'ok',
                'vendor_id': vendor_id,
                'overall_status': verification.overall_status,
            }
        )


class VendorKYCFeatureMixin:
    def dispatch(self, request, *args, **kwargs):
        if not settings.VENDOR_KYC_SERVICE_ENABLED:
            return proxy_to_monolith(request)
        return super().dispatch(request, *args, **kwargs)


class VendorKYCDocumentListView(VendorKYCFeatureMixin, generics.ListCreateAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]
    serializer_class = VendorKYCDocumentSerializer

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return VendorKYCDocument.objects.filter(vendor=self.get_vendor()).order_by('-created_at', '-id')

    def perform_create(self, serializer):
        vendor = self.get_vendor()
        serializer.save()
        verification, _ = VendorKYCVerification.objects.get_or_create(vendor=vendor)
        if verification.submitted_at is None:
            verification.submitted_at = timezone.now()
            verification.save(update_fields=['submitted_at', 'updated_at'])
        verification.refresh_status()


class VendorKYCDocumentDetailView(VendorKYCFeatureMixin, generics.RetrieveDestroyAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VendorKYCDocumentSerializer
    lookup_url_kwarg = 'document_id'

    def get_queryset(self):
        return VendorKYCDocument.objects.filter(vendor__owner_user_id=self.request.user.id)

    def perform_destroy(self, instance):
        vendor = instance.vendor
        instance.file.delete(save=False)
        instance.delete()
        verification = VendorKYCVerification.objects.filter(vendor=vendor).first()
        if verification:
            verification.refresh_status()


class VendorKYCVerificationListView(VendorKYCFeatureMixin, generics.ListAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VendorKYCVerificationSerializer

    def get_queryset(self):
        return VendorKYCVerification.objects.filter(vendor__owner_user_id=self.request.user.id).select_related('vendor')


class VendorKYCRefreshView(VendorKYCFeatureMixin, APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        verification, _ = VendorKYCVerification.objects.get_or_create(vendor=vendor)
        kyc_status = verification.refresh_status()
        return Response({'message': 'KYC status refreshed', 'kyc_status': kyc_status})


class VendorTaxInfoListView(VendorKYCFeatureMixin, generics.ListCreateAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = VendorTaxInfoSerializer

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return VendorTaxInfo.objects.filter(vendor=self.get_vendor())

    def create(self, request, *args, **kwargs):
        vendor = self.get_vendor()
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            instance, created = VendorTaxInfo.objects.get_or_create(vendor=vendor)
            serializer.instance = instance
            serializer.save()
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class VendorTaxInfoDetailView(VendorKYCFeatureMixin, generics.RetrieveUpdateDestroyAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = VendorTaxInfoSerializer
    lookup_url_kwarg = 'tax_info_id'

    def get_queryset(self):
        return VendorTaxInfo.objects.filter(vendor__owner_user_id=self.request.user.id)

    def perform_destroy(self, instance):
        if instance.tax_clearance_certificate:
            instance.tax_clearance_certificate.delete(save=False)
        instance.delete()


class VendorSanctionsScreeningListView(VendorKYCFeatureMixin, generics.ListAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VendorSanctionsScreeningSerializer

    def get_queryset(self):
        return VendorSanctionsScreening.objects.filter(
            vendor__owner_user_id=self.request.user.id
        ).order_by('-screened_at', '-id')


class VendorSanctionsScreeningDetailView(VendorKYCFeatureMixin, generics.RetrieveAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = VendorSanctionsScreeningSerializer
    lookup_url_kwarg = 'screening_id'

    def get_queryset(self):
        return VendorSanctionsScreening.objects.filter(vendor__owner_user_id=self.request.user.id)


class VendorNotificationListView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        notifications = VendorNotificationOutbox.objects.filter(vendor=vendor).order_by('-created_at', '-id')[:100]
        return Response(VendorNotificationSerializer(notifications, many=True).data)

    def patch(self, request):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        serializer = VendorNotificationMarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = VendorNotificationOutbox.objects.filter(
            vendor=vendor,
            id__in=serializer.validated_data['notification_ids'],
            read_at__isnull=True,
        ).update(read_at=timezone.now())
        return Response({'updated': updated})


class VendorNotificationPreferenceListView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        preferences = VendorNotificationPreference.objects.filter(
            owner_user_id=request.user.id,
            active=True,
        )
        return Response(VendorNotificationPreferenceSerializer(preferences, many=True).data)

    def put(self, request):
        get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        serializer = VendorNotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preference, _ = VendorNotificationPreference.objects.update_or_create(
            owner_user_id=request.user.id,
            channel=serializer.validated_data['channel'],
            category=serializer.validated_data['category'],
            defaults={
                'active': serializer.validated_data['active'],
                'reason': serializer.validated_data['reason'],
            },
        )
        return Response(VendorNotificationPreferenceSerializer(preference).data)


class InternalVendorNotificationPreferenceView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def authorize(self, request):
        supplied_token = request.headers.get('X-Vendor-Notification-Token', '')
        return bool(settings.VENDOR_NOTIFICATION_TOKEN) and hmac.compare_digest(
            supplied_token,
            settings.VENDOR_NOTIFICATION_TOKEN,
        )

    def get(self, request, owner_user_id):
        if not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        channel = request.query_params.get('channel')
        category = request.query_params.get('category')
        if channel not in dict(VendorNotificationPreference.CHANNELS):
            raise ValidationError({'channel': ['A valid channel is required.']})
        if category not in ('MARKETING', 'PROMO', 'ALL'):
            raise ValidationError({'category': ['A valid marketing category is required.']})
        preference_category = 'MARKETING' if category == 'PROMO' else category
        opted_out = VendorNotificationPreference.objects.filter(
            owner_user_id=owner_user_id,
            channel=channel,
            category__in=[preference_category, 'ALL'],
            active=True,
        ).exists()
        return Response({'is_opted_out': opted_out})

    def put(self, request, owner_user_id):
        if not self.authorize(request):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = VendorNotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preference, _ = VendorNotificationPreference.objects.update_or_create(
            owner_user_id=owner_user_id,
            channel=serializer.validated_data['channel'],
            category=serializer.validated_data['category'],
            defaults={
                'active': serializer.validated_data['active'],
                'reason': serializer.validated_data['reason'],
            },
        )
        return Response(VendorNotificationPreferenceSerializer(preference).data)


class VendorComplianceDashboardView(VendorKYCFeatureMixin, APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        verification = VendorKYCVerification.objects.filter(vendor=vendor).first()
        missing_documents = verification.missing_required_types() if verification else []
        expiring_documents = []
        for document in vendor.kyc_documents.filter(
            is_required=True,
            verification_status='VERIFIED',
            expiry_date__isnull=False,
            expiry_date__lte=timezone.localdate() + timedelta(days=30),
        ):
            expiring_documents.append(
                {
                    'document_type': document.document_type,
                    'document_type_display': document.get_document_type_display(),
                    'expiry_date': document.expiry_date,
                    'days_until_expiry': document.days_until_expiry(),
                }
            )

        latest_screening = vendor.sanctions_screenings.first()
        pep_screening = vendor.sanctions_screenings.filter(screening_type='PEP').first()
        tax_info = VendorTaxInfo.objects.filter(vendor=vendor).first()
        return Response(
            {
                'vendor_id': vendor.id,
                'business_name': vendor.business_name,
                'kyc_status': vendor.kyc_status(),
                'is_kyc_verified': vendor.is_kyc_verified(),
                'missing_documents': missing_documents,
                'expiring_documents': expiring_documents,
                'sanctions_status': latest_screening.status if latest_screening else 'PENDING',
                'pep_status': pep_screening.status if pep_screening else 'PENDING',
                'zimra_pin_verified': tax_info.zimra_pin_verified if tax_info else False,
                'should_be_suspended': vendor.should_be_suspended(),
                'next_review_date': verification.next_review_date if verification else None,
            }
        )


class VendorMenuCategoryListCreateView(generics.ListAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RestaurantMenuCategorySerializer

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return (
            RestaurantMenuCategory.objects.filter(vendor_id=self.get_vendor().id, is_active=True)
            .order_by('order', 'name')
        )

    def get_serializer_context(self):
        vendor = self.get_vendor()
        categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
        return {
            **super().get_serializer_context(),
            'vendor': vendor,
            'include_unavailable': True,
            'categories': {category.id: category.name for category in categories},
        }

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        data = self.get_serializer(queryset, many=True).data
        return Response({'results': data, 'count': len(data)})


class VendorMenuCategoryCreateView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        serializer = RestaurantMenuCategoryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        last_order = (
            RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
            .order_by('-order')
            .values_list('order', flat=True)
            .first()
        )
        category = serializer.save(
            vendor_id=vendor.id,
            order=serializer.validated_data.get('order', (last_order or 0) + 1),
        )
        return Response(
            {
                'message': 'Category created',
                'category': RestaurantMenuCategorySerializer(
                    category,
                    context={'request': request, 'vendor': vendor},
                ).data,
            },
            status=201,
        )


class VendorMenuCategoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RestaurantMenuCategorySerializer
    lookup_url_kwarg = 'category_id'

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return RestaurantMenuCategory.objects.filter(vendor_id=self.get_vendor().id)

    def get_serializer_context(self):
        vendor = self.get_vendor()
        categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
        return {
            **super().get_serializer_context(),
            'vendor': vendor,
            'include_unavailable': True,
            'categories': {category.id: category.name for category in categories},
        }

    def get_serializer_class(self):
        if self.request.method in {'PUT', 'PATCH'}:
            return RestaurantMenuCategoryInputSerializer
        return super().get_serializer_class()

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        category = self.get_object()
        serializer = self.get_serializer(category, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return Response(
            RestaurantMenuCategorySerializer(category, context=self.get_serializer_context()).data
        )

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class LegacyVendorMenuNumericDetailView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def _vendor(self, request):
        return get_object_or_404(VendorProfile, owner_user_id=request.user.id)

    def get(self, request, item_id):
        vendor = self._vendor(request)
        item = RestaurantMenuItem.objects.filter(id=item_id, vendor_id=vendor.id).prefetch_related(
            'options',
            'images',
        ).first()
        if item:
            categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
            context = {
                'request': request,
                'vendor': vendor,
                'include_unavailable': True,
                'categories': {category.id: category.name for category in categories},
            }
            return Response(RestaurantMenuItemSerializer(item, context=context).data)
        category = get_object_or_404(RestaurantMenuCategory, id=item_id, vendor_id=vendor.id)
        return Response(
            RestaurantMenuCategorySerializer(
                category,
                context={
                    'request': request,
                    'vendor': vendor,
                    'include_unavailable': True,
                    'categories': {category.id: category.name},
                },
            ).data
        )

    def put(self, request, item_id):
        return self._update_category(request, item_id, partial=False)

    def patch(self, request, item_id):
        return self._update_category(request, item_id, partial=True)

    def _update_category(self, request, category_id, *, partial):
        vendor = self._vendor(request)
        category = get_object_or_404(RestaurantMenuCategory, id=category_id, vendor_id=vendor.id)
        serializer = RestaurantMenuCategoryInputSerializer(category, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        return Response(
            RestaurantMenuCategorySerializer(
                category,
                context={
                    'request': request,
                    'vendor': vendor,
                    'include_unavailable': True,
                    'categories': {category.id: category.name},
                },
            ).data
        )

    def delete(self, request, item_id):
        vendor = self._vendor(request)
        category = get_object_or_404(RestaurantMenuCategory, id=item_id, vendor_id=vendor.id)
        category.is_active = False
        category.save(update_fields=['is_active'])
        return Response(status=204)


class VendorMenuItemListCreateView(generics.ListCreateAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RestaurantMenuItemSerializer

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return (
            RestaurantMenuItem.objects.filter(vendor_id=self.get_vendor().id)
            .prefetch_related('options', 'images')
        )

    def get_serializer_context(self):
        vendor = self.get_vendor()
        categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
        return {
            **super().get_serializer_context(),
            'vendor': vendor,
            'categories': {category.id: category.name for category in categories},
        }


class VendorMenuItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = RestaurantMenuItemSerializer
    lookup_url_kwarg = 'item_id'

    def get_vendor(self):
        return get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)

    def get_queryset(self):
        return (
            RestaurantMenuItem.objects.filter(vendor_id=self.get_vendor().id)
            .prefetch_related('options', 'images')
        )

    def get_serializer_context(self):
        vendor = self.get_vendor()
        categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
        return {
            **super().get_serializer_context(),
            'vendor': vendor,
            'categories': {category.id: category.name for category in categories},
        }

    def perform_destroy(self, instance):
        instance.is_available = False
        instance.updated_at = timezone.now()
        instance.save(update_fields=['is_available', 'updated_at'])


class LegacyVendorMenuItemCreateView(VendorMenuItemListCreateView):
    def create(self, request, *args, **kwargs):
        vendor = self.get_vendor()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category_id = serializer.validated_data.get('category_id')
        if not category_id or not RestaurantMenuCategory.objects.filter(
            id=category_id,
            vendor_id=vendor.id,
            is_active=True,
        ).exists():
            return Response({'error': 'Category not found'}, status=404)
        item = serializer.save()
        return Response(
            {'message': 'Menu item created', 'item': self.get_serializer(item).data},
            status=201,
        )


class LegacyVendorMenuItemUpdateView(VendorMenuItemDetailView):
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        item = serializer.save()
        return Response({'message': 'Menu item updated', 'item': self.get_serializer(item).data})


class PublicVendorMenuView(generics.ListAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = RestaurantMenuCategorySerializer

    def get_vendor(self):
        return get_object_or_404(VendorProfile, id=self.kwargs['vendor_id'], is_active=True)

    def get_queryset(self):
        vendor = self.get_vendor()
        return RestaurantMenuCategory.objects.filter(
            vendor_id=vendor.id,
            is_active=True,
        ).order_by('order', 'name')

    def get_serializer_context(self):
        vendor = self.get_vendor()
        categories = RestaurantMenuCategory.objects.filter(vendor_id=vendor.id)
        return {
            **super().get_serializer_context(),
            'vendor': vendor,
            'categories': {category.id: category.name for category in categories},
        }

    def list(self, request, *args, **kwargs):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class LegacyMenuImageUploadView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, item_id):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        item = get_object_or_404(RestaurantMenuItem, id=item_id, vendor_id=vendor.id)
        uploads = request.FILES.getlist('images')
        if not uploads:
            return Response({'error': 'No images provided'}, status=400)

        created = []
        errors = []
        next_order = item.images.count()
        for upload in uploads:
            error = validate_image_upload(upload)
            if error:
                errors.append(f'{upload.name}: {error}')
                continue
            image = RestaurantMenuItemImage.objects.create(
                menu_item=item,
                image=upload,
                sort_order=next_order,
            )
            created.append(RestaurantMenuItemImageSerializer(image, context={'request': request}).data)
            next_order += 1
        return Response(
            {
                'message': f'{len(created)} image(s) uploaded',
                'created': created,
                'errors': errors,
            }
        )


class LegacyMenuSingleImageUploadView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, item_id):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        item = get_object_or_404(RestaurantMenuItem, id=item_id, vendor_id=vendor.id)
        upload = request.FILES.get('image')
        if not upload:
            return Response({'error': 'No image provided'}, status=400)
        error = validate_image_upload(upload)
        if error:
            return Response({'error': error}, status=400)
        previous = item.image
        item.image = default_storage.save(f'menu_items/{basename(upload.name)}', upload)
        item.updated_at = timezone.now()
        item.save(update_fields=['image', 'updated_at'])
        if previous and previous != item.image:
            default_storage.delete(previous)
        return Response(
            {
                'message': 'Image uploaded',
                'image': public_media_url(request, f'/api/v1/vendors/menu/{item.id}/image/'),
            }
        )


class LegacyMenuImageDeleteView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, item_id):
        vendor = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        item = get_object_or_404(RestaurantMenuItem, id=item_id, vendor_id=vendor.id)
        image_id = request.data.get('image_id')
        if not image_id:
            return Response({'error': 'image_id is required'}, status=400)
        image = get_object_or_404(RestaurantMenuItemImage, id=image_id, menu_item=item)
        image_name = image.image.name
        image.delete()
        default_storage.delete(image_name)
        return Response({'message': 'Image deleted'}, status=200)


class LegacyMenuImageFileView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, image_id):
        image = get_object_or_404(
            RestaurantMenuItemImage.objects.select_related('menu_item'),
            id=image_id,
            menu_item__vendor_id__in=public_vendor_profiles().values('id'),
        )
        return _image_file_response(image.image.name)


class PublicProductImageFileView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, image_id):
        image = get_object_or_404(
            ProductImage.objects.select_related('product'),
            id=image_id,
            product__vendor_id__in=public_vendor_profiles().values('id'),
        )
        return _image_file_response(image.image.name)


class LegacyMenuItemImageFileView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, item_id):
        item = get_object_or_404(
            RestaurantMenuItem.objects.filter(
                vendor_id__in=public_vendor_profiles().values('id')
            ),
            id=item_id,
        )
        if not item.image:
            raise NotFound()
        return _image_file_response(item.image)


def _image_file_response(name):
    if not default_storage.exists(name):
        raise NotFound('Image file not found.')
    content_type = guess_type(name)[0] or 'application/octet-stream'
    response = FileResponse(default_storage.open(name, 'rb'), content_type=content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'public, max-age=3600'
    return response


class UnsupportedLegacyMenuPathView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        return Response({'error': 'This menu endpoint is not supported by vendor-api.'}, status=404)


class PublicMenuItemListView(generics.ListAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicRestaurantMenuItemSerializer

    def get_queryset(self):
        active_vendor_ids = public_vendor_profiles().values_list('id', flat=True)
        queryset = RestaurantMenuItem.objects.filter(vendor_id__in=active_vendor_ids).prefetch_related('options')
        vendor_id = self.request.query_params.get('vendor_id')
        if vendor_id:
            queryset = queryset.filter(vendor_id=vendor_id)
        if self.request.query_params.get('available') == 'true':
            queryset = queryset.filter(is_available=True)
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        vendor_ids = set(self.get_queryset().values_list('vendor_id', flat=True))
        context['vendors'] = {
            vendor.id: vendor
            for vendor in public_vendor_profiles().filter(id__in=vendor_ids)
        }
        return context


class PublicMenuItemDetailView(generics.RetrieveAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicRestaurantMenuItemSerializer
    lookup_url_kwarg = 'item_id'

    def get_queryset(self):
        active_vendor_ids = public_vendor_profiles().values_list('id', flat=True)
        return RestaurantMenuItem.objects.filter(vendor_id__in=active_vendor_ids).prefetch_related('options')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        vendor_id = self.get_object().vendor_id
        context['vendors'] = {
            vendor.id: vendor
            for vendor in public_vendor_profiles().filter(id=vendor_id)
        }
        return context


class MyVendorProfileView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    RESTRICTED_FIELDS = frozenset({'delivery_fee', 'minimum_order', 'delivery_radius_km'})

    def get(self, request):
        profile = get_object_or_404(VendorProfile, owner_user_id=request.user.id)
        return Response(VendorProfileSerializer(profile).data)

    @transaction.atomic
    def put(self, request):
        provided_restricted = self.RESTRICTED_FIELDS.intersection(request.data.keys())
        if provided_restricted:
            fields = ', '.join(sorted(provided_restricted))
            return Response(
                {'error': f'Fields not editable by vendors: {fields}'},
                status=400,
            )
        profile = get_object_or_404(VendorProfile.objects.select_for_update(), owner_user_id=request.user.id)
        serializer = VendorProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        request_catalog_sync(profile.id)
        return Response(serializer.data)


class VendorAvailabilityView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = VendorAvailabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = (
            VendorProfile.objects.select_for_update()
            .filter(owner_user_id=request.user.id)
            .first()
        )
        if profile is None:
            return Response({'error': 'Vendor profile not found'}, status=404)

        is_open = serializer.validated_data['is_open']
        profile.is_open = is_open
        profile.save(update_fields=['is_open'])
        request_catalog_sync(profile.id)
        return Response(
            {
                'is_open': is_open,
                'message': f'Your business is now {"open" if is_open else "closed"}',
            }
        )


class VendorProductListCreateView(generics.ListCreateAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProductSerializer

    def get_queryset(self):
        profile = get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)
        return Product.objects.filter(vendor=profile).select_related('category').prefetch_related('images')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return ProductCreateUpdateSerializer
        return ProductSerializer

    def perform_create(self, serializer):
        profile = get_object_or_404(VendorProfile, owner_user_id=self.request.user.id)
        if not profile.is_active or not profile.is_verified:
            raise PermissionDenied('Only an active, verified vendor can manage catalog products.')
        serializer.save(vendor=profile)


class VendorProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated, IsVendorOwner]
    serializer_class = ProductSerializer
    lookup_url_kwarg = 'product_id'

    def get_queryset(self):
        return (
            Product.objects.filter(vendor__owner_user_id=self.request.user.id)
            .select_related('category', 'vendor')
            .prefetch_related('images')
        )

    def get_serializer_class(self):
        if self.request.method in {'PUT', 'PATCH'}:
            return ProductCreateUpdateSerializer
        return ProductSerializer

    @transaction.atomic
    def perform_update(self, serializer):
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        instance.delete()


class PublicVendorListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicVendorSerializer
    queryset = public_vendor_profiles().order_by('business_name')


class PublicVendorLookupView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        raw_ids = request.query_params.get('ids', '')
        values = raw_ids.split(',')
        if any(not value.strip() for value in values):
            raise ValidationError({'ids': 'Provide comma-separated positive integers.'})
        try:
            vendor_ids = [int(value) for value in values]
        except ValueError:
            raise ValidationError({'ids': 'Provide comma-separated positive integers.'}) from None
        if not vendor_ids or any(vendor_id <= 0 for vendor_id in vendor_ids):
            raise ValidationError({'ids': 'Provide at least one positive vendor ID.'})
        if len(vendor_ids) > 200:
            raise ValidationError({'ids': 'Request no more than 200 vendor IDs at a time.'})

        vendors = (
            VendorProfile.objects.filter(id__in=set(vendor_ids))
            .exclude(sanctions_screenings__status='CONFIRMED_MATCH')
            .values('id', 'business_name')
        )
        return Response({'results': list(vendors)})


class PublicProductLookupView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        raw_ids = request.query_params.get('ids', '')
        values = raw_ids.split(',')
        if any(not value.strip() for value in values):
            raise ValidationError({'ids': 'Provide comma-separated positive integers.'})
        try:
            product_ids = [int(value) for value in values]
        except ValueError:
            raise ValidationError({'ids': 'Provide comma-separated positive integers.'}) from None
        if not product_ids or any(product_id <= 0 for product_id in product_ids):
            raise ValidationError({'ids': 'Provide at least one positive product ID.'})
        if len(product_ids) > 200:
            raise ValidationError({'ids': 'Request no more than 200 product IDs at a time.'})

        products = (
            Product.objects.filter(id__in=set(product_ids))
            .exclude(vendor__sanctions_screenings__status='CONFIRMED_MATCH')
            .prefetch_related('images')
        )
        results = []
        for product in products:
            image = product.images.first()
            results.append(
                {
                    'id': product.id,
                    'name': product.name,
                    'price': str(product.price),
                    'image': (
                        public_media_url(
                            request,
                            f'/api/v1/vendors/product-images/{image.id}/file/',
                        )
                        if image
                        else None
                    ),
                }
            )
        return Response({'results': results})


class PublicVendorDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicVendorSerializer
    queryset = public_vendor_profiles()
    lookup_url_kwarg = 'vendor_id'


class PublicCategoryListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = CatalogCategorySerializer
    queryset = CatalogCategory.objects.filter(is_active=True).order_by('name')


class PublicProductListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PublicProductSerializer
    queryset = Product.objects.filter(
        is_available=True,
        vendor__in=public_vendor_profiles(),
    ).select_related('category', 'vendor').prefetch_related('images')


class MarketplaceProductPagination(PageNumberPagination):
    page_size = 20


class MarketplaceProductsQuerysetMixin:
    serializer_class = MarketplaceProductSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = MarketplaceProductPagination
    search_fields = ('name', 'description', 'brand', 'barcode')
    ordering_fields = {'price', 'created_at', 'name', 'rating', 'is_popular'}

    def _boolean(self, name):
        value = self.request.query_params.get(name)
        if value is None or value == '':
            return None
        if value.lower() in ('true', '1'):
            return True
        if value.lower() in ('false', '0'):
            return False
        raise ValidationError({name: 'Use true or false.'})

    def _decimal(self, name):
        value = self.request.query_params.get(name)
        if value is None or value == '':
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            raise ValidationError({name: 'Enter a valid number.'}) from None

    def _search(self, queryset, value):
        for term in value.split():
            matching_fields = Q()
            for field in self.search_fields:
                matching_fields |= Q(**{f'{field}__icontains': term})
            queryset = queryset.filter(matching_fields)
        return queryset

    def get_queryset(self):
        queryset = (
            Product.objects.filter(
                is_available=True,
                vendor__in=public_vendor_profiles(),
            )
            .select_related('category', 'vendor')
            .prefetch_related('images')
        )

        in_stock = self._boolean('in_stock')
        if in_stock is not False:
            queryset = queryset.filter(Q(is_inventory_tracked=False) | Q(quantity_on_hand__gt=0))

        for field in ('product_type',):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})

        for field in ('category', 'vendor'):
            value = self.request.query_params.get(field)
            if value:
                try:
                    queryset = queryset.filter(**{field: int(value)})
                except ValueError:
                    raise ValidationError({field: 'Enter a valid integer.'}) from None

        discounted = self._boolean('is_discounted')
        if discounted is None:
            discounted = self._boolean('discount')
        if discounted is not None:
            queryset = queryset.filter(is_discounted=discounted)

        min_price = self._decimal('min_price')
        max_price = self._decimal('max_price')
        if min_price is not None:
            queryset = queryset.filter(price__gte=min_price)
        if max_price is not None:
            queryset = queryset.filter(price__lte=max_price)

        for parameter, field in (
            ('pickup', 'vendor__pickup_available'),
            ('open_now', 'vendor__is_open'),
            ('free_delivery', 'vendor__delivery_fee'),
        ):
            enabled = self._boolean(parameter)
            if enabled:
                queryset = queryset.filter(**{field: 0 if parameter == 'free_delivery' else True})

        rating = self._decimal('rating')
        if rating is not None:
            queryset = queryset.filter(rating__gte=rating)

        max_time = self.request.query_params.get('max_time')
        if max_time:
            try:
                max_time_value = int(max_time)
            except ValueError:
                raise ValidationError({'max_time': 'Enter a valid integer.'}) from None
            queryset = queryset.filter(vendor__preparation_time_mins__lte=max_time_value)

        popular = self._boolean('popular')
        if popular is not None:
            queryset = queryset.filter(is_popular=popular)

        if self._boolean('new'):
            queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=30))

        search = self.request.query_params.get('search', '')
        if search:
            queryset = self._search(queryset, search)
        query = self.request.query_params.get('q', '')
        if query:
            queryset = self._search(queryset, query)

        if (
            getattr(self, 'exclude_restaurant_products', False)
            and not self.request.query_params.get('vendor')
            and not search
            and not query
        ):
            queryset = queryset.exclude(vendor__category__in=['RESTAURANT', 'FAST_FOOD'])

        ordering = self.request.query_params.get('ordering', '')
        requested_ordering = []
        for field in ordering.split(','):
            name = field.removeprefix('-')
            if name in self.ordering_fields:
                requested_ordering.append(field)
        if requested_ordering:
            queryset = queryset.order_by(*requested_ordering)
        else:
            queryset = queryset.order_by('-is_popular', F('rating').desc(nulls_last=True), 'name', 'id')
        return queryset


class MarketplaceProductListView(MarketplaceProductsQuerysetMixin, generics.ListAPIView):
    exclude_restaurant_products = True


class MarketplaceProductDetailView(MarketplaceProductsQuerysetMixin, generics.RetrieveAPIView):
    exclude_restaurant_products = False
    pagination_class = None
    lookup_url_kwarg = 'product_id'


class MarketplaceCategoryQuerysetMixin:
    permission_classes = [permissions.AllowAny]
    serializer_class = MarketplaceCategorySerializer

    def get_queryset(self):
        active_children = CatalogCategory.objects.filter(is_active=True).order_by('sort_order', 'name', 'id')
        queryset = (
            CatalogCategory.objects.filter(is_active=True, parent=None, vendor=None)
            .order_by('sort_order', 'name', 'id')
            .prefetch_related(Prefetch('children', queryset=active_children, to_attr='active_children'))
        )
        return queryset

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'category_depth': 1}


class MarketplaceCategoryListView(MarketplaceCategoryQuerysetMixin, generics.ListAPIView):
    pagination_class = None


class MarketplaceCategoryDetailView(MarketplaceCategoryQuerysetMixin, generics.RetrieveAPIView):
    pagination_class = None
    lookup_url_kwarg = 'category_id'
