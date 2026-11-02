import csv
import io
import logging
import math
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point, Polygon
from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Exists, OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Cast, ExtractMinute, TruncDate
from django.http import StreamingHttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import (
    action,
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.decorators import (
    throttle_classes as throttle_classes_decorator,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from core.auth_cookies import set_auth_cookies
from core.feature_flags import flag_enabled
from core.models import IdempotencyKey
from core.throttles import LoginRateThrottle, MutationRateThrottle, RegisterRateThrottle
from orders.models import Order, OrderItem
from orders.serializers import OrderDetailSerializer, OrderSerializer, VendorOrderListSerializer

from .date_ranges import local_day_bounds
from .models import (
    Category,
    InventoryAdjustmentLog,
    MenuCategory,
    MenuItem,
    MenuItemImage,
    MenuItemOption,
    Product,
    Promotion,
    VendorKYCDocument,
    VendorKYCVerification,
    VendorProfile,
    VendorSanctionsScreening,
    VendorSupportTicket,
    VendorTaxInfo,
    VendorTicketMessage,
)
from .permissions import IsVendorUser
from .serializers import (
    AcceptOrderSerializer,
    AddCategorySerializer,
    AddMenuItemSerializer,
    CategorySerializer,
    InventoryAdjustmentLogSerializer,
    InventoryAdjustSerializer,
    InventoryItemSerializer,
    MenuCategorySerializer,
    MenuItemImageSerializer,
    MenuItemSerializer,
    ProductCreateUpdateSerializer,
    ProductSerializer,
    PromotionSerializer,
    PublicVendorSerializer,
    RejectOrderSerializer,
    ToggleOpenSerializer,
    VendorComplianceSummarySerializer,
    VendorCreateSupportTicketSerializer,
    VendorKYCDocumentSerializer,
    VendorKYCVerificationSerializer,
    VendorProfileSerializer,
    VendorSanctionsScreeningSerializer,
    VendorSupportTicketSerializer,
    VendorTaxInfoSerializer,
    VendorTicketMessageSerializer,
    VendorUserSerializer,
)
from .tasks import process_csv_import

User = get_user_model()

logger = logging.getLogger(__name__)


class AvailableVendorSearchFilter(filters.SearchFilter):
    """Search vendors and only customer-orderable menu/products."""

    def filter_queryset(self, request, queryset, view):  # noqa: ARG002 - DRF override signature
        terms = self.get_search_terms(request)
        if not terms:
            return queryset

        for term in terms:
            queryset = queryset.filter(
                Q(business_name__icontains=term)
                | Q(menu_items__is_available=True, menu_items__name__icontains=term)
                | Q(menu_items__is_available=True, menu_items__description__icontains=term)
                | Q(products__is_available=True, products__name__icontains=term)
                | Q(products__is_available=True, products__description__icontains=term)
            )
        return queryset.distinct()


MAX_CSV_ROWS = 10_000


def _safe_delay(task_func, *args, log_label=None, **kwargs):
    """Call task.delay() safely, logging failures instead of crashing."""
    try:
        task_func.delay(*args, **kwargs)
    except Exception:
        logger.warning('Failed to queue %s', log_label or task_func.__name__, exc_info=True)


class VendorProfileViewSet(viewsets.GenericViewSet):
    """ViewSet for vendor profile management"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorProfileSerializer

    @action(methods=['get', 'put'], detail=False, url_path='me')
    def my_profile(self, request):
        """Get or update vendor profile"""
        try:
            profile = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            profile = VendorProfile.objects.create(user=request.user)

        if request.method == 'GET':
            serializer = self.get_serializer(profile)
            return Response(serializer.data)

        elif request.method == 'PUT':
            restricted_fields = {'delivery_fee', 'minimum_order', 'delivery_radius_km'}
            provided_restricted = restricted_fields.intersection(request.data.keys())
            if provided_restricted:
                return Response(
                    {'error': f'Fields not editable by vendors: {", ".join(sorted(provided_restricted))}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            serializer = self.get_serializer(profile, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(methods=['post'], detail=False, url_path='toggle-open')
    @throttle_classes([MutationRateThrottle])
    def toggle_open(self, request):
        """Toggle vendor open/closed status"""
        serializer = ToggleOpenSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        is_open = serializer.validated_data['is_open']

        try:
            profile = request.user.vendor_profile
            profile.is_open = is_open
            profile.save(update_fields=['is_open'])
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({'is_open': is_open, 'message': f'Your business is now {"open" if is_open else "closed"}'})

    @action(methods=['post'], detail=False, url_path='upload_image', parser_classes=[MultiPartParser])
    @throttle_classes([MutationRateThrottle])
    def upload_image(self, request):
        """Upload profile/store image"""
        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_media_upload
        from core.media_urls import media_url

        image = request.FILES.get('image')
        if not image:
            return Response({'error': 'No image provided'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_media_upload(image, category='logo')
        except DjangoValidationError:
            logger.exception('Logo validation failed for vendor %s', request.user.id)
            return Response(
                {'error': 'Invalid image. Please upload a valid JPG/PNG/WEBP.'}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            profile = request.user.vendor_profile
            profile.business_logo = image
            profile.save(update_fields=['business_logo'])
            return Response({'message': 'Image uploaded', 'image': media_url(profile.business_logo, request=request)})
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Profile not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(methods=['get'], detail=False, url_path='my-orders')
    def my_orders(self, request):
        """Get vendor's orders"""
        status_filter = request.query_params.get('status')

        orders = Order.objects.filter(vendor=request.user)

        if status_filter:
            orders = orders.filter(status=status_filter)

        orders = (
            orders.select_related('customer', 'driver', 'vendor__vendor_profile')
            .prefetch_related('items', 'items__menu_item', 'items__chosen_options')
            .order_by('-created_at')[:50]
        )

        serializer = OrderSerializer(orders, many=True)
        return Response({'results': serializer.data, 'count': orders.count()})

    @action(methods=['post'], detail=False, url_path='accept-order')
    @throttle_classes([MutationRateThrottle])
    def accept_order(self, request):
        """Accept an incoming order"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        serializer = AcceptOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        order_id = serializer.validated_data['order_id']
        preparation_time = serializer.validated_data.get('preparation_time')

        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(
                    id=order_id,
                    vendor=request.user,
                )
            except Order.DoesNotExist:
                return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

            # Cash orders can be accepted before payment; EcoCash must be PAID
            if order.payment_method != 'CASH' and order.payment_status != 'PAID':
                return Response({'error': 'Payment not yet confirmed'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                order.transition_state('ACCEPTED', actor=request.user, note='Order accepted by vendor')
            except ValueError:
                logger.exception('State transition failed for order %s', order.id)
                return Response(
                    {'error': 'Unable to accept order. Please try again.'}, status=status.HTTP_400_BAD_REQUEST
                )

            if preparation_time:
                try:
                    profile = request.user.vendor_profile
                    profile.preparation_time_mins = int(preparation_time)
                    profile.save(update_fields=['preparation_time_mins'])
                except VendorProfile.DoesNotExist:
                    pass

        logger.info(
            f'Order {order.order_number} accepted by vendor {request.user.id}',
            extra={'order_id': order.order_number, 'vendor_id': request.user.id, 'event': 'order_accepted'},
        )

        response_data = {'message': 'Order accepted', 'order': OrderSerializer(order).data}
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @action(methods=['post'], detail=False, url_path='reject-order')
    @throttle_classes([MutationRateThrottle])
    def reject_order(self, request):
        """Reject an incoming order"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        serializer = RejectOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        order_id = serializer.validated_data['order_id']
        reason = serializer.validated_data.get('reason', '')

        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(
                    id=order_id,
                    vendor=request.user,
                    status__in=['PENDING', 'ACCEPTED', 'PREPARING', 'READY'],
                )
            except Order.DoesNotExist:
                return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

            try:
                order.transition_state('CANCELLED', actor=request.user, note=f'Rejected by vendor: {reason}')
            except ValueError:
                logger.exception('State transition failed for order %s', order.id)
                return Response(
                    {'error': 'Unable to reject order. Please try again.'}, status=status.HTTP_400_BAD_REQUEST
                )

            if order.payment_status == 'PAID':
                try:
                    from payments.services import process_refund

                    process_refund(order)
                except Exception as e:
                    logger.warning('Refund failed for order %s: %s', order.id, e)

        response_data = {'message': 'Order rejected', 'order': OrderSerializer(order).data}
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @action(methods=['get'], detail=False, url_path='stats')
    def vendor_stats(self, request):
        try:
            profile = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

        cache_key = f'vendor_stats_{request.user.id}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        today = timezone.localdate()
        today_start, tomorrow_start = local_day_bounds(today, today)

        today_orders = Order.objects.filter(
            vendor=request.user,
            created_at__gte=today_start,
            created_at__lt=tomorrow_start,
        )
        all_orders = Order.objects.filter(vendor=request.user)

        today_agg = today_orders.aggregate(
            total=Count('id'),
            delivered=Count('id', filter=Q(status='DELIVERED')),
            pending=Count('id', filter=Q(status__in=['PENDING', 'ACCEPTED', 'PREPARING'])),
            cancelled=Count('id', filter=Q(status='CANCELLED')),
            revenue=Sum('total_amount', filter=Q(status='DELIVERED')),
        )

        all_agg = all_orders.aggregate(
            total=Count('id'),
            revenue=Sum('total_amount', filter=Q(status='DELIVERED')),
            delivered_count=Count('id', filter=Q(status='DELIVERED')),
        )

        avg_order_value = 0.0
        if all_agg['delivered_count'] and all_agg['revenue']:
            avg_order_value = float(all_agg['revenue']) / all_agg['delivered_count']

        today_revenue = float(today_agg['revenue'] or 0)
        today_delivered = today_agg['delivered'] or 0

        # OPTIMIZATION: Use aggregation to count distinct customers instead of distinct().count()
        # Avoid N+1 query: distinct().count() requires loading all CustomerProfile objects
        # Instead, count distinct user IDs in orders for this vendor
        today_new_customers = today_orders.values_list('customer_id', flat=True).distinct().count()
        all_customers = all_orders.values_list('customer_id', flat=True).distinct().count()

        today_stats = {
            'total_orders': today_agg['total'] or 0,
            'total_revenue': today_revenue,
            'pending_orders': today_agg['pending'] or 0,
            'completed_orders': today_delivered,
            'cancelled_orders': today_agg['cancelled'] or 0,
            'average_order_value': float(today_agg['revenue'] or 0) / today_delivered if today_delivered else 0,
            'new_customers': today_new_customers,
        }

        all_time_stats = {
            'total_orders': all_agg['total'] or 0,
            'total_revenue': float(all_agg['revenue'] or 0),
            'average_order_value': avg_order_value,
            'rating': float(profile.rating),
            'total_customers': all_customers,
        }

        popular_items = list(
            OrderItem.objects.filter(order__vendor=request.user, order__status='DELIVERED')
            .values('name')
            .annotate(
                total_quantity=Sum('quantity'),
            )
            .order_by('-total_quantity')[:10]
        )

        seven_days_ago = today - timedelta(days=6)
        week_start, tomorrow_start = local_day_bounds(seven_days_ago, today)
        weekly_data = (
            Order.objects.filter(
                vendor=request.user,
                status='DELIVERED',
                created_at__gte=week_start,
                created_at__lt=tomorrow_start,
            )
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(total=Sum('total_amount'))
            .order_by('day')
        )
        revenue_by_day = {str(r['day']): float(r['total'] or 0) for r in weekly_data}
        weekly_revenue = [revenue_by_day.get(str(today - timedelta(days=i)), 0.0) for i in range(6, -1, -1)]

        response_data = {
            'today': today_stats,
            'all_time': all_time_stats,
            'popular_items': popular_items,
            'weekly': {'daily_revenue': weekly_revenue},
        }
        cache.set(cache_key, response_data, 120)
        return Response(response_data)


class MenuViewSet(viewsets.ModelViewSet):
    """ViewSet for menu management"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = MenuCategorySerializer

    def get_queryset(self):
        """Get vendor's menu categories"""
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return MenuCategory.objects.none()

        return (
            MenuCategory.objects.filter(vendor=vendor, is_active=True)
            .prefetch_related('items', 'items__options')
            .order_by('order', 'name')
        )

    def list(self, _request, *_args, **_kwargs):
        """List all menu categories with items"""
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page,
                many=True,
                context={**self.get_serializer_context(), 'include_unavailable': True},
            )
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(
            queryset,
            many=True,
            context={**self.get_serializer_context(), 'include_unavailable': True},
        )
        return Response({'results': serializer.data, 'count': queryset.count()})

    @action(methods=['post'], detail=False, url_path='add-category')
    @throttle_classes([MutationRateThrottle])
    def add_category(self, request):
        """Add a new menu category"""
        serializer = AddCategorySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        name = serializer.validated_data['name']
        description = serializer.validated_data.get('description', '')

        try:
            vendor = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get next order number
        last_category = MenuCategory.objects.filter(vendor=vendor).order_by('-order').first()
        order_num = (last_category.order + 1) if last_category else 1

        category = MenuCategory.objects.create(vendor=vendor, name=name, description=description, order=order_num)

        return Response(
            {'message': 'Category created', 'category': MenuCategorySerializer(category).data},
            status=status.HTTP_201_CREATED,
        )

    @action(methods=['post'], detail=True, url_path='upload-image', parser_classes=[MultiPartParser])
    @throttle_classes([MutationRateThrottle])
    def upload_image(self, request, pk=None):
        try:
            menu_item = MenuItem.objects.get(id=pk, vendor=request.user.vendor_profile)
        except MenuItem.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        image = request.FILES.get('image')
        if not image:
            return Response({'error': 'No image provided'}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_media_upload
        from core.media_urls import media_url

        try:
            validate_media_upload(image, category='product')
        except DjangoValidationError:
            logger.exception('Product image validation failed for vendor %s', request.user.id)
            return Response(
                {'error': 'Invalid image. Please upload a valid JPG/PNG/WEBP.'}, status=status.HTTP_400_BAD_REQUEST
            )

        menu_item.image = image
        menu_item.save(update_fields=['image'])
        return Response({'message': 'Image uploaded', 'image': media_url(menu_item.image, request=request)})

    @action(methods=['post'], detail=True, url_path='upload-images', parser_classes=[MultiPartParser])
    @throttle_classes([MutationRateThrottle])
    def upload_images(self, request, pk=None):
        try:
            menu_item = MenuItem.objects.get(id=pk, vendor=request.user.vendor_profile)
        except MenuItem.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        images = request.FILES.getlist('images')
        if not images:
            return Response({'error': 'No images provided'}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_media_upload

        created = []
        errors = []

        next_order = menu_item.images.count()
        for img in images:
            try:
                validate_media_upload(img, category='product')
            except DjangoValidationError as e:
                errors.append(f'{img.name}: {e}')
                continue

            menu_item_image = MenuItemImage.objects.create(
                menu_item=menu_item,
                image=img,
                sort_order=next_order,
            )
            created.append(MenuItemImageSerializer(menu_item_image).data)
            next_order += 1

        return Response(
            {
                'message': f'{len(created)} image(s) uploaded',
                'created': created,
                'errors': errors,
            }
        )

    @action(methods=['delete'], detail=True, url_path='delete-image')
    @throttle_classes([MutationRateThrottle])
    def delete_image(self, request, pk=None, image_pk=None):
        try:
            menu_item = MenuItem.objects.get(id=pk, vendor=request.user.vendor_profile)
            image_id = image_pk or request.data.get('image_id')
            image = menu_item.images.get(pk=image_id)
            image.delete()
            return Response({'message': 'Image deleted'}, status=status.HTTP_200_OK)
        except MenuItem.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)
        except MenuItemImage.DoesNotExist:
            return Response({'error': 'Image not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(methods=['post'], detail=False, url_path='add-item')
    @throttle_classes([MutationRateThrottle])
    def add_item(self, request):
        """Add a new menu item"""
        serializer = AddMenuItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data

        try:
            vendor = request.user.vendor_profile
            category = MenuCategory.objects.get(id=validated_data['category_id'], vendor=vendor)
        except (VendorProfile.DoesNotExist, MenuCategory.DoesNotExist):
            return Response({'error': 'Category not found'}, status=status.HTTP_404_NOT_FOUND)

        options_data = validated_data.get('options', [])
        menu_item = MenuItem.objects.create(
            vendor=vendor,
            category=category,
            name=validated_data['name'],
            price=validated_data['price'],
            description=validated_data.get('description', ''),
            is_available=validated_data.get('is_available', True),
        )

        MenuItemOption.objects.bulk_create(
            [MenuItemOption(menu_item=menu_item, **opt_data) for opt_data in options_data]
        )

        return Response(
            {'message': 'Menu item created', 'item': MenuItemSerializer(menu_item).data}, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary='Get menu item detail',
        responses={200: MenuItemSerializer, 404: OpenApiTypes.OBJECT},
    )
    @action(methods=['get'], detail=True, url_path='item')
    def get_item(self, request, pk=None):
        try:
            vendor = request.user.vendor_profile
            menu_item = MenuItem.objects.get(id=pk, vendor=vendor)
        except (VendorProfile.DoesNotExist, MenuItem.DoesNotExist):
            return Response({'error': 'Menu item not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(MenuItemSerializer(menu_item).data)

    @action(methods=['put'], detail=True, url_path='update-item')
    @throttle_classes([MutationRateThrottle])
    def update_item(self, request, pk=None):
        """Update a menu item"""
        try:
            vendor = request.user.vendor_profile
            menu_item = MenuItem.objects.get(id=pk, vendor=vendor)
        except (VendorProfile.DoesNotExist, MenuItem.DoesNotExist):
            return Response({'error': 'Menu item not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = AddMenuItemSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data

        if 'name' in validated_data:
            menu_item.name = validated_data['name']
        if 'price' in validated_data:
            menu_item.price = validated_data['price']
        if 'description' in validated_data:
            menu_item.description = validated_data['description']
        if 'is_available' in validated_data:
            menu_item.is_available = validated_data['is_available']
        if 'category_id' in validated_data:
            try:
                category = MenuCategory.objects.get(id=validated_data['category_id'], vendor=vendor)
                menu_item.category = category
            except MenuCategory.DoesNotExist:
                return Response({'error': 'Category not found'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            menu_item.save()

            if 'options' in validated_data:
                menu_item.options.all().delete()
                MenuItemOption.objects.bulk_create(
                    [MenuItemOption(menu_item=menu_item, **opt_data) for opt_data in validated_data['options']]
                )

        return Response({'message': 'Menu item updated', 'item': MenuItemSerializer(menu_item).data})

    @action(methods=['delete'], detail=True, url_path='delete-item')
    @throttle_classes([MutationRateThrottle])
    def delete_item(self, request, pk=None):
        """Delete a menu item"""
        try:
            vendor = request.user.vendor_profile
            menu_item = MenuItem.objects.get(id=pk, vendor=vendor)
        except (VendorProfile.DoesNotExist, MenuItem.DoesNotExist):
            return Response({'error': 'Menu item not found'}, status=status.HTTP_404_NOT_FOUND)

        menu_item.delete()
        return Response({'message': 'Menu item deleted'})


# ==========================================
# Vendor Auth ViewSet
# ==========================================
# ==========================================
# Vendor Order ViewSet
# ==========================================
class VendorOrderPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data, *, status_counts):
        return Response(
            {
                'results': data,
                'count': self.page.paginator.count,
                'status_counts': status_counts,
                'page': self.page.number,
                'page_size': self.page.paginator.per_page,
                'has_next': self.page.has_next(),
            }
        )


class VendorOrderViewSet(viewsets.GenericViewSet):
    """Vendor order management"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    throttle_classes = [UserRateThrottle]
    serializer_class = OrderSerializer

    def get_queryset(self):
        """Get orders for this vendor"""
        return (
            Order.objects.filter(vendor=self.request.user)
            .select_related('customer', 'driver', 'vendor__vendor_profile')
            .prefetch_related('items', 'items__menu_item', 'items__chosen_options', 'status_logs')
            .order_by('-created_at')
        )

    def list(self, request):
        """List all orders for the vendor"""
        orders = self.get_queryset()
        status_counts = dict(
            orders.values('status').annotate(status_total=Count('id')).values_list('status', 'status_total')
        )
        status_filter = request.query_params.get('status')
        if status_filter:
            statuses = [s.strip().upper() for s in status_filter.split(',') if s.strip()]
            if statuses:
                orders = orders.filter(status__in=statuses)

        search_query = request.query_params.get('search', '').strip()
        if search_query:
            if len(search_query) > 100:
                return Response(
                    {'error': 'Search query too long (max 100 characters)'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            orders = orders.filter(
                Q(order_number__icontains=search_query)
                | Q(guest_name__icontains=search_query)
                | Q(customer__first_name__icontains=search_query)
                | Q(customer__last_name__icontains=search_query)
            )

        paginator = VendorOrderPagination()
        paginated = paginator.paginate_queryset(orders, request, view=self)
        serializer = VendorOrderListSerializer(paginated, many=True)
        return paginator.get_paginated_response(serializer.data, status_counts=status_counts)

    @action(methods=['get'], detail=False)
    def list_orders(self, request):
        return self.list(request)

    @action(methods=['get'], detail=True)
    def order_detail(self, request, pk=None):
        """Get order details"""
        try:
            order = (
                Order.objects.select_related('customer', 'driver', 'vendor__vendor_profile')
                .prefetch_related(
                    'items', 'items__menu_item', 'items__chosen_options', 'status_logs', 'driver_locations'
                )
                .get(id=pk, vendor=request.user)
            )
            serializer = OrderDetailSerializer(order)
            return Response(serializer.data)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(methods=['post'], detail=True)
    @throttle_classes_decorator([MutationRateThrottle])
    def accept(self, request, pk=None):
        """Accept an order"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=pk, vendor=request.user)
                if order.payment_method != 'CASH' and order.payment_status != 'PAID':
                    return Response({'error': 'Payment not yet confirmed'}, status=status.HTTP_400_BAD_REQUEST)
                order.transition_state('ACCEPTED', actor=request.user, note='Order accepted by vendor')
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError:
            logger.exception('State transition failed for order %s', pk)
            return Response({'error': 'Unable to accept order. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)

        from core.tasks import send_order_status_update_email

        _safe_delay(send_order_status_update_email, order.id, log_label=f'status email for order {order.id}')

        serializer = OrderSerializer(order)
        response_data = serializer.data
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @action(methods=['post'], detail=True)
    @throttle_classes_decorator([MutationRateThrottle])
    def reject(self, request, pk=None):
        """Reject an order"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=pk, vendor=request.user)
                order.transition_state(
                    'CANCELLED',
                    actor=request.user,
                    note=request.data.get('reason', 'Order rejected by vendor'),
                )
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError:
            logger.exception('State transition failed for order %s', pk)
            return Response({'error': 'Unable to reject order. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)

        from core.tasks import send_order_cancellation_email

        _safe_delay(
            send_order_cancellation_email,
            order.id,
            reason=request.data.get('reason', 'Order rejected by vendor'),
            log_label=f'cancellation email for order {order.id}',
        )

        # Refund the customer if order was paid
        refund_result = None
        if order.payment_status == 'PAID':
            try:
                from payments.services import process_refund

                refund_result = process_refund(order)
            except Exception as e:
                logger.error(f'Refund failed for order {order.id} after vendor reject: {e}', exc_info=True)

        response_data = {'message': 'Order rejected'}
        if refund_result:
            response_data['refund'] = refund_result
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @action(methods=['post'], detail=True)
    @throttle_classes_decorator([MutationRateThrottle])
    def preparing(self, request, pk=None):
        """Start preparing the order"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=pk, vendor=request.user)
                order.transition_state('PREPARING', actor=request.user, note='Preparation started')
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError:
            logger.exception('State transition failed for order %s', pk)
            return Response(
                {'error': 'Unable to start preparation. Please try again.'}, status=status.HTTP_400_BAD_REQUEST
            )

        response_data = OrderSerializer(order).data
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @action(methods=['post'], detail=True)
    @throttle_classes_decorator([MutationRateThrottle])
    def ready(self, request, pk=None):
        """Mark order as ready for pickup"""
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=pk, vendor=request.user)
                order.transition_state('READY', actor=request.user, note='Order is ready for pickup')
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError:
            logger.exception('State transition failed for order %s', pk)
            return Response(
                {'error': 'Unable to mark order as ready. Please try again.'}, status=status.HTTP_400_BAD_REQUEST
            )

        serializer = OrderSerializer(order)
        response_data = serializer.data
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)


# Public endpoints - use MarketplaceProductViewSet from marketplace_views instead
# (MarketplaceProductViewSet at /api/marketplace/products/ is the canonical public listing)


# Vendor endpoints
class VendorProductViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, IsVendorUser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'is_available', 'quantity_on_hand']
    search_fields = ['name', 'description', 'barcode']
    ordering_fields = ['name', 'price', 'quantity_on_hand', 'created_at']

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Product.objects.none()
        return (
            Product.objects.filter(vendor=vendor)
            .select_related('category', 'vendor')
            .prefetch_related('images', 'bestseller_entries')
        )

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ProductCreateUpdateSerializer
        return ProductSerializer

    def perform_create(self, serializer):
        serializer.save(vendor=self.request.user.vendor_profile)


class VendorCategoryViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, IsVendorUser]
    serializer_class = CategorySerializer

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Category.objects.none()
        return Category.objects.filter(vendor=vendor).select_related('vendor', 'parent')


# Bulk import / export
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsVendorUser])
def task_status(_request, task_id):
    from celery.result import AsyncResult

    result = AsyncResult(task_id)
    data = {
        'task_id': task_id,
        'status': result.status,
    }
    if result.status == 'SUCCESS' and result.ready():
        import_result = result.result
        if isinstance(import_result, dict):
            data['imported_count'] = int(import_result.get('created', 0))
            if import_result.get('reason'):
                data['reason'] = import_result['reason']
        else:
            data['imported_count'] = int(import_result)
    elif result.status == 'FAILURE':
        data['error'] = 'Import failed'
    return Response(data)


@api_view(['GET', 'POST'])
@permission_classes([permissions.IsAuthenticated, IsVendorUser])
@throttle_classes([MutationRateThrottle])
def bulk_import_csv(request):
    if request.method == 'GET':
        export = request.query_params.get('export')
        if export:
            vendor_profile = request.user.vendor_profile

            def generate_csv():
                header = [
                    'name',
                    'description',
                    'price',
                    'category',
                    'quantity_on_hand',
                    'low_stock_threshold',
                    'unit',
                    'is_available',
                    'barcode',
                    'product_type',
                    'is_inventory_tracked',
                    'weight_kg',
                    'dimensions',
                    'brand',
                ]
                output = io.StringIO(newline='')
                writer = csv.writer(output)
                writer.writerow(header)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
                products = (
                    Product.objects.filter(vendor=vendor_profile)
                    .select_related('category')
                    .iterator(chunk_size=500)
                )
                for p in products:
                    writer.writerow(
                        [
                            p.name,
                            p.description,
                            p.price,
                            p.category.name if p.category else '',
                            p.quantity_on_hand,
                            p.low_stock_threshold,
                            p.unit or '',
                            'yes' if p.is_available else 'no',
                            p.barcode or '',
                            p.product_type or '',
                            'yes' if p.is_inventory_tracked else 'no',
                            p.weight_kg if p.weight_kg is not None else '',
                            p.dimensions or '',
                            p.brand or '',
                        ]
                    )
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)

            response = StreamingHttpResponse(generate_csv(), content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="products.csv"'
            return response

    file = request.FILES.get('file')
    if not file:
        return Response({'error': 'No file'}, status=400)
    from django.core.exceptions import ValidationError as DjangoValidationError

    from core.file_validation import validate_csv_file

    try:
        validate_csv_file(file)
    except DjangoValidationError:
        logger.exception('CSV validation failed for vendor %s', request.user.id)
        return Response({'error': 'Invalid CSV file. Please check format and try again.'}, status=400)
    file.seek(0)
    text_file = io.TextIOWrapper(file.file, encoding='utf-8-sig', newline='')
    rows = []
    try:
        reader = csv.DictReader(text_file)
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_CSV_ROWS:
                return Response(
                    {'error': f'CSV file too large. Maximum {MAX_CSV_ROWS:,} rows allowed.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
    except (UnicodeDecodeError, csv.Error):
        return Response({'error': 'CSV file is not valid UTF-8 CSV data.'}, status=status.HTTP_400_BAD_REQUEST)
    finally:
        text_file.detach()

    task = process_csv_import.delay(request.user.vendor_profile.id, rows)
    return Response({'task_id': task.id, 'rows': len(rows)})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsVendorUser])
def vendor_analytics(request):
    """Analytics dashboard data for vendors"""
    try:
        profile = request.user.vendor_profile
    except VendorProfile.DoesNotExist:
        return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

    period = request.query_params.get('period', 'today')
    today = timezone.localdate()
    dashboard_v2_enabled = flag_enabled('vendor_dashboard_v2', request=request)
    cache_key = f'vendor_analytics:{request.user.id}:{period}:{today.isoformat()}:{int(dashboard_v2_enabled)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached)

    if period == 'today':
        start_date = today
    elif period == 'week':
        start_date = today - timedelta(days=today.weekday())
    elif period == 'month':
        start_date = today.replace(day=1)
    elif period == 'year':
        start_date = today.replace(month=1, day=1)
    else:
        start_date = today

    start_at, end_at = local_day_bounds(start_date, today)
    orders = Order.objects.filter(vendor=request.user, created_at__gte=start_at, created_at__lt=end_at)
    delivered = orders.filter(status='DELIVERED')

    status_aggregate_fields = [
        (status_value, f'order_status_{index}')
        for index, (status_value, _label) in enumerate(Order.STATUS_CHOICES)
    ]
    orders_agg = orders.aggregate(
        total_orders=Count('id'),
        total_revenue=Sum('total_amount', filter=Q(status='DELIVERED')),
        delivered_orders=Count('id', filter=Q(status='DELIVERED')),
        pending_orders=Count('id', filter=Q(status__in=['PENDING', 'ACCEPTED', 'PREPARING'])),
        **{
            field_name: Count('id', filter=Q(status=status_value))
            for status_value, field_name in status_aggregate_fields
        },
    )
    summary = {
        'total_orders': orders_agg['total_orders'],
        'total_revenue': float(orders_agg['total_revenue'] or 0),
        'avg_order_value': (
            float(orders_agg['total_revenue'] or 0) / orders_agg['delivered_orders']
            if orders_agg['delivered_orders']
            else 0
        ),
        'pending_orders': orders_agg['pending_orders'],
    }

    days = 7 if period == 'today' else (30 if period == 'week' else 90 if period == 'month' else 365)
    start_lookup = today - timedelta(days=days - 1)
    revenue_start, revenue_end = local_day_bounds(start_lookup, today)
    revenue_by_day = (
        Order.objects.filter(
            vendor=request.user,
            status='DELIVERED',
            created_at__gte=revenue_start,
            created_at__lt=revenue_end,
        )
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('total_amount'))
        .order_by('day')
    )
    revenue_map = {}
    for r in revenue_by_day:
        revenue_map[r['day']] = float(r['total'])
    revenue_trend = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        revenue_trend.append(
            {
                'label': day.strftime('%b %d'),
                'revenue': revenue_map.get(day, 0.0),
            }
        )

    top_products = (
        OrderItem.objects.filter(order__vendor=request.user, order__status='DELIVERED')
        .values('name')
        .annotate(quantity=Sum('quantity'))
        .order_by('-quantity')[:10]
    )

    category_breakdown = list(
        Product.objects.filter(vendor=profile).values('category__name').annotate(value=Count('id'))
    )
    category_breakdown = [
        {'name': c['category__name'] or 'Uncategorized', 'value': c['value']} for c in category_breakdown
    ]

    status_labels = dict(Order.STATUS_CHOICES)
    order_status_breakdown = [
        {'name': status_labels.get(status_value, status_value), 'value': orders_agg[field_name]}
        for status_value, field_name in status_aggregate_fields
        if orders_agg[field_name]
    ]

    response = {
        'dashboard_version': 'v1',
        'summary': summary,
        'revenue_trend': revenue_trend,
        'top_products': list(top_products),
        'category_breakdown': category_breakdown,
        'order_status_breakdown': order_status_breakdown,
    }

    # vendor_dashboard_v2: opt-in richer analytics (peak hours, delivery time).
    if dashboard_v2_enabled:
        response['dashboard_version'] = 'v2'
        try:
            from django.db.models.functions import ExtractHour

            peak = (
                Order.objects.filter(vendor=request.user, status='DELIVERED')
                .annotate(hour=ExtractHour('delivered_at'))
                .values('hour')
                .annotate(orders=Count('id'))
                .order_by('-orders')[:3]
            )
            response['peak_hours'] = [{'hour': p['hour'], 'orders': p['orders']} for p in peak]
            delivered_times = delivered.filter(delivered_at__isnull=False, created_at__isnull=False)
            if delivered_times.exists():
                avg_min = (
                    delivered_times.aggregate(
                        avg=Avg(
                            ExtractHour('delivered_at') * 60
                            + ExtractMinute('delivered_at')
                            - (ExtractHour('created_at') * 60 + ExtractMinute('created_at'))
                        )
                    )['avg']
                    or 0
                )
                response['avg_delivery_minutes'] = round(float(avg_min), 1)
        except Exception as exc:
            logger.warning('vendor_dashboard_v2 metrics failed: %s', exc)

    cache.set(cache_key, response, 120)
    return Response(response)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsVendorUser])
def vendor_analytics_csv(request):
    """Export analytics as CSV"""

    try:
        _ = request.user.vendor_profile
    except VendorProfile.DoesNotExist:
        return Response({'error': 'Vendor profile not found'}, status=404)

    period = request.query_params.get('period', 'today')
    today = timezone.localdate()

    if period == 'today':
        start_date = today
    elif period == 'week':
        start_date = today - timedelta(days=today.weekday())
    elif period == 'month':
        start_date = today.replace(day=1)
    elif period == 'year':
        start_date = today.replace(month=1, day=1)
    else:
        start_date = today

    start_at, end_at = local_day_bounds(start_date, today)
    orders = (
        Order.objects.filter(
            vendor=request.user,
            created_at__gte=start_at,
            created_at__lt=end_at,
            status='DELIVERED',
        )
        .select_related('customer')
        .prefetch_related('items', 'items__menu_item', 'items__product', 'items__product_variant')
    )

    def generate_csv():
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['Order #', 'Date', 'Customer', 'Items', 'Subtotal', 'Delivery Fee', 'Total', 'Status'])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for order in orders.iterator(chunk_size=250):
            items_str = '; '.join(f'{oi.quantity}x {oi.name}' for oi in order.items.all())
            writer.writerow(
                [
                    order.order_number,
                    timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M'),
                    f'{order.customer.email[:2]}***@***' if order.customer and order.customer.email else 'N/A',
                    items_str,
                    order.subtotal,
                    order.delivery_fee,
                    order.total_amount,
                    order.status,
                ]
            )
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    response = StreamingHttpResponse(generate_csv(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="analytics-{period}.csv"'
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsVendorUser])
def vendor_payouts(request):
    """Vendor-facing payout history (own payouts only)."""
    from payments.models import VendorPayout
    from payments.serializers import VendorPayoutSerializer

    payouts = VendorPayout.objects.filter(vendor=request.user).order_by('-created_at')[:50]
    serializer = VendorPayoutSerializer(payouts, many=True)
    return Response({'results': serializer.data, 'count': payouts.count()})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsVendorUser])
