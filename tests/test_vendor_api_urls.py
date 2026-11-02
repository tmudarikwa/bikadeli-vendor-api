from unittest.mock import patch

from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import Resolver404, resolve
from vendor_api.urls import proxy_vendor_orders


class VendorApiUrlTests(TestCase):
    urlconf = "vendor_api.urls"

    def test_vendor_routes_are_owned_or_routed_to_their_domain_service(self):
        self.assertEqual(
            resolve("/api/v1/vendors/register/", urlconf=self.urlconf).url_name,
            "vendor-application-register",
        )
        self.assertEqual(
            resolve(
                "/internal/v1/vendors/preparation-time/", urlconf=self.urlconf
            ).url_name,
            "internal-vendor-preparation-time",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/orders/42/accept/", urlconf=self.urlconf).url_name,
            "orders-service-vendor-workflows",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/login/", urlconf=self.urlconf).url_name,
            "identity-service-vendor-login",
        )
        self.assertEqual(
            resolve("/api/v1/auth/login/", urlconf=self.urlconf).url_name,
            "identity-service-auth",
        )
        self.assertEqual(
            resolve("/api/v1/auth/logout-all/", urlconf=self.urlconf).url_name,
            "identity-service-auth",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/profile/me/", urlconf=self.urlconf).url_name,
            "my-profile",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/profile/toggle-open/", urlconf=self.urlconf
            ).url_name,
            "toggle-vendor-availability",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/payouts/", urlconf=self.urlconf).url_name,
            "finance-service-vendor-payouts",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/reconciliation/", urlconf=self.urlconf).url_name,
            "finance-service-vendor-reconciliation",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/menu-items/", urlconf=self.urlconf).url_name,
            "vendor-menu-items",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/public/menu-items/", urlconf=self.urlconf
            ).url_name,
            "public-menu-items",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/menu/add-item/", urlconf=self.urlconf).url_name,
            "legacy-vendor-menu-item-create",
        )
        self.assertEqual(
            resolve("/api/v1/vendors/menu/", urlconf=self.urlconf).url_name,
            "legacy-vendor-menu-list",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/menu/71/upload-images/", urlconf=self.urlconf
            ).url_name,
            "legacy-vendor-menu-item-upload-images",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/menu/71/delete-image/", urlconf=self.urlconf
            ).url_name,
            "legacy-vendor-menu-item-delete-image",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/product-images/501/file/", urlconf=self.urlconf
            ).url_name,
            "product-image-file",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/public/vendors/71/menu/", urlconf=self.urlconf
            ).url_name,
            "public-vendor-menu",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/public/vendors/lookup/", urlconf=self.urlconf
            ).url_name,
            "public-vendor-lookup",
        )
        self.assertEqual(
            resolve(
                "/api/v1/vendors/menu/unimplemented-action/", urlconf=self.urlconf
            ).url_name,
            "unsupported-legacy-vendor-menu-path",
        )
        self.assertEqual(
            resolve("/api/v1/pricing/weather-surge/", urlconf=self.urlconf).url_name,
            "pricing-service-weather-surge",
        )
        with self.assertRaises(Resolver404):
            resolve("/api/v1/drivers/profile/", urlconf=self.urlconf)

    def test_health_endpoints(self):
        client = Client()
        self.assertEqual(client.get("/health/").status_code, 200)
        self.assertEqual(client.get("/health/ready/").status_code, 200)

    def test_unknown_menu_paths_return_local_404(self):
        response = Client().get("/api/v1/vendors/menu/unknown-action/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"],
            "This menu endpoint is not supported by vendor-api.",
        )

    @override_settings(VENDOR_ORDER_SERVICE_ENABLED=True)
    @patch("vendor_api.urls.proxy_to_orders_service")
    @patch("vendor_api.urls.proxy_to_monolith")
    def test_vendor_order_gateway_uses_orders_service_when_enabled(
        self, proxy_to_monolith, proxy_to_orders_service
    ):
        request = RequestFactory().get("/api/v1/vendors/orders/")

        response = proxy_vendor_orders(request)

        self.assertIs(response, proxy_to_orders_service.return_value)
        proxy_to_orders_service.assert_called_once_with(request)
        proxy_to_monolith.assert_not_called()

    @override_settings(VENDOR_ORDER_SERVICE_ENABLED=False)
    @patch("vendor_api.urls.proxy_to_orders_service")
    @patch("vendor_api.urls.proxy_to_monolith")
    def test_vendor_order_gateway_retains_compatibility_when_disabled(
        self, proxy_to_monolith, proxy_to_orders_service
    ):
        request = RequestFactory().get("/api/v1/vendors/orders/")

        response = proxy_vendor_orders(request)

        self.assertIs(response, proxy_to_monolith.return_value)
        proxy_to_monolith.assert_called_once_with(request)
        proxy_to_orders_service.assert_not_called()
