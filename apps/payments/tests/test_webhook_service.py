# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""handle_webhook_event: event-id idempotency, out-of-order safety, orphan release."""

import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory
from apps.payments import services, tasks
from apps.payments.models import Payment, ProcessedWebhookEvent
from apps.payments.providers.base import WebhookEvent, WebhookType

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status


class _RecordingProvider:
    """Provider that records release calls instead of touching a real PSP."""

    def __init__(self):
        self.released: list[tuple[str, str]] = []

    def release(self, *, provider_id, idempotency_key):
        self.released.append((provider_id, idempotency_key))
        return None


def _payment(*, booking_status=BS.PENDING, payment_status=PS.CREATED, provider_id="mock-hold-1"):
    booking = BookingFactory(status=booking_status, price=Decimal("1500.00"))
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id=provider_id,
        amount=booking.price,
        status=payment_status,
    )
    return booking, payment


def _event(event_type, *, provider_id="mock-hold-1", event_id="evt-1", amount=None):
    return WebhookEvent(
        event_id=event_id, type=str(event_type), provider_id=provider_id, amount=amount
    )


def test_hold_succeeded_confirms_pending_booking():
    booking, payment = _payment()

    services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED))

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PS.HELD
    assert booking.status == BS.CONFIRMED
    # The event was anchored exactly once for idempotency.
    assert ProcessedWebhookEvent.objects.filter(event_id="evt-1").count() == 1


def test_duplicate_event_id_is_ignored():
    booking, payment = _payment()

    services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, event_id="evt-dup"))
    services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, event_id="evt-dup"))

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    # No second edge was walked and no second anchor written.
    assert payment.transitions.count() == 1
    assert ProcessedWebhookEvent.objects.filter(event_id="evt-dup").count() == 1


@pytest.mark.parametrize("cancelled", [BS.CANCELLED_BY_STUDENT, BS.CANCELLED_BY_TUTOR])
def test_late_hold_succeeded_on_cancelled_booking_releases_the_hold(
    monkeypatch, django_capture_on_commit_callbacks, cancelled
):
    # The webhook confirms a hold, but the booking is already gone (timed out or
    # cancelled). The hold backs nothing, so the payment is failed and the money
    # freed at the PSP — never left held on a dead booking.
    booking, payment = _payment(booking_status=cancelled)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED))

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PS.FAILED
    assert booking.status == cancelled  # the booking is not resurrected
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_hold_failed_fails_a_created_payment():
    booking, payment = _payment()

    services.handle_webhook_event(_event(WebhookType.HOLD_FAILED))

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PS.FAILED
    # The booking is left pending for the timeout sweep to cancel; not torn down here.
    assert booking.status == BS.PENDING


def test_hold_failed_does_not_tear_down_a_confirmed_hold():
    # Out-of-order: a stray failure arrives after the hold already confirmed. A
    # held payment (backing a confirmed booking) must not be reverted through the
    # otherwise legal held → failed edge.
    booking, payment = _payment(booking_status=BS.CONFIRMED, payment_status=PS.HELD)

    services.handle_webhook_event(_event(WebhookType.HOLD_FAILED))

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert not payment.transitions.exists()


def test_hold_failed_on_a_terminal_payment_is_a_clean_noop():
    # A failure for a payment that already settled must not raise (which would 500
    # the endpoint and roll back the idempotency anchor) — it is recorded and inert.
    booking, payment = _payment(booking_status=BS.COMPLETED)
    Payment.objects.filter(pk=payment.pk).update(status=PS.CAPTURED, captured_amount=payment.amount)

    services.handle_webhook_event(_event(WebhookType.HOLD_FAILED))

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert not payment.transitions.exists()
    assert ProcessedWebhookEvent.objects.filter(event_id="evt-1").count() == 1


