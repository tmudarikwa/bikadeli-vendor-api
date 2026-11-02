from decimal import Decimal

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from core.encrypted_fields import EncryptedCharField
from core.storage import image_storage, kyc_storage

User = settings.AUTH_USER_MODEL


class VendorProfile(models.Model):
    """Extended profile for vendors/restaurants"""

    CATEGORY_CHOICES = [
        ('RESTAURANT', 'Restaurant'),
        ('FAST_FOOD', 'Fast Food'),
        ('GROCERY', 'Grocery Store'),
        ('PHARMACY', 'Pharmacy'),
        ('LIQUOR', 'Liquor Store'),
        ('FLOWERS', 'Flowers & Gifts'),
        ('BAKERY', 'Bakery'),
        ('OTHER', 'Other'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='vendor_profile')

    business_name = models.CharField(max_length=200)
    business_logo = models.ImageField(upload_to='vendors/logos/', blank=True, storage=image_storage)
    business_cover = models.ImageField(upload_to='vendors/covers/', blank=True, storage=image_storage)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description = models.TextField(blank=True)

    business_phone = EncryptedCharField(max_length=15, blank=True)
    business_email = models.EmailField(blank=True)
    business_address = models.TextField()

    business_location = gis_models.PointField(srid=4326, null=True, blank=True)
    what3words = models.CharField(max_length=100, blank=True)

    is_open = models.BooleanField(default=False)
    opening_time = models.TimeField(default='08:00')
    closing_time = models.TimeField(default='20:00')

    business_license = models.ImageField(upload_to='vendors/licenses/', blank=True, storage=kyc_storage)
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    preparation_time_mins = models.IntegerField(default=15)
    delivery_radius_km = models.IntegerField(default=5)
    minimum_order = models.DecimalField(max_digits=8, decimal_places=2, default=5.00)
    delivery_fee = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    packing_fee = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text='Packing fee per order')
    pickup_available = models.BooleanField(default=True)

    total_orders = models.IntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0, validators=[MaxValueValidator(5)])

    # Payout info (encrypted — Fernet ciphertext is ~168 chars, so column must be large)
    eco_cash_number = EncryptedCharField(max_length=200, blank=True)
    bank_name = EncryptedCharField(max_length=200, blank=True)
    bank_account = EncryptedCharField(max_length=200, blank=True)
    commission_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=15.00, help_text='Platform commission % on each order'
    )

    LIFECYCLE_DRAFT = 'DRAFT'
    LIFECYCLE_KYC_PENDING = 'KYC_PENDING'
    LIFECYCLE_UNDER_REVIEW = 'UNDER_REVIEW'
    LIFECYCLE_APPROVED = 'APPROVED'
    LIFECYCLE_ACTIVE = 'ACTIVE'
    LIFECYCLE_ON_HOLD = 'ON_HOLD'
    LIFECYCLE_SUSPENDED = 'SUSPENDED'
    LIFECYCLE_OFFBOARDED = 'OFFBOARDED'
    LIFECYCLE_CHOICES = [
        (LIFECYCLE_DRAFT, 'Draft'),
        (LIFECYCLE_KYC_PENDING, 'KYC Pending'),
        (LIFECYCLE_UNDER_REVIEW, 'Under Review'),
        (LIFECYCLE_APPROVED, 'Approved'),
        (LIFECYCLE_ACTIVE, 'Active'),
        (LIFECYCLE_ON_HOLD, 'On Hold'),
        (LIFECYCLE_SUSPENDED, 'Suspended'),
        (LIFECYCLE_OFFBOARDED, 'Offboarded'),
    ]
    lifecycle_state = models.CharField(
        max_length=20,
        choices=LIFECYCLE_CHOICES,
        default=LIFECYCLE_DRAFT,
        db_index=True,
    )
    lifecycle_reason = models.TextField(blank=True, help_text='Reason for current lifecycle state')
    lifecycle_changed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Vendor Profile'
        verbose_name_plural = 'Vendor Profiles'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active', 'is_verified'], name='idx_vendor_active_verified'),
            models.Index(fields=['category', 'is_active', 'is_verified']),
            models.Index(fields=['category', 'is_active', 'is_verified', 'is_open']),
            gis_models.Index(fields=['business_location'], name='idx_vendor_business_location'),
            GinIndex(fields=['business_name'], opclasses=['gin_trgm_ops'], name='idx_vendor_name_trgm'),
        ]

    def __str__(self):
        return self.business_name

    def is_currently_open(self):
        if not self.is_open:
            return False
        if not self.opening_time or not self.closing_time:
            return self.is_open
        now = timezone.localtime().time()
        if self.opening_time <= self.closing_time:
            return self.opening_time <= now <= self.closing_time
        # Overnight hours (e.g. 18:00–02:00): open if after opening or before closing.
        return now >= self.opening_time or now <= self.closing_time

    def kyc_status(self):
        """
        Returns the overall KYC status for this vendor.
        VERIFIED = all required docs verified and not expired
        PENDING = docs submitted but not all verified
        REJECTED = any required doc rejected
        EXPIRED = any required doc expired
        INCOMPLETE = missing required docs
        """
        required_docs = self.kyc_documents.filter(is_required=True)
        if not required_docs.exists():
            return 'INCOMPLETE'

        if required_docs.filter(verification_status='REJECTED').exists():
            return 'REJECTED'

        if required_docs.filter(verification_status='EXPIRED').exists():
            return 'EXPIRED'

        if required_docs.exclude(verification_status='VERIFIED').exists():
            return 'PENDING'

        return 'VERIFIED'

    def is_kyc_verified(self):
        return self.kyc_status() == 'VERIFIED'

    def kyc_expiry_alert_needed(self):
        """Check if vendor needs 30-day expiry alert."""
        today = timezone.localdate()
        thirty_days = today + timezone.timedelta(days=30)
        return self.kyc_documents.filter(
            is_required=True,
            expiry_date__lte=thirty_days,
            expiry_date__gte=today,
            verification_status='VERIFIED',
        ).exists()

    def should_be_suspended(self):
        """Check if vendor should be suspended due to KYC expiry."""
        return self.kyc_documents.filter(
            is_required=True,
            expiry_date__lt=timezone.localdate(),
            verification_status__in=['VERIFIED', 'PENDING', 'UNDER_REVIEW', 'EXPIRED'],
        ).exists()

    def compliance_holds(self):
        """Return list of compliance holds blocking ordering/payouts."""
        holds = []
        if not self.is_kyc_verified():
            holds.append('KYC_INCOMPLETE')
        if self.should_be_suspended():
            holds.append('KYC_EXPIRED')
        tax = getattr(self, 'tax_info', None)
        if tax and tax.tax_clearance_expiry and tax.tax_clearance_expiry < timezone.localdate():
            holds.append('TAX_CLEARANCE_EXPIRED')
        latest_sanctions = self.sanctions_screenings.order_by('-screened_at').first()
        if latest_sanctions and latest_sanctions.status == 'CONFIRMED_MATCH':
            holds.append('SANCTIONS_MATCH')
        return holds

    def can_accept_orders(self):
        return (
            self.is_active
            and self.is_verified
            and self.lifecycle_state in {self.LIFECYCLE_APPROVED, self.LIFECYCLE_ACTIVE}
            and not self.compliance_holds()
        )

    def transition_lifecycle(self, new_state, reason='', performed_by=None):
        """One-way lifecycle state transition with audit log."""
        valid = {
            self.LIFECYCLE_DRAFT: {self.LIFECYCLE_KYC_PENDING, self.LIFECYCLE_UNDER_REVIEW},
            self.LIFECYCLE_KYC_PENDING: {self.LIFECYCLE_UNDER_REVIEW, self.LIFECYCLE_SUSPENDED},
            self.LIFECYCLE_UNDER_REVIEW: {self.LIFECYCLE_APPROVED, self.LIFECYCLE_SUSPENDED, self.LIFECYCLE_ON_HOLD},
            self.LIFECYCLE_APPROVED: {self.LIFECYCLE_ACTIVE, self.LIFECYCLE_SUSPENDED},
            self.LIFECYCLE_ACTIVE: {self.LIFECYCLE_ON_HOLD, self.LIFECYCLE_SUSPENDED, self.LIFECYCLE_OFFBOARDED},
            self.LIFECYCLE_ON_HOLD: {self.LIFECYCLE_ACTIVE, self.LIFECYCLE_SUSPENDED, self.LIFECYCLE_OFFBOARDED},
            self.LIFECYCLE_SUSPENDED: {self.LIFECYCLE_ACTIVE, self.LIFECYCLE_OFFBOARDED},
            self.LIFECYCLE_OFFBOARDED: set(),
        }
        if new_state not in valid.get(self.lifecycle_state, set()):
            raise ValueError(
                f'Cannot transition vendor {self.id} from {self.lifecycle_state} to {new_state}. '
                f'Valid: {valid.get(self.lifecycle_state, set())}'
            )
        old_state = self.lifecycle_state
        self.lifecycle_state = new_state
        self.lifecycle_reason = reason
        self.lifecycle_changed_at = timezone.now()
        self.save(update_fields=['lifecycle_state', 'lifecycle_reason', 'lifecycle_changed_at', 'updated_at'])

        VendorLifecycleLog.objects.create(
            vendor=self,
            action=new_state,
            from_state=old_state,
            to_state=new_state,
            reason=reason,
            performed_by=performed_by,
        )
        return old_state


