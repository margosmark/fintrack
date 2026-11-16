# 25 — Bill Reminders

Track upcoming bills and recurring payments with due-date notifications.

## Context

Users need to know what bills are coming up and when they're due. The existing subscription tracker detects recurring payments but doesn't track due dates or send reminders. Bill reminders fill this gap by letting users explicitly set due dates for known bills.

## Decisions

1. **Manual entry, not auto-detect.** Users explicitly create bill reminders with name, amount, due date, and frequency. Auto-detection from transactions is a future enhancement — the subscription tracker already does pattern detection; bill reminders are for known, fixed-date obligations. (M55 below adds a one-way bridge *into* the manual form; it does not weaken this decision — nothing is created unless the user submits the form.)

2. **Linked to categories, not accounts.** Bills are categorized (e.g., "housing", "utilities") but not tied to specific accounts — the same bill might be paid from different accounts.

4. **List view with urgency badges.** Sorted list showing upcoming bills with countdown badges (overdue/due soon/upcoming).

4. **No payment tracking.** Bill reminders are informational — they tell you what's coming, not track whether you paid. Payment tracking is handled by transactions.

5. **Reminder thresholds.** Show bills due within 7 days prominently, 30 days as upcoming, beyond 30 days as future.

## Schema

```sql
-- migration 0021
CREATE TABLE bill_reminders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name VARCHAR(200) NOT NULL,
    amount NUMERIC(12,2),           -- estimated amount (nullable for variable bills)
    category_id INTEGER REFERENCES categories(id),
    frequency VARCHAR(20) NOT NULL, -- monthly, quarterly, yearly, one_time
    next_due_date DATE NOT NULL,
    remind_days_before INTEGER DEFAULT 3,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

No table, column, or migration is added by M55 (see below). **M116 adds three
columns and one migration** — see §M116 Schema.

## API

- `GET /bills` — list upcoming bills sorted by next_due_date
- `POST /bills` — create bill reminder
- `PATCH /bills/{id}` — update bill reminder
- `DELETE /bills/{id}` — delete bill reminder
- `POST /bills/{id}/mark-paid` — mark bill paid and advance due date

**M116 adds** (exact wire in docs/08 §Bill Reminders): `PUT /bills/{id}/rule`,
`GET /bills/{id}/detail`, `POST /bills/{id}/find-past`, `POST /bills/find-past`,
the match fields on `POST /bills`, and `payment_count` / `rolling_average` on
every `GET /bills` item.

### Error envelope (M47, docs/29 F7)

| code | HTTP | when |
|------|------|------|
| `category_not_found` | 404 | `category_id` missing or owned by another user (create AND update) |
| `invalid_due_date` | 422 | `next_due_date` not a valid calendar date (schema type is `date`; Pydantic rejects garbage) |

Category ownership is validated exactly like `create_budget`; `_advance_due_date`
operates on a typed `date` so `strptime` can no longer raise. (M180 replaced that
function with `services/bill_schedule.py::next_due`, which is typed end to end
and can no longer build an impossible date either — see §M180.)

## Frontend

- **Bills page** (`/bills`) — list view with urgency badges + upcoming summary card;
  its create/edit form carries the matcher's **Category scope** (`Category
  blank = any`, M123) and reads `/bills?prefill=transactions&…` (docs/09
  §Create from search)
- **Bill detail** (`/bills/:billId`, M116) — matched transactions, rolling
  averages and the period chart
- **Dashboard widget** — "Upcoming Bills" card showing next 7 days of bills with total
- **Sidebar** — Bills link in Tools section

---

## M55 — Subscription → bill draft autofill (M27 → M40 bridge)

### Context

Decision 1 still holds — nothing is auto-created. What M55 adds is the one-way
bridge the plan calls the "M27 → M40 bridge": when the subscription detector
(M27, `docs/24`) has already found a recurring charge, the user can seed a bill
reminder draft from it instead of retyping payee, amount, cadence and next due
date, then reviews and submits it like any other bill.

Detection itself is unchanged for THIS bridge and stays where M48-S3 put it:
in the browser (`frontend/src/lib/s3Engine.ts::subscriptionsReport`, `docs/08`
§Subscriptions) — still the source of the `/bills?autofill=…` hand-off shape.
(The Dashboard's subscriptions card stopped reading it in M138: it now queries
the strict detector, docs/09 §Bills you probably have.)
There is **no subscriptions table** (`docs/02`) and `GET /api/v1/subscriptions`
answers **404**, so "the user's own subscription row" cannot literally be a row.
A subscription is therefore identified by the detector's own grouping identity —
its `payee` — and resolved server-side through the single canonical
implementation both ports are pinned to
(`backend/app/services/subscriptions.py::detect_subscriptions`, mirrored to the
byte by `frontend/tests/fixtures/s3-vectors.json`).

### Decisions (M55)

6. **Autofill, never auto-create.** The subscriptions surface offers a "Create
   bill reminder" action; it only opens the M40 create form prefilled. No
   `bill_reminders` row is written until the user submits that form.

7. **`from_subscription_id` = `Subscription.payee`.** The detector groups by
   normalized payee, so its `payee` is unique within one user's subscription
   list and stable across runs — it *is* the subscription's identity. It is not
   a foreign key and no table is added. Length 1–200 (same bound as
   `bill_reminders.name`).

8. **Resolve through the one detector, never a second one.** On `POST /bills`
   the server calls `detect_subscriptions(db, current_user.id)` — the same
   user-scoped, committed-only, trailing-13-month scan the browser runs — and
   matches the id against that list. An id that is not in *this* user's
   detected set is `404 subscription_not_found`: tenancy holds by construction,
   because another user's subscription is never in this user's list. No new
   detection logic, no change to M27's detection rules, no network (`docs/07`),
   no new table.

9. **Cadence mapping is total or it fails loudly.** Bill frequencies are
   `monthly | quarterly | yearly | one_time` (schema above); detected
   frequencies are `weekly | biweekly | monthly | quarterly | yearly`
   (`docs/24` M27). Mapping is `monthly→monthly`, `quarterly→quarterly`,
   `yearly→yearly`. `weekly` and `biweekly` have no bill cadence → **422
   `unsupported_bill_cadence`**. No silent fallback to `one_time`, no silent
   drop of the cadence.

10. **Explicit request values win.** For each of `name`, `amount`, `frequency`,
    `next_due_date`: a value the request explicitly set is used as-is (the user
    edited the prefill), otherwise the server derives it from the subscription.
    The rule is Pydantic `model_fields_set` — the same exclude-unset semantics
    `PATCH /bills/{id}` already uses — so a client may send the id plus a subset.

11. **`name`, `frequency`, `next_due_date` are conditionally required.** They
    stay required exactly when `from_subscription_id` is absent (a
    `model_validator` enforces it), so the no-autofill contract — including its
    `422 validation_error` envelope for a missing field — is unchanged.

### API (M55)

`POST /api/v1/bills` — M40 shape plus one field. Response is unchanged
(`201` + `BillReminderPublic`).

| field | type | required | M55 notes |
| --- | --- | --- | --- |
| `from_subscription_id` | string, 1–200 | no | detector `payee` of the subscription seeding the draft; resolved per decision 8 |
| `name` | string, 1–200 | **iff `from_subscription_id` absent** | derived from `payee` when omitted |
| `amount` | decimal string \| null | no (as before) | derived from `avg_amount` when omitted |
| `frequency` | `monthly\|quarterly\|yearly\|one_time` | **iff `from_subscription_id` absent** | derived per decision 9 when omitted |
| `next_due_date` | `YYYY-MM-DD` | **iff `from_subscription_id` absent** | derived from `next_expected` when omitted |
| `category_id`, `remind_days_before`, `enabled` | as M40 | no | never derived — always taken from the request |

Minimal autofill request (everything else defaulted):

```json
POST /api/v1/bills
{"from_subscription_id": "NETFLIX.COM"}
```

→ `201 {"id": 12, "user_id": 1, "name": "NETFLIX.COM", "amount": "15.99",
"category_id": null, "frequency": "monthly", "next_due_date": "2026-10-18",
"remind_days_before": 3, "enabled": true}` — the four derived values are whatever
the detector projects for that subscription at request time.

#### Error envelope (M55 additions)

| code | HTTP | when |
|------|------|------|
| `subscription_not_found` | 404 | `from_subscription_id` does not resolve to a subscription detected **for the requesting user**: unknown id, another tenant's subscription, or a payee group that no longer meets the ≥2-occurrence / 13-month window. Message: `Subscription not found`. |
| `unsupported_bill_cadence` | 422 | resolved subscription's cadence has no bill equivalent (`weekly`, `biweekly`). Message: `Subscription cadence has no bill reminder equivalent`. |
| `subscription_next_due_missing` | 422 | resolved subscription projects no `next_expected`. Message: `Subscription has no next expected date`. Defensive: unreachable with the current detector (it always projects one from ≥2 occurrences) — pinned so a future detector change can never turn into a bare 500. |
| `category_not_found` | 404 | unchanged (M47) |
| `validation_error` | 422 | unchanged: schema-layer rejection, including a missing `name`/`frequency`/`next_due_date` when `from_subscription_id` is absent. A defensive (unreachable) guard in `create_bill` re-raises the same code rather than letting a NULL draft field reach the table. |

Check order inside `create_bill`: `from_subscription_id` resolution (404) →
cadence mapping (422) → `next_expected` presence (422) → category ownership
(404) → insert.

Footnote on the pre-existing row above: the `invalid_due_date` label (M47 /
`docs/29` F7) names this same write-time rejection, but the envelope code the
API actually returns for a malformed date is `validation_error` — the schema
rejects it before the handler runs
(`backend/tests/test_m29_validation.py::test_create_bill_garbage_due_date_422`
asserts the 422). M55 does not change that behavior; the label drift into
`docs/29`/`docs/35` is reported separately rather than edited from this card.

### Frontend (M55)

- **Subscriptions surface** — `SubscriptionsPanel`
  (`frontend/src/components/reports/SubscriptionsPanel.tsx`, rendered on the
  Dashboard; there is no `/subscriptions` page, `docs/24` M27) gains a per-row
  **"Create bill reminder"** action, rendered on **detected subscriptions only**.
- **Cadence guard is client-side too.** For `weekly`/`biweekly` rows the action
  is shown but does not navigate: it surfaces "Weekly subscriptions can't become
  a bill reminder (monthly, quarterly or yearly only)" and writes nothing.
  `monthly|quarterly|yearly` rows navigate.
- **Hand-off URL** — the action navigates to
  `/bills?autofill=<payee>&amount=<avg_amount>&frequency=<mapped>&due=<YYYY-MM-DD>`,
  each value `encodeURIComponent`-encoded; all four params are written together.
- **Bills page** (`frontend/src/pages/Bills.tsx`) reads those params on mount,
  prefills the create form (name/amount/frequency/due date) and opens it. A
  malformed or partial param set degrades to the ordinary form: the amount falls
  back to blank when it is not a finite number, the frequency to `monthly` when
  it is not a bill frequency, the date to blank when it is not `YYYY-MM-DD`.
  The server remains the authority for whatever is submitted.
- **Submit** carries `from_subscription_id: <autofill>` whenever the form was
  autofilled (so the tenancy + cadence rules run server-side) plus whatever the
  user has in the form. Params are cleared after a successful create and on
  cancel; **editing an existing bill never sends `from_subscription_id`** — it
  is a create-only field.

### Touch points (M55)

| path | change |
| --- | --- |
| `backend/app/schemas/bill_reminders.py` | `from_subscription_id`; conditionally-required draft fields |
| `backend/app/api/bill_reminders.py` | `_resolve_subscription()` + derivation in `create_bill` |
| `backend/tests/test_bills_autofill.py` | new — contract tests below |
| `frontend/src/api/bills.ts` | `from_subscription_id?` on `BillPayload` |
| `frontend/src/components/reports/SubscriptionsPanel.tsx` | per-row action |
| `frontend/src/pages/Bills.tsx` | query-param prefill + submit wiring |
| `frontend/tests/billAutofill.test.tsx` | new — contract tests below |
| `frontend/tests/responsiveContracts.test.ts` | harness fix: the panel now needs a router context (its Toast was already mocked there) |
| `backend/app/services/subscriptions.py` | **module docstring only** (zero logic lines): "unreachable from the API" stopped being true |
| `docs/24` (M27 section) | bridge + "reachable again from `POST /bills`" note |
| `docs/08` §Bill Reminders | `POST /bills` request line |

Not touched: M27 detection rules (`services/subscriptions.py` behaviour,
`s3Engine.ts` detection), notifications (M51), calendar (M52), E2EE (M53).

### Verification (M55)

Backend (`backend/tests/test_bills_autofill.py`, real Postgres, isolated
`fintrack_m55_test`):

1. Seed a user with ≥2 same-payee committed charges in the 13-month window
   (monthly cadence) → `POST /bills {"from_subscription_id": "<payee>"}` →
   **201**, `name`/`amount`/`frequency`/`next_due_date` equal that same
   `detect_subscriptions()` output's `payee`/`avg_amount`/mapped
   cadence/`next_expected` (asserted against the detector, not literals).
2. Same request from a **second user** (the foreign id) → **404
   `subscription_not_found`**; the second user's `GET /bills` stays empty.
3. Unknown id (`"NOPE"`) → **404 `subscription_not_found`**.
4. User with a `weekly` detected subscription → **422
   `unsupported_bill_cadence`**; `biweekly` → same code.
5. Id present **and** `next_due_date` explicitly set → the explicit value wins;
   id present and `name` explicitly set → explicit `name` wins (decision 10).
6. No id + missing `name` → **422 `validation_error`** (decision 11 unchanged);
   existing M40/M47 tests stay green.

Frontend (`frontend/tests/billAutofill.test.tsx`):

7. Draft builder maps `monthly|quarterly|yearly` and refuses `weekly|biweekly`.
8. Param parsing: full set → prefilled draft; partial/garbage set → safe
   fallbacks (blank amount, `monthly`, blank date).
9. `SubscriptionsPanel` renders the action on detected rows; clicking a
   `monthly` row navigates to the documented `/bills?…` URL while a `weekly`
   row does not navigate and writes nothing.
10. Bills page with autofill params: form opens prefilled and the create request
    body carries `from_subscription_id`; an edit request body does not.

Stack smoke (running stack, throwaway non-demo account — `POST /bills` is a
demo-blocked mutation): dashboard → "Create bill reminder" on a detected monthly
subscription → `/bills` form prefilled → Create → the bill is listed on `/bills`
with the mapped cadence and next due date; then `POST /bills` carrying another
tenant's `from_subscription_id` → 404 envelope.

---

## M116 — Match rules, the past-payment finder, and the bill drill-down

### Context

M40 built a *reminder* — name, amount, cadence, due date — and M55 bridged the
detector into its create form. Nothing linked a bill to the money that actually
left the account: `bill_reminders` had no matching machinery, the Bills page
(205 lines) had no drill-down, and no query anywhere joined transactions to
bills. Mark's four asks were smarter matching rules, a drillable bill page, a
rolling average, and a bar chart of what was actually paid.

The design constraint that decides everything else: **the link must be derived,
not stored.** A copy table of "transactions of this bill" goes stale the moment
a payee is re-normalised, an amount corrected, or the rule itself is edited —
and a stale linkage on a financial page is worse than no linkage at all.

### Decisions (M116)

1. **Every bill has a rule, from the first write.** `match_mode`
   (`contains | starts_with | exact`) + `match_term` (1–120 chars) +
   `amount_tolerance` (nullable), with the bill's existing `category_id` reused
   as the category scope. `match_term` is NOT NULL: a bill with no term cannot
   answer "how many payments did I find". It is derived from `name` when the
   request omits it — an M55 autofill already sets `name` to the detector's
   payee, so the two agree out of the box — and migration `0044` backfills
   existing rows from `left(name, 120)`.
2. **The whole rule must hold.** A transaction matches when the normalised
   payee matches per mode **AND** (if the bill has a `category_id`) its category
   equals it **AND** (if the bill has an `amount`) its amount is within the band.
   `amount_tolerance: null` means the amount must match **exactly** — that is
   what "no tolerance" means; a tolerance of `±T` accepts both directions.
   A bill with `amount: null` (a variable bill) has no amount clause at all,
   because there is nothing to be within.
3. **A tolerance with nothing to measure against is refused, not ignored.**
   `PUT /bills/{id}/rule` answers 422 `validation_error` while the bill has no
   `amount`, and `PATCH /bills/{id}` refuses to null the `amount` while a
   tolerance is set. The band is cleared through the rule endpoint, never
   stranded. (AGENTS §3: strict validation beats silent tolerance.)
4. **Matched = derived, never copied (D3).** There is no bill-link table. Every
   read — the detail page, the list's counts, the chart, the find-past preview —
   re-applies the current rule over the committed ledger. Editing a rule
   therefore changes retroactively which transactions a bill "contains", and
   that is the intended honesty; the rule editor says so in copy
   ("Saving re-matches every committed transaction against this rule right
   away"). Manual pin/unpin of individual rows would need a copy table and is
   explicitly **v2, out of scope**.
5. **Normalisation is the shared normalizer, and the payee test runs in
   Python.** Both sides go through
   `services/subscriptions.py::normalize_payee_key` — the ONE grouping
   normalisation in this codebase (M27/M96 import it, so a bill and the
   recurring detector can never disagree about identity: `"AMAZON MKTP 123"` and
   `"amazon mktP"` are the same merchant). Since **M165** the key is also
   **whitespace-free**: upper-case, every space run dropped, then the trailing
   `#?digits` strip — so `"GOOGLE *YouTubePremium"`, `"Google *Youtubepremium"`
   and `"Google*Youtubepremium"` are ONE payee and ONE term covers all three
   statement spellings (verified on prod: tenant 343's YouTube Premium rows
   arrived in all three, and the detector used to split them into two candidate
   groups). The browser's port of this function is
   `frontend/src/lib/payeeKey.ts`, used by `lib/billCoverage.ts` so the
   prompt/dashboards' "is this ALREADY a bill?" question (M165) answers the
   same way the matcher does — see docs/09 §Recurring prompt → Already a bill. The consequence for the wire: the
   payee predicate is applied in `services/bill_match.py`, NOT in the WHERE
   clause. `str.upper()` and Postgres case folding are different functions
   (`'straße'.upper()` → `STRASSE`, `upper('straße')` → `STRAßE`, verified on
   the live server), so no `ILIKE`/`upper()` predicate over the raw column is a
   proven *superset* of the Python rule — and a prefilter that can be NARROWER
   than the rule silently loses rows. The scan is bounded instead by
   ownership + committed staging + `amount_native < 0` (+ optional category and
   date window), all of which ARE indexed, and the plan is measured
   (Verification). **No index was added for M116.**
