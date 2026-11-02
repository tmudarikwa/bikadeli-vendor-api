"""Vendor-authenticated endpoint for map tiles."""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from core.map_tiles import map_tile_response
from vendors.permissions import IsVendorUser


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsVendorUser])
def vendor_map_tiles(request, z, x, y):  # noqa: ARG001 - framework signature must stay fixed
    return map_tile_response(z, x, y)
