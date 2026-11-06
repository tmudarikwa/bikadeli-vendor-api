import os
from pathlib import Path
from urllib.parse import urlsplit

from bikadeli_common.settings import (
    configure_database,
    configure_gateway_settings,
    configure_service_oauth,
    configure_token_verification,
    configure_upstream_url,
)

configure_gateway_settings(globals(), 'vendor-api')
configure_database(globals(), 'vendor-api')
configure_service_oauth(
    globals(),
    service_name='vendor-api',
    owner_domain='vendors',
    features=['vendor_profile', 'vendor_catalog', 'vendor_orders', 'vendor_payouts', 'vendor_kyc', 'vendor_notifications'],
    downstream_services=['identity-service', 'orders-service', 'finance-service', 'admin-api'],
)
configure_token_verification(globals())
configure_upstream_url(globals(), 'PRICING_SERVICE_URL', 'PRICING_SERVICE_URL')
configure_upstream_url(globals(), 'IDENTITY_SERVICE_URL', 'IDENTITY_SERVICE_URL')
configure_upstream_url(globals(), 'FINANCE_SERVICE_URL', 'FINANCE_SERVICE_URL')
configure_upstream_url(globals(), 'ORDERS_SERVICE_URL', 'ORDERS_SERVICE_URL')
TIME_ZONE = 'Africa/Harare'
USE_TZ = True
CHANNEL_REDIS_URL = os.getenv('CHANNEL_REDIS_URL', os.getenv('REDIS_URL', '')).strip()
if CHANNEL_REDIS_URL:
    channel_redis_url = urlsplit(CHANNEL_REDIS_URL)
    if channel_redis_url.scheme not in {'redis', 'rediss'} or not channel_redis_url.hostname:
        raise ValueError('CHANNEL_REDIS_URL must be a Redis URL.')
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [CHANNEL_REDIS_URL]},
        },
    }
else:
    if not DEBUG:
        raise ValueError('CHANNEL_REDIS_URL or REDIS_URL must be configured.')
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }
FOOD_CATALOG_SYNC_URL = os.getenv('FOOD_CATALOG_SYNC_URL', '').strip()
FOOD_CATALOG_SYNC_TOKEN = os.getenv('FOOD_CATALOG_SYNC_TOKEN', '')
VENDOR_REGISTRATION_TOKEN = os.getenv('VENDOR_REGISTRATION_TOKEN', '')
VENDOR_ADMIN_TOKEN = os.getenv('VENDOR_ADMIN_TOKEN', '')
IDENTITY_USER_LOOKUP_TOKEN = os.getenv('IDENTITY_USER_LOOKUP_TOKEN', '')
VENDOR_NOTIFICATION_TOKEN = os.getenv('VENDOR_NOTIFICATION_TOKEN', '')
VENDOR_PROFILE_UPDATE_SECRET = os.getenv('VENDOR_PROFILE_UPDATE_SECRET', '')
ORDER_CANCELLATION_NOTIFICATION_SECRET = os.getenv(
    'ORDER_CANCELLATION_NOTIFICATION_SECRET', ''
)
VENDOR_KYC_SERVICE_ENABLED = os.getenv('VENDOR_KYC_SERVICE_ENABLED', 'false').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}
VENDOR_ADMIN_OPS_ENABLED = os.getenv('VENDOR_ADMIN_OPS_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
VENDOR_PAYOUT_SAFETY_ENABLED = os.getenv(
    'VENDOR_PAYOUT_SAFETY_ENABLED', 'false'
).strip().lower() in {'1', 'true', 'yes', 'on'}
VENDOR_INTEGRITY_SERVICE_ENABLED = os.getenv('VENDOR_INTEGRITY_SERVICE_ENABLED', 'false').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}
VENDOR_ORDER_SERVICE_ENABLED = os.getenv('VENDOR_ORDER_SERVICE_ENABLED', 'false').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}
if VENDOR_ORDER_SERVICE_ENABLED and not ORDERS_SERVICE_URL:
    raise ValueError('ORDERS_SERVICE_URL is required when vendor order workflows are enabled.')