class VendorKYCDocument(models.Model):
    """KYC documents for vendor compliance"""

    DOCUMENT_TYPES = [
        ('NATIONAL_ID', 'National Identity Document'),
        ('BUSINESS_LICENSE', 'Business License / Certificate of Incorporation'),
        ('TAX_CLEARANCE', 'Tax Clearance Certificate (ZIMRA)'),
        ('BANK_STATEMENT', 'Bank Statement / Account Verification'),
        ('PROOF_OF_ADDRESS', 'Proof of Business Address'),
        ('FOOD_LICENSE', 'Food Service License / Health Permit'),
        ('LIQUOR_LICENSE', 'Liquor License'),
        ('PHARMACY_LICENSE', 'Pharmacy License'),
        ('OTHER', 'Other'),
    ]

    VERIFICATION_STATUS_CHOICES = [
        ('PENDING', 'Pending Review'),
        ('UNDER_REVIEW', 'Under Review'),
        ('VERIFIED', 'Verified'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='kyc_documents')

    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)
    document_subtype = models.CharField(
        max_length=100, blank=True, help_text="e.g. 'Driver License', 'Passport', 'Certificate of Incorporation'"
    )

    file = models.FileField(
        upload_to='vendors/kyc/%Y/%m/',
        storage=kyc_storage,
        help_text='Encrypted at rest via SSE-KMS (or SSE-S3). Max 10MB.',
    )
    file_size = models.PositiveIntegerField(help_text='Size in bytes')
    original_filename = models.CharField(max_length=255)

    expiry_date = models.DateField(
        null=True, blank=True, help_text='Expiry date of the document (e.g. ID expiry, license renewal)'
    )

    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUS_CHOICES, default='PENDING')

    is_required = models.BooleanField(
        default=True, help_text='Whether this document type is required for this vendor category'
    )

    rejection_reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_documents_reviewed',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # Per-document encryption key reference (for additional client-side encryption)
    encryption_key_id = models.CharField(max_length=100, blank=True, help_text='KMS key alias used for this document')

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_documents_uploaded',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Vendor KYC Document'
        verbose_name_plural = 'Vendor KYC Documents'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'document_type', 'verification_status']),
            models.Index(fields=['verification_status', 'expiry_date']),
            models.Index(fields=['vendor', 'is_required']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['vendor', 'document_type'], name='uq_kyc_doc_vendor_type'),
        ]

    def __str__(self):
        return (
            f'{self.vendor.business_name} - '
            f'{self.get_document_type_display()} '
            f'({self.get_verification_status_display()})'
        )

    def is_expired(self):
        if self.expiry_date:
            return self.expiry_date < timezone.localdate()
        return False

    def days_until_expiry(self):
        if self.expiry_date:
            delta = self.expiry_date - timezone.localdate()
            return delta.days
        return None

    def mark_verified(self, reviewer):
        self.verification_status = 'VERIFIED'
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.rejection_reason = ''
        self.save(update_fields=['verification_status', 'reviewed_by', 'reviewed_at', 'rejection_reason', 'updated_at'])

    def mark_rejected(self, reviewer, reason):
        self.verification_status = 'REJECTED'
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.rejection_reason = reason
        self.save(update_fields=['verification_status', 'reviewed_by', 'reviewed_at', 'rejection_reason', 'updated_at'])

    def mark_expired(self):
        if self.verification_status in ['VERIFIED', 'PENDING', 'UNDER_REVIEW']:
            self.verification_status = 'EXPIRED'
            self.save(update_fields=['verification_status', 'updated_at'])


