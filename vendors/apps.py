from django.apps import AppConfig


class VendorsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vendors'

    def ready(self):
        # Register menu-cache invalidation signals.
        from . import signals  # noqa: F401
