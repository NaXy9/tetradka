"""expand_availability(): tutor-local rules → UTC intervals, including DST edges.

Europe/Berlin switches to summer time on 2026-03-29 (02:00 CET → 03:00 CEST)
and back on 2026-10-25 (03:00 CEST → 02:00 CET).
"""

import datetime as dt

import pytest

from apps.bookings.models import AvailabilityException, AvailabilityRule, Weekday
from apps.bookings.services import expand_availability

from .factories import TutorProfileFactory, UserFactory

pytestmark = pytest.mark.django_db

UTC = dt.UTC


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


def tutor_in(tz_name):
    return TutorProfileFactory(user=UserFactory(timezone=tz_name))


def add_rule(tutor, weekday, start_h, end_h):
    return AvailabilityRule.objects.create(
        tutor=tutor,
        weekday=weekday,
        start_time=dt.time(start_h, 0),
        end_time=dt.time(end_h, 0),
    )


def test_fixed_offset_timezone():
    tutor = tutor_in("Europe/Moscow")  # UTC+3, no DST
    add_rule(tutor, Weekday.MONDAY, 9, 12)

    intervals = expand_availability(tutor, utc(2026, 7, 6), utc(2026, 7, 13))

    assert intervals == [(utc(2026, 7, 6, 6), utc(2026, 7, 6, 9))]


def test_berlin_spring_forward_shifts_utc_offset():
    tutor = tutor_in("Europe/Berlin")
    add_rule(tutor, Weekday.SUNDAY, 9, 12)

    intervals = expand_availability(tutor, utc(2026, 3, 21), utc(2026, 3, 30))

    assert intervals == [
        (utc(2026, 3, 22, 8), utc(2026, 3, 22, 11)),  # CET, UTC+1
        (utc(2026, 3, 29, 7), utc(2026, 3, 29, 10)),  # CEST, UTC+2
    ]


def test_berlin_fall_back_shifts_utc_offset():
    tutor = tutor_in("Europe/Berlin")
    add_rule(tutor, Weekday.SUNDAY, 9, 12)

    intervals = expand_availability(tutor, utc(2026, 10, 17), utc(2026, 10, 26))

    assert intervals == [
        (utc(2026, 10, 18, 7), utc(2026, 10, 18, 10)),  # CEST, UTC+2
        (utc(2026, 10, 25, 8), utc(2026, 10, 25, 11)),  # CET, UTC+1
    ]


def test_window_crossing_spring_gap_shrinks():
    # Local 01:00-04:00 on the spring-forward night: 02:00-03:00 never happens,
    # so the UTC interval is only two hours long (00:00-02:00 UTC).
    tutor = tutor_in("Europe/Berlin")
    add_rule(tutor, Weekday.SUNDAY, 1, 4)

    intervals = expand_availability(tutor, utc(2026, 3, 28, 12), utc(2026, 3, 30))

    assert intervals == [(utc(2026, 3, 29, 0), utc(2026, 3, 29, 2))]


def test_window_crossing_fall_back_grows():
    # Local 01:00-04:00 on the fall-back night contains the repeated hour,
    # so the UTC interval is four hours long (23:00-03:00 UTC).
    tutor = tutor_in("Europe/Berlin")
    add_rule(tutor, Weekday.SUNDAY, 1, 4)

    intervals = expand_availability(tutor, utc(2026, 10, 24, 12), utc(2026, 10, 26))

    assert intervals == [(utc(2026, 10, 24, 23), utc(2026, 10, 25, 3))]


def test_window_entirely_inside_spring_gap_disappears():
    # Local 02:15-02:45 on the spring-forward night never exists on the wall
    # clock, so no phantom interval may be produced.
    tutor = tutor_in("Europe/Berlin")
    AvailabilityRule.objects.create(
        tutor=tutor,
        weekday=Weekday.SUNDAY,
        start_time=dt.time(2, 15),
        end_time=dt.time(2, 45),
    )

    assert expand_availability(tutor, utc(2026, 3, 28, 12), utc(2026, 3, 30)) == []


