import os
import shutil
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import jwt
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from vendor_catalog.models import (
    RestaurantMenuCategory,
    RestaurantMenuItem,
    RestaurantMenuItemImage,
    RestaurantMenuItemOption,
    VendorCatalogSyncOutbox,
    VendorProfile,
)


class RestaurantMenuApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._encryption_environment = patch.dict(
            os.environ,
            {'VENDOR_FIELDS_ENCRYPTION_KEY': Fernet.generate_key().decode()},
        )
        cls._encryption_environment.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._encryption_environment.stop()

    def setUp(self):
        self.vendor = VendorProfile.objects.create(
            id=71,
            owner_user_id=9001,
            business_name='Bika Restaurant',
            category='RESTAURANT',
            is_active=True,
            is_verified=True,
        )
        self.category = RestaurantMenuCategory.objects.create(
            id=34,
            vendor_id=self.vendor.id,
            name='Meals',
            order=1,
        )
        self.item = RestaurantMenuItem.objects.create(
            id=501,
            vendor_id=self.vendor.id,
            category_id=34,
            name='Sadza and stew',
            description='Beef stew with sadza.',
            price=Decimal('5.50'),
        )
        RestaurantMenuItemOption.objects.create(
            id=901,
            menu_item=self.item,
            name='Extra meat',
            price_adjustment=Decimal('1.00'),
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self._access_token(9001)}')

    @staticmethod
    def _access_token(user_id):
        return jwt.encode(
            {
                'token_type': 'access',
                'user_id': user_id,
                'iat': timezone.now(),
                'exp': timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm='HS256',
        )

    def test_owner_crud_keeps_vendor_scope_and_nested_options(self):
        response = self.client.get('/api/v1/vendors/menu-items/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['id'], self.item.id)
        self.assertEqual(response.json()[0]['options'][0]['id'], 901)
        self.assertEqual(response.json()[0]['category_id'], 34)
        detail = self.client.get(f'/api/v1/vendors/menu-items/{self.item.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['id'], self.item.id)

        created = self.client.post(
            '/api/v1/vendors/menu-items/',
            {
                'vendor_id': 999,
                'name': 'Chicken wrap',
                'price': '7.25',
                'options': [{'name': 'Extra sauce', 'price_adjustment': '0.50'}],
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()['vendor_id'], self.vendor.id)
        self.assertEqual(created.json()['options'][0]['name'], 'Extra sauce')
        self.assertIsNone(
            VendorCatalogSyncOutbox.objects.get(vendor_id=self.vendor.id).delivered_at
        )

        changed = self.client.patch(
            f'/api/v1/vendors/menu-items/{self.item.id}/',
            {'options': [{'name': 'Large portion', 'price_adjustment': '2.00'}]},
            format='json',
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['options'][0]['name'], 'Large portion')

        deleted = self.client.delete(f'/api/v1/vendors/menu-items/{created.json()["id"]}/')
        self.assertEqual(deleted.status_code, 204)

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self._access_token(9002)}')
        self.assertEqual(self.client.get(f'/api/v1/vendors/menu-items/{self.item.id}/').status_code, 404)

    def test_legacy_menu_category_detail_update_and_category_create_contract(self):
        menu = self.client.get('/api/v1/vendors/menu/')
        self.assertEqual(menu.status_code, 200)
        self.assertEqual(menu.json()['count'], 1)
        category = menu.json()['results'][0]
        self.assertEqual(category['id'], self.category.id)
        self.assertEqual(category['name'], 'Meals')
        item = category['items'][0]
        self.assertEqual(item['id'], self.item.id)
        self.assertEqual(item['category_name'], 'Meals')
        self.assertEqual(item['images'], [])
        self.assertEqual(item['vendor_id'], self.vendor.id)

        detail = self.client.get(f'/api/v1/vendors/menu/{self.item.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['description'], 'Beef stew with sadza.')
        self.assertEqual(detail.json()['vendor_id'], self.vendor.id)
        changed = self.client.put(
            f'/api/v1/vendors/menu/{self.item.id}/update-item/',
            {
                'name': 'Updated stew',
                'description': 'New description',
                'price': '6.00',
                'is_available': False,
            },
            format='json',
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['item']['name'], 'Updated stew')
        listed_after_update = self.client.get('/api/v1/vendors/menu/')
        self.assertEqual(listed_after_update.json()['results'][0]['items'][0]['is_available'], False)

        created = self.client.post(
            '/api/v1/vendors/menu/add-category/',
            {'name': 'Drinks', 'description': 'Cold drinks'},
            format='json',
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()['category']['order'], 2)
        added_item = self.client.post(
            '/api/v1/vendors/menu/add-item/',
            {
                'category_id': created.json()['category']['id'],
                'name': 'Water',
                'price': '1.25',
                'description': 'Bottled water',
            },
            format='json',
        )
        self.assertEqual(added_item.status_code, 201)
        self.assertEqual(added_item.json()['item']['category_name'], 'Drinks')
        soft_deleted = self.client.delete(
            f'/api/v1/vendors/menu/{added_item.json()["item"]["id"]}/delete-item/'
        )
        self.assertEqual(soft_deleted.status_code, 204)
        self.assertFalse(
            RestaurantMenuItem.objects.get(pk=added_item.json()['item']['id']).is_available
        )

    @override_settings(MEDIA_ROOT=str(Path(__file__).resolve().parent / '.menu-media-test'))
    def test_legacy_image_upload_serving_validation_and_delete(self):
        media_root = Path(settings.MEDIA_ROOT)
        self.addCleanup(shutil.rmtree, media_root, True)
        gif = bytes.fromhex(
            '47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b'
        )
        upload = SimpleUploadedFile('dish.gif', gif, content_type='image/gif')
        response = self.client.post(
            f'/api/v1/vendors/menu/{self.item.id}/upload-images/',
            {'images': upload},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.content)
        image_data = response.json()['created'][0]
        self.assertEqual(image_data['vendor_id'], self.vendor.id)
        self.assertEqual(image_data['sort_order'], 0)
        self.assertTrue(image_data['image'].endswith(f'/api/v1/vendors/menu-images/{image_data["id"]}/file/'))

        detail = self.client.get(f'/api/v1/vendors/menu/{self.item.id}/')
        self.assertEqual(detail.json()['images'][0]['id'], image_data['id'])
        file_response = APIClient().get(urlsplit(image_data['image']).path)
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response['Content-Type'], 'image/gif')
        self.assertEqual(b''.join(file_response.streaming_content), gif)

        rejected = self.client.post(
            f'/api/v1/vendors/menu/{self.item.id}/upload-images/',
            {
                'images': SimpleUploadedFile(
                    'invalid.jpg',
                    b'not an image',
                    content_type='image/jpeg',
                )
            },
            format='multipart',
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()['created'], [])
        self.assertEqual(len(rejected.json()['errors']), 1)
        oversized = self.client.post(
            f'/api/v1/vendors/menu/{self.item.id}/upload-images/',
            {
                'images': SimpleUploadedFile(
                    'oversized.jpg',
                    b'x' * (5 * 1024 * 1024 + 1),
                    content_type='image/jpeg',
                )
            },
            format='multipart',
        )
        self.assertEqual(oversized.status_code, 200)
        self.assertIn('maximum 5 MiB', oversized.json()['errors'][0])

        removed = self.client.delete(
            f'/api/v1/vendors/menu/{self.item.id}/delete-image/',
            {'image_id': image_data['id']},
            format='json',
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(RestaurantMenuItemImage.objects.filter(pk=image_data['id']).exists())

    @override_settings(
        FOOD_CATALOG_SYNC_URL='https://orders.example.test/api/v1/orders/internal/catalog/menu-items/',
        FOOD_CATALOG_SYNC_TOKEN='s' * 32,
    )
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.httpx.Client')
    def test_delete_soft_deactivates_item_and_later_sync_sends_unavailable(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.post.return_value.is_success = True

        deleted = self.client.delete(f'/api/v1/vendors/menu-items/{self.item.id}/')
        self.assertEqual(deleted.status_code, 204)
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_available)
        self.assertTrue(RestaurantMenuItemOption.objects.filter(menu_item=self.item, pk=901).exists())

        owner_list = self.client.get('/api/v1/vendors/menu-items/')
        self.assertEqual(owner_list.status_code, 200)
        self.assertEqual([row['id'] for row in owner_list.json()], [self.item.id])
        self.assertFalse(owner_list.json()[0]['is_available'])

        output = StringIO()
        call_command('sync_food_menu_snapshots', stdout=output)
        self.assertIn('synced=1, failed=0', output.getvalue())
        self.assertFalse(client.post.call_args.kwargs['json']['is_available'])
        self.assertEqual(client.post.call_args.kwargs['json']['id'], self.item.id)

    def test_public_contract_contains_order_snapshot_fields_and_hides_unverified_vendor(self):
        response = APIClient().get('/api/v1/vendors/public/menu-items/?vendor_id=71')
        self.assertEqual(response.status_code, 200)
        row = response.json()[0]
        self.assertNotIn('vendor_user_id', row)
        self.assertEqual(row['vendor_name'], 'Bika Restaurant')
        self.assertEqual(row['price'], '5.50')
        self.assertEqual(row['options'][0]['price_adjustment'], '1.00')
        self.assertNotIn('vendor_address', row)
        self.assertNotIn('vendor_user_id', row)
        detail = APIClient().get(f'/api/v1/vendors/public/menu-items/{self.item.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['id'], self.item.id)

        self.vendor.is_verified = False
        self.vendor.save()
        hidden = APIClient().get('/api/v1/vendors/public/menu-items/?vendor_id=71')
        self.assertEqual(hidden.json(), [])

    def test_legacy_public_vendor_menu_returns_active_categories_and_available_items(self):
        unavailable = RestaurantMenuItem.objects.create(
            id=502,
            vendor_id=self.vendor.id,
            category_id=self.category.id,
            name='Sold out',
            price=Decimal('3.00'),
            is_available=False,
        )
        response = APIClient().get(f'/api/v1/vendors/public/vendors/{self.vendor.id}/menu/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual([item['id'] for item in response.json()[0]['items']], [self.item.id])
        self.assertNotIn(unavailable.id, [item['id'] for item in response.json()[0]['items']])

    @override_settings(
        FOOD_CATALOG_SYNC_URL='https://orders.example.test/api/v1/orders/internal/catalog/menu-items/',
        FOOD_CATALOG_SYNC_TOKEN='s' * 32,
    )
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.httpx.Client')
    def test_explicit_sync_command_posts_orders_snapshot_contract(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.post.return_value.is_success = True
        call_command('sync_food_menu_snapshots', stdout=StringIO())

        args, kwargs = client.post.call_args
        self.assertEqual(args[0], settings.FOOD_CATALOG_SYNC_URL)
        self.assertEqual(kwargs['headers']['X-Catalog-Sync-Token'], settings.FOOD_CATALOG_SYNC_TOKEN)
        self.assertEqual(kwargs['json']['id'], self.item.id)
        self.assertEqual(kwargs['json']['vendor_user_id'], 9001)
        self.assertEqual(kwargs['json']['options'][0]['id'], 901)

    @override_settings(
        FOOD_CATALOG_SYNC_URL='https://orders.example.test/api/v1/orders/internal/catalog/menu-items/',
        FOOD_CATALOG_SYNC_TOKEN='s' * 32,
    )
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.httpx.Client')
    def test_availability_change_is_delivered_from_pending_catalog_outbox(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.post.return_value.is_success = True

        response = self.client.post(
            '/api/v1/vendors/profile/toggle-open/',
            {'is_open': True},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        event = VendorCatalogSyncOutbox.objects.get(vendor_id=self.vendor.id)
        self.assertIsNone(event.delivered_at)

        output = StringIO()
        call_command('sync_food_menu_snapshots', '--pending-only', stdout=output)

        self.assertIn('synced=1, failed=0', output.getvalue())
        self.assertTrue(client.post.call_args.kwargs['json']['vendor_is_open'])
        event.refresh_from_db()
        self.assertIsNotNone(event.delivered_at)
        self.assertEqual(event.attempts, 0)

    @override_settings(
        FOOD_CATALOG_SYNC_URL='https://orders.example.test/api/v1/orders/internal/catalog/menu-items/',
        FOOD_CATALOG_SYNC_TOKEN='s' * 32,
    )
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.time.sleep')
    @patch('vendor_catalog.management.commands.sync_food_menu_snapshots.httpx.Client')
    def test_failed_pending_snapshot_remains_retryable(self, client_factory, _sleep):
        client = client_factory.return_value.__enter__.return_value
        client.post.return_value.is_success = False
        client.post.return_value.status_code = 503
        client.post.return_value.text = 'orders unavailable'

        response = self.client.post(
            '/api/v1/vendors/profile/toggle-open/',
            {'is_open': True},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

        with self.assertRaises(CommandError):
            call_command('sync_food_menu_snapshots', '--pending-only', '--retries=0', stdout=StringIO())

        event = VendorCatalogSyncOutbox.objects.get(vendor_id=self.vendor.id)
        self.assertIsNone(event.delivered_at)
        self.assertEqual(event.attempts, 1)
        self.assertIn('HTTP 503', event.last_error)
