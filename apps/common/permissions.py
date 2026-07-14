"""Shared DRF permissions."""

from rest_framework.permissions import BasePermission


class IsTutor(BasePermission):
    """Allow only users who own a tutor profile.

    A denied anonymous request still surfaces as 401 (not 403): DRF raises
    NotAuthenticated when an authenticator is configured but none succeeded.
    The reverse one-to-one is cached on the user instance on first access, so
    the hasattr check costs at most one query per request.
    """

    message = "Only tutors may access this resource."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and hasattr(user, "tutor_profile"))
