# 34 — API performance budgets & N+1 audit (M60)

Backend + tests + this spec. Makes "fast" a *tested* property: every heavy read
surface gets a **SQL-statements-per-request budget** that CI enforces, plus a
**p95 latency budget** that the same harness measures and reports. The audit
that produced the budgets swept **every feasible `GET` on the demo tenant**,
so the N+1s found are measured facts, not suspicions.

Source: candidate backlog, M60 (no `NEXT_WAVE_PLAN.md` section — this doc is
the milestone's plan of record).

## Context

Nothing in the repo pinned how many queries a request may issue. Two N+1
classes had shipped unnoticed, both scaling with a *user-controlled* number:

| surface | what the count scales with | measured before |
| --- | --- | --- |
| `GET /transactions` | page size (`limit`, up to 200) | 1 query **per row**: 53 at `limit=50`, **203 at `limit=200`**, 4 at `limit=1` |
| `GET /reports/net-worth` | bucket count (granularity) | 1 `SELECT securities` **per bucket**: 18 at `granularity=month`, **368 at `granularity=day`** (p50 553 ms) |
| `compute_user_tax_state` (feeds `GET /reports/capital-gains` + `GET /reports/investment-performance`) | number of non-CAD trades | 1 exact-date `SELECT fx_rates` **per trade**: 25 on the 74-trade demo seed |

A query-count budget catches all three at PR time; a latency budget alone would
not (the day-granularity net-worth report only *feels* slow on a warm cache).

### Plain statement of what is NOT enforced (no hiding)

- **Latency is not a CI gate by default.** Wall-clock on a shared CI runner is
  not a stable baseline, so `tests/test_perf_budgets.py` asserts p95 only when
  `PERF_LATENCY_GATE=1` is set. The measured table is printed as markdown in
  the test output on every run either way, and written as JSON to
  `$PERF_REPORT` when that variable is set.
- **A test that DOES pin a wall-clock budget enforces it only when the box can
  answer it.** `tests/conftest.py::latency_budget_blocker` is the single
  helper: it returns a skip reason when the 1-minute load average has reached
  the core count, and `None` on a quiet box. The sanctioned shape is *plan /
  query-count / functional assertions enforced unconditionally, measured
  milliseconds printed, stopwatch assertions last and behind the helper* —
  the numbers ride in the `pytest.skip` reason, so the evidence survives the
  skip instead of becoming a red that measures contention. It gates six pins:

  | pin | budget | what it measures |
  | --- | --- | --- |
  | `tests/test_bills_match.py::test_detail_first_paint_is_under_the_1_5s_budget_at_10k_rows` (docs/25 §M116 Wire) | 1.5 s | DB-bound first paint at 10k rows |
  | `tests/test_reports_tags.py::test_matrix_and_drill_are_indexed_at_10k_rows` (docs/12 §1, M124) | 1.5 s | two aggregates over 10k rows |
  | `tests/test_account_value_history.py::test_perf_366_days_by_six_securities_under_1_5s` (docs/08 §Performance) | 1.5 s | 366 daily buckets over the shared Postgres |
  | `tests/test_demo_seed.py::test_seed_is_deterministic_and_fast` | 3.0 s | `ensure_demo_account` ×2, CPU + DB |
  | `tests/test_import_dos.py::test_stray_quote_sanitizer_200k_quotes_well_under_two_seconds` (docs/03, audit H2) | 2.0 s | pure-Python 200k-quote sanitize |
  | `tests/test_import_dos.py::test_stray_quote_sanitizer_quote_space_lookahead_well_under_two_seconds` | 2.0 s | the quote+space lookahead scan |

  The first two reded on contention alone in one of two simultaneous full
  suites — drill 2355 ms at load 11 on 4 cores, green solo in 6.75 s for the
  whole file (t_428d4b99) — and the sweep that followed them (t_4980b7ed)
  converted the other four. AGENTS §5 runs suites concurrently, so a clock
  assertion that does NOT go through the helper is a flake generator: convert
  it when you touch it.
- **Two wall-clock pins are deliberately NOT gated, and this is why.**
  `tests/test_m148_pool_deadlock.py` (`loop_seconds < LOOP_BUDGET_S`, 1 s) and
  `tests/test_m149_offloop_routes.py` (0.5 s) assert that an event loop was
  **not frozen** by an in-flight blocking operation. They are *ratio* pins,
  not budgets: the frozen cost is the test's own injected stall — the pool
  checkout timeout (4 s) and the artificial 2 s stall — so what the clock
  discriminates is "answered immediately" against "answered only when the
  stall ended", and a loaded box slows both sides together. The probe is
  `/docs`, which takes no database connection and measured 7.1–18.4 ms
  across solo and loaded runs (load 3–7 on 4 cores), 25–140× inside budget.
  Gating them would turn the regression detector for a whole-API freeze into
  a skip exactly when the box is busiest — the one outcome the rule above
  exists to prevent. A budget is gated because it measures the box; these are
  kept because they measure the loop against a stall of known length.
- **`tests/test_security_hardening.py`'s `elapsed < 5.0` is functional, not a
  budget.** It asserts the regex engine's own 2 s timeout is *enforced*: an
  unenforced timeout hangs rather than slow-paths, so the assertion is a bug
  detector and must stay unconditional. Do not gate it.
- **The query-count budget is per-endpoint, not global.** An endpoint with no
  row in the budget table has **no budget** — the audit section below lists
  every endpoint that was measured, and the waivers section names the ones
  that are over a sane budget but were deliberately not fixed in M60.
- **There is no production slow-query logger.** M60 documents and uses a
  threshold in the measurement harness only; the hook point for a production
  one is `backend/app/api/deps.py::get_engine` (see "Slow-query log
  threshold"). No setting, no `.env.example` key, no runtime behaviour change.

## Decisions

1. **Query count is the enforced budget; p95 latency is the reported budget.**
   Query count is deterministic and machine-independent; p95 is not. Both are
   pinned in the table below; only query count gates CI.
2. **Budget = SQL statements executed per request**, counted with a SQLAlchemy
   `before_cursor_execute` listener on the app's own engine, measured through
   an in-process `TestClient` (no HTTP hop, no proxy). Statements counted
   include the authenticated `SELECT users …` issued by `get_current_user` —
   that is part of the cost of serving the request.
3. **Measurement dataset = the M10 demo seed plus a synthetic fx cache.**
   `fx_rates` is never seeded (docs/30 H7), so on a bare demo seed the
   fx-dependent endpoints take their `422 fx_rate_missing` short-circuit and
   the audit would measure the *error* path. The harness therefore loads one
   deterministic `USD→CAD` row per calendar day over the trailing 400 days and
   runs `app.tax.acb.rebuild_user_pools`, so capital-gains and
   investment-performance exercise their real computation path.
4. **Latency budget rule: 2× the measured baseline p95**, rounded up to a
   readable number. Stated as a rule so a future re-baseline is reproducible.
5. **Scale-independence is a first-class invariant, not just a ceiling.**
   Absolute budgets can be raised by mistake; the page-size/granularity
   invariants below cannot be satisfied by an N+1 at all.
6. **The budget table lives here (docs/34) and is mirrored verbatim in
   `backend/tests/test_perf_budgets.py::BUDGETS`.** This doc is the human
   source of truth; the test is the enforced copy. Changing one without the
   other is a §9 reconciliation failure.
7. **M52's `GET /reports/cashflow-calendar` gets a reserved row, not a
   number.** The endpoint does not exist (M52 is still PROPOSED per
   `NEXT_WAVE_PLAN.md`); inventing a budget for it would be a false spec claim.
   When M52 lands it must fill the row in the same commit.

## Measurement dataset (exact)

Created in a **module-scoped, hermetic database** derived from the suite
database name — `DATABASE_URL`'s database `D` → `D_perf` (e.g.
`fintrack_test_p123456` → `fintrack_test_p123456_perf`; the suite database
carries a per-session id so two runs never share it — AGENTS §5), dropped and
re-created with `WITH (FORCE)` through `tests/conftest.py::drop_database_force`
(that drop waits out a backend this role may not signal instead of failing the
fixture — AGENTS §5), stamped with `alembic upgrade head`, then:

1. `app.services.demo_seed.ensure_demo_account(session)` with
   `Settings.seed_demo_account = True` — the full M10 seed. Measured on
   2026-09-28: `transactions=370`, `trades=74`, `accounts=6`, `securities=7`,
   seed wall time 0.57–1.10 s.
2. 401 `fx_rates` rows (`base='USD'`, `quote='CAD'`, one per calendar day over
   the trailing 400 days, `source='manual'`, rate `1.30 + 0.02·(ordinal mod
   60)/60` rounded to 6 dp) — synthetic and deterministic, seeded only into the
   perf database, never into a shared one.
3. `app.tax.acb.rebuild_user_pools(session, user.id)` → `{'pools': 3, 'trades': 74}`.
4. `Settings.seed_demo_account` restored to `False`; `DATABASE_URL`,
   `get_settings.cache_clear()` and `app.api.deps._engine` restored on teardown
   exactly as `tests/test_demo_seed.py` does.

The perf client deliberately does **not** install `get_db` dependency
overrides: requests open real sessions against the perf engine, which is what
production does (the rollback-per-test `session` fixture would hide pool
behaviour and add savepoints to the count).

**Date window** (`from`/`to`): `to = today`, `from` = the 1st of the month
12 months before `today` → **13 monthly buckets** (393 daily buckets when
measured on 2026-09-28), i.e. a full year of history plus the current partial
month, matching the reports tab default and docs/12's history span.

## Measurement method (exact)

- **Transport:** FastAPI `TestClient` in-process. The app→Postgres hop is an
  SSH tunnel to example.internal (`localhost:15432`), so every statement carries one
  extra round trip — latency numbers are therefore an **upper bound**, and
  query count (not milliseconds) is the portable metric.
- **Counter:** `sqlalchemy.event.listen(engine, "before_cursor_execute", …)`
  increments a counter; `after_cursor_execute` records the statement's
  duration. Listener is attached for the measured window and removed
  afterwards.
- **Samples:** `PERF_SAMPLES` env (default **40**) timed requests per endpoint
  after **3 warm-up** requests that are not counted. **Run the latency gate with
  `PERF_SAMPLES=200`**: p95 over 40 samples is one outlier away from a flake
  (a single 432 ms scheduling stall on this host moved net-worth's 40-sample p95
  from 96 ms to 432 ms). The published before → after table uses 200.
- **p95:** nearest-rank on the sorted sample list
  (`lat[round(0.95 × (n − 1))]`).
- **Stability requirement:** every sample of an endpoint must return the same
  query count — `queries_stable` is asserted, so a flaky/conditional query path
  fails rather than averaging out.

## Budget table

`q` = SQL statements per request (enforced, `≤`). `p95` = milliseconds
(reported; asserted only with `PERF_LATENCY_GATE=1`). Baseline measured
2026-09-28, `PERF_SAMPLES=200`, seed `transactions=370 trades=74`.

| # | endpoint class | method + path (exact query string) | q before | **q budget** | p95 before | **p95 budget** |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | dashboard aggregate (donut) | `GET /api/v1/reports/spending/categories?from=YYYY-MM-DD&to=YYYY-MM-DD` | 3 | **3** | 48.3 | **90** |
| 2 | net-worth history (12 months) | `GET /api/v1/reports/net-worth?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=month` | 18 | **7** | 128.4 | **250** |
| 3 | cash flow (12 months) | `GET /api/v1/reports/cashflow?from=YYYY-MM-DD&to=YYYY-MM-DD&granularity=month` | 4 | **4** | 42.0 | **120** |
| 4 | transactions list, default page | `GET /api/v1/transactions?limit=50&offset=0` | 53 | **5** | 63.7 | **250** |
| 5 | transactions list, dashboard recents | `GET /api/v1/transactions?limit=10&offset=0` | 13 | **5** | 51.2 | **120** |
| 6 | investment performance | `GET /api/v1/reports/investment-performance?from=YYYY-MM-DD&to=YYYY-MM-DD` | 31 | **7** | 121.8 | **250** |
| 7 | capital gains (annual) | `GET /api/v1/reports/capital-gains?year=YYYY&format=json` | 27 | **3** | 45.7 | **150** |
| 8 | global search (docs/33 S1, M137 P0-1 fuzzy group) | `GET /api/v1/search?q=grocery&limit=5` | 6 | **6** | 31.0 | **120** |
| 9 | cash-flow calendar (M52) | `GET /api/v1/reports/cashflow-calendar?month=YYYY-MM` | — | **reserved** | — | **reserved** |

Row 9 is **not implemented** (M52 PROPOSED, `NEXT_WAVE_PLAN.md` §Wave 3); the
row exists so M52 cannot land without filling it.

**M108 P4 moved rows 4 and 5 from 4 to 5 statements.** The receipt-image flag
(`TransactionPublic.has_receipt`, docs/09 §Receipt images) costs ONE batched
`SELECT transaction_id FROM transaction_receipts WHERE user_id = <owner> AND
transaction_id IN (page)` per request — constant regardless of `limit`, so it
is the *constant term* that moved, not a per-row lookup. The `user_id`
predicate is the M93 tripwire's R3 (every statement touching a tenant table
carries an ownership predicate), and it binds the OWNER on both surfaces
`_transaction_page` serves — the owner's list and the partner projection. The
paired assertion is invariant 1 below,
which still passes: a genuine N+1 would show up there first, and that is what
this budget is for. The M60 before/after tables further down record M60's own
measurement and are deliberately left at 4.

