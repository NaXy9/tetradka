# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment API: the student opens payment (the hold flow); the PSP posts webhooks."""

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bookings.models import Booking

from .providers import PaymentProviderError, get_payment_provider
from .serializers import PaymentSerializer
from .services import BookingNotPayableError, handle_webhook_event, initiate_payment

# Header carrying the HMAC signature over the raw webhook body. When the real
# YooKassa backend lands, signature extraction may move into the provider so each
# PSP can name its own header; the mock and the abstraction sign the raw body.
WEBHOOK_SIGNATURE_HEADER = "X-Payment-Signature"


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


class WebhookRejected(APIException):
    """400 for a webhook that fails signature verification or is malformed."""

    status_code = status.HTTP_400_BAD_REQUEST
    # Deliberately generic: do not reveal whether the signature or the body was bad.
    default_detail = "Invalid webhook."
    default_code = "invalid_webhook"


class PaymentWebhookView(APIView):
    """POST /webhooks/payment — provider callbacks driving the hold lifecycle.

    Unauthenticated by design: the caller is the PSP, not a logged-in user. Trust
    comes from the signature over the raw request body, verified before the body is
    parsed or any action is taken. A verified, well-formed event always gets 200 —
    even a duplicate or one we do not act on — so the PSP stops redelivering;
    idempotency (by event id) and out-of-order handling live in the service layer.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=None,
        responses={
            200: OpenApiResponse(description="Event accepted."),
            400: OpenApiResponse(description="Bad signature or malformed body."),
        },
    )
    def post(self, request: Request) -> Response:
        provider = get_payment_provider()
        signature = request.headers.get(WEBHOOK_SIGNATURE_HEADER, "")
        # Raw bytes, not request.data: the signature covers the exact body the PSP
        # sent, and re-serializing parsed JSON would not reproduce it byte-for-byte.
        if not provider.verify_signature(body=request.body, signature=signature):
            raise WebhookRejected()
        try:
            event = provider.parse_webhook(request.body)
        except PaymentProviderError as exc:
            raise WebhookRejected() from exc
        handle_webhook_event(event)
        return Response(status=status.HTTP_200_OK)
