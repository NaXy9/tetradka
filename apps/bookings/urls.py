# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking routes, mounted under /api/v1/."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    AvailabilityExceptionViewSet,
    AvailabilityRuleViewSet,
    BookingCancelView,
    BookingListCreateView,
)

# No trailing slashes — matches the URL style of the rest of the API.
router = SimpleRouter(trailing_slash=False)
router.register("tutor/availability/rules", AvailabilityRuleViewSet, basename="availability_rule")
router.register(
    "tutor/availability/exceptions",
    AvailabilityExceptionViewSet,
    basename="availability_exception",
)

urlpatterns = [
    path("bookings", BookingListCreateView.as_view(), name="booking_list_create"),
    path("bookings/<int:pk>/cancel", BookingCancelView.as_view(), name="booking_cancel"),
    *router.urls,
]
