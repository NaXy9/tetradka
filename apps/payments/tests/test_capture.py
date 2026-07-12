"""capture_and_credit / request_capture: a completed lesson's hold settles and pays the tutor.

Covers the capture-and-credit machinery: the provider takes the money first, then the
payment flips to captured and the tutor's balance is credited in one transaction — never
optimistically, exactly once under a redelivered retry.
"""

import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory, TutorProfileFactory
from apps.catalog.models import TutorProfile
from apps.payments import services, tasks
from apps.payments.models import Payment
from apps.payments.providers.base import CaptureResult, PaymentProviderError

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status


class _RecordingProvider:
    """Provider that records capture calls and settles the full requested amount."""

    def __init__(self):
        self.captured: list[tuple[str, Decimal, str]] = []

    def capture(self, *, provider_id, amount, idempotency_key):
        self.captured.append((provider_id, amount, idempotency_key))
        return CaptureResult(provider_id=provider_id, captured_amount=Decimal(amount))


def _completed_with_held_payment(*, price="1500.00", provider_id="mock-hold-1", tutor=None):
    """A completed booking backed by a held payment ready to capture."""
    booking = BookingFactory(
        tutor=tutor or TutorProfileFactory(), status=BS.COMPLETED, price=Decimal(price)
    )
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id=provider_id,
        amount=booking.price,
        status=PS.HELD,
    )
    return booking, payment


# --- The happy path: capture the full price and credit the tutor --------------


def test_request_capture_captures_full_price_and_credits_tutor(monkeypatch):
    booking, payment = _completed_with_held_payment(price="1500.00")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert payment.captured_amount == Decimal("1500.00")
    assert payment.commission == Decimal("150.00")  # 10% of the captured amount
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("1350.00")  # price − commission
    assert provider.captured == [("mock-hold-1", Decimal("1500.00"), f"capture-{payment.id}")]
    edge = payment.transitions.get()
    assert (edge.from_status, edge.to_status) == (PS.HELD, PS.CAPTURED)
    assert edge.actor is None  # system settlement


def test_request_capture_is_idempotent(monkeypatch):
    # A retry after the money already settled must not capture or credit twice.
    booking, payment = _completed_with_held_payment()
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_capture(payment.id)
    services.request_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert payment.transitions.count() == 1
    # The second run sees a captured payment and returns before touching the PSP.
    assert provider.captured == [("mock-hold-1", Decimal("1500.00"), f"capture-{payment.id}")]
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("1350.00")  # credited once


# --- Guards: nothing is captured unless a completed lesson backs a held hold ---


def test_request_capture_skips_a_hold_whose_booking_is_not_completed(monkeypatch):
    # A held payment on a still-confirmed booking is left alone — only a delivered
    # (completed) lesson settles its hold.
    booking = BookingFactory(status=BS.CONFIRMED, price=Decimal("1500.00"))
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id="mock-hold-1",
        amount=booking.price,
        status=PS.HELD,
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert provider.captured == []


def test_request_capture_without_a_hold_id_is_a_noop(monkeypatch):
    # No provider id means the hold was never opened at the PSP; there is nothing to
    # capture even though the booking is completed.
    booking, payment = _completed_with_held_payment(provider_id="")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert provider.captured == []


def test_capture_and_credit_is_idempotent_when_already_captured():
    # The primitive itself no-ops on an already-captured payment, so the tutor is
    # never double-credited even if it is called directly a second time.
    booking, payment = _completed_with_held_payment()
    Payment.objects.filter(pk=payment.pk).update(
        status=PS.CAPTURED, captured_amount=Decimal("1500.00"), commission=Decimal("150.00")
    )
    payment.refresh_from_db()

    services.capture_and_credit(payment=payment, captured_amount=Decimal("1500.00"), actor=None)

    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("0")  # not credited a second time
    assert not payment.transitions.exists()


# --- A provider failure leaves the hold intact for the retry -------------------


def test_provider_failure_leaves_the_hold_untouched(monkeypatch):
    booking, payment = _completed_with_held_payment()

    class _FailingProvider:
        def capture(self, *, provider_id, amount, idempotency_key):
            raise PaymentProviderError("PSP unavailable")

    monkeypatch.setattr(services, "get_payment_provider", lambda: _FailingProvider())

    # The error propagates so the Celery task retries; nothing is captured or credited.
    with pytest.raises(PaymentProviderError):
        services.request_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert not payment.transitions.exists()
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("0")


def test_task_delegates_to_service(monkeypatch):
    booking, payment = _completed_with_held_payment()
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    # CELERY_TASK_ALWAYS_EAGER runs the task inline; .delay returns its result.
    tasks.capture_payment.delay(payment.id).get()

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert provider.captured == [("mock-hold-1", Decimal("1500.00"), f"capture-{payment.id}")]


# --- Concurrency: the tutor is credited exactly once --------------------------


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_captures_credit_the_tutor_exactly_once():
    # The money-critical race: two capture runs settle the same hold at the same
    # instant (a retry overlapping the original). Both lock the payment row inside
    # capture_and_credit, so they serialize — the first flips held→captured and
    # credits, the second sees captured and no-ops. The tutor is paid once, not twice.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking, payment = _completed_with_held_payment(price="1500.00")
    barrier = threading.Barrier(2)

    def settle():
        try:
            barrier.wait(timeout=10)
            services.capture_and_credit(
                payment=payment, captured_amount=Decimal("1500.00"), actor=None
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=settle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert payment.transitions.count() == 1  # a single capture edge
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("1350.00")  # credited once, not 2700
