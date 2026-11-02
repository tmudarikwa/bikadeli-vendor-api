from rest_framework.permissions import BasePermission


class IsVendorOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.vendor.owner_user_id == request.user.id
