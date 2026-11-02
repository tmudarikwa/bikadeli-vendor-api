from rest_framework import permissions


class IsVendorUser(permissions.BasePermission):
    def has_permission(self, request, view):  # noqa: ARG002 - required by DRF permission/filter API
        return request.user.is_authenticated and request.user.is_vendor()
