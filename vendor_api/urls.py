from django.conf import settings
from django.urls import include, path, re_path

from bikadeli_common.gateway import (
    liveness,
    proxy_to_finance_service,
    proxy_to_identity_service,
    proxy_to_monolith,
    proxy_to_orders_service,
    proxy_to_pricing_service,
    readiness,
)


def proxy_vendor_orders(request, *args, **kwargs):
    if settings.VENDOR_ORDER_SERVICE_ENABLED:
        return proxy_to_orders_service(request, *args, **kwargs)
    return proxy_to_monolith(request, *args, **kwargs)


urlpatterns = [
    path("health/", liveness, name="health-liveness"),
    path("health/ready/", readiness, name="health-readiness"),
    re_path(
        r"^api/v1/vendors/(?:orders(?:/.*)?|profile/(?:my-orders|accept-order|reject-order)/?)$",
        proxy_vendor_orders,
        name="orders-service-vendor-workflows",
    ),
    path("", include("vendor_catalog.urls")),
    re_path(
        r"^api/v1/auth/(?:register|login|logout|logout-all|token/refresh)/?$",
        proxy_to_identity_service,
        name="identity-service-auth",
    ),
    re_path(
        r"^api/v1/vendors/login/?$",
        proxy_to_identity_service,
        name="identity-service-vendor-login",
    ),
    re_path(
        r"^api/v1/pricing/weather-surge/$",
        proxy_to_pricing_service,
        name="pricing-service-weather-surge",
    ),
    re_path(
        r"^api/v1/vendors/payouts/?$",
        proxy_to_finance_service,
        name="finance-service-vendor-payouts",
    ),
    re_path(
        r"^api/v1/vendors/reconciliation/?$",
        proxy_to_finance_service,
        name="finance-service-vendor-reconciliation",
    ),
    re_path(
        r"^api/v1/vendors(?:/.*)?$", proxy_to_monolith, name="vendor-api-compatibility"
    ),
]
