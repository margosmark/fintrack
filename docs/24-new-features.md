# 24 — New Features (M25–M35)

Additional features added beyond the original M1–M24 roadmap.

## M25 — Budget tracking

Per-category monthly/yearly spending limits with visual progress bars on the Dashboard.

**Schema:** `budgets` table (migration 0017) — `user_id`, `category_id` (unique per user), `amount` (Decimal 12,2), `period` (monthly|yearly).

**API:**
- `GET /budgets` → list with computed `spent`, `remaining`, `pct_used` for current period
- `POST /budgets` → create (409 `duplicate_budget` if category already budgeted)
- `PATCH /budgets/{id}` → update amount/period
- `DELETE /budgets/{id}` → remove

**Spent computation:** signed reporting-currency net spend over committed
transactions for the category in the current month (or year), floored at 0 —
outflows add, refunds offset, inflows never count (the old `SUM(ABS(amount))`
reading was wrong and was fixed by M47 F13; docs/08 §Budgets is the contract).
Rows without a `cad_amount` mirror price at the newest cached fx rate on or
before their date and an unresolvable row is excluded, never summed at face
value. Period membership uses booking `date` against `today_et()`.

**M48-S3 (client-side):** `GET /budgets` is still the only read path for
budget *definitions*, so the browser calls it — but `BudgetCards`/`Budgets`
recompute `spent`/`remaining`/`pct_used` locally
(`frontend/src/lib/s3Engine.ts::budgetsProgress`) from rows and categories it
already holds. Server and client must stay numerically identical (golden
`budgets.progress`).

**Frontend:** `BudgetCards` component on Dashboard showing progress bars with color coding (green <80%, amber 80–100%, red >100%).

## M26 — Tax-loss harvesting alerts

Flags unrealized losses in taxable (non-registered) accounts with estimated tax benefit.

**API:** `GET /taxes/alerts` → positions at loss with `unrealized_loss_cad`, `estimated_tax_benefit_cad`, `superficial_loss_risk`.

**Logic:**
- Compares `market_value_cad` vs `book_cost_cad` from `compute_positions`
- Taxability comes from the position's account: `taxable` iff
  `type == brokerage_non_registered` (docs/02) — loaded once per call
  (M47, docs/29 F2; the old `pos["taxable"]` key never existed)
- Threshold: unrealized loss > $100 CAD
- Superficial loss risk: checks for buys within ±30 days (docs/06 rule)
- Tax benefit estimate: 50% inclusion × 30% marginal rate = 15%

**Frontend:** `TaxAlertsPanel` on Taxes page showing loss table with risk badges.

## M27 — Subscription tracker

Auto-detects recurring transactions from committed spending.

**M96 split (docs/09 §Recurring prompt):** this M27 tracker is the *reporting*
heuristic — a loose ≥2-occurrence scan. The M96 pass is the *tagging*
heuristic on `/transactions`: stricter (≥3 occurrences, ±15% amount band,
every gap fitting a cadence) because it WRITES to the ledger
(`transactions.is_recurring` + cadence + next date) and can be declined for
good. Both are local-only, both group by the same
`services/subscriptions.py::normalize_payee_key`, and neither makes a network
call.

**M138 — one detector for both surfaces (docs/09 §Bills you probably have).**
service. The loose rule is no longer dashboard substrate. M133 proved it was
the single source of both of Mark's complaints: FILM.CA ×2 / LOBLAW ×53 /
TIM HORTONS ×95 / SHELL ×18 presented as bill suggestions (4 of 9 rows were
not bills) and no way to dismiss them. `SubscriptionsPanel` now reads the
**strict** engine via `GET /transactions/recurring/candidates?source=subscriptions`,
the card gained cap-5 + `Show all`, a per-row `Not a bill`, a header
`Dismiss all (N)` and a 6-second Undo toast with permanent per-payee
suppression, and `listSubscriptions` (M137's thin read of the re-mounted
route) has no `src/` consumer left. The ≥2 rule below survives as the shape of
the DETECTOR — the client port (`s3Engine.subscriptionsReport`) retired with
M160 (P2-5), so the route is the only implementation and the golden pins it —
`docs/33` A12.