6. **Only committed EXPENSE rows are payments.** `identity_tx_conditions`
   (M107 IDENTITY: split parents are the statement lines, children excluded —
   the population `detect_subscriptions` also scans) plus `amount_native < 0`.
   A pending import can never be counted as a payment; a split counts once, at
   its parent; a credit card refund is not a payment, so the chart total is
   `Σ |amount|` over matched rows and never double-counts a reversal.
7. **Periods follow the bill's cadence, and a miss is data.** `monthly` →
   calendar month, `quarterly` → calendar quarter, `yearly` → calendar year,
   `one_time` → month (it has no cadence; the least surprising unit). A bucket
   key is the period's **first date** — the same key shape `GET /reports`
   hands the frontend — so the chart labels itself with the shared `fmtBucket`
   instead of inventing a second time axis. Every period inside the span gets a
   bar: a period with no payment is `total: "0.00"`, `count: 0`,
   `missed: true`, drawn zero-height with a dashed marker through the category
   and spelled **missed** in the data table. The rolling windows (6 and 12)
   average those calendar buckets INCLUDING the zeros — averaging only the
   months that happened would hide exactly the fact the chart exists to show.
8. **One scan per request, not one per bill.** `load_rows` selects the tenant's
   committed expense rows once and precomputes each row's normalised payee, so
   `GET /bills` costs one query and N cheap string tests rather than N ledger
   scans. The detail page hydrates only its own page slice by id through the
   SAME `_transaction_page` builder the ledger list uses (tags, receipts and
   split headers come along; nothing is forked).