class VendorKYCVerification(models.Model):
    """Aggregated KYC verification workflow for a vendor"""

    OVERALL_STATUS_CHOICES = [
        ('INCOMPLETE', 'Incomplete - Missing Documents'),
        ('PENDING', 'Pending - Submitted, Awaiting Review'),
        ('UNDER_REVIEW', 'Under Review - Admin Checking'),
        ('VERIFIED', 'Fully Verified'),
        ('REJECTED', 'Rejected - One or More Documents Failed'),
        ('EXPIRED', 'Expired - Documents Need Renewal'),
        ('SUSPENDED', 'Suspended - KYC Lapsed'),
    ]

    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='kyc_verification')

    overall_status = models.CharField(max_length=20, choices=OVERALL_STATUS_CHOICES, default='INCOMPLETE')

    # Required document types for this vendor's category
    required_document_types = models.JSONField(
        default=list, help_text='List of document_type codes required for this vendor'
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_verifications_completed',
    )

    rejection_summary = models.TextField(blank=True)

    # Sanctions/PEP screening
    sanctions_check_passed = models.BooleanField(default=False)
    sanctions_check_date = models.DateTimeField(null=True, blank=True)
    sanctions_check_details = models.JSONField(default=dict, blank=True)

    pep_check_passed = models.BooleanField(default=False)
    pep_check_date = models.DateTimeField(null=True, blank=True)
    pep_check_details = models.JSONField(default=dict, blank=True)

    # Next review date (for periodic re-verification)
    next_review_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Vendor KYC Verification'
        verbose_name_plural = 'Vendor KYC Verifications'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['overall_status']),
            models.Index(fields=['next_review_date']),
        ]

    def __str__(self):
        return f'{self.vendor.business_name} - {self.get_overall_status_display()}'

    def refresh_status(self):
        """Recalculate overall status from individual documents."""
        docs = self.vendor.kyc_documents.all()
        required_docs = docs.filter(is_required=True)

        if not required_docs.exists():
            self.overall_status = 'INCOMPLETE'
        elif required_docs.filter(verification_status='REJECTED').exists():
            self.overall_status = 'REJECTED'
        elif required_docs.filter(verification_status='EXPIRED').exists():
            self.overall_status = 'EXPIRED'
        elif required_docs.exclude(verification_status='VERIFIED').exists():
            self.overall_status = 'PENDING'
        else:
            self.overall_status = 'VERIFIED'

        self.save(update_fields=['overall_status', 'updated_at'])
        return self.overall_status

    def all_required_verified(self):
        required = self.vendor.kyc_documents.filter(is_required=True)
        return required.exists() and not required.exclude(verification_status='VERIFIED').exists()

    def get_missing_required_types(self):
        """Return list of required document types not yet uploaded."""
        uploaded_types = set(self.vendor.kyc_documents.values_list('document_type', flat=True))
        required = set(self.required_document_types)
        return list(required - uploaded_types)