**API (M48-S3 ... M137 P1-2):** `GET /subscriptions` was REMOVED and detection
moved client-side to `frontend/src/lib/s3Engine.ts::subscriptionsReport`; the
route was then RE-MOUNTED as a thin server read and the client port was
retired with M160 (P2-5). The browser consumes the same
`{subscriptions: [{payee, frequency, avg_amount, annual_cost, occurrences,
last_date, next_expected}], total_monthly, total_annual}` payload the route
computes from committed rows. Contract + arithmetic: docs/08 §Subscriptions.

Where each old guarantee is pinned now: the route (mounted again since M137
P1-2) in `backend/tests/test_subscriptions.py`; the arithmetic and the port
edges (`posting_date` fallback/preference, half-even `avg_amount`, the
unquantized `annual_cost` multiplier, the 13-month window boundary, tie
order) in the frozen golden `tests/fixtures/s3-vectors.json::subscriptions.detect`,
replayed against the route by `backend/tests/test_m160_replay_s3.py` — the
`s3ClientCompute` client port retired with M160.
`backend/app/services/subscriptions.py` is **not mounted as a route** — it stays
as the reference implementation the TS port mirrors, and
`tests/test_m29_aggregates.py` still pins its staging/window semantics. The one
server-side caller since M55 is `POST /bills` (docs/25): it resolves the optional
`from_subscription_id` through this same user-scoped scan, so a subscription id
that is not in the requesting user's detected set is a 404. No route, no second
detector, no change to the rules below.

**Detection:**
- Groups transactions by normalized payee (uppercase, strip trailing numbers)
- Requires ≥2 occurrences
- Frequency from average gap: weekly (≤10d), biweekly (≤20d), monthly (≤40d), quarterly (≤100d), yearly
- Annual cost: `avg_amount × frequency multiplier`
- Scan is bounded to the trailing 13 calendar months (relative to
  `today_et()`, M47 docs/29 F1/F10) and uses the shared committed-only
  staging filter

**Frontend (M138):** `SubscriptionsPanel` on Dashboard, headed **"Bills you probably have"**, showing strict-detector suggestions with neutral cadence badges, capped at 5 rows with `Show all`, a per-row **"Not a bill"** and a header **"Dismiss all (N)"** (6s Undo toast, permanent per-payee suppression). M55 (docs/25) adds a per-row **"Create bill reminder"** action — rendered on detected candidates only — which hands the draft to `/bills?autofill=…` (`weekly`/`biweekly`/`four_weekly` rows refuse with a toast instead: no bill cadence).

**M170 — the detector's output is a CANDIDATE everywhere, and it now refuses
statement-generic descriptors (docs/36 §M170).** The cash-flow calendar stopped
reading this module: its `subscription` series now comes from
`services/confirmed_subscriptions.py` (active bills plus the recurring groups
the user accepted), so a guess can no longer render on a surface that reads as
fact — the defect was a guessed `Google*Youtubeprmium` under Mark's real
YouTube bill plus a bare `Electronic Funds Transfer −$359.08`. What is left
here is candidate-shaped for both of its callers — `POST /bills`' autofill
resolution of `from_subscription_id` (docs/25) and the re-mounted
`GET /subscriptions` (docs/08) — and `detect_subscriptions` now skips any row
whose `normalize_payee_key` output starts with one of the module tuple
`STATEMENT_GENERIC_PREFIXES` — `ELECTRONICFUNDSTRANSFER`, `EFT`,
`INTERACE-TRANSFER`, `INTERACTRANSFER`, `E-TRANSFER`, `TAXPAYMENT`, `PAYROLL` —
before any amount or cadence fit is considered: a bank statement prints the
transfer TYPE in the payee column, so no part of the descriptor is a merchant
identity, and a rent-sized monthly transfer is still a transfer. Scope is this
detector only — the M96/M138 prompt surfaces (`services/recurring.py`) are
deliberately untouched and still show such candidates, and a group the user
accepts is confirmed state regardless of this list.

## M28 — Spending velocity insights

Month-over-month deltas, daily burn rate, and per-category trends.

**API:** `GET /reports/insights` → `current_month_spending`, `previous_month_spending`, `month_over_month_pct`, `daily_burn_rate`, `projected_month_end`, `category_deltas`.

