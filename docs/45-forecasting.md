# 45 — Free local forecasting (M109): net-worth what-if scenarios

## Context

Monarch's most-requested feature is a paywalled $199-tier "what's next". We ship
it **free, local and deterministic** (D1/D2 rulings: never tier-gated, we sell
hosting and support — never features).

A forecast answers *"given what I already track, what does my net worth look
like if <life event> happens?"* It is deliberately **not** a prediction engine:

- **NOT Monte Carlo.** Same inputs → byte-identical output. There is no
  randomness anywhere in this document or its code. The Monte Carlo simulator
  ships on its own page `/retirement` (docs/57, M188) precisely so this claim
  and that feature can never blur.
- **NOT investment-return modelling.** One optional flat annual rate applied to
  the whole positions line; no per-asset forecast, no risk model.
- **NOT a tax simulator.** The Canadian tax projection (docs/06) keeps its own
  surface; a scenario that changes income does **not** recalculate it, and the
  UI says so out loud instead of omitting it.

This spec is the whole contract (AGENTS §9). The M109 card approved this shape
before implementation; deviations are reported, not hidden.

## Approach

### 1. The engine (`backend/app/services/forecast.py`)

A pure function of its inputs. No randomness, no clock reads, no network, no
LLM. `run_forecast(as_of, inputs, events, ...) -> dict` takes `as_of` as a
parameter so a test can pin the date; the route passes `today_et()`.

Money is `Decimal` end-to-end (AGENTS §2); every emitted value is quantized
`0.01` `ROUND_HALF_UP` and serialised as a string.

#### 1.1 Baseline inputs

| Input | Source |
| --- | --- |
| `cash_cad`, `positions_cad`, `net_worth_cad` | `services/reports.py::net_worth_report(as_of, as_of, "day")` — the LAST (only) bucket, i.e. docs/12 §4's `net_cad = cash_cad + positions_cad` **reused, never re-derived** |
| `income_monthly_cad`, `expense_monthly_cad` | `services/reports.py::cashflow_report(history_from, as_of, "month")` summed over the window, divided by the window's month count — docs/12 §3 definitions (unresolved-CAD rows excluded on both sides, trade principal excluded, `transfer_in/transfer_out` excluded) |
| recurring rows | committed `transactions` with `is_recurring = true` **and** `recurring_cadence IS NOT NULL` in the same window |

`flow_window_months` (default **12**, allowed 1–36; the UI offers 6 and 12)
sets `history_from = as_of − flow_window_months`.

#### 1.2 The double-count rule (the one thing to get right)

Historical flow already *contains* the recurring rows, so adding both would
count them twice. Decompose instead:

```
recurring_income_monthly  = Σ over recurring rows (cad_amount > 0) of cad_amount / periods_per_month(cadence)
recurring_expense_monthly = Σ over recurring rows (cad_amount < 0) of −cad_amount / periods_per_month(cadence)

residual_income_monthly   = income_monthly  − recurring_income_monthly
residual_expense_monthly  = expense_monthly − recurring_expense_monthly
```

