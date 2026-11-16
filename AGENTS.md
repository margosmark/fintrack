# AGENTS.md — working agreement for coding agents on fin_tracker

You are extending a **docs-first blueprint that is fully implemented** (milestones M1–M23, see `docs/10-roadmap.md`). The docs in `docs/` are the specification. This file tells you how to work here without breaking it.

## 0. Precedence

1. Numbered docs in `docs/` (the spec) → 2. `docs/13-agent-execution.md` (process playbook) → 3. this file → 4. nothing else. When sources conflict, the more specific spec doc wins; fix the loser in the same change.

## 1. Read before you write

- Any change: `docs/01` (locked stack, layout, layering rules), `docs/02` (every table, enum, convention, invariant).
- Feature work: the owning doc — ingestion `03`, categorization `04`, investments `05`, tax `06` (safety-critical: implement the math EXACTLY), fx/market-data `07`, API shapes `08`, UI `09`, roadmap/DoD `10`, privacy `11`, reports `12`, accounts `14`.
- Never redesign what a doc already specifies. The blueprint invents nothing at execution time: filenames, error codes, formulas, and response shapes are pinned.

## 2. Non-negotiable code invariants

- **Money/quantities/rates are `Decimal` end-to-end.** Floats never touch them. Decimals serialize as strings.
- **Error envelope everywhere**: `{detail, code}` via `app.core.errors.api_error` — never a bare 500 for a domain failure.
- **`acb_pools` single write path**: only through `app/tax/acb.py::rebuild_user_pools`, inside the caller's transaction. Trade create/update/delete call it before commit.
- **Single source of gain math**: `app/tax/reports.py`. Reports reuse it; never duplicate.
- **External network only in `app/services/fx.py` and `app/services/market_data.py`** (privacy lockdown wraps these seams). Ingestion and report endpoints never fetch — cache reads only.
- **Manual data wins**: manual prices over yfinance backfill; manual fx fills gaps only, never overwrites BoC rows.
- **Research caches are manual-wins and source-isolated**: `fundamentals`/`etf_holdings` manual rows are permanent; yfinance refresh/import replaces ONLY `source="yfinance"` rows. The look-through breakdown (`services/exposure_breakdown.py`) is pure cache-only aggregation — never a network call.
- **Tenancy**: `user_id` on every domain table and every query, except the shared `fx_rates` cache.
- **Staging**: reads filter `import_batches.status='committed'`; dedup UNIQUE spans pending+committed.
- **Enums**: StrEnum via `sa.Enum(native_enum=False)` — widening = constraint rebuild migration, no table rewrite.
- **Taxability is derived server-side** (`type == brokerage_non_registered`); clients cannot set it.

## 3. Process that works here

- **Waves & slices**: decompose into slices with DISJOINT file ownership. Write a binding contract (per-slice ownership table, forbidden shared files, pinned behaviors, exact payload shapes) and give every slice the same contract. Slices validate only their own files.
- **Shared seams are pre-staged by the orchestrator**, never edited concurrently: stub routers mounted in `main.py`, new deps locked in `pyproject.toml`/`uv.lock`/`package-lock.json` upfront, per-slice test databases via `DATABASE_URL`-derived names (conftest derives `<db>_test_<session-id>`; see §5).
- **One integration owner** runs ALL project-wide gates after slices land and fixes fallout (expect real defects: fixture wiring, missing `from_attributes`, wrong `select` imports, missing runtime deps).
- **Deviations are reported, not hidden.** If a spec is ambiguous or a slice needs a shared file: stop and surface it with both readings.
- **Arbitration principle**: strict validation beats silent tolerance for financial data (e.g., undefined corporate-action forms are rejected 422, never skipped).

## 4. Gates (definition of done for every change)

**Full sequence before every push:**

0. **Doc reconciliation** — the spec doc(s) for every changed path are updated (or a new `docs/NN` created where none exists), matching the implemented contracts. Runs before lint/test/build.

1. **Lint** — backend and frontend must pass with zero warnings:
   ```bash
   cd backend  && uv run ruff format --check && uv run ruff check
   cd frontend && npx tsc --noEmit && npx eslint . --max-warnings 0
   ```
