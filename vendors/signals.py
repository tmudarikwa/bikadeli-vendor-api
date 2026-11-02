"""Vendor signals: cache invalidation, price audit, promotion approval, rating decay."""

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from core.caching import MenuCatalogCache

from .models import (
    MenuCategory,
    MenuItem,
    Product,
    ProductVariant,
    Promotion,
    VendorPriceChange,
    VendorRatingSnapshot,
)

logger = logging.getLogger(__name__)


def _vendor_id_of(instance):
    vendor = getattr(instance, 'vendor', None)
    if vendor is None and hasattr(instance, 'product'):
        vendor = getattr(instance.product, 'vendor', None)
    return getattr(vendor, 'id', None)


# ─────────────────────────────────────────────────────────────────────────
# Cache invalidation (existing)
# ─────────────────────────────────────────────────────────────────────────


@receiver(post_save, sender=MenuItem)
@receiver(post_delete, sender=MenuItem)
@receiver(post_save, sender=MenuCategory)
@receiver(post_delete, sender=MenuCategory)
@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
@receiver(post_save, sender=ProductVariant)
@receiver(post_delete, sender=ProductVariant)
def _invalidate_vendor_menu(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    vendor_id = _vendor_id_of(instance)
    if vendor_id:
        MenuCatalogCache.invalidate(vendor_id)


# ─────────────────────────────────────────────────────────────────────────
# Price change audit trail
# ─────────────────────────────────────────────────────────────────────────


def _get_ip(request=None):
    if request is None:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _record_price_change(vendor, content_type, object_id, old_price, new_price, user=None, ip=None):
    old = Decimal(str(old_price))
    new = Decimal(str(new_price))
    if old == 0 or old == new:
        return

    change_pct = ((new - old) / old) * Decimal('100')

    # Check if >50% change within 24h window
    requires_approval = abs(change_pct) > Decimal('50')
    if requires_approval:
        window_start = timezone.now() - timedelta(hours=24)
        recent_count = VendorPriceChange.objects.filter(
            vendor=vendor,
            content_type=content_type,
            object_id=object_id,
            created_at__gte=window_start,
            requires_approval=True,
        ).count()
        if recent_count >= 2:
            requires_approval = True

    VendorPriceChange.objects.create(
        vendor=vendor,
        content_type=content_type,
        object_id=object_id,
        old_price=old,
        new_price=new,
        change_pct=change_pct,
        changed_by=user,
        ip_address=ip,
        requires_approval=requires_approval,
        is_approved=None if requires_approval else True,
    )

    if requires_approval:
        logger.warning(
            'Price spike detected: %s#%s changed %.1f%% ($%s → $%s) — requires approval',
            content_type,
            object_id,
            float(change_pct),
            old,
            new,
        )


@receiver(pre_save, sender=MenuItem)
def _audit_menu_item_price(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    if not instance.pk:
        return
    # Skip if only non-price fields are being updated (common bulk operation)
    update_fields = kwargs.get('update_fields')
    if update_fields and 'price' not in update_fields:
        return
    try:
        old = MenuItem.objects.only('price').get(pk=instance.pk)
    except MenuItem.DoesNotExist:
        return
    if old.price != instance.price:
        _record_price_change(
            vendor=instance.vendor,
            content_type='MENU_ITEM',
            object_id=instance.pk,
            old_price=old.price,
            new_price=instance.price,
            user=getattr(instance, '_changed_by', None),
        )


@receiver(pre_save, sender=Product)
def _audit_product_price(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    if not instance.pk:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields and 'price' not in update_fields:
        return
    try:
        old = Product.objects.only('price').get(pk=instance.pk)
    except Product.DoesNotExist:
        return
    if old.price != instance.price:
        _record_price_change(
            vendor=instance.vendor,
            content_type='PRODUCT',
            object_id=instance.pk,
            old_price=old.price,
            new_price=instance.price,
            user=getattr(instance, '_changed_by', None),
        )


@receiver(pre_save, sender=ProductVariant)
def _audit_variant_price(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    if not instance.pk:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields and 'price_adjustment' not in update_fields:
        return
    try:
        old = ProductVariant.objects.only('price_adjustment').get(pk=instance.pk)
    except ProductVariant.DoesNotExist:
        return
    old_price = instance.product.price + old.price_adjustment
    new_price = instance.product.price + instance.price_adjustment
    if old_price != new_price:
        _record_price_change(
            vendor=instance.product.vendor,
            content_type='PRODUCT_VARIANT',
            object_id=instance.pk,
            old_price=old_price,
            new_price=new_price,
            user=getattr(instance, '_changed_by', None),
        )


# ─────────────────────────────────────────────────────────────────────────
# Menu → catalog sync: every MenuItem gets a linked Product row so the
# Products/Inventory pages (which read the Product table) stay in sync
# with the Menu page. Menu is the source of truth for food items;
# manually-created catalog products (menu_item=None, e.g. via CSV import)
# are never touched.
# ─────────────────────────────────────────────────────────────────────────

_FALLBACK_CATEGORY_NAME = 'Other'


def _resolve_catalog_category(menu_category):
    """Map a MenuCategory to a global Category by exact name (case-insensitive).

    Falls back to the shared 'Other' row (created on demand) so the
    required Product.category FK is always satisfied without littering the
    global taxonomy with per-vendor names.
    """
    from .models import Category

    if menu_category is not None and menu_category.name:
        match = Category.objects.filter(
            name__iexact=menu_category.name.strip(),
            vendor__isnull=True,
            parent__isnull=True,
        ).first()
        if match is not None:
            return match
    fallback, _ = Category.objects.get_or_create(
        name=_FALLBACK_CATEGORY_NAME,
        vendor=None,
        parent=None,
        defaults={'icon': 'shopping_bag'},
    )
    return fallback


def sync_product_for_menu_item(menu_item):
    """Create or update the Product linked to ``menu_item``.

    Shared by the post_save receiver and the backfill migration so both
    paths behave identically. Returns the Product.
    """
    from .models import Product

    category = _resolve_catalog_category(menu_item.category)
    product, _ = Product.objects.get_or_create(
        menu_item=menu_item,
        defaults={
            'vendor': menu_item.vendor,
            'name': menu_item.name,
            'description': menu_item.description,
            'category': category,
            'price': menu_item.price,
            'is_available': menu_item.is_available,
        },
    )
    changed = False
    for field, value in (
        ('name', menu_item.name),
        ('description', menu_item.description),
        ('category', category),
        ('price', menu_item.price),
        ('is_available', menu_item.is_available),
    ):
        if getattr(product, field) != value:
            setattr(product, field, value)
            changed = True
    if changed:
        product.save(update_fields=['name', 'description', 'category', 'price', 'is_available'])
    return product


@receiver(post_save, sender=MenuItem)
def _sync_product_on_menu_item_save(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    sync_product_for_menu_item(instance)


# NOTE: pre_delete (not post_delete) — the menu_item FK is SET_NULL on
# delete, so by post_delete time the link is already gone.
@receiver(pre_delete, sender=MenuItem)
def _unlist_product_on_menu_item_delete(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    from .models import Product

    Product.objects.filter(menu_item=instance).update(is_available=False)
    logger.info('MenuItem %s deleted — linked catalog product unlisted', instance.pk)


# ─────────────────────────────────────────────────────────────────────────
# Promotion auto-approval: >30% discount requires admin approval
# ─────────────────────────────────────────────────────────────────────────


@receiver(pre_save, sender=Promotion)
def _enforce_promotion_approval(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    if not instance.pk:
        return
    try:
        old = Promotion.objects.get(pk=instance.pk)
    except Promotion.DoesNotExist:
        return

    needs_approval = (
        instance.discount_type == 'PERCENTAGE'
        and instance.discount_value > Decimal('30')
        and old.discount_value <= Decimal('30')
    )

    if needs_approval and instance.status == 'ACTIVE':
        instance.status = 'PENDING_APPROVAL'
        logger.info(
            'Promotion %s (%.0f%%) requires admin approval',
            instance.code,
            instance.discount_value,
        )


# ─────────────────────────────────────────────────────────────────────────
# Vendor rating decay: exponential moving average
# ─────────────────────────────────────────────────────────────────────────


def compute_ema_rating(vendor, half_life_days=90, recent_weight=0.7):
    """Compute exponential moving average rating favoring recent ratings.

    Uses an EMA where recent ratings (within half_life) are weighted at
    ``recent_weight`` and older ratings at ``1 - recent_weight``.
    """
    now = timezone.now()
    cutoff = now - timedelta(days=half_life_days)

    recent = (
        VendorRatingSnapshot.objects.filter(
            vendor=vendor,
            created_at__gte=cutoff,
        )
        .order_by('-created_at')
        .values_list('rating', flat=True)
    )

    older = VendorRatingSnapshot.objects.filter(
        vendor=vendor,
        created_at__lt=cutoff,
    ).values_list('rating', flat=True)

    recent_list = [Decimal(str(r)) for r in recent]
    older_list = [Decimal(str(r)) for r in older]

    if not recent_list and not older_list:
        return Decimal('0.00')

    if not older_list:
        return round(sum(recent_list) / len(recent_list), 2)

    if not recent_list:
        return round(sum(older_list) / len(older_list), 2)

    recent_avg = sum(recent_list) / len(recent_list)
    older_avg = sum(older_list) / len(older_list)

    # Weight recent ratings more heavily
    decay = Decimal(str(recent_weight))
    ema = (recent_avg * decay) + (older_avg * (Decimal('1') - decay))

    return round(ema, 2)


@receiver(post_save, sender=VendorRatingSnapshot)
def _update_vendor_rating(sender, instance, **kwargs):  # noqa: ARG001 - framework signature must stay fixed
    """Update VendorProfile.rating using EMA when a new rating is recorded."""
    vendor = instance.vendor
    new_rating = compute_ema_rating(vendor)
    vendor.rating = new_rating
    vendor.save(update_fields=['rating'])
