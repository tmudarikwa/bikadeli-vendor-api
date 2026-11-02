from django.contrib.auth import get_user_model
from django.core.validators import FileExtensionValidator
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Category,
    InventoryAdjustmentLog,
    MenuCategory,
    MenuItem,
    MenuItemImage,
    MenuItemOption,
    Product,
    ProductImage,
    ProductVariant,
    Promotion,
    VendorKYCDocument,
    VendorKYCExpiryLog,
    VendorKYCVerification,
    VendorProfile,
    VendorSanctionsScreening,
    VendorSupportTicket,
    VendorTaxInfo,
    VendorTicketMessage,
)

User = get_user_model()


class VendorUserSerializer(serializers.ModelSerializer):
    """Serializer for vendor user data"""

    business_name = serializers.SerializerMethodField()
    vendor_id = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'user_type', 'business_name', 'vendor_id']

    def get_business_name(self, obj):
        try:
            return obj.vendor_profile.business_name
        except VendorProfile.DoesNotExist:
            return None

    def get_vendor_id(self, obj):
        try:
            return obj.vendor_profile.id
        except VendorProfile.DoesNotExist:
            return None


# ==========================================
# Vendor Profile Serializers (EXISTING)
# ==========================================
class VendorProfileSerializer(serializers.ModelSerializer):
    """Serializer for VendorProfile"""

    full_name = serializers.SerializerMethodField()
    email = serializers.EmailField(source='user.email', read_only=True)
    phone = serializers.CharField(source='user.phone_number', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    is_open_now = serializers.SerializerMethodField()

    class Meta:
        model = VendorProfile
        fields = [
            'id',
            'user',
            'full_name',
            'email',
            'phone',
            'business_name',
            'business_logo',
            'business_cover',
            'category',
            'category_display',
            'description',
            'business_address',
            'business_phone',
            'business_email',
            'business_location',
            'what3words',
            'is_open',
            'is_open_now',
            'opening_time',
            'closing_time',
            'is_verified',
            'is_active',
            'preparation_time_mins',
            'delivery_radius_km',
            'minimum_order',
            'delivery_fee',
            'pickup_available',
            'eco_cash_number',
            'bank_name',
            'bank_account',
            'total_orders',
            'rating',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['total_orders', 'rating', 'created_at', 'is_verified', 'is_active', 'delivery_radius_km']

    def get_full_name(self, obj):
        return obj.user.get_full_name()

    def get_is_open_now(self, obj):
        return obj.is_currently_open()


class PublicVendorSerializer(serializers.ModelSerializer):
    """Serializer for public vendor listings.

    Excludes precise/sensitive fields (business_location, what3words, auth
    user). business_phone / business_email are intentionally public contact
    fields for marketplace listings.
    """

    category_display = serializers.CharField(source='get_category_display', read_only=True)
    distance_km = serializers.SerializerMethodField()
    is_open_now = serializers.SerializerMethodField()
    beyond_recommended = serializers.SerializerMethodField()
    matching_menu_items = serializers.SerializerMethodField()

    class Meta:
        model = VendorProfile
        fields = [
            'id',
            'business_name',
            'description',
            'category',
            'category_display',
            'minimum_order',
            'delivery_fee',
            'pickup_available',
            'business_phone',
            'business_email',
            'business_address',
            'rating',
            'is_open',
            'preparation_time_mins',
            'delivery_radius_km',
            'distance_km',
            'is_open_now',
            'beyond_recommended',
            'matching_menu_items',
        ]

    def get_distance_km(self, obj):
        d = getattr(obj, 'distance_km', None)
        try:
            return round(float(d), 1) if d is not None else None
        except (TypeError, ValueError):
            return None

    def get_is_open_now(self, obj):
        return obj.is_currently_open()

    def get_beyond_recommended(self, obj):
        return bool(getattr(obj, 'beyond_recommended', False))

    def get_matching_menu_items(self, obj):
        request = self.context.get('request')
        if not request or not request.query_params.get('search', '').strip():
            return []
        items = getattr(obj, 'search_matches_menu_items', ())
        return [{'id': item.id, 'name': item.name, 'price': str(item.price)} for item in items]


class VendorDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorProfile
        fields = [
            'id',
            'user',
            'business_name',
            'business_logo',
            'business_cover',
            'category',
            'description',
            'business_phone',
            'business_email',
            'business_address',
            'business_location',
            'what3words',
            'is_open',
            'is_active',
            'is_verified',
            'rating',
            'total_orders',
            'preparation_time_mins',
            'delivery_fee',
            'minimum_order',
            'opening_time',
            'closing_time',
            'created_at',
            'updated_at',
            'delivery_radius_km',
        ]


class VendorListSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorProfile
        fields = ['id', 'business_name', 'description', 'is_active']


# ==========================================
# Menu Serializers (EXISTING)
# ==========================================
class MenuItemOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItemOption
        fields = ['id', 'name', 'price_adjustment']


class MenuItemImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItemImage
        fields = ['id', 'image', 'alt_text', 'sort_order']


class MenuItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    vendor = serializers.PrimaryKeyRelatedField(read_only=True)
    vendor_name = serializers.CharField(source='vendor.business_name', read_only=True)
    images = MenuItemImageSerializer(many=True, read_only=True)
    options = MenuItemOptionSerializer(many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            'id',
            'name',
            'description',
            'price',
            'is_available',
            'is_popular',
            'image',
            'images',
            'options',
            'category_name',
            'vendor',
            'vendor_name',
        ]


class MenuCategorySerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = MenuCategory
        fields = ['id', 'name', 'description', 'order', 'items']

    def get_items(self, obj):
        # obj.items.all() uses prefetch cache when prefetch_related('items') is set.
        # Filter in Python to avoid bypassing cache with .filter()
        all_items = obj.items.all()
        if not self.context.get('include_unavailable'):
            all_items = [i for i in all_items if i.is_available]
        return MenuItemSerializer(all_items, many=True).data


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'name', 'icon', 'parent', 'children', 'sort_order']

    def get_children(self, obj):
        depth = self.context.get('category_depth', 1)
        if depth <= 0 or depth > 3:
            return []
        # obj.children.all() uses prefetch cache when prefetch_related('children') is set
        return CategorySerializer(
            obj.children.all(), many=True, context={**self.context, 'category_depth': depth - 1}
        ).data


class ProductVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'price_adjustment', 'quantity_on_hand', 'sku', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'alt_text', 'sort_order']


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    category_name = serializers.SerializerMethodField()
    vendor_name = serializers.CharField(source='vendor.business_name', read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    stock_status = serializers.ReadOnlyField()
    is_low_stock = serializers.ReadOnlyField()
    bestseller_rank = serializers.SerializerMethodField()
    product_type_display = serializers.CharField(source='get_product_type_display', read_only=True)

    def get_category_name(self, obj):
        if obj.category and obj.category.name and obj.category.name.strip():
            return obj.category.name.strip()
        return None

    def get_bestseller_rank(self, obj):
        """Rank within the product's Category row; None when not a top seller."""
        rank = getattr(obj, 'bestseller_rank', None)
        if rank is not None:
            return rank
        entries = getattr(obj, 'bestseller_entries', None)
        entry = entries.first() if entries is not None else None
        return entry.rank if entry else None

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
            'unit',
            'is_available',
            'is_discounted',
            'compare_at_price',
            'dietary_tags',
            'product_type',
            'product_type_display',
            'stock_status',
            'is_low_stock',
            'is_popular',
            'bestseller_rank',
            'rating',
            'weight_kg',
            'dimensions',
            'brand',
            'images',
            'variants',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['vendor', 'is_popular', 'created_at', 'updated_at']


class ProductCreateUpdateSerializer(serializers.ModelSerializer):
    images = serializers.ListField(
        child=serializers.ImageField(validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])]),
        write_only=True,
        required=False,
    )
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.none(),
        required=True,
        allow_null=False,
        error_messages={'required': 'Category is required.', 'null': 'Category may not be null.'},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            try:
                vendor = request.user.vendor_profile
            except VendorProfile.DoesNotExist:
                vendor = None
            if vendor is None:
                self.fields['category'].queryset = Category.objects.filter(vendor__isnull=True)
            else:
                self.fields['category'].queryset = Category.objects.filter(
                    Q(vendor=vendor) | Q(vendor__isnull=True)
                )

    class Meta:
        model = Product
        fields = [
            'name',
            'description',
            'category',
            'price',
            'unit',
            'is_available',
            'dietary_tags',
            'barcode',
            'images',
            'rating',
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
        read_only_fields = ['vendor', 'rating', 'created_at', 'updated_at']
        extra_kwargs = {
            'category': {'required': True, 'allow_null': False},
        }

    def validate_category(self, value):
        if value is None or not str(value).strip():
            raise serializers.ValidationError('Category is required.')
        return value

    def validate(self, attrs):
        compare_at = attrs.get('compare_at_price', getattr(self.instance, 'compare_at_price', None))
        price = attrs.get('price', getattr(self.instance, 'price', None))
        if compare_at is not None and price is not None and compare_at <= price:
            raise serializers.ValidationError({'compare_at_price': 'Must exceed the sale price to display a discount.'})
        return attrs

    def validate_images(self, value):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_media_upload

        for img in value:
            try:
                validate_media_upload(img, category='product')
            except DjangoValidationError as e:
                raise serializers.ValidationError(str(e)) from e
        return value

    def create(self, validated_data):
        images_data = validated_data.pop('images', [])
        # perform_create() already passes vendor through serializer.save(vendor=...);
        # pop it so Product.objects.create() doesn't receive the kwarg twice.
        vendor = validated_data.pop('vendor', None) or self.context['request'].user.vendor_profile
        product = Product.objects.create(vendor=vendor, **validated_data)
        ProductImage.objects.bulk_create(
            [ProductImage(product=product, image=img, sort_order=idx) for idx, img in enumerate(images_data)]
        )
        return product

    def update(self, instance, validated_data):
        images_data = validated_data.pop('images', None)
        instance = super().update(instance, validated_data)
        if images_data is not None:
            instance.images.all().delete()
            ProductImage.objects.bulk_create(
                [ProductImage(product=instance, image=img, sort_order=idx) for idx, img in enumerate(images_data)]
            )
        return instance


class InventoryItemSerializer(serializers.ModelSerializer):
    category_name = serializers.SerializerMethodField()
    current_stock = serializers.IntegerField(source='quantity_on_hand', read_only=True)
    min_stock = serializers.IntegerField(source='low_stock_threshold', read_only=True)
    is_low_stock = serializers.SerializerMethodField()

    def get_category_name(self, obj):
        if obj.category and obj.category.name and obj.category.name.strip():
            return obj.category.name.strip()
        return None

    class Meta:
        model = Product
        fields = ['id', 'name', 'current_stock', 'min_stock', 'unit', 'category_name', 'is_low_stock', 'updated_at']

    def get_is_low_stock(self, obj):
        return obj.is_inventory_tracked and obj.quantity_on_hand <= obj.low_stock_threshold


class InventoryAdjustmentLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryAdjustmentLog
        fields = ['id', 'quantity', 'note', 'created_at']


class PromotionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Promotion
        fields = [
            'id',
            'vendor',
            'code',
            'description',
            'discount_type',
            'discount_value',
            'max_discount_amount',
            'min_order_amount',
            'usage_limit',
            'times_used',
            'is_active',
            'valid_from',
            'valid_until',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'vendor', 'times_used', 'created_at']
        extra_kwargs = {
            'valid_from': {'required': False, 'allow_null': True},
            'valid_until': {'required': False, 'allow_null': True},
            'max_discount_amount': {'required': False, 'allow_null': True},
        }

    def validate_valid_from(self, value):
        return value or None

    def validate_valid_until(self, value):
        return value or None

    def validate_max_discount_amount(self, value):
        if value == '' or value is None:
            return None
        return value


class ToggleOpenSerializer(serializers.Serializer):
    is_open = serializers.BooleanField()


class AcceptOrderSerializer(serializers.Serializer):
    order_id = serializers.IntegerField()
    preparation_time = serializers.IntegerField(min_value=1, required=False)


class RejectOrderSerializer(serializers.Serializer):
    order_id = serializers.IntegerField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AddCategorySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)


class MenuItemOptionInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    price_adjustment = serializers.DecimalField(max_digits=10, decimal_places=2, default=0)


class AddMenuItemSerializer(serializers.Serializer):
    category_id = serializers.IntegerField()
    name = serializers.CharField(max_length=200)
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    description = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    is_available = serializers.BooleanField(default=True)
    options = MenuItemOptionInputSerializer(many=True, required=False)


class InventoryAdjustSerializer(serializers.Serializer):
    quantity = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


# ─────────────────────────────────────────────────────────────────────────
# KYC Compliance Serializers
# ─────────────────────────────────────────────────────────────────────────


class VendorKYCDocumentSerializer(serializers.ModelSerializer):
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    verification_status_display = serializers.CharField(source='get_verification_status_display', read_only=True)
    days_until_expiry = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    reviewed_by_name = serializers.CharField(source='reviewed_by.get_full_name', read_only=True)
    uploaded_by_name = serializers.CharField(source='uploaded_by.get_full_name', read_only=True)
    file_url = serializers.SerializerMethodField()

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
            'reviewed_by_name',
            'reviewed_at',
            'uploaded_by',
            'uploaded_by_name',
            'encryption_key_id',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'file_size',
            'original_filename',
            'encryption_key_id',
            'reviewed_by',
            'reviewed_at',
            'uploaded_by',
            'created_at',
            'updated_at',
        ]

    def get_days_until_expiry(self, obj):
        return obj.days_until_expiry()

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_file_url(self, obj):
        if obj.file:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return None

    def validate_file(self, value):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from core.file_validation import validate_media_upload

        try:
            validate_media_upload(value, category='kyc')
        except DjangoValidationError as e:
            raise serializers.ValidationError(str(e)) from e
        return value

    def validate_expiry_date(self, value):
        if value and value < timezone.localdate():
            raise serializers.ValidationError('Expiry date cannot be in the past')
        return value


