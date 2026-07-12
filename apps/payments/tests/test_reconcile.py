# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""reconcile_booking_payment: a cancelled/timed-out booking settles its hold.

Covers the single reconciliation point wired into cancel_booking and the pending
timeout sweep: a full refund releases a held hold, an unconfirmed hold is failed and
freed, and a late partial cancellation is deferred to the capture flow.
"""

import datetime as dt
import threading
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import (
    PENDING_PAYMENT_TIMEOUT,
    BookingNotCancellableError,
    cancel_booking,
    expire_pending_bookings,
)
from apps.bookings.tests.factories import BookingFactory, TutorProfileFactory, UserFactory
from apps.payments import services
from apps.payments.models import Payment
from apps.payments.providers.base import HoldResult

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


def _confirmed_with_held_payment(*, price="1500.00", refund_percent=0, hours_ahead=48):
    """A confirmed booking backed by a held payment, party-scoped and priced."""
    tutor = TutorProfileFactory(late_cancellation_refund_percent=refund_percent)
    student = UserFactory()
    starts_at = timezone.now() + dt.timedelta(hours=hours_ahead)
    booking = BookingFactory(
        student=student,
        tutor=tutor,
        status=BS.CONFIRMED,
        starts_at=starts_at,
        ends_at=starts_at + dt.timedelta(hours=1),
        price=Decimal(price),
    )
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id="mock-hold-1",
        amount=booking.price,
        status=PS.HELD,
    )
    return booking, payment


def _pending_with_created_payment(*, price="1500.00", provider_id="mock-hold-1"):
    booking = BookingFactory(status=BS.PENDING, price=Decimal(price))
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id=provider_id,
        amount=booking.price,
        status=PS.CREATED,
    )
    return booking, payment


# --- Full-refund cancellations release the held hold --------------------------


def test_student_cancel_over_24h_releases_the_held_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _confirmed_with_held_payment(hours_ahead=48, refund_percent=0)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert booking.status == BS.CANCELLED_BY_STUDENT
    # Full refund: the hold is released, not captured — no money settles.
    assert payment.status == PS.REFUNDED
    assert payment.captured_amount == Decimal("0")
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_tutor_cancel_releases_the_held_payment(monkeypatch, django_capture_on_commit_callbacks):
    # A tutor cancellation always refunds in full, even inside the 24h window.
    booking, payment = _confirmed_with_held_payment(hours_ahead=2, refund_percent=0)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.tutor.user)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert booking.status == BS.CANCELLED_BY_TUTOR
    assert payment.status == PS.REFUNDED
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_late_cancel_with_full_policy_releases_the_held_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A 100% late-cancel policy makes even a <24h cancellation a full refund, so the
    # hold is released rather than partially captured.
    booking, payment = _confirmed_with_held_payment(hours_ahead=1, refund_percent=100)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert payment.status == PS.REFUNDED
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


# --- A late partial cancellation is deferred to the capture flow ---------------


def test_late_partial_cancel_leaves_the_hold_for_the_capture_flow(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A <24h student cancellation with a partial policy must capture the retained part
    # and pay the tutor — the capture-and-credit machinery that ships with lesson
    # completion. Until then the hold is deliberately left in place (never released in
    # full, which would over-refund the student). This pins that boundary so the
    # follow-up capture increment has to change it.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=50
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("750.00")  # advisory amount is still computed
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status == PS.HELD  # untouched: no partial capture yet
    assert not payment.transitions.exists()
    assert provider.released == []


# --- An unconfirmed (created) hold is failed and freed ------------------------


def test_cancel_pending_fails_and_releases_the_created_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _pending_with_created_payment(provider_id="mock-hold-2")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status == PS.FAILED
    assert provider.released == [("mock-hold-2", f"release-{payment.id}")]


def test_cancel_pending_created_without_a_hold_id_releases_nothing(
    monkeypatch, django_capture_on_commit_callbacks
):
    # The hold has not been opened at the PSP yet (no provider id), so there is
    # nothing to void — the payment is simply failed.
    booking, payment = _pending_with_created_payment(provider_id="")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert provider.released == []


# --- The timeout sweep reconciles too -----------------------------------------


def test_timeout_fails_and_releases_the_created_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _pending_with_created_payment(provider_id="mock-hold-3")
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        assert expire_pending_bookings() == 1

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status == PS.FAILED
    assert provider.released == [("mock-hold-3", f"release-{payment.id}")]


def test_timeout_without_a_payment_still_cancels():
    # The pre-payment path: a stale pending booking with no payment reconciles to a
    # clean no-op and is still cancelled.
    booking = BookingFactory(status=BS.PENDING)
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )

    assert expire_pending_bookings() == 1
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT


# --- Idempotency and terminal payments ----------------------------------------


def test_reconcile_is_a_noop_on_a_terminal_payment():
    # A payment that already settled (captured/refunded/failed) is left alone: only
    # created/held holds are reconciled.
    booking, payment = _confirmed_with_held_payment()
    Payment.objects.filter(pk=payment.pk).update(status=PS.FAILED)

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert not payment.transitions.exists()


def test_reconcile_without_a_payment_is_a_noop():
    booking = BookingFactory(status=BS.CANCELLED_BY_STUDENT)

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    assert not Payment.objects.filter(booking=booking).exists()


def test_reconcile_a_second_time_releases_nothing_more(
    monkeypatch, django_capture_on_commit_callbacks
):
    # After a full-refund cancel released the hold, a redelivered reconcile (the
    # payment now refunded) must not walk another edge or fire a second release.
    booking, payment = _confirmed_with_held_payment(hours_ahead=48)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    payment.refresh_from_db()
    assert payment.status == PS.REFUNDED
    assert payment.transitions.count() == 1  # only the original held→refunded edge
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]  # no second release


# --- request_release / request_hold edges -------------------------------------


def test_request_release_releases_a_refunded_payment(monkeypatch):
    # A full release marks the payment refunded; the release task must still void the
    # hold at the PSP for it (not only for failed payments).
    booking, payment = _confirmed_with_held_payment()
    Payment.objects.filter(pk=payment.pk).update(status=PS.REFUNDED)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_release(payment.id)

    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_request_hold_preserves_the_id_and_releases_when_failed_mid_flight(monkeypatch):
    # The race: the hold is opened at the PSP while the booking is cancelled in the
    # same instant, failing the payment before the id is stored. The id must not be
    # lost (the CAS keys on an empty provider_id, not on the status), and the orphaned
    # hold is released immediately rather than stranded.
    booking, payment = _pending_with_created_payment(provider_id="")
    released: list[tuple[str, str]] = []

    class _RacingProvider:
        def create_hold(self, **kwargs):
            # Stand in for the booking being cancelled while the hold is opening.
            Payment.objects.filter(pk=payment.id).update(status=PS.FAILED)
            return HoldResult(provider_id="mock-raced")

        def release(self, *, provider_id, idempotency_key):
            released.append((provider_id, idempotency_key))

    monkeypatch.setattr(services, "get_payment_provider", lambda: _RacingProvider())

    services.request_hold(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id == "mock-raced"  # id recorded despite the concurrent fail
    assert payment.status == PS.FAILED
    assert released == [("mock-raced", f"release-{payment.id}")]


# --- Concurrency --------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_cancel_and_confirm_hold_never_leave_an_incoherent_pair():
    # The money-critical race: a student cancels (full refund, >24h ahead) at the same
    # instant a hold_succeeded webhook confirms the booking. Both lock the booking row
    # first (cancel via transition_to, confirm_hold explicitly), so they fully
    # serialize and the pair stays coherent: the booking must never be confirmed with a
    # failed payment, nor cancelled with the hold still held/created — a full refund
    # always drives the payment to a released terminal state.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking, payment = _pending_with_created_payment()  # BookingFactory starts 48h ahead
    barrier = threading.Barrier(2)

    def cancel():
        try:
            barrier.wait(timeout=10)
            cancel_booking(booking=booking, actor=booking.student)
        except BookingNotCancellableError:
            pass  # only if the edge vanished; not expected on this pending→confirmed path
        finally:
            connection.close()

    def confirm():
        try:
            barrier.wait(timeout=10)
            services.confirm_hold(payment)
        except services.HoldNotConfirmable:
            pass  # cancel won the lock first; the orphan path handles the hold
        finally:
            connection.close()

    threads = [threading.Thread(target=cancel), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    payment.refresh_from_db()
    # Cancel always succeeds (pending→ and confirmed→cancelled_by_student are both legal
    # edges), so the booking ends cancelled either way; the payment is released, never
    # left backing a dead booking.
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status in (PS.REFUNDED, PS.FAILED)