def test_window_matching_spring_gap_boundaries_disappears():
    tutor = tutor_in("Europe/Berlin")
    add_rule(tutor, Weekday.SUNDAY, 2, 3)  # exactly the skipped hour

    assert expand_availability(tutor, utc(2026, 3, 28, 12), utc(2026, 3, 30)) == []


def test_window_edge_inside_spring_gap_clamps_to_transition():
    # Start 02:30 does not exist; the window must begin at the transition
    # instant (03:00 CEST == 01:00 UTC), not at a phantom pre-gap mapping.
    tutor = tutor_in("Europe/Berlin")
    AvailabilityRule.objects.create(
        tutor=tutor,
        weekday=Weekday.SUNDAY,
        start_time=dt.time(2, 30),
        end_time=dt.time(4, 0),
    )

    intervals = expand_availability(tutor, utc(2026, 3, 28, 12), utc(2026, 3, 30))

    assert intervals == [(utc(2026, 3, 29, 1), utc(2026, 3, 29, 2))]


def test_ambiguous_fall_back_times_resolve_to_first_occurrence():
    # 02:00-02:30 happens twice on the fall-back night; the contract is the
    # first occurrence (still CEST, UTC+2): 00:00-00:30 UTC.
    tutor = tutor_in("Europe/Berlin")
    AvailabilityRule.objects.create(
        tutor=tutor,
        weekday=Weekday.SUNDAY,
        start_time=dt.time(2, 0),
        end_time=dt.time(2, 30),
    )

    intervals = expand_availability(tutor, utc(2026, 10, 24, 12), utc(2026, 10, 26))

    assert intervals == [(utc(2026, 10, 25, 0), utc(2026, 10, 25, 0, 30))]


def test_day_off_exception_removes_windows():
    tutor = tutor_in("Europe/Moscow")
    add_rule(tutor, Weekday.MONDAY, 9, 12)
    AvailabilityException.objects.create(tutor=tutor, date=dt.date(2026, 7, 6))

    assert expand_availability(tutor, utc(2026, 7, 6), utc(2026, 7, 7)) == []


def test_replacement_exception_overrides_rules():
    tutor = tutor_in("Europe/Moscow")
    add_rule(tutor, Weekday.MONDAY, 9, 12)
    AvailabilityException.objects.create(
        tutor=tutor,
        date=dt.date(2026, 7, 6),
        is_day_off=False,
        start_time=dt.time(15, 0),
        end_time=dt.time(17, 0),
    )

    intervals = expand_availability(tutor, utc(2026, 7, 6), utc(2026, 7, 7))

    assert intervals == [(utc(2026, 7, 6, 12), utc(2026, 7, 6, 14))]


def test_intervals_are_clipped_to_requested_range():
    tutor = tutor_in("Europe/Moscow")
    add_rule(tutor, Weekday.MONDAY, 9, 12)

    intervals = expand_availability(tutor, utc(2026, 7, 6, 7), utc(2026, 7, 6, 8))

    assert intervals == [(utc(2026, 7, 6, 7), utc(2026, 7, 6, 8))]


def test_same_utc_instants_regardless_of_viewer_timezone():
    # The service returns UTC instants; rendering in the student's timezone is a
    # pure conversion, so a UTC+3 tutor and a UTC-5 student see the same moment.
    tutor = tutor_in("Europe/Moscow")
    add_rule(tutor, Weekday.MONDAY, 9, 12)

    (interval,) = expand_availability(tutor, utc(2026, 7, 6), utc(2026, 7, 7))

    student_tz = dt.timezone(dt.timedelta(hours=-5))
    assert interval[0].astimezone(student_tz).hour == 1  # 06:00 UTC == 01:00 UTC-5
    assert interval[0] == utc(2026, 7, 6, 6)


def test_naive_range_rejected():
    tutor = tutor_in("Europe/Moscow")
    with pytest.raises(ValueError):
        expand_availability(tutor, dt.datetime(2026, 7, 6), utc(2026, 7, 7))
