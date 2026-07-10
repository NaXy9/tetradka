"""free_slots(): availability minus active bookings, always in UTC.

Availability expansion (rules, exceptions, DST) is covered by
test_availability_expansion; here the focus is subtracting bookings out of the
expanded intervals and which booking statuses block a slot.
"""

import datetime as dt

import pytest
from django.utils import timezone

from apps.bookings.models import AvailabilityRule, Booking, Weekday
from apps.bookings.services import free_slots

from .factories import BookingFactory, TutorProfileFactory, UserFactory

pytestmark = pytest.mark.django_db

UTC = dt.UTC


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


def moscow_tutor():
    """Tutor in UTC+3 (no DST) available Monday 09:00–12:00 local = 06:00–09:00 UTC."""
    tutor = TutorProfileFactory(user=UserFactory(timezone="Europe/Moscow"))
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.MONDAY, start_time=dt.time(9), end_time=dt.time(12)
    )
    return tutor


def book(tutor, start, end, status=Booking.Status.CONFIRMED):
    return BookingFactory(tutor=tutor, starts_at=start, ends_at=end, status=status)


# 2026-07-06 is a Monday.
MON = (utc(2026, 7, 6), utc(2026, 7, 7))


def test_no_bookings_returns_full_availability():
    tutor = moscow_tutor()

    assert free_slots(tutor, *MON) == [(utc(2026, 7, 6, 6), utc(2026, 7, 6, 9))]


def test_booking_in_the_middle_splits_the_interval():
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 7), utc(2026, 7, 6, 8))

    assert free_slots(tutor, *MON) == [
        (utc(2026, 7, 6, 6), utc(2026, 7, 6, 7)),
        (utc(2026, 7, 6, 8), utc(2026, 7, 6, 9)),
    ]


def test_booking_covering_the_whole_interval_removes_it():
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 6), utc(2026, 7, 6, 9))

    assert free_slots(tutor, *MON) == []


def test_booking_at_the_start_edge_trims_the_front():
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 6), utc(2026, 7, 6, 7))

    assert free_slots(tutor, *MON) == [(utc(2026, 7, 6, 7), utc(2026, 7, 6, 9))]


def test_adjacent_booking_does_not_split_the_interval():
    # Booking ends exactly when availability starts — no time is consumed.
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 5), utc(2026, 7, 6, 6))

    assert free_slots(tutor, *MON) == [(utc(2026, 7, 6, 6), utc(2026, 7, 6, 9))]


def test_two_bookings_leave_a_gap_between_them():
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 6), utc(2026, 7, 6, 7))
    book(tutor, utc(2026, 7, 6, 8), utc(2026, 7, 6, 9))

    assert free_slots(tutor, *MON) == [(utc(2026, 7, 6, 7), utc(2026, 7, 6, 8))]


@pytest.mark.parametrize(
    "status",
    [
        Booking.Status.CANCELLED_BY_STUDENT,
        Booking.Status.CANCELLED_BY_TUTOR,
        Booking.Status.COMPLETED,
        Booking.Status.NO_SHOW,
    ],
)
def test_inactive_bookings_free_the_slot(status):
    # Only pending/confirmed bookings block a slot; everything else frees the time.
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 7), utc(2026, 7, 6, 8), status=status)

    assert free_slots(tutor, *MON) == [(utc(2026, 7, 6, 6), utc(2026, 7, 6, 9))]


def test_pending_booking_blocks_the_slot():
    tutor = moscow_tutor()
    book(tutor, utc(2026, 7, 6, 7), utc(2026, 7, 6, 8), status=Booking.Status.PENDING)

    assert free_slots(tutor, *MON) == [
        (utc(2026, 7, 6, 6), utc(2026, 7, 6, 7)),
        (utc(2026, 7, 6, 8), utc(2026, 7, 6, 9)),
    ]


