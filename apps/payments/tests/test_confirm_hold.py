# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""confirm_hold: a confirmed hold marks the payment held and confirms its booking."""

import datetime as dt
import threading
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.bookings.models import Booking
from apps.bookings.services import PENDING_PAYMENT_TIMEOUT, expire_pending_bookings
from apps.bookings.tests.factories import BookingFactory, UserFactory
from apps.payments.models import Payment
from apps.payments.services import HoldNotConfirmable, confirm_hold

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status


def _pending_with_created_payment(**booking_kwargs):
    booking = BookingFactory(status=BS.PENDING, price=Decimal("1500.00"), **booking_kwargs)
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id="mock-abc",
        amount=booking.price,
    )
    return booking, payment


def test_confirm_hold_holds_payment_and_confirms_booking():
    booking, payment = _pending_with_created_payment()

    confirm_hold(payment)

    payment.refresh_from_db()
    booking.refresh_from_db()
    # The linkage invariant: a confirmed booking is backed by a held payment.
    assert payment.status == PS.HELD
    assert booking.status == BS.CONFIRMED
    # Holding is not capturing: no money moves and no commission is taken yet.
    assert payment.captured_amount == Decimal("0")
    assert payment.commission == Decimal("0")


def test_confirm_hold_writes_audit_logs_for_both():
    actor = UserFactory()
    booking, payment = _pending_with_created_payment()

    confirm_hold(payment, actor=actor)

    payment_log = payment.transitions.get()
    assert (payment_log.from_status, payment_log.to_status) == (PS.CREATED, PS.HELD)
    assert payment_log.actor == actor
    booking_log = booking.transitions.get()
    assert (booking_log.from_status, booking_log.to_status) == (BS.PENDING, BS.CONFIRMED)
    assert booking_log.actor == actor


def test_confirm_hold_is_idempotent_on_redelivery():
    booking, payment = _pending_with_created_payment()

    confirm_hold(payment)
    confirm_hold(payment)  # a duplicate webhook delivery

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert payment.status == PS.HELD
    assert booking.status == BS.CONFIRMED
    # No second edge was walked on either side.
    assert payment.transitions.count() == 1
    assert booking.transitions.count() == 1


def test_confirm_hold_is_a_noop_for_a_completed_booking():
    # After a lesson auto-completes its hold is still held, awaiting the async capture.
    # A late or duplicate hold_succeeded landing in that window must leave the hold
    # alone: it is owed to the capture flow, never released as an orphan (which would
    # refund the student a delivered lesson and never pay the tutor).
    booking, payment = _pending_with_created_payment()
    Booking.objects.filter(pk=booking.pk).update(status=BS.COMPLETED)
    Payment.objects.filter(pk=payment.pk).update(status=PS.HELD)
    payment.refresh_from_db()

    confirm_hold(payment)  # must not raise

    payment.refresh_from_db()
    assert payment.status == PS.HELD  # untouched, ready for capture
    assert not payment.transitions.exists()


@pytest.mark.parametrize("booking_status", [BS.CANCELLED_BY_STUDENT, BS.CONFIRMED])
def test_confirm_hold_rejects_a_non_pending_booking(booking_status):
    # A hold that confirms after the booking already left pending (timed out, or
    # was confirmed by another attempt) cannot force it back: the payment stays
    # created so nothing is half-applied, and the caller releases the orphan hold.
    booking, payment = _pending_with_created_payment()
    Booking.objects.filter(pk=booking.pk).update(status=booking_status)

    with pytest.raises(HoldNotConfirmable):
        confirm_hold(payment)

    payment.refresh_from_db()
    assert payment.status == PS.CREATED
    assert not payment.transitions.exists()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_confirm_hold_and_timeout_sweep_never_both_apply():
    # A hold confirms at the same instant the pending-timeout sweep runs. Both take
    # the booking's row lock (confirm_hold locks booking-first, like the sweep), so
    # whichever grabs it first wins and the loser is rejected. The booking must never
    # end up both confirmed and cancelled, and the payment must end in a state coherent
    # with the booking: held under a confirmed booking, failed under a swept one (the
    # sweep now reconciles the unconfirmed hold, not just the booking).
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking, payment = _pending_with_created_payment()
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )
    barrier = threading.Barrier(2)

    def sweep():
        try:
            barrier.wait(timeout=10)
            expire_pending_bookings()
        finally:
            connection.close()

    def confirm():
        try:
            barrier.wait(timeout=10)
            confirm_hold(payment)
        except HoldNotConfirmable:
            pass  # sweep cancelled first; that ordering is valid too
        finally:
            connection.close()

    threads = [threading.Thread(target=sweep), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    payment.refresh_from_db()
    if booking.status == BS.CONFIRMED:
        # Confirm won the lock: the hold is held and the sweep left it alone.
        assert payment.status == PS.HELD
    else:
        # Sweep won the lock: the booking timed out and its unconfirmed hold was
        # reconciled to failed in the same transaction.
        assert booking.status == BS.CANCELLED_BY_STUDENT
        assert payment.status == PS.FAILED
