from unittest.mock import patch

from django.http import JsonResponse
from django.test import Client, TestCase, override_settings


class FinanceProxyTests(TestCase):
    @override_settings(FINANCE_SERVICE_URL="http://finance-service:8000")
    @patch("bikadeli_common.gateway._proxy_to_upstream")
    def test_vendor_payout_reads_are_forwarded_to_finance_service(self, proxy):
        proxy.return_value = JsonResponse({"results": [], "count": 0})

        response = Client().get(
            "/api/v1/vendors/payouts/?page=2",
            HTTP_AUTHORIZATION="Bearer test-access-token",
        )

        self.assertEqual(response.status_code, 200)
        proxy.assert_called_once()
        request, base_url, target_name = proxy.call_args.args
        self.assertEqual(request.path, "/api/v1/vendors/payouts/")
        self.assertEqual(request.GET["page"], "2")
        self.assertEqual(request.headers["Authorization"], "Bearer test-access-token")
        self.assertEqual(base_url, "http://finance-service:8000")
        self.assertEqual(target_name, "finance service")

    @override_settings(FINANCE_SERVICE_URL="http://finance-service:8000")
    @patch("bikadeli_common.gateway._proxy_to_upstream")
    def test_vendor_reconciliation_reads_are_forwarded_to_finance_service(self, proxy):
        proxy.return_value = JsonResponse({"results": [], "count": 0})

        response = Client().get("/api/v1/vendors/reconciliation/")

        self.assertEqual(response.status_code, 200)
        proxy.assert_called_once()
        request, base_url, target_name = proxy.call_args.args
        self.assertEqual(request.path, "/api/v1/vendors/reconciliation/")
        self.assertEqual(base_url, "http://finance-service:8000")
        self.assertEqual(target_name, "finance service")

    @override_settings(FINANCE_SERVICE_URL="")
    def test_unconfigured_finance_service_fails_explicitly(self):
        response = Client().get("/api/v1/vendors/payouts/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": "Upstream service is not configured."}
        )
