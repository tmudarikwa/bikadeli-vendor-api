from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .fields import EncryptedCharField
from .kyc_storage import kyc_document_storage


class VendorProfile(models.Model):
    owner_user_id = models.PositiveBigIntegerField(unique=True, db_index=True)
    business_name = models.CharField(max_length=200)
    category = models.CharField(max_length=32)
    description = models.TextField(blank=True)
    business_phone = EncryptedCharField(max_length=32, blank=True)
    business_email = models.EmailField(blank=True)
    business_address = models.TextField(blank=True)
    is_open = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    is_verified = models.BooleanField(default=False, db_index=True)
    opening_time = models.TimeField(null=True, blank=True)
    closing_time = models.TimeField(null=True, blank=True)
    preparation_time_mins = models.PositiveSmallIntegerField(default=15)
    minimum_order = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    pickup_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['business_name', 'id']

    def __str__(self):
        return self.business_name

    def is_currently_open(self):
        if not self.is_open:
            return False
        if self.opening_time is None or self.closing_time is None:
            return True
        now = timezone.localtime().time()
        if self.opening_time <= self.closing_time:
            return self.opening_time <= now <= self.closing_time
        return now >= self.opening_time or now <= self.closing_time

    def kyc_status(self):
        try:
            return self.kyc_verification.overall_status
        except VendorKYCVerification.DoesNotExist:
            return 'INCOMPLETE'

    def is_kyc_verified(self):
        return self.kyc_status() == 'VERIFIED'

    def should_be_suspended(self):
        return self.kyc_documents.filter(
            is_required=True,
            expiry_date__lt=timezone.localdate(),
            verification_status__in=['VERIFIED', 'PENDING', 'UNDER_REVIEW', 'EXPIRED'],
        ).exists()

    def save(self, *args, **kwargs):
        if not self._state.adding and 'updated_at' not in kwargs.get('update_fields', ()):
            self.updated_at = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = tuple(kwargs['update_fields']) + ('updated_at',)
        super().save(*args, **kwargs)


class VendorPreparationTimeUpdate(models.Model):
    vendor = models.ForeignKey(
        VendorProfile, on_delete=models.PROTECT, related_name="preparation_time_updates"
    )
    order_id = models.PositiveBigIntegerField(unique=True)
    previous_minutes = models.PositiveSmallIntegerField()
    new_minutes = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Preparation-time audit records are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Preparation-time audit records are immutable.")


class VendorProfileAuditLog(models.Model):
    vendor = models.ForeignKey(VendorProfile, on_delete=models.PROTECT, related_name='audit_entries')
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField()
    action = models.CharField(max_length=40)
    previous_is_verified = models.BooleanField()
    is_verified = models.BooleanField()
    previous_is_active = models.BooleanField()
    is_active = models.BooleanField()
    request_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']


class VendorKYCDocument(models.Model):
    DOCUMENT_TYPES = [
        ('NATIONAL_ID', 'National Identity Document'),
        ('BUSINESS_LICENSE', 'Business License / Certificate of Incorporation'),
        ('TAX_CLEARANCE', 'Tax Clearance Certificate'),
        ('BANK_STATEMENT', 'Bank Statement / Account Verification'),
        ('PROOF_OF_ADDRESS', 'Proof of Business Address'),
        ('FOOD_LICENSE', 'Food Service License / Health Permit'),
        ('LIQUOR_LICENSE', 'Liquor License'),
        ('PHARMACY_LICENSE', 'Pharmacy License'),
        ('OTHER', 'Other'),
    ]
    VERIFICATION_STATUSES = [
        ('PENDING', 'Pending Review'),
        ('UNDER_REVIEW', 'Under Review'),
        ('VERIFIED', 'Verified'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='kyc_documents')
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)
    document_subtype = models.CharField(max_length=100, blank=True)
    file = models.FileField(upload_to='vendors/kyc/%Y/%m/', storage=kyc_document_storage)
    file_size = models.PositiveIntegerField()
    original_filename = models.CharField(max_length=255)
    expiry_date = models.DateField(null=True, blank=True)
    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUSES, default='PENDING')
    is_required = models.BooleanField(default=True)
    rejection_reason = models.TextField(blank=True)
    reviewed_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    reviewed_by_email = models.EmailField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    uploaded_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    encryption_key_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['vendor', 'document_type', 'verification_status']),
            models.Index(fields=['verification_status', 'expiry_date']),
            models.Index(fields=['vendor', 'is_required']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['vendor', 'document_type'], name='uq_kyc_doc_vendor_type'),
        ]

    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.localdate())

    def days_until_expiry(self):
        return (self.expiry_date - timezone.localdate()).days if self.expiry_date else None


