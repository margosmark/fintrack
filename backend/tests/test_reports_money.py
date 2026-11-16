"""Reports money-formatting edge contracts: ROUND_HALF_UP is pinned for money.

Covers the rounding mode used everywhere money is serialized (docs/12), so a
regression to banker's rounding would break these asserts (scout M6 et al.).
"""

from __future__ import annotations

from decimal import Decimal

from app.services.reports import _cents, _fmt


def test_cents_rounds_half_up() -> None:
    assert _cents(Decimal("0.005")) == Decimal("0.01")
    assert _cents(Decimal("0.004")) == Decimal("0.00")
    assert _cents(Decimal("2.345")) == Decimal("2.35")
    assert _cents(Decimal("-0.005")) == Decimal("-0.01")


def test_fmt_money_rounds_half_up_and_serializes() -> None:
    assert _fmt(Decimal("0.005")) == "0.01"
    assert _fmt(Decimal("1234.5")) == "1234.50"
    assert _fmt(Decimal("0.004")) == "0.00"


def test_fmt_non_cents_does_not_round() -> None:
    # Non-money decimals are normalized, not cents-quantized (no rounding).
    assert _fmt(Decimal("0.005"), cents=False) == "0.005"