def vendor_scorecard(request):
    """Vendor's own performance scorecard (if computed)."""
    try:
        sc = request.user.vendor_profile.scorecard
    except Exception:
        return Response({'error': 'Scorecard not yet computed'}, status=status.HTTP_404_NOT_FOUND)
    data = {
        'vendor_id': sc.vendor_id,
        'vendor_name': sc.vendor.business_name,
        'on_time_prep_rate': float(sc.on_time_prep_rate),
        'cancellation_rate': float(sc.cancellation_rate),
        'avg_rating_30d': float(sc.avg_rating_30d),
        'avg_rating_90d': float(sc.avg_rating_90d),
        'total_orders_30d': sc.total_orders_30d,
        'total_orders_90d': sc.total_orders_90d,
        'avg_prep_time_mins': float(sc.avg_prep_time_mins),
        'repeat_customer_rate': float(sc.repeat_customer_rate),
        'revenue_30d': str(sc.revenue_30d),
        'commission_owed_30d': str(sc.commission_owed_30d),
        'computed_at': sc.computed_at.isoformat(),
    }
    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsVendorUser])
def vendor_reconciliation(request):
    """Vendor's own payout reconciliation (own vendor_profile)."""
    from vendors.models import PayoutReconciliation

    try:
        profile = request.user.vendor_profile
    except Exception:
        return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)
    qs = PayoutReconciliation.objects.filter(vendor_profile=profile).order_by('-period_start')[:50]
    results = [
        {
            'id': pr.id,
            'period_start': pr.period_start.isoformat(),
            'period_end': pr.period_end.isoformat(),
            'total_orders_amount': str(pr.total_orders_amount),
            'total_commission': str(pr.total_commission),
            'expected_payout': str(pr.expected_payout),
            'actual_payout': str(pr.actual_payout),
            'variance': str(pr.variance),
            'order_count': pr.order_count,
            'is_resolved': pr.is_resolved,
            'resolution_note': pr.resolution_note,
            'created_at': pr.created_at.isoformat(),
        }
        for pr in qs
    ]
    return Response({'results': results, 'count': len(results)})


