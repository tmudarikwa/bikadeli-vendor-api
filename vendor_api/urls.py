from django.urls import include, path

from core.health import liveness_view, readiness_view
from vendors import urls as vendor_urls

urlpatterns = [
    path('health/', liveness_view, name='health-liveness'),
    path('health/ready/', readiness_view, name='health-readiness'),
    path('api/v1/vendors/', include(vendor_urls.urlpatterns)),
]
