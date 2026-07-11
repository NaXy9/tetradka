# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment domain services: commission math and the hold-authorization flow."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking
from apps.users.models import User

from .models import Payment
from .providers.factory import get_payment_provider

# Platform commission taken from the captured amount. Pro tutors get a reduced
# rate; the plan flag does not exist on the model yet, so callers opt in via
# is_pro until it does.
COMMISSION_RATE_STANDARD = Decimal("0.10")
COMMISSION_RATE_PRO = Decimal("0.05")
_CENT = Decimal("0.01")


class BookingNotPayableError(Exception):
    """The booking is not in a status from which a payment can be opened (not pending)."""


class HoldNotConfirmable(Exception):
    """A confirmed hold arrived for a booking that is no longer pending.

    The booking timed out or was cancelled before the authorization confirmed, so
    the hold now backs nothing and must be released. Releasing it belongs to the
    webhook layer, which owns the out-of-order / late-succeeded cases.
    """


def calc_commission(captured_amount: Decimal, *, is_pro: bool = False) -> tuple[Decimal, Decimal]:
    """Split a captured amount into platform commission and tutor payout.

    Commission is rounded to the cent (ROUND_HALF_UP); the payout is taken by
    subtraction so commission + payout always equals captured_amount exactly, with
    no rounding drift regardless of the rate.

    Args:
        captured_amount: Money actually captured from the student's hold.
        is_pro: Whether the tutor is on the reduced-commission Pro plan.

    Returns:
        ``(commission, payout)`` as Decimals that sum to captured_amount.
    """
    rate = COMMISSION_RATE_PRO if is_pro else COMMISSION_RATE_STANDARD
    commission = (Decimal(captured_amount) * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
    payout = Decimal(captured_amount) - commission
    return commission, payout


def initiate_payment(*, booking: Booking, actor: User) -> Payment:
    """Open payment for a pending booking and queue the provider's hold, async.

    Records the *intent* to authorize: a ``created`` Payment for the server-side
    ``booking.price`` (never a client-supplied amount). The provider is not called
    inline — a Celery task does that after commit, so a slow or failing PSP never
    blocks the request and the hold is retried with backoff.

    Idempotent under the booking's row lock: a booking that already has a live
    (created or held) payment returns it instead of opening a second hold, so a
    double-tapped "pay" cannot double-charge. The booking must be pending.

    Args:
        booking: Booking to pay for; must belong to ``actor`` as the student.
        actor: The paying student (audit context for later transitions).

    Returns:
        The live Payment for the booking — freshly created or the existing one.

    Raises:
        BookingNotPayableError: If the booking is not in the ``pending`` status.
    """
    with transaction.atomic():
        # Re-fetch under the lock so the status check and the amount both come from
        # committed state, and so this serializes with the pending-timeout sweep.
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        if locked.status != Booking.Status.PENDING:
            raise BookingNotPayableError("only a pending booking can be paid")

        existing = locked.payments.filter(
            status__in=(Payment.Status.CREATED, Payment.Status.HELD)
        ).first()
        if existing is not None:
            return existing  # a hold is already in flight; do not open a second one

        payment = Payment.objects.create(
            booking=locked,
            provider=settings.PAYMENT_PROVIDER,
            amount=locked.price,
        )
        # Fire the provider call only once the row is committed and visible to the
        # worker; a rolled-back request must not leave a hold dangling at the PSP.
        transaction.on_commit(lambda: _enqueue_hold(payment.id))
    return payment


def _enqueue_hold(payment_id: int) -> None:
    # Imported lazily: tasks import this module, so a top-level import would cycle.
    from .tasks import initiate_hold

    initiate_hold.delay(payment_id)


def request_hold(payment_id: int) -> None:
    """Ask the provider to open the authorization hold and store its id.

    Idempotent and safe for the Celery task to retry: only a still-``created``
    payment without a provider id triggers a provider call, so a retry that runs
    after the id was stored (or after the hold already confirmed) is a no-op. The
    idempotency_key is stable per payment, so a retried create_hold is de-duplicated
    PSP-side and returns the same hold — this is what makes an at-least-once retry
    (e.g. a worker that died after the PSP call but before the DB write) converge
    to a single hold instead of orphaning one.

    Raises:
        PaymentProviderError: On a provider-side failure, so the task retries.
    """
    payment = Payment.objects.get(pk=payment_id)
    if payment.status != Payment.Status.CREATED or payment.provider_id:
        return

    provider = get_payment_provider()
    result = provider.create_hold(
        amount=payment.amount,
        booking_id=payment.booking_id,
        idempotency_key=f"hold-{payment.id}",
    )
    # Compare-and-swap: store the id only while the row is still created with no id,
    # so a concurrent or redelivered run cannot overwrite an already-stored
    # provider_id and lose the hold it points at.
    Payment.objects.filter(pk=payment.id, status=Payment.Status.CREATED, provider_id="").update(
        provider_id=result.provider_id, updated_at=timezone.now()
    )


def confirm_hold(payment: Payment, *, actor: User | None = None) -> None:
    """Apply a confirmed hold: Payment ``created→held`` and its booking ``pending→confirmed``.

    Both moves run in one transaction, so the pair can never be half-applied: if
    the booking cannot be confirmed the payment is not held either. The booking
    row is locked first, then the payment — a stable order that matches the
    pending-timeout sweep (which locks the booking), so a hold confirming at the
    same instant the sweep runs serializes cleanly and whichever grabs the booking
    row first wins.

    Idempotent: a redelivered confirmation (payment already held, booking already
    confirmed) is a no-op.

    Args:
        payment: The payment whose hold the provider confirmed.
        actor: User behind the change; None means the system (a webhook).

    Raises:
        HoldNotConfirmable: If the booking is no longer pending (it timed out or
            was cancelled first); the caller must release the orphaned hold.
        InvalidStatusTransition: If the payment cannot move to ``held`` (already
            terminal, e.g. a prior failure).
    """
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)

        if (
            locked_payment.status == Payment.Status.HELD
            and booking.status == Booking.Status.CONFIRMED
        ):
            payment.status = Payment.Status.HELD  # keep the passed instance in sync
            return

        if booking.status != Booking.Status.PENDING:
            raise HoldNotConfirmable(f"booking {booking.pk} is {booking.status}, not pending")

        locked_payment.transition_to(Payment.Status.HELD, actor=actor, reason="hold authorized")
        booking.transition_to(Booking.Status.CONFIRMED, actor=actor, reason="payment held")

    payment.status = Payment.Status.HELD