class VendorTaxInfo(models.Model):
    """Zimbabwe tax compliance information for vendors"""

    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='tax_info')

    zimra_pin = EncryptedCharField(
        max_length=50,
        blank=True,
        validators=[RegexValidator(regex=r'^\d{10}$', message='ZIMRA PIN must be exactly 10 digits')],
        help_text='ZIMRA Taxpayer Identification Number (10 digits)',
    )

    zimra_pin_verified = models.BooleanField(default=False)
    zimra_verification_date = models.DateTimeField(null=True, blank=True)
    zimra_verification_method = models.CharField(max_length=50, blank=True)

    vat_registered = models.BooleanField(default=False)
    vat_number = EncryptedCharField(max_length=50, blank=True)

    tax_clearance_certificate = models.FileField(upload_to='vendors/tax/%Y/%m/', storage=kyc_storage, blank=True)
    tax_clearance_expiry = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Vendor Tax Information'
        verbose_name_plural = 'Vendor Tax Information'
        ordering = ['-created_at']

    def __str__(self):
        return (
            f'{self.vendor.business_name} - ZIMRA PIN: {"*" * 6 + self.zimra_pin[-4:] if self.zimra_pin else "Not Set"}'
        )


class VendorSanctionsScreening(models.Model):
    """Sanctions and PEP screening records for vendors"""

    SCREENING_STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('CLEAR', 'Clear - No Matches'),
        ('POTENTIAL_MATCH', 'Potential Match - Manual Review Required'),
        ('CONFIRMED_MATCH', 'Confirmed Match - Action Required'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='sanctions_screenings')

    screening_type = models.CharField(
        max_length=20,
        choices=[
            ('OFAC', 'OFAC SDN List'),
            ('UN', 'UN Sanctions List'),
            ('EU', 'EU Sanctions List'),
            ('UK', 'UK Sanctions List'),
            ('PEP', 'Politically Exposed Persons'),
            ('ADVERSE_MEDIA', 'Adverse Media'),
        ],
    )

    status = models.CharField(max_length=20, choices=SCREENING_STATUS_CHOICES, default='PENDING')

    matched_name = models.CharField(max_length=255, blank=True)
    matched_fields = models.JSONField(default=dict, blank=True)
    confidence_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    screened_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='sanctions_reviews'
    )
    review_notes = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Vendor Sanctions Screening'
        verbose_name_plural = 'Vendor Sanctions Screenings'
        ordering = ['-screened_at']
        indexes = [
            models.Index(fields=['vendor', 'screening_type', 'status']),
        ]

    def __str__(self):
        return f'{self.vendor.business_name} - {self.screening_type}: {self.get_status_display()}'

    def mark_clear(self, reviewer):
        self.status = 'CLEAR'
        self.reviewed_at = timezone.now()
        self.reviewed_by = reviewer
        self.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'updated_at'])

    def mark_potential_match(self, matched_name, matched_fields, confidence, reviewer=None):
        self.status = 'POTENTIAL_MATCH'
        self.matched_name = matched_name
        self.matched_fields = matched_fields
        self.confidence_score = confidence
        if reviewer:
            self.reviewed_at = timezone.now()
            self.reviewed_by = reviewer
        self.save(
            update_fields=[
                'status',
                'matched_name',
                'matched_fields',
                'confidence_score',
                'reviewed_at',
                'reviewed_by',
                'updated_at',
            ]
        )