2. **Test** — backend tests against real Postgres:
   ```bash
   cd backend && uv run pytest -q
   ```
   The suite has no known failures: a full run ends **0 failed**. A red you
   cannot attribute to your own change is suite-order pollution until proven
   otherwise — conftest's guards (`_guard_database_url`,
   `_guard_committed_demo_tenant`) fail the test that COMMITTED rows into the
   session database, so look there before you go hunting in your diff. Note
   that the run is safe to start while another agent is running one (per-pid
   suite databases, §5), but a wall-clock latency pin cannot be measured that
   way — any test that asserts a millisecond **budget** (it measures how fast
   the box answers a workload) must put the stopwatch assertions behind
   `tests/conftest.py::latency_budget_blocker` (it skips them, with the
   measured numbers, when `load1 >= cores`) and keep its plan / query-count /
   functional assertions enforced unconditionally; see
   `tests/test_bills_match.py::test_detail_first_paint_is_under_the_1_5s_budget_at_10k_rows`
   and the six gated pins listed in docs/34 "what is NOT enforced". The
   exception is a **ratio** pin, which asserts a loop was not frozen against a
   stall the test injects itself (the M148/M149 `LOOP_BUDGET_S` pins): both
   sides slow down together, so it survives load and must stay unconditional —
   gating it would skip the freeze detector exactly when the box is busiest.
   docs/34 names which is which; when in doubt, gate the budget.
3. **Build** — frontend production build:
   ```bash
   cd frontend && npm run build
   ```
4. **Rebuild & restart docker** — required after ANY backend or frontend change so the running stack reflects the new code:
   ```bash
   docker compose up -d --build
   ```
   For backend-only changes: `docker compose up -d --build api`
   For frontend-only changes: `docker compose up -d --build web`
5. **Smoke test** — exercise the changed path through the running stack via `http://localhost:8080` (nginx proxy, NOT just TestClient). UI changes get browser-level verification (headless Chrome; screenshots).
6. **Commit & push** — secret-scan first (`git ls-files | grep -iE '\.env$|secret|token|pem'` must be empty), then commit per integrated wave with a descriptive message, push to `origin/main`.

**Never skip the docker rebuild.** Lint + tests passing locally does not mean the running stack works — always rebuild, restart, and smoke before pushing.

## 5. Environment realities

