import csv
import hmac
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import VendorPriceChange, VendorPromotion, VendorScorecard

MAX_PAGE_SIZE = 200


class _IntegrityView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]

    def initial(self, request, *args, **kwargs):
        supplied = request.headers.get('X-Vendor-Admin-Token', '')
        self._authorized = (
            settings.VENDOR_INTEGRITY_SERVICE_ENABLED
            and bool(settings.VENDOR_ADMIN_TOKEN)
            and hmac.compare_digest(supplied, settings.VENDOR_ADMIN_TOKEN)
        )
        super().initial(request, *args, **kwargs)

    def _deny(self):
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    @staticmethod
    def _page(request):
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            size = min(max(int(request.query_params.get('page_size', 50)), 1), MAX_PAGE_SIZE)
        except ValueError:
            page, size = 1, 50
        return page, size

    @staticmethod
    def _actor(data):
        actor_id = data.get('actor_id')
        actor_email = data.get('actor_email')
        if type(actor_id) is not int or actor_id < 1 or not isinstance(actor_email, str) or not actor_email:
            return None
        return actor_id, actor_email


def _vendor_fields(vendor):
    return {'vendor_id': vendor.id, 'vendor_name': vendor.business_name}


def _price_change(row):
    return {
        'id': row.id,
        **_vendor_fields(row.vendor),
        'content_type': row.content_type,
        'object_id': row.object_id,
        'field_name': row.field_name,
        'old_price': str(row.old_price),
        'new_price': str(row.new_price),
        'change_pct': str(row.change_pct),
        'is_price_increase': row.is_price_increase,
        'reason': row.reason,
        'requires_approval': row.requires_approval,
        'is_approved': row.is_approved,
        'changed_by': None,
        'approved_by': row.approved_by_email or None,
        'approved_at': row.approved_at,
        'created_at': row.created_at,
    }


def _promotion(row):
    return {
        'id': row.id,
        **_vendor_fields(row.vendor),
        'code': row.code,
        'description': row.description,
        'discount_type': row.discount_type,
        'discount_value': str(row.discount_value),
        'min_order_amount': str(row.min_order_amount),
        'valid_from': row.valid_from,
        'valid_until': row.valid_until,
        'usage_limit': row.usage_limit,
        'times_used': row.times_used,
        'status': row.status,
        'is_active': row.is_active,
        'rejection_reason': row.rejection_reason,
        'approved_by': row.approved_by_email or None,
        'approved_at': row.approved_at,
        'created_at': row.created_at,
    }


SCORECARD_SORTS = {
    'compliance_score',
    'cancellation_rate',
    'on_time_prep_rate',
    'avg_rating_30d',
    'revenue_30d',
    'refund_dispute_rate',
    'fulfillment_rate',
    'acceptance_rate',
}


def _scorecard(row):
    data = {
        field.name: (str(getattr(row, field.name)) if hasattr(getattr(row, field.name), 'quantize') else getattr(row, field.name))
        for field in VendorScorecard._meta.concrete_fields
        if field.name not in {'id', 'vendor'}
    }
    return {'id': row.id, **_vendor_fields(row.vendor), **data}


class InternalPriceChangeListView(_IntegrityView):
    def get(self, request):
        if not self._authorized:
            return self._deny()
        queryset = VendorPriceChange.objects.select_related('vendor')
        if request.query_params.get('pending') in {'1', 'true', 'True'}:
            queryset = queryset.filter(requires_approval=True, is_approved__isnull=True)
        period = request.query_params.get('period', '')
        days = {'7d': 7, '30d': 30, '90d': 90}.get(period)
        if days:
            queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=days))
        page, size = self._page(request)
        total = queryset.count()
        rows = queryset[(page - 1) * size : page * size]
        return Response({'count': total, 'page': page, 'results': [_price_change(r) for r in rows]})