Rows are grouped by M96's own identity — `merchant_key(normalized_payee or
raw_description)` + cadence, the same payee derivation
`api/transactions.py::_recurring_inputs` feeds the detector — because a tagged
group stores several rows and one projected stream per ROW would multiply a
single Netflix subscription into three.

`periods_per_month(cadence)`: `weekly` 30.4375/7, `biweekly` 30.4375/14,
`four_weekly` 30.4375/28, `monthly` 1 (the M96 `AVERAGE_MONTH_DAYS` constant).

Projection then applies, per month:

- `residual_*` **once per monthly anchor**, and
- each recurring row **on its own cadence dates** (so a weekly grocery series
  lands every 7 days and totals its monthly value over a month).

Their sum over a month equals the historical figure. **Residuals are never
clamped**: if recurring rows over-explain a short window the residual goes
negative, and it is *shown* in the assumptions panel rather than silently
adjusted (docs/09 honesty: never fabricate a nicer number).

#### 1.3 Timeline

- Effects are collected as `(date, kind, payload)` and sorted; the series walks
  bucket-end dates in ascending order, applying every effect with
  `effect_date <= bucket_end`.
- **Monthly anchors** for the baseline residual flow and investment growth:
  `as_of + k months`, `k = 1..horizon_months`, day clamped to the month end
  (Jan 31 → Feb 28) — the same clamping `services/recurring.py` uses.
- Event-anchored effects (salary change, cost delta, mortgage payments) carry
  **their own** anchor date, so a change effective 2027-06-15 first moves the
  line at 2027-06-15 and then monthly on the 15th.
- Granularity only changes **sampling**, never the state evolution: `day`
  (default), `week` (Monday-aligned, like every other report) or `month`,
  using `reports.bucket_starts` / `bucket_end`. With `week`/`month` an event
  between two bucket ends surfaces at the next bucket end — stated, not hidden.

#### 1.4 Ordering at a monthly anchor (pinned)

1. investment growth: `positions *= (1 + annual_return_pct/100 ** (1/12))`
   — growth **first**, exactly like docs/12 §7;
2. residual flow: `cash += residual_income − residual_expense`;
3. salary-change and recurring-cost-delta amounts for that anchor;
4. mortgage accrual + payment (§1.5);
5. emit the quantized value.

Recurring-row occurrences land on their own dates, in date order with
everything else.

#### 1.5 Event types (curated, typed — v1 is a CLOSED set)

There is **no formula editor and no scripting**: an event is one of five
discriminated shapes with typed parameters. Anything not in this table is not
buildable, deliberately.

| `type` | Parameters (all validated) | Effect on the timeline |
| --- | --- | --- |
| `house_purchase` | `date`, `home_price_cad > 0`, `0 <= down_payment_cad < home_price_cad`, `0 <= mortgage_rate_pct <= 25`, `1 <= amortization_years <= 50` | On `date`: `cash -= down_payment`, `asset_delta += home_price`, `liability_delta += home_price − down_payment` → **net worth does not move on purchase day** (cash became equity). Then monthly, anchored on `date`: `interest = balance × rate/12`, `payment = L·i/(1−(1+i)^−n)` rounded to cents, `balance -= payment − interest` (floored at 0), i.e. `cash -= payment`, `liability_delta -= principal` ⇒ net worth moves by **−interest** that period |
| `career_break` | `start_date`, `end_date` (`end_date > start_date`) | **Modifier, not an effect:** at every monthly anchor in `[start_date, end_date]` — **both ends inclusive**, so "no income from A to B" means B too — **all income components are zero**: residual income, recurring income occurrences and any salary change occurrence falling inside the range. Expenses continue, and one-off cash events / cost deltas are never suppressed. Multiple breaks union. Disclosed in the assumptions panel: salary and any other income are indistinguishable at row level, so suppressing "income" is the honest reading of "no income from A to B" |
| `salary_change` | `date`, `monthly_delta_cad` (signed, may be 0) | `cash += monthly_delta_cad` at `date`, then monthly on `date`'s day-of-month, through the horizon |
| `one_off_cash_event` | `date`, `amount_cad` (signed) | `cash += amount_cad` exactly on `date` |
| `recurring_cost_delta` | `start_date`, `monthly_amount_cad` (signed), `end_date` (nullable) | `cash += monthly_amount_cad` at `start_date` then monthly on that day-of-month, **inclusive of `end_date`** when set — the last charge lands ON it (a "have a child" cost is negative) |

Running scenario components (only these three exist):

```
cash_delta    — scenario cash minus baseline cash
asset_delta   — scenario-only assets (the house)
liability_delta — scenario-only liabilities (the mortgage)

net_worth = (baseline cash + cash_delta)
          + (baseline positions + asset_delta)
          − liability_delta