**M107 adds ONE statement, and only to a page that actually contains split
children** (docs/44 §6.4). `split_parents` is fetched by a single
`LEFT JOIN … GROUP BY` that folds `child_count` into the same statement, keyed
by the page's distinct `parent_split_id`s, and it is taken **only when that
list is non-empty** — so a ledger without splits pays nothing for the feature.
The real ceiling for rows 4 and 5 is therefore:

- **5 statements** — a page with no split children (the enforced `q` in the
  budget table above; the M10 demo dataset the budgets are measured on has no
  splits);
- **6 statements** — a page whose rows include at least one split child
  (the same 5 + the one conditional `split_parents` fetch).

The `BUDGETS` entries stay at **5** because that is what the measurement
dataset produces, and the with-splits case is pinned by its own guard:
`tests/test_split_transactions.py::test_perf_list_query_count_unchanged_with_splits`
requires the page-with-children count to be exactly budget+1 with no statement
repeating per row (invariant 3 does the repeating part). Neither case is a
per-row query, so invariant 1 below is unaffected: whichever of the two counts
a dataset produces is the same at `limit=1`, `limit=50` and `limit=200`.

Every budgeted endpoint returned `200` for all samples.

**Provenance of both columns.** The `before` columns were measured against the
pre-fix tree `d7d2ad2` and the `after` columns in the fixes table below against
the fixed tree, **back to back in the same load window**: `PERF_SAMPLES=200`,
same harness, same fixture, same database name, same host — so the two columns
are directly comparable rather than two samples of a noisy machine. Both runs
route the app→Postgres hop over the SSH tunnel to example.internal.