9. **One-click adoption from M96, never auto-create.** The recurring prompt
   card gains a **Create bill** action that navigates to a PREFILLED
   `/bills?autofill=…&match=…` draft — name, match term, cadence and next due
   come from the detected group, and the signed `monthly_total` is passed as a
   POSITIVE amount (a bill's `amount` is what it costs; a signed amount would
   make the `|transaction| vs amount` band match nothing, ever). Nothing is
   written until the user submits the form (M55 decision 6 unchanged). Only
   `monthly` groups map onto a bill cadence; anything else says so instead of
   silently becoming a `one_time` bill. **Auto-creation stays out of scope.**
10. **Everything local.** The matched set, both rolling windows and the chart
    series are computed in-process from rows the server already holds: no LLM,
    no network client in `services/bill_match.py`, no external service
    (docs/11 privacy posture).

### Schema (M116 — migration `0044_bill_match_rules`)

Three columns on the existing table, no side table (docs/02 §bill_reminders):

```sql
ALTER TABLE bill_reminders ADD COLUMN match_mode VARCHAR(16) NOT NULL DEFAULT 'contains';
ALTER TABLE bill_reminders ADD COLUMN match_term  VARCHAR(120);
UPDATE bill_reminders SET match_term = left(name, 120) WHERE match_term IS NULL OR match_term = '';
ALTER TABLE bill_reminders ALTER COLUMN match_term SET NOT NULL;
ALTER TABLE bill_reminders ADD COLUMN amount_tolerance NUMERIC(12,2);
-- ck_bill_reminders_match_term_len : length(match_term) BETWEEN 1 AND 120
-- ck_bill_reminders_tolerance_nonneg: amount_tolerance IS NULL OR amount_tolerance >= 0
```

`match_mode` is a `StrEnum` stored non-natively (AGENTS §2). No index is
added — see decision 5 and docs/02 §Indexes.

### API (M116)

The exact wire — request/response shapes, bounds and every error code — lives
in **docs/08 §Bill Reminders**, reconciled in the same commit. Summary:

| Method/Path | Purpose |
| --- | --- |
| `GET /bills` | unchanged shape, items now carry `payment_count` + `rolling_average` (B4) |
| `POST /bills` | + `match_mode`, `match_term`, `amount_tolerance` |
| `PUT /bills/{id}/rule` | full replacement of the rule |
| `GET /bills/{id}/detail?from=&to=&limit=&offset=` | matched page + rolling + chart |
| `POST /bills/{id}/find-past` | read-only preview of the saved (or overridden) rule — **M123:** `category_id` joins the optional overrides so an edit that moves the scope is testable before it is saved |
| `POST /bills/find-past` | the same preview for a rule with no bill row yet |

Error envelopes added: `validation_error` (422) for a term that normalises to
nothing, a term over 120 chars, a tolerance without an amount, and an amount
nulled under a live tolerance. Tenancy: an unknown or foreign `bill_id` is
`404 not_found` on all three `{id}` routes **before** a single row is scanned.

**Reported deviation (not pinned by the card):** the card pins only
`POST /bills/{id}/find-past`. The create form has no bill id to hang it on, so
`POST /bills/find-past` was added alongside it; both share one handler
(`_find_past_response`) and answer the same shape.

**Reported deviation (docs/35 + docs/37):** `bill_due` inbox/delivery `link`
now points at `/bills/{id}` instead of `/bills`, because the drill-down exists.
`item_key` (`bill_due:{id}`) is unchanged, so read state and outbox rows are
untouched; the two tests that pinned `/bills` were updated with it.

### Frontend (M116)

- **`/bills/:billId`** (`pages/BillDetail.tsx`, lazy, wired in `App.tsx`): the
  header states the rule in prose, the `N payments` badge (or **0 payments
  yet**), both rolling windows (mean + median, last 6 and last 12), the period
  chart, and the paginated matched table — a row click opens the SAME
  `TransactionEditor` the ledger uses. **Edit rule** is inline.
- **`components/charts/BillHistoryBarChart.tsx`**: `ChartFrame` +
  `chartAriaLabel` (one leaf node, docs/12) over Recharts `BarChart`, X ticks
  from `fmtBucket(period, granularity)` (the `quarter` bucket case was added to
  `fmtBucket` — "Q3 2026", never a month name for a quarter), a dashed
  `ReferenceLine` per missed period, and a `<details>` **data table** after the
  frame: keyboard-reachable through the native `summary`, walkable by a screen
  reader, with `missed` written out. The scroll box carries `tabIndex` +
  `aria-label` (M102 contract).
- **`pages/Bills.tsx`**: each row links to its drill-down and shows
  `N payments` / `0 payments yet` and `avg $X`; the create/edit form gains the
  inline **Match transactions** block and a **Find past payments** button whose
  preview groups hits by period before anything is saved.
- **`components/transactions/RecurringPromptCard.tsx`**: the **Create bill**
  hand-off (decision 9).

### Touch points (M116)

| path | change |
| --- | --- |
| `backend/alembic/versions/0044_bill_match_rules.py` | new — three columns, backfill, two CHECKs |
| `backend/app/models/bill_reminders.py` | `BillMatchMode` StrEnum + three fields |
| `backend/app/services/bill_match.py` | new — rule, scan, period bucketing, rolling windows |
| `backend/app/schemas/bill_reminders.py` | rule/detail/find-past shapes + term validation |
| `backend/app/api/bill_reminders.py` | list stats, `PUT /rule`, `GET /detail`, both `find-past` |
| `backend/app/api/notifications.py` | `bill_due` link → `/bills/{id}` |
| `backend/tests/test_bills_match.py` | new — 20 contract tests incl. the 10k perf sweep |
| `backend/tests/test_notifications.py`, `test_notification_delivery.py` | the pinned `/bills` link |
| `frontend/src/api/bills.ts` | rule/detail/find-past client + `matchTerm` on the autofill |
| `frontend/src/pages/BillDetail.tsx` | new — the drill-down |
| `frontend/src/pages/Bills.tsx` | row stats, rule block, preview pane |
| `frontend/src/components/charts/BillHistoryBarChart.tsx` | new — period chart + data table |
| `frontend/src/components/charts/format.ts`, `aria.ts` | `quarter` bucket label / granularity word |
| `frontend/src/components/transactions/RecurringPromptCard.tsx` | **Create bill** hand-off |
| `frontend/src/App.tsx` | `bills/:billId` lazy route |
| `frontend/tests/m116BillDetail.test.tsx` | new; `billAutofill`, `recurringPrompt`, `m95EmptyStates`, `m102ScrollRegions` reconciled |
| `docs/02`, `docs/08`, `docs/09`, `docs/35`, `docs/37`, `docs/10` | reconciled in this commit |

Not touched: M40 CRUD semantics, M55 autofill derivation, M51/M59 notification
generation (only the `link` value), M96 detection rules, M107 split rules.

### Verification (M116)

Backend — `backend/tests/test_bills_match.py` (20 tests, real Postgres,
isolated suite database), every number hand-recomputed in the test body:

1. `normalize_payee_key` semantics: `contains` catches the un-numbered twin of
   `"AMAZON MKTP 123"`, `starts_with "amazon"` keeps both spellings plus
   `"AMAZON PRIME"`, `exact "Amazon Mktp"` matches the numbered row and
   `exact "AMAZON"` matches nothing. M165 adds the spacing case: one
   `contains` term `"Google*Youtubepremium"` matches all three YouTube
   Premium statement spellings.
2. Category scope and amount band are AND-ed (a differently-categorised
   same-payee row is excluded; with no category scope it comes back).
3. Tolerance is exact by default (`15.99` bill → 1 match out of 4) and
   symmetric with `±3` (3 matches: `13.00`, `15.99`, `18.50`; `30.00` stays
   out); a variable bill ignores the amount clause entirely.
4. `PUT /rule` refuses a blank term and a tolerance-without-amount (422);
   `PATCH` refuses to null an amount under a live tolerance (422).
5. **Derived, not copied:** `contains "AMAZON MKTP"` → 1 row; widen to
   `starts_with "AMAZON"` → 2; narrow to `exact "AMAZON PRIME"` → 1, with the
   right row on the page. No write happened to any transaction.
6. **Hand-recomputed rolling average:** six monthly payments of
   `10, 20, 30, 40, 50, 60` → mean `210/6 = 35.00`, median `(30+40)/2 = 35.00`,
   both windows, and the six chart totals summing to `210.00` — all asserted
   as literals in the test, not read back from the code under test.
7. **Chart shape:** Jan + Mar paid, Feb skipped → three points, the middle one
   `missed: true / count: 0 / total: "0.00"`, and the mean is
   `(25 + 0 + 25)/3 = 16.67` — a miss counts against the average.
8. Quarterly buckets key on `2026-01-01 / 04-01 / 07-01` and label `Q1 2026`;
   yearly buckets on `2025-01-01 / 2026-01-01`.
9. Pagination keeps the true total (`total: 20` on every page, newest first);
   `from`/`to` bounds the page AND the chart while `bill.payment_count` stays
   lifetime.
10. `find-past` groups newest-period-first with true per-group `count`/`total`,
    resolves account names in one statement, and writes NOTHING (row count
    before == after); a pending (uncommitted) import never counts.
11. Tenancy: a foreign `bill_id` 404s on detail / find-past / rule, the other
    tenant's `GET /bills` is empty and its own preview finds 0 rows.
12. Staging and splits: a pending-import row matches 0; a split parent matches
    once and its child never does.
13. **Performance (`…at_10k_rows`):** 10,000 committed rows → the loader's
    EXPLAIN carries the `user_id` predicate (the tripwire — drop it and the
    test fails), the ILIKE-analogue plan is recorded alongside for docs/02, and
    the measured first paint is **0.647 s** against the 1.5 s budget
    (list: **0.508 s**). Evidence written to `m116_perf_evidence.json` next to
    the test run.

