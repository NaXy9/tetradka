"""POST /bookings/{id}/cancel — auth, party scoping, status edges and refunds."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.bookings.models import AvailabilityRule, Booking
from apps.catalog.models import TutorSubject

from .factories import (
    BookingFactory,
    SubjectFactory,
    TutorProfileFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db

UTC = dt.UTC
S = Booking.Status


def cancel_url(booking_id):
    return f"/api/v1/bookings/{booking_id}/cancel"


def auth_client(user=None):
    user = user or UserFactory()
    client = APIClient()
    client.force_authenticate(user)
    return client, user


def confirmed_booking(
    *, student=None, tutor=None, hours_ahead=48, price="1500.00", refund_percent=0
):
    """A confirmed booking starting `hours_ahead` from now, priced and party-scoped."""
    tutor = tutor or TutorProfileFactory(late_cancellation_refund_percent=refund_percent)
    student = student or UserFactory()
    starts_at = timezone.now() + dt.timedelta(hours=hours_ahead)
    return BookingFactory(
        student=student,
        tutor=tutor,
        status=S.CONFIRMED,
        starts_at=starts_at,
        ends_at=starts_at + dt.timedelta(hours=1),
        price=Decimal(price),
    )


# --- Refund policy ------------------------------------------------------------


def test_student_cancel_more_than_24h_refunds_full():
    booking = confirmed_booking(hours_ahead=25, price="1500.00", refund_percent=50)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == S.CANCELLED_BY_STUDENT
    assert Decimal(body["refund_amount"]) == Decimal("1500.00")
    booking.refresh_from_db()
    assert booking.status == S.CANCELLED_BY_STUDENT


def test_student_cancel_less_than_24h_uses_tutor_policy():
    booking = confirmed_booking(hours_ahead=23, price="1500.00", refund_percent=50)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 200
    assert Decimal(response.json()["refund_amount"]) == Decimal("750.00")


def test_student_cancel_less_than_24h_zero_policy_refunds_nothing():
    booking = confirmed_booking(hours_ahead=23, price="1500.00", refund_percent=0)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert Decimal(response.json()["refund_amount"]) == Decimal("0.00")


def test_exactly_24h_ahead_is_a_late_cancellation():
    # The full-refund window is strict: at (a hair under) 24h the tutor's late
    # policy applies, not 100%. now() advances past the 24h mark by the time the
    # refund is computed, so this deterministically lands on the late branch.
    booking = confirmed_booking(hours_ahead=24, price="1500.00", refund_percent=50)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert Decimal(response.json()["refund_amount"]) == Decimal("750.00")


def test_late_refund_rounds_half_up():
    # 999.99 * 50% = 499.995 → 500.00 (ROUND_HALF_UP), matching create_booking.
    booking = confirmed_booking(hours_ahead=1, price="999.99", refund_percent=50)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert Decimal(response.json()["refund_amount"]) == Decimal("500.00")


def test_tutor_cancel_always_refunds_full():
    # A tutor cancelling inside the 24h window still owes the student a full refund.
    booking = confirmed_booking(hours_ahead=2, price="1500.00", refund_percent=0)
    client, _ = auth_client(user=booking.tutor.user)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == S.CANCELLED_BY_TUTOR
    assert Decimal(body["refund_amount"]) == Decimal("1500.00")


def test_pending_booking_can_be_cancelled():
    booking = confirmed_booking(hours_ahead=48, refund_percent=50)
    booking.status = S.PENDING
    booking.save(update_fields=["status"])
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 200
    assert response.json()["status"] == S.CANCELLED_BY_STUDENT


# --- Access control and status edges ------------------------------------------


def test_cancel_requires_authentication():
    booking = confirmed_booking()

    response = APIClient().post(cancel_url(booking.id))

    assert response.status_code == 401


def test_cancel_by_non_party_is_404():
    # A stranger's id must be indistinguishable from a missing one, not a 403.
    booking = confirmed_booking()
    client, _ = auth_client()

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 404


def test_cancel_completed_booking_is_409():
    booking = confirmed_booking()
    booking.status = S.COMPLETED
    booking.save(update_fields=["status"])
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 409
    booking.refresh_from_db()
    assert booking.status == S.COMPLETED


def test_cancel_no_show_booking_is_409():
    # no_show is terminal, like completed: it has no cancellation edge.
    booking = confirmed_booking()
    booking.status = S.NO_SHOW
    booking.save(update_fields=["status"])
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id))

    assert response.status_code == 409
    booking.refresh_from_db()
    assert booking.status == S.NO_SHOW


def test_double_cancel_is_409():
    booking = confirmed_booking(hours_ahead=48)
    client, _ = auth_client(user=booking.student)

    assert client.post(cancel_url(booking.id)).status_code == 200
    assert client.post(cancel_url(booking.id)).status_code == 409


def test_cancel_records_reason_in_audit_log():
    booking = confirmed_booking(hours_ahead=48)
    client, _ = auth_client(user=booking.student)

    client.post(cancel_url(booking.id), {"reason": "changed my mind"}, format="json")

    log = booking.transitions.get()
    assert log.to_status == S.CANCELLED_BY_STUDENT
    assert log.actor == booking.student
    assert log.reason == "changed my mind"


def test_over_long_reason_is_400():
    booking = confirmed_booking(hours_ahead=48)
    client, _ = auth_client(user=booking.student)

    response = client.post(cancel_url(booking.id), {"reason": "x" * 256}, format="json")

    assert response.status_code == 400
    booking.refresh_from_db()
    assert booking.status == S.CONFIRMED


# --- Cancellation frees the slot ----------------------------------------------


def test_cancelling_frees_the_slot_for_rebooking():
    tutor = TutorProfileFactory(hourly_rate=1500)
    for weekday in range(7):
        AvailabilityRule.objects.create(
            tutor=tutor, weekday=weekday, start_time=dt.time(0), end_time=dt.time(23, 59, 59)
        )
    subject = SubjectFactory()
    TutorSubject.objects.create(tutor=tutor, subject=subject)

    day = (dt.datetime.now(UTC) + dt.timedelta(days=3)).date()
    starts_at = dt.datetime.combine(day, dt.time(10), tzinfo=UTC)
    body = {
        "tutor": tutor.id,
        "subject": subject.id,
        "starts_at": starts_at.isoformat().replace("+00:00", "Z"),
        "ends_at": (starts_at + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }

    first_client, _ = auth_client()
    booking_id = first_client.post("/api/v1/bookings", body).json()["id"]

    # The slot is taken until the booking is cancelled.
    second_client, _ = auth_client()
    assert second_client.post("/api/v1/bookings", body).status_code == 409

    assert first_client.post(cancel_url(booking_id)).status_code == 200
    assert second_client.post("/api/v1/bookings", body).status_code == 201
