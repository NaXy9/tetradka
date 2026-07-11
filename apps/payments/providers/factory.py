# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Select the payment provider named by settings.PAYMENT_PROVIDER."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .base import PaymentProvider
from .mock import MockProvider


def get_payment_provider() -> PaymentProvider:
    """Return the configured provider instance.

    Raises:
        ImproperlyConfigured: If PAYMENT_PROVIDER names a backend that is unknown
            or not implemented yet (the real YooKassa backend lands later).
    """
    name = settings.PAYMENT_PROVIDER
    if name == "mock":
        return MockProvider()
    raise ImproperlyConfigured(f"Unknown or unimplemented PAYMENT_PROVIDER: {name!r}")