def test_late_hold_succeeded_on_a_held_orphan_releases_the_hold(
    monkeypatch, django_capture_on_commit_callbacks
):
    # Defense in depth: a confirmed booking was cancelled without releasing its hold
    # (a gap the orchestrator later closes), then a fresh hold_succeeded arrives. The
    # money is really authorized, so the held payment is voided and released — never
    # left held on a dead booking.
    booking, payment = _payment(booking_status=BS.CANCELLED_BY_TUTOR, payment_status=PS.HELD)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, event_id="evt-fresh"))

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_late_hold_succeeded_on_a_completed_booking_keeps_the_hold_for_capture(
    monkeypatch, django_capture_on_commit_callbacks
):
    # The money-critical regression: a lesson auto-completed and its hold is still
    # held, awaiting the async capture, when a late/duplicate hold_succeeded arrives.
    # The hold must stay held (owed to capture) — never failed and released, which
    # would refund the student a delivered lesson and leave the tutor unpaid.
    booking, payment = _payment(booking_status=BS.COMPLETED, payment_status=PS.HELD)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, event_id="evt-late"))

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert not payment.transitions.exists()
    assert provider.released == []  # the hold is not voided


def test_release_task_delegates_to_the_service(monkeypatch):
    _, payment = _payment(payment_status=PS.FAILED)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    # CELERY_TASK_ALWAYS_EAGER runs the task inline.
    tasks.release_hold.delay(payment.id)

    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_out_of_order_succeeded_after_failed_releases_the_hold(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A hold_failed was processed first (payment failed) and then a later
    # hold_succeeded reports the authorization actually went through. The money may
    # really be held, so it is released defensively; the dead payment is not revived.
    booking, payment = _payment(payment_status=PS.FAILED)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, event_id="evt-late"))

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert booking.status == BS.PENDING
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_unknown_provider_id_is_ignored_and_not_recorded():
    # A callback for a hold we do not know (foreign, or one that raced our own
    # persistence of the id) takes no action and is not anchored, so a genuine
    # later redelivery can still be picked up.
    _payment(provider_id="mock-known")

    services.handle_webhook_event(_event(WebhookType.HOLD_SUCCEEDED, provider_id="mock-stranger"))

    assert not ProcessedWebhookEvent.objects.exists()


def test_unhandled_event_type_is_recorded_but_changes_nothing():
    # Capture/refund echoes are server-authoritative on the call's return; the
    # webhook is anchored for dedup but drives no domain change yet.
    booking, payment = _payment(booking_status=BS.CONFIRMED, payment_status=PS.HELD)

    services.handle_webhook_event(_event(WebhookType.CAPTURE_SUCCEEDED, amount=Decimal("1500.00")))

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert not payment.transitions.exists()
    recorded = ProcessedWebhookEvent.objects.filter(event_type=WebhookType.CAPTURE_SUCCEEDED)
    assert recorded.count() == 1


def test_request_release_is_a_noop_for_a_non_failed_payment(monkeypatch):
    _, payment = _payment(booking_status=BS.CONFIRMED, payment_status=PS.HELD)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_release(payment.id)

    assert provider.released == []


def test_request_release_calls_the_provider_for_a_failed_payment(monkeypatch):
    _, payment = _payment(payment_status=PS.FAILED)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_release(payment.id)

    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_succeeded_and_failed_never_confirm_a_failed_payment():
    # A hold_succeeded and a hold_failed for the same payment race under distinct
    # event ids. Both take the payment's row lock (hold_succeeded via confirm_hold,
    # locking booking-first), so they serialize: the booking must never end up
    # confirmed while its payment is failed, nor held while the booking is not
    # confirmed. PostgreSQL-only: SQLite ignores select_for_update, so no real lock.
    booking, payment = _payment()
    barrier = threading.Barrier(2)

    def deliver(event_type, event_id):
        try:
            barrier.wait(timeout=10)
            services.handle_webhook_event(_event(event_type, event_id=event_id))
        finally:
            connection.close()

    threads = [
        threading.Thread(target=deliver, args=(WebhookType.HOLD_SUCCEEDED, "evt-ok")),
        threading.Thread(target=deliver, args=(WebhookType.HOLD_FAILED, "evt-no")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    payment.refresh_from_db()
    if booking.status == BS.CONFIRMED:
        # Success won the payment lock: the hold is held; the failure was inert.
        assert payment.status == PS.HELD
    else:
        # Failure won: the payment is failed and the booking is left pending.
        assert booking.status == BS.PENDING
        assert payment.status == PS.FAILED
