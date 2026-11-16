# 27 — Tax Projection

Project estimated tax liability for the current year based on realized gains, income, and deductions.

## Context

The tax engine (M5) computes realized gains and ACB. Tax projection extends this to estimate the total tax bill for the year, helping users plan estimated payments and understand their tax position before year-end.

## Decisions

1. **Projection, not filing.** Estimates tax liability based on realized gains and estimated income. Does not file taxes or generate official forms.

2. **Uses actual data + estimates.** Realized gains come from the tax engine. Income estimates come from recurring transactions (payroll). Deductions are user-entered.

2. **Year-aware Canadian tax brackets.** Bracket tables are keyed by TAX YEAR
   and sourced from the CRA's published "tax rates and income brackets" pages
   (`app/services/tax_projection.py::BRACKET_YEARS`). Tables for **2026** and
   **2025** ship today; the 2026 federal first band is 14% (14.5% in 2025, 15% in
   2024) — 2024 tables are no longer used, so a `year=2026` request is no longer
   priced with 2024 numbers. Adding a year is a data-only change: add a
   `_BRACKETS_<year>` table and register it in `BRACKET_YEARS` — no code
   change.

   **A year with no published table is never silently priced with another
   year's numbers.** `project_tax` falls back to `FALLBACK_BRACKET_YEAR` (2026)
   and reports the substitution explicitly in the response:
   `bracket_year` (the year actually used, `null` when it is a fallback),
   `brackets_fallback` (bool), `brackets_fallback_year` (the substituted year,
   `null` otherwise). A client can always tell an exact projection from an
   approximate one. Source URLs are pinned in the module docstring.

3. **Provincial split.** Federal + provincial (user selects province). Currently supports ON, BC, AB — each from the same tax year as the federal table. Other well-formed provinces return that year's Ontario brackets as fallback (decision 4).

4. **Input bounds (M47, docs/29 F7).** `province` is validated as exactly 2
   uppercase A-Z chars (`invalid_province` 422 otherwise — a 7-char string like
   `Ontario` previously overflowed the varchar(2) column as a bare 500).
   Unknown but well-formed 2-char codes still fall back to Ontario brackets.
   `year` must be 2000–2100 (`invalid_year` 422).

5. **Form-prefill contract (M47 contract addition).** `GET /taxes/projection`
   also returns a `stored` object — the raw stored row values for the year:
   `{year, province, estimated_income, deductions, capital_gains_override,
   dividend_income_override}`, Decimals serialized as strings, `null` for
   unset overrides (zeroed fallbacks when no row is saved yet). `PATCH` keeps
   its query-param shape, but an absent numeric param now means "keep the
   stored value" (the frontend sends only edited fields); the overrides accept
   `?capital_gains_override=` (empty value) as an explicit clear to null — a
   URL query cannot carry JSON null.

6. **Scenario support.** Users can model "what if I sell X shares" scenarios without executing trades.

## Schema

```sql
-- No new tables needed. Projection is computed from existing data + user inputs.
-- User inputs stored in a JSON settings column or separate table.
CREATE TABLE tax_projections (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    year INTEGER NOT NULL,
    province VARCHAR(2) NOT NULL DEFAULT 'ON',
    estimated_income NUMERIC(12,2) DEFAULT 0,
    deductions NUMERIC(12,2) DEFAULT 0,
    capital_gains_override NUMERIC(12,2),  -- NULL = use actual
    dividend_income_override NUMERIC(12,2),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, year)
);
```

## API

- `GET /taxes/projection?year=YYYY` — return projection with current data, computed from `year`'s brackets
- `PATCH /taxes/projection` — update user inputs (income, deductions, province); the returned projection uses `year`'s brackets
- `POST /taxes/projection/simulate` — **not implemented.** There is no such route and no caller in backend or frontend (see docs/08); what-if scenarios go through `GET /taxes/projection` with different stored inputs.

## Projection calculation

```
Taxable income = Employment income + Net capital gains (50% inclusion) + Eligible dividends (gross-up by 38%) - Deductions   (floored at 0)
Federal tax = Σ over the requested year's federal brackets of (income falling in each band × band rate)
Provincial tax = same, over the requested year's brackets for the selected province
Dividend tax credit = 15.0198% of the grossed-up dividends (federal only)
Total tax = Federal + Provincial - Dividend tax credit   (floored at 0)
Average rate = Total tax / Taxable income
```

`GET`/`PATCH /taxes/projection` additionally return `bracket_year` and
`brackets_fallback` (decision 2) so a caller can detect a year priced with
fallback tables.

## Frontend

- **Tax Projection page** (`/taxes/projection`) — extend Taxes page with Projection tab
- **Inputs form** — province selector, income estimate, deductions
- **Form prefill + partial save (M47, docs/29 F9).** The form prefills from
  `GET /taxes/projection`'s `stored` object (decision 5). Save sends ONLY the
  edited fields as query params — omitted params keep stored values, and a
  cleared override sends the empty value (clear-to-null). Failed saves toast
  via `apiErrorMessage`.
- **Projection summary** — estimated tax, average rate, marginal rate
- **Breakdown chart** — federal vs provincial, by income source
- **Scenario panel** — "what if I sell X" simulator

## Verification

- `GET /taxes/projection?year=2026` with `estimated_income=100000`, province
  `ON` → `federal_tax = "16696.00"`, `provincial_tax = "6940.47"`,
  `total_tax = "23636.47"`, `bracket_year = 2026`, `brackets_fallback = false`.
- `year=2025`, `estimated_income=120000`, `ON` → `federal_tax = "21446.25"`,
  `provincial_tax = "9097.60"`, `total_tax = "30543.85"`; the same income for
  `year=2026` returns `federal_tax = "20958.53"`, `total_tax = "29974.52"` — the
  two years are NOT priced from one table.
- `year=2026`, `estimated_income=50000`, `ON` → `federal_tax = "7000.00"`
  (50,000 × 14%), pinning the year-specific first-band rate.
- `year=2026`, `estimated_income=100000`, `BC` → `provincial_tax = "6642.38"`
  (BC's own 2026 table, first band 5.6%).
- `year=2026`, `province=ZZ` (well-formed, unknown) → 200, `province = "ZZ"`,
  `provincial_tax = "6940.47"` (Ontario fallback, unchanged from decision 4).
- `year=2031` (no published table) → 200 with `bracket_year = null`,
  `brackets_fallback = true`, `brackets_fallback_year = 2026` — the fallback is
  explicit, never silent.
- `estimated_income=0` → `total_tax = "0.00"` (no division by zero).

## Acceptance criteria

- [ ] Projection uses actual realized gains from tax engine
- [ ] Federal + provincial brackets apply correctly
- [ ] User inputs (income, deductions, province) persist
- [ ] Scenario simulator runs without saving
- [ ] Breakdown shows tax by source (employment, capital gains, dividends)
- [ ] Projection updates when new trades are recorded

## Priority: medium
## Effort: 3-4 days
## Dependencies: M5 (tax engine), M14 (dividends)
