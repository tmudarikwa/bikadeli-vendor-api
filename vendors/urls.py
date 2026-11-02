from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from . import marketplace_views, views
from .map_tiles import vendor_map_tiles

router = DefaultRouter()
router.register(r'profile', views.VendorProfileViewSet, basename='vendor-profile')
router.register(r'menu', views.MenuViewSet, basename='vendor-menu')
router.register(r'orders', views.VendorOrderViewSet, basename='vendor-orders')
router.register(r'products', views.VendorProductViewSet, basename='vendor-products')
router.register(r'categories', views.VendorCategoryViewSet, basename='vendor-categories')
router.register(r'public/vendors', views.PublicVendorViewSet, basename='public-vendors')
router.register(r'inventory', views.InventoryViewSet, basename='vendor-inventory')
router.register(r'promotions', views.PromotionViewSet, basename='vendor-promotions')
router.register(r'support-tickets', views.VendorSupportTicketViewSet, basename='vendor-support-tickets')

# KYC Compliance routes
router.register(r'kyc/documents', views.VendorKYCDocumentViewSet, basename='vendor-kyc-documents')
router.register(r'kyc/verification', views.VendorKYCVerificationViewSet, basename='vendor-kyc-verification')
router.register(r'kyc/tax', views.VendorTaxInfoViewSet, basename='vendor-tax-info')
router.register(r'kyc/sanctions', views.VendorSanctionsScreeningViewSet, basename='vendor-sanctions')
router.register(r'kyc/dashboard', views.VendorComplianceDashboardViewSet, basename='vendor-compliance-dashboard')

# Admin KYC routes (add admin permission check in views)
router.register(r'admin/kyc/documents', views.AdminKYCDocumentViewSet, basename='admin-kyc-documents')
router.register(r'admin/kyc/verifications', views.AdminKYCVerificationViewSet, basename='admin-kyc-verifications')

marketplace_router = DefaultRouter()
marketplace_router.register(
    r'products', marketplace_views.VendorMarketplaceProductViewSet, basename='vendor-marketplace-products'
)

urlpatterns = [
    re_path(r'^login/?$', views.login_vendor_api, name='vendor-api-login'),
    re_path(r'^register/?$', views.register_vendor_api, name='vendor-api-register'),
    path('map/tiles/<int:z>/<int:x>/<int:y>/', vendor_map_tiles, name='vendor-map-tiles'),
    path('', include(router.urls)),
    path('vendor/bulk-import/', views.bulk_import_csv, name='bulk-import'),
    path('tasks/<str:task_id>/', views.task_status, name='task-status'),
    path('analytics/', views.vendor_analytics, name='vendor-analytics'),
    path('analytics/export-csv/', views.vendor_analytics_csv, name='vendor-analytics-csv'),
    path('payouts/', views.vendor_payouts, name='vendor-payouts'),
    path('holiday-overrides/', views.vendor_holiday_overrides, name='vendor-holiday-overrides'),
    path('scorecard/', views.vendor_scorecard, name='vendor-scorecard'),
    path('reconciliation/', views.vendor_reconciliation, name='vendor-reconciliation'),
    path('marketplace/', include(marketplace_router.urls)),
]