- **Postgres 16 required** (compose `db`); tests NEVER run on SQLite. Tests need `DATABASE_URL` pointing at an isolated DB — conftest drops/creates `<db>_test_<session-id>` and runs alembic.
- **Suite runs never share a database, so run them concurrently.** One Postgres serves every agent, and a fixed `<db>_test` name meant the second `uv run pytest` dropped the first one's database mid-run (`AdminShutdown`, re-run migrations underneath it). The suite database is now named per session — `<db>_test_p<pid>` by default: unique across live runs, so no coordination is needed and two worktrees can run the suite at the same time, and the NEXT run reaps it (plus its `_demo` / `_perf`, which inherit the name) once its pid is dead. `TEST_DB_ID=<id>` instead pins an exact, findable name (`<db>_test_<id>`) that deliberately outlives the run — nobody can prove a dead pin from a name alone, so a pinned name is never auto-reaped; pin the same id again to reclaim it. A session pinned at a database somebody else is already using fails at setup, naming it, instead of stealing it. An id longer than 52 characters does not fit (the id is the part that must survive; the base is what shortens) and is REFUSED at setup rather than truncated — truncating would map two different pins onto one database.
- **Every `DROP DATABASE … WITH (FORCE)` in the tests goes through `tests/conftest.py::drop_database_force`.** Postgres validates every attached backend BEFORE signalling any (`storage/ipc/procarray.c::TerminateOtherDBBackends`), so one backend this role may not signal — a background worker whose role is not set yet (`acl.c::has_privs_of_role(me, InvalidOid)` is false) or another role's session — fails the whole statement with `InsufficientPrivilege: permission denied to terminate process` and drops NOTHING. Autovacuum attaches to a freshly seeded database at random, which is what failed 1 of 3 concurrent suite pairs on its own `<suite>_perf` (t_00b6d06d). The helper waits for the database to become droppable (bounded deadline, backing off) and, if it never does, raises naming the attached pids instead of hanging; `tests/test_suite_db_isolation.py` pins that against a real un-signalable backend (`sudo -n -u postgres psql`, skipped where that is unavailable). `GRANT pg_signal_backend TO <suite-role>` on the host removes the condition entirely.
- **A test that made an un-signalable backend must DRAIN before it drops — not kill once off a snapshot.** `pg_stat_activity` is a snapshot: a `psql` client that had not finished connecting is not in it, so the kill loop passes it by, it attaches a moment later, and `DROP … WITH (FORCE)` then spends its whole deadline on a backend this role may not signal (red three times on this box — once in a solo run and twice in five runs while two of them executed concurrently — the same shape as the full-suite red this documents, which appeared only while two suites shared the box). The drop waits for BOTH facts that make it safe: nothing attached (re-checked every pass, so a late attacher is caught) AND every client process exited — only an exited client can never attach again. `_drain_then_drop` in that file is the shape, and every `finally` in it that spawned a client now uses it.
- **Test isolation rules**: demo lifespan seeding is disabled in conftest (`SEED_DEMO_ACCOUNT=false` env — survives settings cache clears; `tests/test_demo_seed.py` opts back in with a hermetic own-DB fixture). Opting back in REQUIRES that hermetic fixture (drop/create `<suite>_demo`, alembic, restore in try/finally, module listed in conftest's `_SWAP_MODULES`): a `TestClient`'s startup runs `app.main.lifespan` → `demo_seed.seed_demo_from_engine()`, which opens an engine of its own and really COMMITS, so a module-wide `seed_demo_account=True` on the suite database leaves the whole demo tenant behind for every later module (`tests/test_locked_fixture_tenant.py` did exactly that — 20 failures in five modules, t_17d025b2). `_guard_database_url` cannot see that shape (it asserts the URL, not the rows already committed into the CORRECT database), so `conftest._guard_committed_demo_tenant` fails the POLLUTING test at teardown when the suite database holds a committed `demo@fintracker.local`. A NON-demo tenant leaks the same way — `tests/test_m149_offloop_routes.py` drives a LIVE server, whose `POST /auth/register` commits a registered user plus its 10 default categories where no rollback reaches, and its teardown had taken the account away but not the identity — so every read of a table with a `user_id` in a test must be scoped to that user, and `conftest._guard_no_committed_tenant_survives` fails the RUN (naming the leftover address) if a `users` row outlives the session (§6, t_a451f171). `fx_rates` is SHARED across users — suites that seed fx must stay on isolated DBs or they collide.
- **Lifespan/settings gotcha**: read settings LIVE inside `lifespan` (`get_settings()` at call time). An import-time snapshot ignores test mutations and re-seeds demo data everywhere.
- **Docker**: `api` container uses public DNS (router null-routes `fc.yahoo.com`, killing yfinance). `env_file: .env` — a missing `.env` breaks compose locally; `cp .env.example .env`.
- **Headless Chrome**: new headless mode can stop servicing rAF on this host, freezing `AnimatePresence mode="wait"` swaps — UI verification uses `--headless=old` (verify rAF is alive before blaming the app).
- **Runtime deps that must never be dropped**: `uvicorn` (compose CMD), `python-multipart` (multipart routes), `httpx` (LLM adapters).

## 6. Code traps (each bit us once)

- Use `sqlmodel.select`, NOT `sqlalchemy.select`, with SQLModel `Session.exec()` — the latter returns raw Rows without attribute access.
- Pydantic response models need `model_config = ConfigDict(from_attributes=True)` to validate ORM objects.
- `@pytest.fixture(name="x")` registers as `x` — depend on the registered name, not the function name.
- `POST /prices` is an upsert (manual wins); `POST /accounts` derives `taxable`; corp-actions accept ONLY docs/06 forms (negative quantity → 422 `invalid_corporate_action`).
- Signed-quantity convention: buy/drip positive, sell negative, cash actions quantity=0.
- Alembic: inspect live constraint names before dropping (`pg_constraint`); revisions named `NNNN_<slug>`.
- yfinance field units vary by version/field (dividend yield arrives as a fraction in some versions, a percent in others) — verify each mapping against ONE live call and pin the normalization in code.
- Numeric scale must fit the domain max: `etf_holdings.weight_pct` is `Numeric(7,4)` because exactly 100.0000 does not fit `(6,4)`.
- Frontend: consume only semantic tokens (`bg-surface`, `text-muted`…) — no literal dark utilities; both themes must pass the documented contrast pairs; respect `prefers-reduced-motion`.
- Alembic revision IDs max 32 chars (Postgres `varchar(32)` in `alembic_version.version_num`). Long names like `0009_add_account_starting_balance` (33 chars) cause `StringDataRightTruncation` — shorten to `0009_add_starting_balance`.
- SSE (`EventSource`) doesn't support custom headers — auth must be via query parameter (`?token=`). nginx needs `proxy_buffering off` and `X-Accel-Buffering: no` for SSE endpoints.
- `motion.div {...fadeUp}` spreads `hidden`/`show` as invalid HTML attributes. Use `motion.div variants={fadeUp} initial="hidden" animate="show"` or wrap in a `staggerContainer`.
- Market-hours check for background refresh: skip yfinance calls when markets are closed (before 6 AM or after 8 PM ET) to avoid wasting calls on stale data. Manual refresh still works anytime.
- `EmptyState` component requires `icon` and `title` props, not `message`. Use `ErrorPanel` for error states.
- **Never assert a clock-derived date against a date literal** — `expect(request.from).toBe("2026-09-09")` is true for exactly ONE day: m126's default-range pin (`tests/m126PriceHistory.test.tsx`) went red for every agent at 00:00 on 2026-10-10 and took the web gate with it. Re-derive the expectation from the clock at assertion time (re-implement `today − 1 calendar month` in the test rather than importing the helper under test), or scope `vi.useFakeTimers()` narrowly around the one interaction (m95's pattern) — a file-wide freeze hangs `@testing-library`'s `waitFor`, which needs real timers. To sweep the whole suite for date-locks in one pass: `faketime '<future date> 09:00' env NODE_ENV=test npx vitest run`.
- **A date captured at IMPORT goes stale on a midnight-crossing suite run** — pytest imports every test module long before it runs any of them, while the product re-reads `today_et()` per request, so a module-level `_TODAY = today_et()` disagrees with the product by a day once a run crosses local midnight (budgets seeded for "this month" read zero spend). Read the clock at CALL time — `def _today(): return today_et()`, test_notifications' pattern (t_435c2d14); the budget/aggregates/notifications-delivery/demo-seed files were converted in the wall-clock sweep 2. To sweep the backend for the whole class: `faketime '<future date> 23:59' env FAKETIME_DONT_FAKE_MONOTONIC=1 .venv/bin/python -m pytest -q` — NOT `uv run`, whose own TLS to PyPI dies on cert validity at far-future clocks before pytest starts. Triage: a red that also fails SOLO at the shifted date is date-shaped; one that passes solo but fails in a midnight-crossing full session is an import-capture straddle; one that passes solo ONLY because a cold module cache takes a different path than the full suite is the third family (the privacy-lockdown dead-symbol red, next bullet). Three families are faketime ARTIFACTS, not bugs — anything comparing a DB-stamped `created_at` (server `func.now()`) to the Python clock, which one wall clock never does in reality: the notifications-inbox "today" window, the spending-alerts daily dedup, and token/invite expiry-vs-`created_at` deltas.
- **A yfinance double that ANSWERS with an empty frame books a hard "quote not found" verdict** — `Ticker.history()` returning `empty=True` is docs/07's verdict about the SYMBOL (an unlisted ticker's shape), not "no answer", and three of them (`DEAD_SYMBOL_MISSES`) set `securities.dead_symbol`, after which `ensure_recent_price` serves the cache and never reaches the seam the test meant to pin. `test_privacy_lockdown::test_valuation_stale_price_fetch_blocked_under_privacy` red on exactly ONE shape — a full suite at a clock inside 09:30–20:00 America/New_York, where the creation backfill + the positions read + `_markets_are_open()`'s background refresh book three verdicts on ONE read — and stayed green in every single-file run because a COLD `market_data._yf_session` lru_cache raises on the fake `yfinance` module BEFORE `Ticker.history` is reached: solo answered `unavailable`, the full suite (an earlier file having warmed the cache) answered `not_found`. So a double must REFUSE, not answer: raise an exception that is not `YFTickerMissingError` so `_classify_history_error` books `unavailable` (which never advances the counter), and stub `_yf_session` so the single-file run and the full suite take the same path — `test_privacy_lockdown._forbid_network` is the pattern, and the test now asserts `quote_miss_count == 0` so this drift fails on EVERY clock instead of on one.
- **A unit test that reaches a live URL runs on that server's calendar** — the probe-runner tests drive `rp.main`, whose M148 health preflight hits production /health: the deployed cert ended 2026-11-22, so every future-clock run failed all five with `refusing to run` (exit 2) — and an offline box fails them identically. Cache-behavior tests pass `--skip-health-check`; the gate keeps its coverage hermetically (`test_health_preflight_gate_is_pinned_without_the_network`).
- **A timer scheduled in product code belongs to whoever scheduled it** — cancel it on unmount (`Toast.tsx` armed a 4200 ms auto-dismiss and never cleared it). The SPA masks the leak (the provider outlives every toast); vitest does not: a test that ends before the deadline leaves it armed into the torn-down jsdom `window`, the callback throws `ReferenceError: window is not defined` from react-dom's `getCurrentEventPriority`, and the runner **exits 1 with ZERO failing tests** — a red web gate with nothing to point at. Track armed handles in a ref and sweep them in the unmount effect; `frontend/tests/toastTimerCleanup.test.tsx` is the pattern.
- **Copy that renders in TWO cards is not a single-match query** — SecurityDetail's dead-symbol verdict prints on the price card as soon as the overview lands AND again on the chart card once the price-history query leaves `isPending` (m141), so `getByText(/possibly delisted/)` races in both directions: "Found multiple elements" when the chart lands first, a silent one-match pass when it lands late. That is a load-dependent red at ANY date (1 full-suite run in 3, 0 in 25 single-file runs). Wait for the LATER, unique copy (`findByText(/no closes to chart/)`), then pin each card with `getAllByText(...)` and an exact length.
- **A test that COMMITS rows of its own must delete every one, and read only its own `user_id`** — the suite rolls each test back through the `session` fixture's outer transaction, so the only rows a later test can see are the ones somebody COMMITTED through a connection of their own: a live server's engine (`tests/test_m149_offloop_routes.py`), `Session(get_engine())` (`tests/test_m155_price_session.py`, the auth-hardening races). `POST /auth/register` creates the user's 10 `DEFAULT_CATEGORIES` in that same commit, so one tenant left behind puts a second "groceries" in front of every later test that reads `categories` with no `user_id` filter — and WHICH of two same-named rows such a read returns is heap order, i.e. noise: `test_commit_creates_accounts_categories_and_staged_rows` went red in 2 of ~8 full runs and never solo (t_a451f171). So scope every read to your own tenant, delete the whole tenant when the fixture that made it tears down (`DELETE /users/me` runs docs/20's cascade; or delete by `user_id` on a connection of your own), and expect `conftest._guard_no_committed_tenant_survives` to fail the RUN — naming the leftover address — if a `users` row outlives the session. That guard is the check for the class, not one member of it.

