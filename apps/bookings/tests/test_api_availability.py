"""CRUD for a tutor's own availability: rules and one-off exceptions.

Covers ownership scoping, the tutor-only permission, and the window/overlap
validation that keeps expand_availability free of duplicated or shadowed
intervals. The timezone maths itself lives in the expansion tests.
"""

import datetime as dt
import threading

import pytest
from django.db import connection
from rest_framework.test import APIClient

from apps.bookings.models import AvailabilityException, AvailabilityRule, Weekday
from apps.bookings.services import expand_availability

from .factories import TutorProfileFactory, UserFactory

RULES_URL = "/api/v1/tutor/availability/rules"
EXCEPTIONS_URL = "/api/v1/tutor/availability/exceptions"

pytestmark = pytest.mark.django_db

UTC = dt.UTC
MON = Weekday.MONDAY


def tutor_client():
    """An authenticated client whose user owns a tutor profile."""
    tutor = TutorProfileFactory()
    client = APIClient()
    client.force_authenticate(tutor.user)
    return client, tutor


def rule_payload(weekday=MON, start="10:00", end="12:00"):
    return {"weekday": weekday, "start_time": start, "end_time": end}


# --- rules: create & ownership ------------------------------------------------


def test_create_rule_returns_201_owned_by_caller():
    client, tutor = tutor_client()

    response = client.post(RULES_URL, rule_payload())

    assert response.status_code == 201
    rule = AvailabilityRule.objects.get(pk=response.json()["id"])
    assert rule.tutor == tutor
    assert rule.weekday == MON
    assert rule.start_time == dt.time(10) and rule.end_time == dt.time(12)


def test_create_rule_ignores_tutor_in_body():
    # `tutor` is not a serializer field; a spoofed id in the body is dropped and
    # the rule is owned by the authenticated caller.
    client, tutor = tutor_client()
    other = TutorProfileFactory()

    response = client.post(RULES_URL, {**rule_payload(), "tutor": other.id})

    assert response.status_code == 201
    assert AvailabilityRule.objects.get(pk=response.json()["id"]).tutor == tutor