class VendorKYCExpiryLog(models.Model):
    """Audit log for KYC expiry alerts and actions"""

    ACTION_CHOICES = [
        ('ALERT_SENT_30', '30-Day Expiry Alert Sent'),
        ('ALERT_SENT_7', '7-Day Expiry Alert Sent'),
        ('ALERT_SENT_1', '1-Day Expiry Alert Sent'),
        ('DOCUMENT_EXPIRED', 'Document Expired'),
        ('VENDOR_SUSPENDED', 'Vendor Suspended'),
        ('VENDOR_REACTIVATED', 'Vendor Reactivated After KYC Renewal'),
        ('RENEWAL_UPLOADED', 'Renewal Document Uploaded'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='kyc_expiry_logs')
    kyc_document = models.ForeignKey(
        VendorKYCDocument, on_delete=models.SET_NULL, null=True, blank=True, related_name='expiry_logs'
    )

    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Vendor KYC Expiry Log'
        verbose_name_plural = 'Vendor KYC Expiry Logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'action']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f'{self.vendor.business_name} - {self.get_action_display()} at {self.created_at}'


class MenuCategory(models.Model):
    """Categories for vendor menus"""

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Menu Category'
        verbose_name_plural = 'Menu Categories'
        ordering = ['order', 'name']

    def __str__(self):
        return f'{self.vendor.business_name} - {self.name}'


class MenuItem(models.Model):
    """Menu items for vendors"""

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='menu_items')
    category = models.ForeignKey(MenuCategory, on_delete=models.SET_NULL, null=True, related_name='items')

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    image = models.ImageField(
        upload_to='menu_items/', blank=True, null=True, storage=image_storage, help_text='JPEG, max 1024x1024px, ≤500KB'
    )

    is_available = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    preparation_time_mins = models.IntegerField(default=10)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Menu Item'
        verbose_name_plural = 'Menu Items'
        ordering = ['category__order', 'name']
        indexes = [
            models.Index(fields=['vendor', 'is_available']),
            models.Index(fields=['category', 'is_available']),
            GinIndex(fields=['name'], opclasses=['gin_trgm_ops'], name='idx_menu_name_trgm'),
            GinIndex(fields=['description'], opclasses=['gin_trgm_ops'], name='idx_menu_desc_trgm'),
        ]

    def __str__(self):
        return f'{self.name} - ${self.price}'


class MenuItemOption(models.Model):
    """Options/Modifiers for menu items"""

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='options')
    name = models.CharField(max_length=100)
    price_adjustment = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        verbose_name = 'Menu Item Option'
        verbose_name_plural = 'Menu Item Options'
        ordering = ['name']

    def __str__(self):
        return f'{self.menu_item.name} - {self.name}'


# NOTE: Category vs MenuCategory separation
# -----------------------------------------------------------------------
# MenuCategory: Vendor-specific food menu organization (restaurant menus)
#   - Belongs to a single VendorProfile
#   - Has order field for display sorting
#   - Contains MenuItems (food/drink items)
#   - Used by food ordering flow (customer app restaurant view)
#
# Category: Global marketplace product taxonomy (multi-vendor)
#   - Self-referential (parent/children for hierarchy)
#   - Optional vendor FK (null = global category, set = vendor-specific override)
#   - Contains Products (marketplace items: electronics, fashion, grocery, etc.)
#   - Used by marketplace browsing (customer app marketplace view)
#   - Has icon field for UI
# -----------------------------------------------------------------------


class Category(models.Model):
    name = models.CharField(max_length=100)
    icon = models.CharField(max_length=50, blank=True, help_text='Icon class (FontAwesome)')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='children')
    vendor = models.ForeignKey(
        VendorProfile, on_delete=models.CASCADE, related_name='product_categories', null=True, blank=True
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['name', 'vendor', 'parent'], name='uq_category_name_vendor_parent'),
        ]

    def __str__(self):
        return self.name