class VendorKYCVerification(models.Model):
    STATUSES = [
        ('INCOMPLETE', 'Incomplete - Missing Documents'),
        ('PENDING', 'Pending - Submitted, Awaiting Review'),
        ('UNDER_REVIEW', 'Under Review - Admin Checking'),
        ('VERIFIED', 'Fully Verified'),
        ('REJECTED', 'Rejected - One or More Documents Failed'),
        ('EXPIRED', 'Expired - Documents Need Renewal'),
        ('SUSPENDED', 'Suspended - KYC Lapsed'),
    ]

    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='kyc_verification')
    overall_status = models.CharField(max_length=20, choices=STATUSES, default='INCOMPLETE', db_index=True)
    required_document_types = models.JSONField(default=list)
    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    verified_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    verified_by_email = models.EmailField(blank=True)
    rejection_summary = models.TextField(blank=True)
    sanctions_check_passed = models.BooleanField(default=False)
    sanctions_check_date = models.DateTimeField(null=True, blank=True)
    sanctions_check_details = models.JSONField(default=dict, blank=True)
    pep_check_passed = models.BooleanField(default=False)
    pep_check_date = models.DateTimeField(null=True, blank=True)
    pep_check_details = models.JSONField(default=dict, blank=True)
    next_review_date = models.DateField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def missing_required_types(self):
        uploaded = set(
            self.vendor.kyc_documents.values_list('document_type', flat=True)
        )
        return sorted(set(self.required_document_types) - uploaded)

    def required_documents(self):
        return self.vendor.kyc_documents.filter(
            models.Q(is_required=True) | models.Q(document_type__in=self.required_document_types)
        )

    def all_required_verified(self):
        required = self.required_documents()
        return (
            required.exists()
            and not self.missing_required_types()
            and not required.exclude(verification_status='VERIFIED').exists()
            and not required.filter(expiry_date__lt=timezone.localdate()).exists()
        )

    def refresh_status(self):
        required = self.required_documents()
        if not required.exists() or self.missing_required_types():
            self.overall_status = 'INCOMPLETE'
        elif required.filter(verification_status='REJECTED').exists():
            self.overall_status = 'REJECTED'
        elif required.filter(verification_status='EXPIRED').exists() or required.filter(
            expiry_date__lt=timezone.localdate()
        ).exists():
            self.overall_status = 'EXPIRED'
        elif required.exclude(verification_status='VERIFIED').exists():
            self.overall_status = 'PENDING'
        else:
            self.overall_status = 'VERIFIED'
        self.updated_at = timezone.now()
        self.save(update_fields=['overall_status', 'updated_at'])
        return self.overall_status


class VendorTaxInfo(models.Model):
    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='tax_info')
    zimra_pin = EncryptedCharField(max_length=50, blank=True)
    zimra_pin_verified = models.BooleanField(default=False)
    zimra_verification_date = models.DateTimeField(null=True, blank=True)
    zimra_verification_method = models.CharField(max_length=50, blank=True)
    vat_registered = models.BooleanField(default=False)
    vat_number = EncryptedCharField(max_length=50, blank=True)
    tax_clearance_certificate = models.FileField(
        upload_to='vendors/tax/%Y/%m/',
        storage=kyc_document_storage,
        blank=True,
    )
    tax_clearance_expiry = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']


class VendorSanctionsScreening(models.Model):
    SCREENING_TYPES = [
        ('OFAC', 'OFAC SDN List'),
        ('UN', 'UN Sanctions List'),
        ('EU', 'EU Sanctions List'),
        ('UK', 'UK Sanctions List'),
        ('PEP', 'Politically Exposed Persons'),
        ('ADVERSE_MEDIA', 'Adverse Media'),
    ]
    STATUSES = [
        ('PENDING', 'Pending'),
        ('CLEAR', 'Clear - No Matches'),
        ('POTENTIAL_MATCH', 'Potential Match - Manual Review Required'),
        ('CONFIRMED_MATCH', 'Confirmed Match - Action Required'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='sanctions_screenings')
    screening_type = models.CharField(max_length=20, choices=SCREENING_TYPES)
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING')
    matched_name = models.CharField(max_length=255, blank=True)
    matched_fields = models.JSONField(default=dict, blank=True)
    confidence_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    screened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-screened_at', '-id']
        indexes = [models.Index(fields=['vendor', 'screening_type', 'status'])]


class VendorKYCExpiryLog(models.Model):
    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='kyc_expiry_logs')
    document = models.ForeignKey(
        VendorKYCDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expiry_logs',
    )
    action = models.CharField(max_length=30)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['vendor', 'action'])]


