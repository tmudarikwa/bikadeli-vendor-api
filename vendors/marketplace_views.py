import logging

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action, throttle_classes
from rest_framework.response import Response

from core.throttles import MutationRateThrottle

from .models import Category, CategoryBestseller, Product, ProductVariant
from .permissions import IsVendorUser
from .serializers import (
    CategorySerializer,
    ProductCreateUpdateSerializer,
    ProductSerializer,
    ProductVariantSerializer,
)

logger = logging.getLogger(__name__)


class MarketplaceProductViewSet(viewsets.ReadOnlyModelViewSet):
    """Public marketplace product listing"""

    queryset = (
        Product.objects.filter(is_available=True)
        .select_related('category', 'vendor')
        .prefetch_related('images', 'variants')
    )
    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['product_type', 'category', 'vendor', 'is_discounted']
    search_fields = ['name', 'description', 'brand', 'barcode']
    ordering_fields = [
        'price',
        'created_at',
        'name',
        'rating',
        'is_popular',
        'bestseller_rank',
        '-price',
        '-is_popular',
        '-bestseller_rank',
    ]
    # Explicit NULLS LAST keeps products without ratings in the list without
    # letting nullable rating values lead the default ordering.
    ordering = [F('is_popular').desc(), F('rating').desc(nulls_last=True), 'name']

    def get_queryset(self):
        qs = super().get_queryset().annotate(
            bestseller_rank=Subquery(
                CategoryBestseller.objects.filter(category=OuterRef('category'), product=OuterRef('pk')).values(
                    'rank'
                )[:1]
            )
        )
        min_price = self.request.query_params.get('min_price')
        max_price = self.request.query_params.get('max_price')
        pickup = self.request.query_params.get('pickup')
        open_now = self.request.query_params.get('open_now')
        free_delivery = self.request.query_params.get('free_delivery')
        rating = self.request.query_params.get('rating')
        max_time = self.request.query_params.get('max_time')

        if min_price:
            qs = qs.filter(price__gte=min_price)
        if max_price:
            qs = qs.filter(price__lte=max_price)
        if pickup == 'true':
            qs = qs.filter(vendor__pickup_available=True)
        if open_now == 'true':
            qs = qs.filter(vendor__is_open=True)
        if free_delivery == 'true':
            qs = qs.filter(vendor__delivery_fee=0)
        if rating:
            qs = qs.filter(vendor__rating__gte=int(rating))
        if max_time:
            qs = qs.filter(vendor__preparation_time_mins__lte=int(max_time))
        popular = self.request.query_params.get('popular')
        if popular == 'true':
            qs = qs.filter(is_popular=True)
        elif popular == 'false':
            qs = qs.filter(is_popular=False)
        in_stock = self.request.query_params.get('in_stock')
        if in_stock != 'false':
            qs = qs.filter(Q(is_inventory_tracked=False) | Q(quantity_on_hand__gt=0))
        new_param = self.request.query_params.get('new')
        if new_param == 'true':
            from datetime import timedelta

            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created_at__gte=cutoff)

        q = self.request.query_params.get('q')
        if q:
            qs = qs.annotate(similarity=TrigramSimilarity('name', q) + TrigramSimilarity('description', q)).filter(
                similarity__gt=0.15
            )

        # Restaurant/fast-food items belong to their store pages, not the
        # marketplace browse surfaces. Kept findable via search and via
        # ?vendor=<id> (store page). Sorting/top-picks stay clean because
        # only real text search (search/q) or an explicit vendor lift this.
        # Scoped to list so retrieve (store-page product modal) still resolves.
        if (
            self.action == 'list'
            and not self.request.query_params.get('vendor')
            and not self.request.query_params.get('search')
            and not self.request.query_params.get('q')
        ):
            qs = qs.exclude(vendor__category__in=['RESTAURANT', 'FAST_FOOD'])

        return qs


class MarketplaceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Public marketplace category listing — top-level shared categories only."""

    queryset = Category.objects.filter(parent=None, vendor=None).prefetch_related('children')
    serializer_class = CategorySerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = None  # small fixed set, no pagination needed


class VendorMarketplaceProductViewSet(viewsets.ModelViewSet):
    """Vendor's own product management for marketplace"""

    permission_classes = [permissions.IsAuthenticated, IsVendorUser]
    serializer_class = ProductSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['product_type', 'category', 'is_available', 'is_inventory_tracked']
    search_fields = ['name', 'description', 'brand', 'barcode']
    ordering_fields = ['name', 'price', 'quantity_on_hand', 'created_at', '-price']

    def get_queryset(self):
        return (
            Product.objects.filter(vendor=self.request.user.vendor_profile)
            .select_related('category', 'vendor')
            .prefetch_related('images', 'variants', 'bestseller_entries')
        )

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ProductCreateUpdateSerializer
        return ProductSerializer

    def perform_create(self, serializer):
        serializer.save(vendor=self.request.user.vendor_profile)

    @action(methods=['post'], detail=True, url_path='variants')
    @throttle_classes([MutationRateThrottle])
    def add_variant(self, request, pk=None):
        try:
            product = self.get_queryset().get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = ProductVariantSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save(product=product)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(methods=['put', 'delete'], detail=True, url_path='variants/(?P<variant_pk>[^/.]+)')
    @throttle_classes([MutationRateThrottle])
    def manage_variant(self, request, pk=None, variant_pk=None):
        try:
            product = self.get_queryset().get(pk=pk)
            variant = product.variants.get(pk=variant_pk)
        except (Product.DoesNotExist, ProductVariant.DoesNotExist):
            return Response({'error': 'Variant not found'}, status=status.HTTP_404_NOT_FOUND)

        if request.method == 'DELETE':
            variant.delete()
            return Response({'message': 'Variant deleted'}, status=status.HTTP_204_NO_CONTENT)

        serializer = ProductVariantSerializer(variant, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)

    @action(methods=['post'], detail=True, url_path='stock-check')
    @throttle_classes([MutationRateThrottle])
    def check_stock(self, request, pk=None):
        try:
            product = self.get_queryset().get(pk=pk)
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        quantity = request.data.get('quantity', 1)
        if product.is_inventory_tracked and product.quantity_on_hand < int(quantity):
            return Response(
                {
                    'available': False,
                    'message': f'Only {product.quantity_on_hand} in stock',
                    'current_stock': product.quantity_on_hand,
                }
            )

        return Response(
            {
                'available': True,
                'current_stock': product.quantity_on_hand,
            }
        )

    @action(methods=['post'], detail=False, url_path='bulk-import')
    @throttle_classes([MutationRateThrottle])
    def bulk_import(self, request):
        import csv
        import io

        from .tasks import process_csv_import

        file = request.FILES.get('file')
        if not file:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_csv_file

        try:
            validate_csv_file(file)
        except DjangoValidationError:
            logger.exception('Marketplace CSV validation failed for vendor %s', request.user.id)
            return Response(
                {'error': 'Invalid CSV file. Please check format and try again.'}, status=status.HTTP_400_BAD_REQUEST
            )

        decoded = file.read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(decoded))
        rows = list(reader)
        if len(rows) > 10_000:
            return Response(
                {'error': 'CSV file too large. Maximum 10,000 rows allowed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        task = process_csv_import.delay(request.user.vendor_profile.id, rows)
        return Response({'task_id': task.id, 'rows': len(rows)})
