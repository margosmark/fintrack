# 12 — Reporting & Graphs

Aggregation endpoints backing the dashboard charts and the `/reports` page (docs/09). Milestone wiring lives in docs/10.

## Principles

- **CAD reporting currency.** Every aggregate reports in CAD (`REPORTING_CURRENCY`, docs/01). Native amounts stay per-row; all conversions read the `fx_rates` cache only.
- **Server-side computation (restored M137, mirrors retired M160).** Reports are aggregates over committed data plus cached prices/fx, and every report number the UI renders is **computed by the server**: the browser issues one request per report and renders the payload it gets back (docs/33 §Server-first rule 1). The S2a/S2b/S2c/S4 browser ports that used to re-derive spending, spending-categories, cashflow, net-worth, investment-performance, dividends and capital-gains from a full-ledger download were deleted in **M160** (M134 P2-5, M158 Decision 2 option c) along with their parity adapters and nine mirror tests — both legs of the last-resort justification were void: `encryption_required` is a write guard that branches zero read paths, so server compute works fully under E2EE, and the offline fallback had no runtime wiring (no service worker, no offline model).
  **What still computes in the browser, and why it is not a mirror:** (1) `lib/budgets.ts::budgetsProgress` — the partner view's budget progress, an AUDITED CLIENT-ONLY compute with no server twin by D3 design (docs/43 §9: the server must not read hidden accounts into the partner answer), so M135 §4.1 parity is unsatisfiable by design; (2) `lib/sankeyEngine.ts::cashflowSankey` — not a mirror and no longer a panel's implementation either: **M160-R2 (t_85311c34)** re-pointed the last three panels (dividends / sankey / total-return) at their own routes and deleted the `lib/reportClient.ts` shim that carried them, so nothing the UI renders is computed in the browser anymore; M160-R1 ruling 3 keeps `sankeyEngine` as the reference implementation the route was written against (its `SankeyReport` interface is still the wire shape the chart renders); (3) `lib/dec.ts`, the fixed-point primitives the surviving client code shares (not a compute, a library).
  **The golden fixtures stayed and changed job.** `frontend/tests/fixtures/*-vectors.json` and `capture.ts` are now SERVER-ROUTE REGRESSION PINS: `backend/tests/test_m160_replay_*.py` seeds each captured dataset into a throwaway tenant, replays the captured request against the mounted route and byte-diffs the answer (the `test_search_goldens.py` pattern). The Python paths are the only implementation, so "must stay numerically identical" is now a property the replay suite asserts against the frozen captures rather than two live implementations agreeing. Neither path ever triggers yfinance or BoC Valet fetches during report rendering, so reports work unchanged under privacy mode (the docs/11 lockdown blocks external calls, never cached reads).
- **On-the-fly aggregation, no materialization in v1.** Personal-scale data aggregates in query time; `acb_pools` (docs/02) stays the only cache table and exists for cross-account tax correctness, not reporting speed. A snapshot/materialization path may be added later behind the SAME endpoints.
- **Zero-fill.** Every time series contains one entry per bucket in `[from, to]` — value `0` when no data falls in the bucket (net-worth balances carry forward automatically; see below). Charts need continuous axes.
- **Performance budgets (M60, docs/34).** Every budgeted report route has a CI-enforced ceiling on SQL statements per request — `GET /reports/spending/categories` ≤ 3, `/reports/net-worth` ≤ 7, `/reports/cashflow` ≤ 4, `/reports/investment-performance` ≤ 7, `/reports/capital-gains` ≤ 3 — plus a p95 latency target, both measured on the M10 demo dataset by `backend/tests/test_perf_budgets.py`. **Query count, not milliseconds, is what CI enforces** (wall-clock is not a stable baseline). That choice is what caught the two report N+1s M60 fixed: `net_worth_report` issued one `SELECT securities` **per bucket** (18 monthly, 368 daily) and the ACB replay one `fx_rates` read **per non-CAD trade** (25 per computation).

## Time bucketing (shared contract)

