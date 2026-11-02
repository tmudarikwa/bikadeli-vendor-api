from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .kyc_uploads import validate_kyc_upload
from .media import public_media_url, validate_image_upload
from .models import (
    CatalogCategory,
    Product,
    ProductImage,
    RestaurantMenuCategory,
    RestaurantMenuItem,
    RestaurantMenuItemImage,
    RestaurantMenuItemOption,
    VendorKYCDocument,
    VendorKYCVerification,
    VendorNotificationOutbox,
    VendorNotificationPreference,
    VendorProfile,
    VendorSanctionsScreening,
    VendorTaxInfo,
    request_catalog_sync,
)


class VendorAvailabilitySerializer(serializers.Serializer):
    is_open = serializers.BooleanField()


class VendorApplicationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    business_name = serializers.CharField(max_length=200, required=False, default='My Business')
    category = serializers.CharField(max_length=32, required=False, allow_blank=True, default='')
    business_address = serializers.CharField(required=False, allow_blank=True, default='')
    business_phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default='')
    business_email = serializers.EmailField(required=False, allow_blank=True, default='')


class InternalVendorVerificationSerializer(serializers.Serializer):
    is_verified = serializers.BooleanField()
    actor_id = serializers.IntegerField(min_value=1)
    actor_email = serializers.EmailField()
    request_id = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')


class InternalVendorKYCReviewSerializer(serializers.Serializer):
    actor_id = serializers.IntegerField(min_value=1)
    actor_email = serializers.EmailField()
    request_id = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class InternalVendorSanctionsReviewSerializer(serializers.Serializer):
    actor_id = serializers.IntegerField(min_value=1)
    actor_email = serializers.EmailField()
    request_id = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    matched_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    matched_fields = serializers.DictField(child=serializers.JSONField(), required=False)
    confidence_score = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        required=False,
        allow_null=True,
    )


class VendorKYCDocumentSerializer(serializers.ModelSerializer):
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    verification_status_display = serializers.CharField(source='get_verification_status_display', read_only=True)
    days_until_expiry = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    reviewed_by = serializers.CharField(source='reviewed_by_email', read_only=True)
    file = serializers.FileField(write_only=True)

    class Meta:
        model = VendorKYCDocument
        fields = [
            'id',
            'document_type',
            'document_type_display',
            'document_subtype',
            'file',
            'file_url',
            'file_size',
            'original_filename',
            'expiry_date',
            'verification_status',
            'verification_status_display',
            'is_required',
            'rejection_reason',
            'days_until_expiry',
            'is_expired',
            'reviewed_by',
            'reviewed_at',
            'uploaded_by_user_id',
            'encryption_key_id',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'file_size',
            'original_filename',
            'encryption_key_id',
            'verification_status',
            'rejection_reason',
            'is_required',
            'reviewed_by',
            'reviewed_at',
            'uploaded_by_user_id',
            'created_at',
            'updated_at',
        ]

    def validate_file(self, value):
        try:
            return validate_kyc_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def validate_expiry_date(self, value):
        if value and value < timezone.localdate():
            raise serializers.ValidationError('Expiry date cannot be in the past.')
        return value

    def validate(self, attrs):
        request = self.context['request']
        vendor = VendorProfile.objects.filter(owner_user_id=request.user.id).first()
        if vendor is None:
            raise serializers.ValidationError({'detail': 'Vendor profile not found.'})
        if VendorKYCDocument.objects.filter(vendor=vendor, document_type=attrs.get('document_type')).exists():
            raise serializers.ValidationError({'document_type': ['A document of this type already exists.']})
        return attrs

    def create(self, validated_data):
        request = self.context['request']
        vendor = VendorProfile.objects.get(owner_user_id=request.user.id)
        upload = validated_data['file']
        validated_data.update(
            vendor=vendor,
            file_size=upload.size,
            original_filename=upload.name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1][:255],
            uploaded_by_user_id=request.user.id,
        )
        return super().create(validated_data)

    def get_days_until_expiry(self, obj):
        return obj.days_until_expiry()

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_file_url(self, obj):
        if not obj.file:
            return None
        if not settings.USE_S3_MEDIA:
            return None
        try:
            return obj.file.url
        except ValueError:
            return None