```

**House value is held constant at the purchase price** — no appreciation, no
depreciation, no maintenance reserve. Stated in the assumptions panel whenever
a `house_purchase` event is present. The mortgage is a **flat-rate model**
(monthly nominal rate, no Canadian semi-annual compounding, no stress test) —
also stated.

#### 1.6 Future vs real dollars

The response carries **both** series so the toggle is instant and offline:

```
real_values[i] = values[i] / (1 + inflation_pct/100) ** (days(bucket_end_i − as_of) / 365.25)
```

One constant rate, applied to the whole nominal path, compounded over the exact
day fraction of each bucket. Default `inflation_pct = 2.00` (CAD), editable
0–20. This is **not** inflation modelling; docs/12 §7's integer-year
deflator is the same idea at year resolution, and this day-fraction form is the
refinement needed for sub-year buckets (docs/12 §7 cross-references here).

#### 1.7 Performance & granularity

Budget: ≤ 5 s per scenario for a 2-year `day` run on example.internal. The engine builds
`horizon_days + anchors + event dates + recurring occurrences` effect dates and
walks them once — no per-bucket query happens per bucket.

**Measured (2026-10-06, this host):** a 2-year `day` run — 732 buckets, all five
event types live at once, plus 120 weekly recurring streams — is **48 ms**
best-of-5. A 10-year `day` run (3,654 buckets) is **882 ms**. Both are 2 orders
of magnitude under budget, so **`day` stays the default for every horizon** and
the fallback this section contemplates (drop to `week`, allow `day` only to
1 year) is NOT triggered. Re-measure and update this number if the engine's
effect set ever grows.

### 2. Data model

**One column, no table** — the M108-P2 `saved_report_views` reasoning again
(0038): scenarios are per-user content that roams with the account.

```
users.forecast_scenarios  JSON  NOT NULL  DEFAULT '[]'::json
```

Each entry is validated at the API seam by
`app/schemas/forecast.py::ForecastScenario`, not by a CHECK constraint — the
event set is an application contract shared with the UI, and a constraint would
duplicate it in SQL.

```json
{ "id": "sc_7f3k2p9qx1", "name": "House in 2027", "events": [ … ] }
```

- `id`: **client-generated** crypto-random token, `^[A-Za-z0-9_-]{8,64}$`
  (M108-P2 rule), so copy/rename address the id the list already carries.
- `name`: 1–80 chars, stripped of surrounding whitespace, not blank.
- `events`: 0–12 events, each `{id, type, …}` with `id` following the same id
  pattern; `extra="forbid"` so a typo'd field is a 422, never a silent drop.
- Cap: **25 scenarios per user** (409 `scenario_limit_reached`).

**E2EE / export.** Like budgets, goals and saved views, a scenario is ordinary
user content: stored in the tenant's own row, returned only to its owner, and
included in the one-click export automatically — the ZIP's table set is
*derived* from `app.models` (docs/11 §File list), and a JSON column on `users`
travels inside `users.csv`. No cipher column is claimed; docs/33's inventory is
not changed by this feature.

### 3. Endpoints (`backend/app/api/forecast.py`, prefix `/forecast`, mounted at `/api/v1`)

All JWT via `get_current_user` — which is also the demo mutation guard, so
**every** `POST`/`PATCH`/`DELETE` below answers `403 demo_account_restricted`
for the demo tenant and `GET` stays readable.

| Method/Path | Request | Response |
| --- | --- | --- |
| `GET /forecast/scenarios` | — | `200 {items: [Scenario], count: int}` |
| `POST /forecast/scenarios` | `Scenario` | `201 Scenario` |
| `GET /forecast/scenarios/{id}` | — | `200 Scenario` |
| `PATCH /forecast/scenarios/{id}` | `Scenario` (full replace) | `200 Scenario` |
| `POST /forecast/scenarios/{id}/copy` | `{id?, name?}` (both optional) | `201 Scenario` — server mints the id when absent |
| `DELETE /forecast/scenarios/{id}` | — | `204` |
| `GET /forecast/defaults` | — | `200 Defaults` (UI prefill, §3.2) |
| `GET /forecast/run` | query params §3.1 | `200 Run` §3.3 |

`Scenario = {id, name, events[]}` exactly as §2.

#### 3.1 `GET /forecast/run` query parameters

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `scenario_ids` | comma-separated ids | *(absent → baseline only)* | 0–6 ids, each must resolve for this user else `404 scenario_not_found` |
| `horizon_months` | int | `24` | 1–120 |
| `granularity` | `day \| week \| month` | `day` | — |
| `inflation_pct` | decimal string | `2.00` | 0–20 |
| `annual_return_pct` | decimal string | `0.00` | −50–50 |
| `flow_window_months` | int | `12` | 1–36 |

Bounds failures answer `422 validation_error` (`{detail, code}`), the same
envelope docs/08 pins for `/projections/run`.

#### 3.2 `GET /forecast/defaults`

```json
{ "as_of": "2026-10-06", "reporting_currency": "CAD",
  "net_worth_cad": "80235.79", "cash_cad": "12345.67", "positions_cad": "67890.12",
  "income_monthly_cad": "5000.00", "expense_monthly_cad": "3800.00",
  "flow_window_months": 12, "recurring_rows": 7, "scenario_count": 2 }