## Scale-independence invariants (the actual N+1 guards)

Asserted in addition to the absolute budgets. These fail on an N+1 even if the
absolute budget is later raised:

1. **Page size:** `q(GET /transactions?limit=1) == q(…?limit=50) == q(…?limit=200)`
   — measured before: 4 / 53 / 203; after M60: 4 / 4 / 4; after M108 P4:
   5 / 5 / 5 (the batched receipt-flag statement is flat by construction).
   After M107 the asserted sequence is unchanged — the guard runs on the M10
   dataset, which has no splits — while a page that DOES contain split children
   costs exactly one statement more (the conditional `split_parents` fetch in
   the M107 note above), which is a constant term, never a per-row one.
2. **Bucket count:** `q(GET /reports/net-worth?granularity=day) ==
   q(…?granularity=month)` over the same 12-month window — measured before:
   368 / 18; after: 7 / 7.
3. **Statement repetition (the generic N+1 net):** no single normalized
   statement may run more than `MAX_SAME_STATEMENT = 3` times inside one
   request, on any budgeted endpoint. This is independent of the absolute
   budget and catches classes 1–2 above plus the ACB fx-per-trade case (25
   identical `SELECT fx_rates …` statements before M60), even if someone later
   raises a budget. Measured after M60: **max repeat = 1 on every budgeted
   endpoint.**

