from django.contrib import admin
from django.utils import timezone

from .models import (
    Category,
    InventoryAdjustmentLog,
    MenuItem,
    MenuItemImage,
    MenuItemOption,
    PayoutReconciliation,
    Product,
    ProductImage,
    ProductVariant,
    Promotion,
    VendorInternalNote,
    VendorKYCDocument,
    VendorKYCExpiryLog,
    VendorKYCVerification,
    VendorPriceChange,
    VendorProfile,
    VendorRatingSnapshot,
    VendorSanctionsScreening,
    VendorScorecard,
    VendorSupportTicket,
    VendorTaxInfo,
    VendorTicketAttachment,
    VendorTicketMessage,
)


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'vendor', 'category', 'price', 'is_available', 'is_popular']
    list_filter = ['is_available', 'is_popular', 'category']
    search_fields = ['name', 'vendor__business_name']


@admin.register(MenuItemOption)
class MenuItemOptionAdmin(admin.ModelAdmin):
    list_display = ['menu_item', 'name', 'price_adjustment']
    search_fields = ['menu_item__name', 'name']


@admin.register(MenuItemImage)
class MenuItemImageAdmin(admin.ModelAdmin):
    list_display = ['id', 'menu_item', 'sort_order']
    list_filter = ['menu_item']


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'vendor',
        'category',
        'price',
        'stock',
        'is_available',
        'is_popular',
        'bestseller_info',
    ]
    list_filter = ['is_available', 'is_discounted', 'is_popular', 'category']
    search_fields = ['name', 'vendor__business_name', 'barcode']
    readonly_fields = ['is_popular']

    @admin.display(description='Category bestseller (auto)')
    def bestseller_info(self, obj):
        if not obj.is_popular:
            return '—'
        entry = obj.bestseller_entries.first()
        if not entry:
            return 'Popular (snapshot pending)'
        when = f'{entry.computed_at:%Y-%m-%d}'
        return (
            f'#{entry.rank} in {entry.category.name} · '
            f'{entry.units_30d} units / {entry.orders_30d} orders · computed {when}'
        )


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ['name', 'product', 'price_adjustment', 'quantity_on_hand', 'sku', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'sku', 'product__name']


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ['id', 'product', 'sort_order']
    list_filter = ['product']


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'parent', 'vendor', 'sort_order']
    list_filter = ['vendor']


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ['code', 'vendor', 'discount_type', 'discount_value', 'is_active', 'valid_from', 'valid_until']
    list_filter = ['discount_type', 'is_active']
    search_fields = ['code', 'vendor__business_name']


@admin.register(InventoryAdjustmentLog)
class InventoryAdjustmentLogAdmin(admin.ModelAdmin):
    list_display = ['product', 'quantity', 'note', 'created_at']
    list_filter = ['created_at']
    search_fields = ['product__name', 'note']


@admin.register(VendorProfile)
class VendorProfileAdmin(admin.ModelAdmin):
    list_display = [
        'business_name',
        'user',
        'category',
        'is_verified',
        'is_active',
        'total_orders',
        'rating',
        'created_at',
    ]
    list_filter = ['category', 'is_verified', 'is_active', 'created_at']
    search_fields = ['business_name', 'user__email', 'business_phone']
    actions = ['approve_vendors', 'deactivate_vendors', 'run_kyc_check']

    def approve_vendors(self, request, queryset):
        queryset.update(is_verified=True, is_active=True)
        self.message_user(request, f'{queryset.count()} vendors approved.')

    approve_vendors.short_description = 'Approve selected vendors'

    def deactivate_vendors(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, f'{queryset.count()} vendors deactivated.')

    deactivate_vendors.short_description = 'Deactivate selected vendors'

    def run_kyc_check(self, request, queryset):
        from .tasks import run_sanctions_screening

        for vendor in queryset:
            run_sanctions_screening.delay(vendor.id)
        self.message_user(request, f'KYC screening queued for {queryset.count()} vendors.')

    run_kyc_check.short_description = 'Run sanctions/PEP screening'