Frontend — `frontend/tests/m116BillDetail.test.tsx` (4 tests): the header
states mode + term + amount band; both rolling windows come from the server;
the chart frame is ONE `role="img"` node whose label carries
`3 points … 1 missed period`; the `<details>` table is scoped, real, has a
caption, and writes **missed**; a row opens the shared editor; and the inline
rule editor PUTs the three fields with the re-match copy visible while open.
Plus `billAutofill` (17), `recurringPrompt` (14 — including the hand-off URL
and its positive amount), `m95EmptyStates` (the ninth `none` exception) and
`m102ScrollRegions` (the chart's scroll box is focusable and named).

Full gates per AGENTS §4: `ruff format --check` + `ruff check` clean,
`pytest -q` green on real Postgres (1340 passed), `tsc --noEmit` +
`eslint --max-warnings 0` clean, `vitest run` green (1033 passed), `npm run
build` clean, docker rebuild on example.internal (`docker compose up -d --build api mcp
web site`, migration `0044_bill_match_rules` applied and verified in
`alembic_version`), then browser-level verification of `/bills/:billId` against
the deployed stack.

**Live evidence (deployed stack, 2026-10-08, commit `c46db79`).**
`frontend/scripts/a11y/m116_bill_probe.py --base
https://fintrac.princessandmark.com` — **31/31 checks passed**, evidence in
`frontend/scripts/a11y/m116-bill-probe.json` + six screenshots
(`m116-bill-{390,412,1440}-{dark,light}.png`). The probe runs the whole story
against a throwaway tenant through the REAL import flow (a CSV uploaded with
`POST /imports/upload` + `profile_json`, then `/imports/{id}/commit`), not
hand-posted rows:

* the imported ledger is six `HYDRO ONE` charges of 10/20/30/50/60/70 across
  Jan–Jul 2026 with **April deliberately skipped**, plus a `HYDRO QUOTE` decoy;
* the server answers `matched_count 6`, `payment_count 6`, and every figure the
  tests hand-compute: 12-period mean **34.29** / median **30.00**, 6-period
  mean **38.33** / median **40.00**, seven chart periods with exactly April
  `missed`, totals summing to **240.00**;
* `GET /bills` reports `payment_count 6` + `rolling_average 34.29`; `find-past`
  answers six rows in six period groups with the account name resolved;
* the page renders the rule line verbatim ("Matches transactions whose payee
  starts with “HYDRO ONE” at $40.00 ± $70.00."), and the chart's ONE
  `role="img"` node is labelled "…7 points. total $240.00; highest Jul 2026
  $70.00; 1 missed period.";
* **keyboard**: the data-table `<summary>` is the 7th Tab stop, shows the accent
  focus ring, opens with Enter, and the table carries its 7 period rows with
  `missed` spelled out;
* **axe: 0 violations (0 critical / 0 serious / 0 moderate / 0 minor) in all
  six width × theme runs** — 390, 412 and 1440, dark and light, with the real
  viewport recorded per run;
* **derived, not copied**: widening the term to `HYDRO` through
  `PUT /bills/{id}/rule` moved the match from **6 → 7** with no write to any
  transaction.

One measured trap worth keeping: the first run matched 0 of 7 because the bill
carried a `category_id` and imported CSV rows are uncategorised — the category
clause is AND-ed, exactly as D2 says, so the probe drops it.


## M121 — Multi-term match rules: a term LIST with AND/OR

> Board card `t_ced11431`, filed under the heading **"M120"** — but M120 is
> already the receipts milestone (`docs/M120_RECEIPTS_SPEC.md`, docs/10
> §M120), so this work is documented as **M121**, the first free number.
> Reported, not hidden (AGENTS §3).

### Context (M121)

M116 gave a bill ONE term. Real ledgers don't cooperate: "Rogers" arrives as
`ROGERS`, `ROGERS COMMUNICATIONS` and `RCOM`, and one term can only catch one
spelling — while making the single term fatter starts matching somebody else.
The same question is asked in three places: a **bill's** rule, a
**categorization rule**'s `exact_payee` / `contains`, and the **recurring
detector**'s "have I already answered for this merchant?". Each had its own
single-term comparison, so the three could drift about what "matches" means
while all three still looked right.

### Decisions (M121)

1. **A rule is a TERM LIST with a mode.** `match_terms: list[str]` (1..10, each
   1..120 chars once trimmed) plus `match_logic: 'any' | 'all'`:
   * **`any` (OR, the default)** — a transaction matches when ANY term matches
     under the bill's `match_mode`: the widening case (`["ROGERS", "RCOM"]`
     catches every spelling);
   * **`all` (AND)** — EVERY term must match: the narrowing case
     (`["ROGERS", "WIRELESS"]` keeps only `ROGERS WIRELESS`).

   `any` is the default because a single-term rule — every rule that existed
   before M121 — behaves **identically** under both modes, so the default can
   never change an existing bill's answer.
2. **`match_mode` still decides each term's test** (`contains | starts_with |
   exact`), applied per NORMALIZED term; `match_logic` decides how the terms
   combine. A term list never changes what `contains` means (docs/25 §M116 D1
   unchanged).
3. **ONE matcher, three callers.** `services/match_terms.py::match_terms(
   terms, mode, normalize, subject, comparison="contains")` is the single
   implementation of the AND/OR contract:
   * bills — `bill_match.payee_matches` (passes pre-normalized keys with the
     identity normalizer `as_is`, keeping M116's "one normalization per ledger
     ROW, never one per (row, bill)" property);
   * categorization rules — `rules_engine._payee_matches`, `exact_payee` as
     `(pattern, "any", as_is, payee, "exact")` and `contains` as
     `(pattern, "any", str.lower, payee, "contains")`, which reproduce the
     pre-M121 comparisons byte for byte (case-sensitive equality;
     case-insensitive substring). **Regex is deliberately not routed through
     it** — a regex is a pattern language, not a term list, and
     `invalid_regex` stays its own documented failure shape (docs/04, docs/31);
   * the recurring detector — `recurring.merchant_answered`, the candidates
     route's "already answered" exclusion, as ANY-of over the tagged merchants'
     keys.

   Same function, same signature, so behavior cannot drift among the three.
   Every empty answer is explicit: an empty term list matches NOTHING under
   either mode (never vacuous truth), and a term or subject that normalizes to
   nothing never matches.
4. **The terms live in a CHILD TABLE** — not JSON, not more columns on the
   bill: `bill_reminder_match_terms (id, bill_reminder_id, position, term)`.
   docs/02 keeps JSON out of these tables; a row per term makes the 120-char
   bound enforceable in the TABLE, the order explicit, and "the terms of N
   bills" one indexed query. `position 0` = terms[0] =
   `bill_reminders.match_term`.
5. **`match_term` stays, and stays HONEST.** The pre-M121 wire field is kept:
   written as terms[0] in the SAME transaction as the rows, read as the whole
   rule by anything that only knows it. A request may send `match_terms` OR
   `match_term` OR both; both must agree (`match_term == match_terms[0]`), and
   a client that disagrees with itself is a 422 — never silently resolved one
   way (AGENTS §3).
6. **Backward compatibility is a READ-path guarantee, not a data fix-up.**
   Migration 0046 adds `match_logic` with server default `'any'` (so every
   existing row gets the widening mode in place) and copies each bill's
   `match_term` into position 0 — idempotent by `NOT EXISTS`, so a re-run can
   never duplicate a term. On top of that `terms_of()` falls back to
   `[match_term]` for a row with no term rows at all, so a pre-M121 bill (or a
   Python fixture) reads back as `terms=[old_term], logic='any'` even with an
   empty child table, and its matched set is unchanged.
7. **The rule surface is rate-limited:** at most 10 terms, each ≤ 120 chars,
   each still naming a payee after `normalize_payee_key` runs (blank /
   digits-only / over-length → 422 `validation_error`). A 500-term rule is a
   scan cost nobody chose.
8. **The UI is chips + an ALL/ANY switch beside them** (docs/09): one chip per
   term, Enter commits, Backspace on an EMPTY field removes the last chip, a
   labelled ✕ removes any chip, and the segmented ANY/ALL control sits beside
   the field. The typed-but-uncommitted text is still part of the rule
   (`composeTerms`), so Save can never drop a term the user typed, and a full
   list refuses the 11th term with visible copy instead of swallowing it.
9. **The header says the conjunction out loud.** `contains “ROGERS” or
   “RCOM”` for ANY, `contains “ROGERS” and “WIRELESS”` for ALL — the sentence
   must widen/narrow exactly where the counts do, or the page can describe one
   rule while showing another's numbers.
10. **Out of scope (pinned):** per-term negation (must-NOT-match) — an explicit
    `+1` if someone asks; regex terms — that complexity belongs to the
    categorization-rules surface (docs/04), not here.

### Schema (M121 — migration `0046_multi_term_match`)

```sql
ALTER TABLE bill_reminders
  ADD COLUMN match_logic VARCHAR(16) NOT NULL DEFAULT 'any';   -- 'any' | 'all'

CREATE TABLE bill_reminder_match_terms (
  id               SERIAL PRIMARY KEY,
  bill_reminder_id INTEGER NOT NULL
                   REFERENCES bill_reminders(id) ON DELETE CASCADE,
  position         INTEGER NOT NULL,
  term             VARCHAR(120) NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_bill_reminder_match_terms_bill_position
                   UNIQUE (bill_reminder_id, position),
  CONSTRAINT ck_bill_reminder_match_terms_term_len
                   CHECK (length(term) BETWEEN 1 AND 120),
  CONSTRAINT ck_bill_reminder_match_terms_position CHECK (position >= 0)
);
-- then BACKFILL_SQL (module-level constant in the revision):
--   position 0 = bill_reminders.match_term, guarded by NOT EXISTS
```

No index beyond the UNIQUE: `bill_reminder_id` is its leading column, so
"terms of bill N, in position order" is already an index scan. `match_logic`
is a `StrEnum` stored non-natively (AGENTS §2); `down_revision =
0045_merge_dup_securities`.

### API (M121)

The exact wire stays in **docs/08 §Bill Reminders**, reconciled in the same
commit. Summary of the delta:

| Method/Path | M121 change |
| --- | --- |
| `POST /bills` | + `match_terms: list[str]` (1..10) and `match_logic: "any" \| "all"` (default `any`); `match_term` still accepted as the one-term form |
| `PUT /bills/{id}/rule` | same two fields; `match_term` no longer required on its own (at least one TERM source is — neither present → 422) |
| `POST /bills/{id}/find-past`, `POST /bills/find-past` | + `match_terms`, `match_logic` overrides; absent = run the saved rule (unchanged) |
| every bill response | + `match_terms: list[str]`, `match_logic: string`; `match_term` = terms[0] |
| `BillFindPast` | + `match_terms`, `match_logic` (what the preview actually ran) |

Error envelopes: 422 `validation_error` for >10 terms, a blank term, a term
over 120 chars, a term that normalises to nothing, an unknown `match_logic`,
and `match_term != match_terms[0]` when both are sent. Tenancy and the 404
ordering are unchanged.

### Frontend (M121)

- **`components/bills/MatchTermsField.tsx`** (new): the chips input + the
  ALL/ANY segmented control, `label` "Match terms" on the input AND on the
  chip list, `aria-describedby` hint carrying the keyboard contract and the
  10-term cap, one labelled remove button per chip (`Remove term — ROGERS`).
- **`pages/Bills.tsx`**: the create/edit form's single term field becomes the
  chips field; the payload carries `match_terms` + `match_logic`; the preview
  caption lists the terms it ran.
- **`pages/BillDetail.tsx`**: **Edit rule** edits chips; the header prose joins
  the terms with `or` / `and` per `match_logic`.
- **`api/bills.ts`**: `BILL_MATCH_LOGICS`, labels/hints, `composeTerms`,
  `readTerms` / `readLogic` (tolerant readers: an M116-era payload degrades to
  its own single term), `describeTerms`.

### Touch points (M121)

| path | change |
| --- | --- |
| `backend/alembic/versions/0046_multi_term_match.py` | new — column + child table + `BACKFILL_SQL` |
| `backend/app/models/bill_reminders.py` | `BillMatchLogic` StrEnum, `match_logic`, `BillReminderMatchTerm` |
| `backend/app/models/__init__.py` | `BillReminderMatchTerm` exported — the export registry is derived from this package, so an unexported model is `500 export_registry_drift` |
| `backend/app/api/export.py` | `VIA_PARENT` entry: the term rows are caller-scoped through `bill_reminders` (docs/11) |
| `backend/app/services/match_terms.py` | new — THE shared matcher + validation |
| `backend/app/services/bill_match.py` | multi-term rule, `load_terms` / `terms_of`, `payee_matches` → shared matcher |
| `backend/app/services/rules_engine.py` | `exact_payee` / `contains` → shared matcher (regex untouched) |
| `backend/app/services/recurring.py` | `merchant_answered` → shared matcher |
| `backend/app/api/transactions.py` | candidates route calls `merchant_answered` |
| `backend/app/api/bill_reminders.py` | `_require_match_terms`, `_write_terms`, `_public`, term-aware detail/list/rule/find-past |
| `backend/app/schemas/bill_reminders.py` | `match_terms` / `match_logic` + `resolve_terms` |
| `backend/app/services/demo_seed.py` | demo bills write their term row |
| `backend/tests/test_m121_multi_term.py` | new — 16 tests (see Verification) |
| `frontend/src/components/bills/MatchTermsField.tsx` | new — chips + ALL/ANY |
| `frontend/src/api/bills.ts`, `pages/Bills.tsx`, `pages/BillDetail.tsx` | wire, chips, prose |
| `frontend/tests/m121MatchTerms.test.tsx` | new; `m116BillDetail`, `billAutofill` reconciled |
| `docs/02`, `docs/04`, `docs/08`, `docs/09`, `docs/10`, `docs/31` | reconciled in this commit |

Not touched: M116's match modes, amount band and derived-set rule (D2/D3),
M55 autofill derivation, M96 detection heuristics, M40 CRUD semantics.

### Verification (M121)

Backend — `backend/tests/test_m121_multi_term.py` (16 tests, real Postgres):

1. ANY/ALL cross-checked under `contains`, `starts_with` and `exact`, with
   subjects chosen so each comparison has a case only it can decide;
2. the never-silent answers: no terms → no match under either mode; a blank
   subject → no match; a digits-only term fails ALL outright and never
   contributes under ANY;
3. normalization parity: the matcher, `merchant_key` and the M96 helper agree
   on `"rogers #12"` ↔ `"Rogers 1234"`, `"  Superstore"` ↔ `"SUPERSTORE 4711"`;
4. all THREE surfaces asserted directly — `payee_matches`, `rule_matches`
   (case-sensitive `exact_payee`, case-insensitive `contains`) and
   `merchant_answered` — against the same subjects;
5. the API: create with a list, legacy `match_term` alone (→ `["X"]`, `any`),
   full replacement dropping old terms, and six 422s (11 terms, blank chip,
   121 chars, digits-only, unknown logic, disagreeing `match_term`);
6. **the acceptance case**: one Rogers bill — `["ROGERS"]` matches 3,
   ANY `["ROGERS","RCOM"]` widens to 4, ALL narrows to 0, ALL
   `["ROGERS","WIRELESS"]` narrows to exactly the one row carrying both — with
   `matched_count`, `payment_count`, the chart total (200.00), the rolling
   `payments` and the paginated `transactions.total` all moving together, and
   `limit=2` still reporting `total: 4`;
7. backward compatibility: a bill inserted as the pre-M121 schema left it
   (columns only, no term rows) answers `match_logic: "any"`,
   `match_terms: ["Netflix"]`, `payment_count: 1`; running the revision's own
   `BACKFILL_SQL` gives it position 0 WITHOUT changing any answer, and a second
   run adds nothing;
8. cascade and batching: deleting a bill drops its term rows; `load_terms`
   answers two bills in one call.
9. **the export/backup surface (docs/11)**: the child table is reachable from
   the model registry (exported from `app.models`) and scoped through
   `bill_reminders` in `export.py`'s `VIA_PARENT` — `test_export_registry`,
   `test_export_zip`, `test_backup`, `test_privacy_export`,
   `test_household_sharing` and `test_transaction_receipts` all green. Both
   fail-closed guards made this visible: the first full-suite run reported 28
   export/restore failures and NAMED the table (`export_registry_drift: no
   model class resolves for table(s) ['bill_reminder_match_terms']`) instead of
   shipping a silently shorter snapshot — a new child table MUST be exported
   from `app.models` and given a scoping rule, or nothing else in the feature
   can land.

Live — `frontend/scripts/a11y/m121_multi_term_probe.py` against the deployed
stack: **37/37 checks** (evidence `m121-multi-term-probe.json` +
`m121-rule-{390,1440}-{dark,light}.png`), including the widen 3 → 4, the
narrow 4 → 0 and the intersecting ALL → 1 above, four 422s, the keyboard lap
and **axe 0 violations in all four width × theme runs with the editor open**.
The migration ran on prod (`alembic_version = 0046_multi_term_match`, all 12
existing bills backfilled to position 0 under `any`).

Frontend — `frontend/tests/m121MatchTerms.test.tsx` (9 tests): Enter commits
and trims, Backspace only removes when the field is empty (and stops at zero),
a middle chip removes by its labelled button, the 11th term is refused with
visible copy and the typed text preserved, no duplicate chips, ANY/ALL as
visible words with `aria-selected` flipping (ANY default), `composeTerms`
never losing/mis-sizing a term, `readTerms` degrading an M116 payload, and
`describeTerms` joining with `or` / `and`. `m116BillDetail` and `billAutofill`
were reconciled to the chips payload.

## M122 — Rule-editor UX: ledger-backed suggestions, dry-run Test, rolling line, all-time/YTD stats

> Board card `t_fb581573`, titled "M122" — the first free number after M121.

### Context (M122)

M121 made a bill's rule a TERM LIST; Mark's follow-up is about the editor
surface around it. Terms were still typed from memory ("what did Rogers
arrive as?"), the only way to see a rule's effect was to save it, the period
chart showed bars without the trend the card already computed, and the page
stopped at the rolling windows — no all-time or year-to-date spend. All four
asks extend the M116/M121 surface in place: no new table, no migration.

### Decisions (M122)

1. **Suggestions are the caller's OWN ledger, grouped by the matcher's key.**
   `GET /bills/suggest-terms` scans the tenant's committed expense rows (the
   same `load_rows` the bill scan uses) and groups by
   `normalize_payee_key(normalized_payee)` — the SAME key the matcher
   compares, so `Rogers 1234` and `rogers #77` are ONE group — labelled with
   the NEWEST row's spelling (a real string out of the data; nothing external,
   nothing hallucinated). Rank: frequency desc, recency desc, then label.
   `limit` 1..25, default 8.
