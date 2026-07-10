# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking API: bookings for either party plus the tutor's own availability CRUD."""

from django.db import transaction
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.response import Response

from apps.catalog.models import TutorProfile
from apps.common.permissions import IsTutor

from .filters import BookingFilter
from .models import AvailabilityException, AvailabilityRule, Booking
from .serializers import (
    AvailabilityExceptionSerializer,
    AvailabilityRuleSerializer,
    BookingCancelRequestSerializer,
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingSerializer,
)
from .services import (
    BookingNotCancellableError,
    SlotUnavailableError,
    cancel_booking,
    create_booking,
)


class SlotConflict(APIException):
    """409 for a slot that cannot be booked: taken, outside availability, or too soon.

    create_booking makes the authoritative decision under a row lock, so every
    one of its rejections surfaces here as a conflict with the tutor's current
    state rather than being re-validated (and duplicated) in the serializer.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The requested slot cannot be booked."
    default_code = "slot_conflict"


class CancelConflict(APIException):
    """409 for a booking that cannot be cancelled from its current status."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The booking cannot be cancelled."
    default_code = "cancel_conflict"


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


class BookingCancelView(generics.GenericAPIView):
    """POST /bookings/{id}/cancel — either party cancels; response carries the refund."""

    serializer_class = BookingCancelSerializer

    def get_queryset(self):
        # Scoped to the caller's own bookings so a stranger's id is a 404, not a
        # 403 that would leak its existence — matching the list endpoint's scope.
        user = self.request.user
        return Booking.objects.filter(Q(student=user) | Q(tutor__user=user)).select_related(
            "tutor__user", "subject", "student"
        )

    @extend_schema(request=BookingCancelRequestSerializer, responses={200: BookingCancelSerializer})
    def post(self, request: Request, *args, **kwargs) -> Response:
        booking = self.get_object()
        request_serializer = BookingCancelRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        try:
            refund = cancel_booking(
                booking=booking,
                actor=request.user,
                reason=request_serializer.validated_data["reason"],
            )
        except BookingNotCancellableError as exc:
            raise CancelConflict(str(exc)) from exc
        booking.refund_amount = refund
        return Response(self.get_serializer(booking).data)


class _OwnAvailabilityViewSet(viewsets.ModelViewSet):
    """Shared base for a tutor's self-service availability collections.

    Every action is scoped to the caller's own profile, so another tutor's row
    id is a 404 (never a 403 that would confirm it exists), and `tutor` is set
    from the request user on create rather than trusted from the body. The
    weekly-schedule editor loads the whole collection at once, hence no
    pagination.
    """

    permission_classes = [IsTutor]
    pagination_class = None

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["tutor"] = self.request.user.tutor_profile
        return context

    def create(self, request: Request, *args, **kwargs) -> Response:
        with transaction.atomic():
            self._lock_own_profile()
            return super().create(request, *args, **kwargs)

    def update(self, request: Request, *args, **kwargs) -> Response:
        with transaction.atomic():
            self._lock_own_profile()
            return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(tutor=self.request.user.tutor_profile)

    def _lock_own_profile(self) -> None:
        """Serialize a tutor's concurrent availability writes on their profile row.

        Availability has no DB range/exclusion constraint, so two concurrent
        creates could each pass the serializer's overlap/duplicate-date check
        against stale state and both commit. Locking the profile row makes the
        check-then-write see committed state, the same guarantee create_booking
        gets from its select_for_update. A no-op on SQLite; the real
        serialization point on PostgreSQL.
        """
        TutorProfile.objects.select_for_update().get(pk=self.request.user.tutor_profile.pk)


class AvailabilityRuleViewSet(_OwnAvailabilityViewSet):
    """CRUD under /tutor/availability/rules for recurring weekly windows."""

    serializer_class = AvailabilityRuleSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no user
            return AvailabilityRule.objects.none()
        return AvailabilityRule.objects.filter(tutor=self.request.user.tutor_profile)


class AvailabilityExceptionViewSet(_OwnAvailabilityViewSet):
    """CRUD under /tutor/availability/exceptions for one-off date overrides."""

    serializer_class = AvailabilityExceptionSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no user
            return AvailabilityException.objects.none()
        return AvailabilityException.objects.filter(tutor=self.request.user.tutor_profile)