class InternalPriceChangeReviewView(_IntegrityView):
    def post(self, request, change_id):
        if not self._authorized:
            return self._deny()
        data = request.data if isinstance(request.data, dict) else {}
        actor = self._actor(data)
        if actor is None:
            return Response({'detail': 'Actor attributes are required.'}, status=status.HTTP_400_BAD_REQUEST)
        approve = data.get('approve')
        if type(approve) is not bool:
            return Response({'approve': ['Must be a boolean.']}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            row = VendorPriceChange.objects.select_for_update().select_related('vendor').filter(pk=change_id).first()
            if row is None:
                return Response({'detail': 'Price change not found.'}, status=status.HTTP_404_NOT_FOUND)
            if row.is_approved is not None:
                return Response({'detail': 'Price change was already reviewed.'}, status=status.HTTP_409_CONFLICT)
            row.is_approved = approve
            row.approved_by_user_id, row.approved_by_email = actor
            row.approved_at = timezone.now()
            row.save(update_fields=['is_approved', 'approved_by_user_id', 'approved_by_email', 'approved_at'])
        return Response(_price_change(row))


class InternalPromotionListView(_IntegrityView):
    def get(self, request):
        if not self._authorized:
            return self._deny()
        queryset = VendorPromotion.objects.select_related('vendor')
        wanted = request.query_params.get('status', 'ALL').upper()
        if wanted != 'ALL':
            queryset = queryset.filter(status=wanted)
        page, size = self._page(request)
        total = queryset.count()
        rows = queryset[(page - 1) * size : page * size]
        return Response({'count': total, 'page': page, 'results': [_promotion(r) for r in rows]})


class InternalPromotionReviewView(_IntegrityView):
    def post(self, request, promotion_id):
        if not self._authorized:
            return self._deny()
        data = request.data if isinstance(request.data, dict) else {}
        actor = self._actor(data)
        if actor is None:
            return Response({'detail': 'Actor attributes are required.'}, status=status.HTTP_400_BAD_REQUEST)
        approve = data.get('approve')
        if type(approve) is not bool:
            return Response({'approve': ['Must be a boolean.']}, status=status.HTTP_400_BAD_REQUEST)
        reason = str(data.get('reason', ''))[:500]
        if not approve and not reason.strip():
            return Response({'reason': ['A rejection reason is required.']}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            row = VendorPromotion.objects.select_for_update().select_related('vendor').filter(pk=promotion_id).first()
            if row is None:
                return Response({'detail': 'Promotion not found.'}, status=status.HTTP_404_NOT_FOUND)
            if row.status != 'PENDING_APPROVAL':
                return Response({'detail': 'Promotion is not pending approval.'}, status=status.HTTP_409_CONFLICT)
            row.status = 'ACTIVE' if approve else 'REJECTED'
            row.is_active = approve
            row.rejection_reason = '' if approve else reason
            row.approved_by_user_id, row.approved_by_email = actor
            row.approved_at = timezone.now()
            row.updated_at = row.approved_at
            row.save()
        return Response(_promotion(row))


class InternalPromotionSetActiveView(_IntegrityView):
    def post(self, request, promotion_id):
        if not self._authorized:
            return self._deny()
        data = request.data if isinstance(request.data, dict) else {}
        if self._actor(data) is None:
            return Response({'detail': 'Actor attributes are required.'}, status=status.HTTP_400_BAD_REQUEST)
        is_active = data.get('is_active')
        if type(is_active) is not bool:
            return Response({'is_active': ['Must be a boolean.']}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            row = VendorPromotion.objects.select_for_update().select_related('vendor').filter(pk=promotion_id).first()
            if row is None:
                return Response({'detail': 'Promotion not found.'}, status=status.HTTP_404_NOT_FOUND)
            if is_active and row.status != 'ACTIVE':
                return Response({'detail': 'Only approved promotions can be activated.'}, status=status.HTTP_409_CONFLICT)
            row.is_active = is_active
            row.updated_at = timezone.now()
            row.save(update_fields=['is_active', 'updated_at'])
        return Response(_promotion(row))


class InternalScorecardListView(_IntegrityView):
    def get(self, request):
        if not self._authorized:
            return self._deny()
        sort = request.query_params.get('sort', 'compliance_score')
        if sort not in SCORECARD_SORTS:
            sort = 'compliance_score'
        prefix = '-' if request.query_params.get('direction', 'asc') == 'desc' else ''
        queryset = VendorScorecard.objects.select_related('vendor').order_by(f'{prefix}{sort}', 'id')
        page, size = self._page(request)
        total = queryset.count()
        rows = queryset[(page - 1) * size : page * size]
        return Response({'count': total, 'page': page, 'results': [_scorecard(r) for r in rows]})


def _csv_cell(value):
    text = '' if value is None else str(value)
    return f"'{text}" if text[:1] in {'=', '+', '-', '@', '\t', '\r'} else text


class InternalPriceChangeCsvView(_IntegrityView):
    def get(self, request):
        if not self._authorized:
            return self._deny()
        days = {'7d': 7, '30d': 30, '90d': 90}.get(request.query_params.get('period', '30d'), 30)
        rows = VendorPriceChange.objects.select_related('vendor').filter(
            created_at__gte=timezone.now() - timedelta(days=days)
        )[:10000]
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="price-changes.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                'ID', 'Vendor', 'Content Type', 'Object ID', 'Old Price', 'New Price', 'Change %',
                'Direction', 'Requires Approval', 'Approved', 'Approved By', 'Date',
            ]
        )
        for row in rows:
            approved = '' if row.is_approved is None else ('Yes' if row.is_approved else 'No')
            values = (
                row.id, row.vendor.business_name, row.content_type, row.object_id, row.old_price,
                row.new_price, row.change_pct, 'Increase' if row.is_price_increase else 'Decrease',
                'Yes' if row.requires_approval else 'No', approved, row.approved_by_email,
                row.created_at.isoformat(),
            )
            writer.writerow([_csv_cell(v) for v in values])
        return response
