# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment status machine: only the explicitly allowed edges are legal."""

import threading
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.db.utils import IntegrityError

from apps.bookings.tests.factories import BookingFactory, UserFactory
from apps.payments.models import InvalidStatusTransition, Payment

pytestmark = pytest.mark.django_db

S = Payment.Status

ALLOWED = [
    (S.CREATED, S.HELD),
    (S.CREATED, S.FAILED),
    (S.HELD, S.CAPTURED),
    (S.HELD, S.REFUNDED),
    (S.HELD, S.FAILED),
]

ALL_PAIRS = [(a, b) for a in S for b in S if a != b]
FORBIDDEN = [pair for pair in ALL_PAIRS if pair not in ALLOWED]


def _payment(**kwargs):
    defaults = {
        "booking": BookingFactory(),
        "provider": Payment.Provider.MOCK,
        "amount": Decimal("1500"),
    }
    defaults.update(kwargs)
    return Payment.objects.create(**defaults)


@pytest.mark.parametrize(("src", "dst"), ALLOWED)
def test_allowed_transition(src, dst):
    payment = _payment(status=src)

    payment.transition_to(dst)

    payment.refresh_from_db()
    assert payment.status == dst


@pytest.mark.parametrize(("src", "dst"), FORBIDDEN)
def test_forbidden_transition(src, dst):
    payment = _payment(status=src)

    with pytest.raises(InvalidStatusTransition):
        payment.transition_to(dst)

    payment.refresh_from_db()
    assert payment.status == src


@pytest.mark.parametrize("terminal", [S.CAPTURED, S.REFUNDED, S.FAILED])
def test_terminal_status_has_no_exit(terminal):
    payment = _payment(status=terminal)
    for dst in S:
        if dst == terminal:
            continue
        with pytest.raises(InvalidStatusTransition):
            payment.transition_to(dst)


def test_invalid_status_value_rejected():
    payment = _payment(status=S.CREATED)
    with pytest.raises(ValueError):
        payment.transition_to("bogus")


def test_transition_is_logged_with_actor_and_reason():
    actor = UserFactory()
    payment = _payment(status=S.CREATED)

    payment.transition_to(S.HELD, actor=actor, reason="provider authorized")

    log = payment.transitions.get()
    assert log.from_status == S.CREATED
    assert log.to_status == S.HELD
    assert log.actor == actor
    assert log.reason == "provider authorized"


def test_system_transition_has_no_actor():
    payment = _payment(status=S.CREATED)

    payment.transition_to(S.FAILED, reason="pending payment timeout")

    log = payment.transitions.get()
    assert log.actor is None


def test_capture_writes_amount_and_commission_atomically():
    payment = _payment(status=S.HELD, amount=Decimal("1500"))

    payment.transition_to(S.CAPTURED, captured_amount=Decimal("1500"), commission=Decimal("150.00"))

    payment.refresh_from_db()
    assert payment.status == S.CAPTURED
    assert payment.captured_amount == Decimal("1500")
    assert payment.commission == Decimal("150.00")


@pytest.mark.parametrize("release", [S.REFUNDED, S.FAILED])
def test_release_edges_leave_captured_amount_zero(release):
    payment = _payment(status=S.HELD, amount=Decimal("1500"))

    payment.transition_to(release, reason="100% release")

    payment.refresh_from_db()
    assert payment.status == release
    assert payment.captured_amount == Decimal("0")


@pytest.mark.parametrize(
    ("src", "dst"), [(S.CREATED, S.HELD), (S.HELD, S.REFUNDED), (S.HELD, S.FAILED)]
)
def test_money_rejected_on_non_capture_edge(src, dst):
    payment = _payment(status=src, amount=Decimal("1500"))

    with pytest.raises(ValueError):
        payment.transition_to(dst, captured_amount=Decimal("100"))

    payment.refresh_from_db()
    assert payment.status == src
    assert payment.captured_amount == Decimal("0")


def test_captured_amount_cannot_exceed_amount():
    payment = _payment(amount=Decimal("1500"))
    payment.captured_amount = Decimal("2000")
    with pytest.raises(IntegrityError), transaction.atomic():
        payment.save(update_fields=["captured_amount"])


def test_commission_cannot_exceed_captured_amount():
    payment = _payment(amount=Decimal("1500"))
    payment.captured_amount = Decimal("100")
    payment.commission = Decimal("200")
    with pytest.raises(IntegrityError), transaction.atomic():
        payment.save(update_fields=["captured_amount", "commission"])


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_transitions_take_exactly_one_edge():
    # Two workers race to move the same held payment. The row lock forces each
    # edge check to run against the committed status, so the first wins and the
    # second finds a terminal status with no edge left.
    payment = _payment(status=S.HELD)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(dst):
        try:
            barrier.wait(timeout=10)
            Payment.objects.get(pk=payment.pk).transition_to(dst)
            outcomes.append("applied")
        except InvalidStatusTransition:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=attempt, args=(S.CAPTURED,)),
        threading.Thread(target=attempt, args=(S.REFUNDED,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["applied", "rejected"]
    assert Payment.objects.get(pk=payment.pk).transitions.count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_captures_persist_the_winning_amount():
    # Two captures with different amounts race the same held payment. Because the
    # amount is written under the same lock as the status flip, the stored amount
    # must belong to the capture that won the status — never a mix of the two.
    payment = _payment(status=S.HELD, amount=Decimal("2000"))
    amounts = {"full": Decimal("2000"), "partial": Decimal("1000")}
    barrier = threading.Barrier(2)
    winners = []

    def attempt(key):
        try:
            barrier.wait(timeout=10)
            Payment.objects.get(pk=payment.pk).transition_to(
                S.CAPTURED, captured_amount=amounts[key]
            )
            winners.append(key)
        except InvalidStatusTransition:
            pass
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(key,)) for key in amounts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(winners) == 1
    payment.refresh_from_db()
    assert payment.captured_amount == amounts[winners[0]]