## Enforcement (`backend/tests/test_perf_budgets.py`)

- Module-scoped `_perf_database` fixture: drop/create `<suite>_perf`, alembic
  `upgrade head`, seed, teardown-restore (see "Measurement dataset"). Two
  invariants added after a suite run crossing local midnight failed (t_435c2d14):
  everything from the `DATABASE_URL` swap onward sits in `try/finally`, so a
  setup error still restores `DATABASE_URL`, the settings cache and `deps._engine`
  (before, the restore sat after an unreachable `yield` and every later test in
  the session ran against the perf DB); and every date — report window, `year=`
  budget, fx-seed end — comes from ONE `date.today()` read inside the fixture,
  never from import time. `tests/test_perf_harness.py` pins both, and the
  session-wide `_guard_database_url` fixture in `conftest.py` fails loudly if a
  swap ever leaks again.
- `perf_client` fixture: `TestClient(app)` with **no** `get_db` override, demo
  bearer token from `app.core.security.create_access_token(user.id)`.
- `query_counter` fixture: attaches/removes the two engine listeners around a
  single request.
- Tests (all run in the default `uv run pytest -q` / CI suite), 19 in total:
  - `test_query_budget[<endpoint>]` (×8) — measured `q ≤ budget` and
    `queries_stable` across all samples; prints one markdown table row. Asserts
    p95 **only** when `PERF_LATENCY_GATE=1`, otherwise reports it.
  - `test_no_statement_repeats_beyond_cap[<endpoint>]` (×8) — invariant 3.
  - `test_transactions_query_count_is_page_size_independent` — invariant 1.
  - `test_net_worth_query_count_is_bucket_count_independent` — invariant 2.
  - `test_budget_table_matches_spec` — asserts a row of this doc's budget table
    carries each enforced `BUDGETS` entry's path, `q` and `p95`, so editing one
    without the other fails CI.
- At module teardown the harness prints the full markdown table and, when
  `PERF_REPORT=/path/report.json` is set, writes the machine-readable version
  (per-endpoint query count, stability flag, max statement repeat, p50/p95/max
  ms, sample count, per-statement counts per request, slow-query threshold).
- `RATE_LIMIT_ENABLED=false` (set by `conftest.py`) keeps the auth limiter off
  for the repeated requests; the perf module never re-enables it.

## Slow-query log threshold

**Threshold: 20 ms per statement** (`PERF_SLOW_QUERY_MS`, default `20`).

- Rationale: the fastest interesting statements in the baseline measured 1.2 ms
  and the slowest single statement (the per-row `tags` join) 6.7 ms p50 / 56 ms
  worst case, all over the tunnel — 20 ms is ~3× the observed steady-state
  worst and therefore a real outlier, not noise.
- Where it is implemented: in the measurement harness only. Every statement
  whose `after_cursor_execute` duration is ≥ threshold is collected and
  printed in the "slow statements" section of the harness output, keyed by
  normalized SQL with its worst duration and occurrence count.
- **No production logger exists.** If one is wanted, the hook point is
  `backend/app/api/deps.py::get_engine` (a `before_cursor_execute` /
  `after_cursor_execute` pair on the singleton engine, threshold from a new
  `Settings` field), which would also require a `.env.example` line per
  `app/core/config.py`'s header rule. That is deliberately **not** part of M60:
  it changes runtime behaviour and the config surface, and no measurement in
  this audit requires it.

## Audit — every feasible read endpoint (measured 2026-09-28)

`q` = SQL statements per request, `PERF_SAMPLES=5`, demo tenant, all `200`
unless noted. This table is the audit deliverable: a point-in-time measurement
of the **pre-fix tree (`d7d2ad2`)**, not a budget — the three rows M60 fixed
(`net-worth` month/day, `transactions?limit=50/200`,
`investment-performance`, `capital-gains`) now measure 7 / 7 / 4 / 4 / 7 / 3;
everything else in the table is unchanged and carries the waiver below where it
is over budget.