2. **Empty `q` = the candidates; `q` = the draft's matches under the editor's
   mode.** On opening the editor the endpoint (empty `q`) returns the top
   payee groups — the UI renders them as quick-add chips
   ("ROGERS COMMUNICATIONS ×14", frequency as social proof). A draft of ≥3
   chars (the client gate — the hook fetches nothing for 1–2 chars) filters
   through the SAME shared `match_terms` matcher under the request's
   `match_mode` (default `contains`), so suggestions follow the comparison the
   editor is using. A `q` that normalizes to nothing (`"123"`) answers `[]` —
   exactly what its committed chip would get (§M121 D7).
3. **Already-matched payees are excluded SERVER-side.** With `bill_id`, the
   bill's CURRENT stored rule runs over the same scan and any payee group
   with ≥1 row inside the matched set is dropped: a suggestion that would
   change nothing is noise. Any-of semantics — if one row of a group is
   already matched, the group's other rows fail on category/amount, so adding
   the term changes nothing. A foreign `bill_id` is a 404 before any
   suggestion is computed (tenancy).
4. **The dry-run summary counts the WHOLE matched set.** Both `find-past`
   routes gain `stats`: `first_date`, `last_date`, `last_12_total` (sum of the
   last ≤12 calendar period totals, misses as `$0.00` — the same population
   `rolling.twelve.mean` divides) and `account_counts`
   (`[{account_id, account_name, count}]`, count desc). Never computed over
   the previewed slice — `items` truncates at the 500-row cap and a summary
   describing the slice would be the fabricated number the M91 rule exists to
   prevent.
