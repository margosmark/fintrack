"""Deterministic investment projections (M15, docs/12): monthly-compounding
future-value math over a starting value with optional inflation adjustment.

Pure functions ONLY — `project_scenario` takes no `Session`, opens no query and
reaches no network; a scenario is a deterministic function of its arguments, so
tests pin exact hand-computed values. (The module imports
`app.tax.acb.cents`, the canonical money-rounding helper, which pulls the ORM
modules into the IMPORT GRAPH — that is a module import, not a database read,
and the docs/12 §7 "pure math, no DB reads" contract above still holds at
runtime. Rounding goes through the one canonical helper so a `.xx5` tie here
cannot disagree with any other money surface.)
"""

from decimal import Decimal

from app.tax.acb import cents

# Precomputed once at module scope; Decimal division honors the default
# 28-digit context, plenty for cent-exact results over <=50 years.
_TWELFTH = Decimal(1) / Decimal(12)
_HUNDRED = Decimal(100)


def project_scenario(
    start_value: Decimal,
    annual_contribution: Decimal,
    annual_return_pct: Decimal,
    years: int,
    inflation_pct: Decimal,
) -> list[dict]:
    """Project one scenario and return one row per YEAR k=1..years.

    Monthly steps pin the EXACT order: growth first, then month-END
    contribution (`value = value * (1 + monthly_rate); value += annual/12`).
    Input bounds guarantee the compounding base stays >= 0.5, so Decimal pow
    with a fractional exponent is always defined.

    Rows: {"year": k, "contributed_cad": str, "value_cad": str,
    "value_real_cad": str} where contributed counts ONLY contributions
    (`annual_contribution * k`, start_value excluded) and
    `value_real = value / (1 + inflation_pct/100)**k`. All amounts are
    cents-quantized ROUND_HALF_UP (canonical `app.tax.acb.cents`) before
    serialization as strings — the M65 parity contract with the client mirror
    `frontend/src/lib/projectionEngine.ts`, checked both directions against the
    frozen goldens in `tests/fixtures/projection-parity.json`.
    """
    monthly_rate = (Decimal(1) + annual_return_pct / _HUNDRED) ** _TWELFTH - Decimal(1)
    monthly_contribution = annual_contribution / Decimal(12)

    value = start_value
    rows: list[dict] = []
    for k in range(1, years + 1):
        for _month in range(12):
            value = value * (Decimal(1) + monthly_rate)
            value += monthly_contribution
        deflator = (Decimal(1) + inflation_pct / _HUNDRED) ** k
        rows.append(
            {
                "year": k,
                "contributed_cad": str(cents(annual_contribution * k)),
                "value_cad": str(cents(value)),
                "value_real_cad": str(cents(value / deflator)),
            }
        )
    return rows
