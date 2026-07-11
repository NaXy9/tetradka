# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Celery tasks for the payment domain."""

from celery import shared_task

from . import services
from .providers.base import PaymentProviderError


@shared_task(
    name="payments.initiate_hold",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def initiate_hold(payment_id: int) -> None:
    """Ask the PSP to open the authorization hold for a created payment (async entry).

    Thin wrapper over services.request_hold so the domain logic stays broker-free
    and unit-testable. A provider-side failure raises PaymentProviderError, which
    is retried with exponential backoff so a transient PSP outage does not drop
    the hold.
    """
    services.request_hold(payment_id)