5. **"Test" is the editor action, and the panel says it is a draft.**
   BillDetail's inline rule editor gains a **Test** button beside **Save
   rule** → `POST /bills/{id}/find-past` with the DRAFT overrides; the Bills
   create/edit form's dry-run button becomes **Test rule** (one word for one
   behavior across both editors) and both render the shared
   `RuleTestPanel`: the "Draft preview — not saved" badge, N matching
   transactions, first → last date, the last-12-period sum, per-account
   counts, the first 8 matched rows and the period table. The preview is
   cleared the moment the draft changes — a stale preview is a lie about the
   current inputs.
6. **The chart's rolling line is SERVER-computed per point.** `ChartPoint`
   gains `rolling_six`: the trailing ≤6-period mean ENDING at that point
   (missed periods `$0.00`, window front-truncated), computed in
   `build_chart` from the SAME quantized point totals the bars and the
   rolling readout use — so the line's LAST point IS `rolling.six.mean`, the
   card's stat, by construction rather than coincidence. The chart draws it
   as a dashed accent polyline; the fallback table carries the column.
7. **All-time and YTD are a different population from the rolling windows, on
   purpose.** `stats.all_time = {total, payments, average, years,
   yearly_average}` over the LIFETIME matched set: a MISSED period never
   enters `total` (that is spend that did not happen — the rolling windows
   keep their missed-`$0.00` semantics separately); `years` = distinct
   calendar years holding ≥1 payment (the honest divisor); `yearly_average` =
   `total / years` quantized to cents. `stats.ytd = {year, total, payments}`
   is the CALENDAR year, Jan 1 → server `today_et()` — not the bill's
   anniversary — and a future-dated row is real spend (all-time) but never
   YTD. `GET /bills` rows gain `ytd_total` from the same matched set. Every
   figure recomputes server-side per read (M91); the detail's `from`/`to`
   window narrows the chart and rolling, never `stats`.
8. **Privacy (docs/11):** suggestions are a DB query over the caller's own
   rows — no LLM, no network client in `api/bill_reminders.py` or
   `services/bill_match.py` (pinned by an AST test), and `privacy_mode`
   changes nothing because there is nothing to privatize.
9. **Out of scope (pinned):** per-term negation (parked with M121), rule
   sharing across bills, charts beyond the single rolling line.

### API (M122)

The exact wire lives in **docs/08 §Bill Reminders**, reconciled in the same
commit. Delta:

| Method/Path | M122 change |
| --- | --- |
| `GET /bills/suggest-terms?q=&limit=&bill_id=&match_mode=` | **NEW** — `[BillSuggestTerm {normalized_payee, count, total, last_date}]`; empty `q` = top groups; `limit` 1..25 default 8; foreign `bill_id` → 404 `not_found`; unknown `match_mode` / `limit` out of band / `q` > 120 chars → 422 |
| `GET /bills` | `BillStatPublic` + `ytd_total: decimal` (calendar-YTD from the same matched set; `0.00` is the honest "nothing this year") |
| `GET /bills/{id}/detail` | + `stats: {all_time: {total, payments, average, years, yearly_average}, ytd: {year, total, payments}}` (LIFETIME, window-independent); `chart.points[]` + `rolling_six: decimal \| null`; `rolling.{six,twelve}` + `total: decimal \| null` |
| `POST /bills/{id}/find-past`, `POST /bills/find-past` | + `stats: {first_date, last_date, last_12_total, account_counts: [{account_id, account_name, count}]}` over the WHOLE matched set |

### Frontend (M122)

- **`components/bills/MatchTermsField.tsx`** — the input becomes a real
  combobox (`role`, `aria-expanded`, `aria-controls`, `aria-autocomplete`,
  `aria-activedescendant`, the M104 TagPicker contract, docs/09 §Tag UX):
  ArrowUp/ArrowDown/Home/End walk the suggestion list, Enter adds the
  HIGHLIGHTED suggestion (with no highlight it still commits the typed text,
  unchanged from M121), Escape dismisses the list first and only that, and a
  click keeps the input focused (`mousedown` preventDefault). Selecting a
  suggestion REPLACES the draft. The empty-draft candidates render as
  quick-add buttons ("payee ×14", accessible name "Add suggested term — …").
- **`components/bills/useTermSuggestions.ts`** (new) — the WHEN: 200 ms
  debounce, the empty/≥3 gate, `keepPreviousData`, chips filtered out
  client-side, `enabled` tied to the editor being open.
- **`components/bills/RuleTestPanel.tsx`** (new) — the shared dry-run panel
  (decision 5), used by both editors.
- **`pages/BillDetail.tsx`** — Test button + panel; "Rolling average" card
  renamed **Spending stats** with all-time total, per-payment mean, this-year
  (YTD), and yearly average alongside the two rolling windows.
- **`pages/Bills.tsx`** — suggestions wired into the form; the dry-run
  becomes "Test rule" + the shared panel; each list row gains `· YTD $x`.
- **`components/charts/BillHistoryBarChart.tsx`** — dashed `Line` over the
  bars (`rollingSix`), the aria sentence gains "6-period average ends at …",
  and the fallback table gains the "6-period avg" column.
- **`api/bills.ts`** — `suggestTerms`, `BILL_SUGGEST_MIN_CHARS`,
  `BillSuggestTerm`, `BillFindPastStats`, `BillSpendingStats`,
  `rolling_six`, `ytd_total`, `RollingWindow.total`.

### Touch points (M122)

| path | change |
| --- | --- |
| `backend/app/services/bill_match.py` | `ChartPoint.rolling_six`, `rolling_line`, `RollingWindow.total`, `SpendStats`/`spend_stats` (all-time + YTD), `BillHistory.spend` |
| `backend/app/schemas/bill_reminders.py` | `BillSuggestTerm`, `BillFindPastStats`/`BillFindPastAccountCount`, `BillSpendingStats`/`BillAllTimeStats`/`BillYtdStats`, + `rolling_six` / `total` / `ytd_total` / `stats` fields |
| `backend/app/api/bill_reminders.py` | `GET /bills/suggest-terms`, `_find_past_stats`, `_spending_public`, list/detail/find-past wiring |
| `backend/tests/test_m122_bill_ux.py` | new — 15 tests (see Verification) |
| `frontend/src/api/bills.ts` | suggest-terms client + the M122 types |
| `frontend/src/components/bills/MatchTermsField.tsx` | combobox listbox + quick-add chips |
| `frontend/src/components/bills/useTermSuggestions.ts`, `RuleTestPanel.tsx` | new |
| `frontend/src/pages/BillDetail.tsx`, `Bills.tsx` | Test action, Spending stats, YTD rows, suggestions |
| `frontend/src/components/charts/BillHistoryBarChart.tsx` | rolling line + fallback column |
| `frontend/tests/m122BillSuggestions.test.tsx` | new — 8 tests; `m116BillDetail`, `m121MatchTerms`, `billAutofill` reconciled (the term input is a combobox now; fixtures gain `stats`/`rolling_six`/`total`) |
| `docs/08`, `docs/09`, `docs/10` | reconciled in this commit (docs/02 untouched — M122 adds NO table or column) |

Not touched: M116's match modes / amount band / derived-set rule, M121's
multi-term matcher, M55 autofill derivation, M96 detection, M40 CRUD
semantics, and NO migration (every M122 field is derived at read time).

### Verification (M122)

Backend — `backend/tests/test_m122_bill_ux.py` (15 tests, real Postgres):

