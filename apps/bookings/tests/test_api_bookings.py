"""POST /bookings and GET /bookings — auth, validation, conflicts and scoping."""

import datetime as dt

import pytest
from rest_framework.test import APIClient

from apps.bookings.models import AvailabilityRule, Booking
from apps.catalog.models import TutorSubject

from .factories import (
    BookingFactory,
    SubjectFactory,
    TutorProfileFactory,
    UserFactory,
)

BOOKINGS_URL = "/api/v1/bookings"

pytestmark = pytest.mark.django_db

UTC = dt.UTC
S = Booking.Status


def slot(days=3, hour=10, duration=1):
    """An aware UTC interval a few days out, safely past the booking lead time."""
    day = (dt.datetime.now(UTC) + dt.timedelta(days=days)).date()
    start = dt.datetime.combine(day, dt.time(hour), tzinfo=UTC)
    return start, start + dt.timedelta(hours=duration)


def bookable_tutor(hourly_rate=1500):
    """Tutor open around the clock every day, teaching a single subject."""
    tutor = TutorProfileFactory(hourly_rate=hourly_rate)
    for weekday in range(7):
        AvailabilityRule.objects.create(
            tutor=tutor, weekday=weekday, start_time=dt.time(0), end_time=dt.time(23, 59, 59)
        )
    subject = SubjectFactory()
    TutorSubject.objects.create(tutor=tutor, subject=subject)
    return tutor, subject


def auth_client(user=None):
    user = user or UserFactory()
    client = APIClient()
    client.force_authenticate(user)
    return client, user


def payload(tutor, subject, starts_at=None, ends_at=None):
    if starts_at is None:
        starts_at, ends_at = slot()
    return {
        "tutor": tutor.id,
        "subject": subject.id,
        "starts_at": starts_at.isoformat().replace("+00:00", "Z"),
        "ends_at": ends_at.isoformat().replace("+00:00", "Z"),
    }


# --- POST /bookings -----------------------------------------------------------


def test_create_booking_returns_201_pending_owned_by_caller():
    tutor, subject = bookable_tutor()
    client, student = auth_client()

    response = client.post(BOOKINGS_URL, payload(tutor, subject))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == S.PENDING
    assert body["tutor"]["id"] == tutor.id
    assert body["student"]["id"] == student.id
    assert body["subject"]["slug"] == subject.slug
    booking = Booking.objects.get(pk=body["id"])
    assert booking.student == student
    assert booking.price == tutor.hourly_rate  # 1h at the hourly rate


def test_create_booking_requires_authentication():
    tutor, subject = bookable_tutor()

    response = APIClient().post(BOOKINGS_URL, payload(tutor, subject))

    assert response.status_code == 401


def test_naive_datetime_is_400():
    tutor, subject = bookable_tutor()
    client, _ = auth_client()
    data = payload(tutor, subject)
    data["starts_at"] = "2026-07-06T10:00:00"  # no offset

    response = client.post(BOOKINGS_URL, data)

    assert response.status_code == 400
    assert "starts_at" in response.json()


def test_ends_at_not_after_starts_at_is_400():
    tutor, subject = bookable_tutor()
    client, _ = auth_client()
    starts_at, _ = slot()
    data = payload(tutor, subject, starts_at=starts_at, ends_at=starts_at)

    response = client.post(BOOKINGS_URL, data)

    assert response.status_code == 400
    assert "ends_at" in response.json()


def test_subject_not_taught_by_tutor_is_400():
    tutor, _ = bookable_tutor()
    client, _ = auth_client()
    other_subject = SubjectFactory()

    response = client.post(BOOKINGS_URL, payload(tutor, other_subject))

    assert response.status_code == 400
    assert "subject" in response.json()


def test_booking_yourself_is_400():
    tutor, subject = bookable_tutor()
    client, _ = auth_client(user=tutor.user)

    response = client.post(BOOKINGS_URL, payload(tutor, subject))

    assert response.status_code == 400


def test_unfinished_tutor_profile_is_400():
    # hourly_rate=0 profiles are hidden everywhere; the id resolves to no tutor.
    tutor, subject = bookable_tutor(hourly_rate=0)
    client, _ = auth_client()

    response = client.post(BOOKINGS_URL, payload(tutor, subject))

    assert response.status_code == 400
    assert "tutor" in response.json()


def test_non_utc_offset_is_normalized_to_utc():
    # A client may send any offset; the domain stores UTC. 13:00+05:00 == 08:00Z.
    tutor, subject = bookable_tutor()
    client, _ = auth_client()
    day = (dt.datetime.now(UTC) + dt.timedelta(days=3)).date()
    data = {
        "tutor": tutor.id,
        "subject": subject.id,
        "starts_at": f"{day.isoformat()}T13:00:00+05:00",
        "ends_at": f"{day.isoformat()}T14:00:00+05:00",
    }

    response = client.post(BOOKINGS_URL, data)

    assert response.status_code == 201
    booking = Booking.objects.get(pk=response.json()["id"])
    assert booking.starts_at == dt.datetime.combine(day, dt.time(8), tzinfo=UTC)