def test_list_returns_only_callers_rules_unpaginated():
    client, tutor = tutor_client()
    mine = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )
    AvailabilityRule.objects.create(
        tutor=TutorProfileFactory(), weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    body = client.get(RULES_URL).json()

    # A flat array (no pagination): the weekly editor wants the whole schedule.
    assert isinstance(body, list)
    assert [r["id"] for r in body] == [mine.id]


def test_other_tutors_rule_is_404():
    client, _ = tutor_client()
    foreign = AvailabilityRule.objects.create(
        tutor=TutorProfileFactory(), weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    assert client.get(f"{RULES_URL}/{foreign.id}").status_code == 404
    assert client.patch(f"{RULES_URL}/{foreign.id}", {"end_time": "13:00"}).status_code == 404
    assert client.delete(f"{RULES_URL}/{foreign.id}").status_code == 404


# --- rules: update & delete ---------------------------------------------------


def test_update_rule_patch():
    client, tutor = tutor_client()
    rule = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.patch(f"{RULES_URL}/{rule.id}", {"end_time": "13:00"})

    assert response.status_code == 200
    rule.refresh_from_db()
    assert rule.end_time == dt.time(13)


def test_replace_rule_put():
    client, tutor = tutor_client()
    rule = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.put(
        f"{RULES_URL}/{rule.id}", rule_payload(weekday=Weekday.TUESDAY, start="09:00", end="10:30")
    )

    assert response.status_code == 200
    rule.refresh_from_db()
    assert rule.weekday == Weekday.TUESDAY
    assert rule.start_time == dt.time(9) and rule.end_time == dt.time(10, 30)


def test_delete_rule():
    client, tutor = tutor_client()
    rule = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    assert client.delete(f"{RULES_URL}/{rule.id}").status_code == 204
    assert not AvailabilityRule.objects.filter(pk=rule.id).exists()


# --- rules: window & overlap validation ---------------------------------------


def test_end_before_start_is_400():
    client, _ = tutor_client()

    response = client.post(RULES_URL, rule_payload(start="12:00", end="10:00"))

    assert response.status_code == 400
    assert "end_time" in response.json()


def test_invalid_weekday_is_400():
    client, _ = tutor_client()

    response = client.post(RULES_URL, rule_payload(weekday=9))

    assert response.status_code == 400
    assert "weekday" in response.json()


def test_overlapping_rule_same_weekday_is_400():
    client, tutor = tutor_client()
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.post(RULES_URL, rule_payload(start="11:00", end="13:00"))

    assert response.status_code == 400


def test_adjacent_rule_same_weekday_is_ok():
    # Half-open windows: [10:00, 12:00) and [12:00, 14:00) touch but do not overlap.
    client, tutor = tutor_client()
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.post(RULES_URL, rule_payload(start="12:00", end="14:00"))

    assert response.status_code == 201


def test_overlap_on_different_weekday_is_allowed():
    client, tutor = tutor_client()
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.post(
        RULES_URL, rule_payload(weekday=Weekday.TUESDAY, start="10:00", end="12:00")
    )

    assert response.status_code == 201


def test_update_does_not_clash_with_itself():
    # Editing a rule must not count its own row as an overlap.
    client, tutor = tutor_client()
    rule = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.patch(f"{RULES_URL}/{rule.id}", {"end_time": "11:00"})

    assert response.status_code == 200


def test_update_weekday_clashes_with_other_rule():
    # Moving a rule to another weekday must run the overlap check against that
    # weekday's rules, not only exclude the row itself.
    client, tutor = tutor_client()
    AvailabilityRule.objects.create(
        tutor=tutor, weekday=Weekday.TUESDAY, start_time=dt.time(10), end_time=dt.time(12)
    )
    moving = AvailabilityRule.objects.create(
        tutor=tutor, weekday=MON, start_time=dt.time(10), end_time=dt.time(12)
    )

    response = client.patch(f"{RULES_URL}/{moving.id}", {"weekday": Weekday.TUESDAY})

    assert response.status_code == 400


def test_created_rule_appears_in_expand_availability():
    # End-to-end: a rule written through the API is the same one the expansion
    # reads. Tutor tz is UTC, so a Monday 10:00–12:00 rule projects unchanged.
    client, tutor = tutor_client()
    client.post(RULES_URL, rule_payload())

    monday = _next_weekday(MON)
    start = dt.datetime.combine(monday, dt.time(0), tzinfo=UTC)
    intervals = expand_availability(tutor, start, start + dt.timedelta(days=1))

    assert intervals == [
        (
            dt.datetime.combine(monday, dt.time(10), tzinfo=UTC),
            dt.datetime.combine(monday, dt.time(12), tzinfo=UTC),
        )
    ]


# --- exceptions: create & window validation -----------------------------------


def test_create_day_off_exception_201():
    # is_day_off defaults to True; a day off carries no replacement window.
    client, tutor = tutor_client()

    response = client.post(EXCEPTIONS_URL, {"date": "2026-08-15"}, format="json")

    assert response.status_code == 201
    exc = AvailabilityException.objects.get(pk=response.json()["id"])
    assert exc.tutor == tutor and exc.is_day_off is True
    assert exc.start_time is None and exc.end_time is None


def test_create_replacement_exception_201():
    client, tutor = tutor_client()

    response = client.post(
        EXCEPTIONS_URL,
        {"date": "2026-08-15", "is_day_off": False, "start_time": "14:00", "end_time": "16:00"},
        format="json",
    )

    assert response.status_code == 201
    exc = AvailabilityException.objects.get(pk=response.json()["id"])
    assert exc.is_day_off is False
    assert exc.start_time == dt.time(14) and exc.end_time == dt.time(16)


def test_day_off_with_window_is_400():
    client, _ = tutor_client()

    response = client.post(
        EXCEPTIONS_URL,
        {"date": "2026-08-15", "is_day_off": True, "start_time": "14:00"},
        format="json",
    )

    assert response.status_code == 400


def test_replacement_without_window_is_400():
    client, _ = tutor_client()

    response = client.post(
        EXCEPTIONS_URL, {"date": "2026-08-15", "is_day_off": False}, format="json"
    )

    assert response.status_code == 400


def test_replacement_end_before_start_is_400():
    client, _ = tutor_client()

    response = client.post(
        EXCEPTIONS_URL,
        {"date": "2026-08-15", "is_day_off": False, "start_time": "16:00", "end_time": "14:00"},
        format="json",
    )

    assert response.status_code == 400
    assert "end_time" in response.json()


def test_duplicate_date_is_400():
    client, tutor = tutor_client()
    AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 8, 15))

    response = client.post(EXCEPTIONS_URL, {"date": "2026-08-15"}, format="json")

    assert response.status_code == 400
    assert "date" in response.json()


def test_update_exception_keeps_its_date():
    # Turning a day off into a replacement window on the same date must not trip
    # the duplicate-date guard on the row's own date.
    client, tutor = tutor_client()
    exc = AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 8, 15))

    response = client.patch(
        f"{EXCEPTIONS_URL}/{exc.id}",
        {"is_day_off": False, "start_time": "09:00", "end_time": "11:00"},
        format="json",
    )

    assert response.status_code == 200
    exc.refresh_from_db()
    assert exc.is_day_off is False and exc.start_time == dt.time(9)


