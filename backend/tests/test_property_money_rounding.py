"""Hypothesis properties for the canonical money-rounding entry point (docs/02).

`app.tax.acb.cents` is THE rounding helper the whole app shares (AGENTS §2):
every stored money figure passes through it, so a drift here silently moves
numbers everywhere at once. These are algebraic properties, not examples —
the ones a hand-written table tends to get wrong are the exact half-cent ties
and the away-from-zero direction on negatives.

No database: `cents` is a pure Decimal function.
"""

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.tax.acb import cents

# The box is shared with other agents' suites, so hypothesis's default
# 200 ms/example deadline would measure the machine, not the code.
settings.register_profile(
    "fin_tracker", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("fin_tracker")

CENT = Decimal("0.01")
HALF_CENT = Decimal("0.005")

# Money magnitudes a real ledger sees; also keeps every quantize result far
# inside Decimal's default 28-digit precision.
money = st.decimals(
    allow_nan=False,
    allow_infinity=False,
    min_value=Decimal("-1000000000000"),
    max_value=Decimal("1000000000000"),
)


@given(money)
def test_cents_is_idempotent(x: Decimal) -> None:
    """Rounding twice must not move money: cents(cents(x)) == cents(x)."""
    once = cents(x)
    assert cents(once) == once


@given(money)
def test_cents_stays_within_half_a_cent(x: Decimal) -> None:
    """The rounded value never strays more than half a cent from the input."""
    assert abs(cents(x) - x) <= HALF_CENT


@given(money)
def test_cents_always_lands_on_two_decimal_places(x: Decimal) -> None:
    """Numeric(18,2) columns store this value verbatim — it must be 2dp."""
    assert cents(x).as_tuple().exponent == -2


@given(st.integers(min_value=1, max_value=10**9).filter(lambda n: n % 2 == 1))
def test_half_up_ties_round_away_from_zero(n: int) -> None:
    """docs/06 pins ROUND_HALF_UP: an exact half-cent tie goes AWAY from zero.

    n odd makes n * 0.005 an exact tie (its thousandths digit is 5 with
    nothing beyond it), so both the positive and negative case are real ties,
    not lookalikes.
    """
    tie = Decimal(n) * HALF_CENT
    assert cents(tie) == tie + HALF_CENT
    assert cents(-tie) == -(tie + HALF_CENT)


@given(st.integers(min_value=1, max_value=10**9).filter(lambda n: n % 2 == 1))
def test_half_even_ties_land_on_an_even_cent(n: int) -> None:
    """The documented HALF_EVEN surfaces (valuation, exposure) tie to even.

    Same exact ties as the HALF_UP test — the mode is the only difference,
    which is exactly why both live on one shared implementation.
    """
    tie = Decimal(n) * HALF_CENT
    for value in (tie, -tie):
        rounded = cents(value, rounding=ROUND_HALF_EVEN)
        assert abs(rounded - value) <= HALF_CENT
        assert int(rounded * 100) % 2 == 0, "a HALF_EVEN tie must land on an even cent"


@given(st.integers(min_value=1, max_value=10**6), st.dates(date(2000, 1, 1), date(2035, 12, 31)))
def test_quantized_storage_round_trips_through_the_pinned_scale(
    account_id: int, when: date
) -> None:
    """cents(x) survives a str round-trip — the Decimal-serialises-as-string
    contract (AGENTS §2) depends on it: json → Decimal(str) must not move."""
    x = Decimal(account_id) / Decimal(7)
    assert Decimal(str(cents(x))) == cents(x)
    assert cents(Decimal(when.year)) == Decimal(f"{when.year}.00")
