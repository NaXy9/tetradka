# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""POST /webhooks/payment — signature gate, malformed-body rejection, HTTP idempotency."""

import hashlib
import hmac
from decimal import Decimal

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory
from apps.payments.models import Payment, ProcessedWebhookEvent
from apps.payments.providers import MockProvider, WebhookType

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status

WEBHOOK_URL = "/api/v1/webhooks/payment"


def _pending_payment(provider_id="mock-hold-1"):
    booking = BookingFactory(status=BS.PENDING, price=Decimal("1500.00"))
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id=provider_id,
        amount=booking.price,
    )
    return booking, payment


def _post(client, body, signature):
    # The signature travels in a header; the raw body is what it authenticates.
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_X_PAYMENT_SIGNATURE=signature,
    )


def _sign(body: bytes) -> str:
    return hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_valid_hold_succeeded_webhook_confirms_booking():
    booking, payment = _pending_payment()
    body, signature = MockProvider().build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id=payment.provider_id
    )

    # No credentials: the PSP is not a logged-in user; trust is the signature.
    response = _post(APIClient(), body, signature)

    assert response.status_code == 200
    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PS.HELD
    assert booking.status == BS.CONFIRMED


def test_missing_signature_is_rejected():
    _, payment = _pending_payment()
    body, _sig = MockProvider().build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id=payment.provider_id
    )

    response = _post(APIClient(), body, "")

    assert response.status_code == 400
    payment.refresh_from_db()
    assert payment.status == PS.CREATED  # nothing was applied
    assert not ProcessedWebhookEvent.objects.exists()


def test_bad_signature_is_rejected():
    _, payment = _pending_payment()
    body, _sig = MockProvider().build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id=payment.provider_id
    )

    response = _post(APIClient(), body, "deadbeef")

    assert response.status_code == 400
    payment.refresh_from_db()
    assert payment.status == PS.CREATED


def test_tampered_body_is_rejected():
    # A valid signature for the original body must not authenticate an altered one.
    _, payment = _pending_payment()
    body, signature = MockProvider().build_webhook(
        event_id="evt-1", type=WebhookType.HOLD_SUCCEEDED, provider_id=payment.provider_id
    )

    response = _post(APIClient(), body + b" ", signature)

    assert response.status_code == 400
    payment.refresh_from_db()
    assert payment.status == PS.CREATED


def test_malformed_but_signed_body_is_400():
    # Signature passes (signed with the real secret) but the body is not a valid
    # event: the parse failure is a clean 400, not a 500.
    body = b"not a json event"
    response = _post(APIClient(), body, _sign(body))

    assert response.status_code == 400
    assert not ProcessedWebhookEvent.objects.exists()


def test_duplicate_delivery_is_idempotent_over_http():
    booking, payment = _pending_payment()
    body, signature = MockProvider().build_webhook(
        event_id="evt-dup", type=WebhookType.HOLD_SUCCEEDED, provider_id=payment.provider_id
    )
    client = APIClient()

    first = _post(client, body, signature)
    second = _post(client, body, signature)  # the PSP redelivers the same event

    assert first.status_code == second.status_code == 200
    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert payment.transitions.count() == 1
    assert ProcessedWebhookEvent.objects.filter(event_id="evt-dup").count() == 1


def test_webhook_for_unknown_payment_returns_200_without_recording():
    body, signature = MockProvider().build_webhook(
        event_id="evt-x", type=WebhookType.HOLD_SUCCEEDED, provider_id="mock-nobody"
    )

    response = _post(APIClient(), body, signature)

    assert response.status_code == 200
    assert not ProcessedWebhookEvent.objects.exists()


def test_get_is_not_allowed():
    response = APIClient().get(WEBHOOK_URL)

    assert response.status_code == 405