class VendorKYCVerificationSerializer(serializers.ModelSerializer):
    missing_documents = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    verified_by = serializers.CharField(source='verified_by_email', read_only=True)

    class Meta:
        model = VendorKYCVerification
        fields = [
            'id',
            'vendor_id',
            'overall_status',
            'required_document_types',
            'missing_documents',
            'submitted_at',
            'verified_at',
            'rejected_at',
            'verified_by',
            'rejection_summary',
            'sanctions_check_passed',
            'sanctions_check_date',
            'sanctions_check_details',
            'pep_check_passed',
            'pep_check_date',
            'pep_check_details',
            'next_review_date',
            'documents',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_missing_documents(self, obj):
        return obj.missing_required_types()

    def get_documents(self, obj):
        return VendorKYCDocumentSerializer(obj.vendor.kyc_documents.all(), many=True, context=self.context).data


class PrivateKYCFileField(serializers.FileField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            return validate_kyc_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def to_representation(self, value):
        if not value or not settings.USE_S3_MEDIA:
            return None
        try:
            return value.url
        except ValueError:
            return None


class VendorTaxInfoSerializer(serializers.ModelSerializer):
    zimra_pin_masked = serializers.SerializerMethodField()
    vat_number_masked = serializers.SerializerMethodField()
    zimra_pin = serializers.CharField(write_only=True, required=False, allow_blank=True)
    vat_number = serializers.CharField(write_only=True, required=False, allow_blank=True)
    tax_clearance_certificate = PrivateKYCFileField(required=False, allow_empty_file=False)

    class Meta:
        model = VendorTaxInfo
        fields = [
            'id',
            'vendor',
            'zimra_pin_masked',
            'vat_number_masked',
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
        ]
        read_only_fields = [
            'id',
            'vendor',
            'zimra_pin_verified',
            'zimra_verification_date',
            'zimra_verification_method',
            'created_at',
            'updated_at',
        ]

    def get_zimra_pin_masked(self, obj):
        return '*' * 6 + obj.zimra_pin[-4:] if obj.zimra_pin else None

    def get_vat_number_masked(self, obj):
        return '*' * 6 + obj.vat_number[-4:] if obj.vat_number else None

    def validate_zimra_pin(self, value):
        if value and (len(value) != 10 or not value.isdigit()):
            raise serializers.ValidationError('ZIMRA PIN must be exactly 10 digits.')
        return value


class VendorSanctionsScreeningSerializer(serializers.ModelSerializer):
    screening_type_display = serializers.CharField(source='get_screening_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    reviewed_by = serializers.IntegerField(source='reviewed_by_user_id', read_only=True)
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = VendorSanctionsScreening
        fields = [
            'id',
            'vendor',
            'screening_type',
            'screening_type_display',
            'status',
            'status_display',
            'matched_name',
            'matched_fields',
            'confidence_score',
            'screened_at',
            'reviewed_at',
            'reviewed_by',
            'reviewed_by_name',
            'review_notes',
        ]
        read_only_fields = fields

    def get_reviewed_by_name(self, obj):
        return None


class VendorNotificationSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    channel = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    sent_at = serializers.DateTimeField(source='published_at', read_only=True)
    delivered_at = serializers.SerializerMethodField()
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = VendorNotificationOutbox
        fields = [
            'id',
            'category',
            'priority',
            'channel',
            'title',
            'body',
            'data',
            'status',
            'created_at',
            'sent_at',
            'delivered_at',
            'read_at',
            'is_read',
        ]
        read_only_fields = fields

    def get_category(self, _obj):
        return 'SYSTEM'

    def get_channel(self, _obj):
        return 'IN_APP'

    def get_status(self, obj):
        return 'SENT' if obj.status == 'PUBLISHED' else 'PENDING'

    def get_is_read(self, obj):
        return obj.read_at is not None

    def get_delivered_at(self, _obj):
        return None


class VendorNotificationMarkReadSerializer(serializers.Serializer):
    notification_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=100,
    )


class VendorNotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorNotificationPreference
        fields = ['id', 'channel', 'category', 'reason', 'active', 'created_at']
        read_only_fields = ['id', 'created_at']


class VendorNotificationPreferenceUpdateSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=VendorNotificationPreference.CHANNELS)
    category = serializers.ChoiceField(
        choices=VendorNotificationPreference.CATEGORIES,
        default='MARKETING',
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    active = serializers.BooleanField(default=True)


class VendorProfileSerializer(serializers.ModelSerializer):
    is_open_now = serializers.BooleanField(source='is_currently_open', read_only=True)

    class Meta:
        model = VendorProfile
        fields = [
            'id',
            'business_name',
            'category',
            'description',
            'business_phone',
            'business_email',
            'business_address',
            'is_open',
            'is_open_now',
            'is_active',
            'is_verified',
            'opening_time',
            'closing_time',
            'preparation_time_mins',
            'minimum_order',
            'delivery_fee',
            'pickup_available',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'is_active', 'is_verified', 'created_at', 'updated_at']


class CatalogCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = CatalogCategory
        fields = ['id', 'name', 'slug', 'icon', 'sort_order', 'parent', 'vendor']
        read_only_fields = fields


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)
    images = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'vendor',
            'category',
            'category_name',
            'name',
            'description',
            'sku',
            'price',
            'stock',
            'is_available',
            'barcode',
            'product_type',
            'quantity_on_hand',
            'low_stock_threshold',
            'is_inventory_tracked',
            'dietary_tags',
            'rating',
            'is_discounted',
            'is_popular',
            'compare_at_price',
            'weight_kg',
            'dimensions',
            'brand',
            'images',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'vendor', 'created_at', 'updated_at']

    def get_images(self, obj):
        return ProductImageSerializer(obj.images.all(), many=True, context=self.context).data


class ProductImageSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'alt_text', 'sort_order']
        read_only_fields = fields

    def get_image(self, obj):
        url = f'/api/v1/vendors/product-images/{obj.id}/file/'
        request = self.context.get('request')
        return public_media_url(request, url)


class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(queryset=CatalogCategory.objects.none())
    images = serializers.ListField(
        child=serializers.ImageField(validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])]),
        write_only=True,
        required=False,
    )

    class Meta:
        model = Product
        fields = [
            'name',
            'description',
            'category',
            'price',
            'is_available',
            'dietary_tags',
            'barcode',
            'images',
            'is_discounted',
            'compare_at_price',
            'product_type',
            'quantity_on_hand',
            'low_stock_threshold',
            'is_inventory_tracked',
            'weight_kg',
            'dimensions',
            'brand',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        user_id = getattr(getattr(request, 'user', None), 'id', None)
        self.fields['category'].queryset = CatalogCategory.objects.filter(
            Q(vendor__isnull=True) | Q(vendor__owner_user_id=user_id)
        )

    def validate_images(self, value):
        for image in value:
            error = validate_image_upload(image)
            if error:
                raise serializers.ValidationError(f'{image.name}: {error}')
        return value

    def validate(self, attrs):
        compare_at = attrs.get('compare_at_price', getattr(self.instance, 'compare_at_price', None))
        price = attrs.get('price', getattr(self.instance, 'price', None))
        if compare_at is not None and price is not None and compare_at <= price:
            raise serializers.ValidationError(
                {'compare_at_price': 'Must exceed the sale price to display a discount.'}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        images = validated_data.pop('images', [])
        vendor = validated_data.pop('vendor', None) or VendorProfile.objects.get(
            owner_user_id=self.context['request'].user.id
        )
        product = Product.objects.create(vendor=vendor, **validated_data)
        ProductImage.objects.bulk_create(
            [ProductImage(product=product, image=image, sort_order=index) for index, image in enumerate(images)]
        )
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        images = validated_data.pop('images', None)
        instance = super().update(instance, validated_data)
        if images is not None:
            instance.images.all().delete()
            ProductImage.objects.bulk_create(
                [ProductImage(product=instance, image=image, sort_order=index) for index, image in enumerate(images)]
            )
        return instance

    def to_representation(self, instance):
        return ProductSerializer(instance, context=self.context).data


class RestaurantMenuItemOptionSerializer(serializers.ModelSerializer):
    name = serializers.CharField(max_length=100)
    price_adjustment = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, default=0)

    class Meta:
        model = RestaurantMenuItemOption
        fields = ['id', 'name', 'price_adjustment']
        read_only_fields = ['id']


class RestaurantMenuItemImageSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    vendor_id = serializers.IntegerField(source='menu_item.vendor_id', read_only=True)

    class Meta:
        model = RestaurantMenuItemImage
        fields = ['id', 'image', 'alt_text', 'sort_order', 'vendor_id']
        read_only_fields = fields

    def get_image(self, obj):
        url = f'/api/v1/vendors/menu-images/{obj.id}/file/'
        request = self.context.get('request')
        return public_media_url(request, url)


class RestaurantMenuItemSerializer(serializers.ModelSerializer):
    options = RestaurantMenuItemOptionSerializer(many=True, required=False)
    images = RestaurantMenuItemImageSerializer(many=True, read_only=True)
    category_id = serializers.IntegerField(required=False, allow_null=True)
    category_name = serializers.SerializerMethodField()
    vendor_id = serializers.IntegerField(read_only=True)
    vendor = serializers.IntegerField(source='vendor_id', read_only=True)
    vendor_name = serializers.SerializerMethodField()
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    image = serializers.SerializerMethodField()

    class Meta:
        model = RestaurantMenuItem
        fields = [
            'id',
            'vendor_id',
            'category_id',
            'name',
            'description',
            'price',
            'image',
            'is_available',
            'is_popular',
            'preparation_time_mins',
            'category_name',
            'vendor_id',
            'vendor',
            'vendor_name',
            'created_at',
            'updated_at',
            'images',
            'options',
        ]
        read_only_fields = ['id', 'vendor_id', 'vendor', 'vendor_name', 'category_name', 'created_at', 'updated_at']

    def validate_category_id(self, value):
        if value is not None and not RestaurantMenuCategory.objects.filter(
            id=value,
            vendor_id=self.context['vendor'].id,
            is_active=True,
        ).exists():
            raise serializers.ValidationError('Category not found.')
        return value

    def create(self, validated_data):
        options = validated_data.pop('options', [])
        category_id = validated_data.pop('category_id', None)
        with transaction.atomic():
            item = RestaurantMenuItem.objects.create(
                vendor_id=self.context['vendor'].id,
                category_id=category_id,
                **validated_data,
            )
            RestaurantMenuItemOption.objects.bulk_create(
                [RestaurantMenuItemOption(menu_item=item, **option) for option in options]
            )
            request_catalog_sync(item.vendor_id)
        return item

    def update(self, instance, validated_data):
        options = validated_data.pop('options', None)
        if 'category_id' in validated_data:
            instance.category_id = validated_data.pop('category_id')
        with transaction.atomic():
            for field, value in validated_data.items():
                setattr(instance, field, value)
            instance.updated_at = timezone.now()
            instance.save()
            if options is not None:
                instance.options.all().delete()
                RestaurantMenuItemOption.objects.bulk_create(
                    [RestaurantMenuItemOption(menu_item=instance, **option) for option in options]
                )
            request_catalog_sync(instance.vendor_id)
        return instance

    def get_image(self, obj):
        if not obj.image:
            return None
        url = f'/api/v1/vendors/menu/{obj.id}/image/'
        request = self.context.get('request')
        return public_media_url(request, url)

    def get_vendor_name(self, obj):
        vendor = self.context.get('vendor')
        return vendor.business_name if vendor else ''

    def get_category_name(self, obj):
        return self.context.get('categories', {}).get(obj.category_id, '')

class RestaurantMenuCategorySerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = RestaurantMenuCategory
        fields = ['id', 'name', 'description', 'order', 'items']
        read_only_fields = ['id', 'items']

    def get_items(self, obj):
        items = RestaurantMenuItem.objects.filter(
            category_id=obj.id,
            vendor_id=self.context['vendor'].id,
        ).prefetch_related('options', 'images')
        if not self.context.get('include_unavailable'):
            items = items.filter(is_available=True)
        return RestaurantMenuItemSerializer(items, many=True, context=self.context).data


class RestaurantMenuCategoryInputSerializer(serializers.ModelSerializer):
    order = serializers.IntegerField(required=False)

    class Meta:
        model = RestaurantMenuCategory
        fields = ['name', 'description', 'order']
        extra_kwargs = {
            'name': {'max_length': 100},
            'description': {'required': False, 'allow_blank': True},
            'order': {'required': False},
        }


class PublicRestaurantMenuItemSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    vendor_is_open = serializers.SerializerMethodField()
    vendor_is_active = serializers.SerializerMethodField()
    minimum_order = serializers.SerializerMethodField()
    options = RestaurantMenuItemOptionSerializer(many=True, read_only=True)

    class Meta:
        model = RestaurantMenuItem
        fields = [
            'id',
            'vendor_id',
            'vendor_name',
            'vendor_is_open',
            'vendor_is_active',
            'minimum_order',
            'category_id',
            'name',
            'description',
            'price',
            'image',
            'is_available',
            'is_popular',
            'preparation_time_mins',
            'created_at',
            'updated_at',
            'options',
        ]

    def _vendor(self, obj):
        return self.context['vendors'].get(obj.vendor_id)

    def get_vendor_name(self, obj):
        vendor = self._vendor(obj)
        return vendor.business_name if vendor else ''

    def get_vendor_is_open(self, obj):
        vendor = self._vendor(obj)
        return vendor.is_open if vendor else False

    def get_vendor_is_active(self, obj):
        vendor = self._vendor(obj)
        return vendor.is_active if vendor else False

    def get_minimum_order(self, obj):
        vendor = self._vendor(obj)
        return vendor.minimum_order if vendor else 0


class PublicVendorSerializer(serializers.ModelSerializer):
    is_open_now = serializers.BooleanField(source='is_currently_open', read_only=True)

    class Meta:
        model = VendorProfile
        fields = [
            'id',
            'business_name',
            'category',
            'description',
            'is_open_now',
            'preparation_time_mins',
            'minimum_order',
            'delivery_fee',
            'pickup_available',
        ]
        read_only_fields = fields


class PublicProductSerializer(serializers.ModelSerializer):
    vendor_id = serializers.IntegerField(read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = ['id', 'vendor_id', 'category_name', 'name', 'description', 'price', 'images']
        read_only_fields = fields


class MarketplaceCategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = CatalogCategory
        fields = ['id', 'name', 'icon', 'parent', 'children', 'sort_order']
        read_only_fields = fields

    def get_children(self, obj):
        depth = self.context.get('category_depth', 1)
        if depth <= 0:
            return []
        children = getattr(obj, 'active_children', None)
        if children is None:
            children = obj.children.filter(is_active=True).order_by('sort_order', 'name', 'id')
        return MarketplaceCategorySerializer(
            children,
            many=True,
            context={**self.context, 'category_depth': depth - 1},
        ).data


class MarketplaceProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)
    vendor_name = serializers.CharField(source='vendor.business_name', read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    stock_status = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()
    product_type_display = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'name',
            'description',
            'category',
            'category_name',
            'vendor',
            'vendor_name',
            'price',
            'is_available',
            'is_discounted',
            'compare_at_price',
            'dietary_tags',
            'product_type',
            'product_type_display',
            'stock_status',
            'is_low_stock',
            'is_popular',
            'rating',
            'weight_kg',
            'dimensions',
            'brand',
            'images',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_stock_status(self, obj):
        if not obj.is_inventory_tracked:
            return 'NOT_TRACKED'
        if obj.quantity_on_hand <= 0:
            return 'OUT_OF_STOCK'
        if obj.quantity_on_hand <= obj.low_stock_threshold:
            return 'LOW_STOCK'
        return 'IN_STOCK'

    def get_is_low_stock(self, obj):
        return obj.is_inventory_tracked and obj.quantity_on_hand <= obj.low_stock_threshold

    def get_product_type_display(self, obj):
        return {
            'FOOD': 'Food',
            'ELECTRONICS': 'Electronics',
            'FASHION': 'Fashion',
            'GROCERY': 'Grocery',
            'HOME': 'Home & Garden',
            'PARCEL': 'Parcel',
            'OTHER': 'Other',
        }.get(obj.product_type, obj.product_type)