def test_client_cannot_set_status_price_or_student():
    # BookingCreateSerializer declares no such fields: create_booking always
    # produces a pending booking, priced from the tutor's row, owned by the caller.
    tutor, subject = bookable_tutor()
    client, student = auth_client()
    other = UserFactory()
    data = payload(tutor, subject)
    data.update(status=S.CONFIRMED, price="1", student=other.id)

    response = client.post(BOOKINGS_URL, data)

    assert response.status_code == 201
    booking = Booking.objects.get(pk=response.json()["id"])
    assert booking.status == S.PENDING
    assert booking.student == student
    assert booking.price == tutor.hourly_rate


def test_taken_slot_is_409():
    tutor, subject = bookable_tutor()
    starts_at, ends_at = slot()
    BookingFactory(tutor=tutor, starts_at=starts_at, ends_at=ends_at, status=S.CONFIRMED)
    client, _ = auth_client()

    response = client.post(BOOKINGS_URL, payload(tutor, subject, starts_at, ends_at))

    assert response.status_code == 409


def test_slot_outside_availability_is_409():
    tutor = TutorProfileFactory(hourly_rate=1500)  # no availability rules
    subject = SubjectFactory()
    TutorSubject.objects.create(tutor=tutor, subject=subject)
    client, _ = auth_client()

    response = client.post(BOOKINGS_URL, payload(tutor, subject))

    assert response.status_code == 409


def test_slot_starting_too_soon_is_409():
    tutor, subject = bookable_tutor()
    starts_at = dt.datetime.now(UTC) + dt.timedelta(hours=1)  # inside the lead time
    client, _ = auth_client()

    response = client.post(
        BOOKINGS_URL, payload(tutor, subject, starts_at, starts_at + dt.timedelta(hours=1))
    )

    assert response.status_code == 409


# --- GET /bookings ------------------------------------------------------------


def test_list_returns_only_callers_bookings():
    tutor, subject = bookable_tutor()
    client, student = auth_client()
    mine = BookingFactory(student=student, tutor=tutor, subject=subject)
    BookingFactory()  # someone else's booking

    response = client.get(BOOKINGS_URL)

    assert response.status_code == 200
    ids = [b["id"] for b in response.json()["results"]]
    assert ids == [mine.id]


def test_list_includes_bookings_where_caller_is_the_tutor():
    tutor, subject = bookable_tutor()
    as_tutor = BookingFactory(tutor=tutor, subject=subject)
    client, _ = auth_client(user=tutor.user)

    ids = [b["id"] for b in client.get(BOOKINGS_URL).json()["results"]]

    assert ids == [as_tutor.id]


def test_role_filter_separates_the_two_sides():
    # A user who is both a student and a tutor: role picks which side to show.
    tutor, subject = bookable_tutor()
    both = tutor.user
    as_tutor = BookingFactory(tutor=tutor, subject=subject)
    as_student = BookingFactory(student=both)
    client, _ = auth_client(user=both)

    tutor_ids = [b["id"] for b in client.get(BOOKINGS_URL, {"role": "tutor"}).json()["results"]]
    student_ids = [b["id"] for b in client.get(BOOKINGS_URL, {"role": "student"}).json()["results"]]

    assert tutor_ids == [as_tutor.id]
    assert student_ids == [as_student.id]


def test_status_filter():
    client, student = auth_client()
    pending = BookingFactory(student=student, status=S.PENDING)
    BookingFactory(student=student, status=S.COMPLETED)

    ids = [b["id"] for b in client.get(BOOKINGS_URL, {"status": "pending"}).json()["results"]]

    assert ids == [pending.id]


def test_role_and_status_filters_combine():
    tutor, subject = bookable_tutor()
    both = tutor.user
    # Two same-tutor bookings must sit in distinct slots: the PostgreSQL
    # exclusion constraint forbids overlapping active bookings for one tutor.
    s1, e1 = slot(days=3)
    s2, e2 = slot(days=4)
    wanted = BookingFactory(
        tutor=tutor, subject=subject, status=S.CONFIRMED, starts_at=s1, ends_at=e1
    )
    # Wrong status, and parked in another slot so it does not clash with `wanted`.
    BookingFactory(tutor=tutor, subject=subject, status=S.PENDING, starts_at=s2, ends_at=e2)
    BookingFactory(student=both, status=S.CONFIRMED)  # wrong role, its own tutor
    client, _ = auth_client(user=both)

    ids = [
        b["id"]
        for b in client.get(BOOKINGS_URL, {"role": "tutor", "status": "confirmed"}).json()[
            "results"
        ]
    ]

    assert ids == [wanted.id]


@pytest.mark.parametrize("param", [{"role": "nobody"}, {"status": "unknown"}])
def test_invalid_filter_value_is_400(param):
    client, _ = auth_client()

    response = client.get(BOOKINGS_URL, param)

    assert response.status_code == 400


def test_list_requires_authentication():
    response = APIClient().get(BOOKINGS_URL)

    assert response.status_code == 401