def test_subtraction_holds_on_a_dst_grown_interval():
    # Berlin fall-back night: local 01:00–04:00 Sunday spans the repeated hour and
    # expands to 23:00–03:00 UTC. A booking at 02:00–03:00 UTC leaves one free piece.
    tutor = TutorProfileFactory(user=UserFactory(timezone="Europe/Berlin"))
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.SUNDAY, start_time=dt.time(1), end_time=dt.time(4)
    )
    book(tutor, utc(2026, 10, 25, 2), utc(2026, 10, 25, 3))

    slots = free_slots(tutor, utc(2026, 10, 24, 12), utc(2026, 10, 26))

    assert slots == [(utc(2026, 10, 24, 23), utc(2026, 10, 25, 2))]


def test_subtraction_holds_on_a_dst_shrunk_interval():
    # Berlin spring-forward night: local 01:00–04:00 Sunday loses the skipped hour
    # and expands to only 00:00–02:00 UTC. A booking at 00:00–01:00 UTC leaves the
    # remaining hour, proving subtraction lines up with the shrunk interval.
    tutor = TutorProfileFactory(user=UserFactory(timezone="Europe/Berlin"))
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.SUNDAY, start_time=dt.time(1), end_time=dt.time(4)
    )
    book(tutor, utc(2026, 3, 29, 0), utc(2026, 3, 29, 1))

    slots = free_slots(tutor, utc(2026, 3, 28, 12), utc(2026, 3, 30))

    assert slots == [(utc(2026, 3, 29, 1), utc(2026, 3, 29, 2))]


def test_multiple_days_with_bookings_scattered_across_them():
    # A range spanning two availability days, each with its own booking, must be
    # subtracted independently and returned sorted.
    tutor = moscow_tutor()  # Monday only
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.WEDNESDAY, start_time=dt.time(9), end_time=dt.time(12)
    )
    book(tutor, utc(2026, 7, 6, 7), utc(2026, 7, 6, 8))  # Monday 10:00–11:00 local
    book(tutor, utc(2026, 7, 8, 6), utc(2026, 7, 8, 7))  # Wednesday 09:00–10:00 local

    slots = free_slots(tutor, utc(2026, 7, 6), utc(2026, 7, 9))

    assert slots == [
        (utc(2026, 7, 6, 6), utc(2026, 7, 6, 7)),
        (utc(2026, 7, 6, 8), utc(2026, 7, 6, 9)),
        (utc(2026, 7, 8, 7), utc(2026, 7, 8, 9)),
    ]


def test_lead_time_horizon_is_not_applied():
    # Deliberate contract: free_slots is a faithful availability projection and does
    # NOT hide slots inside the booking lead time — that horizon lives in
    # create_booking. This locks the decision against a silent regression either way.
    tutor = moscow_tutor()
    now = timezone.now()
    # Next Monday, so an availability window exists; booking is minutes away yet must
    # still surface here even though create_booking would reject it as too soon.
    days_ahead = (Weekday.MONDAY - now.weekday()) % 7 or 7
    monday = (now + dt.timedelta(days=days_ahead)).date()
    imminent = timezone.now() + dt.timedelta(minutes=5)

    slots = free_slots(
        tutor,
        imminent,
        dt.datetime.combine(monday, dt.time(23), tzinfo=UTC),
    )

    # The Monday 06:00–09:00 UTC window is returned in full, regardless of proximity.
    assert (
        dt.datetime.combine(monday, dt.time(6), tzinfo=UTC),
        dt.datetime.combine(monday, dt.time(9), tzinfo=UTC),
    ) in slots


def test_instants_are_utc_regardless_of_viewer_timezone():
    # The service returns UTC instants; a UTC-5 student sees the same moment the
    # UTC+3 tutor offered — rendering in the student's tz is a pure conversion.
    tutor = moscow_tutor()

    ((start, _),) = free_slots(tutor, *MON)

    student_tz = dt.timezone(dt.timedelta(hours=-5))
    assert start == utc(2026, 7, 6, 6)
    assert start.astimezone(student_tz).hour == 1  # 06:00 UTC == 01:00 UTC-5