1. suggest-terms: frequency-then-recency ranking with a two-spelling payee
   collapsed into one group labelled by its newest row; `q` filtering under
   the shared normalizer (case-insensitive, digits-only `q` → `[]`);
   `match_mode=starts_with` narrowing where `contains` matches; exclusion of
   the bill's current matched set (present without `bill_id`, absent with),
   tenancy (another user's payee never appears; foreign `bill_id` → 404),
   `limit` bounds (422 at 0 and 26, 422 for a 121-char `q`), and the
   privacy-mode answer with an AST pin that neither module imports a network
   client;
2. find-past `stats` hand-recomputed on a fixture with a missed month, a
   non-matching neighbour row and TWO accounts (first/last date, `63.96` =
   4 × 15.99 last-12 total, per-account counts in order) — on BOTH routes,
   plus the empty-rule null shape;
3. the chart line: totals `[10.00, 0.00, 20.00, 30.00]` → rolling line
   `[10.00, 5.00, 10.00, 15.00]` hand-recomputed, last point ==
   `rolling.six.mean`;
4. detail stats with missed months AND a future-dated row: all-time
   `167.95 / 5 = 33.59`, `years = 4`, `yearly_average = 41.99`, YTD
   `31.98 / 2`, and a windowed `from`/`to` read that narrows
   `matched_count` while `stats` stays lifetime;
5. `GET /bills` `ytd_total` per bill (`15.99`, `42.00`, and the honest
   `0.00`), and the rule-edit reactivity (ALL-narrow → payments 0, average
   `null`, YTD zeroed) — derived, not copied.

Frontend — `frontend/tests/m122BillSuggestions.test.tsx` (8 tests): the
empty/≥3 fetch gate, quick-add chips with counts, the keyboard lap
(ArrowDown → `aria-activedescendant` → Enter adds the SUGGESTION, not the
draft), Enter-without-highlight still committing the draft, the Test panel
(draft label, server summary, account counts, sample + period tables),
stale-preview clearing and a re-test whose count follows the narrowed draft,
the chart's aria fact + fallback column, and the Spending stats card read
straight off the payload. `m116BillDetail`, `m121MatchTerms` and
`billAutofill` reconciled: the term input is a `combobox` (role change is
the a11y contract, not an accident) and fixtures carry the new server
fields.

Live — `frontend/scripts/a11y/m122_bill_ux_probe.py` against the deployed
stack (throwaway tenant, rows through the REAL import pipeline):
**29/29 checks** (evidence `m122-bill-ux-probe.json` +
`m122-bill-{390,412,1440}-{dark,light}.png`), including the two spellings
collapsing into ONE group labelled by its newest row, `q=rog` narrowing, a
digits-only draft answering `[]`, `bill_id` excluding the saved rule's own
payees, the hand-recomputed all-time/YTD figures and rolling line (last
point = `six.mean` = 42.50 through a missed April), the unsaved-draft
preview, the keyboard lap adding the HIGHLIGHTED suggestion, the Test
panel, and **axe 0 violations in all six width × theme runs with the editor
open** (one of them with the suggestion listbox open). One probe-fixture
correction along the way, recorded because it is a real normalization fact:
`rogers #77` groups with `ROGERS` (trailing `#77` strips), NOT with
`ROGERS COMMUNICATIONS` — a different key that only a containing term
reaches.

## M123 — the category scope becomes a form field (Create from search, bill half)

`bill_reminders.category_id` has existed since M40 and the matcher has always
read it as a scope (`services/bill_match.py` restricts candidate rows to that
category), but **no form ever exposed it** — every bill the UI created had
`category_id = null`. The M123 "Create from search" menu needs the search's
category to be *visible* on a prefilled draft, which is the same thing as
closing that gap.

- **Form** — a `Category (blank = any)` select inside the `Match transactions`
  fieldset, because a bill's category is the set of transactions its rule may
  match (docs/09 §Create from search). It is populated from the existing
  `["categories"]` query and `startEdit` seeds it from the row, so an edit
  shows what is actually stored.
- **Create** (`POST /bills`) sends `category_id` **only when chosen** — an
  untouched form posts exactly what it posted before, which is what
  `billAutofill`'s payload assertions pin.
- **PATCH** (`PATCH /bills/{id}`) always sends it: `number` sets the scope,
  `null` clears it. The handler is `model_dump(exclude_unset=True)`, so an
  omitted key would KEEP the old scope while the select showed blank — a lie
  the user could only discover by re-running the match.
- **Preview** — `POST /bills/{id}/find-past` gains `category_id` as one more
  optional override (absent = the saved `bill.category_id`, same rule as every
  other field there; an explicit `null` previews with NO scope), and the draft
  route already had it. Both preview paths therefore test the scope the form
  shows, not the one that was saved.
- **Deep link** — `/bills?prefill=transactions&q=…&category=…` opens the form
  with `match_mode=contains`, `match_terms=[q]`, the scope selected, amount
  blank, and a `Created from search: …` provenance line naming both what
  landed and what a bill cannot carry. Nothing is created by landing; the
  params are dropped by `resetForm` exactly like M55's `autofill`, so a reload
  restores an unsaved draft but not a saved one.

**Verification** — `frontend/tests/m123CreateFromSearch.test.tsx` C3 (term,
amount, scope, provenance, create still gated) and
`frontend/tests/billAutofill.test.tsx` (the create payload is unchanged when no
category is chosen; the PATCH payload now states `category_id`).

## M180 — the schedule anchor: month-end clamping never eats the day

> Board card `t_ace18cbd`, from the product-review intake ("Bill recurrence
> dates: monthly/quarterly/yearly advancement must never store impossible
> dates"). Spec doc: this section. Cross-doc: docs/02 §bill_reminders (the
> column), docs/08 §Bill Reminders (the mark-paid contract).

### Context (M180)

A bill's recurrence lived entirely in one column, `next_due_date`, which holds a
**real calendar date** — and that made three defects structural, not incidental:

1. **Anchor drift.** `mark-paid` advanced from the previously CLAMPED date, so a
   monthly bill due on the 31st went Jan 31 → Feb 28 → **Mar 28** → Apr 28 …
   The 31st existed exactly once, because February had no room for it. The same
   loss hit the cash-flow calendar, which projects from the same stored value.
2. **Impossible stored dates.** The old `_advance_due_date` built the string
   itself — `f"{y}-{m:02d}-{day:02d}"` with no month-length check — so a
   QUARTERLY bill due on the 31st stored `2026-04-31` and a YEARLY bill due on
   Feb 29 stored `2029-02-29`. Neither is a date. The calendar silently dropped
   such a bill (documented in docs/36 decision 5 as a known defect) and the NEXT
   `mark-paid` raised `ValueError` out of `date.fromisoformat` — a bare 500.
3. **A leap day that never came back.** Feb 29 → Feb 28 → Feb 28 … for the
   next three years, even though 2032 has a Feb 29.

### Decisions (M180)

1. **The schedule keeps its own day-of-month: `bill_reminders.anchor_day`.**
   `INTEGER`, `NULL`-able, CHECK `1..31` or `NULL`. Only the stored DATE is
   clamped; the anchor is what the schedule repeats on, so
   Jan 31 → Feb 28 → **Mar 31** → Apr 30 → May 31, and a Feb-29 schedule moves
   to Feb 28 in a common year and **back to Feb 29** in the next leap year.
   * `NULL` means **un-anchored**: "the stored date's own day IS the anchor" —
     which is exactly the pre-M180 projection, so a row no API path wrote (a
     fixture, a seed, a hand-edited row) can never clamp differently than before,
     and no migration has to invent a value for it.
   * `POST /bills` writes `anchor_day = next_due_date.day`.
   * `PATCH /bills/{id}` re-anchors **only when the date actually changes** (an
     explicit new date re-defines the schedule). An UNCHANGED value keeps the
     stored anchor, because the edit form posts back the date it was shown —
     which is the CLAMPED one — so re-anchoring on a name edit would silently
     demote a 31st schedule to the 28th (`Bills.tsx` always sends
     `next_due_date` on save; docs/09 §Bills).
   * `mark-paid` **pins** a `NULL` anchor before advancing. That first step is
     byte-identical to the pre-M180 answer, and it is what makes a pre-M180 row
     recover the 31st from its second advance on.
   * **No wire change:** `anchor_day` is internal storage. `next_due_date`
     remains the honest, valid date the client renders, so no response shape,
     field or frontend type moves.

2. **ONE recurrence rule: `backend/app/services/bill_schedule.py`.** Every
   surface that projects a bill forward calls it, so the grid can never clamp a
   schedule differently from the write that advances it:

   | function | semantics |
   | --- | --- |
   | `STEP_MONTHS` | `{monthly: 1, quarterly: 3, yearly: 12}` — a cadence absent from the table does not repeat |
   | `MAX_STEPS` | 10 000, the same bound docs/36 decision 5 pins |
   | `stored_due(raw)` | `VARCHAR(10)` → `date`, or `None` |
   | `anchor_day_for(due, anchor)` | the effective day (`anchor` or `due.day`) |
   | `advance_months(due, step_months, anchor=None)` | whole-month shift, day clamped to the target month's length |
   | `next_due(due, frequency, anchor=None)` | the next occurrence; a cadence with no step answers `due` (one_time) |
   | `occurrences(due, frequency, anchor, start, end)` | every occurrence in `[start, end]`, forward-only, each computed from `due` + k WHOLE periods |
   | `first_on_or_after(due, frequency, anchor, today)` | first occurrence ≥ `today`, or `None` when the bounded loop gives up |
   | `bill_occurrences(bill, start, end)` | row-level: the month-grid contract (unreadable date → `[]`) |
   | `bill_next_on_or_after(bill, today)` | row-level: the confirmed-set contract (unreadable date, or a non-repeating bill already past → `None`) |

   `mark-paid` calls `next_due`; the cash-flow calendar's bill series calls
   `bill_occurrences`; the confirmed-subscription query's bill rows call
   `bill_next_on_or_after`. **The former
   `services/confirmed_subscriptions.py::advance_months` is gone** — it was the
   same clamp without the anchor, and two clamps is how the two surfaces would
   drift. The calendar-side wording (docs/36 decision 5, and its "Out of scope"
   bullet that reported this write-side defect) is reconciled with the calendar
   slice that owns those two files.

3. **Migration `0054_bill_due_anchor` repairs what the old write path stored.**
   Two idempotent statements, in this order (module-level constants,
   `ANCHOR_SQL` / `REPAIR_SQL` / `BACKFILL_STATEMENTS`):
   * **anchor first** — every ISO-shaped stored date gets its own day
     (`GREATEST(LEAST(day, 31), 1)`). For a valid date that is the honest answer;
     for an IMPOSSIBLE one (`2026-04-31`, `2029-02-29`) the day component is the
     only surviving record of what the user chose, so it is read *before* the
     repair rewrites the date.
   * **then clamp** — a stored date whose day exceeds its own month's length is
     rewritten to that month's last day (`2026-04-31` → `2026-04-30`,
     `2029-02-29` → `2029-02-28`), with the month length taken from the month's
     own first-of-month date, so leap years are the database's answer rather
     than a table of ours.
   * A row whose string is not ISO-shaped keeps it (nothing can invent a date
     from garbage) and keeps `anchor_day` NULL; every read path already skips
     such a row instead of raising. No backfill value is ever fabricated.
   * No index: nothing queries `anchor_day` on its own.

4. **An unreadable due date fails LOUD on the write path, silently on reads.**
   `POST /bills/{id}/mark-paid` on a row whose `next_due_date` is not a date
   answers **422 `invalid_due_date`** and writes NOTHING (the code docs/25's
   table has carried since M47, finally used for the write it describes); a
   stored date at the end of the representable range (`9999-12-31` yearly, where
   the next occurrence would be year 10000) answers the same envelope. The two
   READ surfaces skip such a row, which is their documented policy and never a
   500.

### Schema (M180 — migration `0054_bill_due_anchor`, `down_revision = 0053_recurring_intent_tags`)

