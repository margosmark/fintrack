"""Tax projection — estimate annual liability using year-specific CRA brackets.

Bracket tables are keyed by TAX YEAR and sourced from the CRA's published
"tax rates and income brackets" pages. A year with no table is NOT silently
priced with another year's numbers: `resolve_brackets` returns an explicit
fallback and the caller reports it via `bracket_year` / `brackets_fallback`
(docs/27).

Sources (verified against canada.ca, page detail 2026-06-25):
- 2026: https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html
- 2025: https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/last-year.html

Adding a year is a data-only change: add a `_BRACKETS_<year>` entry, then
register it in `BRACKET_YEARS`. Nothing else needs to move.
"""

from decimal import Decimal

_CENT = Decimal("0.01")
_ZERO = Decimal("0")

# ---------------------------------------------------------------- 2026 (CRA)
# 14% on the first $58,523 (down from 14.5% in 2025 and 15% in 2024).
FEDERAL_BRACKETS_2026 = [
    (Decimal("58523"), Decimal("0.14")),
    (Decimal("117045"), Decimal("0.205")),
    (Decimal("181440"), Decimal("0.26")),
    (Decimal("258482"), Decimal("0.29")),
    (Decimal("999999999"), Decimal("0.33")),
]

PROVINCIAL_BRACKETS_2026: dict[str, list] = {
    "ON": [
        (Decimal("53891"), Decimal("0.0505")),
        (Decimal("107785"), Decimal("0.0915")),
        (Decimal("150000"), Decimal("0.1116")),
        (Decimal("220000"), Decimal("0.1216")),
        (Decimal("999999999"), Decimal("0.1316")),
    ],
    "BC": [
        (Decimal("50363"), Decimal("0.056")),
        (Decimal("100728"), Decimal("0.077")),
        (Decimal("115648"), Decimal("0.105")),
        (Decimal("140430"), Decimal("0.1229")),
        (Decimal("190405"), Decimal("0.147")),
        (Decimal("265545"), Decimal("0.168")),
        (Decimal("999999999"), Decimal("0.205")),
    ],
    "AB": [
        (Decimal("61200"), Decimal("0.08")),
        (Decimal("154259"), Decimal("0.10")),
        (Decimal("185111"), Decimal("0.12")),
        (Decimal("246813"), Decimal("0.13")),
        (Decimal("370220"), Decimal("0.14")),
        (Decimal("999999999"), Decimal("0.15")),
    ],
}

# ---------------------------------------------------------------- 2025 (CRA)
FEDERAL_BRACKETS_2025 = [
    (Decimal("57375"), Decimal("0.145")),
    (Decimal("114750"), Decimal("0.205")),
    (Decimal("177882"), Decimal("0.26")),
    (Decimal("253414"), Decimal("0.29")),
    (Decimal("999999999"), Decimal("0.33")),
]

PROVINCIAL_BRACKETS_2025: dict[str, list] = {
    "ON": [
        (Decimal("52886"), Decimal("0.0505")),
        (Decimal("105775"), Decimal("0.0915")),
        (Decimal("150000"), Decimal("0.1116")),
        (Decimal("220000"), Decimal("0.1216")),
        (Decimal("999999999"), Decimal("0.1316")),
    ],
    "BC": [
        (Decimal("49279"), Decimal("0.0506")),
        (Decimal("98560"), Decimal("0.077")),
        (Decimal("113158"), Decimal("0.105")),
        (Decimal("137407"), Decimal("0.1229")),
        (Decimal("186306"), Decimal("0.147")),
        (Decimal("259829"), Decimal("0.168")),
        (Decimal("999999999"), Decimal("0.205")),
    ],
    "AB": [
        (Decimal("60000"), Decimal("0.08")),
        (Decimal("151234"), Decimal("0.10")),
        (Decimal("181481"), Decimal("0.12")),
        (Decimal("241974"), Decimal("0.13")),
        (Decimal("362961"), Decimal("0.14")),
        (Decimal("999999999"), Decimal("0.15")),
    ],
}