- `from` and `to`: REQUIRED, inclusive ISO dates. `from > to` → `422 {"code": "invalid_date_range"}` in the standard error envelope (docs/08). Unknown `granularity`, missing params → the standard 422 validation envelope.
- `granularity=day | week | month | year`, default `month`. Weeks start Monday (ISO week). A bucket is keyed by its FIRST date. Series responses (`spending`, `cashflow`, `net-worth`) echo `{from, to, granularity}`.
- **M118 exception — `GET /accounts/{id}/value-history` (docs/08 §Account value history)** reuses these helpers (`bucket_starts`/`bucket_end`/`bucket_key`) and this `422 invalid_date_range`, but `from`/`to` default (the account's creation date → today) and the granularity is COARSENED `day → week → month → year` until the series fits its 400-point roofline instead of overflowing it — the response echoes both `granularity` and `requested_granularity`.
- **M130 exception — `GET /portfolio/value-history` (docs/08 §Portfolio value history)** is the same exception with the scope widened to every investment account: identical defaults (the PORTFOLIO's floor → today), the identical 400-point coarsening, the identical echoed pair. One extra rule the single-account route does not need — the summed series resolves ONE granularity (the coarsest any account reached) so its buckets all have the same width, whatever each account's own window would have picked.
- These list-style endpoints are exempt from the docs/08 pagination convention: output size is bounded by buckets × categories/securities, never by user rows — no `limit`/`offset` on report routes.

## Range → granularity (M126)

Since M126 the chart family offers the same seven ranges and derives its bucket size from ONE formula, instead of each surface hard-coding `month`.

**The formula.** *Pick the FINEST granularity whose estimated bucket count fits `MAX_SERIES_POINTS = 150`:*

```
ceil(span_days / unit_days) <= 150, evaluated day-first
unit_days = { day: 1, week: 7, month: 30.44, year: 365.25 }
```

`month`/`year` use average days on purpose — this is a budget decision about how many labels an axis can carry, not a calendar.

**The table it produces** (what every chart therefore shows):

| range | span | estimated points at the winning granularity | granularity |
| --- | --- | --- | --- |
| `1M` | 31d | 31 | `day` |
| `3M` | 92d | 92 | `day` |
| `6M` | 182d | 26 | `week` |
| `YTD` | Jan 1 → today | ≤ 52 | `week` |
| `1Y` | 365d | 52 | `week` |
| `5Y` | 1,826d | 60 | `month` |
| `All` | the whole cached history | ≤ 150 | **the server picks** — same formula, applied to the data it actually has (`granularity=auto`) |

Boundaries are pinned, not asserted by eye: `150d → day`, `151d → week`, `1826d → month`, `365×40d → year`.

**Implemented twice, pinned twice.** The client needs it where the REPORT endpoints take a bucket size as an argument (`frontend/src/lib/chartRange.ts::granularityForDays` / `granularityForWindow`, asserted in `frontend/tests/chartRange.test.ts`); the server needs it where the client only sends a window (`backend/app/services/price_series.py::pick_granularity`, asserted in `backend/tests/test_prices_history.py`). Both suites assert the SAME table above — a chart that buckets daily in the browser and monthly on the server would disagree with its own x-axis.

**Explicit granularity and the roofline.** A caller who passes `granularity=day|week|month|year` gets that granularity, coarsened only while the series would exceed `POINT_ROOFLINE = 400` points (the guard `services/account_value.py` already applies to M118); the response echoes `granularity` **and** `requested_granularity`, so a client can say honestly when the server narrowed the ask. `GET /accounts/{id}/value-history` keeps its own `day → week → month → year` coarsening to the same 400-point ceiling and echoes the same pair.

**Who owns the choice.** Two controls exist and they are deliberately different (docs/09 §Range control):

| chart | range control | granularity owner | default |
| --- | --- | --- | --- |
| Price history (`PriceHistoryChart` — security page + a positions row's expansion) | chart header (`ChartRangeControl`) | the server (`granularity=auto`) | `1M` |
| Account value timeline (M118, `AccountValueChart`) | chart header when the caller supplies none; the cash view keeps its richer `RangePicker` (custom From/To) | the server (`value-history` coarsening) | `All` |
| Portfolio value timeline (M130, `PortfolioHeaderCard`) | **the HEADER card's control**, not the chart's — one `range` state drives both the header's "over 6 months" sentence (from `stats`) and the chart below it, so the body's own control is turned off with `headerExtra` | the server (`/portfolio/value-history` coarsening) | `All` |
| Payments by period (bills, `BillHistoryBarChart`) | card header | the bill's own frequency (`month\|quarter\|year` — a monthly bill must stay monthly; the range only trims the window) | `All` |
| Net worth (Dashboard, `NetWorthAreaChart`) | the page-level `RangePicker` (1W…1Y + custom dates) | the **formula**, from the page range | `YTD` → `week` |
| Reports tabs | the page-level `RangePicker` + an explicit `granularity` switcher | **the user** — an explicit choice beats the formula, and stays picked across range changes | `month` |

The Dashboard's hard-coded `month` was the bug this replaces: a `1W` range produced a single monthly bucket. The Dashboard's *cash-flow* payload deliberately stays `month` — it feeds the "Range net" KPI (M138 D-5: the value sums `net_cad` over **every** bucket in the range, so the pre-M138 label "Month net" simply lied on anything wider than a month) and its latest-vs-previous-bucket delta chip, not a chart; turning it daily would make that chip a day-over-day number under a label that says "Range net".

## Axis thinning (M126)

The range decides how many POINTS exist; the viewport decides how many of them can carry a LABEL. `frontend/src/components/charts/thinning.ts::axisTickInterval(points, widthPx)` returns Recharts' `interval` (how many points to skip between labels):

```
budget = max(MIN_TICKS=2, floor(widthPx / TICK_BUDGET_PX=90))
interval = points <= budget ? 0 : ceil(points / budget) - 1
```

so 365 points render as ~4 labels at 390/412, ~8 at 768 and ~14 at 1280 — every responsive width the app supports, pinned in `frontend/tests/chartRange.test.ts`. Charts read the live viewport through `useViewportWidth()` (a `resize` listener), so rotating a phone re-thins the axis. The formula never returns a negative step and never drops below `MIN_TICKS` — an axis with one label is a lie about having a scale.

## Report definitions

Shared population rules:

- Committed data only: transactions whose batch has `import_batches.status='committed'`, and likewise trades staged by the investment-CSV import (docs/03 staging model). Manually entered rows (`import_batch_id IS NULL`) are always in scope; the shared predicates are `services/txn_filters.committed_tx_conditions` and `services/trade_filters.committed_trade_conditions`.
- Split transactions (M107, docs/44 §2): **children are counted, split parents are excluded** — the LEDGER rule (`countable_tx_conditions` = `committed_tx_conditions` + `is_split = false`; the list route, budgets, dashboards and the account cash-balance / running-balance rows compose the same `LEDGER_EXCLUDE_SPLIT` predicate onto their own scope). **Totals are preserved exactly**: children sum to the parent in native *and* CAD, so a split moves money between *categories* and never between report totals — every figure on every tab, the honest-pct denominators and the 8-decimal wire strings are byte-identical before and after a split (`test_ledger_population_counts_children_and_never_parents`). Every remaining consumer reads `GET /transactions`, which is LEDGER by construction — so a split needs no special-casing anywhere. (The client-side `splitReportParity` half of that proof retired with the engines in M160; the backend LEDGER test is now the only witness, and it asserts the same invariance on the server's own population.) (Until M160 the browser engines `reportEngine` / `sankeyEngine` / `totalReturnEngine` also needed **zero** code change for the same reason; two of the three are now retired and only `sankeyEngine` remains.)
- CAD conversion uses the `fx_rates` cache exclusively; an amount with no resolvable cached rate is excluded from aggregates rather than fetched or silently backfilled.
- **Transaction-level transfers are not spending.** A row IS a transfer under exactly two spellings, both written by the app's own transfer paths: `transfer_pair_id IS NOT NULL` (the detect / link / accept pairing, docs/19), or booked to the tenant's SYSTEM `transfers` category (what someone sets by hand when there is no counterpart to pair). `services/txn_filters.py` owns the definition once — `transfer_exclusion_conditions` the SQL form (its flag category resolved INSIDE the statement as a scalar subquery, so gaining the rule never costs a report a SQL statement and its docs/34 query budget), `transfers_category_ids` the id list and `is_transfer_row` its Python twin for readers already holding that list — and the two spellings are pinned to agree (`test_python_row_filter_and_sql_conditions_always_agree`). Spend-shaped readers compose the SQL form: directly (budget roll, insights, matrix, the recurring streams) or through `_committed_transactions(exclude_transfers_for=…)` (spending series, category breakdown, cash flow, Sankey). Every SPEND-shaped figure composes it in: the spending series (§1), the category breakdown (§2), the category × tag matrix, the budget roll (docs/26), insights, and forecast's recurring streams. Cash flow drops transfers from **both** sides (§3), so a pair can never show up as income while vanishing from expense. Net worth, account balances, goals, account-level cash flow and total return deliberately do NOT: a transfer moves money between the user's own accounts, and dropping a leg there would delete the money. Trade-level `transfer_in | transfer_out` is the same rule on the trade side (§3, §8).

### 1. Spending

Committed transactions with negative `cad_amount`, summed, grouped by bucket × category — or by bucket × tag when `group_by=tag` (§Tag dimension, M124).

- `category_id NULL` renders as name `"Uncategorized"` — its own slice, never dropped or merged. Under `group_by=tag` the parallel NULL bucket renders as `"Untagged"` (a transaction with no tags), on the same rule.
- **Both responses carry `spend_total_cad`**: every transaction counted exactly ONCE, whatever `items` did with it. Under the tag lens `items` sum to more than that (multi-tag duplication), and the caption labels the difference rather than hiding it.
- **Transaction-level transfers are excluded before grouping** (Shared population rules): both legs of a `transfer_pair_id` pair and every row booked to the system `transfers` category are dropped, so `spend_total_cad` and every bucket count only money that was actually spent. Inter-account movements that arrive as `transfer_in`/`transfer_out` TRADES (docs/02) stay excluded on the trade side as before.

### 2. Category breakdown

Same population over the whole range; total per category ROLLED UP TO THE TOP-LEVEL ANCESTOR (self-referencing `parent_id` tree, docs/02) — children's amounts fold into their root. Sorted descending; `Uncategorized` is its own entry. Under `group_by=tag` (§Tag dimension) there is no tree to roll up: the same population is sliced by tag, `Untagged` is its own entry, and the items carry `tag_id` instead of `category_id`.

### 3. Cash flow

- **Income** = positive transaction `cad_amount` + trade cash INFLOWS `dividend | distribution | return_of_capital | contribution` (input conventions docs/02: `quantity=0`, `price_native` = gross cash amount in native currency; the set is `CASH_INFLOW_ACTIONS`, `backend/app/models/trades.py`).
- **Expenses** = negative transaction `cad_amount` + trade cash OUTFLOWS `fee | withdrawal` + `commission` recorded on `buy | sell | drip` (`CASH_OUTFLOW_ACTIONS`).
- **EXCLUDED:** principal of `buy | sell | drip | corporate_action_adjustment` — position changes, not income/expenses. This exclusion is deliberate; counting buys/sells as expenses is the #1 implementation mistake here.
- **EXCLUDED (both sides):** `transfer_in | transfer_out` — share movements docs/06 treats as in-kind acquisitions/dispositions, not cash flows, so they are income and expenses of nothing (`CASH_INFLOW_ACTIONS`/`CASH_OUTFLOW_ACTIONS` deliberately omit them; M81 corrected a bullet here that listed them as Income/Expenses — §8 was right and depends on this: its balancing `drawn` source is what still makes the graph conserve).
- **EXCLUDED (both sides, transactions):** transfer rows (Shared population rules) — the transaction twin of the trade rule above. BOTH legs drop, so a chequing → TFSA move raises neither Expenses nor Income (counting one leg and not the other would print a transfer as net profit). Net worth and account balances keep counting them: there the money really did move.
- Trade cash converts at `fx_rates(row.currency → CAD)` on the trade date, cache only — `row.currency` is `trades.currency`, the ROW's own code, falling back to `account.currency` when it is NULL (pre-M132 rows); `trades.fx_rate_used` remains a display copy (docs/06 FX precedence). Every surface that converts trade cash — this report, the sankey, the portfolio-value cash leg and total return — uses the SAME row currency (M132, docs/05 §Portfolio value), so a USD buy in a CAD account is USD money everywhere.

### 4. Net worth history

Per bucket, valued at the bucket-END date:

```
net_cad      = cash_cad + positions_cad
cash_cad     = Σ_accounts  (starting_balance + Σ committed amount_native through bucket end)
               × fx_rates(account.currency → CAD, newest rate ≤ bucket end)
positions_cad = Σ_holdings  signed_qty × newest prices.price ≤ bucket end
               × fx_rates(listing_currency → CAD, newest rate ≤ bucket end)

assets_cad     = Σ over account contributions > 0  + max(positions_cad, 0)     (M97)
liabilities_cad = Σ over account contributions < 0  + max(−positions_cad, 0)
```

- **The assets/liabilities split (M97) routes the SAME signed sum by sign** —
  one account's contribution is its cash balance *after* FX, so a credit card,
  line of credit or overdraft (negative) is a liability and an overpaid card
  (positive) is an asset. By construction `assets_cad − liabilities_cad` equals
  `cash_cad + positions_cad` on the raw sums, so `net_cad` does not change; it
  exists only so the net-worth tooltip can print the two numbers it is made of
  (docs/09 §Tooltips). Each cell quantizes independently (half-up), so the two
  rounded cells can differ from `net_cad` by a cent — exactly the rule already
  pinned for `cash_cad + positions_cad` above. The route computes the same
  split (the `lib/reportEngine.ts::netWorthReport` mirror retired with M160);
  the CSV export keeps its four pinned columns
  (`bucket_end,cash_cad,positions_cad,net_cad`).

- **`GET /reports/net-worth` computes this — one implementation since M160 (P2-5).** The inputs and the arithmetic above are the route's own: committed transactions + committed trades (opt-in `GET /trades?committed_only=true`), all price rows, and a cache-only fx range; exact fixed-point sums, then one HALF_UP quantize per cell — `net_cad` rounds from the RAW `cash+positions` sum, so it is not always the sum of the two rounded cells. The S2b browser port (`lib/reportEngine.ts::netWorthReport`) that used to be diffed against it to the string was retired by M160, so the string-equality property is now a property of a single code path rather than two that must be kept in step.
- Price precedence: `manual` rows win over `yfinance` (docs/07).
- Positions count for ALL account types incl. registered plans — net worth ≠ taxable income (docs/05).
- Credit-card / LOC negative balances enter via the signed cash sum.
- When no price exists ≤ bucket end for a holding: fall back to the EARLIEST known price, flag nothing (v1 simplicity, documented here).
- Balances are cumulative sums through bucket end, so quiet buckets carry forward; buckets before the first transaction compute `0`.

### 5. Investment performance

Per security within `[from, to]`:

- `realized_cad` = gains/losses on in-range sells from the capital-gains engine (docs/06), non-registered accounts only. **The single implementation: `backend/app/tax/reports.py`** (the route behind every format). Until M160 the browser ran a deliberate mirror of it — `lib/reportEngine.ts::investmentPerformanceReport` over `computeUserTaxState` (ACB replay, denial fixpoint, exact BigInt allocation math) — pinned byte-identical to live server JSON by the golden fixtures, with a change-BOTH-sides rule. **M160 (P2-5) retired that mirror and the S4 `reportEngine.ts::capitalGainsReport` JSON mirror**, so `GET /reports/capital-gains` is now the only implementation serving `format=json` / `csv` / `html`, the MCP tool `reports_capital_gains` and the backend parity tests. The frozen goldens (`s4-vectors.json`, the investment-performance fixtures) stay on disk as SERVER-ROUTE regression pins replayed by `backend/tests/test_m160_replay_s4.py` and `backend/tests/test_reports_performance.py` — there is no second side left to keep in step.
- Non-registered holdings additionally report current unrealized figures against their pool (`acb_pools`, docs/02): `unrealized_cad = quantity × newest_price − acb_pool_cad`.
- Registered holdings report `market_value_cad` with `acb_pool_cad=null`, `unrealized_cad=null` — cost tracked for reference only (docs/05).

### 6. Dividend income (M14)

Income actions (`dividend | distribution | return_of_capital` — input conventions docs/02) for one calendar year, per security:

- Gross cash received (`price_native`) converted to CAD at the TRADE-DATE rate from the `fx_rates` cache; rows whose rate is unresolved are counted in `unresolved_fx` and excluded from every CAD total.
   - `by_month` is always exactly 12 zero-filled buckets (Jan–Dec) of the selected year (optional — `year` defaults to the latest available / current year, see Endpoints); `ttm_total_cad` is the trailing-365-day total across all securities under the same unresolved rule.
- Reporting only — gross cash received, no tax characterization (docs/06 untouched). Yield-on-cost deliberately omitted: book cost is per-account and no cross-account convention exists.
- **Provenance and freshness (docs/07 §Dividend-view provenance).** The JSON payload carries a top-level `provenance` block so the panel can say WHERE its figures came from and HOW old the evidence is: `income_trades` (ledger; `as_of` = newest recorded income payment in the selected year, never stale because a recorded fact does not age) plus — only when that year converts a foreign denomination — `trade_date_fx` (market data: the distinct `fx_rates.source` values for the bases used, `as_of`/`age_days` of their newest cached rate, `missing` when `unresolved_fx > 0`, `stale` past the 7-day carry-forward window). The block is JSON-only: `format=csv` stays a row export of `items`, and a CAD-only report lists exactly one source instead of claiming a market-data dependency it does not have.
- **Server-computed (the S2c browser port retired in M160).** `GET /reports/dividends` is the only implementation: it computes over committed income trades with cache-only fx (per-row HALF_UP cents on the trade-date rate, TTM window from a server-side `today`). The `lib/reportEngine.ts::dividendReport` port and its `s2cDividends` parity test were deleted in **M160** (M134 P2-5); the frozen golden `tests/fixtures/s2c-vectors.json` stays on disk (M158: keep the goldens) but is not replayed yet — its only reader was the `s2cDividends` mirror test this wave deleted; the route's own arithmetic is pinned by `backend/tests/test_dividends.py` (docs/33 A9). The endpoint contract below governs every consumer, CSV and UI alike.

### 7. Investment projections (M15)

Deterministic future-value scenarios over a starting value with monthly compounding — pure math, no DB reads:

- `monthly_rate = (1 + annual_return_pct/100) ** (1/12) − 1`; each month the balance grows FIRST, then `annual_contribution / 12` is added at month end.
- One row per year k: `contributed_cad = annual_contribution × k` (start value not counted as contributed), `value_cad`, and `value_real_cad = value_cad / ((1 + inflation_pct/100) ** k)`.
- Bounds (422 envelope outside): years 1–50, inflation 0–20%, return −50–50%, 1–8 scenarios, label ≤40 chars. Zero/negative returns and zero amounts are legal inputs.
- **The UI runs this in the browser** (`frontend/src/lib/projectionEngine.ts::runProjectionEngine`): same payload, same response shape, zero network calls. The Reports tab used to POST first (M61 #2), so in the read-only demo tenant the headline calculator rendered `ErrorPanel` (`403 demo_account_restricted`) and drew no chart at all. The run is pure math, so nothing is lost by computing it locally; `POST /projections/run` stays for API consumers and CSV-free tooling. Bounds are validated client-side too and surface as `ProjectionInputError` (`code: validation_error`) in the same ErrorPanel — the endpoint still 422s for direct callers.
  **[SUPERSEDED by M157 — K-12 of the M135 server-side restore]** the client-only posture above was a *demo write-guard artifact*, not an E2EE one (M135 §2 K-12). `/projections/run` is pure request-body math — the handler takes no `Session` and writes no row — so it joined `DEMO_MUTATION_EXEMPT_PATHS` (docs/20 exemption 6) and the demo tenant runs projections like any other tenant. **`ProjectionsPanel` now calls `POST /projections/run`**; the client engine stays as the offline / E2EE fallback, output-identical to the server under the 7 frozen goldens in `tests/fixtures/projection-parity.json`, and retiring it is M160's ruling (docs/33 §Server-first rule 5).
  **The server owns the bounds now**, so an out-of-range input costs one round trip and renders the panel's bounds copy from the 422 `validation_error` envelope (`detail` there is a pydantic-shaped **list**, never a sentence). The endpoint's envelope is pinned byte-for-byte by `backend/tests/test_projections.py::test_run_validation_envelope_is_byte_identical`.
- **Rounding.** `backend/app/services/projections.py` and `backend/app/api/projections.py` round through the single canonical helper `app.tax.acb.cents` at `ROUND_HALF_UP` (docs/02 Conventions); they hold no private `_cents` copy. Note the pure-math contract above still holds at runtime — `project_scenario` takes no `Session` and opens no query — but the module's import graph now includes `app.tax.acb` and therefore the ORM modules, so "no db imports" is true of the function, not of the module's transitive imports.
- **Numeric contract (M65): exact, not float.** The port runs on fixed-point `BigInt` at 40 fraction digits: `(1 + annual_return_pct/100) ** (1/12)` is solved by integer binary search on `(1+m)^12 ≤ y` (correctly rounded to the scale — no ln/exp to get wrong), the monthly loop rounds each product once, and `contributed_cad` / `total_gain_cad` stay exact scaled decimals quantized ROUND_HALF_UP (ties away from zero) exactly like `_cents`. One deliberate deviation: a gain of exactly zero prints `0.00`, where Python's `Decimal` carries a signed zero and prints `-0.00`. Parity is measured, not asserted: 207 reference cases (16,565 series rows) generated from `backend/app/services/projections.py` compare byte-identical (probe in `cache/scratch/m65/`). **Since M157 the standing pin is the 7 frozen goldens in `tests/fixtures/projection-parity.json`, checked in BOTH directions on every push**: the client mirror by `tests/projectionEngine.test.ts`, the server route by `backend/tests/test_projections.py::test_run_matches_the_frozen_m65_golden`, and the live route as the demo tenant by `node frontend/scripts/parity/parity.ts --slice projection`.

### 7a. Net-worth what-if forecasting (M109, docs/45)

A separate engine — `backend/app/services/forecast.py` — but built strictly ON
the definitions above rather than beside them: the starting `cash_cad` /
`positions_cad` come from **§4's** last bucket (`net_worth_report(as_of, as_of,
"day")`) and the trailing income/expense averages come from **§3's**
`cashflow_report` over the configured window, so the assumptions panel prints
the same figures the Cash-flow and Net-worth tabs already show. §7's own
compounding order (growth first, month-END contribution) is what the forecast's
monthly anchor follows, and §7's integer-year deflator `value / (1+i)^k` is
re-expressed there over the exact day fraction, because a forecast samples
sub-year buckets. The endpoint family, the five typed life events, the
determinism guarantee and the honest labels are pinned in **docs/45**.

### 8. Cash-flow Sankey (M77)

A three-column money-flow graph for the Cash-flow tab: **sources → one
`Money in` hub → destinations**. Nobody ships this well (Actual's own issue
#1919 is still open), so the shape is pinned exactly here.

- **Population and signs are §3 (Cash flow) exactly.** Positive transaction
  `cad_amount` and trade cash INFLOWS become sources; negative transaction
  `cad_amount`, trade cash OUTFLOWS and `buy | sell | drip` commissions become
  destinations. Unresolved (`cad_amount IS NULL`) and zero rows are excluded
  from both sides, staged rows never appear (docs/03), and `category_id NULL`
  becomes its own node with the literal id `"null"` and label `Uncategorized`.
- **Categories roll up to their TOP-LEVEL ancestor** (`_top_ancestor`), so
  `Groceries` under `Food` lands on `Food`. Node ids are `src:cat:<root id>` /
  `dst:cat:<root id>`; trade nodes are `src:trade:<action>` /
  `dst:trade:<action>` (commission is the single shared node
  `dst:trade:commission`).
- **Trade principal is not income or expense** (§3), but `buy | drip`
  principal IS money that left the cash pool for securities, so it lands on
  the `Investments` destination (`dst:invested`); whatever surplus is left
  lands on `Savings` (`dst:saved`).
- **Balancing node.** `surplus = income − expenses − invested`. Positive →
  the `Savings` destination; negative → a `Transfers & savings in` SOURCE
  (`src:drawn`) so both columns always sum to `money_in_cad`. The drawn source
  also covers the `transfer_in | transfer_out` share movements docs/06 treats
  as in-kind acquisitions/dispositions and §3 deliberately excludes.
- **Exact to the cent.** Every converted row is quantized to cents ONCE before
  it is summed, so all sums are exact integer cents and
  `money_in_cad = income_cad + drawn_cad`, `money_in_cad = saved_cad +
  invested_cad + expenses_cad`. That is at most one cent of divergence per
  trade row from §3's end-of-sum rounding — deliberate, and what makes the
  graph conserving.
- **Only one link shape exists.** No per-source allocation is derivable (money
  is fungible), so sources sum into `Money in` and destinations fan back out;
  the chart never invents "salary paid for groceries".
- **Ordering** is amount DESC, then label ASC (code points, not UTF-16 units),
  then node id — identical on both implementations.
- **Computed by the route** (`backend/app/services/reports.py::cashflow_sankey`)
  from committed rows + categories + a cache-only fx range. The panel READS it
  since **M160-R2 (t_85311c34)**: `api/reports.ts::getCashflowSankey` is a thin
  GET of `GET /reports/cashflow-sankey` (JSON-only — a graph has no flat table,
  so there is deliberately **no** `format=csv` twin, same decision as
  `/projections/*`). The browser twin `frontend/src/lib/sankeyEngine.ts` is no
  longer the panel's implementation: M160-R1 ruling 3 kept the module as the
  reference implementation the route was written against, and M160-R2 re-homed
  the captured golden `frontend/tests/fixtures/m77-vectors.json` (3 ranges) as a
  ROUTE replay in `backend/tests/test_m160_replay_m77.py`, replacing the
  client-comparison harness (`m77Parity.test.ts`) M160 deleted. The route's own
  arithmetic is pinned hermetically by `backend/tests/test_reports_sankey.py`.
- **Rendering** is a hand-rolled SVG (`components/charts/CashflowSankey.tsx`)
  — the locked stack has no d3-sankey and this slice adds no dependency. It
  follows the Chart accessibility contract below: `ChartFrame` carries
  `role="img"` + one `chartAriaLabel` sentence (money in, largest source,
  largest destination, invested, saved) and the `<svg>` tree is `aria-hidden`.
  Node colours come from `useChartPalette()` semantic tokens only —
  `GAIN` income, `LOSS` spent, `WARN` drawn, `ACCENT_SOFT` invested, `ACCENT`
  hub — no literal colours. No entrance animation runs, satisfying
  `prefers-reduced-motion` by construction. Empty graph → `NoDataPanel`
  (never an empty-chart crash).


## Endpoints

All JWT, mounted under `/api/v1` following docs/08 conventions:

| Method/Path | Shape |
| --- | --- |
| `GET /reports/spending?from=&to=&granularity=&group_by=&category_id=` | **M124** — `{from, to, granularity, group_by, items, total_cad, spend_total_cad}`; item shape follows the dimension (§Tag dimension); `category_id` is the drill filter (§Category drill-down) |
| `GET /reports/spending/categories?from=&to=&group_by=` | **M124** — `{group_by, items, total_cad, spend_total_cad}` — root roll-up, descending (or tag slices under `group_by=tag`) |
| `GET /reports/category-tag-matrix?from=&to=` | **M124** — `{from, to, columns, rows, items, total_cad}`, long form; §Category × Tag matrix |
| `GET /reports/cashflow?from=&to=&granularity=` | `{from, to, granularity, buckets: [{bucket, income_cad, expenses_cad, net_cad}]}` |
| `GET /reports/net-worth?from=&to=&granularity=` | `{from, to, granularity, buckets: [{bucket_end, cash_cad, positions_cad, net_cad}]}` |
| `GET /reports/investment-performance?from=&to=` | `{items: [{security_id, symbol, quantity, realized_cad, unrealized_cad \| null, acb_pool_cad \| null, market_value_cad}], totals: {realized_cad, unrealized_cad, market_value_cad}}` |
| `GET /reports/dividends?year= (optional — defaults to latest available / current year)` | `{year, available_years (desc), items: [{security_id, symbol, name, dividend_cad, distribution_cad, roc_cad, total_cad, payment_count, last_payment_date}], by_month: [{month, total_cad}] (12 zero-filled), totals: {dividend_cad, distribution_cad, roc_cad, total_cad}, ttm_total_cad, unresolved_fx, provenance: {status (ok/stale/missing), as_of (date-string or null), sources: [{key, kind (ledger/market_data), label, provider, as_of, age_days, status, detail}]}` — computed client-side since S2c (docs/33); route kept for `format=csv` |
| `POST /projections/run` | `{start_value_cad, annual_contribution_cad, years, inflation_pct, scenarios: [{label, annual_return_pct}]} → {start_value_cad, years, inflation_pct, scenarios: [{label, series: [{year, contributed_cad, value_cad, value_real_cad}], final_value_cad, total_contributed_cad, total_gain_cad}]}` — **M157: the Reports calculator's own source of truth**, and a documented demo-write exemption (docs/20 §6) because the handler takes no `Session`: the demo tenant runs it too. Errors: `422 {detail: [<pydantic error>], code: "validation_error"}` outside the §7 bounds |
| `GET /projections/defaults` | `{start_value_cad: string \| null}` — current portfolio market value CAD for UI prefill |
| `GET /reports/cashflow-sankey?from=&to=` | **M77, §8.** `{from, to, columns: ["source","total","destination"], nodes: [{id, column, label, amount_cad, group}], links: [{source, target, amount_cad}], totals: {income_cad, expenses_cad, invested_cad, saved_cad, drawn_cad, money_in_cad}}` — node ids and ordering pinned in §8. **UI reads this route since M160-R2 (t_85311c34)** — `api/reports.ts::getCashflowSankey` is `SankeyPanel`'s thin read; `lib/sankeyEngine.ts` survives only as the reference implementation (M160-R1 ruling 3) and still owns the `SankeyReport` wire shape, with the frozen golden replayed against the route by `backend/tests/test_m160_replay_m77.py`. JSON-only: a graph has no flat table, so there is **no** `&format=csv` twin (same as `/projections/*`). Errors: `422 invalid_date_range` for `from > to`. |
| `GET /reports/total-return?from=&to=` | **M77, docs/05 §Total return.** `{from, to, status: "ok" \| "no_data", start_value_cad, end_value_cad, net_flow_cad, income_cad, twr_pct: string \| null, mwr_pct: string \| null, valuation_days, active_days, flow_days, account_count}` — both returns are PERIOD (never annualized), fixed 8-decimal plain strings, `null` when undefined (M65 honesty, never a numeric 0). **This route computes it** — the `lib/totalReturnEngine.ts` client port retired with M160 (P2-5) and its shim with M160-R2 (t_85311c34), which is when `TotalReturnMetrics` started reading this route through `api/reports.ts::getTotalReturn`; the frozen `m77-vectors.json` golden replays against it in `backend/tests/test_m160_replay_m77.py`. JSON-only (no `format=csv`). Errors: `422 invalid_date_range`. |

- Derived cells: cash-flow `net_cad = income_cad − expenses_cad`; net-worth `net_cad = cash_cad + positions_cad`; `totals` sums each field over non-null item values.
- Every REPORT endpoint above accepts `&format=csv`: flat rows of `items`/`buckets` (totals omitted), header names pinned to the JSON field names. M124 columns follow the dimension: `spending` → `bucket,category_id,category_name,total_cad` or `bucket,tag_id,tag_name,total_cad`; `spending/categories` → `category_id,name,cad_total` or `tag_id,name,cad_total`; `category-tag-matrix` → the long-form five. **Exceptions, both deliberate:** the `/projections/*` routes and the two M77 routes (`/reports/cashflow-sankey`, `/reports/total-return`) are JSON-only — a graph and a pair of window returns have no flat table to export.
- Existing `GET /reports/capital-gains?year=` (docs/08) is unchanged by this spec.

## Chart mapping (Recharts, locked stack docs/01)

New guarded page `/reports` (docs/09): one tab per family, a granularity switcher, and a date-range picker defaulting to YTD.

| Tab | Rendering |
| --- | --- |
| Spending | Stacked BarChart — one bar per bucket, stacked by category; **since M108** a second variant, a **treemap** (one block per category over the SAME `items`, chosen by a `Chart view` segmented control in the page header — see below). **since M124** the stacking dimension follows the `Group by` control (`Category` \| `Tag`): the bars, the treemap and the donut all read the same dimension-agnostic `items`, so a tag lens is one control flip and adds no chart. |
| Categories | PieChart donut (`innerRadius`) over the root roll-up; **since M124** a **Category × Tag matrix** table below it (heatmap tint, CSV export) — see §Category × Tag matrix |
| Cash flow | ComposedChart — ± bars for income/expenses + net line; **since M77** a hand-rolled three-column Sankey SVG below it (sources → `Money in` → destinations) with a totals line (Money in · Spent · Invested · Saved) and its own query / skeleton / `NoDataPanel`, so an empty graph never hides the composed chart |
| Net worth | Stacked Areas (cash, positions) + net Line |
| Performance | Sortable table + horizontal bar of unrealized by symbol |
| Dividends | Year selector + monthly BarChart over `by_month` + per-security income table |
| Projections | Multi-line LineChart (one solid line per scenario; first scenario's `value_real_cad` dashed) + results table |

An empty result range renders a "No data in range" panel — never an empty-chart crash. Zero-fill guarantees continuous axes, not content.

### Owner dimension (M108 P3)

A second grouping for the two report definitions whose ENTIRE population is
transactions: **§1 Spending** (`grouped by bucket × category`) and **§2
Category breakdown** (per category). The Reports header gains a `Group by`
segmented control on exactly those two tabs — `Category` (default) | `Owner`
as M108 shipped it, `Category` | `Tag` | `Owner` since M124, with the `Owner`
option present only while `GET /partner/state` answers `{linked: true}`.

**Which rows are in each lens.**

| lens | rows | query surface |
| --- | --- | --- |
| `Category` (default) | the caller's own committed transactions — **byte-identical to the pre-M108 path** | none new |
| `Owner` | the caller's own rows **plus** the owner's rows delivered by `GET /partner/transactions` (M93 allowlist, docs/43 §6), as ONE server-computed population | **M159/M179**: `GET /reports/spending?scope=household`, `GET /reports/spending/categories?scope=household` and `GET /reports/category-tag-matrix?scope=household` (docs/08) — the browser renders; it does not derive (docs/33 §Server-first rule 1). Every panel of the two tabs reads this scope, the drill included |

- **M159 moved the Owner lens' compute server-side.** `scope=household` on the
  two routes above unions the same two row sets the pre-M159 browser engine
  was handed (own + the M93 projection) and runs the SAME aggregations as the
  narrow scope — one param, one population, one math. The lens therefore asks
  for ONE call per tab and renders what the server answers; the client engines
  (`getSpendingOver` / `getSpendingCategoriesOver` in `api/reports.ts`) stay as
  the documented rule-5 fallback and are no longer called by this page
  (M135 §4.3's parity gate: `parity.ts --slice household`, 0 diffs, before the
  swap; M160 owns the engines' retirement).
- **The Me/Partner split is retired with the compute that drew it.** Before
  M159 the browser held BOTH row sets, so it could fold them into two series
  per period — `Me` (own) and `Partner` (projection). The server shape the PM
  ruling pinned (M158 Decision 1) is a UNION, which cannot split the two
  populations, so the lens now draws ONE series per bucket × category over the
  household total, titled *"Spending by category — own + shared household"*,
  with the caption naming the population exactly as before (`Range total (own +
  shared household)`). The two-slice donut went the same way.
- **Category and tag names resolve from BOTH tenants' trees.** A projected row
  carries the OWNER's category/tag ids (docs/43 §6), so the household scope
  loads the counterpart's tree alongside the caller's; where the two trees
  disagree on a bare id the counterpart's entry names it. Without that, a
  foreign id would label as `Uncategorized` — the pre-M159 browser engine's
  behaviour, invisible while the lens collapsed to Me/Partner and now fixed at
  the source.
- **Zero household queries, and no option it cannot fill, for a lone tenant.**
  Since M124 the CONTROL itself renders on both tabs for every session, with
  `Category` | `Tag`; only the `Owner` option is appended to it, and only when
  `GET /partner/state` answers `200 {linked: true}` (M92's shape, so an
  unlinked caller gets a normal `200 {linked: false}` rather than an error).
  No partner ⇒ no Owner option, no empty dimension, and `scope=household` is
  never requested. The OWNER seat lands the same way: the link row is keyed on
  `SharingLink.partner_user_id`, so the owner's own answer is
  `{linked: false}` and their Reports never offers Owner — a household scope
  would be their own rows anyway: M90's grant is one-directional
  (docs/43 §2), partners write nothing (docs/43 §12).
- **Only these two tabs.** The M93 projection delivers exactly four data sets
  — `state`, `accounts`, `transactions`, `categories` (docs/43 §6; there is no
  `/partner/trades`). Cash flow's population mixes transaction rows with TRADE
  cash actions, and Net worth additionally needs the owner's
  `starting_balance` and price rows, so an "owner" split there would print a
  Partner row with **zero** dividend/fee income and a cash balance missing its
  starting balance — both wrong, and both exactly the class of figure docs/43
  §2 forbids. Owner is therefore offered on Spending and Categories only.
  *(The card that requested this named "spending/income tabs"; the income-side
  tabs are the ones that CANNOT carry the dimension honestly — stated here
  rather than papered over. Pinned by
  `frontend/tests/m108Reports.test.tsx::a linked partner gets the option on the
  transaction-backed tabs only`.)*
- **Labels.** The lens is labelled by POPULATION, not by owner: the chart
  title is *"Spending by category — own + shared household"* and the caption
  names the rows (`Range total (own + shared household)`, plus the line saying
  they come from the caller's ledger and the owner's allowlisted accounts —
  never a third account). The pre-M159 `Me` / `Partner` series labels retired
  with the browser compute that drew them (M159, above).
- **Grouping is the population, not a second aggregation.** The lens renders
  the SAME two report definitions (§1 Spending, §2 Category breakdown) over
  the household population — the server's `scope=household` answer, item for
  item what the narrow scope would return over the wider rows. Sums are
  INTEGER CENTS end to end: `0.10 + 0.20` in floats prints
  `0.30000000000000004` straight into a chart axis otherwise.
- **Privacy mode is respected by construction** (docs/43 §9, D4): a
  privacy-mode owner projects NOTHING into the household scope — the partner's
  household answer is their own rows, and the caption's `ownerScopeNote` says
  so. The pre-M159 browser engine skipped a projected row whose `cad_amount`
  was `null`; the server route has no per-row field to null, so the mask is
  population-level (`services/reports.py::_household_projection`), which is
  the same guarantee in the only shape an aggregate can carry. The owner's own
  view is untouched either way — the mask hides the figures from the PARTNER,
  never from the account holder.
- **M179 — the lens owns the whole TAB, not one panel.** Before M179 only the
  Spending chart and the Categories donut asked for `scope=household`; the
  Categories tab's **Category × Tag matrix** (`GET /reports/category-tag-matrix`,
  docs/08) and the **category drill** (`GET /reports/spending?category_id=` over
  the merged namespace) silently answered from the caller's own rows — or, under
  the lens, were not rendered at all. One screen therefore had a household donut
  above an own-rows grid. The scope is now a tab-level lens: the chart, the
  donut, the matrix and the drill are four views of ONE population, and
  `lensScope` is part of every one of their query keys, so switching the lens
  refetches instead of relabelling the previous population.
  - The matrix carries the same caption, ranking, top-8 cut and long-form cells
    as `own`, with category/tag names resolved from both tenants' trees; its CSV
    button carries the scope (docs/08's CSV-parity obligation).
  - The drill is a real control again: a household block/slice opens that
    category's *household* periods. `/transactions?category=` still describes the
    caller's own ledger only, so under the lens the drill **withholds that deep
    link and says so** rather than opening a different population — the drill
    chart is the household's numbers and the caption names them.
- **Owner-lens expansion: the transaction-only routes serve the scope.** `scope`
  is also `own|household` on `/reports/insights` and
  `/reports/spending-comparison` — the two routes of the M179 set whose ENTIRE
  population is committed transactions, which is exactly the one data set the
  M93 projection delivers (docs/43 §6). Each widens through the SAME
  `household_ownership` predicate, the SAME merged category namespace and the
  SAME signed/FX/LEDGER math the tab routes use (one param, one population,
  one math): on the test fixture the partner seat's `current_month_spending`
  and `current_period.total` move 30.00 → 130.00, the owner's UN-shared 55.00
  stays out, an owner seat's household is its own 155.00, and the projected
  rows' OWNER category ids resolve to their owner-tree names instead of
  `Uncategorized`/`Unknown`. Omitted and `scope=own` remain byte-identical,
  unknown and empty values are still 422 at `query.scope`, and neither route
  has a UI lens control — this widening is an API contract (docs/08), so no
  screen changed and no client sends the parameter yet.
- **Population refusal on the ten own-only routes (M179 ruling).** `scope`
  is not a global switch. It is an optional query parameter defaulting to
  `own` on `/reports/cashflow`, `/reports/net-worth`, `/reports/cashflow-sankey`,
  `/reports/total-return`, `/reports/investment-performance`,
  `/reports/capital-gains`, `/reports/dividends`, `/reports/dividend-calendar`,
  `/reports/cashflow-calendar`, and `/reports/networth-snapshot`. The sole
  accepted value is `own`;
  `household`, unknown strings and empty values receive FastAPI's existing
  query-validation 422 at `query.scope` (standard `validation_error` envelope),
  including each implemented CSV/HTML twin. Omitted and explicit `scope=own`
  remain byte-identical. The routes keep their existing own-tenant computations:

  | route | accepted population | reason household is refused |
  | --- | --- | --- |
  | `GET /reports/cashflow` | own only | mixes transaction rows and trade cash actions; no `/partner/trades` projection |
  | `GET /reports/net-worth` | own only | needs owner's starting balances and price rows |
  | `GET /reports/cashflow-sankey` | own only | same transaction + trade population as cashflow |
  | `GET /reports/total-return` | own only | portfolio valuations and cash flows require partner trades/prices |
  | `GET /reports/investment-performance`, `/reports/dividends`, `/reports/capital-gains` | own only | no partner trades, cost basis, or dividend ledger |
  | `GET /reports/dividend-calendar` | own only | projected income is derived from trade history |
  | `GET /reports/cashflow-calendar` | own only | combines sources including subscriptions and dividend trades |
  | `GET /reports/networth-snapshot` | own only | account balances and positions are not projected |

  Five routes therefore carry `scope=own|household`: the three Owner-lens tabs
  (`/reports/spending`, `/reports/spending/categories`,
  `/reports/category-tag-matrix`) and the two transaction-only expansions
  (`/reports/insights`, `/reports/spending-comparison`) — all through the ONE
  shared-account allowlist, privacy mask, committed-only predicate and
  arithmetic of docs/43 §6 (CSV parity wherever a twin exists). The
  disposition criterion is the population, not the consumer: a route answers
  `household` only when the projection covers everything it sums, and refuses
  otherwise rather than printing a narrower population under a household
  caption. `GET /budgets` remains outside this scope model.
  Exports remain caller-tenant snapshots; no `/export/*` behavior changes.

- **Audit correction and verification.** Audit t_98e06dd0 found no cross-tenant
  disclosure; it found a silent population refusal gap: FastAPI ignored an
  undeclared `scope=household` on these twelve routes and returned own-only
  numbers with 200. The contract above closed that gap by declaring a singleton
  `Literal["own"]` on all twelve — no `/reports/*` route ignores a `scope` it
  does not support — and the Owner-lens expansion then widened the two
  transaction-only routes of that set to `own|household`, leaving the other ten
  on the singleton. `backend/tests/test_reports_household.py` retains
  schema-driven route classification and pins the positive contract:
  omitted/own byte parity, unsupported values rejected at `query.scope` on the
  own-only set and (unknown/empty) on the widened set, implemented twins,
  successful partner-own responses with the owner's fixture markers absent, and
  the expanded routes' household union, merged namespace, privacy mask and
  deny-by-default. It also keeps the widened household controls, exports and
  budgets in their own regression tests. UI behavior is unchanged: the Owner
  lens appears only on the two transaction-backed tabs (the matrix and drill
  carry the same population); no UI sends scope to an unsupported route.
- **The caption names the population**: `Range total (own + shared
  household): …` plus a line stating that rows come from the caller's ledger
  and the owner's allowlisted accounts — never a third account. An owner who
  has shared no accounts yet gets that stated too.
- **Revocation is not a stuck skeleton.** `GET /partner/transactions` answers
  `401 sharing_not_linked` for every way a link can vanish (docs/43 §7); the
  page drops back to `Category` instead of waiting forever.
- **Saved views carry the lens**: `state.group_by ∈ {category, tag, owner}` (M124 added `tag`), stored
  only where the dimension is offered (docs/02 §users, docs/09 §Saved report
  views).

### Tag dimension (M124)

A third lens for the two transaction-only report definitions (§1 Spending,
§2 Category breakdown): the `Group by` segmented control becomes `Category`
(default) | `Tag` | `Owner` — the `Owner` option present only while
`GET /partner/state` answers `{linked: true}` (§Owner dimension), so a lone
tenant — or the owner seat — reads exactly `Category` | `Tag`. One GROUP-BY
swap and one chart parametrization —
**no new schema**, `transaction_tags` (docs/02) is already the join.

- **Aggregation.** Same population, same sign convention (negatives only),
  same LEDGER split rule, same zero-fill and same bucket keys as the category
  lens. A transaction carrying several tags lands in **every** one of its tag
  buckets, so the item totals can sum to MORE than was actually spent. That is
  the honest shape of a many-to-many grouping, and both responses carry the
  once-counted denominator `spend_total_cad` beside the duplicated
  `total_cad`, so the UI labels the difference instead of hiding it (docs/09
  §Tag dimension & drill-down).
- **The NULL bucket is `Untagged`** — a transaction with no tags is its own
  slice, never dropped or merged. Exactly parallel to the category lens's
  `Uncategorized`.
- **The item shape follows the dimension**, so a client knows which one it got
  from which fields are present, and the CSV headers stay pinned to the
  fields actually emitted (§CSV below): `category` keeps
  `{bucket, category_id, category_name, total_cad}` **byte-identical to
  pre-M124**, `tag` answers `{bucket, tag_id, tag_name, total_cad}`.
- **It composes with the drill filter** (§Category drill-down):
  `group_by=tag&category_id=<Groceries>` answers "this category, by tag".
- **The treemap needs no variant of its own** (out of scope by design): it
  reads the same `items`, so the dimension switch already hands it tag
  buckets.
- Charts are dimension-agnostic — `lib/reportEngine.ts` normalizes each item
  to `SpendSlice {bucket, key, label, total_cad}` and a `dimension` label
  drives the summary title ("Spending by tag"), so donut/treemap/bars take
  the lens with no per-chart branch.

### Category drill-down (M124)

Depth, not a second view. In the **category** lens, clicking a donut slice, a
treemap block or a stacked bar drills into ONE category's period breakdown —
the same bar-chart-over-time family as the Bills history chart (M116),
reusing `BillHistoryBarChart`'s props (`count` optional, `emptyMessage`
parameterized).

- **Population = the category AND its descendants** (the `parent_id` subtree,
  docs/02). That is what makes the drill return the number the clicked slice
  already showed — a leaf behaves as an exact match, only a parent widens.
  `Uncategorized` drills to the rows with no category.
- **Breadcrumb** (`<nav aria-label="Breadcrumb">`, a `← All categories` back
  button, then `/`, then the category name on `aria-current="page"`) is the way
  back, and leaving the tab clears the drill, so "back" always means the tab
  you came from. The ledger deep link (`/transactions?category=<id|uncategorized>`)
  stays as a secondary link under the chart.
- **Server parity**: `GET /reports/spending?category_id=<int|uncategorized>`
  applies the same filter. The browser computes the drill from rows it already
  holds (no extra round trip); the parameter exists for `format=csv`, the API
  and the tag lens inside a category.
- Zero-fill emits the **drilled** category, not `Uncategorized`.
- **No drill under `Owner`**: "Partner" has no `/transactions?category=`
  target, so the blocks stay static text (the M108 P3 rule, unchanged).

### Category × Tag matrix (M124)

The cross-view on the **Categories** tab — rows are categories, columns are
tags, cells are spend — answering "how much did I spend on *Groceries* tagged
*vacation*?" in one look. `GET /reports/category-tag-matrix?from=&to=`
(§Endpoints).

- **Rows are RAW categories with spend > 0, not root-rolled.** Groceries is a
  child of Food, and rolling up would erase the very row the user came for.
  This is the one place the matrix deliberately differs from the donut above
  it, and the caption says so.
- **Columns are the top 8 tags by spend** (descending, name tiebreak), plus an
  `Other tags` column that **absorbs** the spend of every tag past the cut —
  truncating silently would hide money (docs/09 honesty rule) — plus
  `Untagged` always last.
- **Cells are long form and sparse**: `{category_id, category_name, tag_id,
  tag_name, cad_total}`, ordered row-major by (row, column), zero cells
  absent rather than blank. A multi-tag transaction lands in each of its
  cells, so cells can sum past the report's `total_cad`, which is the
  once-counted figure the caption points at.
- **Two aggregate statements, never one per cell**: one
  `GROUP BY (category_id, tag_id)` over a LEFT JOIN through `transaction_tags`
  (LEFT so an untagged transaction still yields a row instead of silently
  dropping the money), plus one `GROUP BY category_id` for the once-counted
  row totals — the cell grid cannot be reduced to row totals by summing, and
  those totals are also what decides which categories are rows at all.
- **No pagination**: categories are ≤ ~100 and the column set is capped, so
  the whole table is one response. Heatmap tint uses the existing chart
  tokens; the table is a real `<table>` with row/column headers and a
  focusable horizontal scroll region (docs/09 §Scroll regions).

### Spending treemap (M108 P1)

A second shape for the **Spending** tab, chosen by the header's `Chart view`
segmented control (`Bars` | `Treemap`). It is a *view*, not a report: it reads
the SAME `spendingReport.items` (bucket × category) and the same palette, so
it can never disagree with the bars about a number, and it adds **no query
surface**.

- **Blocks.** `items` are re-aggregated over buckets by `category_name`
  (uncategorized included), sorted descending. A block's area is proportional
  to its amount — that proportion is the whole claim the chart makes. Rows
  whose aggregated amount is `0` (bucket zero-fill, §1) get no block: a
  zero-area block has no honest position.
- **Layout** is a hand-rolled squarified treemap
  (`frontend/src/lib/treemap.ts::squarify`) — the locked stack has no
  d3-hierarchy and this slice adds no dependency. Degenerate input (empty
  values, non-positive canvas) returns `[]`, which the component renders as
  `NoDataPanel`, never NaN geometry.
- **Colors** come from the existing `useChartPalette().SERIES_COLORS`, indexed
  by the SAME ordering `SpendingStackedBarChart` derives (by `category_id`,
  uncategorized last) — one category, one color, on both variants. No new
  palette, no new token.
- **Hover** shows the exact amount and the share of the range total, TRIMMED
  to 2 decimals by `lib/percent.ts::trimPct` (M105's rule — sliced, never
  re-rounded). The digits the trim hides stay on the legend button's
  `aria-label`, so assistive tech reads the raw share. The tooltip is
  `aria-hidden`: it is a pointer affordance, and the same numbers live on the
  legend control.
- **No inline labels on the blocks.** Every series fill in both palettes fails
  4.5:1 against a single fixed text color (white on dark-theme `#22d3ee` is
  1.7:1; black on light-theme `#65a30d` is 4.2:1), so names go in the legend —
  the same decision the donut already made.
- **Interaction.** Clicking a block drills into that category's Transactions
  view through the deep link the app already uses:
  `/transactions?category=<id>` or `?category=uncategorized` for `category_id
  NULL` (the URL shape `Transactions.tsx` applies on mount). The `<svg>` is
  `aria-hidden` inside `ChartFrame`'s single `role="img"` sentence (this
  section's contract), so a focusable element inside it would be an axe
  `aria-hidden-focus` violation — therefore the **legend below the chart is a
  row of real `<button>`s** (≥44px, focus-ring, one per block) and it is the
  keyboard path. Blocks get the pointer/touch path only.
- **Chart facts** (table above): top category + its share, grand total.

**Known gap, stated rather than papered over.** The card that requested this
variant described the drill-in as "the same deep-link the bar chart uses" —
the stacked bar chart has never had a click drill-in (verified: no `onClick`
on `SpendingStackedBarChart`, no `navigate` in any chart component). The
treemap therefore adopts the app's existing category deep link (the one the
sidebar and the global search use). Adding the same drill-in to the bars is a
separate, deliberately unclaimed change: a Recharts `<Bar>` click reports the
bucket row, not which series in the stack was hit, so it needs the hit-testing
the current component does not do.

## Chart accessibility (M58)

A chart is a graphic carrying real information, so every chart needs a text
equivalent (WCAG 1.1.1, Level A) — docs/09 requires the same shape everywhere.

**Contract**

1. The chart's **wrapper** element carries `role="img"` and one `aria-label`
   built by `chartAriaLabel` (`frontend/src/components/charts/aria.ts`).
2. The Recharts `<svg>` and its `<path>`/`<rect>` children are `aria-hidden` —
   assistive tech reads the one sentence instead of walking hundreds of nodes.
   Hiding the svg is what makes the wrapper label authoritative.
3. The label must answer **what** (title), **which series**, **over what
   range**, **how many points**, and **the numbers that matter** (endpoints for
   a trend, totals for a breakdown).

**Shape** — `chartAriaLabel(input)` returns
`"<title> — <kind>, <range>, <points> points. <fact>; <fact>."`

```ts
chartAriaLabel({
  title: "Net worth trend",       // what the chart is
  kind: "monthly buckets",        // granularity
  range: "Jan 2026 – Dec 2026",   // x-axis span, inclusive
  points: 12,                     // rendered data points
  facts: ["from $100,000 to $151,800", "latest $151,800"],
})
// → "Net worth trend — monthly buckets, Jan 2026 – Dec 2026, 12 points. from $100,000 to $151,800; latest $151,800."
```

`facts` is ordered most-informative first; an empty chart renders
`", 0 points. No data in range."` after the head, alongside the existing
`NoDataPanel`.

**Per-chart facts** (what each `facts` array must contain):

| Chart | facts |
| --- | --- |
| Net-worth trend (Dashboard) | first bucket net → last bucket net; latest net |
| Spending by category donut (Dashboard, Reports) | top category + its share; grand total |
| Spending stacked bars (Reports) | range total; peak bucket + amount |
| Spending / Categories **by owner** (Reports, M108 P3) | top group + its share; grand total |
| Spending treemap (Reports, M108) | top category + its share; grand total |
| Cash flow composed (Reports) | income total, expenses total, net |
| Unrealized bar (Reports, Performance) | largest unrealized position; total unrealized |
| Price history (Security detail) | first close → last close; 52-week high/low when in range |
| Running balance (Account detail) | first balance → last balance |
| Dividends monthly bar (Reports) | year total; best month + amount |
| Projections multi-line (Reports) | scenario count, horizon years, final value per scenario (≤3) |
| Exposure / category donuts (Investments) | dimension name, slice count, largest slice + share |
| Currency-exposure donut (Investments, `ExposureDonut`) | largest slice + share, plus **one fact per listing currency** through `charts/format.ts::fmtNative` (`163,260.73 CAD; 47,743.27 USD`) — never one summed total, because the slices are native values in different denominations and summing them is not an honest figure (docs/05 §Currency-exposure view). The shared `Donut` therefore takes `totalFact` for this case (docs/46 §D2, F-9). Its centre carries the holdings COUNT, which carries no currency or cents — the one formatter that was ever applied to the label's money fact |
| Exposure world map (Investments, M167) | countries shaded, largest country + share, count of buckets the geometry cannot place |

Charts also spread `{...chrome.anim}` from `useChartChrome()` so no tween runs
under `prefers-reduced-motion: reduce` (docs/09, Reduced-motion contract).

**Interactive charts use `role="group"`, not `role="img"`.** `role="img"`
promises "one node, no descendants with semantics", so a chart whose children
ARE controls (the drillable donut legend, M124; the geography choropleth's
shaded countries, M167) swaps the wrapper to `role="group"` with the SAME
`chartAriaLabel` sentence and makes each control a properly labelled,
focusable child. Everything else about the contract stands: one summary at the
top, no unlabelled node underneath it.

## Backend placement

- Aggregation service `backend/app/services/reports.py`: SQL aggregates; realized figures call the existing `backend/app/tax/reports.py` capital-gains functions (single source of gain math in Python — since S2b the browser holds a parity-pinned mirror of this path; see Local computation).
- Router `backend/app/api/reports.py`, mounted with the other resource routers (docs/08).
- M14 aggregation lives in `backend/app/services/dividends.py`; M15 pure math in `backend/app/services/projections.py` with router `backend/app/api/projections.py` — both mounted under `/api/v1` like the rest (docs/08).