def test_day_off_toggle_keeping_stale_window_is_400():
    # Flipping a replacement back to a day off must clear its window in the same
    # request; a leftover start/end contradicts is_day_off and is rejected.
    client, tutor = tutor_client()
    exc = AvailabilityException.objects.create(
        tutor=tutor,
        date=dt.date(2026, 8, 15),
        is_day_off=False,
        start_time=dt.time(9),
        end_time=dt.time(11),
    )

    response = client.patch(f"{EXCEPTIONS_URL}/{exc.id}", {"is_day_off": True}, format="json")

    assert response.status_code == 400


def test_day_off_toggle_clearing_window_is_200():
    client, tutor = tutor_client()
    exc = AvailabilityException.objects.create(
        tutor=tutor,
        date=dt.date(2026, 8, 15),
        is_day_off=False,
        start_time=dt.time(9),
        end_time=dt.time(11),
    )

    response = client.patch(
        f"{EXCEPTIONS_URL}/{exc.id}",
        {"is_day_off": True, "start_time": None, "end_time": None},
        format="json",
    )

    assert response.status_code == 200
    exc.refresh_from_db()
    assert exc.is_day_off is True and exc.start_time is None


def test_replace_exception_put():
    client, tutor = tutor_client()
    exc = AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 8, 15))

    response = client.put(
        f"{EXCEPTIONS_URL}/{exc.id}",
        {"date": "2026-08-16", "is_day_off": False, "start_time": "14:00", "end_time": "16:00"},
        format="json",
    )

    assert response.status_code == 200
    exc.refresh_from_db()
    assert exc.date == dt.date(2026, 8, 16) and exc.is_day_off is False


# --- exceptions: ownership & delete -------------------------------------------


def test_list_returns_only_callers_exceptions_unpaginated():
    client, tutor = tutor_client()
    mine = AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 8, 15))
    AvailabilityException.objects.create(tutor=TutorProfileFactory(), date=dt.date(2026, 8, 15))

    body = client.get(EXCEPTIONS_URL).json()

    assert isinstance(body, list)
    assert [e["id"] for e in body] == [mine.id]


def test_other_tutors_exception_is_404():
    client, _ = tutor_client()
    foreign = AvailabilityException.objects.create(
        tutor=TutorProfileFactory(), date=dt.date(2026, 8, 15)
    )

    assert client.get(f"{EXCEPTIONS_URL}/{foreign.id}").status_code == 404
    assert client.delete(f"{EXCEPTIONS_URL}/{foreign.id}").status_code == 404


def test_delete_exception():
    client, tutor = tutor_client()
    exc = AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 8, 15))

    assert client.delete(f"{EXCEPTIONS_URL}/{exc.id}").status_code == 204
    assert not AvailabilityException.objects.filter(pk=exc.id).exists()


# --- permission: tutors only --------------------------------------------------


@pytest.mark.parametrize("url", [RULES_URL, EXCEPTIONS_URL])
def test_non_tutor_is_403(url):
    # A student (no tutor profile) may not manage availability, on any verb.
    client = APIClient()
    client.force_authenticate(UserFactory())

    assert client.get(url).status_code == 403
    assert client.post(url, {"date": "2026-08-15"}).status_code == 403
    assert client.patch(f"{url}/1", {}).status_code == 403
    assert client.delete(f"{url}/1").status_code == 403


@pytest.mark.parametrize("url", [RULES_URL, EXCEPTIONS_URL])
def test_anonymous_is_401(url):
    client = APIClient()
    assert client.get(url).status_code == 401
    assert client.post(url, {"date": "2026-08-15"}).status_code == 401
    assert client.delete(f"{url}/1").status_code == 401


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_overlapping_rules_keep_exactly_one():
    # Two overlapping windows for one weekday, posted at once: the profile-row
    # lock serializes them, so the loser's overlap check sees the committed rule.
    tutor = TutorProfileFactory()
    barrier = threading.Barrier(2)
    windows = [("10:00", "12:00"), ("11:00", "13:00")]
    statuses = []

    def attempt(window):
        client = APIClient()
        client.force_authenticate(tutor.user)
        start, end = window
        try:
            barrier.wait(timeout=10)
            statuses.append(client.post(RULES_URL, rule_payload(start=start, end=end)).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(w,)) for w in windows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [201, 400]
    assert AvailabilityRule.objects.filter(tutor=tutor).count() == 1


def _next_weekday(weekday: int) -> dt.date:
    """The next calendar date (today or later) falling on `weekday`."""
    today = dt.datetime.now(UTC).date()
    return today + dt.timedelta(days=(weekday - today.weekday()) % 7)
