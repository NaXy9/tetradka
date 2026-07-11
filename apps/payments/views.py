# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment API: the student opens payment for a booking (the hold flow)."""

from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.response import Response

from apps.bookings.models import Booking

from .serializers import PaymentSerializer
from .services import BookingNotPayableError, initiate_payment


class PayConflict(APIException):
    """409 for a booking that cannot be paid from its current status (not pending)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The booking cannot be paid."
    default_code = "pay_conflict"


class BookingPayView(generics.GenericAPIView):
    """POST /bookings/{id}/pay — the student opens payment for their pending booking.

    Returns 202: the hold is authorized asynchronously by the provider, so the
    payment starts as ``created`` and only becomes ``held`` (confirming the
    booking) once the provider's webhook lands.
    """

    serializer_class = PaymentSerializer

    def get_queryset(self):
        # Scoped to the caller's own bookings *as the student* — the student is the
        # payer, so a tutor's or a stranger's id is a 404, never a 403 that would
        # confirm the booking exists.
        return Booking.objects.filter(student=self.request.user)

    @extend_schema(request=None, responses={202: PaymentSerializer})
    def post(self, request: Request, *args, **kwargs) -> Response:
        booking = self.get_object()
        try:
            payment = initiate_payment(booking=booking, actor=request.user)
        except BookingNotPayableError as exc:
            raise PayConflict(str(exc)) from exc
        return Response(PaymentSerializer(payment).data, status=status.HTTP_202_ACCEPTED)
