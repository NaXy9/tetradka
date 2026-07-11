# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Payment domain services: the single source of truth for commission math."""

from decimal import ROUND_HALF_UP, Decimal

# Platform commission taken from the captured amount. Pro tutors get a reduced
# rate; the plan flag does not exist on the model yet, so callers opt in via
# is_pro until it does.
COMMISSION_RATE_STANDARD = Decimal("0.10")
COMMISSION_RATE_PRO = Decimal("0.05")
_CENT = Decimal("0.01")


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