@admin.register(VendorKYCDocument)
class VendorKYCDocumentAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'document_type',
        'document_subtype',
        'verification_status',
        'expiry_date',
        'is_required',
        'is_expired',
        'reviewed_by',
        'reviewed_at',
        'created_at',
    ]
    list_filter = ['document_type', 'verification_status', 'is_required', 'expiry_date']
    search_fields = ['vendor__business_name', 'vendor__user__email', 'document_subtype']
    readonly_fields = ['file_size', 'original_filename', 'encryption_key_id', 'uploaded_by', 'created_at', 'updated_at']
    actions = ['mark_verified', 'mark_rejected', 'mark_expired']

    def is_expired(self, obj):
        return obj.is_expired()

    is_expired.boolean = True
    is_expired.short_description = 'Expired'

    def mark_verified(self, request, queryset):
        updated = queryset.update(
            verification_status='VERIFIED', reviewed_by=request.user, reviewed_at=timezone.now(), rejection_reason=''
        )
        self.message_user(request, f'{updated} documents marked as verified.')

    mark_verified.short_description = 'Mark selected as verified'

    def mark_rejected(self, request, queryset):
        # In a real implementation, you'd show a form for rejection reason
        updated = queryset.update(
            verification_status='REJECTED',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
            rejection_reason='Rejected by admin',
        )
        self.message_user(request, f'{updated} documents marked as rejected.')

    mark_rejected.short_description = 'Mark selected as rejected (add reason)'

    def mark_expired(self, request, queryset):
        updated = 0
        for doc in queryset:
            if doc.verification_status in ['VERIFIED', 'PENDING', 'UNDER_REVIEW']:
                doc.mark_expired()
                updated += 1
        self.message_user(request, f'{updated} documents marked as expired.')

    mark_expired.short_description = 'Mark selected as expired'


@admin.register(VendorKYCVerification)
class VendorKYCVerificationAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'overall_status',
        'submitted_at',
        'verified_at',
        'verified_by',
        'sanctions_check_passed',
        'pep_check_passed',
        'next_review_date',
    ]
    list_filter = ['overall_status', 'sanctions_check_passed', 'pep_check_passed']
    search_fields = ['vendor__business_name', 'vendor__user__email']
    readonly_fields = [
        'submitted_at',
        'verified_at',
        'rejected_at',
        'verified_by',
        'sanctions_check_date',
        'pep_check_date',
        'created_at',
        'updated_at',
    ]
    actions = ['refresh_status']

    def refresh_status(self, request, queryset):
        for verification in queryset:
            verification.refresh_status()
        self.message_user(request, f'Status refreshed for {queryset.count()} verifications.')

    refresh_status.short_description = 'Refresh KYC status from documents'


@admin.register(VendorTaxInfo)
class VendorTaxInfoAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'zimra_pin_masked',
        'zimra_pin_verified',
        'vat_registered',
        'tax_clearance_expiry',
        'created_at',
    ]
    list_filter = ['zimra_pin_verified', 'vat_registered']
    search_fields = ['vendor__business_name', 'vendor__user__email']
    readonly_fields = [
        'zimra_pin_verified',
        'zimra_verification_date',
        'zimra_verification_method',
        'created_at',
        'updated_at',
    ]

    def zimra_pin_masked(self, obj):
        if obj.zimra_pin:
            return '*' * 6 + obj.zimra_pin[-4:]
        return 'Not Set'

    zimra_pin_masked.short_description = 'ZIMRA PIN'