| endpoint | q | | endpoint | q |
| --- | --- | --- | --- | --- |
| `GET /reports/net-worth?granularity=day` | **368** | | `GET /reports/dividend-calendar` | 3 |
| `GET /transactions?limit=200` | **203** | | `GET /reports/networth-snapshot` | 3 |
| `GET /transactions?limit=50` | **53** | | `GET /reports/spending` | 3 |
| `GET /reports/investment-performance` | **31** | | `GET /reports/spending/categories` | 3 |
| `GET /export/full` | **30** | | `GET /securities` | 3 |
| `GET /notifications/inbox?limit=50` | **29** | | `GET /securities/{id}/holdings` | 3 |
| `GET /reports/capital-gains` | **27** | | `GET /suggestions` | 3 |
| `GET /fx/board` | **19** | | `GET /tags` | 3 |
| `GET /reports/net-worth?granularity=month` | **18** | | `GET /trades?limit=50` | 3 |
| `GET /accounts` | **19** | | `GET /trades/dividend-suggestions` | 4* |
| `GET /portfolio/breakdown` | **17** | | `GET /reports/cashflow` | 4 |
| `GET /taxes/alerts` | **15** | | `GET /reports/dividends` | 4 |
| `GET /portfolio/exposure` | **14** | | `GET /reports/spending-comparison` | 4 |
| `GET /portfolio/positions` | **14** | | `GET /transactions?limit=1` | 4 |
| `GET /projections/defaults` | **14** | | `GET /goals` | 4 |
| `GET /reports/insights` | **11** | | `GET /budgets` | 9 |
| `GET /budgets/alerts` | **9** | | `GET /rules` | 5 |
| `GET /search` | **5** | | `GET /transactions?limit=50&offset=50` | 53 |
| `GET /acb` | 3 | | `GET /accounts/ledger` | 3 |
| `GET /categories` | 3 | | `GET /imports` | 3 |
| `GET /institutions` | 3 | | `GET /notifications` | 3 |
| `GET /prices` | 3 | | `GET /reports/spending/categories` | 3 |
| `GET /debts` | 2 | | `GET /debts/projection` | 2 |
| `GET /taxes/projection` | 2 | | `GET /transactions/transfer-suggestions` | 2 |
| `GET /watchlist` | 2 | | `GET /users/me` | 1 |
| `GET /vault/envelopes?device_id=probe` | 422 `string_too_short` (not measured: `device_id` needs ≥32 chars) | | | |

\* The 2026-09-28 baseline recorded 4 statements and p50 1,394 ms, but its
CPU-bound attribution was incorrect: the 2026-10-12 measured provider-disabled
comparison found 7.7 ms, proving provider wait dominated the 1.3–1.6 s response.
See the dated correction below; historical figures remain intact.

## Waivers (over a sane budget, deliberately not fixed in M60)

Each entry is measured, named, and owned by a follow-up card rather than
silently absorbed:

| endpoint | q | why not fixed in M60 |
| --- | --- | --- |
| `GET /accounts` | 19 | 7 per-security `prices` + 4 `fx_rates` lookups come from `app/services/valuation.py::compute_positions`, a shared seam under four other endpoints. Fixing it is a valuation-layer change, not one of the seven budgeted surfaces. M145 (2026-10) added 2 — one transactions + one trades statement for the whole page's investment cash legs, constant per page, never one per account (measured 19 on the demo-shaped dataset; 17 pre-M145). |
| `GET /portfolio/positions` (measured 2026-10-12) | 50 | `docs/34`'s original 14 was an earlier snapshot; updated in the M184 dated correction below. |
| `GET /notifications/inbox` | 29 | three-source union (M51, docs/35) that re-derives budget/bill/tax-loss sources per request; a fix belongs with docs/35's owner. |
| `GET /export/full` | 30 | one statement per exported table — inherent to a full dump, not an N+1. |
| `GET /fx/board` | 19 | per-pair/date lookups in `app/api/fx.py`; not one of the budgeted surfaces. |
| `GET /reports/insights`, `GET /budgets`, `GET /budgets/alerts` | 9–11 | 2 aggregate statements per budget (scales with budget count, not row count); unfixed, tracked. |
| `GET /trades/dividend-suggestions` (measured 2026-10-12) | 4 (p50 1,394 ms historical) | Provider wait, not CPU; updated in the M184 dated correction below. |

**M184 correction (2026-10-12 audit and 2026-10-13 reproduced baseline).** The
older audit note that positions used 14–17 SQL statements was not current:
read-only instrumentation against served build `fb40fd9` measured positions at
50 statements, maximum repeat 15, p50 1,253 ms in-process; the earlier 14 was a
stale snapshot. Dividend suggestions were 4 statements, p50 1,303 ms; disabling
provider fetch reduced p50 from 1,310 ms to 7.7 ms. This establishes that provider
wait, not CPU, dominated that historical build.

The reproducible 2026-10-13 baseline on commit `804270098a67fd97fcd55b98ad5ab3309662c223`
used runner SHA-256 `f599fd1dfd3fad875fb206c471b965b1b49550090e60517729c459bab11c2cd8`
and workload SHA-256 `5309c931766fde932ffafaab07833f46cfd3156de0b57c600867f03ce52fbd83`.
It used an isolated disposable PostgreSQL database, M10 seed + deterministic FX,
synthetic tenants, fixed in-memory provider data, 50 standard samples, 20 controlled
provider-delay samples and 10 samples per scale case; all responses were HTTP 200
with stable payloads. No live provider calls were made.

| workload | n | q | max repeat | p50 ms | p95 ms | DB p50 ms | non-DB p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dividend suggestions, fixed provider | 50 | 4 | 1 | 17.025 | 67.851 | 5.559 | 10.665 |
| dividend suggestions, 20 ms/symbol controlled wait | 20 | 4 | 1 | 147.829 | 193.267 | 8.750 | 138.790 |
| dividend timeline, 1k trades | 10 | 4 | 1 | 31.686 | 77.225 | 10.435 | 21.611 |
| dividend timeline, 10k trades | 10 | 4 | 1 | 136.313 | 303.904 | 24.230 | 109.940 |
| positions, standard cache-only | 50 | 17 | 6 | 135.583 | 411.432 | 47.616 | 77.339 |
| positions, 1 held security | 10 | 9 | 1 | 43.060 | 98.186 | 15.415 | 26.395 |
| positions, 10 held securities | 10 | 18 | 10 | 71.384 | 252.219 | 31.104 | 37.357 |
| positions, 50 held securities | 10 | 58 | 50 | 208.860 | 693.778 | 132.078 | 76.782 |