**Logic (M47, docs/29 F1/F1d):** all transaction sums use the shared
committed-only staging filter; "today" is `today_et()` (America/Toronto),
and the current-month window keeps its exclusive upper bound at today
(today is never double-counted).

**M138 D-1 (M133's 2nd P1):** the headline is taken **per category** and a
category whose rows are all inflows is income — excluded from
`current_month_spending` and from `category_deltas` alike. Before it netted
every row in the window and floored at zero, so a month where income exceeded
spending printed `$0 / 0% / $0/day / projected $0` while the same response's
breakdown showed the real number. Refunds still offset (they share a category
with the purchase they cancel), and the breakdown still sums to the headline
by construction — both come from the one `_category_flows` reader.

**Frontend:** `InsightsCard` on Dashboard showing spending velocity with trend arrows and top category changes.

## M29 — Dividend calendar

Projects upcoming dividend income from historical payment patterns.

**API:** `GET /reports/dividend-calendar` → `upcoming` (per-security payment projections) + `summary` (next-payment subtotals by denomination; contract below).

**Detection:** Groups committed/manual dividend, distribution and return-of-capital trades by security, detects frequency from date gaps, projects next payment (M47, docs/29 F1c: Security lookup is user-scoped and the trade history scan is bounded to the trailing 13 months; "today" is `today_et()`).

**Frontend:** Per-security table with frequency badges and next expected dates. (Mounted on the `/reports` "dividends" tab below the paid-dividends panel, 2026-09-28 — the component had shipped unmounted since M29; QA finding.)

### Denomination ruling — implemented; independent QA pending (t_a8657d69)

**Decision: retain the summary strip as per-currency subtotals, never one
cross-currency number.** This preserves useful upcoming-income information
without inventing equivalence between CAD and USD or introducing an FX read.
Dropping the strip loses useful information; converting it requires a separate
FX/date/freshness decision and is outside this change. Neither this route nor
the cash-flow calendar may read even cached FX for these projections (docs/36
decision 6); reporting-currency settings do not change their denomination.

**Pre-change gap (verified at origin/main `7726644`):** the service averaged
`price_native` without carrying its currency and summed those means across
currencies; the panel formatted averages, annual projections and scalar
summaries as CAD. The approved replacement contract below is now implemented
by the M29 successor after the cash-flow row-denomination seam landed. Runtime
verification and deployment remain the responsibility of the independent QA
lane; this status does not claim QA approval.

**Row authority and refusal:** resolve each contributing trade as
`trade.currency or account.currency` (docs/02 M132, the same convention as
`services/reports.py`). Validate the resolved code with the existing stated-code
rule (trim, uppercase, three ASCII letters). Do not substitute the security's
listing currency or infer a currency from `fx_rate_used`. A security has one
projected money pair only if ALL rows used in its mean resolve to the SAME
valid code. For mixed codes or any unnameable row, keep the security, frequency,
dates and payment count, but set `currency`, `avg_amount` and
`annual_projection` ALL to JSON `null`. Do not discard inconvenient rows,
split a security into separate predictions, choose its newest currency, or
repair source data on a read. Account joins/lookups remain caller-scoped.

**Wire contract (all money is a two-decimal Decimal string or explicit null):**

```json
{
  "upcoming": [{
    "security_id": 1,
    "symbol": "EXAMPLE",
    "frequency": "quarterly",
    "currency": "USD",
    "avg_amount": "85.00",
    "annual_projection": "340.00",
    "last_payment_date": "2026-07-20",
    "next_expected_date": "2026-10-19",
    "num_payments": 4
  }],
  "provenance": {
    "status": "ok",
    "as_of": "2026-07-20",
    "sources": [{
      "key": "income_trades",
      "kind": "ledger",
      "label": "recorded income trades",
      "provider": null,
      "as_of": "2026-07-20",
      "age_days": null,
      "status": "ok",
      "detail": "Projected from 4 recorded income payment(s) since 2025-10-01 in your ledger — recorded trades, not a market-data feed. Projections are next-payment estimates, not forecasts."
    }]
  },
  "summary": {
    "this_month": null,
    "this_quarter": null,
    "this_year": null,
    "by_currency": [{
      "currency": "USD",
      "this_month": "85.00",
      "this_quarter": "85.00",
      "this_year": "85.00"
    }],
    "unresolved_counts": {"this_month": 0, "this_quarter": 0, "this_year": 0}
  }
}
```

- `upcoming` keeps its existing date ordering and fields (`frequency` remains
  `monthly|quarterly|yearly|unknown`; dates are ISO `YYYY-MM-DD`, counts/IDs
  integers). `currency` is added; both existing money fields become nullable.
- `summary.by_currency` is a list sorted ascending by code, one row per known
  projected denomination, with all three period keys zero-filled as `"0.00"`
  when that currency has no eligible payment in the period. An empty history or
  only unresolved projections yields `by_currency: []`; there is no invented
  CAD zero bucket.
- The legacy scalar `summary.this_month/this_quarter/this_year` keys stay present
  but are ALWAYS `null`, including single-currency and empty histories. This
  deliberately retires their numeric meaning: old CAD-only clients render a
  dash, not a false total. No blended total or default-currency fallback exists.
- Each eligible, resolvable security contributes its historical arithmetic mean
  to its OWN currency's bucket. Preserve existing Decimal precision and
  half-even cent quantization: sum unrounded means within each bucket, then
  quantize once. Per-entry averages and annual projections keep their existing
  same-currency formulas/rounding. No currency conversion or external request.
- `summary.unresolved_counts` contains three non-negative integer counts of
  eligible security projections withheld for mixed/unknown denomination, not
  counts of historical trades. They are excluded from money subtotals; a
  partial subtotal must never claim to be complete.
- `provenance` (docs/07 §Dividend-view provenance) names the ONE source these
  projections come from: `income_trades`, a ledger source whose `as_of` is the
  newest recorded payment inside the trailing window and whose `status` is
  `ok` or `missing` — a recorded fact is never stale, so `age_days` is always
  `null` and no `stale` verdict can appear. The calendar reads NO market data
  (no FX, no provider), so it lists no `market_data` source rather than
  implying one; `DividendCalendarPanel` renders the strip above the table and,
  when `upcoming` is empty, above the empty state — "nothing recorded" and "no
  source ever answered" must be distinguishable. Additive: `?format=csv`
  serializes `upcoming` rows only and never carries the block.
- This ruling changes denomination, NOT schedule arithmetic or period
  membership: retain the existing month/year equality checks and the legacy
  `this_quarter` check `(next_expected_date - today).days <= 90` (including its
  lack of a lower bound). Summaries count ONE next expected payment per
  security, not annualized income or every recurrence. Frequency/date/window
  redesign is out of scope; `annual_projection` remains the separate estimate.
- Endpoint authentication, tenancy, staging and own-only scope remain unchanged.
  No request parameters, persistence, migrations or new error codes are added;
  mixed/unknown currency is a successful 200 with null money, not a route error.

**Panel contract:** show a visible `Estimated income by currency — no FX
conversion` explanation. Keep the three period headings; list each currency's
subtotal separately with its explicit ISO code, including CAD (a bare `$` is
not enough). Both Avg Amount and Annual also carry the row's explicit code.
Use the existing native `formatMoney` and mask subscription; never `formatCad`
on this surface. Unresolved money renders `—` with the visible explanation
`Mixed or unknown currency`; preserve its non-money row fields. For each period
with unresolved entries show `Excludes N projections with mixed or unknown
currency`. If there are no known buckets, use `—`, not `$0.00`; retain the
existing no-history state when `upcoming` is empty. Amounts mask/unmask
immediately without fetching; currency codes, dates and omission counts remain
visible. A null currency must never be passed to a CAD/default formatter.

**Row-currency export — `GET /reports/dividend-calendar?format=csv`:** the
route's flat twin for the spreadsheet consumer, one row per projection. `format`
is the same validated `json` (default) | `csv` literal the other report twins
publish: anything else is a **422 `validation_error` located at `query.format`**,
and `scope` keeps its own-only singleton (422 at `query.scope`). `format=csv`
answers **200 `text/csv`** with `Content-Disposition: attachment;
filename="dividend-calendar-<today_et() as YYYY-MM-DD>.csv"` and a header row
pinned to the JSON field names in wire order:
`security_id,symbol,frequency,currency,avg_amount,annual_projection,last_payment_date,next_expected_date,num_payments`.

- It serializes `compute_dividend_calendar`'s own `upcoming` list — the SAME
  computation as the JSON twin, never a second reader — so the file's rows and
  the on-screen `summary.by_currency` subtotals are one population, and
  staged/other-tenant rows are absent from both surfaces alike.
- `avg_amount` and `annual_projection` keep the two-decimal money string
  (`85.00`, never `85`); dates stay ISO, counts stay integers, and `currency`
  travels on every row that carries a figure.
- A withheld projection empties `currency`, `avg_amount` and `annual_projection`
  TOGETHER — the same all-or-nothing refusal the JSON contract and the panel
  render as the dash (docs/24 "Row authority and refusal").
- The file carries **no total row and no summary block**: the per-currency
  subtotals exist only in JSON, so no export can print one figure across
  denominations. No FX read, no conversion, no persistence, no new error code.
- The reconciliation gate is `backend/tests/test_dividend_calendar_export_reconciliation.py`:
  every subtotal is RE-DERIVED from the exported rows plus this section's period
  predicates (never by calling the service twice), the cash-flow calendar's
  dividend event is tied to the exported `(amount, currency)` cells, and both
  surfaces are made to fail loudly if they consult an FX reader.

**Touch points / sequencing:** the cash-flow dividend fix `t_c1cf39ca` landed
the shared per-entry denomination seam first. This successor consumes that
resolver/entry currency to implement the summary shape, frontend API type and
panel; it updates the prior scalar-summary assertions and reconciles this
section's runtime status. The cash-flow event consumes the SAME entry
amount/currency pair (docs/36); no second resolver in the panel or composer.
Independent QA remains assigned downstream.

**Verification required for implementation and independent QA:**
1. Fixed-date committed CAD `118.40` and USD `85.00` next-payment means eligible
   in all three windows produce separate CAD `118.40` and USD `85.00` buckets,
   never `203.40` in any currency. A USD-only history still has null legacy
   scalar keys. Raw JSON money must be strings, not floats.
2. Explicit USD trade currency on a CAD account resolves USD; legacy NULL trade
   currency on a CAD account resolves CAD even for a USD-listed security.
   Listing currency and reporting-currency preference cannot override either.
3. One security with CAD + USD history, or one valid + one unnameable row,
   withholds ALL three money fields; its dates/count remain. Each eligible
   period's unresolved count increments once and known buckets stay intact.
4. Pin empty history, unresolved-only history, same-currency mean rounding,
   zero-filled periods, deterministic code ordering, staged trade exclusion
   and a second tenant whose rows must not affect either entries or buckets.
5. Patch FX/cache readers and provider fetches to fail: both report routes still
   return 200 without reaching them. The cash-flow dividend event equals the
   M29 entry's amount/currency pair, including withheld nulls.
6. Component tests and a running-stack browser probe show explicit CAD/USD on
   Avg Amount, Annual and all subtotal lines, the partial/unknown explanations,
   and an instant mask flip without network calls. Check light/dark and narrow
   viewports; do not weaken existing mask/null/calendar parity tests.
7. Run the relevant regression modules plus local backend/frontend gates and
   deployed endpoint/browser probes, recording exact results and deployed SHA.
   A docs-only decision does not attest that this runtime contract has shipped.

## M30 — Spending comparison

Period-over-period spending comparison with per-category deltas.

**API:** `GET /reports/spending-comparison?current_start=...&current_end=...` → both periods' totals with `deltas` (per-category change amount and percentage). Previous period auto-computed if omitted.

**Frontend:** `ComparisonPanel` showing previous vs current totals with color-coded delta table.

## M31 — Notification settings

User-configurable alert settings for various event types.

**Schema:** `notification_settings` table (migration 0018) — `user_id`, `alert_type`, `enabled`, `config` (JSON).

**Alert types:** `large_transaction`, `low_balance`, `dividend_received`, `price_alert`, `monthly_summary` — **the M31 five have no generator and nothing reads their `enabled`** (they seed `enabled=false`). **M51 (docs/35) added the three generator-backed types** `budget_threshold`, `bill_due`, `tax_loss` (seed `enabled=true`) and the in-app inbox.

**API:**
- `GET /notifications` → list settings (auto-creates any missing row on first call)
- `PATCH /notifications/{id}` → toggle enabled/disabled, toggle `delivery_enabled`, update config
- `GET /notifications/inbox`, `POST /notifications/inbox/{id}/read`, `POST /notifications/inbox/read-all` → M51 in-app inbox (docs/35, docs/08)
- `GET /notifications/channels`, `PATCH /notifications/channels/{id}` → M59 transports (docs/37, docs/08)
- `GET /notifications/deliveries`, `POST /notifications/deliveries/run`, `POST /notifications/deliveries/{id}/retry` → M59 outbox + dead-letter surface (docs/37, docs/08)

**Frontend:** `NotificationsSection` in Settings with toggle switches per alert type.

## M59 — Notification delivery channels (email + webhook)

Outbound delivery for the M51 inbox: two transports behind one interface, an
auditable outbox with retry/backoff/dead-letter, and the privacy-mode
resolution (in-app only by default; explicit per-channel opt-in).

**Schema:** `notification_settings.delivery_enabled` (migration 0030) plus
`notification_channels`, `notification_deliveries`,
`notification_delivery_attempts`.

**API:** `GET /notifications/channels`, `PATCH /notifications/channels/{id}`,
`GET /notifications/deliveries?limit=&offset=&status=`,
`POST /notifications/deliveries/run`,
`POST /notifications/deliveries/{id}/retry` (docs/08, docs/37).

**Transport contract (docs/37):** exactly five payload fields
(`alert_type`, `severity`, `occurred_at`, `item_key`, `link`) — `title`,
`body` and `amount_cad` never leave the machine, and the email subject is a
constant for every type. Webhook bodies are HMAC-SHA256 signed over
`timestamp.body` with a per-user key derived from `NOTIFY_WEBHOOK_SECRET`
(fallback `JWT_SECRET`).

**Frontend:** none — M59 is backend-only; a delivery UI card can build on these
routes later.

## M32 — Account reconciliation

Compare imported transactions against bank statements.

**Schema:** `reconciliation_items` table (migration 0019) — `account_id`, `transaction_id` (nullable), `statement_date`, `statement_amount`, `statement_description`, `status` (matched|unmatched).

**API:**
- `GET /accounts/{id}/reconciliation` → items + summary (matched/unmatched counts and sums)
- `POST /accounts/{id}/reconciliation/upload` → replace unmatched items with statement data
- `POST /accounts/{id}/reconciliation/match` → match a statement item to a transaction

## M33 — Goal-based savings tracker

Financial savings targets with progress tracking and monthly projections.

**Schema:** `goals` table (migration 0020) — `name`, `target_amount`, `target_date`, `account_id` (optional link), `current_amount_override`.

**Progress computation:**
- If `current_amount_override` set → use it
- If `account_id` linked → sum of committed transactions for that account
  (shared committed-only staging filter, docs/29 F1)
- `progress_pct` = current / target × 100
- `monthly_needed` = remaining / months to target
- `status`: on_track | behind | ahead | completed (≥100%)

**API:** Full CRUD at `/goals` with `GoalWithProgress` response. PATCH
re-validates `account_id` ownership exactly like create (404 on a foreign or
missing account, docs/29 F7).

## M34 — Net worth snapshot drill-down

Per-account cash balance breakdown at a specific historical date.

**API:** `GET /reports/networth-snapshot?target_date=YYYY-MM-DD` → `accounts` (per-account balance) + `total_cash`.

**Logic:** cash per account starts from `Account.starting_balance or 0`, then
`SUM(amount)` on committed transactions with booking `date` ≤ target_date
(M47, docs/29 F1/F1b: pending-review rows were previously counted and
`starting_balance` dropped; `posting_date` is nullable and never gates
period membership — docs/02). `total_cash` sums the corrected per-account
cash.

## M35 — Mobile-responsive layout

Touch-friendly layout for phone browsers.

- Sidebar remains available as a 76px icon rail on mobile (<768px); a fixed
  `MobileNav` bottom tab bar provides the primary navigation. Viewport-fixed
  editors, modals, and popovers portal to `document.body` so they layer above
  the `z-40` tab bar rather than being trapped in the route transform's stacking
  context.
- Tables convert to card layout on small screens
- KPI grid stacks vertically
- 44px minimum touch targets

## M36 — Goals management page

Full CRUD interface for savings targets at `/goals`.

**Features:**
- Create/edit/delete goals with name, target amount, target date
- Progress bars with percentage and monthly-needed calculation
- Status indicators: on_track, behind, ahead, completed
- Link to accounts for auto-tracking or manual override

**Frontend:** `Goals.tsx` page with goal cards, create/edit form, inline delete.

## M37 — Budget management page

Full CRUD interface for spending limits at `/budgets`.

**Features:**
- Create/edit/delete budgets per category
- Monthly/yearly period selection
- Progress bars with color coding (green/amber/red)
- Category picker from existing categories

**Frontend:** `Budgets.tsx` page with budget cards, create/edit form, inline delete.

## M38 — Currencies route fix

Added missing `/currencies` route in App.tsx. The Currencies page component existed but had no route, making it inaccessible.

## M39 — Account Reconciliation page

Full reconciliation interface at `/reconciliation/:accountId`.

**Features:**
- Upload bank statement data (CSV format: date, amount, description)
- View matched/unmatched statement items
- Summary cards showing total/matched/unmatched/difference
- Reconcile link from Account Detail page

**Frontend:** `Reconciliation.tsx` page with upload form, statement items table, summary cards.

## M40 — Bill Reminders page

Full CRUD for recurring bills at `/bills`.

**Features:**
- Create/edit/delete bills with name, amount, frequency, due date
- Mark paid advances next due date automatically
- Upcoming bills summary card (next 7 days)
- Color-coded urgency: overdue (red), due soon (amber), upcoming (neutral)

**Frontend:** `Bills.tsx` page with bill list, create form, mark-paid action.

## M41 — Spending Alerts

Budget threshold notifications at `/budgets/alerts`.

**Features:**
- Auto-detect when spending crosses 80% (warning) or 100% (critical) thresholds
- Daily dedup prevents spam
- Acknowledge alerts to clear them
- Dashboard banner for critical alerts — **[SHIPPED 2026-09-28, M51]** `pages/Dashboard.tsx` renders it from the inbox's unread `budget_threshold` `severity=critical` items, so it respects the `budget_threshold` toggle server-side (docs/35)

**Backend:** `spending_alerts` model + API with threshold detection.

## M42 — Tax Projection page

Estimate annual tax liability at `/tax-projection`.

**Features:**
- CRA federal + provincial tax brackets (ON, BC, AB)
- Employment income, capital gains, dividend income inputs
- Deductions field
- Projection shows taxable income, federal/provincial tax, average rate

**Backend:** `tax_projection` service with bracket computation.

## M43 — Investment Benchmarks

Compare portfolio vs market indexes (SPY, XIU, XBB, XAW).

**Backend:** `benchmarks` service with yfinance price fetching and TWR comparison.

## M44 — Debt Tracking page

Track mortgages, loans, credit cards at `/debts`.

**Features:**
- CRUD for debts with type, interest rate, minimum payment
- Payoff projection (months, total interest, total paid)
- Total debt and interest summary cards
- Linked to accounts for balance tracking

**Backend:** `debts` model + API with amortization calculation.

## M45 — Demo dataset lifestyle coverage

Deterministic demo seed for the lifestyle pages (full spec: docs/30):
5 monthly bill reminders mirroring seeded payees, 3 monthly budgets,
2 debts (credit card + student loan), 2 goals — all dates relative to
today, all guarded per-table so re-runs and the pre-M45 backfill path
are no-ops.

## M46 — Rule preview & targeted apply

Dry-run any rule draft (count + sample, never writes) and backfill one rule
over history (`POST /rules/{id}/apply` with `all | range | forward` modes and
an `overwrite` flag). Since M48-S3 the dry-run runs client-side —
`frontend/src/lib/s3Engine.ts::previewRule`, an exact port of the removed
`POST /rules/preview` route (which now answers 405). Full spec: docs/31.
