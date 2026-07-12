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


@shared_task(
    name="payments.capture_payment",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def capture_payment(payment_id: int) -> None:
    """Capture a completed lesson's hold at the PSP and credit the tutor (async entry).

    Thin wrapper over services.request_capture. The capture call is authoritative for
    the money, so the domain flip to ``captured`` and the tutor's balance credit happen
    only after it returns; a provider-side failure is retried with backoff so a transient
    PSP outage does not strand the settlement, and the retry is idempotent.
    """
    services.request_capture(payment_id)


@shared_task(
    name="payments.release_hold",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def release_hold(payment_id: int) -> None:
    """Void an orphaned authorization hold at the PSP (async entry).

    Thin wrapper over services.request_release. Used when a hold succeeds for a
    booking that is already gone: the payment is failed and its money must be
    freed. A provider-side failure is retried with backoff so a transient PSP
    outage does not strand the student's money.
    """
    services.request_release(payment_id)
