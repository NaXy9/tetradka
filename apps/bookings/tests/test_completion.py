"""Auto-completion: confirmed lessons past their end are completed and their holds captured."""

import datetime as dt
import threading
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.bookings import tasks
from apps.bookings.models import Booking, InvalidStatusTransition
from apps.bookings.services import COMPLETION_GRACE, complete_confirmed_bookings
from apps.payments import services as payment_services
from apps.payments.models import Payment
from apps.payments.providers.base import CaptureResult

from .factories import BookingFactory, TutorProfileFactory

pytestmark = pytest.mark.django_db

S = Booking.Status
PS = Payment.Status


def _finished_confirmed(*, price="1500.00", past=dt.timedelta(minutes=1), tutor=None):
    """A confirmed booking whose end is `past` beyond the completion grace."""
    ends_at = timezone.now() - COMPLETION_GRACE - past
    return BookingFactory(
        tutor=tutor or TutorProfileFactory(),
        status=S.CONFIRMED,
        starts_at=ends_at - dt.timedelta(hours=1),
        ends_at=ends_at,
        price=Decimal(price),
    )


# --- A finished confirmed lesson completes ------------------------------------


def test_finished_confirmed_is_completed():
    booking = _finished_confirmed()

    assert complete_confirmed_bookings() == 1

    booking.refresh_from_db()
    assert booking.status == S.COMPLETED


def test_completion_is_logged_as_system_action():
    booking = _finished_confirmed()

    complete_confirmed_bookings()

    log = booking.transitions.get()
    assert (log.from_status, log.to_status) == (S.CONFIRMED, S.COMPLETED)
    assert log.actor is None  # no human actor: the sweep is the system
    assert log.reason == "lesson auto-completed"


# --- The grace window is respected --------------------------------------------


def test_lesson_within_grace_is_left_alone():
    # Ended a minute ago but still inside the 15-minute grace: not yet due.
    ends_at = timezone.now() - dt.timedelta(minutes=1)
    booking = BookingFactory(
        status=S.CONFIRMED, starts_at=ends_at - dt.timedelta(hours=1), ends_at=ends_at
    )

    assert complete_confirmed_bookings() == 0

    booking.refresh_from_db()
    assert booking.status == S.CONFIRMED


def test_exactly_at_grace_boundary_is_not_yet_completed():
    booking = _finished_confirmed()
    booking.refresh_from_db()  # read the DB-stored ends_at
    # cutoff is strict (ends_at < now - grace): a lesson exactly at the boundary has
    # not yet elapsed its grace.
    now = booking.ends_at + COMPLETION_GRACE

    assert complete_confirmed_bookings(now=now) == 0

    booking.refresh_from_db()
    assert booking.status == S.CONFIRMED


def test_pending_is_never_completed():
    # Only confirmed lessons complete; an old pending booking is the timeout sweep's job.
    ends_at = timezone.now() - COMPLETION_GRACE - dt.timedelta(hours=1)
    booking = BookingFactory(
        status=S.PENDING, starts_at=ends_at - dt.timedelta(hours=1), ends_at=ends_at
    )

    assert complete_confirmed_bookings() == 0

    booking.refresh_from_db()
    assert booking.status == S.PENDING


# --- Completion captures the held payment -------------------------------------


def test_completion_captures_the_held_payment(monkeypatch, django_capture_on_commit_callbacks):
    tutor = TutorProfileFactory()
    booking = _finished_confirmed(price="1500.00", tutor=tutor)
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id="mock-hold-1",
        amount=booking.price,
        status=PS.HELD,
    )

    class _RecordingProvider:
        def __init__(self):
            self.captured: list = []

        def capture(self, *, provider_id, amount, idempotency_key):
            self.captured.append((provider_id, amount, idempotency_key))
            return CaptureResult(provider_id=provider_id, captured_amount=Decimal(amount))

    provider = _RecordingProvider()
    monkeypatch.setattr(payment_services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        assert complete_confirmed_bookings() == 1

    booking.refresh_from_db()
    payment.refresh_from_db()
    tutor.refresh_from_db()
    assert booking.status == S.COMPLETED
    assert payment.status == PS.CAPTURED
    assert payment.captured_amount == Decimal("1500.00")
    assert tutor.balance == Decimal("1350.00")  # price − 10% commission
    assert provider.captured == [("mock-hold-1", Decimal("1500.00"), f"capture-{payment.id}")]


def test_completion_without_a_payment_still_completes():
    # The pre-payment path: a confirmed booking with no payment completes cleanly and
    # captures nothing.
    booking = _finished_confirmed()

    assert complete_confirmed_bookings() == 1

    booking.refresh_from_db()
    assert booking.status == S.COMPLETED
    assert not Payment.objects.filter(booking=booking).exists()


def test_second_sweep_is_a_noop():
    # A redelivered beat tick (or an overlapping run) must not complete twice or add a
    # second audit row.
    booking = _finished_confirmed()

    assert complete_confirmed_bookings() == 1
    assert complete_confirmed_bookings() == 0
    assert booking.transitions.count() == 1


def test_task_delegates_to_service():
    booking = _finished_confirmed()

    # CELERY_TASK_ALWAYS_EAGER runs the task inline; .delay returns its result.
    assert tasks.complete_confirmed_bookings.delay().get() == 1

    booking.refresh_from_db()
    assert booking.status == S.COMPLETED


# --- Concurrency: a cancellation recorded first wins --------------------------


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_sweep_never_completes_a_booking_cancelled_mid_sweep():
    # A tutor cancels at the same instant the completion sweep runs. Both lock the
    # booking row (cancel via transition_to, the sweep explicitly), so they serialize
    # and the outcome stays coherent: whichever grabs the lock first wins, and a
    # booking cancelled first is never force-completed.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking = _finished_confirmed()
    barrier = threading.Barrier(2)

    def sweep():
        try:
            barrier.wait(timeout=10)
            complete_confirmed_bookings()
        finally:
            connection.close()

    def cancel():
        try:
            barrier.wait(timeout=10)
            booking.transition_to(S.CANCELLED_BY_TUTOR, reason="tutor cancelled")
        except InvalidStatusTransition:
            pass  # sweep completed first; completed has no cancel edge
        finally:
            connection.close()

    threads = [threading.Thread(target=sweep), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    to_statuses = list(booking.transitions.values_list("to_status", flat=True))
    if S.COMPLETED in to_statuses:
        # Sweep won the lock: the booking is completed, never also cancelled.
        assert booking.status == S.COMPLETED
        assert S.CANCELLED_BY_TUTOR not in to_statuses
    else:
        # Cancel won the lock: the sweep re-checked confirmed under the lock and skipped it.
        assert booking.status == S.CANCELLED_BY_TUTOR
        assert to_statuses == [S.CANCELLED_BY_TUTOR]


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_sweeps_complete_a_booking_only_once():
    # Two overlapping beat ticks sweep the same finished booking at once. Each locks
    # the booking row and re-checks confirmed before transitioning, so exactly one
    # completes it and the other finds it already gone — a single completion edge, and
    # the capture is never enqueued twice.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking = _finished_confirmed()
    barrier = threading.Barrier(2)

    def sweep():
        try:
            barrier.wait(timeout=10)
            complete_confirmed_bookings()
        finally:
            connection.close()

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    assert booking.status == S.COMPLETED
    assert booking.transitions.count() == 1  # completed exactly once
