"""GET /tutors/{id}/slots — free UTC intervals, query validation, and 404 parity."""

import datetime as dt

import pytest
from rest_framework.test import APIClient

from apps.bookings.models import AvailabilityRule, Booking, Weekday
from apps.bookings.tests.factories import BookingFactory, TutorProfileFactory, UserFactory

TUTORS_URL = "/api/v1/tutors"

# A valid Monday range (2026-07-06 is a Monday), reused across validation tests.
RANGE = {"from": "2026-07-06T00:00:00Z", "to": "2026-07-07T00:00:00Z"}

UTC = dt.UTC


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


@pytest.fixture
def client():
    return APIClient()


def make_tutor(hourly_rate=1500):
    """Tutor in UTC+3 available Monday 09:00–12:00 local = 06:00–09:00 UTC."""
    tutor = TutorProfileFactory(user=UserFactory(timezone="Europe/Moscow"), hourly_rate=hourly_rate)
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.MONDAY, start_time=dt.time(9), end_time=dt.time(12)
    )
    return tutor


def slots_url(tutor_id):
    return f"{TUTORS_URL}/{tutor_id}/slots"


@pytest.mark.django_db
def test_slots_are_public_and_returned_in_utc(client):
    tutor = make_tutor()

    response = client.get(slots_url(tutor.id), RANGE)

    assert response.status_code == 200
    assert response.json() == [
        {"starts_at": "2026-07-06T06:00:00Z", "ends_at": "2026-07-06T09:00:00Z"}
    ]


@pytest.mark.django_db
def test_slots_exclude_active_bookings(client):
    tutor = make_tutor()
    BookingFactory(
        tutor=tutor,
        starts_at=utc(2026, 7, 6, 7),
        ends_at=utc(2026, 7, 6, 8),
        status=Booking.Status.CONFIRMED,
    )

    starts = [s["starts_at"] for s in client.get(slots_url(tutor.id), RANGE).json()]

    assert starts == ["2026-07-06T06:00:00Z", "2026-07-06T08:00:00Z"]


@pytest.mark.django_db
@pytest.mark.parametrize("missing", ["from", "to"])
def test_missing_bound_is_400(client, missing):
    tutor = make_tutor()
    params = {k: v for k, v in RANGE.items() if k != missing}

    response = client.get(slots_url(tutor.id), params)

    assert response.status_code == 400
    assert missing in response.json()


@pytest.mark.django_db
def test_naive_datetime_is_400(client):
    tutor = make_tutor()

    response = client.get(slots_url(tutor.id), {**RANGE, "from": "2026-07-06T00:00:00"})

    assert response.status_code == 400
    assert "from" in response.json()


@pytest.mark.django_db
def test_unparseable_datetime_is_400(client):
    tutor = make_tutor()

    response = client.get(slots_url(tutor.id), {**RANGE, "to": "not-a-date"})

    assert response.status_code == 400
    assert "to" in response.json()


@pytest.mark.django_db
def test_to_before_from_is_400(client):
    tutor = make_tutor()

    response = client.get(
        slots_url(tutor.id),
        {"from": "2026-07-07T00:00:00Z", "to": "2026-07-06T00:00:00Z"},
    )

    assert response.status_code == 400
    assert "to" in response.json()


@pytest.mark.django_db
def test_equal_bounds_are_400(client):
    tutor = make_tutor()

    response = client.get(
        slots_url(tutor.id),
        {"from": "2026-07-06T00:00:00Z", "to": "2026-07-06T00:00:00Z"},
    )

    assert response.status_code == 400
    assert "to" in response.json()


@pytest.mark.django_db
def test_range_of_exactly_31_days_is_allowed(client):
    tutor = make_tutor()

    response = client.get(
        slots_url(tutor.id),
        {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:00Z"},  # exactly 31 days
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_range_one_second_over_the_limit_is_400(client):
    tutor = make_tutor()

    response = client.get(
        slots_url(tutor.id),
        {"from": "2026-07-01T00:00:00Z", "to": "2026-08-01T00:00:01Z"},  # 31 days + 1s
    )

    assert response.status_code == 400
    assert "to" in response.json()


@pytest.mark.django_db
def test_unfinished_profile_slots_are_404(client):
    # Parity with the detail endpoint: a profile with no rate set is hidden here too.
    hidden = make_tutor(hourly_rate=0)

    response = client.get(slots_url(hidden.id), RANGE)

    assert response.status_code == 404