class VendorNotificationOutbox(models.Model):
    ACTIONS = [
        ('ALERT_SENT_30', 'KYC document expires in 30 days'),
        ('ALERT_SENT_7', 'KYC document expires in 7 days'),
        ('ALERT_SENT_1', 'KYC document expires in 1 day'),
        ('STATUS_CHANGED', 'Vendor verification status changed'),
        ('ORDER_CANCELLED', 'Food order cancelled by customer'),
    ]
    PRIORITIES = [
        ('NORMAL', 'Normal'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    ]
    STATUSES = [
        ('PENDING', 'Pending publication'),
        ('PUBLISHED', 'Published to the notification channel'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='notification_outbox')
    document = models.ForeignKey(
        VendorKYCDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notification_outbox',
    )
    recipient_user_id = models.PositiveBigIntegerField(db_index=True)
    action = models.CharField(max_length=20, choices=ACTIONS)
    expiry_date = models.DateField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True)
    title = models.CharField(max_length=200)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITIES, default='NORMAL')
    status = models.CharField(max_length=12, choices=STATUSES, default='PENDING', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['vendor', 'created_at']),
        ]


class VendorNotificationPreference(models.Model):
    CHANNELS = [
        ('SMS', 'SMS'),
        ('PUSH', 'Push'),
        ('EMAIL', 'Email'),
        ('WHATSAPP', 'WhatsApp'),
    ]
    CATEGORIES = [
        ('MARKETING', 'Marketing'),
        ('ALL', 'All'),
    ]

    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    channel = models.CharField(max_length=10, choices=CHANNELS)
    category = models.CharField(max_length=15, choices=CATEGORIES, default='MARKETING')
    reason = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['owner_user_id', 'channel', 'category'],
                name='vendor_notification_pref_unique',
            ),
        ]
        indexes = [models.Index(fields=['owner_user_id', 'active'])]


class VendorKYCAuditLog(models.Model):
    vendor = models.ForeignKey(VendorProfile, on_delete=models.PROTECT, related_name='kyc_audit_entries')
    document = models.ForeignKey(
        VendorKYCDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_audit_entries',
    )
    document_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    verification = models.ForeignKey(
        VendorKYCVerification,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='audit_entries',
    )
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField()
    action = models.CharField(max_length=40)
    previous_status = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, blank=True)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']


class VendorCatalogSyncOutbox(models.Model):
    vendor_id = models.PositiveBigIntegerField(unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['created_at', 'vendor_id']
        indexes = [models.Index(fields=['delivered_at', 'updated_at'])]

def request_catalog_sync(vendor_id):
    VendorCatalogSyncOutbox.objects.update_or_create(
        vendor_id=vendor_id,
        defaults={'delivered_at': None, 'attempts': 0, 'last_error': ''},
    )


class CatalogCategory(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120)
    is_active = models.BooleanField(default=True)
    icon = models.CharField(max_length=50, blank=True)
    sort_order = models.IntegerField(default=0)
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        related_name='children',
        null=True,
        blank=True,
    )
    vendor = models.ForeignKey(
        VendorProfile,
        on_delete=models.CASCADE,
        related_name='catalog_categories',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['sort_order', 'name', 'id']
        verbose_name_plural = 'catalog categories'

    def __str__(self):
        return self.name


class Product(models.Model):
    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='products')
    category = models.ForeignKey(
        CatalogCategory,
        on_delete=models.PROTECT,
        related_name='products',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    stock = models.PositiveIntegerField(default=0)
    is_available = models.BooleanField(default=True, db_index=True)
    barcode = models.CharField(max_length=100, blank=True)
    product_type = models.CharField(max_length=20, default='FOOD')
    quantity_on_hand = models.IntegerField(default=0)
    low_stock_threshold = models.IntegerField(default=5)
    is_inventory_tracked = models.BooleanField(default=False)
    dietary_tags = models.JSONField(default=list, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    is_discounted = models.BooleanField(default=False)
    is_popular = models.BooleanField(default=False)
    compare_at_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    dimensions = models.CharField(max_length=100, blank=True)
    brand = models.CharField(max_length=100, blank=True)
    legacy_menu_item_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['vendor', 'sku'],
                condition=~models.Q(sku=''),
                name='vendor_catalog_unique_nonempty_sku',
            ),
        ]
        indexes = [
            models.Index(fields=['vendor', 'is_available', 'name']),
            models.Index(fields=['category', 'is_available']),
        ]
    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self._state.adding and 'updated_at' not in kwargs.get('update_fields', ()):
            self.updated_at = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = tuple(kwargs['update_fields']) + ('updated_at',)
        super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='products/%Y/%m/')
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']


