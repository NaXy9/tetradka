# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment model constraints: webhook idempotency anchor (§8)."""

import pytest
from django.db import transaction
from django.db.utils import IntegrityError

from apps.bookings.tests.factories import BookingFactory
from apps.payments.models import Payment

pytestmark = pytest.mark.django_db


def _payment(**kwargs):
    defaults = {
        "booking": BookingFactory(),
        "provider": Payment.Provider.MOCK,
        "amount": 1500,
    }
    defaults.update(kwargs)
    return Payment.objects.create(**defaults)


def test_duplicate_provider_id_rejected():
    _payment(provider_id="evt-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        _payment(provider_id="evt-1")


def test_blank_provider_id_not_unique():
    _payment(provider_id="")
    _payment(provider_id="")  # two rows without a provider id are fine


def test_same_provider_id_different_provider_allowed():
    _payment(provider=Payment.Provider.MOCK, provider_id="evt-1")
    _payment(provider=Payment.Provider.YOOKASSA, provider_id="evt-1")
