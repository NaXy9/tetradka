"""DB-level constraints of the booking domain, including the PostgreSQL GiST guard."""

import datetime as dt

import pytest
from django.db import transaction
from django.db.utils import IntegrityError

from apps.bookings.models import AvailabilityException, AvailabilityRule, Booking

from .factories import BookingFactory, TutorProfileFactory

pytestmark = pytest.mark.django_db

S = Booking.Status


class TestAvailabilityConstraints:
    def test_rule_end_must_be_after_start(self):
        with pytest.raises(IntegrityError):
            AvailabilityRule.objects.create(
                tutor=TutorProfileFactory(),
                weekday=0,
                start_time=dt.time(12, 0),
                end_time=dt.time(12, 0),
            )

    def test_day_off_exception_must_not_have_window(self):
        with pytest.raises(IntegrityError):
            AvailabilityException.objects.create(
                tutor=TutorProfileFactory(),
                date=dt.date(2026, 7, 20),
                is_day_off=True,
                start_time=dt.time(10, 0),
                end_time=dt.time(12, 0),
            )

    def test_replacement_exception_requires_valid_window(self):
        with pytest.raises(IntegrityError):
            AvailabilityException.objects.create(
                tutor=TutorProfileFactory(),
                date=dt.date(2026, 7, 20),
                is_day_off=False,
                start_time=None,
                end_time=None,
            )

    def test_duplicate_exception_date_rejected(self):
        # expand_availability honours one override per date; the unique constraint
        # keeps a second row for the same (tutor, date) out of the table.
        tutor = TutorProfileFactory()
        AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 7, 20))
        with pytest.raises(IntegrityError):
            AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 7, 20))

    def test_same_date_other_tutor_allowed(self):
        AvailabilityException.objects.create(tutor=TutorProfileFactory(), date=dt.date(2026, 7, 20))
        AvailabilityException.objects.create(tutor=TutorProfileFactory(), date=dt.date(2026, 7, 20))

    def test_valid_rule_and_exception_pass(self):
        tutor = TutorProfileFactory()
        AvailabilityRule.objects.create(
            tutor=tutor, weekday=0, start_time=dt.time(9, 0), end_time=dt.time(18, 0)
        )
        AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 7, 20))
        AvailabilityException.objects.create(
            tutor=tutor,
            date=dt.date(2026, 7, 21),
            is_day_off=False,
            start_time=dt.time(14, 0),
            end_time=dt.time(16, 0),
        )


@pytest.mark.postgres
class TestBookingExclusionConstraint:
    """GiST guard from migration 0002: the DB-level last line of defense (invariant #1)."""

    def test_overlapping_active_bookings_rejected(self):
        booking = BookingFactory(status=S.CONFIRMED)
        with pytest.raises(IntegrityError), transaction.atomic():
            BookingFactory(
                tutor=booking.tutor,
                starts_at=booking.starts_at + dt.timedelta(minutes=30),
                ends_at=booking.ends_at + dt.timedelta(minutes=30),
                status=S.PENDING,
            )

    def test_overlap_with_cancelled_booking_allowed(self):
        booking = BookingFactory(status=S.CANCELLED_BY_STUDENT)
        BookingFactory(
            tutor=booking.tutor,
            starts_at=booking.starts_at,
            ends_at=booking.ends_at,
            status=S.PENDING,
        )

    def test_same_slot_other_tutor_allowed(self):
        booking = BookingFactory(status=S.CONFIRMED)
        BookingFactory(
            starts_at=booking.starts_at,
            ends_at=booking.ends_at,
            status=S.CONFIRMED,
        )

    def test_adjacent_slots_allowed(self):
        booking = BookingFactory(status=S.CONFIRMED)
        BookingFactory(
            tutor=booking.tutor,
            starts_at=booking.ends_at,
            ends_at=booking.ends_at + dt.timedelta(hours=1),
            status=S.CONFIRMED,
        )