class Product(models.Model):
    PRODUCT_TYPES = [
        ('FOOD', 'Food'),
        ('ELECTRONICS', 'Electronics'),
        ('FASHION', 'Fashion'),
        ('GROCERY', 'Grocery'),
        ('HOME', 'Home & Garden'),
        ('PARCEL', 'Parcel'),
        ('OTHER', 'Other'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    stock = models.IntegerField(default=0, editable=False, help_text='Deprecated: use quantity_on_hand')
    reorder_point = models.IntegerField(default=10, editable=False, help_text='Deprecated: use low_stock_threshold')
    unit = models.CharField(max_length=50, blank=True)
    is_available = models.BooleanField(default=True)
    dietary_tags = models.JSONField(default=list, blank=True)
    barcode = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    rating = models.DecimalField(
        max_digits=3, decimal_places=2, null=True, blank=True, default=None, help_text='Average rating from 0 to 5'
    )
    is_discounted = models.BooleanField(default=False, help_text='Whether the product is currently on sale')
    is_popular = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Auto-set nightly by vendors.tasks.refresh_category_bestsellers (top seller in its category)',
    )
    compare_at_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Original price shown struck-through when on sale. Must exceed price.',
    )

    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPES, default='FOOD')
    quantity_on_hand = models.IntegerField(default=0, help_text='Current physical inventory count')
    low_stock_threshold = models.IntegerField(default=5, help_text='Alert when stock falls below this')
    is_inventory_tracked = models.BooleanField(default=False, help_text='Enable real-time inventory tracking')
    weight_kg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    dimensions = models.CharField(max_length=100, blank=True, help_text='e.g. 30x20x10 cm')
    brand = models.CharField(max_length=100, blank=True)
    menu_item = models.OneToOneField(
        MenuItem,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='product',
        help_text='Linked menu item (auto-synced catalog row, if any)',
    )

    class Meta:
        indexes = [
            models.Index(fields=['vendor', 'is_available']),
            models.Index(fields=['product_type']),
            models.Index(fields=['vendor', 'is_available', 'product_type']),
            models.Index(fields=['is_available', '-created_at'], name='idx_prod_available_created'),
            GinIndex(fields=['name'], opclasses=['gin_trgm_ops'], name='idx_product_name_trgm'),
            GinIndex(fields=['description'], opclasses=['gin_trgm_ops'], name='idx_product_desc_trgm'),
            GinIndex(fields=['brand'], opclasses=['gin_trgm_ops'], name='idx_product_brand_trgm'),
            GinIndex(fields=['barcode'], opclasses=['gin_trgm_ops'], name='idx_product_barcode_trgm'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name}'

    def save(self, *args, **kwargs):
        if self.is_inventory_tracked:
            self.stock = self.quantity_on_hand
            self.reorder_point = self.low_stock_threshold
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.is_inventory_tracked and self.quantity_on_hand <= self.low_stock_threshold

    @property
    def stock_status(self):
        if not self.is_inventory_tracked:
            return 'NOT_TRACKED'
        if self.quantity_on_hand <= 0:
            return 'OUT_OF_STOCK'
        if self.quantity_on_hand <= self.low_stock_threshold:
            return 'LOW_STOCK'
        return 'IN_STOCK'


class CategoryBestseller(models.Model):
    """Snapshot of the top sellers inside one marketplace Category row.

    Rewritten nightly by vendors.tasks.refresh_category_bestsellers, which also
    flips Product.is_popular. Kept for rank history and auditability.
    """

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='bestsellers')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='bestseller_entries')
    rank = models.PositiveIntegerField(help_text='1 = best seller in this category')
    units_30d = models.PositiveIntegerField(default=0)
    orders_30d = models.PositiveIntegerField(default=0, help_text='Distinct delivered orders in the window')
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Category bestsellers'
        ordering = ['rank']
        constraints = [
            models.UniqueConstraint(fields=['category', 'product'], name='uq_bestseller_category_product'),
            models.UniqueConstraint(fields=['category', 'rank'], name='uq_bestseller_category_rank'),
        ]
        indexes = [
            models.Index(fields=['category', 'rank'], name='idx_bestseller_cat_rank'),
        ]

    def __str__(self):
        return f'{self.category.name}: #{self.rank} {self.product.name}'


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=200, help_text="e.g. 'Large', 'Red', '4GB RAM'")
    price_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    quantity_on_hand = models.IntegerField(default=0)
    sku = models.CharField(max_length=100, blank=True, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.product.name} - {self.name}'


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='products/%Y/%m/', storage=image_storage)
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return f'Image #{self.sort_order} for {self.product.name}'


