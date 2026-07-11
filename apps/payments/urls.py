# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment routes, mounted under /api/v1/."""

from django.urls import path

from .views import BookingPayView

urlpatterns = [
    # Booking-scoped, but owned by the payments app alongside the (future) webhook.
    path("bookings/<int:pk>/pay", BookingPayView.as_view(), name="booking_pay"),
]