## 7. Adding a feature

Blueprint first: write/extend a `docs/NN` spec (house style: context → approach with exact shapes → touch points → verification), wire it into `docs/10` as a milestone, get approval, THEN implement via the section-3 process. Update every doc your change invalidates in the same commit — including creating new `docs/NN` specs per §9 when a feature lacks one.

## 8. Git

Commit per integrated wave with a descriptive message; push to `origin/main` after gates. Never commit `.env`, credentials, or seeded-with-real-data dumps.

## 9. Doc reconciliation (standing rule)

The docs/ folder is the living spec. Every code change MUST keep it reconciled, and any
implemented feature without a spec doc MUST get one.

- **Reconcile on every change.** When you edit code, update the owning spec doc(s) in the
  same commit. On conflict, the more specific spec doc wins (§0 precedence); fix the loser,
  not the code, unless the code is the intended source of truth — then update the doc.
- **Create a doc for every feature.** Any implemented behavior with no dedicated `docs/NN`
  spec gets one (new `docs/NN-<slug>.md`), regardless of whether a roadmap line exists.
  A roadmap entry is NOT a substitute for a spec.
- **Completeness bar.** A spec doc is complete only when a fresh agent could implement the
  feature from it alone. It MUST pin: exact endpoint paths/methods, request/response payload
  field names + types, every error-envelope `code`, table/column names + types + invariants,
  enums, stated behaviors, and a Verification section (concrete input → expected observable
  output, running-stack smoke where applicable). Follow the house style in §7.
- **No partial docs.** Do not leave a doc "to be updated later". Reconcile fully within the
  change, or create the missing doc fully.