class Promotion(models.Model):
    DISCOUNT_TYPES = [
        ('PERCENTAGE', 'Percentage'),
        ('FIXED', 'Fixed Amount'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING_APPROVAL', 'Pending Approval'),
        ('ACTIVE', 'Active'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='promotions')
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True, help_text='Promotion description for admin review')
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPES, default='PERCENTAGE')
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    usage_limit = models.IntegerField(default=100)
    times_used = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    is_active = models.BooleanField(default=True)
    rejection_reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_promotions'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'is_active']),
        ]

    def __str__(self):
        return f'{self.code} ({self.vendor.business_name})'


class InventoryAdjustmentLog(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='adjustment_logs')
    quantity = models.IntegerField(help_text='Positive = added, Negative = removed')
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.product.name}: {self.quantity:+d}'


class MenuItemImage(models.Model):
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='menu_items/%Y/%m/', storage=image_storage)
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return f'Image #{self.sort_order} for {self.menu_item.name}'


# ─────────────────────────────────────────────────────────────────────────
# Price Change Audit
# ─────────────────────────────────────────────────────────────────────────


class VendorPriceChange(models.Model):
    CONTENT_TYPE_CHOICES = [
        ('MENU_ITEM', 'Menu Item'),
        ('PRODUCT', 'Product'),
        ('PRODUCT_VARIANT', 'Product Variant'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='price_changes')
    content_type = models.CharField(max_length=20, choices=CONTENT_TYPE_CHOICES)
    object_id = models.PositiveIntegerField()
    field_name = models.CharField(max_length=50, default='price')

    old_price = models.DecimalField(max_digits=10, decimal_places=2)
    new_price = models.DecimalField(max_digits=10, decimal_places=2)
    change_pct = models.DecimalField(max_digits=7, decimal_places=2, help_text='Percentage change')

    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)

    requires_approval = models.BooleanField(default=False, help_text='True if change >50% in 24h window')
    is_approved = models.BooleanField(null=True, default=None, help_text='None=pending, True=approved, False=rejected')
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_price_changes',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'content_type', 'object_id']),
            models.Index(fields=['requires_approval', 'is_approved']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f'Price change {self.content_type}#{self.object_id}: ${self.old_price} → ${self.new_price}'

    @property
    def is_price_increase(self):
        return self.new_price > self.old_price


# ─────────────────────────────────────────────────────────────────────────
# Vendor Rating Decay
# ─────────────────────────────────────────────────────────────────────────


class VendorRatingSnapshot(models.Model):
    """Stores individual ratings for exponential moving average calculation."""

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='rating_snapshots')
    rating = models.DecimalField(max_digits=3, decimal_places=2)
    order = models.ForeignKey('orders.Order', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'created_at']),
        ]

    def __str__(self):
        return f'Rating {self.rating} for {self.vendor.business_name}'


# ─────────────────────────────────────────────────────────────────────────


class VendorScorecard(models.Model):
    """Cached vendor performance metrics, refreshed by Celery."""

    vendor = models.OneToOneField(VendorProfile, on_delete=models.CASCADE, related_name='scorecard')

    on_time_prep_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text='% of orders prepared on time'
    )
    cancellation_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text='% of orders cancelled by vendor'
    )
    avg_rating_30d = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    avg_rating_90d = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_orders_30d = models.IntegerField(default=0)
    total_orders_90d = models.IntegerField(default=0)
    avg_prep_time_mins = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    repeat_customer_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text='% of customers who ordered again within 30d'
    )
    revenue_30d = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    commission_owed_30d = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Additional performance metrics
    acceptance_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='% of orders accepted by vendor',
    )
    fulfillment_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='% of accepted orders fulfilled (not cancelled)',
    )
    refund_dispute_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='% of orders with refund or dispute',
    )
    avg_delivery_delay_mins = models.DecimalField(
        max_digits=6,
        decimal_places=1,
        default=0,
        help_text='Average delivery time vs estimated (positive = late)',
    )
    compliance_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text='Compliance score (0-100) based on KYC, sanctions, tax',
    )

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Vendor Scorecard'
        verbose_name_plural = 'Vendor Scorecards'
        ordering = ['-computed_at']

    def __str__(self):
        return f'Scorecard for {self.vendor.business_name}'


# ─────────────────────────────────────────────────────────────────────────
# Payout Reconciliation
# ─────────────────────────────────────────────────────────────────────────


class PayoutReconciliation(models.Model):
    """Tracks expected vs actual vendor payouts for reconciliation."""

    vendor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payout_reconciliations')
    vendor_profile = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='reconciliations')
    period_start = models.DateField()
    period_end = models.DateField()

    total_orders_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_commission = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    expected_payout = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, help_text='total_orders_amount - total_commission'
    )
    actual_payout = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, help_text='Amount from VendorPayout record'
    )
    variance = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text='expected - actual')
    order_count = models.IntegerField(default=0)

    is_resolved = models.BooleanField(default=False)
    resolution_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']
        constraints = [
            models.UniqueConstraint(
                fields=['vendor', 'period_start', 'period_end'], name='uq_reconciliation_vendor_period'
            ),
        ]
        indexes = [
            models.Index(fields=['vendor', 'period_start', 'period_end']),
            models.Index(fields=['is_resolved']),
        ]

    def __str__(self):
        return f'Reconciliation {self.vendor_profile.business_name} {self.period_start}-{self.period_end}'