class InventoryViewSet(viewsets.GenericViewSet):
    """Inventory management for vendors"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = InventoryItemSerializer

    def get_queryset(self):
        try:
            return Product.objects.filter(vendor=self.request.user.vendor_profile)
        except Exception:
            return Product.objects.none()

    def list(self, request):
        products = self.get_queryset().select_related('category').order_by('-updated_at')
        try:
            page_size = int(request.query_params.get('page_size', 50))
        except (ValueError, TypeError):
            page_size = 50
        if page_size < 1:
            return Response(
                {'error': 'page_size must be a positive integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        page_size = min(page_size, 100)
        try:
            page = int(request.query_params.get('page', 1))
        except (ValueError, TypeError):
            page = 1
        if page < 1:
            return Response(
                {'error': 'page must be a positive integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start = (page - 1) * page_size
        end = start + page_size

        total = products.count()
        serializer = self.get_serializer(products[start:end], many=True)
        return Response(
            {
                'results': serializer.data,
                'count': total,
                'page': page,
                'page_size': page_size,
                'has_next': total > end,
            }
        )

    @action(methods=['post'], detail=True)
    @throttle_classes([MutationRateThrottle])
    def adjust(self, request, pk=None):
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key:
            existing = IdempotencyKey.objects.filter(key=idempotency_key, user=request.user).first()
            if existing:
                return Response(existing.response_data, status=existing.status_code)

        try:
            product = self.get_queryset().get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = InventoryAdjustSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        quantity = serializer.validated_data['quantity']
        note = serializer.validated_data.get('note', '')

        from django.db import transaction as db_transaction

        with db_transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product.pk)
            product.quantity_on_hand = max(0, product.quantity_on_hand + quantity)
            product.save(update_fields=['quantity_on_hand'])
            InventoryAdjustmentLog.objects.create(
                product=product,
                quantity=quantity,
                note=note,
            )

        response_data = {
            'message': 'Stock adjusted',
            'current_stock': product.quantity_on_hand,
        }
        if idempotency_key:
            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=request.user,
                defaults={'response_data': response_data, 'status_code': status.HTTP_200_OK},
            )
        return Response(response_data)

    @extend_schema(
        summary='Get inventory adjustment log',
        responses={200: InventoryAdjustmentLogSerializer(many=True), 404: OpenApiTypes.OBJECT},
    )
    @action(methods=['get'], detail=True)
    def history(self, _request, pk=None):
        try:
            product = self.get_queryset().get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        logs = product.adjustment_logs.all()[:50]
        return Response(InventoryAdjustmentLogSerializer(logs, many=True).data)


class PublicVendorViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = VendorProfile.objects.none()
    serializer_class = PublicVendorSerializer
    permission_classes = [permissions.AllowAny]
    filter_backends = [AvailableVendorSearchFilter]
    search_fields = [
        'business_name',
        'menu_items__name',
        'menu_items__description',
        'products__name',
        'products__description',
    ]

    def _normalized_cache_key(self, request):
        prefix = 'public_vendors'
        params = []
        for key, values in sorted(request.query_params.lists()):
            if key in {'lat', 'lng'}:
                try:
                    normalized_values = [f'{float(value):.3f}' for value in values]
                except (TypeError, ValueError):
                    normalized_values = values
            else:
                normalized_values = values
            params.extend(f'{key}={value}' for value in normalized_values)
        param_str = '&'.join(params)
        return f'{prefix}:{param_str}' if param_str else prefix

    def list(self, request, *args, **kwargs):
        cache_key = self._normalized_cache_key(request)
        cached = cache.get(cache_key)
        if cached is not None:
            from rest_framework.response import Response

            return Response(cached)
        response = super().list(request, *args, **kwargs)
        cache.set(cache_key, response.data, 60 if request.query_params else 120)
        return response

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        return queryset

    def get_queryset(self):
        from .models import VendorKYCDocument, VendorSanctionsScreening, VendorTaxInfo

        today = timezone.localdate()
        required_docs = VendorKYCDocument.objects.filter(vendor_id=OuterRef('pk'), is_required=True)
        latest_sanction_status = (
            VendorSanctionsScreening.objects.filter(vendor_id=OuterRef('pk'))
            .order_by('-screened_at', '-pk')
            .values('status')[:1]
        )
        qs = VendorProfile.objects.filter(
            is_active=True,
            is_verified=True,
            lifecycle_state__in=[
                VendorProfile.LIFECYCLE_ACTIVE,
                VendorProfile.LIFECYCLE_APPROVED,
            ],
        ).annotate(
            _has_required_kyc=Exists(required_docs),
            _has_unverified_required_kyc=Exists(required_docs.exclude(verification_status='VERIFIED')),
            _has_expired_required_kyc=Exists(
                required_docs.filter(
                    expiry_date__lt=today,
                    verification_status__in=['VERIFIED', 'PENDING'],
                )
            ),
            _has_expired_tax_clearance=Exists(
                VendorTaxInfo.objects.filter(vendor_id=OuterRef('pk'), tax_clearance_expiry__lt=today)
            ),
            _latest_sanction_status=Subquery(latest_sanction_status),
        ).filter(
            _has_required_kyc=True,
            _has_unverified_required_kyc=False,
            _has_expired_required_kyc=False,
            _has_expired_tax_clearance=False,
        ).filter(Q(_latest_sanction_status__isnull=True) | ~Q(_latest_sanction_status='CONFIRMED_MATCH'))

        search_terms = self.request.query_params.get('search', '').split()
        if search_terms:
            matching_menu_items = MenuItem.objects.filter(is_available=True)
            menu_search = Q()
            for term in search_terms:
                menu_search &= Q(name__icontains=term) | Q(description__icontains=term)
            qs = qs.prefetch_related(
                Prefetch(
                    'menu_items',
                    queryset=matching_menu_items.filter(menu_search).only('id', 'vendor_id', 'name', 'price'),
                    to_attr='search_matches_menu_items',
                )
            )
        category = self.request.query_params.get('category')
        if category:
            valid_vendor_cats = [c[0] for c in VendorProfile.CATEGORY_CHOICES]
            requested = [c.strip().upper() for c in category.split(',') if c.strip()]
            matched = [c for c in requested if c in valid_vendor_cats]
            if matched:
                qs = qs.filter(category__in=matched)
            elif len(requested) == 1:
                product_type_map = {name.upper(): code for code, name in Product.PRODUCT_TYPES}
                pt = product_type_map.get(requested[0])
                if pt:
                    qs = qs.filter(products__product_type=pt).distinct()

        pickup = self.request.query_params.get('pickup')
        if pickup == 'true':
            qs = qs.filter(pickup_available=True)

        open_now = self.request.query_params.get('open_now')
        if open_now == 'true':
            qs = qs.filter(is_open=True)

        free_delivery = self.request.query_params.get('free_delivery')
        if free_delivery == 'true':
            qs = qs.filter(delivery_fee=0)

        rating = self.request.query_params.get('rating')
        if rating:
            qs = qs.filter(rating__gte=int(rating))

        max_time = self.request.query_params.get('max_time')
        if max_time:
            qs = qs.filter(preparation_time_mins__lte=int(max_time))

        has_promo = self.request.query_params.get('has_promo')
        if has_promo == 'true':
            now = timezone.now()
            qs = qs.filter(
                promotions__is_active=True,
                promotions__valid_until__gte=now,
            ).distinct()

        # Distance filter: dual-radius model.
        # - Primary (profitable): min(MAX_DELIVERY_RADIUS_KM, vendor.delivery_radius_km)
        # - Secondary (furthest): min(override or +inf, EXTENDED_DELIVERY_RADIUS_KM)
        # Vendors beyond the secondary cutoff are excluded. beyond_recommended=True
        # flags vendors beyond the primary zone. lat/lng already arrive from the
        # mobile home screen. Invalid coords or missing locations never 500 — the
        # filter is skipped and distance_km is null.
        lat = self.request.query_params.get('lat')
        lng = self.request.query_params.get('lng')
        if lat is not None and lng is not None:
            try:
                lat_f, lng_f = float(lat), float(lng)
                if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
                    raise ValueError('coords out of range')
                from django.db.models import BooleanField, Case, F, FloatField, Value, When
                from django.db.models.functions import Least

                from core.system_settings import get_float

                max_radius = get_float('MAX_DELIVERY_RADIUS_KM', 5.0)
                extended_radius = get_float('EXTENDED_DELIVERY_RADIUS_KM', 15.0)

                override = self.request.query_params.get('radius_km') or self.request.query_params.get('radius')
                try:
                    override_val = float(override) if override is not None else None
                except (TypeError, ValueError):
                    override_val = None
                if override_val is not None and (not math.isfinite(override_val) or override_val < 0):
                    override_val = None

                cutoff = min(override_val, extended_radius) if override_val is not None else extended_radius

                point = Point(lng_f, lat_f, srid=4326)
                latitude_delta = min(180.0, cutoff / 110.574)
                longitude_scale = max(abs(math.cos(math.radians(lat_f))), 1e-6)
                longitude_delta = min(180.0, cutoff / (111.320 * longitude_scale))
                min_latitude = max(-90.0, lat_f - latitude_delta)
                max_latitude = min(90.0, lat_f + latitude_delta)
                min_longitude = lng_f - longitude_delta
                max_longitude = lng_f + longitude_delta

                def bbox(min_lng, max_lng):
                    return Polygon.from_bbox((min_lng, min_latitude, max_lng, max_latitude))

                if min_longitude < -180.0:
                    location_bounds = Q(business_location__bboverlaps=bbox(-180.0, max_longitude)) | Q(
                        business_location__bboverlaps=bbox(min_longitude + 360.0, 180.0)
                    )
                elif max_longitude > 180.0:
                    location_bounds = Q(business_location__bboverlaps=bbox(min_longitude, 180.0)) | Q(
                        business_location__bboverlaps=bbox(-180.0, max_longitude - 360.0)
                    )
                else:
                    location_bounds = Q(business_location__bboverlaps=bbox(min_longitude, max_longitude))

                geography = GeometryField(srid=4326, geography=True)
                qs = qs.filter(location_bounds).annotate(
                    _distance_m=Distance(
                        Cast('business_location', output_field=geography),
                        Cast(Value(point, output_field=GeometryField(srid=4326)), output_field=geography),
                    ),
                    _primary_cap=Least(Value(max_radius), F('delivery_radius_km'), output_field=FloatField()),
                ).filter(
                    _distance_m__lte=Value(cutoff * 1000.0)
                ).annotate(
                    distance_km=F('_distance_m') / Value(1000.0),
                    beyond_recommended=Case(
                        When(_distance_m__gt=F('_primary_cap') * Value(1000.0), then=Value(True)),
                        default=Value(False),
                        output_field=BooleanField(),
                    ),
                ).order_by('_distance_m')
            except (TypeError, ValueError):
                logger.warning('PublicVendorViewSet: ignoring invalid lat/lng %s,%s', lat, lng)

        return qs

    @action(methods=['get'], detail=True, url_path='menu')
    def vendor_menu(self, _request, pk=None):
        """Public endpoint: get a vendor's menu categories with items."""
        try:
            vendor = VendorProfile.objects.get(id=pk, is_active=True)
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor not found'}, status=status.HTTP_404_NOT_FOUND)

        categories = (
            MenuCategory.objects.filter(vendor=vendor, is_active=True)
            .prefetch_related(
                Prefetch('items', queryset=MenuItem.objects.select_related('category').prefetch_related('options')),
                'items__options',
            )
            .order_by('order', 'name')
        )

        serializer = MenuCategorySerializer(categories, many=True)
        return Response(serializer.data)


