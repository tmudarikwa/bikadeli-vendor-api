import hmac
import logging
from typing import ClassVar

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    VendorProfile,
    VendorProfileAuditLog,
    VendorTaxInfo,
)

PAGE_SIZE = 20
logger = logging.getLogger(__name__)


def _masked_phone(vendor):
    try:
        phone = vendor.business_phone or ''
    except Exception:  # noqa: BLE001 - one undecryptable row must not fail the page
        return None, 'unreadable'
    return (f"{'*' * max(len(phone) - 4, 0)}{phone[-4:]}" if phone else ''), None


def _row(v):
    phone, phone_error = _masked_phone(v)
    return {
        'id': v.id,
        'user_id': v.owner_user_id,
        'email': v.business_email,
        'business_name': v.business_name,
        'category': v.category,
        'phone': phone,
        'phone_error': phone_error,
        'is_verified': v.is_verified,
        'is_active': v.is_active,
        'is_open': v.is_open,
        # Lifecycle, rating, order totals and commission are not owned by vendor-service yet.
        'lifecycle_state': None,
        'lifecycle_reason': '',
        'lifecycle_changed_at': None,
        'can_accept_orders': bool(v.is_active and v.is_verified and v.is_currently_open()),
        'rating': None,
        'total_orders': None,
        'commission_percentage': None,
        'created_at': v.created_at.isoformat(),
    }


class _AdminVendorView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]

    def initial(self, request, *args, **kwargs):
        supplied = request.headers.get('X-Vendor-Admin-Token', '')
        self._authorized = (
            settings.VENDOR_ADMIN_OPS_ENABLED
            and bool(settings.VENDOR_ADMIN_TOKEN)
            and hmac.compare_digest(supplied, settings.VENDOR_ADMIN_TOKEN)
        )
        super().initial(request, *args, **kwargs)

    def _deny(self):
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)


class InternalAdminVendorListView(_AdminVendorView):
    def get(self, request):
        if not self._authorized:
            return self._deny()
        params = request.query_params
        raw_page = params.get('page', '1')
        if not raw_page.isdigit() or int(raw_page) < 1:
            return Response({'error': 'Invalid page number'}, status=status.HTTP_400_BAD_REQUEST)
        page = int(raw_page)
        qs = VendorProfile.objects.defer('business_phone').order_by('-created_at', '-id')
        for param, field in (('verified', 'is_verified'), ('active', 'is_active')):
            if params.get(param) in {'true', 'false'}:
                qs = qs.filter(**{field: params[param] == 'true'})
        if params.get('category'):
            qs = qs.filter(category=params['category'])
        search = params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(business_name__icontains=search) | Q(business_email__icontains=search))
        total = qs.count()
        start = (page - 1) * PAGE_SIZE
        return Response(
            {
                'results': [_row(v) for v in qs[start : start + PAGE_SIZE]],
                'total': total,
                'page': page,
                'page_size': PAGE_SIZE,
            }
        )