class VendorLifecycleLog(models.Model):
    """Immutable audit trail of vendor lifecycle state transitions."""

    ACTION_CHOICES = [
        ('CREATED', 'Account Created'),
        ('KYC_SUBMITTED', 'KYC Submitted'),
        ('UNDER_REVIEW', 'Moved to Under Review'),
        ('APPROVED', 'Approved'),
        ('ACTIVATED', 'Activated'),
        ('ON_HOLD', 'Placed On Hold'),
        ('SUSPENDED', 'Suspended'),
        ('REACTIVATED', 'Reactivated'),
        ('OFFBOARDED', 'Offboarded'),
    ]

    vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE, related_name='lifecycle_logs')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    from_state = models.CharField(max_length=20, blank=True)
    to_state = models.CharField(max_length=20)
    reason = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_lifecycle_actions',
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Vendor Lifecycle Log'
        verbose_name_plural = 'Vendor Lifecycle Logs'
        indexes = [
            models.Index(fields=['vendor', '-created_at']),
            models.Index(fields=['action']),
        ]

    def __str__(self):
        return f'{self.vendor.business_name}: {self.from_state or "(initial)"} → {self.to_state} ({self.action})'


class VendorSupportTicket(models.Model):
    """Support tickets raised by vendors (mirrors driver lane)."""

    PRIORITY_CHOICES = [
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('URGENT', 'Urgent'),
    ]

    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In Progress'),
        ('WAITING_VENDOR', 'Waiting for Vendor'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    ]

    CATEGORY_CHOICES = [
        ('PAYMENT', 'Payment issue'),
        ('PAYOUT', 'Payout issue'),
        ('MENU', 'Menu / catalogue issue'),
        ('ORDER', 'Order issue'),
        ('ACCOUNT', 'Account issue'),
        ('TECHNICAL', 'Technical issue'),
        ('SAFETY', 'Safety concern'),
        ('FRAUD', 'Fraud concern'),
        ('GENERAL', 'General'),
    ]

    vendor = models.ForeignKey(
        VendorProfile,
        on_delete=models.CASCADE,
        related_name='support_tickets',
    )
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_support_tickets',
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default='GENERAL')
    subject = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='MEDIUM')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    escalated = models.BooleanField(default=False)
    escalation_reason = models.CharField(max_length=200, blank=True)
    escalation_team = models.CharField(max_length=20, blank=True, default='')
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_vendor_tickets',
    )
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Vendor Support Ticket'
        verbose_name_plural = 'Vendor Support Tickets'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'status'], name='vendors_ven_vendor__a1b2c3_idx'),
            models.Index(fields=['status', 'created_at'], name='vendors_ven_status__d4e5f6_idx'),
        ]

    def __str__(self):
        return f'Vendor Ticket #{self.id}: {self.subject}'


class VendorInternalNote(models.Model):
    """Private internal notes — separate from public vendor messages."""

    ticket = models.ForeignKey(
        VendorSupportTicket,
        on_delete=models.CASCADE,
        related_name='internal_notes_model',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='vendor_internal_notes'
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Vendor Internal Note'

    def __str__(self):
        return f'Internal note on vendor #{self.ticket_id}: {self.message[:50]}'


class VendorTicketMessage(models.Model):
    """Messages within a vendor support ticket thread"""

    ticket = models.ForeignKey(
        VendorSupportTicket,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    sender_is_vendor = models.BooleanField(default=True)
    message = models.TextField()
    channel = models.CharField(max_length=10, blank=True, default='')
    delivery_result = models.CharField(max_length=50, blank=True, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['ticket', 'created_at'], name='vendors_vticket__a1b2c3_idx'),
        ]

    def __str__(self):
        sender = 'Vendor' if self.sender_is_vendor else 'Support'
        return f'{sender}: {self.message[:50]}'


class VendorTicketAttachment(models.Model):
    ticket = models.ForeignKey(
        VendorSupportTicket,
        on_delete=models.CASCADE,
        related_name='attachments_model',
    )
    file_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=500)
    file_size = models.IntegerField()
    mime_type = models.CharField(max_length=100)
    uploaded_by_email = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Vendor Ticket Attachment'

    def __str__(self):
        return f'Attachment on vendor #{self.ticket_id}: {self.file_name}'
