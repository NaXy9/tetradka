# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Commission math: 10% standard / 5% Pro, ROUND_HALF_UP, exact split."""

from decimal import Decimal

import pytest

from apps.payments.services import calc_commission


def test_standard_rate_is_ten_percent():
    commission, payout = calc_commission(Decimal("1500"))
    assert commission == Decimal("150.00")
    assert payout == Decimal("1350.00")


def test_pro_rate_is_five_percent():
    commission, payout = calc_commission(Decimal("1000"), is_pro=True)
    assert commission == Decimal("50.00")
    assert payout == Decimal("950.00")


def test_commission_and_payout_sum_to_captured_amount():
    amount = Decimal("999.99")
    commission, payout = calc_commission(amount)
    assert commission + payout == amount


def test_half_up_rounding_on_half_cent():
    # 100.05 * 0.10 = 10.005 → rounds up to 10.01; payout keeps the remainder,
    # so the pair still sums back to the captured amount exactly.
    commission, payout = calc_commission(Decimal("100.05"))
    assert commission == Decimal("10.01")
    assert payout == Decimal("90.04")


def test_zero_amount_yields_zero_commission():
    commission, payout = calc_commission(Decimal("0"))
    assert commission == Decimal("0.00")
    assert payout == Decimal("0.00")


@pytest.mark.parametrize("is_pro", [False, True])
def test_commission_is_quantized_to_cents(is_pro):
    commission, _ = calc_commission(Decimal("1234.56"), is_pro=is_pro)
    assert commission.as_tuple().exponent == -2


def test_sub_cent_amount_never_yields_negative_payout():
    # 0.01 * 0.10 = 0.001 → rounds to 0.00; payout stays the full cent, never negative.
    commission, payout = calc_commission(Decimal("0.01"))
    assert commission == Decimal("0.00")
    assert payout == Decimal("0.01")
    assert commission + payout == Decimal("0.01")
