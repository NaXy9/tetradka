# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment domain services: commission math and the hold-authorization flow."""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking
from apps.catalog.models import TutorProfile
from apps.users.models import User

from .models import InvalidStatusTransition, Payment, ProcessedWebhookEvent
from .providers.base import WebhookEvent, WebhookType
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
    # Compare-and-swap on an empty provider_id: store the id only while none is set,
    # so a concurrent or redelivered run cannot overwrite an id already pointing at a
    # live hold. Status is deliberately NOT part of the condition — an empty
    # provider_id already excludes every non-created status (a hold id is always
    # stored before any held/captured/refunded transition), so the only case the
    # dropped status check newly rescues is a payment failed *mid-flight*: if the
    # booking was cancelled while this call was opening the hold, we must still record
    # the hold we opened so it can be released rather than orphaned.
    updated = Payment.objects.filter(pk=payment.id, provider_id="").update(
        provider_id=result.provider_id, updated_at=timezone.now()
    )
    if updated and Payment.objects.filter(pk=payment.id, status=Payment.Status.FAILED).exists():
        # The hold we just opened backs a booking that was cancelled mid-flight; free
        # it now instead of waiting for the PSP's confirmation webhook to notice.
        _enqueue_release(payment.id)


def confirm_hold(payment: Payment, *, actor: User | None = None) -> None:
    """Apply a confirmed hold: Payment ``created→held`` and its booking ``pending→confirmed``.

    Both moves run in one transaction, so the pair can never be half-applied: if
    the booking cannot be confirmed the payment is not held either. The booking
    row is locked first, then the payment — a stable order that matches the
    pending-timeout sweep (which locks the booking), so a hold confirming at the
    same instant the sweep runs serializes cleanly and whichever grabs the booking
    row first wins.

    Idempotent: a redelivered or late confirmation is a no-op when the hold is already
    held and its booking has moved forward — confirmed, or completed and awaiting the
    async capture (whose hold must never be released as an orphan).

    Args:
        payment: The payment whose hold the provider confirmed.
        actor: User behind the change; None means the system (a webhook).

    Raises:
        HoldNotConfirmable: If the booking was cancelled or timed out before the hold
            confirmed; the caller must release the orphaned hold. A booking that has
            already moved forward (confirmed, or completed and awaiting capture) is a
            no-op instead — its held hold is never treated as an orphan.
        InvalidStatusTransition: If the payment cannot move to ``held`` (already
            terminal, e.g. a prior failure).
    """
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)

        if locked_payment.status == Payment.Status.HELD and booking.status in (
            Booking.Status.CONFIRMED,
            Booking.Status.COMPLETED,
        ):
            # Already applied (confirmed), or the lesson is already delivered and the
            # hold is owed to the capture flow (completed). A redelivered or late
            # success is a no-op — critically, a completed booking's held hold must
            # never be torn down and released, or the tutor loses a captured lesson.
            payment.status = Payment.Status.HELD  # keep the passed instance in sync
            return

        if booking.status != Booking.Status.PENDING:
            raise HoldNotConfirmable(f"booking {booking.pk} is {booking.status}, not pending")

        locked_payment.transition_to(Payment.Status.HELD, actor=actor, reason="hold authorized")
        booking.transition_to(Booking.Status.CONFIRMED, actor=actor, reason="payment held")

    payment.status = Payment.Status.HELD