The controlled delay adds **130.804 ms p50** over the fixed-provider baseline,
independently attributing the former foreground wait. Positions query/repeat counts
rise with held securities (9/1 at 1 → 18/10 at 10 → 58/50 at 50). This baseline
remains intact; the measured M184 after-run follows below. The baseline artifacts
are in `/home/hermes/.hermes/profiles/tester/cache/scratch/t_bc46cefa_perf/`
(`BASELINE.md`, `baseline_evidence.json`, `run.stdout`, `run_baseline.py`).

**Reproduced current-origin baseline (2026-10-13, commit `6c7b5ea0de413c9623bc7db11d777df15a833156`; PostgreSQL 16.15).** The archived source tree was run in a disposable isolated database using runner SHA-256 `e73bcbe08b3b1fcfecebdf76a08f5e20dc76436d96cab179aac7ddfdd5100683` and workload SHA-256 `d9b4714681ce6011055c145dccdb956ffbf8c255336093a245acde3307a3f7c6`. This re-run uses the same M10 + deterministic FX fixture, synthetic tenants, fixed in-memory dividends, 50 standard samples, 20 controlled-delay samples, and 10 scale samples. Every response was HTTP 200 with stable payload hash and size; no live provider calls were made. CPU is process CPU measured separately from SQL cursor time; non-DB wall time is separately reported and is not a live-provider estimate. The earlier `8042700` table above remains as historical evidence.

| workload | n | status | q | max repeat | p50 ms | p95 ms | DB p50 ms | CPU p50 ms | non-DB p50 ms | body bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dividend suggestions, fixed provider | 50 | 200 | 4 | 1 | 12.156 | 16.217 | 3.728 | 8.035 | 8.493 | 8176 |
| dividend suggestions, 20 ms/symbol provider delay | 20 | 200 | 4 | 1 | 133.114 | 137.962 | 3.578 | 8.140 | 129.389 | 8176 |
| dividend timeline, 1k trades | 10 | 200 | 4 | 1 | 18.834 | 34.624 | 6.425 | 12.176 | 12.517 | 1197 |
| dividend timeline, 10k trades | 10 | 200 | 4 | 1 | 65.075 | 206.736 | 16.952 | 47.440 | 47.524 | 1222 |
| positions, standard cache-only | 50 | 200 | 17 | 6 | 68.802 | 206.571 | 24.926 | 45.747 | 44.107 | 7623 |
| positions, 1 held security | 10 | 200 | 9 | 1 | 26.639 | 37.164 | 10.277 | 15.619 | 15.650 | 825 |
| positions, 10 held securities | 10 | 200 | 18 | 10 | 45.683 | 61.165 | 22.838 | 24.975 | 23.291 | 6104 |
| positions, 50 held securities | 10 | 200 | 58 | 50 | 120.409 | 221.808 | 59.931 | 61.985 | 54.661 | 29545 |

The reproduced controlled delay adds **120.958 ms p50** over the fixed-provider baseline, distinguishing provider wait from local CPU. Positions repeat/statement counts rise with held securities (9/1 → 18/10 → 58/50). Baseline payload sizes and hashes are in `/home/hermes/.hermes/profiles/soft-dev/cache/scratch/t_2f36d62e_perf/baseline_run/baseline_evidence.json`.

**Paired after-run (2026-10-13, WIP tree rooted at `6c7b5ea0de413c9623bc7db11d777df15a833156`; PostgreSQL 16.15).** Same disposable PostgreSQL/M10/FX workload, with fixed raw dividend history persisted for demo and synthetic timeline securities; 50 standard samples, 20 delayed-provider-guard samples, and 10 per scale case. Every response was HTTP 200 with stable body hash and byte size. The controlled-delay provider spy was called **zero** times across the foreground reads. `q` is SQL statements/request and max repeat is the highest identical statement frequency. Baseline and after body SHA-256 values and byte sizes match for all paired fixtures; only the additive dividend state header is new. CPU is process CPU measured separately from SQL cursor time.

| workload | n | status | q | max repeat | p50 ms | p95 ms | DB p50 ms | CPU p50 ms | non-DB p50 ms | body bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dividend suggestions, persisted fixed history | 50 | 200 | 7 | 2 | 22.159 | 28.866 | 8.697 | 12.571 | 13.389 | 8176 |
| dividend suggestions, delayed-provider guard | 20 | 200 | 7 | 2 | 21.109 | 23.824 | 8.146 | 12.057 | 12.956 | 8176 |
| dividend suggestions, 1k trades | 10 | 200 | 7 | 2 | 24.070 | 29.520 | 9.425 | 11.880 | 13.347 | 1197 |
| dividend suggestions, 10k trades | 10 | 200 | 7 | 2 | 85.804 | 338.459 | 22.502 | 52.678 | 59.008 | 1222 |
| positions, standard cache-only | 50 | 200 | 9 | 1 | 44.266 | 149.352 | 14.664 | 30.085 | 29.394 | 7623 |
| positions, 1 held security | 10 | 200 | 8 | 1 | 29.679 | 36.288 | 12.207 | 17.318 | 17.471 | 825 |
| positions, 10 held securities | 10 | 200 | 8 | 1 | 31.364 | 48.626 | 12.220 | 17.272 | 18.922 | 6104 |
| positions, 50 held securities | 10 | 200 | 8 | 1 | 40.322 | 60.118 | 14.306 | 25.467 | 26.025 | 29545 |