```sql
ALTER TABLE bill_reminders ADD COLUMN anchor_day INTEGER;      -- NULL-able
ALTER TABLE bill_reminders ADD CONSTRAINT ck_bill_reminders_anchor_day
  CHECK (anchor_day IS NULL OR (anchor_day BETWEEN 1 AND 31));

-- then, idempotently and in this order:
--   ANCHOR_SQL: anchor_day = GREATEST(LEAST(substring(next_due_date, 9, 2)::int, 31), 1)
--               WHERE anchor_day IS NULL AND next_due_date ~ ISO-shaped (month 01-12)
--   REPAIR_SQL: next_due_date = <that month's last day>
--               WHERE the ISO-shaped date's day exceeds its month's length
```

### API (M180)

No response shape changes. The deltas are behavioural:

| Method/Path | M180 change |
| --- | --- |
| `POST /bills` | writes `anchor_day = next_due_date.day` (internal; not on the wire) |
| `PATCH /bills/{id}` | a CHANGED `next_due_date` re-anchors; an unchanged one does not |
| `POST /bills/{id}/mark-paid` | advances on the anchor's day (`Jan 31 → Feb 28 → Mar 31`), `one_time` still disables; a date it cannot use → **422 `invalid_due_date`**, nothing written |
| `GET /bills`, `GET /bills/{id}/detail` | unchanged (`next_due_date` is always a real date) |

### Verification (M180)

Backend — `backend/tests/test_bill_schedule.py` (23 tests, real Postgres,
isolated suite database), every chain hand-computed in the test body:

1. **the rule, as arithmetic:** a 13-step monthly walk from 2026-01-31 equals
   `2026-02-28, 2026-03-31, 2026-04-30, 2026-05-31, 2026-06-30, 2026-07-31,
   2026-08-31, 2026-09-30, 2026-10-31, 2026-11-30, 2026-12-31, 2027-01-31,
   2027-02-28`; quarterly from the same date is `2026-04-30, 2026-07-31,
   2026-10-31, 2027-01-31` (the pre-M180 code wrote `2026-04-31`); yearly from
   2028-02-29 is `2029-02-28, 2030-02-28, 2031-02-28, **2032-02-29**, 2033-02-28`.
2. **the sweep:** 4 anchor days × 36 months × 3 cadences × 14 steps = **6048**
   advances, each one a real date whose ISO form parses back — the write path
   can no longer produce an impossible value by construction.
3. `one_time`/unknown cadence never advance; an un-anchored row (`NULL`) behaves
   exactly as before; `occurrences` is forward-only (a month before the stored
   date is `[]`) and reads the anchor; `first_on_or_after` answers Apr 30 for a
   31st bill, never Apr 28.
4. **the API:** `POST /bills` → `mark-paid` ×3 on a Jan-31 monthly bill answers
   `2026-02-28`, `2026-03-31`, `2026-04-30` and the row keeps `anchor_day 31`;
   the quarterly case stores a real `2026-04-30`; the leap-day yearly case
   returns to `2032-02-29`; `one_time` disables and keeps its date.
5. **PATCH:** echoing the shown (clamped) date keeps `anchor_day 31` and the
   next advance is `2026-03-31`; a CHANGED date re-anchors (→ `2026-04-15`).
6. **the row-level readers** the calendar and confirmed-set call:
   `bill_occurrences` answers Feb 28 / Mar 31 / Apr 30 for the same row,
   `bill_next_on_or_after` answers 2026-04-30 for a mid-April read, an
   unreadable date yields `[]`/`None`, and a `one_time` bill past its date
   yields `None` while a monthly one steps forward.
7. **the migration's own statements**, executed against pre-M180-shaped rows
   (`anchor_day` NULL): `2026-04-31` → `('2026-04-30', 31)`, `2029-02-29` →
   `('2029-02-28', 29)`, `2026-02-15` untouched, a non-ISO string untouched with
   a NULL anchor — and a SECOND run changes nothing (idempotent).
8. **the table bound:** a raw `anchor_day = 0` / `= 32` is refused by
   `ck_bill_reminders_anchor_day` (`IntegrityError`), and the refused write
   leaves the row alone.
9. **the read-path policy:** a hand-edited `2026-02-31` row is skipped by the
   calendar (`GET /reports/cashflow-calendar` answers 200 with no event for it)
   and refused by `mark-paid` with the `invalid_due_date` envelope; the storage
   string alone is never trusted.
10. `services/bill_schedule.py` imports no network client (`httpx`, `requests`,
    `yfinance`, `urlopen`, `services.fx`) — the rule is local arithmetic, so no
    fx read can enter a projection (AGENTS §2, docs/11).
11. **the boundary matrix** (`backend/tests/test_calendar_recurrence_matrix.py`,
    68 cases) consolidates this rule's edge cases across the four boundaries it
    shares with M170 — month-end, leap-day, DST and currency — exercising every
    row against the SAME three surfaces (the write path, the month grid, the
    confirmed set) and pinning the one series still NOT on it: an accepted
    recurring group, reported in docs/36 §Out of scope. See docs/36 §Verification
    for the per-class list and the mutation proof.

Gates: `ruff format --check` + `ruff check` clean, `pytest tests/test_bill_schedule.py
tests/test_bills.py tests/test_m29_validation.py tests/test_cashflow_calendar.py
tests/test_m170_confirmed_subscriptions.py` green (64 pre-existing + 23 new),
`alembic upgrade head` builds the schema from scratch in the suite database.

## Acceptance criteria

- [ ] Bills CRUD works with tenancy isolation
- [ ] Calendar view shows bills on correct dates
- [ ] List view sorts by due date with countdown
- [ ] Dashboard shows upcoming bills total
- [ ] Remind_days_before controls visibility threshold
- [ ] Enabled/disabled toggle works
- [ ] Frequency affects next_due_date calculation after marking paid
- [ ] M55: a detected subscription seeds a bill draft and the created bill lands on `/bills` with the mapped cadence + next due date
- [ ] M55: foreign / unknown `from_subscription_id` → 404 `subscription_not_found`
- [ ] M55: unmappable detected cadence → 422 `unsupported_bill_cadence`
- [ ] M55: no subscription table, no new detection logic, M27 detection rules untouched
- [ ] M116: every bill carries a match rule (`contains | starts_with | exact` + term + optional ± band) and `GET /bills` reports its payment count and 12-period rolling average
- [ ] M116: a term that normalises to nothing, and a tolerance with no amount, are 422 — never a rule that matches everything or nothing
- [ ] M116: the matched set is derived — editing a rule retroactively reshapes the detail page, the counts and the chart, with no link table written
- [ ] M116: the rolling average and every chart total are hand-recomputable in the tests (35.00 / 210.00 / 16.67)
- [ ] M116: a skipped period is an explicit zero bar with a `missed` marker, counted as $0.00 in the average
- [ ] M116: 10k-row detail first paint under the 1.5 s budget, with the EXPLAIN plan and timings captured as evidence
- [ ] M116: one-click adoption from the M96 recurring prompt prefills a bill draft (including a POSITIVE amount) and creates nothing until submit
- [ ] M121: a bill's rule is `match_terms` (1..10) + `match_logic` (`any` default / `all`), with `match_term` still reading as terms[0]
- [ ] M121: ANY widens and ALL narrows — verified on one Rogers bill across `matched_count`, `payment_count`, the chart total, the rolling windows and the paginated `total`
- [ ] M121: bills, the rules engine's payee matchers and the recurring detector all call the ONE `match_terms(terms, mode, normalize, subject, comparison)` matcher
- [ ] M121: >10 terms, a blank/over-length/digits-only term, an unknown logic and a self-disagreeing `match_term` are all 422 `validation_error`
- [ ] M121: a pre-M121 row (columns only, no term rows) reads back as `any` + `[old_term]`, and migration 0046's own backfill neither duplicates a term nor changes a count
- [ ] M121: the chips input is keyboard-complete (Enter adds, Backspace on empty removes the last, each chip deletable) and ANY/ALL read as words at 390 px
- [ ] M122: `GET /bills/suggest-terms` answers with the caller's own payee groups (frequency-then-recency), excludes the bill's current matched set, follows the editor's match mode, and answers `[]` for a digits-only draft — with no network client in either module
- [ ] M122: suggestions render as quick-add chips on an empty draft and as a keyboard-complete listbox at ≥3 chars; Enter adds the HIGHLIGHTED suggestion and still commits the typed text with no highlight
- [ ] M122: "Test" runs the DRAFT rule and renders the server's summary (count, first/last, last-12 sum, per-account counts, 8-row sample) labelled "Draft preview — not saved"; editing the draft clears it
- [ ] M122: the chart's rolling line is server-computed per point and its LAST point equals `rolling.six.mean` (hand-recomputed in the tests); the fallback table carries the column
- [ ] M122: all-time total/average/yearly-average and YTD total/count are hand-recomputed per fixture (missed months excluded, calendar-year YTD, future rows never YTD), recomputed per read, and shown on the detail card and the compact list row
- [ ] M180: a month-end schedule keeps its day through a clamp — Jan 31 → Feb 28 → Mar 31 → Apr 30 — and a Feb-29 yearly bill returns to Feb 29 in the next leap year
- [ ] M180: `mark-paid` can never store a non-date (quarterly Apr 31, yearly Feb 29 in a common year); a stored date it cannot use is 422 `invalid_due_date` with nothing written
- [ ] M180: `PATCH` re-anchors only on a CHANGED `next_due_date`, so the edit form's echoed (clamped) date never demotes the schedule
- [ ] M180: migration `0054_bill_due_anchor` backfills the anchor from the ISO day and clamps impossible stored dates to their month's last day, idempotently, inventing nothing
- [ ] M180: `services/bill_schedule.py` is the ONE recurrence rule — the write path, the cash-flow calendar (`bill_occurrences`) and the confirmed-set query (`bill_next_on_or_after`) all read it
- [ ] M180: no wire change — `anchor_day` is internal and `next_due_date` stays a real date on every response

## Priority: medium
## Effort: 2-3 days
## Dependencies: M3 (categories)