class VendorKYCDocumentUploadSerializer(VendorKYCDocumentSerializer):
    """Serializer for uploading KYC documents"""

    class Meta(VendorKYCDocumentSerializer.Meta):
        read_only_fields = VendorKYCDocumentSerializer.Meta.read_only_fields + [
            'verification_status',
            'rejection_reason',
        ]

    def create(self, validated_data):
        validated_data['uploaded_by'] = self.context['request'].user
        validated_data['file_size'] = validated_data['file'].size
        validated_data['original_filename'] = validated_data['file'].name
        return super().create(validated_data)


class VendorKYCVerificationSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.business_name', read_only=True)
    overall_status_display = serializers.CharField(source='get_overall_status_display', read_only=True)
    missing_documents = serializers.SerializerMethodField()
    documents = VendorKYCDocumentSerializer(many=True, read_only=True)
    verified_by_name = serializers.CharField(source='verified_by.get_full_name', read_only=True)

    class Meta:
        model = VendorKYCVerification
        fields = [
            'id',
            'vendor',
            'vendor_name',
            'overall_status',
            'overall_status_display',
            'required_document_types',
            'missing_documents',
            'submitted_at',
            'verified_at',
            'rejected_at',
            'verified_by',
            'verified_by_name',
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
        return obj.get_missing_required_types()


class VendorTaxInfoSerializer(serializers.ModelSerializer):
    zimra_pin_masked = serializers.SerializerMethodField()
    vat_number_masked = serializers.SerializerMethodField()

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
        extra_kwargs = {
            'zimra_pin': {'write_only': True},
            'vat_number': {'write_only': True},
        }

    def get_zimra_pin_masked(self, obj):
        if obj.zimra_pin:
            return '*' * 6 + obj.zimra_pin[-4:]
        return None

    def get_vat_number_masked(self, obj):
        if obj.vat_number:
            return '*' * 6 + obj.vat_number[-4:]
        return None


class VendorSanctionsScreeningSerializer(serializers.ModelSerializer):
    screening_type_display = serializers.CharField(source='get_screening_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.get_full_name', read_only=True)

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
        read_only_fields = ['id', 'screened_at', 'vendor']


class VendorKYCExpiryLogSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = VendorKYCExpiryLog
        fields = ['id', 'vendor', 'kyc_document', 'action', 'action_display', 'message', 'metadata', 'created_at']
        read_only_fields = fields


class VendorSupportTicketSerializer(serializers.ModelSerializer):
    """Serializer for vendor support tickets (mirrors driver lane)."""

    message_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = VendorSupportTicket
        fields = [
            'id',
            'category',
            'subject',
            'description',
            'priority',
            'status',
            'resolution_notes',
            'created_at',
            'updated_at',
            'resolved_at',
            'message_count',
            'last_message',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'resolved_at']

    def get_message_count(self, obj):
        cached = getattr(obj, '_prefetched_objects_cache', {}).get('messages')
        if cached is not None:
            return len(cached)
        return obj.messages.count()

    def get_last_message(self, obj):
        cached = getattr(obj, '_prefetched_objects_cache', {}).get('messages')
        if cached is not None:
            last = sorted(cached, key=lambda m: m.created_at, reverse=True)[:1]
            last = last[0] if last else None
        else:
            last = obj.messages.order_by('-created_at').first()
        if last:
            return {
                'message': last.message[:120],
                'sender_is_vendor': last.sender_is_vendor,
                'created_at': last.created_at.isoformat(),
            }
        return None


class VendorTicketMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorTicketMessage
        fields = ['id', 'sender_is_vendor', 'message', 'created_at']


class VendorCreateSupportTicketSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=VendorSupportTicket.CATEGORY_CHOICES)
    subject = serializers.CharField(max_length=200)
    description = serializers.CharField()
    priority = serializers.ChoiceField(choices=VendorSupportTicket.PRIORITY_CHOICES, default='MEDIUM')
    order_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class VendorComplianceSummarySerializer(serializers.Serializer):
    """Summary serializer for vendor compliance dashboard"""

    vendor_id = serializers.IntegerField()
    business_name = serializers.CharField()
    kyc_status = serializers.CharField()
    is_kyc_verified = serializers.BooleanField()
    missing_documents = serializers.ListField(child=serializers.CharField())
    expiring_documents = serializers.ListField(child=serializers.DictField())
    sanctions_status = serializers.CharField()
    pep_status = serializers.CharField()
    zimra_pin_verified = serializers.BooleanField()
    should_be_suspended = serializers.BooleanField()
    next_review_date = serializers.DateField(allow_null=True)