@admin.register(VendorSanctionsScreening)
class VendorSanctionsScreeningAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'screening_type',
        'status',
        'matched_name',
        'confidence_score',
        'screened_at',
        'reviewed_by',
        'reviewed_at',
    ]
    list_filter = ['screening_type', 'status']
    search_fields = ['vendor__business_name', 'matched_name']
    readonly_fields = ['screened_at', 'reviewed_at', 'reviewed_by', 'confidence_score', 'matched_fields']
    actions = ['mark_clear', 'mark_potential_match']

    def mark_clear(self, request, queryset):
        updated = queryset.update(status='CLEAR', reviewed_at=timezone.now(), reviewed_by=request.user)
        self.message_user(request, f'{updated} screenings marked as clear.')

    mark_clear.short_description = 'Mark selected as clear'

    def mark_potential_match(self, request, queryset):
        # In production, you'd want a form for this
        updated = queryset.update(status='POTENTIAL_MATCH', reviewed_at=timezone.now(), reviewed_by=request.user)
        self.message_user(request, f'{updated} screenings marked as potential match - manual review required.')

    mark_potential_match.short_description = 'Mark as potential match'


@admin.register(VendorKYCExpiryLog)
class VendorKYCExpiryLogAdmin(admin.ModelAdmin):
    list_display = ['vendor', 'action', 'message', 'created_at']
    list_filter = ['action', 'created_at']
    search_fields = ['vendor__business_name', 'message']
    readonly_fields = ['vendor', 'kyc_document', 'action', 'message', 'metadata', 'created_at']
    actions = None
    ordering = ['-created_at']


@admin.register(VendorPriceChange)
class VendorPriceChangeAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'content_type',
        'object_id',
        'old_price',
        'new_price',
        'change_pct',
        'requires_approval',
        'is_approved',
        'created_at',
    ]
    list_filter = ['content_type', 'requires_approval', 'is_approved']
    search_fields = ['vendor__business_name', 'reason']
    raw_id_fields = ['vendor', 'changed_by', 'approved_by']
    readonly_fields = ['created_at']


@admin.register(VendorRatingSnapshot)
class VendorRatingSnapshotAdmin(admin.ModelAdmin):
    list_display = ['vendor', 'rating', 'order', 'created_at']
    search_fields = ['vendor__business_name']
    raw_id_fields = ['vendor', 'order']
    readonly_fields = ['created_at']


@admin.register(VendorScorecard)
class VendorScorecardAdmin(admin.ModelAdmin):
    list_display = [
        'vendor',
        'on_time_prep_rate',
        'cancellation_rate',
        'avg_rating_30d',
        'total_orders_30d',
        'revenue_30d',
        'computed_at',
    ]
    search_fields = ['vendor__business_name']
    raw_id_fields = ['vendor']
    readonly_fields = ['computed_at']


@admin.register(VendorSupportTicket)
class VendorSupportTicketAdmin(admin.ModelAdmin):
    list_display = ['id', 'vendor', 'subject', 'category', 'priority', 'status', 'assigned_to', 'created_at']
    list_filter = ['status', 'priority', 'category', 'created_at']
    search_fields = ['subject', 'description', 'vendor__business_name', 'vendor__user__email']


@admin.register(VendorTicketMessage)
class VendorTicketMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'ticket', 'sender_is_vendor', 'created_at']
    list_filter = ['sender_is_vendor', 'created_at']


@admin.register(VendorInternalNote)
class VendorInternalNoteAdmin(admin.ModelAdmin):
    list_display = ['id', 'ticket', 'author', 'created_at']
    list_filter = ['created_at']


@admin.register(VendorTicketAttachment)
class VendorTicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'ticket', 'file_name', 'mime_type', 'uploaded_at']
    list_filter = ['mime_type']


@admin.register(PayoutReconciliation)
class PayoutReconciliationAdmin(admin.ModelAdmin):
    list_display = [
        'vendor_profile',
        'period_start',
        'period_end',
        'expected_payout',
        'actual_payout',
        'variance',
        'is_resolved',
        'created_at',
    ]
    list_filter = ['is_resolved']
    search_fields = ['vendor_profile__business_name', 'vendor__email']
    raw_id_fields = ['vendor', 'vendor_profile', 'resolved_by']
    readonly_fields = ['created_at']
