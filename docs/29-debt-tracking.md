# 29 — Debt Tracking

Track mortgages, credit cards, loans, and other debts with payoff projections.

## Context

Many users carry debt (mortgage, car loan, student loans, credit cards). Tracking debt alongside assets gives a complete financial picture. The existing net worth calculation includes debt as negative cash, but doesn't track payoff schedules or interest.

## Decisions

1. **Linked to accounts.** Each debt is linked to an existing account (e.g., mortgage account, credit card account). The account's balance represents the current debt.

2. **Interest tracking optional.** Users can optionally enter interest rate and minimum payment. If not entered, the debt is tracked but not projected.

3. **Payoff projection.** Given interest rate and payment amount, project payoff date and total interest paid. Uses amortization formula.

4. **Snowball/avalanche views.** Show two payoff strategies: snowball (smallest balance first) and avalanche (highest interest first). Helps users choose a strategy.

5. **No payment scheduling.** Debt tracking is informational — it shows where you are, not when to pay. Payment scheduling is a future feature.

## Schema

```sql
CREATE TABLE debts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name VARCHAR(200) NOT NULL,
    debt_type VARCHAR(50) NOT NULL,  -- mortgage, credit_card, loan, student_loan, other
    original_amount NUMERIC(12,2),
    interest_rate NUMERIC(5,2),      -- annual % (nullable for 0% interest)
    minimum_payment NUMERIC(12,2),
    payment_frequency VARCHAR(20),   -- monthly, biweekly
    start_date DATE,
    end_date DATE,                   -- expected payoff date
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    -- Note: UNIQUE constraint not yet in migration; add in future migration
);
```

## API

- `GET /debts` — list all debts with current balances
- `POST /debts` — create debt record
- `PATCH /debts/{id}` — update debt record
- `DELETE /debts/{id}` — delete debt record
- `GET /debts/projection` — payoff projections for all debts (`payment_frequency`-aware, see Payoff calculation)
- `GET /debts/projection/{id}` — **not implemented.** There is no single-debt schedule route; the all-debts projection is the only one. (No caller in backend or frontend.)

## Payoff calculation

```
Periods per year = 12 when payment_frequency = monthly, 26 when biweekly (default monthly when NULL)
Period interest   = Balance × (Annual rate / 100 / Periods per year)
New balance       = Balance + Period interest - Payment
Payment count     = number of periods until the balance reaches 0
Payoff months     = ceil(Payment count × 12 / Periods per year)   -- whole calendar months
Total interest    = Σ period interest
Total paid        = Original amount + Total interest
```

Contract details (pinned by `tests/test_debts.py`):

- **Frequency is honored.** A `biweekly` plan accrues 26 times a year, not 12 —
  a plan that previously projected double the time and roughly double the
  interest. `GET /debts/projection` echoes `payment_frequency` and
  `periods_per_year` on every row so the UI can label the timeline.
- **0% rate.** `Payment count = ceil(balance / payment)` and
  `Total paid = balance` (the final payment is partial). Reporting
  `payment × count` would overstate cash out — e.g. 13 × $1,000 = $13,000 for a
  $12,345.67 balance — and contradicted the interest-bearing branch's
  `balance + interest` invariant. `Total paid` now means the same thing in both
  branches.
- **Never amortizes.** `months = -1`, `total_interest = "infinite"`,
  `total_paid = "infinite"` when the payment cannot cover one period's interest,
  **or** when the balance outlives the 50-year amortization horizon
  (`MAX_AMORTIZATION_YEARS`, scaled to `50 × periods_per_year`). The horizon is
  expressed in YEARS, not a flat period count: a 30-year biweekly mortgage needs
  780 payments and must not be reported as infinite.
- **Negative inputs are rejected at the boundary.** `original_amount` and
  `minimum_payment` are `>= 0` on both create and update; a negative payment is
  422, never a projection of "paid off in 0 months" (docs/29 F7).

## Frontend

- **Debts page** (`/debts`) — list of debts with balances, rates, payoff dates
- **Payoff chart** — line chart showing balance declining over time
- **Strategy comparison** — snowball vs avalanche payoff timelines
- **Dashboard widget** — "Total Debt" card with payoff progress
- **Net worth integration** — debts factored into net worth calculation

## Acceptance criteria

- [ ] Debt CRUD works with tenancy isolation
- [ ] Payoff projection calculates correctly
- [ ] Snowball and avalanche strategies show different timelines
- [ ] Dashboard shows total debt and payoff progress
- [ ] Interest tracking optional (nullable fields)
- [ ] Payoff date updates as payments are made

## Priority: medium
## Effort: 3-4 days
## Dependencies: M9 (accounts)
