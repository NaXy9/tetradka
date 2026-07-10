# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Query-param filters for a user's own bookings list."""

import django_filters

from .models import Booking


class BookingFilter(django_filters.FilterSet):
    """Filters of GET /bookings: ?role=student|tutor&status=<status>.

    `role` is not a model field: it narrows the already user-scoped queryset to
    the bookings where the caller is the student or the tutor. An unknown value
    for either filter fails form validation and the backend returns 400.
    """

    role = django_filters.ChoiceFilter(
        choices=[("student", "student"), ("tutor", "tutor")],
        method="filter_role",
    )
    status = django_filters.ChoiceFilter(choices=Booking.Status.choices)

    class Meta:
        model = Booking
        fields = ["role", "status"]

    def filter_role(self, queryset, name, value):
        user = self.request.user
        if value == "student":
            return queryset.filter(student=user)
        return queryset.filter(tutor__user=user)
