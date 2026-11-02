import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import jwt
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient
from vendor_catalog.models import CatalogCategory, Product, ProductImage, VendorProfile


def _png_upload(name="mango.png"):
    image = Image.new("RGB", (1, 1), color="green")
    image_data = BytesIO()
    image.save(image_data, format="PNG")
    return SimpleUploadedFile(name, image_data.getvalue(), content_type="image/png")


class VendorCatalogApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._encryption_environment = patch.dict(
            os.environ,
            {"VENDOR_FIELDS_ENCRYPTION_KEY": Fernet.generate_key().decode()},
        )
        cls._encryption_environment.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._encryption_environment.stop()

    def setUp(self):
        self.owner_id = 41
        self.vendor = VendorProfile.objects.create(
            owner_user_id=self.owner_id,
            business_name="Bika Foods",
            category="GROCERY",
            business_phone="+263771234567",
            business_email="private@example.test",
            business_address="Private vendor location",
            is_active=True,
            is_verified=True,
        )
        self.category = CatalogCategory.objects.create(name="Fruit", slug="fruit")
        self.product = Product.objects.create(
            vendor=self.vendor,
            category=self.category,
            name="Mango",
            sku="MANGO-1",
            price=Decimal("1.50"),
            stock=12,
        )
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self._access_token(self.owner_id)}"
        )

    @staticmethod
    def _access_token(user_id):
        return jwt.encode(
            {
                "token_type": "access",
                "user_id": user_id,
                "iat": timezone.now(),
                "exp": timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm="HS256",
        )

    def test_vendor_profile_and_products_are_owned_by_external_user_id(self):
        profile = self.client.get("/api/v1/vendors/profile/me/")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["business_name"], "Bika Foods")
        self.assertNotIn("owner_user_id", profile.json())

        products = self.client.get("/api/v1/vendors/products/")
        self.assertEqual(products.status_code, 200)
        self.assertEqual(products.json()[0]["name"], "Mango")
        self.assertEqual(products.json()[0]["stock"], 12)

        updated = self.client.patch(
            f"/api/v1/vendors/products/{self.product.pk}/",
            {"price": "1.75"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("1.75"))

    def test_vendor_cannot_change_admin_managed_delivery_pricing(self):
        response = self.client.put(
            "/api/v1/vendors/profile/me/",
            {"delivery_fee": "0.00", "minimum_order": "0.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "Fields not editable by vendors: delivery_fee, minimum_order"},
        )
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.delivery_fee, Decimal("0.00"))
        self.assertEqual(self.vendor.minimum_order, Decimal("0.00"))

    def test_vendor_can_toggle_availability_on_owned_profile(self):
        response = self.client.post(
            "/api/v1/vendors/profile/toggle-open/",
            {"is_open": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"is_open": True, "message": "Your business is now open"},
        )
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_open)

    def test_vendor_availability_requires_boolean_and_valid_access_token(self):
        invalid = self.client.post(
            "/api/v1/vendors/profile/toggle-open/",
            {"is_open": "sometimes"},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400)

        self.client.credentials(HTTP_AUTHORIZATION="Bearer invalid")
        unauthorized = self.client.post(
            "/api/v1/vendors/profile/toggle-open/",
            {"is_open": False},
            format="json",
        )
        self.assertEqual(unauthorized.status_code, 401)

    def test_vendor_availability_returns_not_found_without_owned_profile(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self._access_token(self.owner_id + 1)}"
        )
        response = self.client.post(
            "/api/v1/vendors/profile/toggle-open/",
            {"is_open": True},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Vendor profile not found"})

    def test_vendor_cannot_access_another_vendors_products(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._access_token(99)}")
        response = self.client.get(f"/api/v1/vendors/products/{self.product.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_vendor_mutations_require_a_valid_access_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer invalid")
        response = self.client.get("/api/v1/vendors/profile/me/")
        self.assertEqual(response.status_code, 401)

    def test_public_catalog_does_not_expose_vendor_contact_or_inventory(self):
        response = APIClient().get("/api/v1/vendors/public/vendors/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("business_email", response.json()[0])
        self.assertNotIn("business_phone", response.json()[0])
        self.assertNotIn("business_address", response.json()[0])

        products = APIClient().get("/api/v1/vendors/public/products/")
        self.assertEqual(products.status_code, 200)
        self.assertEqual(products.json()[0]["name"], "Mango")
        self.assertNotIn("stock", products.json()[0])
        self.assertNotIn("sku", products.json()[0])

    def test_public_vendor_lookup_returns_only_ids_and_names(self):
        inactive_vendor = VendorProfile.objects.create(
            owner_user_id=43,
            business_name="Closed Vendor",
            category="GROCERY",
            is_active=False,
            is_verified=False,
        )
        response = APIClient().get(
            "/api/v1/vendors/public/vendors/lookup/",
            {"ids": f"{self.vendor.pk},{inactive_vendor.pk}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [
                {"id": self.vendor.pk, "business_name": "Bika Foods"},
                {"id": inactive_vendor.pk, "business_name": "Closed Vendor"},
            ],
        )

    def test_public_vendor_lookup_validates_ids_and_batch_size(self):
        invalid = APIClient().get(
            "/api/v1/vendors/public/vendors/lookup/", {"ids": "1,nope"}
        )
        too_many = APIClient().get(
            "/api/v1/vendors/public/vendors/lookup/",
            {"ids": ",".join(str(vendor_id) for vendor_id in range(1, 202))},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(too_many.status_code, 400)

    def test_public_product_lookup_returns_minimal_product_data_and_image_url(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(
                MEDIA_ROOT=media_root,
                VENDOR_PUBLIC_BASE_URL="https://gateway.example",
            ),
        ):
            image = ProductImage.objects.create(
                product=self.product, image=_png_upload()
            )
            response = APIClient().get(
                "/api/v1/vendors/public/products/lookup/",
                {"ids": f"{self.product.pk},999"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "results": [
                    {
                        "id": self.product.pk,
                        "name": "Mango",
                        "price": "1.50",
                        "image": f"https://gateway.example/api/v1/vendors/product-images/{image.pk}/file/",
                    }
                ]
            },
        )

    def test_public_product_lookup_validates_ids_and_batch_size(self):
        invalid = APIClient().get(
            "/api/v1/vendors/public/products/lookup/", {"ids": "1,nope"}
        )
        too_many = APIClient().get(
            "/api/v1/vendors/public/products/lookup/",
            {"ids": ",".join(str(product_id) for product_id in range(1, 202))},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(too_many.status_code, 400)

    def test_vendor_product_images_are_uploaded_and_publicly_served(self):
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
        ):
            created = self.client.post(
                "/api/v1/vendors/products/",
                {
                    "name": "Mango",
                    "description": "Ripe mango",
                    "category": self.category.pk,
                    "price": "1.50",
                    "images": [_png_upload()],
                },
                format="multipart",
            )

            self.assertEqual(created.status_code, 201, created.data)
            self.assertEqual(len(created.data["images"]), 1)
            image_url = created.data["images"][0]["image"]
            image_path = image_url.split("://", 1)[-1].split("/", 1)[-1]
            image_path = "/" + image_path
            served = APIClient().get(image_path)
            try:
                self.assertEqual(served.status_code, 200)
                self.assertEqual(served["Content-Type"], "image/png")
                self.assertEqual(served["X-Content-Type-Options"], "nosniff")
                self.assertTrue(b"".join(served.streaming_content))
            finally:
                served.close()

    def test_migrated_vendor_profile_url_resolves_locally(self):
        match = resolve("/api/v1/vendors/profile/me/")
        self.assertEqual(match.view_name, "vendor_catalog:my-profile")

    def test_public_marketplace_products_filter_and_return_owned_fields(self):
        self.product.is_popular = True
        self.product.rating = Decimal("4.50")
        self.product.save()
        hidden_stock = Product.objects.create(
            vendor=self.vendor,
            category=self.category,
            name="Out of stock Mango",
            price=Decimal("1.00"),
            is_inventory_tracked=True,
            quantity_on_hand=0,
        )
        restaurant = VendorProfile.objects.create(
            owner_user_id=42,
            business_name="Restaurant",
            category="RESTAURANT",
            is_active=True,
            is_verified=True,
        )
        Product.objects.create(
            vendor=restaurant,
            category=self.category,
            name="Restaurant item",
            price=Decimal("2.00"),
        )

        client = APIClient()
        response = client.get("/api/v1/marketplace/products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["name"] for item in response.json()["results"]], ["Mango"]
        )
        item = response.json()["results"][0]
        self.assertEqual(item["vendor"], self.vendor.pk)
        self.assertEqual(item["vendor_name"], "Bika Foods")
        self.assertEqual(item["category_name"], "Fruit")
        self.assertEqual(item["rating"], "4.50")
        self.assertEqual(item["stock_status"], "NOT_TRACKED")
        self.assertNotIn("business_email", item)
        self.assertNotIn("stock", item)
        self.assertNotIn("sku", item)

        filtered = client.get(
            "/api/v1/marketplace/products/",
            {
                "q": "MANGO",
                "category": self.category.pk,
                "min_price": "1.25",
                "popular": "true",
            },
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(
            [item["id"] for item in filtered.json()["results"]], [self.product.pk]
        )

        include_out_of_stock = client.get(
            "/api/v1/marketplace/products/", {"in_stock": "false"}
        )
        self.assertEqual(include_out_of_stock.status_code, 200)
        self.assertEqual(
            {item["id"] for item in include_out_of_stock.json()["results"]},
            {self.product.pk, hidden_stock.pk},
        )

    def test_public_marketplace_categories_include_active_hierarchy_only(self):
        self.category.icon = "fruit"
        self.category.save()
        child = CatalogCategory.objects.create(
            name="Citrus", slug="citrus", parent=self.category
        )
        CatalogCategory.objects.create(
            name="Inactive", slug="inactive", parent=self.category, is_active=False
        )
        CatalogCategory.objects.create(
            name="Vendor category",
            slug="vendor-category",
            vendor=self.vendor,
        )

        response = APIClient().get("/api/v1/marketplace/categories/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        root = response.json()[0]
        self.assertEqual(root["id"], self.category.pk)
        self.assertEqual(root["icon"], "fruit")
        self.assertEqual([item["id"] for item in root["children"]], [child.pk])

    def test_marketplace_urls_are_service_owned(self):
        self.assertEqual(
            resolve("/api/v1/marketplace/products/").view_name,
            "vendor_catalog:marketplace-products",
        )
        self.assertEqual(
            resolve("/api/v1/marketplace/categories/").view_name,
            "vendor_catalog:marketplace-categories",
        )