def reconcile_booking_payment(
    *, booking: Booking, refund_amount: Decimal, actor: User | None
) -> None:
    """Settle a booking's live payment after the booking reached a terminal cancel state.

    The single point that keeps the money consistent with a cancelled or timed-out
    booking. Call it from within the caller's transaction, right after the booking's
    terminal transition: the caller has already locked the booking row, and this locks
    the payment, so the lock order stays booking → payment (matching confirm_hold and
    the timeout sweep) and no deadlock forms.

    What happens depends on the payment's state and the refund owed to the student:

    * ``created`` — the hold was never confirmed on our side (the booking was still
      pending), so the money is not ours: the payment is failed and any hold already
      opened at the PSP is released. ``refund_amount`` is irrelevant here — nothing was
      captured, so freeing the whole authorization is the only outcome.
    * ``held`` with a full refund (``refund_amount >= amount``: a tutor cancellation, a
      student cancelling more than 24h ahead, or a 100% late-cancel policy) — the hold
      is released in full (held → refunded) and voided at the PSP.
    * ``held`` with a partial refund (``refund_amount < amount``: a late student
      cancellation) — the retained part must be captured and paid out to the tutor.
      That needs the capture-and-credit machinery introduced with lesson completion, so
      it is intentionally deferred: the hold is left in place until then rather than
      released in full (which would hand the student a refund the policy does not owe).

    Idempotent and safe on a booking with no payment: a redelivered call finds the
    payment already terminal and does nothing.

    Args:
        booking: The booking whose payment to reconcile; already in a terminal status.
        refund_amount: Money owed back to the student under the cancellation policy.
        actor: User behind the change; None means the system (the timeout sweep).
    """
    live = Payment.objects.select_for_update().filter(
        booking=booking, status__in=(Payment.Status.CREATED, Payment.Status.HELD)
    )
    # At most one live payment exists per booking (initiate_payment enforces it under
    # the booking lock); iterating is just defensive against a stray second one.
    for payment in live:
        if payment.status == Payment.Status.CREATED:
            payment.transition_to(
                Payment.Status.FAILED, actor=actor, reason="booking cancelled before hold confirmed"
            )
            transaction.on_commit(lambda pid=payment.id: _enqueue_release(pid))
        elif refund_amount >= payment.amount:
            payment.transition_to(
                Payment.Status.REFUNDED, actor=actor, reason="booking cancelled, hold released"
            )
            transaction.on_commit(lambda pid=payment.id: _enqueue_release(pid))
        # else: a partial refund captures the retained part — deferred to the capture
        # flow, so the hold is left untouched here.


def capture_booking_payment(*, booking: Booking) -> None:
    """Enqueue capture of a completed booking's held hold; call within the booking txn.

    The completion sweep moves a booking to ``completed`` under its row lock and then
    calls this: the held hold is the capture intent and is left in place, while the
    actual provider capture is fired asynchronously after commit (like every other
    provider call), so a slow PSP never blocks the sweep. The async task re-derives
    the amount from committed state and settles it.

    A booking with no held payment (an unpaid edge) is a clean no-op.

    Args:
        booking: The just-completed booking whose hold to capture.
    """
    payment = (
        Payment.objects.select_for_update()
        .filter(booking=booking, status=Payment.Status.HELD)
        .first()
    )
    if payment is None:
        return
    transaction.on_commit(lambda pid=payment.id: _enqueue_capture(pid))


def capture_and_credit(*, payment: Payment, captured_amount: Decimal, actor: User | None) -> None:
    """Capture money from a confirmed hold and credit the tutor's balance, atomically.

    The shared settle-and-credit primitive behind lesson completion (a full capture)
    and, later, a late partial cancellation (capturing the retained part). The status
    flip and the balance credit run in ONE transaction, so the tutor is never credited
    without the payment marked captured, nor the reverse.

    Only ever called after ``provider.capture`` has authoritatively confirmed the money
    was taken — its return is the source of truth for a capture — so the balance is
    credited against a real settlement, never optimistically ahead of it.

    Idempotent: a redelivered capture (payment already ``captured``) is a no-op, so the
    tutor is credited exactly once even if the task retries after the commit.

    Args:
        payment: The held payment to capture.
        captured_amount: Money actually taken from the hold (the provider's figure).
        actor: User behind the change; None means the system (the capture task).
    """
    commission, payout = calc_commission(captured_amount)
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.CAPTURED:
            return  # already captured — must not credit the tutor a second time
        locked.transition_to(
            Payment.Status.CAPTURED,
            actor=actor,
            reason="hold captured",
            captured_amount=captured_amount,
            commission=commission,
        )
        # Lock the profile row so lessons completing concurrently for the same tutor
        # cannot lose a balance update; the once-per-payment guarantee is the status
        # guard above — this lock only serializes the arithmetic.
        tutor = TutorProfile.objects.select_for_update().get(pk=locked.booking.tutor_id)
        tutor.balance += payout
        tutor.save(update_fields=["balance", "updated_at"])
    payment.status = Payment.Status.CAPTURED
    payment.captured_amount = captured_amount
    payment.commission = commission