# Tax year → (federal brackets, provincial brackets).
BRACKET_YEARS: dict[int, tuple[list, dict[str, list]]] = {
    2026: (FEDERAL_BRACKETS_2026, PROVINCIAL_BRACKETS_2026),
    2025: (FEDERAL_BRACKETS_2025, PROVINCIAL_BRACKETS_2025),
}

# Year used when the requested one has no published table here. Callers must
# surface this (`brackets_fallback`) so a stale table is never silent.
FALLBACK_BRACKET_YEAR = 2026

DEFAULT_PROVINCE = "ON"


def resolve_brackets(year: int) -> tuple[list, dict[str, list], int, bool]:
    """Return (federal, provincial, bracket_year, used_fallback) for `year`."""
    if year in BRACKET_YEARS:
        federal, provincial = BRACKET_YEARS[year]
        return federal, provincial, year, False
    federal, provincial = BRACKET_YEARS[FALLBACK_BRACKET_YEAR]
    return federal, provincial, FALLBACK_BRACKET_YEAR, True


def compute_tax(taxable_income: Decimal, brackets: list) -> Decimal:
    """Compute tax using progressive brackets."""
    tax = _ZERO
    prev = _ZERO
    for ceiling, rate in brackets:
        if taxable_income <= prev:
            break
        taxable_in_bracket = min(taxable_income, ceiling) - prev
        tax += taxable_in_bracket * rate
        prev = ceiling
    return tax.quantize(_CENT)


def project_tax(
    employment_income: Decimal,
    capital_gains: Decimal,
    dividend_income: Decimal,
    deductions: Decimal,
    province: str = DEFAULT_PROVINCE,
    year: int | None = None,
) -> dict:
    """Project total tax liability for `year` (defaults to the current bracket year).

    `bracket_year` is the year whose tables actually produced the numbers, and
    `brackets_fallback` is True when that is NOT the requested year (no
    published table for it) — a caller can never mistake a fallback for an exact
    projection.
    """
    if year is None:
        year = FALLBACK_BRACKET_YEAR
    federal_brackets, provincial_brackets, bracket_year, used_fallback = resolve_brackets(year)

    # Capital gains: 50% inclusion
    taxable_capital_gains = capital_gains * Decimal("0.5")
    # Dividends: gross-up by 38% for eligible, 15% for non-eligible (simplified: use 38%)
    grossed_dividends = dividend_income * Decimal("1.38")
    # Eligible dividend tax credit: 15.0198% of grossed amount (federal)
    dividend_tax_credit = grossed_dividends * Decimal("0.150198")

    taxable_income = employment_income + taxable_capital_gains + grossed_dividends - deductions
    if taxable_income < 0:
        taxable_income = _ZERO

    federal_tax = compute_tax(taxable_income, federal_brackets)
    prov_brackets = provincial_brackets.get(province, provincial_brackets[DEFAULT_PROVINCE])
    provincial_tax = compute_tax(taxable_income, prov_brackets)

    # Apply dividend tax credit (federal only for simplicity)
    total_tax = federal_tax + provincial_tax - dividend_tax_credit
    if total_tax < 0:
        total_tax = _ZERO

    return {
        "taxable_income": taxable_income,
        "employment_income": employment_income,
        "taxable_capital_gains": taxable_capital_gains,
        "grossed_dividends": grossed_dividends,
        "deductions": deductions,
        "federal_tax": federal_tax,
        "provincial_tax": provincial_tax,
        "dividend_tax_credit": dividend_tax_credit.quantize(_CENT),
        "total_tax": total_tax.quantize(_CENT),
        "average_rate": (total_tax / taxable_income * 100).quantize(_CENT)
        if taxable_income > 0
        else _ZERO,
        "province": province,
        "bracket_year": None if used_fallback else bracket_year,
        "brackets_fallback": used_fallback,
        "brackets_fallback_year": bracket_year if used_fallback else None,
    }
