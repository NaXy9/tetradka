# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""POST /bookings/{id}/pay — auth, ownership, status guard, idempotency, server amount."""

import threading
from decimal import Decimal

import pytest
from django.db import connection
from rest_framework.test import APIClient

from apps.bookings.models import Booking
from apps.bookings.tests.factories import BookingFactory, TutorProfileFactory, UserFactory
from apps.payments.models import Payment
from apps.payments.services import initiate_payment

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status


def pay_url(booking_id):
    return f"/api/v1/bookings/{booking_id}/pay"


def auth_client(user=None):
    user = user or UserFactory()
    client = APIClient()
    client.force_authenticate(user)
    return client, user


def test_pay_requires_authentication():
    booking = BookingFactory(status=BS.PENDING)

    response = APIClient().post(pay_url(booking.id))

    assert response.status_code == 401


def test_student_opens_payment_for_pending_booking(django_capture_on_commit_callbacks):
    student = UserFactory()
    booking = BookingFactory(status=BS.PENDING, student=student, price=Decimal("1500.00"))
    client, _ = auth_client(student)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = client.post(pay_url(booking.id))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == PS.CREATED
    assert Decimal(body["amount"]) == Decimal("1500.00")
    payment = Payment.objects.get(booking=booking)
    assert payment.amount == booking.price
    # The hold was queued and, run eagerly, stored the provider id.
    assert len(callbacks) == 1
    payment.refresh_from_db()
    assert payment.provider_id.startswith("mock-")


def test_amount_comes_from_the_server_not_the_body():
    student = UserFactory()
    booking = BookingFactory(status=BS.PENDING, student=student, price=Decimal("1500.00"))
    client, _ = auth_client(student)

    # A client trying to underpay is ignored; the price is the server's.
    response = client.post(pay_url(booking.id), {"amount": "1.00"}, format="json")

    assert response.status_code == 202
    payment = Payment.objects.get(booking=booking)
    assert payment.amount == Decimal("1500.00")


def test_paying_twice_is_idempotent():
    student = UserFactory()
    booking = BookingFactory(status=BS.PENDING, student=student)
    client, _ = auth_client(student)

    first = client.post(pay_url(booking.id))
    second = client.post(pay_url(booking.id))

    assert first.status_code == second.status_code == 202
    # No second hold: one payment, and the same one returned both times.
    assert Payment.objects.filter(booking=booking).count() == 1
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.parametrize("status", [BS.CONFIRMED, BS.CANCELLED_BY_STUDENT, BS.COMPLETED])
def test_cannot_pay_a_non_pending_booking(status):
    student = UserFactory()
    booking = BookingFactory(status=status, student=student)
    client, _ = auth_client(student)

    response = client.post(pay_url(booking.id))

    assert response.status_code == 409
    assert not Payment.objects.filter(booking=booking).exists()


def test_tutor_party_cannot_pay():
    # The tutor is a party to the booking but not the payer; scoped out as a 404.
    user = UserFactory()
    tutor = TutorProfileFactory(user=user)
    booking = BookingFactory(status=BS.PENDING, tutor=tutor, student=UserFactory())
    client, _ = auth_client(user)

    response = client.post(pay_url(booking.id))

    assert response.status_code == 404
    assert not Payment.objects.filter(booking=booking).exists()


def test_stranger_cannot_pay():
    booking = BookingFactory(status=BS.PENDING, student=UserFactory())
    client, _ = auth_client(UserFactory())

    response = client.post(pay_url(booking.id))

    assert response.status_code == 404


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_pay_opens_exactly_one_hold():
    # Two "pay" calls race the same pending booking (a double-tapped button). The
    # booking's row lock serializes them: the first opens the hold, the second sees
    # the live payment and returns it — never a second Payment, never a double
    # charge. Called at the service level so the two runs hit real transactions.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    student = UserFactory()
    booking = BookingFactory(status=BS.PENDING, student=student)
    barrier = threading.Barrier(2)

    def attempt():
        try:
            barrier.wait(timeout=10)
            initiate_payment(booking=booking, actor=student)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert Payment.objects.filter(booking=booking).count() == 1


def test_response_exposes_the_payment_shape():
    student = UserFactory()
    booking = BookingFactory(status=BS.PENDING, student=student)
    client, _ = auth_client(student)

    body = client.post(pay_url(booking.id)).json()

    assert set(body) == {"id", "booking", "provider", "status", "amount", "created_at"}
    assert body["booking"] == booking.id
    assert body["provider"] == Payment.Provider.MOCK
