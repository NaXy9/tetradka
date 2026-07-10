# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking API: create a booking and list the caller's own bookings."""

from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.response import Response

from .filters import BookingFilter
from .models import Booking
from .serializers import BookingCreateSerializer, BookingSerializer
from .services import SlotUnavailableError, create_booking


class SlotConflict(APIException):
    """409 for a slot that cannot be booked: taken, outside availability, or too soon.

    create_booking makes the authoritative decision under a row lock, so every
    one of its rejections surfaces here as a conflict with the tutor's current
    state rather than being re-validated (and duplicated) in the serializer.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The requested slot cannot be booked."
    default_code = "slot_conflict"


class BookingListCreateView(generics.ListCreateAPIView):
    """GET /bookings — the caller's bookings; POST /bookings — create one."""

    filter_backends = [DjangoFilterBackend]
    filterset_class = BookingFilter

    def get_queryset(self):
        # A user sees only bookings they are party to, on either side; ?role=
        # narrows this further. select_related feeds the read serializer's
        # nested party/subject fields without an N+1.
        user = self.request.user
        return (
            Booking.objects.filter(Q(student=user) | Q(tutor__user=user))
            .select_related("tutor__user", "subject", "student")
            .distinct()
        )

    def get_serializer_class(self):
        return BookingCreateSerializer if self.request.method == "POST" else BookingSerializer

    @extend_schema(request=BookingCreateSerializer, responses={201: BookingSerializer})
    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            booking = create_booking(
                student=request.user,
                tutor=data["tutor"],
                subject=data["subject"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
            )
        except SlotUnavailableError as exc:
            raise SlotConflict(str(exc)) from exc
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)