class _ToggleView(_AdminVendorView):
    action = ''

    def post(self, request, vendor_id):
        if not self._authorized:
            return self._deny()
        actor_id = request.data.get('actor_id')
        actor_email = request.data.get('actor_email')
        if type(actor_id) is not int or actor_id < 1 or not isinstance(actor_email, str) or not actor_email:
            return Response({'detail': 'Admin actor is required.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            vendor = VendorProfile.objects.select_for_update().filter(id=vendor_id).first()
            if vendor is None:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
            previous_active = vendor.is_active
            response = self.apply(vendor, request.data)
            if response is not None:
                return response
            VendorProfileAuditLog.objects.create(
                vendor=vendor,
                actor_user_id=actor_id,
                actor_email=actor_email,
                action=self.action,
                previous_is_verified=vendor.is_verified,
                is_verified=vendor.is_verified,
                previous_is_active=previous_active,
                is_active=vendor.is_active,
                request_id=request.headers.get('X-Request-ID', '')[:100],
            )
        return Response(self.result(vendor))


class InternalAdminVendorToggleActiveView(_ToggleView):
    action = 'TOGGLE_ACTIVE'

    def apply(self, vendor, data):
        vendor.is_active = not vendor.is_active
        vendor.save(update_fields=['is_active'])

    def result(self, vendor):
        return {'status': 'ok', 'is_active': vendor.is_active}


class InternalAdminVendorToggleOpenView(_ToggleView):
    action = 'TOGGLE_OPEN'

    def apply(self, vendor, data):
        vendor.is_open = bool(data.get('is_open'))
        vendor.save(update_fields=['is_open'])

    def result(self, vendor):
        return {'status': 'ok', 'is_open': vendor.is_open}


class InternalVendorPayoutSafetyView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]

    def get(self, request, owner_user_id):
        supplied = request.headers.get('X-Vendor-Admin-Token', '')
        if (
            not settings.VENDOR_PAYOUT_SAFETY_ENABLED
            or not settings.VENDOR_ADMIN_TOKEN
            or not hmac.compare_digest(supplied, settings.VENDOR_ADMIN_TOKEN)
        ):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        profile = VendorProfile.objects.filter(owner_user_id=owner_user_id).first()
        if profile is None:
            response = Response(
                {
                    'vendor_user_id': owner_user_id,
                    'eligible': False,
                    'holds': ['NO_VENDOR_PROFILE'],
                }
            )
            response['Cache-Control'] = 'no-store'
            return response

        try:
            identity_response = httpx.post(
                f'{settings.IDENTITY_SERVICE_URL.rstrip("/")}/internal/v1/identity/vendors/validate/',
                json={'user_id': owner_user_id},
                headers={
                    'X-Identity-User-Lookup-Token': settings.IDENTITY_USER_LOOKUP_TOKEN,
                },
                timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.RequestError:
            logger.exception(
                'Identity service unavailable during payout safety check for vendor user %s',
                owner_user_id,
            )
            return Response(
                {'error': 'Vendor identity status is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if identity_response.status_code != status.HTTP_200_OK:
            logger.error(
                'Identity service failed payout safety check for vendor user %s with status %s',
                owner_user_id,
                identity_response.status_code,
            )
            return Response(
                {'error': 'Vendor identity status is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            identity_data = identity_response.json()
        except ValueError:
            logger.error(
                'Identity service returned invalid payout safety JSON for vendor user %s',
                owner_user_id,
            )
            return Response(
                {'error': 'Vendor identity status is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if (
            not isinstance(identity_data, dict)
            or identity_data.get('user_id') != owner_user_id
            or type(identity_data.get('is_active_vendor')) is not bool
        ):
            logger.error(
                'Identity service returned invalid payout safety data for vendor user %s',
                owner_user_id,
            )
            return Response(
                {'error': 'Vendor identity status is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        holds = []
        if not profile.is_kyc_verified():
            holds.append('KYC_INCOMPLETE')
        if profile.should_be_suspended():
            holds.append('KYC_EXPIRED')
        try:
            tax_info = profile.tax_info
        except VendorTaxInfo.DoesNotExist:
            tax_info = None
        if (
            tax_info is not None
            and tax_info.tax_clearance_expiry is not None
            and tax_info.tax_clearance_expiry < timezone.now().date()
        ):
            holds.append('TAX_CLEARANCE_EXPIRED')
        latest_screening = profile.sanctions_screenings.order_by('-screened_at', '-id').first()
        if latest_screening and latest_screening.status == 'CONFIRMED_MATCH':
            holds.append('SANCTIONS_MATCH')
        if not profile.is_active or not identity_data['is_active_vendor']:
            holds.append('VENDOR_INACTIVE')

        response = Response(
            {
                'vendor_user_id': owner_user_id,
                'eligible': not holds,
                'holds': holds,
            }
        )
        response['Cache-Control'] = 'no-store'
        return response