def request_capture(payment_id: int) -> None:
    """Capture a completed lesson's hold and credit the tutor; idempotent, safe to retry.

    Asks the provider to take the full held amount, then records the capture and credits
    the tutor via capture_and_credit. Only a still-``held`` payment backing a
    ``completed`` booking is captured, so a retry after the money already settled — or a
    stray enqueue for a booking that never completed — is a no-op. The stable
    idempotency_key lets a retried capture de-duplicate PSP-side.

    Raises:
        PaymentProviderError: On a provider-side failure, so the task retries with
            backoff; the hold stays held and nothing is credited until it settles.
    """
    payment = Payment.objects.select_related("booking").get(pk=payment_id)
    if payment.status != Payment.Status.HELD or not payment.provider_id:
        return
    if payment.booking.status != Booking.Status.COMPLETED:
        return  # capture only settles a delivered (completed) lesson's hold
    provider = get_payment_provider()
    result = provider.capture(
        provider_id=payment.provider_id,
        amount=payment.amount,
        idempotency_key=f"capture-{payment.id}",
    )
    capture_and_credit(payment=payment, captured_amount=result.captured_amount, actor=None)


def _enqueue_capture(payment_id: int) -> None:
    # Imported lazily: tasks import this module, so a top-level import would cycle.
    from .tasks import capture_payment

    capture_payment.delay(payment_id)


def handle_webhook_event(event: WebhookEvent) -> None:
    """Apply a signature-verified provider webhook, idempotently by event id.

    Idempotency is anchored on the PSP's ``event_id`` (recorded once in
    ``ProcessedWebhookEvent``), never on the payment, so a redelivered event is
    dropped while distinct events about the same payment are each processed. The
    record and the domain change commit together: if applying the event fails, the
    record rolls back too and the PSP's next redelivery reprocesses it.

    An event whose ``provider_id`` matches no known payment is ignored and *not*
    recorded — it is foreign traffic, or a callback that raced our own persistence
    of the hold id (real PSPs confirm long after the hold opens, so this is
    effectively foreign). Leaving it unanchored lets a genuine later redelivery be
    picked up once the id is stored; the durable backstop for a hold whose
    confirmation is never redelivered is provider-status reconciliation (a stuck
    ``created`` payment polled against the PSP), owned by the reconciliation job.

    Args:
        event: The normalized, already-verified webhook event.
    """
    provider_name = settings.PAYMENT_PROVIDER
    payment = Payment.objects.filter(provider=provider_name, provider_id=event.provider_id).first()
    if payment is None:
        return

    with transaction.atomic():
        # get_or_create runs its INSERT in a savepoint, so a concurrent duplicate
        # delivery losing the unique race does not poison this transaction.
        _, created = ProcessedWebhookEvent.objects.get_or_create(
            provider=provider_name,
            event_id=event.event_id,
            defaults={"event_type": event.type, "payment": payment},
        )
        if not created:
            return  # a redelivery of an event we have already applied

        handler = _WEBHOOK_HANDLERS.get(event.type)
        if handler is not None:
            handler(payment)
        # Types we do not act on yet (capture/refund echoes, which are authoritative
        # on the server-initiated call's return) are still recorded, so a later
        # redelivery is deduplicated once those flows exist.