```

#### 3.3 `GET /forecast/run` response

```json
{
  "as_of": "2026-10-06",
  "horizon_months": 24, "granularity": "day",
  "inflation_pct": "2.00", "annual_return_pct": "0.00", "flow_window_months": 12,
  "reporting_currency": "CAD",
  "buckets": ["2026-10-07", "…"],                    // bucket END dates, ascending
  "inputs": {
    "cash_cad": "12345.67", "positions_cad": "67890.12", "net_worth_cad": "80235.79",
    "history_from": "2025-10-07", "history_to": "2026-10-06",
    "income_monthly_cad": "5000.00", "expense_monthly_cad": "3800.00",
    "recurring_income_monthly_cad": "0.00", "recurring_expense_monthly_cad": "450.00",
    "residual_income_monthly_cad": "5000.00", "residual_expense_monthly_cad": "3350.00",
    "recurring_rows": 7
  },
  "baseline": { "id": "baseline", "name": "Baseline",
                "values": ["…"], "real_values": ["…"], "final_cad": "…", "delta_cad": "0.00" },
  "scenarios": [ { "id": "sc_…", "name": "House in 2027",
                   "values": ["…"], "real_values": ["…"],
                   "final_cad": "…", "delta_cad": "-41120.55" } ],
  "disclaimer": "Projection, not a prediction."
}
```

- The series is anchored ON `as_of`: for `day` granularity `buckets[0]` is
  `as_of` itself and its value is today's actual net worth, so a run with no
  events opens at the number the Dashboard already shows.
- `values` is **future (nominal) dollars**; `real_values` is the same path in
  today's dollars. The UI toggle switches between them with no refetch.
- `delta_cad = scenario.final_cad − baseline.final_cad`.
- `len(buckets) == len(values) == len(real_values)` for every series.
- **Determinism:** identical inputs ⇒ byte-identical JSON. No timestamps, no
  UUIDs, no dict-iteration-order dependence in the payload.

### 4. Error envelope (new codes only)

| Code | Status | When |
| --- | --- | --- |
| `scenario_not_found` | 404 | unknown id **or another tenant's id** (no existence oracle) |
| `scenario_limit_reached` | 409 | creating a 26th scenario |
| `scenario_exists` | 409 | create/copy supplying an `id` this tenant already holds (a fork, not a rename — `PATCH` carries its own id and is a full replace) |
| `unknown_event_type` | 422 | `event.type` outside the five v1 types |

Reused: `validation_error` (422, bounds/shape), `demo_account_restricted`
(403, demo writes), `invalid_token` (401).

## Boundary — pinned in the UI, not just here

Rendered verbatim on `/forecast` (the strings are load-bearing and asserted):

- `Projection, not a prediction.`
- `Not a tax simulator — a scenario that changes income does not recalculate your tax projection.`
- `Deterministic, not Monte Carlo — the same inputs always give the same output.`
- `Not financial advice.`

### Display mask (docs/46 §D2)

`/forecast` is a money surface like any other: a masked session hides its
absolutes **without changing what the engine sent**. Rendered as `•••••`:

- the four cells of the **"End value per series"** table (each series' end
  value and its Δ vs baseline);
- the five **assumptions** figures — net worth, cash, positions, the income
  total and the expense total, each with its recurring + residual parts;
- the **amounts inside a scenario event row** (`… — ••••• price, ••••• down`).

Readable both ways, per docs/46 §D2's ruling: percentages (`Inflation:
2.00%`, `Flat annual return: 0.00%`), the recurring-row count, the history
dates, scenario names and the four honesty strings above. The unmasked string
is still the response's own, byte for byte — the mask *branches*, it never
re-parses or re-rounds a Decimal, so the panel can still not disagree with the
API it describes. The chart's text alternative is rebuilt every render so it
answers a flip as well (no `useMemo` around money — docs/46 §D6).

Pinned by `frontend/tests/f11ForecastMask.test.tsx` (real `<Forecast/>` in the
real `<AppShell/>`, masked scan + unmasked control + a chip flip with a frozen
fetch log) and by `frontend/tests/m109Forecast.test.tsx` (F15/F16: the
verbatim, no-refetch half). Note the naming collision: **security F-11** is
this mask finding; **F11** below is the backend tenancy test.

## Tenancy, owner model, partner

- `/forecast/*` is an **owner surface**: it authenticates with
  `get_current_user`, i.e. the caller's OWN tenant. Forecast data never appears
  in `/partner/*` or the partner's `/shared` page (docs/43: the partner sees a
  read-only *ledger* projection; forecast is the owner's decision surface).
  This is the first owner-model-only surface in the app — pinned by a test that
  asserts no `forecast`/`scenarios` key exists in any partner response.
- Scenarios are returned only to their owner; every query path is
  `user_id`-scoped (AGENTS §2).
- Demo tenant: writes refused (`403`), reads allowed — a demo user simply has
  an empty scenario list. The live smoke therefore uses a **probe account**,
  not demo, and the probe's scenarios are deleted afterwards.

## Touch points

| Path | Change |
| --- | --- |
| `backend/app/schemas/forecast.py` | **new** — `ForecastEvent`, `ForecastScenario`, run params/response |
| `backend/app/services/forecast.py` | **new** — the engine |
| `backend/app/api/forecast.py` | **new** — the router |
| `backend/app/models/users.py` | `forecast_scenarios` column |
| `backend/alembic/versions/00NN_forecast_scenarios.py` | **new** — one column |
| `backend/app/main.py` | mount `forecast.router` |
| `backend/tests/test_forecast.py` | **new** |
| `frontend/src/pages/Forecast.tsx` | **new** — `/forecast`; later F-11: subscribes with `useMasked()`, table/assumptions/event rows branch on it, chart label rebuilt per render |
| `frontend/src/lib/forecastCopy.ts` | **new** — the honesty strings, one source; `buildAssumptions(inputs, params, masked?)` for the F-11 mask |
| `frontend/src/api/forecast.ts` | **new** — typed client for all eight endpoints (the repo's one-module-per-endpoint-group convention) |
| `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx` | route + nav row |
| `frontend/src/components/reports/ProjectionsPanel.tsx` | `Open what-if scenarios` link |
| `frontend/tests/m109Forecast.test.tsx` | **new** |
| `frontend/tests/f11ForecastMask.test.tsx` | **new** — security F-11: masked scan / unmasked control / flip on the real shell |
| `docs/08`, `docs/09`, `docs/02`, `docs/10`, `docs/12 §7` | reconciled in the same change |

## Named tests (write them, don't thin the list)

**Backend (`tests/test_forecast.py`)**

- **F1** `house_purchase` on date X moves the line **on exactly X**: net worth
  unchanged on the purchase day (cash→equity), then −interest per period; the
  amortization is the disclosed flat-rate model.
- **F2** a weekly grocery series tagged `recurring` lands every 7 days in the
  post-history window and its monthly total equals the historical figure (the
  double-count rule).
- **F3** determinism: the same request twice ⇒ **byte-identical** response body.
- **F4** `career_break` zeroes income only inside `[start, end)` and restores it
  after; expenses never stop.
- **F5** `salary_change` / `recurring_cost_delta` first move the line on their
  own date, then monthly on that day-of-month.
- **F6** `one_off_cash_event` moves that one bucket and no other.
- **F7** two interleaved events (overlap) apply in date order, both visible.
- **F8** edge: user with **no** transactions/balances ⇒ baseline is all
  `0.00`, run is 200 (never an empty-chart crash); **single-account** tenant;
  **negative-balance** debt line projects as a negative start.
- **F9** `real_values` apply exactly one constant inflation rate; `inflation_pct=0`
  ⇒ `real_values == values`.
- **F10** bounds: `horizon_months=121`, `inflation_pct=21`, `annual_return_pct=51`,
  7 `scenario_ids`, 13 events, unknown `type` ⇒ 422 with the pinned codes.
- **F11** tenancy: user B's `scenario_ids` ⇒ `404 scenario_not_found`;
  `GET /forecast/scenarios` returns only the caller's rows.
- **F12** demo tenant: `POST /forecast/scenarios` ⇒ `403 demo_account_restricted`,
  `GET /forecast/run` ⇒ 200 with an empty `scenarios` list.
- **F13** export includes scenarios (derived table set, docs/11).
- **F14** the partner surface carries no forecast: `/partner/state` and the
  `/shared` payload contain no `forecast`/`scenarios` key.

**Frontend (`tests/m109Forecast.test.tsx`)**

- **F15** all four honesty strings render on `/forecast` (load-bearing).
- **F16** the dollar toggle switches `values` ↔ `real_values` without a refetch.
- **F17** the Reports projection card exposes `Open what-if scenarios` and the
  nav exposes the `Forecast` row.
- **F18** event CRUD: create → copy → delete round-trips through the API client.

## Verification (AGENTS §4, in order)

1. **Doc reconciliation** — this doc + docs/08/09/02/10/12 in the same commit.
2. **Lint** — `ruff format --check && ruff check`, `tsc --noEmit && eslint . --max-warnings 0`.
3. **Test** — `uv run pytest -q` (real Postgres) including F1–F14; `npm test`
   including F15–F18.
4. **Build** — `npm run build`.
5. **Docker** — `docker compose up -d --build api web`.
6. **Smoke** through `http://localhost:8080`: probe account → create a scenario
   with a `house_purchase` → `GET /forecast/run` → assert the jump lands on the
   event date → copy → delete → assert `count` back to 0.
7. **Performance pin** — DONE, recorded in §1.7: 2-year `day` = **48 ms**
   best-of-5 against the 5 s budget (10-year `day` = 882 ms), so `day` stays
   the default for every horizon.
8. **Push → CI → deploy → live re-verify** on the deployed stack with the same
   probe, then delete the probe's scenarios.

## Out of scope (definitive)

Monte Carlo / any randomness (shipped separately at `/retirement`, docs/57 —
its life-event planning is docs/58's registry, NOT the five deterministic
events here, and this document stays deterministic) · per-asset-class return modelling · tax-delta
recalculation · a generic formula or scripting editor (not-scriptable = not
buildable) · house appreciation, maintenance or transaction costs · Canadian
semi-annual mortgage compounding · cloud anything · partner-visible forecast.