@extend_schema(
    request=OpenApiTypes.OBJECT,
    responses={
        201: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        409: OpenApiTypes.OBJECT,
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([LoginRateThrottle])
def login_vendor_api(request):
    """API-based vendor login returning JWT tokens."""
    email = request.data.get('email')
    password = request.data.get('password')

    if not email or not password:
        return Response({'error': 'Email and password required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({'error': 'Invalid email or password'}, status=status.HTTP_401_UNAUTHORIZED)

    if not user.is_active:
        return Response({'error': 'Account is deactivated'}, status=status.HTTP_403_FORBIDDEN)

    from django.contrib.auth import authenticate

    user = authenticate(request, username=user.username, password=password)

    if user and user.is_vendor():
        try:
            profile = user.vendor_profile
        except VendorProfile.DoesNotExist:
            profile = VendorProfile.objects.create(user=user)

        refresh = RefreshToken.for_user(user)
        response = Response(
            {
                'access': str(refresh.access_token),
                'refresh': str(refresh),
                'user': VendorUserSerializer(user).data,
                'profile': VendorProfileSerializer(profile).data,
            }
        )
        return set_auth_cookies(response, str(refresh.access_token), str(refresh), request=request)
    return Response({'error': 'Invalid email or password'}, status=status.HTTP_401_UNAUTHORIZED)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterRateThrottle, MutationRateThrottle])
def register_vendor_api(request):
    """Submit a vendor application. The account is created as inactive
    (pending approval) — no password is required or set."""
    email = request.data.get('email')
    first_name = request.data.get('first_name', '')
    last_name = request.data.get('last_name', '')
    phone_number = request.data.get('phone_number', '')
    business_name = request.data.get('business_name', 'My Business')
    category = request.data.get('category', '')
    business_address = request.data.get('business_address', '')
    business_phone = request.data.get('business_phone', '')
    business_email = request.data.get('business_email', '')

    if not email:
        return Response({'error': 'Email is required'}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.filter(email=email).exists():
        return Response(
            {'message': 'If the email is registered, check your inbox for instructions'}, status=status.HTTP_200_OK
        )

    try:
        user = User.objects.create_user(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone_number=phone_number,
            user_type='VENDOR',
            is_active=False,
        )
        user.set_unusable_password()
        user.save(update_fields=['password'])

        VendorProfile.objects.create(
            user=user,
            business_name=business_name,
            category=category,
            business_address=business_address,
            business_phone=business_phone,
            business_email=business_email or email,
        )

        logger.info('Vendor application submitted: %s (%s)', business_name, email)
        return Response(
            {
                'message': (
                    'Application submitted successfully. Our team will review your application and contact you shortly.'
                ),
            },
            status=status.HTTP_201_CREATED,
        )
    except Exception:
        logger.exception('Vendor registration failed')
        return Response({'error': 'Registration failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PromotionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = PromotionSerializer

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Promotion.objects.none()
        return Promotion.objects.filter(vendor=vendor)

    def perform_create(self, serializer):
        vendor = self.request.user.vendor_profile
        serializer.save(vendor=vendor)


# ─────────────────────────────────────────────────────────────────────────
# KYC Compliance Views
# ─────────────────────────────────────────────────────────────────────────


class VendorKYCDocumentViewSet(viewsets.ModelViewSet):
    """Manage KYC documents for vendor"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorKYCDocumentSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return VendorKYCDocument.objects.none()
        return VendorKYCDocument.objects.filter(vendor=vendor).order_by('-created_at')

    def get_serializer_class(self):
        if self.action == 'create':
            from vendors.serializers import VendorKYCDocumentUploadSerializer

            return VendorKYCDocumentUploadSerializer
        return VendorKYCDocumentSerializer

    def perform_create(self, serializer):
        vendor = self.request.user.vendor_profile
        serializer.save(vendor=vendor)


class VendorKYCVerificationViewSet(viewsets.ReadOnlyModelViewSet):
    """View KYC verification status"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorKYCVerificationSerializer

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return VendorKYCVerification.objects.none()
        return VendorKYCVerification.objects.filter(vendor=vendor)


class VendorTaxInfoViewSet(viewsets.ModelViewSet):
    """Manage vendor tax information"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorTaxInfoSerializer

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return VendorTaxInfo.objects.none()
        return VendorTaxInfo.objects.filter(vendor=vendor).select_related('vendor')

    def get_object(self):
        """Get or create tax info for vendor"""
        vendor = self.request.user.vendor_profile
        obj, created = VendorTaxInfo.objects.get_or_create(vendor=vendor)
        return obj

    def update(self, request, *_args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_create(self, serializer):
        serializer.save(vendor=self.request.user.vendor_profile)


class VendorSanctionsScreeningViewSet(viewsets.ReadOnlyModelViewSet):
    """View sanctions screening results"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorSanctionsScreeningSerializer

    def get_queryset(self):
        try:
            vendor = self.request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return VendorSanctionsScreening.objects.none()
        return VendorSanctionsScreening.objects.filter(vendor=vendor).order_by('-screened_at')


class VendorComplianceDashboardViewSet(viewsets.GenericViewSet):
    """Compliance dashboard for vendor"""

    permission_classes = [IsAuthenticated, IsVendorUser]
    serializer_class = VendorComplianceSummarySerializer

    @action(methods=['get'], detail=False, url_path='dashboard')
    def compliance_dashboard(self, request):
        """Get comprehensive compliance status for vendor"""
        try:
            vendor = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

        # KYC Status
        kyc_status = vendor.kyc_status()
        is_verified = vendor.is_kyc_verified()
        missing_docs = (
            vendor.kyc_verification.get_missing_required_types() if hasattr(vendor, 'kyc_verification') else []
        )

        # Expiring documents
        expiring = []
        for doc in vendor.kyc_documents.filter(is_required=True, verification_status='VERIFIED'):
            days = doc.days_until_expiry()
            if days is not None and days <= 30:
                expiring.append(
                    {
                        'document_type': doc.document_type,
                        'document_type_display': doc.get_document_type_display(),
                        'expiry_date': doc.expiry_date,
                        'days_until_expiry': days,
                    }
                )

        # Sanctions/PEP status
        sanctions = vendor.sanctions_screenings.first()
        sanctions_status = sanctions.status if sanctions else 'PENDING'
        pep_status = vendor.sanctions_screenings.filter(screening_type='PEP').first()
        pep_status = pep_status.status if pep_status else 'PENDING'

        # Tax info
        tax_info = getattr(vendor, 'tax_info', None)
        zimra_verified = tax_info.zimra_pin_verified if tax_info else False

        # Suspension check
        should_suspend = vendor.should_be_suspended()

        # Next review
        next_review = None
        if hasattr(vendor, 'kyc_verification') and vendor.kyc_verification.next_review_date:
            next_review = vendor.kyc_verification.next_review_date

        return Response(
            {
                'vendor_id': vendor.id,
                'business_name': vendor.business_name,
                'kyc_status': kyc_status,
                'is_kyc_verified': is_verified,
                'missing_documents': missing_docs,
                'expiring_documents': expiring,
                'sanctions_status': sanctions_status,
                'pep_status': pep_status,
                'zimra_pin_verified': zimra_verified,
                'should_be_suspended': should_suspend,
                'next_review_date': next_review,
            }
        )

    @action(methods=['post'], detail=False, url_path='refresh-kyc')
    @throttle_classes([MutationRateThrottle])
    def refresh_kyc(self, request):
        """Manually refresh KYC status from documents"""
        try:
            vendor = request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return Response({'error': 'Vendor profile not found'}, status=status.HTTP_404_NOT_FOUND)

        verification, created = VendorKYCVerification.objects.get_or_create(vendor=vendor)
        verification.refresh_status()

        return Response(
            {
                'message': 'KYC status refreshed',
                'kyc_status': verification.overall_status,
            }
        )


# Admin-only views for KYC review
class AdminKYCDocumentViewSet(viewsets.ModelViewSet):
    """Admin interface for KYC document review"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = VendorKYCDocumentSerializer
    pagination_class = PageNumberPagination
    page_size = 50

    def get_queryset(self):
        return VendorKYCDocument.objects.select_related('vendor', 'reviewed_by', 'uploaded_by').all()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    @action(methods=['post'], detail=True, url_path='approve')
    def approve(self, request, pk=None):  # noqa: ARG002
        """Approve a KYC document"""
        doc = self.get_object()
        doc.mark_verified(request.user)
        # Refresh vendor's overall KYC status
        if hasattr(doc.vendor, 'kyc_verification'):
            doc.vendor.kyc_verification.refresh_status()
        try:
            from dashboard.decorators import audit_action_direct

            audit_action_direct(
                request,
                'KYC_DOCUMENT_APPROVE',
                'VendorKYCDocument',
                str(doc.id),
                f'Document {doc.document_type} approved for vendor {doc.vendor_id}',
            )
        except Exception:
            logging.getLogger(__name__).exception('KYC doc approve audit failed %s', doc.id)
        return Response({'message': 'Document approved', 'status': 'VERIFIED'})

    @action(methods=['post'], detail=True, url_path='reject')
    def reject(self, request, pk=None):  # noqa: ARG002
        """Reject a KYC document"""
        doc = self.get_object()
        reason = request.data.get('reason', '')
        if not reason:
            return Response({'error': 'Rejection reason required'}, status=status.HTTP_400_BAD_REQUEST)
        doc.mark_rejected(request.user, reason)
        if hasattr(doc.vendor, 'kyc_verification'):
            doc.vendor.kyc_verification.refresh_status()
        try:
            from dashboard.decorators import audit_action_direct

            audit_action_direct(
                request,
                'KYC_DOCUMENT_REJECT',
                'VendorKYCDocument',
                str(doc.id),
                f'Document {doc.document_type} rejected for vendor {doc.vendor_id}: {reason[:80]}',
            )
        except Exception:
            logging.getLogger(__name__).exception('KYC doc reject audit failed %s', doc.id)
        return Response({'message': 'Document rejected', 'status': 'REJECTED'})


class AdminKYCVerificationViewSet(viewsets.ModelViewSet):
    """Admin interface for KYC verification management"""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = VendorKYCVerificationSerializer
    pagination_class = PageNumberPagination
    page_size = 50

    def get_queryset(self):
        return VendorKYCVerification.objects.select_related('vendor', 'verified_by').all()

    @action(methods=['post'], detail=True, url_path='approve')
    def approve(self, request, pk=None):  # noqa: ARG002
        """Approve vendor KYC"""
        verification = self.get_object()
        verification.overall_status = 'VERIFIED'
        verification.verified_at = timezone.now()
        verification.verified_by = request.user
        verification.save(update_fields=['overall_status', 'verified_at', 'verified_by', 'updated_at'])
        verification.vendor.is_verified = True
        verification.vendor.save(update_fields=['is_verified'])
        try:
            from dashboard.decorators import audit_action_direct

            audit_action_direct(
                request,
                'KYC_VERIFICATION_APPROVE',
                'VendorKYCVerification',
                str(verification.id),
                f'Verification {verification.id} approved for vendor {verification.vendor_id}',
            )
        except Exception:
            logging.getLogger(__name__).exception('KYC verification approve audit failed %s', verification.id)
        return Response({'message': 'Vendor KYC approved', 'status': 'VERIFIED'})

    @action(methods=['post'], detail=True, url_path='reject')
    def reject(self, request, pk=None):  # noqa: ARG002
        """Reject vendor KYC"""
        verification = self.get_object()
        reason = request.data.get('reason', '')
        if not reason:
            return Response({'error': 'Rejection reason required'}, status=status.HTTP_400_BAD_REQUEST)
        verification.overall_status = 'REJECTED'
        verification.rejected_at = timezone.now()
        verification.rejection_summary = reason
        verification.save(update_fields=['overall_status', 'rejected_at', 'rejection_summary', 'updated_at'])
        try:
            from dashboard.decorators import audit_action_direct

            audit_action_direct(
                request,
                'KYC_VERIFICATION_REJECT',
                'VendorKYCVerification',
                str(verification.id),
                f'Verification {verification.id} rejected for vendor {verification.vendor_id}: {reason[:80]}',
            )
        except Exception:
            logging.getLogger(__name__).exception('KYC verification reject audit failed %s', verification.id)
        return Response({'message': 'Vendor KYC rejected', 'status': 'REJECTED'})


# ── Holiday overrides (vendor requests to operate on holidays) ───────────────


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, IsVendorUser])
@throttle_classes([MutationRateThrottle])
def vendor_holiday_overrides(request):
    """Vendor-side holiday override requests (an admin decides in the console)."""
    from dashboard.models import Holiday, HolidayVendorOverride
    from dashboard.serializers import VendorHolidayRequestSerializer
    from dashboard.views.holidays import _vendor_override_to_dict

    try:
        profile = request.user.vendor_profile
    except VendorProfile.DoesNotExist:
        return Response({'error': 'No vendor profile for this account.'}, status=403)

    if request.method == 'POST':
        serializer = VendorHolidayRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            holiday = Holiday.objects.get(id=data['holiday_id'])
        except Holiday.DoesNotExist:
            return Response({'error': 'Unknown holiday.'}, status=404)
        if HolidayVendorOverride.objects.filter(holiday=holiday, vendor_id=profile.id, status='PENDING').exists():
            return Response({'error': 'You already have a pending request for this holiday.'}, status=400)
        override = HolidayVendorOverride.objects.create(
            holiday=holiday,
            vendor_id=profile.id,
            vendor_name=profile.business_name,
            requested_hours=data['requested_hours'],
        )
        return Response({'status': 'ok', 'id': override.id}, status=status.HTTP_201_CREATED)

    overrides = (
        HolidayVendorOverride.objects.select_related('holiday').filter(vendor_id=profile.id).order_by('-requested_at')
    )
    return Response({'results': [_vendor_override_to_dict(o) for o in overrides]})


# ===========================
# VENDOR SUPPORT TICKETS
# ===========================


class VendorSupportTicketViewSet(viewsets.GenericViewSet):
    """List/create/reply support tickets for the authenticated vendor.

    Mirrors DriverSupportTicketViewSet (drivers/views.py). Router basename
    ``vendor-support-tickets`` exposes list/create plus detail actions
    ``messages`` (GET) and ``reply`` (POST) under ``support-tickets/``.
    """

    permission_classes = [IsAuthenticated, IsVendorUser]

    def _get_vendor(self, request):
        try:
            return request.user.vendor_profile
        except VendorProfile.DoesNotExist:
            return None

    def list(self, request):
        vendor = self._get_vendor(request)
        if not vendor:
            return Response({'error': 'Vendor profile not found'}, status=404)
        status_filter = request.query_params.get('status')
        qs = vendor.support_tickets.prefetch_related('messages').all()
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        serializer = VendorSupportTicketSerializer(qs[:50], many=True)
        return Response(serializer.data)

    @throttle_classes([MutationRateThrottle])
    def create(self, request):
        vendor = self._get_vendor(request)
        if not vendor:
            return Response({'error': 'Vendor profile not found'}, status=404)
        ser = VendorCreateSupportTicketSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)

        order = None
        if ser.validated_data.get('order_id'):
            try:
                # Scope to the vendor's own orders (tighter than the driver lane).
                order = Order.objects.get(id=ser.validated_data['order_id'], vendor=request.user)
            except Order.DoesNotExist:
                return Response({'error': 'Order not found'}, status=404)

        ticket = VendorSupportTicket.objects.create(
            vendor=vendor,
            order=order,
            category=ser.validated_data['category'],
            subject=ser.validated_data['subject'],
            description=ser.validated_data['description'],
            priority=ser.validated_data.get('priority', 'MEDIUM'),
        )

        VendorTicketMessage.objects.create(
            ticket=ticket,
            sender_is_vendor=True,
            message=ser.validated_data['description'],
        )

        return Response(
            VendorSupportTicketSerializer(ticket).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary='List support ticket messages',
        responses={200: VendorTicketMessageSerializer(many=True), 404: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        vendor = self._get_vendor(request)
        if not vendor:
            return Response({'error': 'Vendor profile not found'}, status=404)
        try:
            ticket = vendor.support_tickets.get(pk=pk)
        except VendorSupportTicket.DoesNotExist:
            return Response({'error': 'Ticket not found'}, status=404)
        msgs = ticket.messages.all()
        return Response(VendorTicketMessageSerializer(msgs, many=True).data)

    @action(detail=True, methods=['post'])
    @throttle_classes([MutationRateThrottle])
    def reply(self, request, pk=None):
        vendor = self._get_vendor(request)
        if not vendor:
            return Response({'error': 'Vendor profile not found'}, status=404)
        try:
            ticket = vendor.support_tickets.get(pk=pk)
        except VendorSupportTicket.DoesNotExist:
            return Response({'error': 'Ticket not found'}, status=404)

        message_text = request.data.get('message', '').strip()
        if not message_text:
            return Response({'error': 'Message is required'}, status=400)
        if len(message_text) > 5000:
            return Response({'error': 'Message is too long (max 5000 characters)'}, status=400)

        if ticket.status in ('RESOLVED', 'CLOSED'):
            ticket.status = 'IN_PROGRESS'
            ticket.resolved_at = None
            ticket.save(update_fields=['status', 'resolved_at', 'updated_at'])
        else:
            ticket.save(update_fields=['updated_at'])

        msg = VendorTicketMessage.objects.create(
            ticket=ticket,
            sender_is_vendor=True,
            message=message_text,
        )

        return Response(
            VendorTicketMessageSerializer(msg).data,
            status=status.HTTP_201_CREATED,
        )
