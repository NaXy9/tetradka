# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payments: hold → capture flow, tutor payout accounts and payouts.

Money invariant: hold on booking → capture after completed → 10% commission
(5% for Pro) → tutor balance. Commission is computed in ONE place:
payments/services.py::calc_commission(). Capture and the balance credit happen
in a single transaction.
"""

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import TimeStampedModel


class Payment(TimeStampedModel):
    """One provider payment attempt for a booking (hold/capture/refund lifecycle)."""

    class Provider(models.TextChoices):
        YOOKASSA = "yookassa", "YooKassa"
        MOCK = "mock", "Mock (dev/tests)"

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        HELD = "held", "Held"
        CAPTURED = "captured", "Captured"
        REFUNDED = "refunded", "Refunded"
        FAILED = "failed", "Failed"

    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="payments"
    )
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CREATED)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]
        constraints = [
            # Idempotency anchor for provider webhooks: one row per provider event id.
            models.UniqueConstraint(
                fields=["provider", "provider_id"],
                condition=~models.Q(provider_id=""),
                name="uniq_payment_provider_id",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment<{self.provider}:{self.provider_id or self.pk}: {self.status}>"


class PayoutAccount(TimeStampedModel):
    """Tutor's payout details. MVP stub: free-form JSON, no real requisites validation."""

    tutor = models.OneToOneField(
        "catalog.TutorProfile", on_delete=models.CASCADE, related_name="payout_account"
    )
    details = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"PayoutAccount<tutor={self.tutor_id}>"


class Payout(TimeStampedModel):
    """A withdrawal of the tutor's accumulated balance for a period (mock in MVP)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    tutor = models.ForeignKey(
        "catalog.TutorProfile", on_delete=models.PROTECT, related_name="payouts"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="payout_period_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"Payout<tutor={self.tutor_id} {self.amount}: {self.status}>"
