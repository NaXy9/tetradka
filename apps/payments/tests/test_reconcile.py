# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""reconcile_booking_payment: a cancelled/timed-out booking settles its hold.

Covers the single reconciliation point wired into cancel_booking and the pending
timeout sweep: a full refund releases a held hold, an unconfirmed hold is failed and
freed, and a late partial cancellation captures the retained part for the tutor while
the provider releases the rest to the student.
"""

import datetime as dt
import threading
import time
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.bookings import services as booking_services
from apps.bookings.models import Booking
from apps.bookings.services import (
    PENDING_PAYMENT_TIMEOUT,
    BookingNotCancellableError,
    cancel_booking,
    expire_pending_bookings,
)
from apps.bookings.tests.factories import BookingFactory, TutorProfileFactory, UserFactory
from apps.catalog.models import TutorProfile
from apps.payments import services, tasks
from apps.payments.models import Payment
from apps.payments.providers.base import CaptureResult, HoldResult, PaymentProviderError

pytestmark = pytest.mark.django_db

BS = Booking.Status
PS = Payment.Status


class _RecordingProvider:
    """Provider that records release/capture calls instead of touching a real PSP."""

    def __init__(self):
        self.released: list[tuple[str, str]] = []
        self.captured: list[tuple[str, Decimal, str]] = []

    def release(self, *, provider_id, idempotency_key):
        self.released.append((provider_id, idempotency_key))
        return None

    def capture(self, *, provider_id, amount, idempotency_key):
        # A partial capture settles `amount` and releases the rest, as a real PSP does.
        self.captured.append((provider_id, Decimal(amount), idempotency_key))
        return CaptureResult(provider_id=provider_id, captured_amount=Decimal(amount))


def _confirmed_with_held_payment(
    *, price="1500.00", refund_percent=0, hours_ahead=48, retained="0"
):
    """A confirmed booking backed by a held payment, party-scoped and priced.

    ``retained`` pins the late-cancellation penalty on the payment, mirroring what reconcile
    persists when a booking is cancelled: tests that call request_partial_capture directly
    set it, while end-to-end cancel tests leave it 0 and assert reconcile persists it.
    """
    tutor = TutorProfileFactory(late_cancellation_refund_percent=refund_percent)
    student = UserFactory()
    starts_at = timezone.now() + dt.timedelta(hours=hours_ahead)
    booking = BookingFactory(
        student=student,
        tutor=tutor,
        status=BS.CONFIRMED,
        starts_at=starts_at,
        ends_at=starts_at + dt.timedelta(hours=1),
        price=Decimal(price),
    )
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id="mock-hold-1",
        amount=booking.price,
        status=PS.HELD,
        retained_amount=Decimal(retained),
    )
    return booking, payment


def _pending_with_created_payment(*, price="1500.00", provider_id="mock-hold-1"):
    booking = BookingFactory(status=BS.PENDING, price=Decimal(price))
    payment = Payment.objects.create(
        booking=booking,
        provider=Payment.Provider.MOCK,
        provider_id=provider_id,
        amount=booking.price,
        status=PS.CREATED,
    )
    return booking, payment


# --- Full-refund cancellations release the held hold --------------------------


def test_student_cancel_over_24h_releases_the_held_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _confirmed_with_held_payment(hours_ahead=48, refund_percent=0)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert booking.status == BS.CANCELLED_BY_STUDENT
    # Full refund: the hold is released, not captured — no money settles and no penalty is pinned.
    assert payment.status == PS.REFUNDED
    assert payment.captured_amount == Decimal("0")
    assert payment.retained_amount == Decimal("0")
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_tutor_cancel_releases_the_held_payment(monkeypatch, django_capture_on_commit_callbacks):
    # A tutor cancellation always refunds in full, even inside the 24h window.
    booking, payment = _confirmed_with_held_payment(hours_ahead=2, refund_percent=0)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.tutor.user)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert booking.status == BS.CANCELLED_BY_TUTOR
    assert payment.status == PS.REFUNDED
    assert payment.retained_amount == Decimal("0")
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_late_cancel_with_full_policy_releases_the_held_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A 100% late-cancel policy makes even a <24h cancellation a full refund, so the
    # hold is released rather than partially captured.
    booking, payment = _confirmed_with_held_payment(hours_ahead=1, refund_percent=100)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert refund == Decimal("1500.00")
    assert payment.status == PS.REFUNDED
    assert payment.retained_amount == Decimal("0")
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


# --- A late partial cancellation captures the retained part --------------------


def test_late_partial_cancel_captures_retained_part_and_credits_tutor(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A <24h student cancellation with a partial policy captures the retained part (price
    # − refund) for the tutor; the provider releases the refunded rest in the same partial
    # capture, so there is no separate release call.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=50
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert refund == Decimal("750.00")  # half refunded to the student
    assert booking.status == BS.CANCELLED_BY_STUDENT
    # The retained penalty is pinned in committed state, then captured and paid out after
    # commission (10% of 750).
    assert payment.retained_amount == Decimal("750.00")
    assert payment.status == PS.CAPTURED
    assert payment.captured_amount == Decimal("750.00")
    assert payment.commission == Decimal("75.00")
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("675.00")
    assert provider.captured == [("mock-hold-1", Decimal("750.00"), f"capture-{payment.id}")]
    assert provider.released == []  # the partial capture releases the rest itself


def test_late_partial_cancel_with_zero_refund_captures_the_full_hold(
    monkeypatch, django_capture_on_commit_callbacks
):
    # A 0% policy: the student forfeits everything, so the whole hold is captured (nothing
    # is released) — the retained amount equals the full price.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=0
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert refund == Decimal("0.00")
    assert payment.retained_amount == Decimal("1500.00")  # the whole hold is retained
    assert payment.status == PS.CAPTURED
    assert payment.captured_amount == Decimal("1500.00")
    assert provider.captured == [("mock-hold-1", Decimal("1500.00"), f"capture-{payment.id}")]
    assert provider.released == []


def test_late_partial_cancel_persists_retained_even_if_the_capture_never_runs(monkeypatch):
    # Durability: the retained penalty is written to committed state when the booking is
    # cancelled, not only carried in the enqueued capture message. Here the on_commit
    # callbacks are never executed (no django_capture_on_commit_callbacks), standing in for a
    # broker that drops the message — the hold stays held, but the penalty survives on the
    # payment and the capture stays recoverable, since amount holds the full price and the
    # policy is never recomputed.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=50
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    refund = cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert refund == Decimal("750.00")
    assert payment.status == PS.HELD  # the async capture never ran
    assert payment.retained_amount == Decimal("750.00")  # ...but the penalty is durable
    assert provider.captured == []


def test_request_partial_capture_is_idempotent(monkeypatch):
    # A retry after the retained part already settled must not capture or credit twice. The
    # retained amount is read from the payment (persisted by reconcile), not passed in.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=50, retained="750.00"
    )
    Booking.objects.filter(pk=booking.pk).update(status=BS.CANCELLED_BY_STUDENT)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_partial_capture(payment.id)
    services.request_partial_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert payment.transitions.count() == 1
    assert provider.captured == [("mock-hold-1", Decimal("750.00"), f"capture-{payment.id}")]
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("675.00")  # credited once


def test_request_partial_capture_skips_a_booking_not_cancelled_by_student(monkeypatch):
    # A held payment whose booking is still confirmed (a stray enqueue) is left alone —
    # only a student-cancelled booking's hold is partially captured.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, refund_percent=50, retained="750.00"
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_partial_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert provider.captured == []


def test_request_partial_capture_without_a_hold_id_is_a_noop(monkeypatch):
    # No provider id means the hold was never opened at the PSP; there is nothing to
    # capture even though the booking was cancelled.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, refund_percent=50, retained="750.00"
    )
    Payment.objects.filter(pk=payment.pk).update(provider_id="")
    Booking.objects.filter(pk=booking.pk).update(status=BS.CANCELLED_BY_STUDENT)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_partial_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert provider.captured == []


def test_request_partial_capture_without_a_pinned_retained_is_a_noop(monkeypatch):
    # The penalty was never pinned (retained_amount still 0): a stray enqueue that ran ahead
    # of the reconcile which sets it must leave the hold held and recoverable, never settle a
    # zero capture into a terminal captured state.
    booking, payment = _confirmed_with_held_payment(hours_ahead=1, refund_percent=50)
    Booking.objects.filter(pk=booking.pk).update(status=BS.CANCELLED_BY_STUDENT)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_partial_capture(payment.id)

    payment.refresh_from_db()
    assert payment.retained_amount == Decimal("0")
    assert payment.status == PS.HELD
    assert provider.captured == []


def test_partial_capture_provider_failure_leaves_the_hold_untouched(monkeypatch):
    # A provider failure propagates so the Celery task retries; the hold stays held and
    # nothing is captured or credited until it settles.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, refund_percent=50, retained="750.00"
    )
    Booking.objects.filter(pk=booking.pk).update(status=BS.CANCELLED_BY_STUDENT)

    class _FailingProvider:
        def capture(self, *, provider_id, amount, idempotency_key):
            raise PaymentProviderError("PSP unavailable")

    monkeypatch.setattr(services, "get_payment_provider", lambda: _FailingProvider())

    with pytest.raises(PaymentProviderError):
        services.request_partial_capture(payment.id)

    payment.refresh_from_db()
    assert payment.status == PS.HELD
    assert not payment.transitions.exists()
    tutor = TutorProfile.objects.get(pk=booking.tutor_id)
    assert tutor.balance == Decimal("0")


def test_capture_partial_payment_task_reads_retained_and_delegates(monkeypatch):
    # The task boundary: it carries only the payment id now — the retained amount is read
    # from the payment's committed state, so a lost or redelivered message cannot corrupt it
    # (CELERY_TASK_ALWAYS_EAGER runs the task inline; .get() returns its result).
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, refund_percent=50, retained="750.00"
    )
    Booking.objects.filter(pk=booking.pk).update(status=BS.CANCELLED_BY_STUDENT)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    tasks.capture_partial_payment.delay(payment.id).get()

    payment.refresh_from_db()
    assert payment.status == PS.CAPTURED
    assert payment.captured_amount == Decimal("750.00")
    assert provider.captured == [("mock-hold-1", Decimal("750.00"), f"capture-{payment.id}")]


# --- An unconfirmed (created) hold is failed and freed ------------------------


def test_cancel_pending_fails_and_releases_the_created_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _pending_with_created_payment(provider_id="mock-hold-2")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status == PS.FAILED
    assert provider.released == [("mock-hold-2", f"release-{payment.id}")]


def test_cancel_pending_created_without_a_hold_id_releases_nothing(
    monkeypatch, django_capture_on_commit_callbacks
):
    # The hold has not been opened at the PSP yet (no provider id), so there is
    # nothing to void — the payment is simply failed.
    booking, payment = _pending_with_created_payment(provider_id="")
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert provider.released == []


# --- The timeout sweep reconciles too -----------------------------------------


def test_timeout_fails_and_releases_the_created_payment(
    monkeypatch, django_capture_on_commit_callbacks
):
    booking, payment = _pending_with_created_payment(provider_id="mock-hold-3")
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        assert expire_pending_bookings() == 1

    payment.refresh_from_db()
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status == PS.FAILED
    assert provider.released == [("mock-hold-3", f"release-{payment.id}")]


def test_timeout_without_a_payment_still_cancels():
    # The pre-payment path: a stale pending booking with no payment reconciles to a
    # clean no-op and is still cancelled.
    booking = BookingFactory(status=BS.PENDING)
    Booking.objects.filter(pk=booking.pk).update(
        created_at=timezone.now() - PENDING_PAYMENT_TIMEOUT - dt.timedelta(minutes=1)
    )

    assert expire_pending_bookings() == 1
    booking.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT


# --- Idempotency and terminal payments ----------------------------------------


def test_reconcile_is_a_noop_on_a_terminal_payment():
    # A payment that already settled (captured/refunded/failed) is left alone: only
    # created/held holds are reconciled.
    booking, payment = _confirmed_with_held_payment()
    Payment.objects.filter(pk=payment.pk).update(status=PS.FAILED)

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    payment.refresh_from_db()
    assert payment.status == PS.FAILED
    assert not payment.transitions.exists()


def test_reconcile_without_a_payment_is_a_noop():
    booking = BookingFactory(status=BS.CANCELLED_BY_STUDENT)

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    assert not Payment.objects.filter(booking=booking).exists()


def test_reconcile_a_second_time_releases_nothing_more(
    monkeypatch, django_capture_on_commit_callbacks
):
    # After a full-refund cancel released the hold, a redelivered reconcile (the
    # payment now refunded) must not walk another edge or fire a second release.
    booking, payment = _confirmed_with_held_payment(hours_ahead=48)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking=booking, actor=booking.student)
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        services.reconcile_booking_payment(booking=locked, refund_amount=booking.price, actor=None)

    payment.refresh_from_db()
    assert payment.status == PS.REFUNDED
    assert payment.transitions.count() == 1  # only the original held→refunded edge
    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]  # no second release


# --- request_release / request_hold edges -------------------------------------


def test_request_release_releases_a_refunded_payment(monkeypatch):
    # A full release marks the payment refunded; the release task must still void the
    # hold at the PSP for it (not only for failed payments).
    booking, payment = _confirmed_with_held_payment()
    Payment.objects.filter(pk=payment.pk).update(status=PS.REFUNDED)
    provider = _RecordingProvider()
    monkeypatch.setattr(services, "get_payment_provider", lambda: provider)

    services.request_release(payment.id)

    assert provider.released == [("mock-hold-1", f"release-{payment.id}")]


def test_request_hold_preserves_the_id_and_releases_when_failed_mid_flight(monkeypatch):
    # The race: the hold is opened at the PSP while the booking is cancelled in the
    # same instant, failing the payment before the id is stored. The id must not be
    # lost (the CAS keys on an empty provider_id, not on the status), and the orphaned
    # hold is released immediately rather than stranded.
    booking, payment = _pending_with_created_payment(provider_id="")
    released: list[tuple[str, str]] = []

    class _RacingProvider:
        def create_hold(self, **kwargs):
            # Stand in for the booking being cancelled while the hold is opening.
            Payment.objects.filter(pk=payment.id).update(status=PS.FAILED)
            return HoldResult(provider_id="mock-raced")

        def release(self, *, provider_id, idempotency_key):
            released.append((provider_id, idempotency_key))

    monkeypatch.setattr(services, "get_payment_provider", lambda: _RacingProvider())

    services.request_hold(payment.id)

    payment.refresh_from_db()
    assert payment.provider_id == "mock-raced"  # id recorded despite the concurrent fail
    assert payment.status == PS.FAILED
    assert released == [("mock-raced", f"release-{payment.id}")]


# --- Concurrency --------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_cancel_and_confirm_hold_never_leave_an_incoherent_pair():
    # The money-critical race: a student cancels (full refund, >24h ahead) at the same
    # instant a hold_succeeded webhook confirms the booking. Both lock the booking row
    # first (cancel via transition_to, confirm_hold explicitly), so they fully
    # serialize and the pair stays coherent: the booking must never be confirmed with a
    # failed payment, nor cancelled with the hold still held/created — a full refund
    # always drives the payment to a released terminal state.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking, payment = _pending_with_created_payment()  # BookingFactory starts 48h ahead
    barrier = threading.Barrier(2)

    def cancel():
        try:
            barrier.wait(timeout=10)
            cancel_booking(booking=booking, actor=booking.student)
        except BookingNotCancellableError:
            pass  # only if the edge vanished; not expected on this pending→confirmed path
        finally:
            connection.close()

    def confirm():
        try:
            barrier.wait(timeout=10)
            services.confirm_hold(payment)
        except services.HoldNotConfirmable:
            pass  # cancel won the lock first; the orphan path handles the hold
        finally:
            connection.close()

    threads = [threading.Thread(target=cancel), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    payment.refresh_from_db()
    # Cancel always succeeds (pending→ and confirmed→cancelled_by_student are both legal
    # edges), so the booking ends cancelled either way; the payment is released, never
    # left backing a dead booking.
    assert booking.status == BS.CANCELLED_BY_STUDENT
    assert payment.status in (PS.REFUNDED, PS.FAILED)


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_late_cancel_and_confirm_hold_never_release_the_retained_penalty(monkeypatch):
    # The partial-capture twin of the race above: a hold_succeeded webhook lands while a
    # student's late cancellation (50% policy, 1h ahead) is in flight. The cancellation
    # pins the penalty and leaves the hold held for the async partial capture, so the
    # webhook meets a held payment under an already-cancelled booking — which must NOT be
    # read as an orphan and released, or the tutor loses the penalty.
    #
    # The interleaving is forced rather than raced for: the webhook path is a short read,
    # so a plain barrier lets it finish long before the cancellation commits and it only
    # ever sees the booking still confirmed — the uninteresting ordering, which passes even
    # against the bug this test guards. Instead the cancellation signals once it holds the
    # booking row lock and lingers there, so the webhook thread blocks on that lock inside
    # confirm_hold and wakes on committed state. That is the real PostgreSQL lock path, and
    # it is the ordering that used to lose money.
    #
    # The capture is stubbed out to hold the window open: tasks run eagerly in tests, so a
    # live capture would settle the hold the instant the cancellation commits, hiding the
    # state under test. The capture itself is covered by the tests above.
    # PostgreSQL-only: SQLite ignores select_for_update, so no real lock forms.
    booking, payment = _confirmed_with_held_payment(
        hours_ahead=1, price="1500.00", refund_percent=50
    )
    monkeypatch.setattr(services, "_enqueue_partial_capture", lambda payment_id: None)

    penalty_pinned = threading.Event()
    reconcile = booking_services.reconcile_booking_payment

    def reconcile_then_hold_the_lock(**kwargs):
        reconcile(**kwargs)
        # Still inside cancel_booking's transaction, holding the booking row lock: wake the
        # webhook thread and linger so it blocks on that lock rather than racing ahead.
        penalty_pinned.set()
        time.sleep(0.3)

    monkeypatch.setattr(booking_services, "reconcile_booking_payment", reconcile_then_hold_the_lock)

    def cancel():
        try:
            cancel_booking(booking=booking, actor=booking.student)
        finally:
            connection.close()

    def confirm():
        try:
            penalty_pinned.wait(timeout=10)
            # The webhook seam, not confirm_hold: an orphaned hold is torn down by the
            # _fail_and_release fallback here, so calling confirm_hold directly would let
            # a regression pass unnoticed — it only raises, it never releases.
            services._on_hold_succeeded(payment)
        finally:
            connection.close()

    threads = [threading.Thread(target=cancel), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == BS.CANCELLED_BY_STUDENT
    # The money invariant: the penalty is pinned and its hold is still held, awaiting the
    # partial capture. A failed or refunded payment here would mean the webhook tore down
    # a hold the tutor had already earned.
    assert payment.retained_amount == Decimal("750.00")
    assert payment.status == PS.HELD