class RestaurantMenuItem(models.Model):
    """Vendor-owned restaurant menu item; vendor/category IDs are external scalar references."""

    id = models.BigAutoField(primary_key=True)
    vendor_id = models.PositiveBigIntegerField(db_index=True)
    category_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    image = models.CharField(max_length=500, blank=True, null=True)
    is_available = models.BooleanField(default=True, db_index=True)
    is_popular = models.BooleanField(default=False)
    preparation_time_mins = models.IntegerField(default=10)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name', 'id']
        indexes = [models.Index(fields=['vendor_id', 'is_available'])]


class RestaurantMenuCategory(models.Model):
    id = models.BigAutoField(primary_key=True)
    vendor_id = models.PositiveBigIntegerField(db_index=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['order', 'name', 'id']


class RestaurantMenuItemOption(models.Model):
    id = models.BigAutoField(primary_key=True)
    menu_item = models.ForeignKey(
        RestaurantMenuItem,
        on_delete=models.CASCADE,
        related_name='options',
    )
    name = models.CharField(max_length=100)
    price_adjustment = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        ordering = ['name', 'id']


class RestaurantMenuItemImage(models.Model):
    id = models.BigAutoField(primary_key=True)
    menu_item = models.ForeignKey(
        RestaurantMenuItem,
        on_delete=models.CASCADE,
        related_name='images',
    )
    image = models.ImageField(upload_to='menu_items/%Y/%m/')
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']


class VendorPriceChange(models.Model):
    CONTENT_TYPES = (
        ('MENU_ITEM', 'Menu Item'),
        ('PRODUCT', 'Product'),
        ('PRODUCT_VARIANT', 'Product Variant'),
    )

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='price_changes')
    source_change_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    content_type = models.CharField(max_length=20, choices=CONTENT_TYPES)
    object_id = models.PositiveIntegerField()
    field_name = models.CharField(max_length=50, default='price')
    old_price = models.DecimalField(max_digits=10, decimal_places=2)
    new_price = models.DecimalField(max_digits=10, decimal_places=2)
    change_pct = models.DecimalField(max_digits=7, decimal_places=2)
    changed_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    requires_approval = models.BooleanField(default=False)
    is_approved = models.BooleanField(null=True, default=None)
    approved_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    approved_by_email = models.EmailField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['requires_approval', 'is_approved'], name='vendor_pricechg_approval'),
        ]

    @property
    def is_price_increase(self):
        return self.new_price > self.old_price


class VendorPromotion(models.Model):
    STATUSES = (
        ('PENDING_APPROVAL', 'Pending approval'),
        ('ACTIVE', 'Active'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
        ('DRAFT', 'Draft'),
    )

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='promotions')
    source_promotion_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    code = models.CharField(max_length=50)
    description = models.CharField(max_length=500, blank=True)
    discount_type = models.CharField(max_length=20)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    times_used = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING_APPROVAL')
    is_active = models.BooleanField(default=False)
    rejection_reason = models.CharField(max_length=500, blank=True)
    approved_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    approved_by_email = models.EmailField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-id']


class VendorScorecard(models.Model):
    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='scorecard')
    on_time_prep_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cancellation_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    avg_rating_30d = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    avg_rating_90d = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_orders_30d = models.IntegerField(default=0)
    total_orders_90d = models.IntegerField(default=0)
    avg_prep_time_mins = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    repeat_customer_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    revenue_30d = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    commission_owed_30d = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    acceptance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    fulfillment_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    refund_dispute_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    avg_delivery_delay_mins = models.DecimalField(max_digits=6, decimal_places=1, default=0)
    compliance_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    computed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-computed_at', '-id']