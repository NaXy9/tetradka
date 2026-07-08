"""create_booking(): slot validity, overlap protection and price calculation."""

import datetime as dt
import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.bookings.models import AvailabilityException, AvailabilityRule, Booking
from apps.bookings.services import SlotUnavailableError, create_booking

from .factories import BookingFactory, SubjectFactory, TutorProfileFactory, UserFactory

pytestmark = pytest.mark.django_db

UTC = dt.UTC
S = Booking.Status


def slot(duration=1, hour=10):
    day = (dt.datetime.now(UTC) + dt.timedelta(days=3)).date()
    start = dt.datetime.combine(day, dt.time(hour), tzinfo=UTC)
    return start, start + dt.timedelta(hours=duration)


def open_all_week(tutor):
    for weekday in range(7):
        AvailabilityRule.objects.create(
            tutor=tutor,
            weekday=weekday,
            start_time=dt.time(0, 0),
            end_time=dt.time(23, 59, 59),
        )


def available_tutor(**kwargs):
    tutor = TutorProfileFactory(**kwargs)
    open_all_week(tutor)
    return tutor


def book(tutor, starts_at, ends_at, subject=None):
    return create_booking(
        student=UserFactory(),
        tutor=tutor,
        subject=subject or SubjectFactory(),
        starts_at=starts_at,
        ends_at=ends_at,
    )


def test_creates_pending_booking_with_price_from_hourly_rate():
    tutor = available_tutor(hourly_rate=Decimal("1500.00"))
    starts_at, ends_at = slot(duration=2)

    booking = book(tutor, starts_at, ends_at)

    assert booking.status == S.PENDING
    assert booking.price == Decimal("3000.00")


def test_price_comes_from_committed_row_not_stale_object():
    tutor = available_tutor(hourly_rate=Decimal("1500.00"))
    TutorProfileFactory._meta.model.objects.filter(pk=tutor.pk).update(
        hourly_rate=Decimal("2000.00")
    )
    starts_at, ends_at = slot()

    booking = book(tutor, starts_at, ends_at)  # in-memory tutor still says 1500

    assert booking.price == Decimal("2000.00")


def test_fractional_duration_price_rounding():
    tutor = available_tutor(hourly_rate=Decimal("1000.00"))
    starts_at, _ = slot()
    ends_at = starts_at + dt.timedelta(minutes=25)  # 1000 * 25/60 = 416.66(6)

    booking = book(tutor, starts_at, ends_at)

    assert booking.price == Decimal("416.67")


def test_half_cent_rounds_up_not_to_even():
    tutor = available_tutor(hourly_rate=Decimal("15.00"))
    starts_at, _ = slot()
    ends_at = starts_at + dt.timedelta(seconds=30)  # 15 * 30/3600 = 0.125

    booking = book(tutor, starts_at, ends_at)

    assert booking.price == Decimal("0.13")  # ROUND_HALF_UP, not banker's 0.12


@pytest.mark.parametrize("blocking_status", [S.PENDING, S.CONFIRMED])
def test_overlap_with_active_booking_rejected(blocking_status):
    starts_at, ends_at = slot()
    existing = BookingFactory(starts_at=starts_at, ends_at=ends_at, status=blocking_status)
    open_all_week(existing.tutor)

    with pytest.raises(SlotUnavailableError):
        book(
            existing.tutor,
            starts_at + dt.timedelta(minutes=30),
            ends_at + dt.timedelta(minutes=30),
        )


@pytest.mark.parametrize(
    "inactive_status", [S.CANCELLED_BY_STUDENT, S.CANCELLED_BY_TUTOR, S.COMPLETED, S.NO_SHOW]
)
def test_inactive_booking_does_not_block_slot(inactive_status):
    starts_at, ends_at = slot()
    existing = BookingFactory(starts_at=starts_at, ends_at=ends_at, status=inactive_status)
    open_all_week(existing.tutor)

    booking = book(existing.tutor, starts_at, ends_at)

    assert booking.pk is not None


def test_adjacent_slot_allowed():
    starts_at, ends_at = slot()
    existing = BookingFactory(starts_at=starts_at, ends_at=ends_at, status=S.CONFIRMED)
    open_all_week(existing.tutor)

    booking = book(existing.tutor, ends_at, ends_at + dt.timedelta(hours=1))

    assert booking.pk is not None


def test_slot_outside_availability_rejected():
    tutor = TutorProfileFactory()  # no availability rules at all
    starts_at, ends_at = slot()

    with pytest.raises(SlotUnavailableError):
        book(tutor, starts_at, ends_at)


def test_slot_on_day_off_rejected():
    tutor = available_tutor()
    starts_at, ends_at = slot()
    AvailabilityException.objects.create(tutor=tutor, date=starts_at.date())

    with pytest.raises(SlotUnavailableError):
        book(tutor, starts_at, ends_at)


def test_slot_partially_outside_availability_rejected():
    tutor = TutorProfileFactory()
    starts_at, ends_at = slot(duration=2)  # 10:00-12:00
    AvailabilityRule.objects.create(
        tutor=tutor,
        weekday=starts_at.weekday(),
        start_time=dt.time(9, 0),
        end_time=dt.time(11, 0),
    )

    with pytest.raises(SlotUnavailableError):
        book(tutor, starts_at, ends_at)


def test_slot_starting_too_soon_rejected():
    tutor = available_tutor()
    starts_at = dt.datetime.now(UTC) + dt.timedelta(hours=1)

    with pytest.raises(SlotUnavailableError):
        book(tutor, starts_at, starts_at + dt.timedelta(hours=1))


def test_invalid_interval_rejected():
    tutor = available_tutor()
    starts_at, _ = slot()
    with pytest.raises(ValueError):
        book(tutor, starts_at, starts_at)


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_requests_create_exactly_one_booking():
    tutor = available_tutor()
    subject = SubjectFactory()
    students = [UserFactory(), UserFactory()]
    starts_at, ends_at = slot()
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(student):
        try:
            barrier.wait(timeout=10)
            create_booking(
                student=student,
                tutor=tutor,
                subject=subject,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            outcomes.append("created")
        except SlotUnavailableError:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(student,)) for student in students]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "rejected"]
    assert Booking.objects.filter(tutor=tutor).count() == 1