The positions statement count is constant at 8 across 1/10/50 held securities, max repeat 1; versus the current-origin baseline, counts fell 9/18/58 → 8/8/8 and max repeat 1/10/50 → 1/1/1. Standard positions fell 17 → 9 statements and max repeat 6 → 1. Dividend suggestions use 3 additional batched metadata/history statements versus the legacy 4-query path, while exact body size/hash remains identical; the delayed provider spy proves the foreground read does not wait. Fixed-source dividend p50 moved 12.156 → 22.159 ms as the list-compatible state adds 3 bounded queries; the controlled wait path moved 133.114 → 21.109 ms with 0 provider calls, a timing-noise difference from the fixed-source run. Standard p95s are 28.866 ms (dividend suggestions) and 149.352 ms (positions), below the proposed 300 ms target. The 10k scale-case p95 was 338.459 ms (baseline 206.736 ms); it is observational, not the standard-route target. No timeline-loop rewrite was made: this paired run does not demonstrate an algorithm change with a scaling win, so it does not justify speculative complexity. Wall-clock remains observational, not a CI latency gate.

Baseline runner SHA-256 `e73bcbe08b3b1fcfecebdf76a08f5e20dc76436d96cab179aac7ddfdd5100683`; after-run runner SHA-256 `19110fc5cf6175423b7078d84ff00d14f934c10f408370eb97e88375396023db`; workload SHA-256 `d9b4714681ce6011055c145dccdb956ffbf8c255336093a245acde3307a3f7c6`; implementation-source SHA-256 (tracked backend/frontend diff plus untracked implementation files) `d8e914f28f91a163382df5de5ef023c089ff9c1c93fd2fd8d811a0a0f2c0989c`; baseline evidence: `/home/hermes/.hermes/profiles/soft-dev/cache/scratch/t_2f36d62e_perf/baseline_run/baseline_evidence.json` and `BASELINE.md`; after evidence: `/home/hermes/.hermes/profiles/soft-dev/cache/scratch/t_2f36d62e_perf/baseline_evidence.json` and `BASELINE.md`.

## N+1 fixes in M60 (before → after, paired `PERF_SAMPLES=200`)

| endpoint | q before | **q after** | p95 before | **p95 after** | fix |
| --- | --- | --- | --- | --- | --- |
| `GET /reports/spending/categories` | 3 | **3** | 48.3 | **15.0** | none — already at budget; p95 move is load noise |
| `GET /reports/net-worth?granularity=month` | 18 | **7** | 128.4 | **96.1** | hoist (2) |
| `GET /reports/cashflow?granularity=month` | 4 | **4** | 42.0 | **38.9** | none — already at budget |
| `GET /transactions?limit=50` | 53 | **4** | 63.7 | **13.9** | batch (1) |
| `GET /transactions?limit=10` | 13 | **4** | 51.2 | **11.8** | batch (1) |
| `GET /reports/investment-performance` | 31 | **7** | 121.8 | **80.3** | preload (3) |
| `GET /reports/capital-gains` | 27 | **3** | 45.7 | **26.5** | preload (3) |
| `GET /search` | 5 | **5** | 31.0 | **11.9** | none — already at budget |
| `GET /reports/net-worth?granularity=day` (scale probe) | 368 | **7** | 552.7* | **—** | hoist (2) |
| `GET /transactions?limit=200` (scale probe) | 203 | **4** | 262.6* | **—** | batch (1) |

\* scale-probe latency from the pre-fix audit run (`PERF_SAMPLES=5`); the
paired 200-sample run did not time them.

The three changes, and nothing else:

1. **Batch the per-row tag lookup.** `backend/app/api/transactions.py::
   list_transactions` issued `SELECT tags … JOIN transaction_tags … WHERE
   transaction_id = ?` once per row (50×/200×). One `IN` over the page's ids
   now, grouped back per transaction; a globally `Tag.name`-ordered result
   grouped per transaction preserves the exact per-row ordering the old query
   produced.
2. **Hoist the per-bucket securities read.**
   `backend/app/services/reports.py::net_worth_report` ran
   `SELECT securities WHERE id IN (…)` inside the bucket loop. Positions only
   ever gain keys from `trades`, so the union of trade security ids is a
   superset of every per-bucket key set and one read before the loop is
   equivalent.
3. **Preload exact-date fx for the whole replay.**
   `backend/app/tax/acb.py` gained `FxRateBook`, built once per
   `compute_user_tax_state` call from the trade-date span, and `_replay` takes
   it as a parameter instead of calling `fx_rate_cad` per non-CAD trade (25
   statements per computation, repeated for every refinement pass). A missing
   date raises the identical `422 fx_rate_missing` envelope through the shared
   `_fx_rate_missing` helper, and `fx_rate_cad` itself is unchanged for its
   other caller (`app/services/valuation.py`).

No other code was changed. Nothing was optimised that the measurement did not
prove slow.

## Scroll-frame budget (M101) — the reference table for CSS/scroll work