def _on_hold_succeeded(payment: Payment) -> None:
    """Confirm an authorization: hold the payment and confirm its booking.

    Resilient to a *late* success — one that lands after the booking already left
    ``pending`` (timed out or was cancelled). Then the hold backs nothing, so the
    payment is failed and the hold released rather than forcing a dead booking back
    to confirmed. Idempotent on redelivery via confirm_hold.
    """
    try:
        confirm_hold(payment)
    except (HoldNotConfirmable, InvalidStatusTransition):
        _fail_and_release(payment)


def _fail_and_release(payment: Payment) -> None:
    # The hold succeeded at the PSP but can no longer back this booking (it timed
    # out or was cancelled). Void the payment and free the money. Both a `created`
    # hold (never confirmed on our side) and a `held` one (confirmed, then the
    # booking was cancelled without releasing it — a state the future orchestrator
    # closes) reach `failed`; a payment already `failed` out-of-order just needs the
    # release re-issued. Re-read under the row lock so the decision is made against
    # committed state, not the snapshot taken before the transaction opened.
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status in (Payment.Status.CREATED, Payment.Status.HELD):
        locked.transition_to(Payment.Status.FAILED, reason="orphaned hold released")
    elif locked.status != Payment.Status.FAILED:
        return  # captured/refunded: the hold is already settled, nothing to free
    # The release call goes through Celery after commit, like every other provider
    # call, so a slow or failing PSP never blocks the webhook.
    transaction.on_commit(lambda: _enqueue_release(payment.id))


def _on_hold_failed(payment: Payment) -> None:
    """Mark a declined authorization failed; the booking is left to time out.

    Only a still-``created`` payment is failed: a hold that already confirmed is
    not torn down by a stray or out-of-order failure, and a replay is a no-op. The
    status is re-read under the row lock (not from the pre-transaction snapshot), so
    a hold confirming concurrently cannot be flipped to failed through the otherwise
    legal ``held → failed`` edge.
    """
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status == Payment.Status.CREATED:
        locked.transition_to(Payment.Status.FAILED, reason="authorization declined")


# A hold is voided at the PSP for a payment in either terminal "money goes back to
# the student" state: ``failed`` (an orphaned or declined hold) or ``refunded`` (a
# confirmed hold released in full when its booking was cancelled). Both free the
# whole authorization; a partial capture keeps its remainder and is never released.
_RELEASABLE_STATUSES = frozenset({Payment.Status.FAILED, Payment.Status.REFUNDED})


def request_release(payment_id: int) -> None:
    """Ask the provider to void a hold and free the money; idempotent and safe to retry.

    Called for a payment whose whole authorization must go back to the student: a
    ``failed`` hold (declined, or an orphan whose booking is gone) or a ``refunded``
    one (a confirmed hold released when its booking was cancelled). Only a payment in
    one of those states that actually holds a provider authorization is released; the
    stable idempotency_key lets a retried release de-duplicate PSP-side.

    Raises:
        PaymentProviderError: On a provider-side failure, so the task retries.
    """
    payment = Payment.objects.get(pk=payment_id)
    if payment.status not in _RELEASABLE_STATUSES or not payment.provider_id:
        return
    provider = get_payment_provider()
    provider.release(provider_id=payment.provider_id, idempotency_key=f"release-{payment.id}")


def _enqueue_release(payment_id: int) -> None:
    # Imported lazily: tasks import this module, so a top-level import would cycle.
    from .tasks import release_hold

    release_hold.delay(payment_id)


# Only the hold lifecycle drives domain changes here; capture/refund reconciliation
# arrives with the completion/refund flows.
_WEBHOOK_HANDLERS = {
    WebhookType.HOLD_SUCCEEDED: _on_hold_succeeded,
    WebhookType.HOLD_FAILED: _on_hold_failed,
}