VENDOR_PUBLIC_BASE_URL = os.getenv('VENDOR_PUBLIC_BASE_URL', '').strip().rstrip('/')
MEDIA_ROOT = os.getenv('MEDIA_ROOT', '/app/media')
MEDIA_URL = '/media/'
USE_S3_MEDIA = os.getenv('USE_S3_MEDIA', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
KYC_MEDIA_ROOT = os.getenv('KYC_MEDIA_ROOT', str(Path(MEDIA_ROOT).parent / 'private-kyc'))
AWS_STORAGE_BUCKET_NAME = os.getenv('AWS_STORAGE_BUCKET_NAME', '').strip()
AWS_S3_REGION_NAME = os.getenv('AWS_S3_REGION_NAME', 'af-south-1').strip()
AWS_S3_ENDPOINT_URL = os.getenv('AWS_S3_ENDPOINT_URL', '').strip() or None
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID', '').strip() or None
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY', '').strip() or None
AWS_SESSION_TOKEN = os.getenv('AWS_SESSION_TOKEN', '').strip() or None
AWS_S3_KMS_KEY_ARN = os.getenv('AWS_S3_KMS_KEY_ARN', '').strip()
CLAMAV_ENABLED = os.getenv('CLAMAV_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
CLAMAV_HOST = os.getenv('CLAMAV_HOST', 'localhost').strip()
CLAMAV_PORT = int(os.getenv('CLAMAV_PORT', '3310'))
CLAMAV_TIMEOUT_SECONDS = float(os.getenv('CLAMAV_TIMEOUT_SECONDS', '30'))
if not DEBUG and not IDENTITY_SERVICE_URL:
    raise ValueError('IDENTITY_SERVICE_URL must be configured.')
if not DEBUG and len(VENDOR_REGISTRATION_TOKEN) < 32:
    raise ValueError('VENDOR_REGISTRATION_TOKEN must contain at least 32 characters.')
if not DEBUG and len(VENDOR_ADMIN_TOKEN) < 32:
    raise ValueError('VENDOR_ADMIN_TOKEN must contain at least 32 characters.')
if VENDOR_PAYOUT_SAFETY_ENABLED and (
    not IDENTITY_SERVICE_URL
    or len(IDENTITY_USER_LOOKUP_TOKEN) < 32
    or len(VENDOR_ADMIN_TOKEN) < 32
):
    raise ValueError(
        'Vendor payout safety requires Identity service, identity lookup token, and vendor admin token.'
    )
if VENDOR_NOTIFICATION_TOKEN and len(VENDOR_NOTIFICATION_TOKEN) < 32:
    raise ValueError('VENDOR_NOTIFICATION_TOKEN must contain at least 32 characters.')
if VENDOR_PROFILE_UPDATE_SECRET and len(VENDOR_PROFILE_UPDATE_SECRET) < 32:
    raise ValueError('VENDOR_PROFILE_UPDATE_SECRET must contain at least 32 characters.')
if ORDER_CANCELLATION_NOTIFICATION_SECRET and len(
    ORDER_CANCELLATION_NOTIFICATION_SECRET
) < 32:
    raise ValueError(
        'ORDER_CANCELLATION_NOTIFICATION_SECRET must contain at least 32 characters.'
    )
if USE_S3_MEDIA and not AWS_STORAGE_BUCKET_NAME:
    raise ValueError('AWS_STORAGE_BUCKET_NAME must be configured when USE_S3_MEDIA is enabled.')
if not USE_S3_MEDIA:
    public_media_root = Path(MEDIA_ROOT).resolve()
    private_kyc_root = Path(KYC_MEDIA_ROOT).resolve()
    if private_kyc_root == public_media_root or public_media_root in private_kyc_root.parents:
        raise ValueError('KYC_MEDIA_ROOT must be outside MEDIA_ROOT when using local KYC storage.')
if not DEBUG and VENDOR_KYC_SERVICE_ENABLED:
    if not USE_S3_MEDIA:
        raise ValueError('USE_S3_MEDIA=true is required when vendor KYC extraction is enabled.')
    if not AWS_S3_KMS_KEY_ARN:
        raise ValueError('AWS_S3_KMS_KEY_ARN must be configured for KYC document encryption.')
    if not CLAMAV_ENABLED:
        raise ValueError('CLAMAV_ENABLED=true is required to scan KYC documents in production.')
    if CLAMAV_PORT < 1 or CLAMAV_PORT > 65535 or CLAMAV_TIMEOUT_SECONDS <= 0:
        raise ValueError('ClamAV host, port, and timeout must be valid.')
AWS_DEFAULT_ACL = 'private'
AWS_QUERYSTRING_AUTH = True
AWS_QUERYSTRING_EXPIRE = 900
AWS_S3_FILE_OVERWRITE = False
AWS_S3_ADDRESSING_STYLE = os.getenv('AWS_S3_ADDRESSING_STYLE', 'path' if AWS_S3_ENDPOINT_URL else 'virtual')

ROOT_URLCONF = 'vendor_api.urls'
WSGI_APPLICATION = 'vendor_api.wsgi.application'
ASGI_APPLICATION = 'vendor_api.asgi.application'
DATABASE_READINESS_CHECK = True
INSTALLED_APPS = [
    'channels',
    'rest_framework',
    'vendor_catalog.apps.VendorCatalogConfig',
]
VENDOR_FIELDS_ENCRYPTION_KEY = os.getenv('VENDOR_FIELDS_ENCRYPTION_KEY', '')
if not DEBUG and not VENDOR_FIELDS_ENCRYPTION_KEY:
    raise ValueError('VENDOR_FIELDS_ENCRYPTION_KEY must be configured.')
if not DEBUG and not VENDOR_PUBLIC_BASE_URL:
    raise ValueError('VENDOR_PUBLIC_BASE_URL must be configured for public media URLs.')
REST_FRAMEWORK = {
    'UNAUTHENTICATED_USER': None,
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'bikadeli_common.authentication.AccessTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'vendor_registration': os.getenv('VENDOR_REGISTRATION_RATE_LIMIT', '10/hour'),
    },
}
