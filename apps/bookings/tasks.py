# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Celery tasks for the booking domain."""

from celery import shared_task

from . import services


@shared_task(name="bookings.expire_pending_bookings")
def expire_pending_bookings() -> int:
    """Auto-cancel bookings whose payment window has elapsed (Celery beat entry point).

    Thin wrapper over services.expire_pending_bookings so the domain logic stays
    broker-free and unit-testable. Returns the number of bookings cancelled.
    """
    return services.expire_pending_bookings()
