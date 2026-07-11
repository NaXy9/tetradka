# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""request_hold / initiate_hold: the async provider call that opens the hold."""

from decimal import Decimal

import pytest

from apps.bookings.tests.factories import BookingFactory
from apps.payments import services, tasks
from apps.payments.models import Payment
from apps.payments.providers.base import HoldResult, PaymentProviderError

pytestmark = pytest.mark.django_db

PS = Payment.Status


def _created_payment(**kwargs):
    booking = BookingFactory(price=Decimal("1500.00"))
    defaults = {
        "booking": booking,
        "provider": Payment.Provider.MOCK,
        "amount": booking.price,
    }
    defaults.update(kwargs)
    return Payment.objects.create(**defaults)


class _BoomProvider:
    """Provider whose hold call always fails, to drive the retry path."""

    def create_hold(self, **kwargs):
        raise PaymentProviderError("psp down")


class _AssertNotCalledProvider:
    """Provider that must never be touched; asserts if the hold is requested."""

    def create_hold(self, **kwargs):
        raise AssertionError("create_hold must not be called")


def test_request_hold_stores_the_provider_id():
    payment = _created_payment()
    assert payment.provider_id == ""

    services.request_hold(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id.startswith("mock-")
    # The hold is only *requested* here; confirming it (created→held) is the
    # webhook's job, so the status stays created.
    assert payment.status == PS.CREATED


def test_request_hold_is_a_noop_when_id_already_stored(monkeypatch):
    payment = _created_payment(provider_id="mock-existing")
    monkeypatch.setattr(services, "get_payment_provider", _AssertNotCalledProvider)

    services.request_hold(payment.id)  # must not call the provider again

    payment.refresh_from_db()
    assert payment.provider_id == "mock-existing"


def test_request_hold_is_a_noop_for_a_non_created_payment(monkeypatch):
    payment = _created_payment(status=PS.HELD)
    monkeypatch.setattr(services, "get_payment_provider", _AssertNotCalledProvider)

    services.request_hold(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id == ""


def test_request_hold_propagates_provider_error_for_retry(monkeypatch):
    # request_hold must surface the provider failure unchanged so the Celery task's
    # autoretry_for=(PaymentProviderError,) backs off and tries again.
    payment = _created_payment()
    monkeypatch.setattr(services, "get_payment_provider", _BoomProvider)

    with pytest.raises(PaymentProviderError):
        services.request_hold(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id == ""


def test_task_delegates_to_service(monkeypatch):
    payment = _created_payment()
    monkeypatch.setattr(
        services,
        "get_payment_provider",
        lambda: _StubProvider(),
    )

    # CELERY_TASK_ALWAYS_EAGER runs the task inline.
    tasks.initiate_hold.delay(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id == "mock-stub"


class _StubProvider:
    def create_hold(self, **kwargs):
        return HoldResult(provider_id="mock-stub")
