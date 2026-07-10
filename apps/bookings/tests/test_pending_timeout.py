"""Pending-payment timeout: unpaid bookings are auto-cancelled after 15 minutes."""

import datetime as dt
import threading

import pytest
from django.db import connection
from django.utils import timezone

from apps.bookings import tasks
from apps.bookings.models import Booking, InvalidStatusTransition
from apps.bookings.services import PENDING_PAYMENT_TIMEOUT, expire_pending_bookings

from .factories import BookingFactory

pytestmark = pytest.mark.django_db

S = Booking.Status


def test_stale_pending_is_cancelled_and_slot_freed():
    booking = BookingFactory(status=S.PENDING)
    # Look at the world from past the timeout so the just-created booking is stale.
    now = timezone.now() + PENDING_PAYMENT_TIMEOUT + dt.timedelta(minutes=1)

    cancelled = expire_pending_bookings(now=now)

    assert cancelled == 1
    booking.refresh_from_db()
    assert booking.status == S.CANCELLED_BY_STUDENT
    # A cancelled booking no longer blocks its slot (free_slots ignores it).
    assert not Booking.objects.filter(pk=booking.pk, status__in=(S.PENDING, S.CONFIRMED)).exists()


def test_cancellation_is_logged_as_system_action():
    booking = BookingFactory(status=S.PENDING)
    now = timezone.now() + PENDING_PAYMENT_TIMEOUT + dt.timedelta(minutes=1)

    expire_pending_bookings(now=now)

    log = booking.transitions.get()
    assert log.from_status == S.PENDING
    assert log.to_status == S.CANCELLED_BY_STUDENT
    assert log.actor is None  # no human actor: the sweep is the system
    assert log.reason == "pending payment timeout"


def test_fresh_pending_is_left_alone():
    booking = BookingFactory(status=S.PENDING)

    assert expire_pending_bookings() == 0

    booking.refresh_from_db()
    assert booking.status == S.PENDING


def test_exactly_at_timeout_is_not_yet_expired():
    booking = BookingFactory(status=S.PENDING)
    booking.refresh_from_db()  # read the DB-assigned created_at
    # cutoff is strict (created_at < now - timeout): a booking exactly at the
    # boundary has not yet timed out.
    now = booking.created_at + PENDING_PAYMENT_TIMEOUT

    assert expire_pending_bookings(now=now) == 0
    booking.refresh_from_db()
    assert booking.status == S.PENDING


def test_confirmed_booking_is_never_swept():
    # Even an old confirmed booking is off-limits: only pending times out.
    booking = BookingFactory(status=S.CONFIRMED)
    now = timezone.now() + PENDING_PAYMENT_TIMEOUT + dt.timedelta(hours=1)

    assert expire_pending_bookings(now=now) == 0
    booking.refresh_from_db()
    assert booking.status == S.CONFIRMED


def test_only_stale_pending_among_several():
    stale = BookingFactory(status=S.PENDING)
    fresh = BookingFactory(status=S.PENDING)
    confirmed = BookingFactory(status=S.CONFIRMED)
    # Age only the stale one past the window; keep the rest current.
    Booking.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )

    assert expire_pending_bookings() == 1

    stale.refresh_from_db()
    fresh.refresh_from_db()
    confirmed.refresh_from_db()
    assert stale.status == S.CANCELLED_BY_STUDENT
    assert fresh.status == S.PENDING
    assert confirmed.status == S.CONFIRMED


def test_task_delegates_to_service():
    stale = BookingFactory(status=S.PENDING)
    Booking.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )

    # CELERY_TASK_ALWAYS_EAGER runs the task inline; .delay returns its result.
    result = tasks.expire_pending_bookings.delay()

    assert result.get() == 1
    stale.refresh_from_db()
    assert stale.status == S.CANCELLED_BY_STUDENT


def test_second_sweep_is_a_noop():
    # A redelivered beat tick (or an overlapping run) must not cancel twice or add
    # a second audit row.
    booking = BookingFactory(status=S.PENDING)
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )

    assert expire_pending_bookings() == 1
    assert expire_pending_bookings() == 0
    assert booking.transitions.count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_sweep_never_cancels_a_booking_confirmed_mid_sweep():
    # The central guarantee of this increment: a payment that confirms a booking
    # at the same instant the timeout sweep runs must win. confirmed→
    # cancelled_by_student is a legal edge, so the status machine alone would let
    # the sweep cancel a just-paid lesson; only the under-lock pending re-check
    # prevents it. Whichever transaction grabs the row lock first wins, and both
    # orderings must leave the booking in a coherent state.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking = BookingFactory(status=S.PENDING)
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
            booking.transition_to(S.CONFIRMED, reason="payment held")
        except InvalidStatusTransition:
            pass  # sweep cancelled first; that ordering is valid too
        finally:
            connection.close()

    threads = [threading.Thread(target=sweep), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    to_statuses = list(booking.transitions.values_list("to_status", flat=True))
    if S.CONFIRMED in to_statuses:
        # Payment won the lock: the booking stays confirmed, never cancelled.
        assert booking.status == S.CONFIRMED
        assert S.CANCELLED_BY_STUDENT not in to_statuses
    else:
        # Sweep won the lock: the booking timed out and the confirm was rejected.
        assert booking.status == S.CANCELLED_BY_STUDENT
        assert to_statuses == [S.CANCELLED_BY_STUDENT]
