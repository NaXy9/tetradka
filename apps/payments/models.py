# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payments: hold → capture flow, tutor payout accounts and payouts.

Money invariant: hold on booking → capture after completed → 10% commission
(5% for Pro) → tutor balance. Commission is computed in ONE place:
payments/services.py::calc_commission(). Capture and the balance credit happen
in a single transaction.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction

from apps.common.models import TimeStampedModel


class InvalidStatusTransition(Exception):
    """Raised on an attempt to move a Payment along a non-existent edge."""


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

    # The only legal edges of the payment lifecycle; captured/refunded/failed are
    # terminal here. held → refunded is a full release of a confirmed hold (tutor
    # cancel, student > 24h, no-show); held → failed voids a hold that never
    # captured — a provider decline, a pending-payment timeout, or an orphaned hold
    # freed after its booking was cancelled; held → captured takes the money (full
    # on completion, partial on a late cancellation). A post-capture refund
    # (captured → refunded) is deferred to the real provider.
    ALLOWED_TRANSITIONS = {
        Status.CREATED: {Status.HELD, Status.FAILED},
        Status.HELD: {Status.CAPTURED, Status.REFUNDED, Status.FAILED},
    }

    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="payments"
    )
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_id = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    # Money actually taken from the hold: full price on completion, price − release
    # on a partial (late-cancellation) capture, 0 while only held or fully released.
    captured_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    # The penalty to capture for the tutor on a late student cancellation, pinned from
    # the cancellation policy and written when the booking is cancelled — the capture
    # *intent*, distinct from captured_amount (the settled result). Persisting it (vs.
    # carrying it only in the enqueued capture message) makes it recoverable if the
    # message is lost: amount holds the full price and the policy is never recomputed
    # in the payments layer, so committed state is the only place it survives. 0 on
    # every other path (full capture, full release, still held).
    retained_amount = models.DecimalField(
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
            # Money guards: can never capture more than was held, the platform
            # commission can never exceed what was actually captured, and the retained
            # penalty can never exceed the held amount.
            models.CheckConstraint(
                condition=models.Q(captured_amount__lte=models.F("amount")),
                name="payment_captured_le_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(commission__lte=models.F("captured_amount")),
                name="payment_commission_le_captured",
            ),
            models.CheckConstraint(
                condition=models.Q(retained_amount__lte=models.F("amount")),
                name="payment_retained_le_amount",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment<{self.provider}:{self.provider_id or self.pk}: {self.status}>"

    def transition_to(
        self,
        new_status: str,
        *,
        actor=None,
        reason: str = "",
        captured_amount: Decimal | None = None,
        commission: Decimal | None = None,
    ) -> None:
        """Move along one edge of the payment lifecycle and record an audit entry.

        Locks the row first so concurrent transitions (a provider webhook vs. a
        Celery reconciliation) serialize and the edge check runs against the
        committed status, not a stale in-memory copy.

        Money settled on the edge — captured_amount and its commission — is
        written under that same lock, in the same transaction as the status flip.
        Never set those fields in a separate save: doing so would let a racing
        capture flip the status while a different attempt's amount is persisted.
        Both are only meaningful on a capture, so they are rejected on any other
        edge.

        Args:
            new_status: Target status; must be reachable from the current one.
            actor: User behind the change; None means the system (Celery).
            reason: Free-text audit note.
            captured_amount: Money taken from the hold (capture edge only).
            commission: Platform commission on that capture (capture edge only).

        Raises:
            InvalidStatusTransition: If the current status has no edge to
                `new_status`.
            ValueError: If `new_status` is not a valid Status, or money is passed
                on a non-capture edge.
        """
        new_status = self.Status(new_status)
        settles_money = captured_amount is not None or commission is not None
        if settles_money and new_status != self.Status.CAPTURED:
            raise ValueError("captured_amount/commission are only valid on a capture")
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            old_status = self.Status(locked.status)
            if new_status not in self.ALLOWED_TRANSITIONS.get(old_status, set()):
                raise InvalidStatusTransition(f"{old_status} → {new_status} is not allowed")
            locked.status = new_status
            update_fields = ["status", "updated_at"]
            if captured_amount is not None:
                locked.captured_amount = captured_amount
                update_fields.append("captured_amount")
            if commission is not None:
                locked.commission = commission
                update_fields.append("commission")
            locked.save(update_fields=update_fields)
            PaymentStatusTransition.objects.create(
                payment=locked,
                from_status=old_status,
                to_status=new_status,
                actor=actor,
                reason=reason,
            )
        self.status = new_status
        if captured_amount is not None:
            self.captured_amount = captured_amount
        if commission is not None:
            self.commission = commission


class PaymentStatusTransition(models.Model):
    """Audit log of payment lifecycle transitions. actor=None means the system (Celery)."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=16, choices=Payment.Status.choices)
    to_status = models.CharField(max_length=16, choices=Payment.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.payment_id}: {self.from_status} → {self.to_status}"


class ProcessedWebhookEvent(models.Model):
    """Inbox anchor that makes provider webhooks idempotent by event id.

    A PSP redelivers the same event (at-least-once), possibly out of order, so the
    handler records every event id it processes and drops any redelivery. Keyed by
    (provider, event_id) — the PSP's own event identifier, NOT the payment — so two
    distinct events about the same payment (hold, then capture) are each processed
    once, while a duplicate of either is skipped.
    """

    provider = models.CharField(max_length=32, choices=Payment.Provider.choices)
    event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=32)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="webhook_events")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="uniq_processed_webhook_event"
            ),
        ]

    def __str__(self) -> str:
        return f"WebhookEvent<{self.provider}:{self.event_id} {self.event_type}>"


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
