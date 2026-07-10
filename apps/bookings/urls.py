# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking routes, mounted under /api/v1/."""

from django.urls import path

from .views import BookingCancelView, BookingListCreateView

urlpatterns = [
    path("bookings", BookingListCreateView.as_view(), name="booking_list_create"),
    path("bookings/<int:pk>/cancel", BookingCancelView.as_view(), name="booking_cancel"),
]