The API budgets above say nothing about the **client's** scroll path. M101
(Mark's *"really slow to scroll"* on Android Firefox) added a second, equally
measured budget: a scripted-scroll frame budget, so a later wave has numbers to
compare against instead of a feeling.

**Harness:** `frontend/scripts/perf/scrollbench.py` (deps: `websocket-client`,
`selenium`, system `chromium` + `firefox`, `Xvfb :99` for Firefox — Firefox's
toolkit clamps a window below 500 CSS px, so the mobile runs use 500×915 @
DPR 2.6 via `layout.css.devPixelsPerPx`; Chromium is driven over CDP with
`Emulation.setDeviceMetricsOverride` at 412×915 @ DPR 2.6 and 6× CPU
throttling). Serve a production build with
`frontend/scripts/local/serve_proxy.py --dist dist --port 4173` (proxies `/api`
to prod — the demo tenant, `POST /auth/demo`), then:

```bash
Xvfb :99 -screen 0 1400x3000x24 &
DISPLAY=:99 python3 frontend/scripts/perf/scrollbench.py \
  --browser firefox --variant base --variants base,m_media \
  --page /dashboard --runs 3 --out /tmp/m101.json
```

**Method (identical across every run):** one scripted sweep of the whole
document top → bottom → top at a **fixed 1400 px/s**, driven by the page's own
`requestAnimationFrame`, recording every frame delta plus `longtask` entries.
Budget metric: **long frames = rAF deltas > 50 ms** (a 60 Hz frame that lost ≥3
vsyncs) and the fps over the sweep. `--trace` (on by default) additionally
captures a Chromium trace, whose `Display::DrawAndSwap` / `SoftwareRenderer`
totals are the compositor-side cross-check.

**Baseline (2026-10-05, pre-M101) → shipped (post-M101), median of 3 runs,
Firefox 153.0esr, software raster (host has no GPU — deltas are the transferable
number, absolute fps is not):**

| page | viewport | long frames before | fps before | long frames after | fps after |
| --- | --- | --- | --- | --- | --- |
| `/transactions` | 412–500 @2.6 | **26** | 43.4 | **0** | 59.1 |
| `/dashboard` | 412–500 @2.6 | **27** | 25.1 | **0** | 59.1 |
| `/investments` | 412–500 @2.6 | **35** | 22.4 | **0** | 59.0 |
| `/dashboard` (desktop, unchanged) | 1440×900 @1 | 9 | 47.8 | 9 | 47.8 |

**Budget:** at `≤767.98px`, a scripted sweep of any of the three pages above
must stay **≤ 2 long frames** (the observed post-fix number is 0–1; ≤2 leaves
room for background noise on a shared box). The desktop row is a *recorded*
number, not a budget — desktop keeps its glass (docs/09 §Scroll performance)
and re-baselining it is a deliberate decision, not a side effect.

**Cross-engine:** Chromium 154, same viewport, 6× throttle, `/dashboard`:
0 long frames either way (its compositor hides the cost), but compositor draw
wall-time for one sweep fell **4025 → 274 ms (−93%)** — the same change, seen
as time instead of lost frames.

**Not enforced by CI.** Like the latency column above, this is a measured
reference: running it needs a real browser, a built bundle and ~2 minutes.
The thing that *is* enforced is the shape of the fix —
`frontend/tests/m101ScrollPerf.test.ts` fails CI if the kill rule loses its
`!important`, gets wrapped in a `@layer`, escapes the `max-width` scope, or
`.glass` loses its `isolation: isolate`.

## Touch points

- `docs/34-api-performance-budgets.md` — this spec (new).
- `backend/tests/test_perf_budgets.py` — fixtures, `BUDGETS`, guards (new).
- `backend/app/api/transactions.py` — `list_transactions` tag batching.
- `backend/app/services/reports.py` — `net_worth_report` listings hoist.
- `backend/app/tax/acb.py` — `FxRateBook` + `_replay` lookup parameter.
- `docs/08-api-spec.md` — performance-budget pointer on the transactions and
  reports rows.
- `docs/12-reporting-graphs.md` — budget pointer in Principles.
- `backend/app/api/transactions.py` — `_split_parent_rows` (the one conditional
  `split_parents` statement) + `backend/tests/test_split_transactions.py::
  test_perf_list_query_count_unchanged_with_splits` (M107: budget+1, no N+1).
- `docs/10-roadmap.md` — M60 milestone entry.
- **Untouched by design:** `app/core/crypto.py` / E2EE (M53), notification
  delivery (M59), `app/api/auth.py`, `app/services/demo_seed.py`.

## Verification

1. **Spec first (§7/§9):** this file exists and is complete — exact paths and
   query strings, the budget table, the measurement dataset and method, the
   slow-query threshold, every error code touched (`fx_rate_missing` behaviour
   preserved, `string_too_short` noted as unmeasured), waivers, and a
   Verification section — before any code change.
2. **Guards green in CI:**
   `cd backend && uv run pytest -q tests/test_perf_budgets.py` → all guard
   tests pass with the budgets above, `queries_stable` true for every endpoint.
3. **Scale-independence:** `limit=1/50/200` and `granularity=day/month`
   produce identical query counts.
4. **Latency table:** run with `PERF_SAMPLES=200` and publish the before →
   after p95 table in the completion report (before values are pinned in the
   budget table above).
5. **Full §4 gates:** `ruff format --check`, `ruff check`, `uv run pytest -q`
   (whole suite, zero deselections), `tsc --noEmit`, `eslint --max-warnings 0`,
   `npm run build`.
6. **Regression proof for the fixes:** each of the three fixes must fail its
   guard when reverted (the guard is what makes the fix permanent) — asserted
   by running the guard test against the pre-fix query counts recorded above.
7. **No false claims:** grep the doc for endpoints it asserts and confirm each
   exists (`app.openapi()`); row 9 is explicitly marked unimplemented.
