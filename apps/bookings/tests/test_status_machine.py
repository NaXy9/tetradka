"""Booking status machine: only the explicitly allowed transitions are legal."""

import pytest
from django.db.utils import IntegrityError

from apps.bookings.models import Booking, InvalidStatusTransition

from .factories import BookingFactory, UserFactory

pytestmark = pytest.mark.django_db

S = Booking.Status

ALLOWED = [
    (S.PENDING, S.CONFIRMED),
    (S.PENDING, S.CANCELLED_BY_STUDENT),
    (S.CONFIRMED, S.COMPLETED),
    (S.CONFIRMED, S.CANCELLED_BY_STUDENT),
    (S.CONFIRMED, S.CANCELLED_BY_TUTOR),
    (S.CONFIRMED, S.NO_SHOW),
]

ALL_PAIRS = [(a, b) for a in S for b in S if a != b]
FORBIDDEN = [pair for pair in ALL_PAIRS if pair not in ALLOWED]


@pytest.mark.parametrize(("src", "dst"), ALLOWED)
def test_allowed_transition(src, dst):
    booking = BookingFactory(status=src)

    booking.transition_to(dst)

    booking.refresh_from_db()
    assert booking.status == dst


@pytest.mark.parametrize(("src", "dst"), FORBIDDEN)
def test_forbidden_transition(src, dst):
    booking = BookingFactory(status=src)

    with pytest.raises(InvalidStatusTransition):
        booking.transition_to(dst)

    booking.refresh_from_db()
    assert booking.status == src


def test_completed_is_terminal():
    booking = BookingFactory(status=S.COMPLETED)
    for dst in S:
        if dst == S.COMPLETED:
            continue
        with pytest.raises(InvalidStatusTransition):
            booking.transition_to(dst)


def test_transition_is_logged_with_actor_and_reason():
    actor = UserFactory()
    booking = BookingFactory(status=S.PENDING)

    booking.transition_to(S.CONFIRMED, actor=actor, reason="payment held")

    log = booking.transitions.get()
    assert log.from_status == S.PENDING
    assert log.to_status == S.CONFIRMED
    assert log.actor == actor
    assert log.reason == "payment held"


def test_system_transition_has_no_actor():
    booking = BookingFactory(status=S.PENDING)

    booking.transition_to(S.CANCELLED_BY_STUDENT, reason="pending timeout 15m")

    log = booking.transitions.get()
    assert log.actor is None


def test_booking_ends_after_starts_constraint():
    booking = BookingFactory()
    booking.ends_at = booking.starts_at
    with pytest.raises(IntegrityError):
        booking.save()
