from importlib import import_module

_base_settings = import_module('zimdelivery.settings')
for _setting_name in dir(_base_settings):
    if _setting_name.isupper():
        globals()[_setting_name] = getattr(_base_settings, _setting_name)

ROOT_URLCONF = 'vendor_api.urls'
WSGI_APPLICATION = 'vendor_api.wsgi.application'
ASGI_APPLICATION = 'vendor_api.asgi.application'
ROLE_API_NAME = 'vendor-api'
