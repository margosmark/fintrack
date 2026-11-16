# 10 — Roadmap

Twenty-three milestones. Each is scoped as one subagent-sized workstream with acceptance criteria and dependencies (execution protocol for autonomous agents: docs/13). Feature coverage: bank/credit-card import (M2), transaction categorization + LLM learning loop (M3), investment tracking (M4), Canadian ACB/capital-gains tax engine (M5), currency exposure (M4/M6 dashboards), privacy export & local backup (M7), reporting & graphs (M8), account & institution breadth (M9), demo experience (M10), securities UX & watchlist (M11), search & similar examples (M12), security dashboard & search integration (M13), dividend income analytics / projections / security fundamentals (M14–M16), ETF/MF holdings composition (M17), look-through exposure breakdown (M18), recursive l…

Definition of done for EVERY milestone: `alembic upgrade head` clean on a fresh database, `ruff check` + `ruff format --check` clean, `pytest` green against real Postgres, the milestone smoke test executed for real against running services, and (from M1 onward for web changes) `tsc --noEmit` + `vite build` green, and the owning spec doc(s) reconciled with the implementation per AGENTS.md §9.

**Next wave (M48–M52): M48–M52 shipped — see the M52 section below and `../NEXT_WAVE_PLAN.md` for the wave record. M59 (notification delivery channels) shipped as `docs/37`.**

## M1 — Scaffold

Compose file, FastAPI app shell, all SQLModel models + Alembic initial migration, auth endpoints, CI (ruff + pytest). Creates the monorepo layout of docs/01.
**Deps:** none.
**Smoke test:** `docker-compose up` → `POST /auth/register` returns tokens → `alembic upgrade head` idempotent → CI green.

## M2 — Ingestion

OFX/QFX + CSV-profile importers, dedup, batch review endpoints.
**Deps:** M1.
**Acceptance:** checked-in fixtures (docs/03: one QFX, two differently-shaped CSVs) parse through the API; duplicate rows skipped with counts in the batch summary.
**Smoke test:** upload each fixture via `POST /imports/upload`, commit, assert transaction counts and zero duplicate inserts.

## M3 — Categorization

Categories CRUD, rules engine, learned map, LLM provider abstraction + suggestion queue.
**Deps:** M2.
**Acceptance:** rule beats learned map by priority; LLM output constrained to existing category ids; accept writes learned-map entry.
**Smoke test:** with Ollama running (`qwen2.5:7b-instruct`), run suggestions over an uncategorized batch, accept one, re-import same payee → auto-categorized without LLM call.

## M4 — Investments

Securities, trades, valuation, yfinance integration. Parallel-safe with M2/M3.
**Deps:** M1 only.
**Acceptance:** symbol lookup autofills name/kind/currency; positions = signed quantity sums; currency-exposure view groups by listing_currency.
**Smoke test:** record CAD buy + USD buy of one security, assert position quantities and both currency values.

## M5 — Tax engine

ACB pools, superficial-loss detection, ROC handling, gains reports.
**Deps:** M4.
**Acceptance:** computed ACB matches hand-calculated values on the trades fixture (docs/03): buys in CAD and USD, a partial sell, a superficial-loss scenario, a ROC distribution, and the 2:1 split `corporate_action_adjustment` case all correct.
**Smoke test:** load fixture trades, assert computed ACB equals hand-calculated value including the superficial-loss case, the ROC case, and the 2:1 split case; `GET /reports/capital-gains?year=` returns matching CSV.

## M6 — Frontend completion

Dashboard, remaining pages, report exports.
**Deps:** M2, M3, M4, M5.
**Acceptance:** every page of docs/09 functional against live API; dark mode default; capital-gains report exportable.
**Smoke test:** full user journey in browser — register, import fixture, accept suggestions, record trades, open Taxes page, download CSV report.

## M7 — Privacy export & local backup

Executes docs/11 (`11-privacy-export-local-backup.md`) as written: `privacy_mode` + per-user LLM settings on `users` (docs/02), `GET /export/full`, Settings UI (download, encrypted IndexedDB backup, toggles), cloud-LLM and external-market-data lockdown with the specified error codes.
**Deps:** M6 (Settings page exists).
**Acceptance:** all four items of the plan's Verification section pass; snapshot keys match the tables of docs/02 and exclude every credential field.
**Smoke test:** the plan's Verification §1–§4.

## M8 — Reporting & graphs

Aggregation endpoints (docs/12), Reports page, chart wiring.
**Smoke test:** seed fixture data, call each `/reports/*` endpoint, compare against hand-computed values, download CSV per report, open `/reports` tabs in browser and confirm each chart renders.

## M9 — Accounts & institutions

Executes docs/14 (`14-accounts-institutions.md`) as written: widen account types (lira, rdsp), Settings Accounts & Institutions manager with computed balances, Imports inline account creation, completed accounts API client.
**Deps:** M6 (Settings page), M2 (accounts routes exist since W2).
**Acceptance:** migration up/down clean with rows of every legacy type present; UI-level CRUD round-trip incl. `account_in_use` guard; net-worth sums identical when accounts share vs split institutions; Imports wizard creates an account mid-flow.
**Smoke test:** create institution + chequing + TFSA through Settings, import into the chequing, buy into the TFSA, confirm both appear under their institution groups with correct balances.

## M10 — Demo experience

Idempotent `POST /auth/demo` + lifespan seeding of `demo@fintracker.local`: 12 months of deterministic synthetic data across 4 institutions / 6 accounts / 7 securities (~360 transactions, ~75 trades, manual-source fx + prices). Login page gains an "Explore the demo" one-click button. `SEED_DEMO_ACCOUNT=false` disables both; fake market data is always labelled `manual`, never `boc_valet`/`yfinance`.
**Deps:** M9.
**Acceptance:** seeding idempotent (identical fingerprints across fresh seeds); ≥95% transactions categorized; net-worth spans 12 non-zero buckets; performance splits realized vs unrealized; kill-switch returns 404 `demo_disabled`; cold seed < 3 s.
**Smoke test:** restart the stack, click "Explore the demo" on the login page, land on a populated Dashboard, flip through Reports tabs with live charts.

## M11 — Securities UX & watchlist

Trade form: searchable existing-securities picker, accounts gated to investment-capable types (422 `account_not_investment` backend-enforced), lookup failure states plainly that the symbol won't be tracked. Security creation + watchlist-add dispatch a 365-day price backfill; `POST /portfolio/refresh` re-quotes held ∪ watched. Watchlist table (docs/02) with glass-card UI.
**Deps:** M9.
**Acceptance:** trades into chequing/savings rejected 422; picker filters and selects; lookup failure shows the persistent notice; watchlist add/dup-409/remove round-trips; refresh queues held∪watched and privacy seam blocks fetches under privacy mode.
**Smoke test:** add NVDA via lookup, watch VFV, refresh prices, attempt a trade from a chequing account and confirm the rejection.

## M12 — Search & similar examples

`pg_trgm` extension + GIN trigram index on `transactions.normalized_payee` (migration 0004). `GET /transactions/similar` returns fuzzy past categorized examples as a deterministic display aid (threshold 0.3, ranked, tenancy-scoped); `GET /search` fans one query out to transaction/category/account groups; `GET /transactions` gains a signed-amount `direction=paid|received` filter. UI: per-row similar-examples popover on Transactions, debounced global search box in the sidebar.
**Deps:** M6.
**Acceptance:** `alembic upgrade head` applies 0004 cleanly with `pg_trgm` present in `pg_extension`; `/transactions/similar` ranks matching categorized rows by score, never returns uncategorized or foreign-tenant rows, honors `exclude_id`; `/search` returns all three groups tenant-scoped with empty groups (not 404) for unknown text and a 422 envelope for missing `q`; `direction=paid|received` returns only negative/positive amounts and composes with existing filters.
**Smoke test:** register a user, create `Metro Grocery` (Groceries) + `Metro Pharmacy` (Health), then `GET /transactions/similar?q=metro%20groc` ranks Grocery first with score + category name, `GET /search?q=metro` shows transaction + category groups, `GET /transactions?direction=paid` lists only negatives, and in the browser the nav search box filters Transactions via result click while the row History popover applies a past category.

## M13 — Security dashboard & search integration

`GET /search` gains a fourth `securities` group (trigram-scored on symbol + name, top 3, tenant-scoped). New `GET /securities/{id}/overview` serves the whole per-security dashboard page in one payload: latest cached close with staleness, 365-day stats and ascending history from the `prices` cache, positions reused verbatim from `valuation.compute_positions` with summed totals, watchlist flag, last 10 trades. Frontend: `/securities/:securityId` detail page (price card, 52w strip, price-history chart, holdings table, recent trades, watchlist toggle); search dropdown gains a Securities group; Investments position/watchlist symbols link to the dashboard.
**Deps:** M11, M12.
**Acceptance:** `/search` returns all four groups tenant-scoped (securities matched by symbol AND name fragment) keeping the empty-groups-not-404 contract; overview serves correct `latest`/`high_52w`/`low_52w`/ascending `history`/hand-computed `change_pct` from seeded manual prices, aggregates positions across accounts (fully-closed accounts drop out), flips `watchlisted` on watchlist add/remove, caps `recent_trades` at 10 newest-first, and answers 404 for foreign ids; positions math stays single-sourced in `valuation.compute_positions`.
**Smoke test:** register a user, create NVDA via `POST /securities`, seed manual prices, then `GET /search?q=nvda` and `q=nvidia` both surface it, the overview endpoint returns the seeded chart/stats, and in the browser search → Securities group opens the dashboard while Investments symbols link to the same page.

## M14 — Dividend income analytics

Yearly dividend overview (docs/12 §6): `GET /reports/dividends?year=` aggregates `dividend | distribution | return_of_capital` trades per security with trade-date CAD conversion from the fx cache, 12 zero-filled month buckets, action-type totals, a trailing-365-day total and an unresolved-fx count; `&format=csv` exports flat item rows. Reports page gains a Dividends tab (year selector, totals strip, monthly bar chart, per-security table).
**Deps:** M8.
**Acceptance:** per-action aggregation matches hand-seeded trades; conversion uses the on-or-before trade-date rate; unavailable rates count into `unresolved_fx` without breaking totals; `available_years` desc; tenancy strict.
**Smoke test:** record dividend + distribution trades via the trade form, open Reports ▸ Dividends, confirm per-security CAD rows, correct totals and the year selector.

## M15 — Investment projections

Deterministic future-value scenarios (docs/12 §7): `POST /projections/run` runs 1–8 labeled scenarios (monthly compounding, growth-then-month-end contribution, optional inflation deflation) over a start value; `GET /projections/defaults` returns the current portfolio market value for prefill. Pure-math service, no DB writes. Reports page gains a Projections tab: preset chips (Conservative 4% / Balanced 6.5% / Aggressive 9%) plus custom scenarios, years slider 1–50, multi-line chart with the first scenario's real value dashed, results table.
**Deps:** M8.
**Acceptance:** hand-computed monthly-compounding cases exact (10000 @ 12%, 1yr → 11200.00); real-value deflation math exact; validation bounds (years/inflation/return/scenario count/label length) all 422-enveloped; defaults are tenancy-scoped.
**Smoke test:** run Conservative + Balanced + custom 12% from the prefilled start value; chart lines diverge monotonically and the endpoint value equals the hand-computed 2-year case.

## M16 — Security fundamentals

`fundamentals` table (migration 0005, docs/02) caching per-security research data from yfinance `Ticker.info`: EPS, P/E, market cap, dividend yield %, beta, sector/industry/country, 52-week range. `fetch_fundamentals` sits behind the privacy guard and degrades to None; `ensure_fundamentals` serves a 7-day cache with permanent manual-wins. Overview payload gains `fundamentals`; `POST /portfolio/refresh` fan-out refreshes them; `PUT /securities/{id}/fundamentals` upserts manual overrides. Dashboard card between stats and holdings with inline manual editing.
**Deps:** M13.
**Acceptance:** overview includes the block with silent degradation (privacy mode → cached/null, never an error); a second call inside 7 days does not re-fetch; manual rows survive refresh forever; PUT is tenancy-guarded (foreign id → 404).
**Smoke test:** open a held stock's dashboard (card populated where yfinance reachable, EmptyState otherwise), edit sector manually, save, reload, trigger a price refresh — the manual value persists.

## M17 — ETF/MF holdings composition

`etf_holdings` table (migration 0006, docs/02) storing per-fund top holdings with asset class / region / sector weights and a named weight CHECK. Manual-first: `PUT /securities/{id}/holdings` bulk-replaces manual rows (weight sums over 100% → 422 `holdings_weight_exceeded`, under-100 legal as uncategorized residual); `POST /securities/{id}/holdings/import` best-effort imports yfinance `funds_data` (503 `market_data_unavailable` when Yahoo has nothing, privacy 403 passthrough) replacing only yfinance rows. Overview carries the composition cache-only; the fund dashboard renders a composition card with weight bars, the unclassified remainder, import button and an inline manual editor.
**Deps:** M16.
**Acceptance:** PUT round-trip replaces only manual rows; over-weight rejected with the pinned envelope; import replaces only yfinance rows and preserves manual ones; import-unavailable → 503 envelope; privacy mode → 403 envelope passthrough; overview block null for stocks; tenancy 404 on all three routes.
**Smoke test:** on an ETF dashboard: Import from market data fills the table (or 503 toast when yfinance lacks the fund); manual Edit → save → weights persist; footer shows the unclassified remainder.

## M18 — Holdings breakdown by sector / geography / asset class

`GET /portfolio/breakdown?dimension=…` (docs/05): look-through aggregation over `compute_positions` — fund composition weights with denominator max(sum of known weights, 100): partial funds keep face weights with the residual → `Unclassified`, over-known sums renormalize; else stock fundamentals, else unclassified with asset_class falling back to the security kind. Investments page Exposure card gains Sector / Geography / Asset class tabs (donut + legend) beside the existing currency view.
**Deps:** M16, M17.
**Acceptance:** hand-computed look-through case exact (position 1000 with 60/30 weights → 600 / 300 / 100 Unclassified); stock fundamentals land in their sector/country; fund without composition → Unclassified; totals reconcile to the positions market-value sum; dimension validation 422; tenancy strict.
## M19 — Recursive look-through & company exposure

Recursive look-through (depth ≤ 3, cycle-safe) for funds-of-funds (`etf_holdings` rows resolving to other user funds with their own rows), plus a `company` dimension aggregating by underlying holding ticker/name. Leaf rows fall back to the matched security's `fundamentals` then `Unclassified`. Eager background enrichment auto-creates sub-fund securities (yfinance lookup) and fetches their composition + fundamentals on holdings import/PUT (depth-1, bounded 12, privacy-degrading).
**Deps:** M16, M17, M18.
**Acceptance:** pinned hand case exact (F=1000 holds S 60% + A 30%; S holds B 50% + C 30% → company A 300, B 300, C 180, Unclassified 220); 4-fund chain depth cap (level-4 fund is a leaf); cycle terminates; sector/geography use fundamentals fallback; enrichment creates sub-fund + fetches rows (non-funds skipped, privacy degrades, cap enforced); totals reconcile.
**Smoke test:** create XEQT.TO, buy, import from market data → sub-fund securities appear → Exposure ▸ Companies donut + legend shows underlying holdings; sector/geography show stock sectors/countries where fundamentals exist; totals reconcile to positions sum.
**Smoke test:** with one fundamentals-backed stock and one imported-composition ETF held: Exposure ▸ Sector shows the stock's sector AND the ETF's top-holding sectors with values distributed by weight; Geography / Asset class consistent; totals reconcile to `GET /portfolio/positions`.

## M20 — Account sidebar, detail page & transfer suggestions

Global left sidebar section listing all accounts grouped by type (Cash / Credit / Registered / Non-registered) with balances, each clickable to a full account detail page (`/accounts/:id`). Account detail page shows: running balance area chart (recharts, last ≤365 days from ledger), editable starting balance for reconciliation, filterable transactions table (same filter set as Transactions page — date range, payee, amount, direction), inline category dropdown per row, transfer suggestion panel. New backend: `starting_balance` column on accounts (nullable, defaults 0), `GET /accounts/ledger` endpoint with running balances, `GET /transactions/transfer-suggestions` (unpaired opposite-amount pairs ±10 days, deduplicated), `POST /transactions/accept-transfer-suggestion` (pair + assign transfers category). Account names become clickable links in Transactions and Settings pages. Styled to match Transactions page: glass filter bar, two-column layout with Account Info sidebar, sticky uppercase headers, row stagger animation.

**Deps:** M9, M12.
**Acceptance:** migration adds `starting_balance` nullable column; `cash_balance` = committed transactions + starting_balance; ledger returns transactions with running balances starting from starting_balance; transfer suggestions find unpaired opposite-amount transactions within ±10 days; accept-transfer pairs both transactions and assigns the transfers category; sidebar shows accounts grouped by type with balances; account detail page renders chart, filterable transactions, transfer suggestions; account names clickable in Transactions and Settings.
**Smoke test:** create account with `starting_balance: 1000`, verify `cash_balance` reflects it; `GET /accounts/ledger` returns running balances; create two uncommitted-category transactions on different accounts with opposite amounts within 10 days, `GET /transactions/transfer-suggestions` returns the pair, accept → both get transfers category; sidebar shows accounts; click account → detail page with chart, filtered transactions, suggestions.

## M21 — SSE cached-price streaming & market-hours optimization

Server-Sent Events (SSE) streaming for cached price updates: `GET /securities/stream/prices?security_ids=` endpoint streams the newest cached close on or before today every 5 seconds; nginx SSE support (no buffering, `X-Accel-Buffering: no`); SSE authentication via query parameter (JWT token in URL for EventSource compatibility). Frontend: `usePriceStream` hook with `EventSource` connection, `SSEStatusIndicator` component (green pulsing dot + `Stream connected` label when connected, yellow `Connecting…`, red `Connection error`, gray `Offline`; freshness remains a separate price-date/stale-age indicator), Investments page: Connected SSE stream indicator in header, positions auto-refresh via background tasks on each fetch (only when markets are open — see docs/18 for the current window; M117 replaced the 6 AM–8 PM ET fixed-offset estimate with weekdays 09:30–20:00 `America/New_York`). Security detail page: SSE connection with backfill polling (5s while `cached_days` is 0, up to 3 minutes). Manual "Refresh prices" still works anytime regardless of market hours.

**Deps:** M11, M13.
**Acceptance:** SSE endpoint streams cached price updates for requested security IDs; nginx forwards SSE without buffering; frontend connects and shows the stream-state indicator; positions auto-refresh during market hours, skip after hours; manual refresh works anytime; security detail page polls during backfill.
**Smoke test:** open Investments page, verify green `Stream connected` dot appears; open security detail, verify the cached stream message timestamp and the quote's `as of` date remain visible separately; outside weekdays 09:30–20:00 ET, verify no yfinance calls in logs; click "Refresh prices" → works regardless of time.

## M22 — Transaction CRUD

Complete tenant-scoped transaction CRUD for manual rows and committed imported rows. `POST /transactions` creates manual ledger rows with exact-date cache-only CAD conversion and canonical deduplication; `PATCH /transactions/{id}` edits ledger fields, including account moves, while recomputing derived fields; `DELETE /transactions/{id}` cleans dependent suggestions and unlinks transfer pairs atomically. Pending-review rows remain batch-managed. Transactions and account detail surfaces share an add/edit modal and confirmation-backed delete controls.

**Deps:** M6, M9.

**Acceptance:** manual create returns 201 with default account currency and exact cached CAD conversion or NULL when unresolved; category/account tenancy and duplicate conflicts use the pinned envelopes; edit recomputes dedup/CAD; pending rows reject CRUD; delete returns 204, removes suggestions, unlinks a counterpart, and leaves batch history unchanged; list shows manual and committed rows only. Both UI surfaces support required-field validation, Escape/backdrop close, add/edit/delete, persistence, pagination correction, and query invalidation.

**Smoke test:** through the running stack, register/login, add/edit/delete a transaction from Transactions and account detail (account preselected), verify native/CAD values and persistence, test required-field validation and duplicate error toast, then delete a paired transaction and confirm its counterpart is unpaired and the row disappears.

## M23 — Currency Tracker

Read-only board of CAD-per-1-unit rates for a curated set of major global
currencies with a recent-trend sparkline per currency (docs/15). Derived entirely
from the shared BoC `fx_rates` cache; no model, migration, or persistence.
**Deps:** M6 (sidebar + Settings shell exist), M11 (BoC cache backfill path).
**Acceptance:** `GET /api/v1/fx/board` returns one `FxBoardItem` per curated
currency (CAD excluded) with `latest_rate`/`as_of`/`series`; privacy mode degrades
to `latest_rate=None`/`series=[]` with no 5xx; `days` query capped 1–365;
`/currencies` renders the board under a new "Tools" sidebar group.
**Smoke test:** open `/currencies` as the demo user, confirm curated rows render as
`1 USD = 1.37 CAD` with sparklines, and under privacy mode they show `—` with no error.

## M24 — MCP Server

Standalone Python MCP server (docs/23) exposing all REST endpoints as typed tools. Includes: `mcp_tokens` table (migration 0015) for opaque bearer auth, MCP token CRUD endpoints (`/auth/mcp-tokens`), `McpAuthMiddleware` ASGI middleware for token validation, internal endpoint for backend JWT issuance, Docker service on port 8001. 105 tools covering every API endpoint. Stdio mode for local dev, streamable-http for remote access.
**Deps:** M1.
**Acceptance:** `mcp_tokens` migration clean; token create/list/revoke round-trips; MCP server starts in both stdio and streamable-http modes; auth middleware validates tokens and rejects invalid ones; all 105 tools listed via `tools/list`; tool calls proxied to backend with correct user context.
**Smoke test:** create MCP token, start MCP server, connect with token, call `health_check` and `accounts_list`, verify responses match direct API calls.

## M29 — Review Remediation

Fixes from the 2026-09-19 full-application review (docs/29). Committed-only
staging filter via the shared `app/services/txn_filters.py` predicate applied to
budgets, spending alerts, insights, spending comparison, net-worth snapshot
(plus `starting_balance`), subscriptions (13-month window), goals, and dividend
calendar (13-month window + user-scoped Security select); tax-loss alerts read
account taxability so `/taxes/alerts` fires; delete paths (users, accounts,
securities, categories, transactions) gain complete FK guards with pinned 409
codes; MCP client fails closed instead of falling back to a shared token; SSE
stream restored to docs/18 cache-only contract with Decimal-as-string prices
and a client-side reconnect that refreshes tokens; benchmarks money math moves
to Decimal (docs/28 placeholder retained); input validation gaps closed
(bill category ownership + date typing, goal PATCH account check, debt update
constraints, typed reconciliation uploads, tax projection province/year bounds)
with new pinned 422/404/409 codes; rate limits on `/users/me/llm-test` and
`/securities/lookup`; JWT secret validator rejects the `.env.example`
placeholder and enforces ≥32 chars; `secrets.compare_digest` on the internal
secret; frontend fixes (TaxProjection data-loss save, mutation onError toasts,
reconciliation Difference card + CSV NaN filter, e2ee stale-resolution cache,
login 401 handling, Currencies hook order); superficial-loss replay de-O(n²)-ed;
model metadata synced to migrations; compose healthchecks + parameterized DB
password + unprivileged web; CI gains `--max-warnings 0`, secret-scan, and
concurrency group; stale `test_csv_split` auto-resolve test + all ruff errors
fixed.
**Deps:** M21–M24.
**Acceptance:** all gates green (ruff format/check, pytest on Postgres, tsc,
eslint --max-warnings 0, build); delete-user smoke leaves no orphan rows;
pending imports never inflate budgets/insights/net-worth; SSE emits string
prices with no network in the stream path; MCP resolves user tokens or errors —
never a foreign tenant.
**Smoke test:** per docs/29 Verification.

## M52 — Cash-flow calendar

**Description:** `/calendar` — one month grid over the three forward-looking cash-flow sources the app already computes: bill reminders (M40, docs/25), the M29 dividend calendar (docs/24, `services/dividend_calendar.py`) and the M27 subscription detector (docs/24, `services/subscriptions.py`). Composition and presentation only — no new detection, no persistence, no migration, no network (docs/07, docs/12). Spec: **docs/36**.

**Approach:**
- **API** `GET /api/v1/reports/cashflow-calendar?month=YYYY-MM` — `month` optional (absent = the server's `today_et()` month, the `?year=`-style default), anything else a pinned **422 `invalid_month`** in the 1900–2200 band (it also bounds the bill loop). Response `{month, today, days[]}` with **exactly one entry per calendar day, zero-filled**; `today` is `today_et()` (docs/29 F1d) and the only clock behind `status: past|today|upcoming`. `amount` is a **signed** Decimal string (dividend +, bill/subscription −, `null` only for a variable-amount bill) **stated in its own `currency`** (see the M170 follow-up section at the end of this file: the pair is all-or-nothing); `source_id` is `bill id | security id | confirmed payee` and `kind` names the click-through target (`/bills`, `/securities/{id}`, `/bills?autofill=…&amount=…&due=…`). Bills project **forward** from `next_due_date` by `monthly|quarterly|yearly` cadence with the day of month anchored on the original date (2026-01-31 → Feb 28, Apr 30), `one_time` never repeats, malformed stored dates are skipped rather than 500ing, and **past months are never replayed** (bills carry no payment history — docs/25 decision 4). Every query is user-scoped; both detectors already apply the shared committed-only predicates, so staged rows can't appear; no fx read at all.
- **Frontend** `/calendar` → `pages/Calendar.tsx` wired the M49 way (`pages.calendar` + `route(pages.calendar)`, lazy). One query key = one queryFn: `["cashflow-calendar", month ?? "current"]` (the default fetch and an explicit month are separate cache entries); the client never derives the current month or "today" — Prev/Next shift the returned `month`, **Today** resets to the default fetch. Monday-first grid (docs/12), kind-colored dots + `+N` overflow, today ring from the response, portaled day popover (Escape/backdrop/close) whose rows link to each source; semantic tokens only. No sidebar entry: `AppShell.tsx` is out of this card's ownership, so `/calendar` is URL-reachable until a follow-up card adds the nav row (docs/36 "Out of scope").

**Touch points:** `docs/36-cash-flow-calendar.md` (new, pinned before code), `backend/app/services/cashflow_calendar.py` (new), `backend/app/api/reports.py`, `backend/app/schemas/reports.py`, `backend/tests/test_cashflow_calendar.py` + `test_cashflow_calendar_demo.py` (new), `frontend/src/api/cashflowCalendar.ts`, `frontend/src/pages/Calendar.tsx`, `frontend/tests/calendarPage.test.tsx` (all new), `frontend/src/App.tsx`, `docs/08`, `docs/09`, `docs/10`, `NEXT_WAVE_PLAN.md`.

**Acceptance:** spec committed before the first code line (AGENTS §7); `ruff format --check` + `ruff check` clean; `pytest -q` against real Postgres on the isolated suite DB — **872 passed, 0 failed, no deselections** (25 of them new: 23 contract tests + 2 hermetic demo-month tests); `tsc --noEmit` and `eslint . --max-warnings 0` clean; full `NODE_ENV=test vitest run` **34 files / 332 tests** green (exit 0); `npm run build` green with the entry chunk at **448,848 B** (≤ 700,000 B target) and `Calendar` shipped as its own **8,465 B** lazy chunk; the demo-month acceptance passes hermetically (every seeded bill due date, every M29 projection and every detector `next_expected` renders, zero-filled, demo read 200 while a mutation on the same token stays 403 `demo_account_restricted`).

**Smoke test:** the integration card rebuilds/restarts docker and exercises `/calendar` in a browser through `http://localhost:8080`; the endpoint's running-stack smoke is `GET /reports/cashflow-calendar` as the demo tenant (200, three kinds present) plus `month=2026-13` → 422 `invalid_month`.

## M59 — Notification delivery channels (email + webhook)

Spec-first (docs/37 written and committed before any code). Adds the outbound
half of the M51 inbox: two transports (SMTP `smtplib`, signed generic webhook)
behind one `send()` contract, an auditable outbox
(`notification_channels` / `notification_deliveries` /
`notification_delivery_attempts`), an in-process worker with 5-attempt
backoff (30/60/120/240 s) and dead-letter + re-arm endpoint, and the
privacy-mode resolution — delivery off by default at both levels, payload of
exactly five fields (never `title`/`body`/`amount_cad`), per-channel explicit
opt-in required under privacy mode. Secrets stay env-only
(`NOTIFY_SMTP_*`, `NOTIFY_WEBHOOK_SECRET`), so the export payload stays
secret-free.
**Deps:** M51 (docs/35 union) — no other milestone.
**Acceptance:** all gates green (ruff format/check, pytest on Postgres, tsc,
eslint --max-warnings 0, vitest, build, docker rebuild + smoke); privacy-mode
user produces **zero** outbox rows and **zero** sends until they opt in;
toggling a type off removes it from the inbox AND stops its delivery; failed
deliveries leave the inbox byte-identical; webhook signature verifies from raw
bytes and the message body contains no budget/bill name or amount.

## M60 — API performance budgets + N+1 audit

Makes "fast" a tested property instead of a feeling: a per-endpoint
**SQL-statements-per-request budget** that CI enforces, plus a **p95 latency
budget** the same harness measures and reports (asserted only under
`PERF_LATENCY_GATE=1`, because wall-clock on a shared runner is not a stable
baseline). Both tables, the measurement dataset and method, the slow-query
threshold and every waiver live in `docs/34`, which also carries the audit of
**all 50 feasible `GET` endpoints** — so the N+1s below are measured facts.

Three measured N+1s fixed (before → after, paired `PERF_SAMPLES=200`):
per-row tag lookups on `GET /transactions` (**53** queries at `limit=50`,
**203** at `limit=200` → **4**, p95 63.7 → 13.9 ms); a per-bucket
`SELECT securities` in `net_worth_report` (**18** monthly / **368** daily →
**7**, p95 128.4 → 96.1 ms); and a per-trade `fx_rates` read in the ACB replay
(**25** statements per computation → **1**, `capital-gains` **27 → 3**,
`investment-performance` **31 → 7**).
**Deps:** M10 (demo dataset), M29/M8 (report routes).
**Acceptance:** `docs/34` complete per §9 (budget table, method, tooling,
waivers) BEFORE any code; query-count guards green in CI for the eight
budgeted endpoints; page-size and bucket-count scale-independence invariants
green (a guard that fails 12/19 on the pre-fix tree); measured before → after
p95 table in the completion report; any endpoint over budget either fixed or
waived with rationale in the spec; §4 gates green; `docs/08` + `docs/12` +
this entry reconciled in the same commit.
**Smoke test:** `PERF_SAMPLES=200 PERF_REPORT=<file> uv run pytest -q
tests/test_perf_budgets.py` prints the budget table — every budgeted endpoint
200, `queries_stable` true, `max_statement_repeat = 1`.

## M77 — Analytics depth: Sankey cash-flow + total-return metrics

Wave-5. Two analytics surfaces that build entirely on data the app already
stores — Sankey cash-flow is still open+experimental upstream (Actual #1919:
nobody ships it well) and total return including reinvested dividends is
Ghostfolio's top open ask (#7468).

**Sankey cash-flow chart** (docs/12 §8) on the Reports Cash-flow tab: sources
→ one `Money in` hub → destinations, hand-rolled SVG (no d3-sankey — the locked
stack adds no dependency), semantic palette tokens + the M67 CVD rules, one
`chartAriaLabel` sentence over an `aria-hidden` svg. Population and signs are
the cash-flow report's exactly; `buy | drip` principal lands on `Investments`
and the surplus on `Savings` (or a balancing `Transfers & savings in` source
when spending exceeds income), cent-quantized before summing so the graph
conserves to the cent. Own query, skeleton and `NoDataPanel` so an empty graph
never hides the composed chart.

**Total-return metrics** (docs/05 §Total return) on Investments: TWR (daily
chain-linked) and MWR (Modified Dietz) as PERIOD returns over the same window
the benchmark comparison uses — one pair of constants in `Investments.tsx`, so
the two cards can never drift. Reinvested dividends are neutral by construction
(price gives up what the cash/DRIP gives back) while `income_cad` still reports
gross income. The benchmark honesty gate is unchanged (`benchmark_points < 2` →
"No benchmark data for this window", M65/docs/28).

**Money math:** `Decimal` on the backend (`app/services/total_return.py`,
`services/reports.py::cashflow_sankey`), BigInt-mirror client engines
(`lib/totalReturnEngine.ts`, `lib/sankeyEngine.ts`), one shared integer
`ratio_pct` divisor so both sides round once, half-up, to a fixed 8-decimal
plain string. Routes `GET /reports/cashflow-sankey` and
`GET /reports/total-return` stay mounted as the reference implementations; both
are JSON-only (no flat table to export).

**Deps:** M67 (palette/CVD), M65 (honesty gates), M48 S3/S4 (parity harness
pattern), docs/33 (client-compute contract).

**Acceptance:** parity goldens `frontend/tests/fixtures/m77-vectors.json`
(3 ranges × 2 reports, captured by `backend/scripts/capture_m77.py`) diffed
byte-for-byte by `frontend/tests/m77Parity.test.ts` — 0 diffs (that harness
retired with M160/P2-5; the vectors are kept as route pins, replay wiring is
card `t_62afc8a5`); behaviour tests
`frontend/tests/m77Behavior.test.tsx` (Sankey renders ≥2 income + ≥3 expense
categories, panel totals + no-data states) and
`backend/tests/test_total_return.py` (hand-derived ledger inside 1e-8:
TWR 10.9%, MWR 12.46915167%, a DRIP-neutral window, a pure transfer at exactly
0); §4 gates green; `docs/12` + `docs/05` + `docs/08` reconciled in the same
commit.

## M76 — YNAB import (preview-first budget migration)

Moves a household off YNAB in one upload: `POST /api/v1/imports/ynab/preview`
returns the complete mapping — accounts (with a type-mapping table), the
group → category tree, group tags, targets (MF → `budgets`, TB/TBD → `goals`,
everything else flagged unsupported with a reason), and every transaction row
— without writing anything, and `/commit` applies that exact plan in one
transaction into the usual `pending_review` batches. Amounts (milliunits →
cents), dates (ISO, or the CSV's own day/month evidence with an explicit note
when assumed), cleared state (new nullable `transactions.cleared`, migration
`0033`), transfer pairs, and the docs/03 dedup contract are all preserved;
rows that cannot import (bad date/amount, deleted or unmappable account,
missing account) come back with a code, a reason and their row number rather
than being skipped. Spec: `docs/38`.
**Deps:** M9 (accounts/categories), M52-era batch pipeline in `docs/03` — no
new milestone dependency; M71's shared dry-run never landed, so the preview
is its own read-only plan builder (deviation recorded in `docs/38` §8).
**Acceptance:** all gates green (ruff format/check, pytest on Postgres, tsc,
eslint --max-warnings 0, vitest, build, docker rebuild + smoke); the preview
test asserts every table's row count is unchanged after a preview; a second
commit of the same budget reports `net_new = 0` with all counts reused and
the live transaction count unchanged after batch commit; every unmappable row
in the fixture returns a distinct code and a reason; a 5 MB file and a
non-YNAB payload return `{detail, code}` envelopes (`file_too_large`,
`ynab_parse_error`), never a 500; E2EE tenants are refused on commit with
`encryption_required`; the dedicated `/help/migrate-from-ynab` guide
(export recipes + the "what maps cleanly" / "what does not come across"
tables) and its static-site mirror stay in sync.
**Smoke test:** `uv run pytest -q tests/test_ynab_migration.py` prints 11
passed against the JSON/CSV fixtures.

## M79 — Bank sync readiness (open-banking seam)

Spec-first (docs/39 written and committed with the code). Canada's
Consumer-Driven Banking Act reaches royal assent Mar 2026, draft regs closed
Aug 2026, so read access goes live late 2026–2027 with the Bank of Canada as
regulator and ~9M Canadians on screen scraping today. This milestone builds
the **seam**, not an integration: a four-method `Provider` protocol and slug
registry, a deterministic stub adapter behind a fixture, `sync_agreements`
consent/revocation (`active | auth_required | revoked`, **no credential
column**), per-account `last_sync_at` + `provider_status` status-page
groundwork, and — the part that matters — **ingestion parity**: a pull stages
through the same `services/import_staging.stage_rows()` that
`POST /imports/upload` uses, so the batch lifecycle, the 2000-row cap, the
cache-only CAD rule and the duplicate bridge are one code path rather than two
implementations. Adopting an accredited provider later is a
`register_provider` config change.
**Deps:** M19 (imports/staging), M65 (account model) — no other milestone.
**Acceptance:** docs/39 complete per §9 (endpoints, error codes, consent
semantics, verification incl. the CDB timeline) before/with any code; all §4
gates green; `POST /imports/upload` and `POST /sync/agreements/{id}/pull` share
one staging function (test asserts CSV-then-sync of the same transaction
collapses to ONE live row plus a staged match decision); revoke stops further
pulls; `encryption_required` tenants pull plaintext rows while client-side
writes still 422; privacy mode blocks every provider call with `403
privacy_mode_blocks_external_api` while `GET /sync/providers` stays open;
`413 too_many_rows` stages zero rows; the export registry picks up
`sync_agreements` with no registry edit.
**Superseded in part, 2026-10-12 (M181, t_89fa8b10):** the line
*"`encryption_required` tenants pull plaintext rows while client-side writes
still 422"* was M79's acceptance for the seam as it shipped and is **no longer
the behaviour**. Every ingestion write that would create a plaintext row for an
opted-in tenant — the sync pull, OFX/QFX/CSV upload and its commit, the
brokerage trade import and its commit, a recorded dividend, and manual
`POST`/`PATCH /trades` — now fails closed with `422 encryption_required`
(disclosed in docs/33 § "OUTSIDE THE ENVELOPE (M79)"). The rest of the M79
acceptance above stands unchanged: the staging parity, revoke, privacy mode,
and the `413` cap all still hold, and a tenant that has NOT opted in pulls into
the plaintext columns exactly as before.


## M74 — Auth hardening: TOTP 2FA, passkeys, optional OIDC SSO

The account gets a second factor and an SSO door, without a second session
contract: TOTP (RFC 6238) enrollment with single-use hashed recovery codes,
WebAuthn passkeys (register on a live session, passwordless login, rpID =
`APP_ORIGIN`), and an OPTIONAL config-gated OIDC authorization-code flow against
self-hosted providers (Authelia/Keycloak class) that links by email only after
an explicit confirmation and never creates a user. Every path — password,
TOTP, recovery code, passkey, SSO confirm — mints the session through the SAME
`_issue_pair` + httpOnly refresh cookie, so the MCP `BackendClient` cookie
contract is untouched by construction and the login-burst 429 pattern is
extended, not replaced. The demo tenant is exempt everywhere: it never
challenges, it cannot enroll, and SSO refuses to link it.
**Deps:** M16/M29 security waves (auth plumbing, audit M1 cookie contract).
**Acceptance:** behavior tests for enroll → challenge → login (valid, wrong,
expired, replayed code), recovery-code single-use, passkey
register → assertion → session, mocked OIDC callback → confirmation → the same
cookie session, demo exemption, and `tests/test_mcp_server_client.py` green
unchanged; §4 gates green; `docs/38` written FIRST (spec-first), with
`docs/08` + `docs/02` + this entry reconciled in the same commit.
**Smoke test:** `uv run pytest -q tests/test_auth_totp.py tests/test_auth_passkeys.py
tests/test_auth_oidc.py` green against Postgres, then a live enroll → logout →
login → code → dashboard cycle through `http://localhost:8080`.


## M80 — MCP agent surface: read-only tokens, tool filtering, write receipts

Spec-first (docs/40 written with the code). Competitive research
(`/home/hermes/wave5-research/wave5-competitive-research.md`) shows the churn
vector has moved from app-to-app to app-to-DIY-agent: the moat this app can
claim that a prompt-driven stack cannot is **money safety at the agent seam**.
Two things make that claim testable. (1) A **scoped credential**: `mcp_tokens`
gains `policy` (`full` | `read_only`, server default `full`), and the MCP
server's `AgentSurfaceMiddleware` (context tier) removes every write tool from
`tools/list` for a restricted session — `policy != full` OR the demo tenant —
and refuses a write `tools/call` with `403 mcp_read_only` / the demo tenant's
own `demo_account_restricted` **before the proxy runs** (no backend call, no
JWT). Classification is derived from `server.py` itself (AST over each tool's
`client.request` method) so it cannot drift: 105 tools = 52 read + 53 write,
with a seven-POST compute-only allowlist that a test keeps honest. (2) A
**receipt**: every `tools/call` lands in `mcp_audit` (user, token, tool,
sha256 of the arguments, response code, duration) through the existing
`MCP_INTERNAL_SECRET` seam, best effort by design, readable at
`GET /mcp-audit` (own rows only), wiped with the account. Plus the
documentation this is judged on: an EXECUTED quickstart at
`/help/agent-integration` and the MCP reference at `/help/configuration`,
static twins, home-page section notes, and the public path
`https://<host>/mcp/mcp` (nginx `location /mcp/` → the mcp container over
Docker DNS, validated with `nginx -t` on the generated config).
**Deps:** M24 (MCP server), M29 (MCP tokens), M50 (demo guard) — no other
milestone.
**Acceptance:** docs/40 complete per §9 before/with the code; all §4 gates
green; mint → list → validate round-trip returns `policy`/`token_id`/`is_demo`;
a read-only session lists 52 annotated tools and a refused write reaches no
backend (spied transport asserts zero calls) while its receipt is recorded;
`response_code` derivation matches the pinned table (403 / passthrough / 400 /
500 / 200); tenancy holds on `GET /mcp-audit`; `internal-mcp-audit` is 403
without the shared secret and `deny all` at nginx; the export registry picks up
`mcp_audit` with no registry edit; the quickstart in the help guide is executed
against the running stack and its transcript committed.
**Smoke test:** mint a read-only token through the deployed stack, connect a
real MCP client to `…/mcp/mcp`, `tools/list` → 52 tools with markers, call
`accounts_create` → `403 mcp_read_only`, then `GET /api/v1/mcp-audit` → a row
for every call in the run including the refusal.

## M82 — Brokerage CSV import profiles (Questrade, Wealthsimple, IBKR, Fidelity, TD/RBC DI, NBDB)

Spec-first: `docs/41-brokerage-import-profiles.md` written before the code.
The competitive evidence for this wave is that no mainstream personal-finance
app ingests brokerage data reliably — *"Not one can interface with brokerage
firms like IBKR and Fidelity without issues"* — while Canadian tax depth (M5)
is untouched by all of them. M82 turns that gap into breadth: **seven
profiles that are pure configuration plus a fixture each**, all funneling into
`investment_profile.build_trades` and the ONE trade-staging loop, so a broker
export either imports with correct semantics or is refused with a
per-institution reason — never partially and never silently. Profiles:
`questrade`, `wealthsimple`, `ibkr` (flat Flex Query CSV **and** the sectioned
Activity Statement; IBKR Canada folds in because the layout is identical),
`fidelity`, `td-di`, `rbc-di`, `nbdb` (English **and** the French
semicolon-separated export). Each ships a reconstructed golden fixture with a
`.expected.json` sidecar of its own stated totals, an explicit per-account
currency rule (`row_column` / `file_constant` / `target_account` — never
per-row), a zero-write dry run (`POST /trades/import/preview`) that reports
the exact verdicts staging will produce, and a per-institution export recipe
at `/help/brokerage-imports` with a static twin.
Out of scope (unchanged): new migrations, direct API connectivity to any
broker, screen scraping, aggregator/bank auto-sync (M79), and any UI beyond
the import wizard's per-institution picker.
**Deps:** M71 (import bar), M79 (`stage_rows` seam), M5 (ACB engine),
docs/07 fx rules — no other milestone.
**Acceptance:** docs/41 complete per §9; `pytest -q
tests/test_brokerage_profiles.py` green (34 tests; 9 smoke fixtures covering
all 7 profiles, per-institution numbers pinned in docs/41 § Verification);
per fixture dry run → stage → commit → `positions` and `cash_effect`
recomputed **from the database** match the sidecar, and a re-import reports
net-new 0; dry-run counts are byte-identical before and after every preview;
every unmapped row carries its institution label and `parsed + unmapped ==
eligible data rows`; a `file_constant` USD row survives a CAD account
(`listing_currency == "USD"`); unknown corporate actions answer `422
unknown_corporate_action` with zero writes; `unknown_import_profile`,
`profile_header_mismatch` and the stuck-row `parse_error` are all 422
envelopes; docs/02, docs/03, docs/05, docs/06 and docs/08 cross-reference the
new profiles in the same commit; the export registry and the three
`ImportBatch(` construction sites are unchanged; all §4 gates green.
**Smoke test:** through the deployed stack — pick an institution in the
Imports wizard, upload that broker's activity CSV, confirm the staged counts
plus the per-institution notices, commit, and check positions and cash effect
against the file's own totals; uploading the same file again reports net-new 0.

## M81 — demo MCP surface + doc reconciliation (QA follow-up to M80)

M81 was the QA acceptance pass over M80 (Wave 6); five findings came back, all
docs-vs-ship gaps. The one that needed a decision was **M81-1**: the guide and
docs/40 described a demo-tenant MCP surface no deployment could reach — the
demo write guard 403'd `POST /auth/mcp-tokens`, so `mcp_tokens` had 0 demo rows
and the `is_demo` branch only ever ran in CI. The PM decision (docs/40 §1):
the demo account is the evaluation front door, so the demo tenant may mint —
**forced to `read_only`**. Implemented as two enumerated demo-guard exemptions
(`POST /auth/mcp-tokens` exact path, `DELETE /auth/mcp-tokens/{id}` path
prefix), `McpTokenCreate.policy` typed `McpTokenPolicy | None` so "omitted" is
distinguishable from an explicit `full`, and a new `422 demo_mcp_read_only` for
the latter. Every other demo mutation still answers `403
demo_account_restricted`. The other four were documentation truth-ups, no
behaviour change: **M81-2** the guide claimed a revoked token is refused "with
`invalid_mcp_token`" — the public answer is `401 {"error":"Invalid MCP token"}`
with no code field (the code lives on the internal validate route nginx 403s);
**M81-3** docs/40 promised `{"readOnlyHint": true, "destructiveHint": false}`
for read tools (the wire omits `destructiveHint` for reads — it is set only in
the `if write:` branch) and `args_hash CHAR(64)` (the migration creates
`VARCHAR(64)`); **M81-4** docs/12 §3 listed `transfer_in`/`transfer_out` in
Income/Expenses, contradicting §8 and `CASH_INFLOW_ACTIONS`/`CASH_OUTFLOW_ACTIONS`
— the QA hand-derivation matches §3-with-transfers-excluded byte-for-byte;
**M81-5** leftover deployed `m80-quickstart-*` users were deleted through the
normal delete-user path after the code shipped.
**Deps:** M80 (docs/40), M50 (demo guard), M29 (delete-user cascade) — no other
milestone.
**Acceptance:** docs/40/08/20/23/12 reconciled in the same commit as the code;
the demo mint contract pinned by
`test_demo_mcp_token_routes_are_the_only_new_exemption` (201 `read_only` →
422 `demo_mcp_read_only` → validate `{read_only, is_demo}` → 204 revoke) and the
exemption sets pinned by `test_demo_write_exemptions_are_enumerated_never_inherited`;
the full §4 gates green.
**Smoke test:** through the deployed stack — demo login → `POST
/api/v1/auth/mcp-tokens` (no policy) → 201 `read_only`; same call with
`"policy":"full"` → 422 `demo_mcp_read_only`; `POST /mcp/mcp` `tools/list` →
52 tools titled `… · read-only token · demo tenant is read-only`;
`transactions_create` → 403 `demo_account_restricted`; revoke → 204; every other
demo write still 403 `demo_account_restricted`.

## M84 — MCP cross-tenant credential leak (P0, security fix for M83/F-1)

The M83 security review wire-proved that the MCP server's ONE module-level
`httpx.Client` kept every tenant's `Set-Cookie: refresh_token` in its shared
cookie jar and replayed it on any later request whose path matched the
cookie's `Path` — so tenant B's `auth_refresh` returned an access token for
tenant A (`sub=124`), and `auth_logout` could cascade-revoke a victim's
session. Reachable from the public internet with any valid MCP token,
including a read-only one (the auth tools are in `COMPUTE_ONLY_TOOLS`).
**M84-1** the shared jar can no longer store: `new_http_client()` installs a
`_NoStoreCookies` (an `httpx.Cookies` whose `extract_cookies` is a no-op) after
`Client.__init__`, and a response's refresh cookie is read off that response's
raw `Set-Cookie` headers into the request/session-scoped `refresh_token`
ContextVar instead (`_absorb_refresh_cookie`), so identity and cookie state
live and die together. **M84-2** `_refresh_cookie()` consults only the
caller-bound token and otherwise emits no cookie at all; the jar preference
(`cookie_refresh_token() or refresh_token`) is hard-deleted. **M84-3**
`policy.AUTH_TOOLS` (`auth_register`, `auth_login`, `auth_demo`,
`auth_refresh`, `auth_logout`) are refused on the streamable-HTTP path with
`403 {"detail": {"code": "auth_tools_unavailable_on_mcp"}}`, checked before
the policy guard and before the proxy; stdio keeps all five.
**Deps:** M80 (docs/40, the seam + receipts), M24 (MCP server), M47/M29
(fail-closed identity), M83 (the review that found it) — no other milestone.
**Acceptance:** regression tests in `test_mcp_server_client.py` +
`test_mcp_agent_surface.py` pin the jar, the caller-bound refresh and the
five refusals (all listed in docs/40 §2 Verification 8); docs/23 + docs/40
reconciled in the same commit; the reviewer's own `jar_leak_probe.py` re-run
against the deployed stack returns `REFUSED` instead of
`CROSS-TENANT TOKEN LEAK` (transcripts under `docs/assets/m84/`); the full
§4 gates green.
**Smoke test:** through the deployed stack — `tools/call auth_refresh` under
a real MCP token → `isError` with `auth_tools_unavailable_on_mcp` and no API
request on the wire; `tools/call accounts_list` still 200 (identity from the
MCP token); `GET /api/v1/mcp-audit` shows a 403 receipt for the refused call.


## M87 — Own-the-edge security: enumeration oracles, passkey counter, API map, CORS, headers

**Why now:** Mark's post-review decision is that the app **owns its own edge
security** — the Pangolin PIN in front of the public origin must not be
assumed, because fin_tracker will launch on a VPS with no edge gate. That
elevates four M83 findings from P2/P3 to P1 and adds one the reviewer
discovered by probing: FastAPI was serving `/docs` and `/openapi.json` open on
an internet-facing app.

**S1 — account-existence oracles closed (M83 F-7).** `POST /auth/register`
answers `200 {status: "accepted", detail}` identically for a free and a taken
address, with no token and no cookie; the free branch still creates the
account, so recovery is "sign in with the password you just chose" (no SMTP
required — docs/37). `POST /auth/passkeys/login/options` answers `200` with a
same-shaped challenge envelope for every address; keyless cases get a throwaway
challenge bound to a user id that resolves to nothing, and the ceremony is
where a wrong case fails (`401`). The responder's `allowCredentials` id VALUE
still discloses passkey enrollment — a documented residual of email-first
non-discoverable login (real, DB-stable id vs per-request throwaway), not a
uniform channel (docs/42 §2). `409 duplicate_email`
and `404 no_passkeys` are retired from those two routes; both survive where
they belong (profile change; nowhere). The decision record is **docs/29 F15**,
the full contract **docs/42 §1–2**.

**S2 — passkey sign-counter hardening (M83 F-6).** `sign_count` is adopted only
when `new_count > 0`; a `0` ("this authenticator does not count", WebAuthn
§6.1.3) leaves the stored counter untouched. This makes code match the rule
docs/38 had been documenting all along, and makes the invariant local instead
of relying on `webauthn` — which, measured, already refuses a `0` against a
non-zero stored count inside `verify_authentication_response`, so M83 F-6's
"one non-counting sign-in disarms clone detection" was not reproducible
through this path (docs/42 §3 records the measurement rather than the
assumption).

**S3 — the API map is authenticated-only.** FastAPI's built-in docs routes are
disabled and re-registered behind `require_docs_access`: session access token
or a live MCP token ⇒ `200`; anything else ⇒ `401 docs_auth_required` (a
malformed bearer included — never a 500). `API_DOCS_PUBLIC=true` is the
local-dev keep-working path and is unset in production, so it fails closed.
nginx proxies `^/(docs|redoc|openapi\.json)$` to the API before the SPA
fallback, inheriting the CSP — which is why the interactive Swagger UI is for
local dev / the API port while `/openapi.json` works through the edge.

**S4 — production `CORS_ORIGINS`** corrected in place on example.internal (never
committed); `.env.example` now documents the prod shape and the failure it
prevents.

**S5 — header hardening** in the frontend nginx: HSTS gains
`includeSubDomains; preload`, plus `Permissions-Policy`, `COOP` and `CORP`,
emitted `always` at server level and in the two `add_header`-declaring
locations that would otherwise drop them. Additive only.

**Deps:** M83 (the review that found them), M74 (passkeys, docs/38), M80 (MCP
tokens as a docs credential), M84 (its probe must stay green). No other
milestone.
**Acceptance:** a pytest test per item (docs/42 §Verification names them),
pre/post curl evidence through the running stack for S1–S3, a header scan and
a CORS preflight before/after for S4–S5, `scripts/smoke.sh` 11/11 after the
rebuild, and `docs/assets/m84/jar_leak_probe.py` still returning `REFUSED`.
**Smoke test:** through `http://localhost:8080` — register twice with the same
address and get identical 200 bodies; `POST /auth/passkeys/login/options` for an
unknown email returns 200 with an `allowCredentials` list; `GET /docs`
returns the 401 envelope with no header and 200 with a session token;
`curl -sI` on any page shows the four hardened headers.

## M89 — Launch story: proof-linked landing, /changelog, guided first run

The surfaces a first-time visitor actually meets: a landing page that argues
with evidence instead of adjectives, a changelog in user language, a demo that
teaches itself, and three walkthroughs that show the product doing the thing.

**S1 — landing page (`site/index.html`).** Hero states the positioning
(agent-drivable · self-hosted · Canadian · import-as-hero) with the demo login
as the front door and a link to the changelog; four differentiator cards, each
pointing at the guide that proves it rather than asserting the claim. New
**What we are not** section — no scraping, no ads, no trackers, no model filing
your transactions behind your back, no lock-in — closing on the refusal
framing. New pricing/posture table pairing every promise with what backs it;
the "no price rise" row is labelled as the aspiration it is, because there is
no billing code in the product to enforce it. (Layout note: the landing page
was rebuilt for the public URL in t_fe44260e — the pricing table, agent block
and guide-card grid were dropped, and the limitations list now lives under
`#limits`; docs/32 § Static site is the current structure of record, and the
text above is the historical scope of this milestone.)

**S2 — `/changelog` (`site/changelog.html`).** M-number → user-visible-benefit
tables grouped by wave, TL;DR first, newest first, no commit hashes. Served by
an nginx exact-match location in `site/Dockerfile` (a bare `try_files $uri`
404s a file-backed route with no extension). Linked from the nav, the hero and
a "What shipped this wave" section.

**S3 — Help hub quickstart + guided first tour** (docs/09 § "Help hub
quickstart" / "Guided first tour"). Three executable tracks above the guide
grid, a **Take the tour** resurface button, and four first-session coach marks
on the demo dashboard: persistent per-account dismissal in `localStorage`, a
per-tab-session guard in `sessionStorage`, no server field, never modal.

**S4 — three walkthrough guides** (docs/32 § "M89 learning path").
`first-import` (all six import states, with the red/amber/green verdicts
explained against the shipped UI), `first-budget` (the 206% worked example),
`agent-tour` (M80's transcript and receipts as proof images) — every step a
real prod capture, each mirrored to `site/guides/<slug>.html`.

**S5 — static contract.** Token discipline with the contrast pairs documented
where the tokens live, landmarks/favicon/`tabindex` on scrollable regions for
every new or changed page, and tests that fail when either drifts
(`tests/siteTokens.test.ts`, `tests/m89HelpPath.test.tsx`, `tests/tour.test.tsx`).

**Deps:** M65 (the honest budget percentage the worked example reuses), M71
(the import verdicts the walkthrough explains), M80 (the transcript
`agent-tour` renders), M82 (the 390/412 floors the pages are measured against),
docs/32 (the copy-drift rule the mirrors satisfy). No other milestone.

**Acceptance:** landing page, `/changelog` and the three mirrors each report 0
axe-core violations at 390 and 1280, 0 console errors, and no horizontal scroll
at 390 or 412; the critical path (HTML + CSS, raw) stays ≤ 75 KB; `tsc
--noEmit`, `eslint --max-warnings 0` and the vitest suite are green with the
three new test files; the three tracks are walked end to end in a fresh session
with a screenshot at each step; the tour fires on a first demo session, stays
gone on a second fresh session, resurfaces from Help, and is keyboard-clean
(focus, Escape, Tab wrap).

**Deviation (reported):** docs/34 pins backend query/latency budgets and the
repo has no axe/HAR CI pipeline, so the ≤75 KB critical path and the axe/console
numbers are locally measured (headless Chromium + axe-core 4.13) and recorded
here rather than enforced by a CI gate.

**Smoke test:** through the running stack — `/` and `/changelog` render clean
on the site container with `curl -sI` showing the hardened headers; `/help`
shows the three tracks and the resurface button; a fresh demo session shows the
four coach marks exactly once; `/help/first-import`, `/help/first-budget` and
`/help/agent-tour` each load with every referenced screenshot present.

## M90 — Household sharing: owner + partner, read-scoped approved projection

**Spec-first: `docs/43-household-sharing.md` written BEFORE any code; the PM
approves it in-line on the card before implementation starts** (the card asked for `docs/42`; that slot belongs to M87 and is cited
from 14 files, so this takes the next free number — see the deviation note at
the top of docs/43). This is the deferred-from-Wave-5 table-stakes gap vs
Monarch (blanket multi-user) and Lunch Money (per-invite scoped roles), and
the first feature here where a **second authenticated user reads a first
user's rows** — which is why it gets its own tenancy wave.

The contract in one paragraph: **two roles, one link** (`owner` +
`partner`, one active link per side, enforced by partial unique indexes);
**scope = per-account allowlist with a deny-by-default `[]`** — an
unallowlisted account contributes *literally nothing* to any partner payload,
so M90 adds no `/partner/reports/*` at all: the partner's page computes net
worth/budgets/spending client-side from the rows it holds, exactly as the
owner's page does since docs/33 S2, putting the allowlist where it cannot be
bypassed. Consent is `pending → active → revoked` (7-day single-use email
token, M79-style idempotent local revoke); revocation is effective on the
partner's **very next request** because the partner holds no token of the
owner's — every `/partner/*` data read re-reads the link row and answers `401
sharing_not_linked` with the clean envelope (`/partner/state` flips to
`200 {linked: false}` instead — M92, docs/43 §6). The partner's only write in the
entire system is accepting the invite addressed to them: the read surface is
`GET`-only, with a test. E2EE: the partner resolves the owner's rows with
`dek = null`, unconditionally, so cipher fields render `🔒` — the owner's
locked shape, in both of the owner's lock states, with no key material and no
unlock lever anywhere in a partner payload (there is no server-side unlock
action in this product to call). Privacy mode governs from the **owner's**
row: the invite email is suppressed (`email_sent: false`) and partner reads
bind the owner's privacy lock onto all three egress seams — zero outbound HTTP
on a full sweep. Audit receipts (`invite_create` / `invite_cancel` /
`invite_accept` / `scope_update` / `revoke`) land in `sharing_audit` on the
`mcp_audit` shape (docs/40 §3), handler-level refusals included (a
pre-handler 429/403/422 leaves no row — docs/43 §3), best effort. Demo tenant:
cannot invite, cannot be invited, cannot accept — all `403
demo_account_restricted`.

**Tenancy rule (the escalation tripwire):** every partner query expresses
ownership as `<table>.user_id == link.user_id` reached from
`SharingLink.partner_user_id == current_user.id` — a JOIN path, never a
column-level workaround. A SQL-sweep test asserts both predicates on every
statement a partner request issues; a static scan asserts no pre-existing
route imports the link table at all. If a query ever reads cross-tenant rows
without the join, that is an escalation to stop for, not a design choice.

**Deps:** M79 (consent/revoke semantics, docs/39), M80 (`mcp_audit` pattern,
docs/40), M74/M87 (session + enumeration-oracle rules, docs/38/docs/42), M50
(demo guard, docs/20), docs/33 (locked shape), docs/11 (privacy mode). No
other milestone.
**Acceptance:** docs/43 complete per §9 with explicit PM approval comment
before code; the full E2E walkthrough (invite → accept → scoped projection →
scope change → revoke → partner's next data read is `401
sharing_not_linked` while their own session still works, and
`GET /partner/state` flips to `200 {linked: false}` (M92); E2EE / privacy-mode / demo-tenant cases each
stating exactly what the partner sees; the tenancy invariant tests above; the
§4 gates green with `docs/02` + `docs/08` + `docs/09` + `docs/11` reconciled
in the same commit as the code.
**Smoke test:** through `http://localhost:8080` — two browser profiles: owner
shares account A only, partner sees A and no trace of B; owner empties the
scope → partner's next refresh is the empty projection with no logout; revoke
→ partner renders "No shared household" and their own `GET /users/me` still
answers 200; `GET /sharing/audit` lists the whole lifecycle with actor ids.

**Kilter adoption wave (t_09492311) — verified, not rebuilt.** The Kilter
card ("second credential per household, own login, roles full vs view-only,
audit who changed what") is satisfied by M90 + its follow-ups (M92/M93/M98/
M106/M107/M159/M179): the partner's own account and session, owner=full /
partner=view-only (docs/43 §1), per-account shared scope with personal login
data out of every payload, and `sharing_audit` receipts on the `mcp_audit`
shape. The card's two untested-by-name ACs gained pins in
`backend/tests/test_partner_role_enforcement.py` — 15 owner-seat/domain
mutation attempts from a view-only partner all refused with the owner's data
byte-identical (`test_view_only_partner_is_refused_by_every_mutation_surface`),
and two complete households serving each other nothing
(`test_two_households_never_leak_into_each_other`) — both rows documented in
docs/43 §Verification 2. A full-access invitee (write-sharing) stays out of
scope per docs/43 §12.

## M95 — QoL batch 1: ⌘K palette, empty-state choreography, 5-second undo

Wave 8's three user-facing quality-of-life wins, **one commit per workstream**
(P1/P2/P3) so each rolls back on its own. Non-spec affordances: no new
product surface was specified before them, but every one is documented in
`docs/09` in the same commit it ships (§9 of AGENTS.md).

### P1 — global command palette (⌘K / Ctrl+K)

One palette wired to **existing routes only** — the route table is its data
source, so it adds no endpoint and issues no request. Registry in priority
order: six pages (Transactions, Budgets, Reports, Goals, Settings, Help), three
actions (Import a statement → `/imports`; Take the tour → the M89 tour's
resurface event; Mint an MCP token → `/help/agent-integration`), the seven
report tabs (`/reports?tab=<id>` — a **new query-param contract on an existing
route**: read on mount, followed in-page, written back with `replace` on every
tab click), and every Help guide (`/help/:slug`). Empty query shows the first
eight entries verbatim; matching is plain case-insensitive
prefix → word-prefix → substring → keyword, **no fuzzy dependency**; the
no-results copy is the honest `No match — Try Import or Help`.

Two decisions worth their lines:
- **The palette joins the shell's single keydown listener** (M72 F1's router)
  instead of adding a second one, and is the top-most Escape layer
  (palette → inbox → search → drawer). `⌘K` / `Ctrl+K` is handled in that same
  listener, so `tests/m72Mobile.test.tsx`'s "exactly one listener" pin keeps
  holding; `tests/m95CommandPalette.test.tsx` adds the no-new-listener pin
  across open/close cycles.
- **It ships as its own chunk**, lazy-mounted only while open: the registry
  carries the whole guide-copy table, and the entry chunk keeps its ≤ 700,000 B
  ceiling (docs/09 §Bundle facts).

The card's "Mint an MCP token → settings section anchor" was verified against
the tree and does not exist (minting is API-only, docs/40; Settings has no
token surface), so the action routes to the guide Help's third track already
links to — recorded here rather than an anchor to nowhere.

**Deps:** M49 (route chunks), M72 (shell keydown router, drawer, touch floor),
M89 (tour event, help guides, screenshots). No milestone's backend.

**Acceptance (P1):** `tests/m95CommandPalette.test.tsx` green (16 tests —
registry/top-8/no-fuzz, open → type → Enter → route → close → focus back,
arrow navigation, report-tab jump, tour event, no-results copy, ⌘K toggle,
Escape-before-drawer ordering, no new keydown listener, mobile trigger at the
44px floor, rail row when labels show, zero console errors on every
invocation, **axe-core zero violations on the open palette**) + the `?tab=` pins
in `tests/m82ReportsTabStrip.test.tsx`; full frontend gate (tsc, eslint
`--max-warnings 0`, `NODE_ENV=test vitest run`) green; axe 0 on the palette
surface; docs/09 §Command palette + §Escape routing reconciled in the same
commit.

**Live-axe finding, fixed after P1 shipped (recorded, not hidden).** The
card's "axe clean" acceptance was asserted in prose but P1's own test file
never ran axe. The first live scan of the OPEN palette against the production
build reported `nested-interactive` on **all 8 default rows** (impact
serious): each `role="option"` wrapped its label in a real `<button>`. The fix
moves the pointer handlers onto the `<li>` itself (docs/09 §Command palette →
"Dialog contract"), adds the axe pin so it cannot return, and the re-scan
reports zero violations at every impact level.

**Smoke test:** on the deployed stack — `Ctrl+K` in the rail's collapsed and
expanded states and the mobile header magnifier at 390, type `bud`, Enter
lands on `/budgets`, focus returns to the trigger, `Esc` behind an open drawer
closes only the palette first; `/reports?tab=dividends` opens on Dividends.

### P2 — empty-state choreography (all pages)

`EmptyState` now carries a **type-enforced three-part contract** — heading +
real product screenshot + the one action that fills the surface — and the
compiler was the sweep: `tsc --noEmit` went from 0 to **47 errors** the moment
the props became required, which is exactly the call-site list. Two slices
upgraded the 47 sites across 33 files (pages and components, disjoint), the
integration pass fixed what a route-relative reading gets wrong, and
`tests/m95EmptyStates.test.tsx` pins the result.

Decisions worth keeping:
- **`preview` is a discriminated union**: `{src, alt}` OR `{none: "<reason>"}`.
  Eight sites are documented exceptions (Tags, Reconciliation ×2,
  SharedHousehold, HelpGuide-not-found, OidcLogin status, SuggestionQueue,
  SharingSection) because the demo tenant cannot produce a populated capture of
  them — the test pins that set exactly, so a ninth cannot appear silently.
- **Seven new captures, all real**: `goals`, `bills`, `debts`, `investments`,
  `security-detail`, `currencies`, `settings-accounts` were shot at 1600×913
  from THIS branch's own production build against the demo tenant (local
  `vite preview` + `/api` → the deployed stack), after the public front
  (.100:443) started refusing connections mid-card — the LAN path served the
  same stack, so the captures carry the M95 sidebar and no tour overlay. The
  existing seven M89 captures cover the rest; nothing was generated or drawn.
- **Actions are never route no-ops**: where the action's route IS the current
  page, it acts in place (Investments scrolls/focuses `#trade-form`, focuses
  the watchlist picker, or clears the trades log's filters; SecurityDetail's
  two price panels call the page's single shared `refreshSecurity`). Where copy
  was factually stale it moved with the fix: BudgetCards no longer claims
  budgets live in Settings, the benchmark panel no longer invites a range
  selection it does not offer, and the security page no longer promises a
  manual price editor that does not exist anywhere in the SPA.
- **No fake loading**: sweeping every `enabled:` query found one real defect —
  Investments' dividend-suggestions card skeleton-ed forever for a tenant with
  no investment accounts; it now hides the way it already did for an empty
  result set.

**Deps:** M89 (guide capture pool + sweep rule), M58 (axe/keyboard contracts),
M92 (unique accessible names). No milestone's backend — zero API changes.

**Acceptance (P2):** `tests/m95EmptyStates.test.tsx` green (6 tests — 47/47
sites carry both props, the eight exceptions are exactly the documented set,
every referenced capture exists on disk, Budgets-before-setup renders all three
testids with honest alt text and an action that opens the create form, a
deep-link action renders a working `<a href>`, zero console errors); full
frontend gate green; docs/09 §Empty-state choreography written in the same
commit; mobile 390/412 checks (h-scroll, overlap, console) run with the batched
M95 live verification.

**Smoke test:** on an empty tenant the skeletons are gone where they could
never resolve, every empty page shows heading → screenshot → working CTA, and
the CTA performs the documented action.

### P3 — the 5-second undo on destructive deletes

Three surfaces deleted forever behind a `window.confirm` — a transaction row
(Transactions and AccountDetail), an account (Settings → Accounts), an import
batch revert (Imports → Batch history). All three now run the **two-stage
contract** (docs/09 §Destructive actions): stage 1 removes the row from the UI
and offers `Undo (5s) / Dismiss` in a polite snackbar **without sending
anything**; Undo restores it exactly (order and count, still no request);
window expiry or Dismiss fires the real request once, and success refetches so
server truth replaces the optimistic state — a failure restores the row and
surfaces the envelope `detail` verbatim. The seam is one helper
(`components/ui/deferDelete.ts`) plus `toast.undo(...)` on the existing
`ToastProvider`, so all three surfaces share one exactly-once timer.

Decisions worth keeping:
- **The account delete opens a blast-radius dialog first** — live counts for
  the seven tables `DELETE /accounts/{id}` pre-checks (transactions, trades,
  tax lots, import batches, debts, goals, reconciliation items), disabled while
  anything is attached with the server's 409 refusal quoted verbatim. **Budgets
  are absent on purpose**: they hang off categories, never off an account, so
  the card's "N transactions, N lots, M budgets" shorthand was verified and
  corrected rather than transcribed. Unverifiable counts (`?`) never block the
  user — the server still decides.
- **Dismiss means acknowledge**, not abort: it closes the snackbar and commits
  immediately rather than leaving a hidden timer the user cannot see.
- **Counts come from the existing list endpoints** — `listImports` gained the
  `account_id` paging parameter on the client only (the server accepted it
  since docs/03); debts and goals are filtered client-side; zero backend
  changes in this workstream.
- **Sibling fix, same class:** the blast-radius dialog's first live check
  exposed that BOTH `AccountsSection` dialogs wrapped `createPortal(...)` in
  `<AnimatePresence>`, and framer drops portal children
  (`isValidElement(portal) === false`) — Settings → Accounts → New account did
  nothing in the running stack. The wrappers are removed, the rule is pinned in
  docs/09 §Keyboard contract, and a regression test covers the editor dialog.
- **Out of scope, reported:** Imports' *institution* delete still uses
  `window.confirm` — a separate blast radius this card did not enumerate.

**Deps:** M48 (invalidateLedgerData), M58 (dialogA11y, polite live regions),
M64/M72 (touch-target floor), M43 (Toast). No milestone's backend.

**Acceptance (P3):** `tests/m95UndoDelete.test.tsx` green (11 tests — per
surface: nothing sent while the window is open, Undo restores order + count,
expiry commits once and the refetch reflects it; the 409 path restores and
quotes the detail verbatim; `UNDO_WINDOW_MS === 5000`, polite live region,
focusable native Undo button, countdown ≤ 5; the seven-row blast radius with
its blocked copy and disabled button; the editor-dialog regression); full
frontend gate green; docs/09 §Destructive actions written in the same commit.

**Smoke test:** on the running stack — delete a transaction row, watch the
snackbar count down, press Undo and see the row return with no DELETE in the
network log; delete it for real and see the refetched list stay empty; on
Settings → Accounts the New account dialog opens (sibling fix) and the trash
button's dialog lists the account's real dependents.

## M97 — QoL batch 3: honest aggregate tooltips + dashboard layout presets

Two small quality-of-life changes, both about a number the user cannot currently
explain to themselves. Shipped as two commits (E1 then E2), each with its docs
reconciled in the same commit.

### E1 — "why is this number what it is" tooltips

Every non-self-evident aggregate carries its honest DERIVATION on hover, focus
or tap — not a definition. The primitive is the shared `HelpHint`
(docs/09 §Tooltips & in-app help), rebuilt in place rather than replaced: it
was a native `title=` (hover-only, undismissable, clipped by any scrolling
container), it is now a portalled bubble whose `aria-describedby` is wired at
all times — the copy sits `sr-only` while closed, so a screen reader reads it
from the focused trigger with no hover anywhere in the loop — shown on focus,
pointer hover and tap, dismissed with Escape, viewport-clamped so a long
derivation cannot overflow 390px. **No dependency was added.**

Surfaces: **net worth** (`Assets − Liabilities, as of <date>` with BOTH figures
inline, backed by the new `assets_cad` / `liabilities_cad` split of the same
signed sum in both engines — docs/12 §4, `net_cad` unchanged — plus what is
excluded and why), Range net (labelled "Month net" when M97 shipped; renamed
in M138 because the value sums the whole range) / Positions value / Cash, the four
spending-insight aggregates (including the honest shape of "vs last month":
this month **to date** against **all** of last month), `pct_used` on all three
budget surfaces (M65 true ratio), and TWR / MWR / net flows / investment income
(docs/05 §Total return).

**Sweep rule (docs/09):** no tooltip where the derivation is literally on the
same screen — the net-worth trend chart draws cash + positions stacked to net,
the spending donut prints its own total, and every `% used` card prints spent /
limit directly above its bar (its hint exists only for the clamp the bar hides).

### E2 — dashboard layout presets

Three FIXED orders over the same six cards — `overview` (first visit, today's
order), `import_first`, `budget_first` — picked from a `SegmentedControl` in
the dashboard header and persisted on `users.dashboard_preset` (ONE column,
migration `0036_dashboard_preset`, because docs/02's preference precedent is
columns-on-the-row: no new table, no JSON blob, and the value is an enum with
a CHECK). A preset stores an ORDER only: it never creates, removes, hides or
configures a card. The full contract — orders, the DOM-order-is-visual-order
rule, the 390 behaviour, the coach-mark invariant, and the recorded deviation
on Import-first's naming — is **docs/09 §Dashboard layout presets**; the code
side is `CARD_ORDER` in `frontend/src/pages/Dashboard.tsx`.

**Rejected scope — the whole "dashboard flexibility" budget for M97:**

- **Drag-and-drop / free-form layout editor.** A reorderable canvas is a widget
  framework (drag handles, arbitrary-order persistence, collision rules, undo)
  with no honest default for a first-time user — and the tour would have to be
  re-taught against every possible arrangement.
- **Widget hiding / minimisation.** It removes information; M97's rule is that
  every preset keeps ALL cards accessible in content or one scroll away.
- **Custom card sets / user-created cards.** New cards are a content problem,
  not a layout one (and explicitly out of scope on the card).
- **Per-card sizing and per-preset card sets** (two orders that differ in WHICH
  cards exist, rather than only their order).

**Deps:** M89 G1 (coach marks target surfaces, docs/09 §Guided first tour), M65
(honest budget %), M77 (TWR/MWR, docs/05 §Total return), docs/33 S2b (the
client net-worth engine the split mirrors), docs/02 (preference columns), M50
(the demo write guard that makes the preset picker roll back on the shared
tenant).

**Acceptance:** two commits (E1, E2); AGENTS §4 gates green; docs/09 + docs/02
+ docs/08 + docs/12 reconciled in the commit that changes them; push → CI →
deploy → verified on production (tooltips visible in both themes on the real
aggregates, preset switch persists across a reload on the live site); QA
re-verifies bundles when both land.

**Tests:** `helpHint.test.ts` (focus, Escape, tap at 390, touch-vs-pointer
discrimination, gap contract, contrast-token pin, axe-core 0 violations closed
AND open) · `m97Tooltips.test.tsx` (every surface renders its copy on focus,
Escape dismisses) · `m97DashboardPresets.test.tsx` (all six cards in every
order, server round-trip across a reload, rollback on a refused write, 390 DOM
order, axe 0 under a non-default preset) · `m68Navigation.test.tsx` (Tour.tsx's
own mark list resolves, target+fallback, under all three presets) ·
`test_dashboard_preset.py` (round-trip, unknown value → 422, per-user) ·
`test_reports_networth.py::test_net_worth_assets_liabilities_split`
(hand-computed 23488.00 / 750.00 → 22738.00) · the S2b golden re-captured by
`backend/scripts/capture_s2b_networth.py --seed`, replaying the fixture's
frozen inputs and asserting every pre-existing bucket field byte-identical
before the two new ones were written.


## M100 — Collapsed-sidebar Accounts → a real `/accounts` page

**The bug (Mark, on prod).** Clicking **ACCOUNTS** in the collapsed rail
navigated to the Settings page's **Security** card instead of an accounts
surface.

**Root cause — never-worked, not a regression.** The collapsed-only control
was `<NavLink to="/settings" title="N accounts">` from M21 (b7d03a7, 2026-08-26)
onward, and the sentence *"Collapsed sidebar: wallet icon link to settings"* was
pinned verbatim in **docs/09 §Sidebar** and **docs/14 C7** — a spec-legacy
mistake enshrined as a design decision, not a regression from M78 (drawer,
215fe19) or M95 (palette, d7f0596), neither of which touched that line
(`git log -L` shows only b7d03a7).

**The ruling (Option A): build the destination.** A control labelled
"Accounts" needs a route that IS accounts — a settings-anchor keeps accounts
buried in Settings forever and "expand the rail instead" makes the resulting
route test a non-answer. Hence:

- **`/accounts` index** (`pages/Accounts.tsx`, lazy, M49 pattern): the
  sidebar's OWN grouping via the shared `lib/accountGroups.ts::TYPE_GROUPS`,
  one row per account (name · type label · `total_value` in the account's own
  currency, **no cross-currency sum anywhere** so no FX rate is fetched) →
  `/accounts/:accountId`; header action `Add account` → `/settings` where the
  create form lives; three-part empty state over a real capture of the
  populated page (`/help/accounts.png`, taken from this branch's production
  build against the demo tenant at 1600×913).
- **Wire-up:** collapsed wallet → `/accounts` (the fix), expanded `Accounts`
  heading → `<NavLink to="/accounts" end>` (renders even with zero accounts;
  `end` keeps its active pill off `/accounts/:id`, where the per-account row
  owns the highlight), ⌘K gains `page-accounts`, `/settings` unchanged.
- **Rail-wide a11y cleanup the audit turned up:** every collapsed control now
  carries `aria-label` == the expanded row's visible label (the old native
  `title` was hover-only and invisible to keyboard), a `min-h-11` 44px floor,
  its existing focus ring, and a **`RailTip`** label bubble (docs/09 §Rail
  label bubble — portalled out of the rail's overflow column, shown on focus
  and hover, Escape closes only the bubble, `enabled={false}` when expanded so
  the labelled rail is untouched).
- **Docs reconciled in the same commit:** docs/09 (pages table, routes, sidebar
  section, §Tooltips, §Empty-state preview sources + site count 47→48,
  §Touch-target floor, §Command palette), docs/14 (C2 and C7 marked
  **SUPERSEDED** with pointers, new **C10** spec), docs/10 (this entry).

**Deps:** M49 (lazy route pattern), M95 P2 (empty-state contract), M95 P1
(palette registry), M58 (axe bar + the 44px floor precedent), docs/14 C2 (the
accounts manager the empty-state action points at).

**Acceptance:** AGENTS §4 gates green; push → CI → deploy (example.internal `web`) →
verified on production: collapsed **Accounts** click → `/accounts`, expanded
heading → `/accounts`, both themes, and at 412 width; the standing QA sweep
picks the surface up next card.

**Tests:** `m100AccountsNav.test.tsx` (16: the regression test
`collapsed_accounts_icon_goes_to_accounts_page`, the full rail audit — every
collapsed and expanded link's `href` and every click's resulting route, in BOTH
themes — `aria-label` == expanded label, tab order + activation, the label
bubble, axe-core 0 violations with a bubble open, the page's list/empty/error
states with the capture checked on disk, ⌘K navigation, the phone drawer) ·
`m68Navigation.test.tsx` (the rail's href list ends `/accounts`) ·
`m95CommandPalette.test.tsx` (registry route + default-8 order) ·
`m95EmptyStates.test.tsx` (48 call sites, every referenced capture on disk) ·
`routeCodeSplit.test.tsx` (the new page is lazy-wired).

## M101 — Android Firefox scroll jank: `backdrop-filter` off below md

**Report (Mark, prod):** *"really slow to scroll"* on the phone — his daily
surface. Companion to M100 (same shell file, rebased before push); scope was
CSS/scroll-path only.

**Measured cause, one suspect at a time** (`frontend/scripts/perf/scrollbench.py`,
full method + budget in docs/34 §Scroll-frame budget): a scripted
top→bottom→top sweep at 1400 px/s on a mobile viewport @ DPR 2.6, Firefox
153.0esr, median of 3 runs. **The blur is the cost, not the translucency:**

- `.glass`'s `backdrop-filter` removed (alpha left byte-identical): `/transactions`
  26 → **2** long frames; `.glass` made opaque with the blur kept: **25** (no
  change); fixed header + drawer blur removed: **26** (no change).
- On `/dashboard` each of those two groups was *independently* enough to hold
  the page at 26 long frames — only killing every `backdrop-filter` below md
  reaches 0. Hence one blanket, unlayered `@media (max-width: 767.98px)` block
  instead of a per-surface patchwork.
- Chromium 154 drops no frames either way, but its compositor draw wall-time
  per sweep fell **4025 → 274 ms (−93%)** — same work, seen as time.

**Shipped:**

- `src/index.css`: unlayered, `!important` `backdrop-filter: none` for `*` at
  `≤767.98px` (a future `backdrop-blur-*` utility cannot silently reintroduce
  it), plus `isolation: isolate` on `.glass` / `.glass-strong` so the stacking
  context their filter used to open survives the removal (docs/09 §Sidebar
  stacking context). Every token background/border is untouched — **alpha
  measured free**, so the contrast-audit pairs are unchanged.
- `AppShell.tsx`: the fixed mobile header (`md:hidden`) goes
  `bg-surface-raised/80` → **opaque** `bg-surface-raised` — with no blur behind
  it, 80% would ghost scrolling rows through the header.
- Desktop keeps its glass: the kill rule lives inside the `max-width` block and
  the header only renders below `md`, so `≥768px` is byte-identical (screenshot
  A/B: desktop before/after differences sit *under* the same-build capture
  noise floor; mobile before == mobile with the blur re-injected, byte-for-byte).

**Result (long frames >50 ms per sweep, fps):**

| page | before | after |
| --- | --- | --- |
| `/transactions` | 26 (43.4 fps) | **0 (59.1 fps)** |
| `/dashboard` | 27 (25.1 fps) | **0 (59.1 fps)** |
| `/investments` | 35 (22.4 fps) | **0 (59.0 fps)** |

**Measured, deliberately not shipped:** desktop `/dashboard` still loses 7–10
of ~120 frames per sweep with glass on this GPU-less host. Recorded in docs/34
so the choice is visible; widening the rule to all widths is one line.

**Docs reconciled:** docs/09 (new §Scroll performance with the advisory line
*"glass blur disabled at ≤768px viewports for scroll perf, kept on desktop"*,
§Sidebar stacking context + §Glass tokens updated), docs/34 (§Scroll-frame
budget — the baseline table future waves compare against), docs/10 (this
entry).

**Deps:** M82's `< 768px` breakpoint precedent, docs/09 §Contrast audit (why
alphas could not move), M49's lazy-route build (unchanged).

**Acceptance:** AGENTS §4 gates green; push → CI → deploy → verified on prod;
A/B evidence (Chromium + Firefox traces, before/after screenshots) attached to
the card.

**Tests:** `m101ScrollPerf.test.ts` (5: the kill rule exists for both
spellings and uses `*`; it is unlayered; every `backdrop-filter: none` sits
inside a `max-width: 767.98px` block while `.glass` still declares
`blur(14px)` + `background: var(--glass-bg)`; `.glass` keeps
`isolation: isolate`; the fixed header is opaque with no `backdrop-blur`).

## M102 — Scroll regions: every `overflow-x-auto` box is keyboard-reachable

**Report (child card of M101).** M101's mandated mobile a11y sweep — axe-core at
390 and 412, both themes, 6 pages — reported **8 of the 24 page×theme×width runs**
failing `scrollable-region-focusable` (*"Scrollable region must have keyboard
access"*, serious): 4 runs at 390 + 4 at 412, 12 violating nodes (1 on
`/investments`, 2 on `/taxes` per combo), 0 at 1440, byte-identical before →
after M101's CSS work. Pre-existing, out of M101's scope, routed here.

**Cause.** Three bare `<div className="… overflow-x-auto">` wrappers around wide
tables inside their card — *Trades log* (`/investments`), *Capital gains {year}*
and *ACB per holding (non-registered)* (`/taxes`). axe only flags a box whose
content overflows *right now*, which is exactly why 1440 (where the columns fit)
reports nothing: the boxes were neither focusable nor named, so a keyboard user
could not scroll them (WCAG 2.1.1).

**Shipped — 26 sites, one pinned shape:**

- every horizontal scroll box carries `role="region"` + `aria-label` +
  `tabIndex={0}` inline (docs/09 §Scroll regions pins the shape and the
  label-uniqueness rule). The two sites whose scroll box *is* the card
  (`DividendsPanel`, `ProjectionsPanel`) put the same three attributes on the
  `Card`; `SegmentedControl` keeps its `role="tablist"`/`aria-label` and gains
  `tabIndex={0}`.
- `frontend/tests/m102ScrollRegions.test.tsx` (4) parses every `.ts`/`.tsx` in
  `src/` with the TypeScript compiler: any `overflow-x-auto` literal that is not
  the `className` of a focusable + named JSX element (or a `<TableScroller>`)
  fails, including a class hidden behind a variable. Mutation-checked — dropping
  `tabIndex`/`aria-label` from one site fails the run.
- `scripts/a11y/axe_scan.py` gains `--width/--height`, enforced through
  `Emulation.setDeviceMetricsOverride` with the real `window.innerWidth` recorded
  in the report. **`--window-size` is not the viewport:** headless Chrome clamps
  a window below **500 CSS px**, so `--width 390` used to lay the page out at
  500 and never exercise the narrow-width rules at all.

**Result (axe-core 4.13, 6 pages × 2 themes, production build + prod API, demo
tenant):**

| viewport | before | after |
| --- | --- | --- |
| **390×915** | 4 serious runs / 6 nodes | **0 critical / 0 serious / 0 moderate / 0 minor** |
| **412×915** | 4 serious runs / 6 nodes | **0 / 0 / 0 / 0** |
| 1440×900 | 0 | **0 / 0 / 0 / 0** |

**8 failing runs → 0** (and 12 violating nodes → 0), console errors 0 in every
run. Keyboard proof at 390×915: each of the three boxes takes focus
(`tabIndex 0`) and `ArrowRight`×5 pans it `0 → 200 / 200 / 122` px of a
`412 / 253 / 122` px overflow, and Tab alone reaches them (154 / 31 / 31
presses).

**Channel C re-run (`scripts/a11y/keywalk.py`, same harness, before vs after
build):** every M58 keyboard contract check is unchanged — skip link first on
all six pages, **0** focus stops without an indicator, **0** keyboard traps, 0
running animations under `prefers-reduced-motion`. Focus stops +5 on
`/investments` and +3 on `/taxes` (the new regions; the denials region only
renders when there are denials), 0 on budgets/bills, +2 on dashboard/transactions
where M102 added nothing (dynamic stops, same harness both runs). One
**pre-existing** failure carries over byte-identically from the before run:
`/investments`' `Add a symbol` button reports `dialog=no-effect` — not this
card's; routed to **M103**, which settled it as a false report (the harness
could not see a focus move; the button was working).

**Docs reconciled:** docs/09 (new §Scroll regions + the channel-A command line
for `--width`), docs/10 (this entry).

**Deps:** M64 `TableScroller` (the tier this deliberately does not replace), M58's
axe channel A and the `*:focus-visible` baseline, M101's sweep that found the
defect.

**Acceptance:** axe 0 critical + 0 serious at 390 and 412 in both themes with
desktop still 0; one regression test over every `overflow-x-auto` in `src/`;
AGENTS §4 gates green.

**Tests:** `m102ScrollRegions.test.tsx` (4: whole-tree scan, no unreachable box,
no unattributable literal, `Card` forwards the region attributes).

## M103 — channel C: an "observable effect" the dialog probe could not see

**Report (follow-up card opened by M102).** M102's channel-C re-run reported:

```
FAIL investments: dialog Escape/focus contract — {'trigger': 'Add a symbol', 'result': 'no-effect'}
```

Identical against the pre-M102 build, so it was pre-existing and out of M102's
scope. The card's open question: is the control inert, or does it do something
the probe's heuristic cannot see?

**Verdict — not inert; the probe's definition of "observable" was too
narrow.** The trigger is `EmptyState`'s action on the watchlist card
(`src/pages/Investments.tsx`), added by **M95 P2** (`771eb4a`, which replaced
Investments' route no-ops with "scrolls/focuses … the watchlist picker"). Its
handler does `document.querySelector('input[placeholder="Add a symbol to
follow…"]')?.focus()`, and that input's own `onFocus` opens the watchlist's
security combobox — a `role="listbox"` of options. That is also why the
committed `keywalk.json` from 2026-09-29 recorded `no-dialog-on-page`: before
M95 P2 the only Add-ish control on the page was the form's submit button,
which is `disabled` until a security is picked, so the trigger selector skipped
it.

**Evidence — `frontend/scripts/a11y/m103_click_probe.py` →
`m103-click-probe.json`, 390×915 and 1440×900**, each run against a page first
proven at rest (a 7 s window with **no** click moves 0 elements, 0 visible
controls, 0 focus):

| click → | 390×915 | 1440×900 |
| --- | --- | --- |
| `document.activeElement` | `body` → `input[role=combobox][placeholder="Add a symbol to follow…"]` | same |
| that input's `aria-expanded` | `false` → `true` | same |
| `[role="listbox"]` / `[role="option"]` | 0 → 1 / 0 → 7 | same |
| elements / `innerHTML` | +29 / +2 364 B | same |
| visible `input`,`select`,`textarea` — **what the old check counted** | **+0** | **+0** |
| dialog / URL / live region | none | none |

So every one of `dialog_probe`'s four checks was blind to it: no
`role="dialog"`, no navigation, no *new form control*, no live-region text →
`no-effect`.

**Shipped — `keywalk.py`'s `dialog_probe` classifies before it condemns:**

navigation → inline form → live region → **focus moved** → **elements added** →
only then `no-effect`. Focus is compared by *identity*
(`window.__keywalkFocusBefore`, set immediately before the click, because
`HTMLElement.click()` never focuses what it dispatches on, and two controls can
share a label); both new checks read the DOM 0.25 s after the click — before
the 6 s dialog deadline — so data still arriving cannot be mistaken for the
control's own effect, and `no-effect` stays reachable for a genuinely dead
control. The negative control in the committed JSON proves the second half: an
injected button with no handler, prepended ahead of the app's own controls, is
run through the **shipped** `dialog_probe` and still returns `no-effect`.

After the change the same harness run (`keywalk.json`, 2026-10-05, rebuilt
frontend) reports `/investments` as
`non-dialog-surface — focus moved to <input> Add a symbol to follow…` with
**`failures=0`** on all six pages (skip link first everywhere, 0 stops without
an indicator, 0 traps, 0 running animations under reduced motion, 0 console
errors); every other page's verdict is unchanged in kind.

**Docs reconciled:** docs/09 (§Audit method channel C names the six-way
classification; §Verification carries the new harness bullet, the 2026-10-05
walkthrough table and the evidence files), docs/10 (this entry; M102's
"follow-up card" pointer now names it).

**Deps:** M58/M58b (channel C and its dialog contract), M95 P2 (the control
that surfaced the gap), M102 (the re-run that reported it).

**Acceptance:** determination with browser evidence at 390 and 1440 (settled:
not inert); `python3 scripts/a11y/keywalk.py` → **`failures=0`** against a
rebuilt frontend with `keywalk.json` committed; AGENTS §4 gates.

**Tests:** `m103EmptyStateAction.test.tsx` (3 — the click moves focus,
flips `aria-expanded` and mounts the option list; an untouched control stays
closed; Escape closes it with focus left in the field). Mutation-verified:
commenting out the handler's `input?.focus()` fails 2 of the 3.


## M104 — Tag UX: quick-tag, keyboard-complete picker, multi-tag AND filter, tag management

**Requested by PM** on the board as *"M102: Tag UX"* — that number was already
taken by the scroll-regions milestone when the card was written (M102 and M103
both shipped first), so it lands here as **M104**.

**Context.** Adding a tag cost 4–5 interactions (open the editor → find the Tags
row → focus the picker → type → Enter) because the list rows offered no
affordance at all; the filter bar's single-tag `<select>` disagreed with rows
carrying several tags; the modal's Tags block was the last thing before the
fold; and a typo'd tag was unfixable — `PATCH /tags/{id}` into an existing name
answers 409 `duplicate_tag`, so "Goceries" and "Groceries" could coexist
forever with no merge anywhere.

**Shipped.** Row-level `+` popover (click → type → Enter = **2** interactions,
anchored + clamped + flipped so it stays on a 390px screen); the picker became a
real `combobox`/`listbox` with arrow-key navigation, `aria-activedescendant`,
session recency when the query is empty, `min-w-56`/`w-64`/`text-sm` sizing, and
a two-press Escape that closes its list before the editor modal; Tags promoted
above `Raw description` with the section label *"Tags — as many as you like;
category stays single"* and a `w-full` picker; the filter bar now carries
`TagFilterPicker` (chips, **"matches all selected"**, no inline-create) driving
`GET /transactions?tag_ids=` (repeated, AND); and `TagManagerModal` off the
picker's **"Manage tags…"** footer lists usage counts, renames, deletes only
unused tags, and merges a selection into one survivor through the new
**`POST /tags/merge`**.

**Backend:** `GET /tags` items gain `transaction_count` + `rule_count`
(`TagWithUsage`, embedded `TransactionPublic.tags` unchanged), `POST /tags/merge`
(re-points `transaction_tags` / `rule_tags`, `ON CONFLICT DO NOTHING`, every id
checked before the first write), `GET /transactions?tag_ids=` (AND). All three
documented in docs/08; the UI half in docs/09 §Tag UX.

**Acceptance (measured against the deployed stack, 2026-10-05):**
add-one-tag-from-list = **2 interactions** (click `+` → type → Enter;
`m104-tag-probe-{1440,412}.json` 19/19 at each width) ✓; Tags section above the
raw description ✓ (`m104TagUx.test.tsx` DOM order + the modal's `w-full` picker);
multi-tag AND filter — the list query carries `tag_ids=` twice and only the row
holding both survives ✓; rename endpoint already existed (`PATCH /tags/{id}`),
**merge added** because rename cannot resolve a duplicated typo (409
`duplicate_tag`); docs/08 + docs/09 reconciled in the same commit ✓; AGENTS §4
gates green (ruff, **pytest 1183**, tsc, eslint 0, **vitest 875**, build, CSP);
axe channel A **36/36 runs with 0 violations of any severity** at 1440/390/412 ×
2 themes, axe on the three OPEN tag surfaces 0 critical/serious, keywalk channel
C **`failures=0`** (434 stops on `/transactions`, `STOPS` backstop 400 → 600),
0 console/CSP errors, prod verified at 1440 desktop and 412 mobile — on the
public origin itself (`m104-tag-probe-prod-{1440,412}.json`: 19/19 each, plus
`m104-axe-prod-1440.json`: 12/12 runs 0 violations of any severity in both
themes).

**Tests:** `backend/tests/test_tags.py` (10 — 5 pre-existing + counts, merge ×3,
`tag_ids` AND), `frontend/tests/m104TagUx.test.tsx` (14 — listbox keyboard,
Escape nesting, section order, quick-tag interaction count, unique trigger
names, filter chips → `tagIds`, management modal counts/delete-gate/rename/
merge). Two `outline-none` exemptions added to `focusRing.test.ts` for the
popover and manager surfaces (`tabIndex=-1` `useDialogFocus` targets, the same
category as `SimilarExamples`).


## M105 — TWR/MWR display: the 8-decimal wire string trimmed to 2 dp (display-only)

**Requested by Mark** on the board as *"M103: TWR/MWR percent display"* — that
number was already taken by the channel-C follow-up when the card was written,
so it lands here as **M105**. Screenshot `img_b3a480ee6dd1.png` (the
Investments/Reports period-return card) printed `+21.38091492%` and
`+20.65817326%`: correct numbers, unreadable presentation.

> **The wire contract is untouched.** `ratio_pct` still emits a fixed 8-decimal
plain string — and the `lib/totalReturnEngine.ts` byte-for-byte mirror and its
`m77Parity.test.ts` harness retired with M160 (P2-5), so the golden fixture
and the backend tests (unchanged) are now the only witnesses.
Only the DISPLAY changed, and only in
`components/investments/TotalReturnMetrics.tsx`:

- **`formatPct` SLICES to a 2-decimal reading** (`+21.38%`) — a `String.slice`
  on an exactly-`[-]digits.8dp` match, never `Number().toFixed()`, so this is a
  trim and not a re-round: no digit on screen is invented out of the wire
  value. Any other shape (fewer/more decimals, scientific notation) fail-opens
  to the previous rendering rather than being mis-parsed.
- **Tone still comes from the RAW string.** An exact `0.00000000` is the only
  muted zero — `0.00000123%` renders `+0.00%` in gain green instead of
  disappearing into the muted style, and a negative keeps danger.
- **The trim is recoverable, not lossy.** Each value now carries a `HelpHint`
  beside it — `21.38091492% precise` (hover on desktop, tap on mobile, focus
  for keyboards, and in the tree via `aria-describedby` even when closed) — and
  the span's `aria-label` is the full string, so assistive tech reads the real
  number while the eye reads the trimmed one. The affordance renders only when
  the display actually hides digits; an untrimmed or non-percentage value
  shows no hint.

**Docs reconciled:** docs/05 §Total return (new **Display** paragraph pinning
slice-not-re-round, raw-string tone, the precision affordance and fail-open),
docs/09 §Tooltips (the `Period return` row now names the value's own precision
hint), docs/10 (this entry).

**Deps:** M77 (the totals and their wire shape), M65 (the honesty rules this
preserves), M97 (the `HelpHint` primitive the precision affordance reuses).

**Scope sweep (the card's §3):** the 8-decimal shape exists nowhere else.
`ratio_pct` has exactly two call sites — `twr_pct` and `mwr_pct`, both on this
card — and every other percentage surface already rounds server-side
(`insights`/`spending_comparison` `round(…, 1)`, `securities.change_pct`
`round(…, 2)`) or formats client-side with `toFixed(2)` (`BenchmarkPanel`,
holdings `weight_pct`, `SecurityDetail` change). So nothing else needed the
same fix, and no engine's output was touched.

**Acceptance (measured against the deployed stack, 2026-10-05):** AGENTS §4
gates green — ruff `format --check` + `check` ✓ (346 files), **pytest 1183
passed** locally *and* in CI with **0 deselected**, `tsc --noEmit` ✓,
`eslint --max-warnings 0` ✓, **vitest 885 passed / 1 skipped across 85
files**, `npm run build` ✓; CI run `37347085127` green on all four jobs
(secret-scan, backend, web incl. CSP hash, status); example.internal `web` rebuilt at
`5ea0a3c` and the served bundle proved by content
(`/assets/Investments-CglvILET.js` carries the ` precise` marker). Verified on
**prod**, on the exact screenshot surface (`/investments` → `Period return`),
in **both themes**: `+21.38%` (aria-label `21.38091492%`) and `+20.65%`
(aria-label `20.65817326%`), both `text-gain`, six `HelpHint`s on the card of
which the two value ones read `21.38091492% precise` / `20.65817326% precise`;
a **real** CDP `Input.dispatchMouseEvent` over the value's trigger opens the
bubble (161×31 at 512,421, `role="tooltip"`, `aria-describedby` wired) in each
theme — screenshots `m105-prod-light.png` / `m105-prod-dark.png`. Channel A
against the public origin: `frontend/scripts/a11y/m105-axe-prod-1440.json` —
**12/12 runs (6 pages × 2 themes @1440×900), 0 violations of any severity, 0
console errors**, `A11Y_BASE=https://fintrac.princessandmark.com`.

**Tests:** `m105PctDisplay.test.tsx` (10 — slice to 2 dp with the sign kept,
trailing zeros, exact zero → `0.00%` muted, `null` → `No data`, five fail-open
shapes, tone from the raw string including the `0.00000123%` gain and a
negative zero; card render at Mark's two values with the aria-label + hint
text, the muted-zero card, the null card (4 triggers, no precision affordance),
and axe-core zero violations with the hint open) plus `m77Behavior.test.tsx`'s
metric render now asserting the trimmed display **and** pinning the full
string in `aria-label`.

## M106 — per-transaction review state + the append-only activity log

**Wave 11 opener 2** (Monarch-scan #2). Their users demanded receipts after an
11k-edits/day data-loss incident; `/transactions` gets both halves of the
answer: a **review queue** that says which rows still need a human, and a
**History drawer** that says what already happened to a row, in order,
forever. The rule both are built on: *edits never silently vanish; every
change is a receipt* (docs/02 §transaction_events).

**Review state** — two nullable columns on `transactions`, `reviewed_at` +
`reviewed_by` (no FK — deleting the reviewer's account must not blank the
record of who signed off). **NULL `reviewed_at` IS "unreviewed"**; there is
deliberately no companion boolean, because two columns that both mean
"reviewed" eventually disagree. That single rule makes an import seed a queue
for free: `stage_rows` writes NEITHER column, explicitly, so a future column
default cannot quietly pre-review a stranger's statement. The partial index
`ix_transactions_user_unreviewed` keeps the queue count an index probe.

**Receipts** — the new `transaction_events` table: `tx_id` (**no FK**, so
`deleted` survives its subject), `user_id`, `event_kind` (`created | edited |
categorized | tagged | reviewed | split | deleted` — `split` reserved for the
split feature — M107 has since landed and widened it with `merge`, docs/44 §11), `payload`, `payload_hash` (canonical `mcp_audit.args_hash`
sha256), `actor` (NULL = a cloud-service action, which is what a bank-sync
pull stages with), plus the house timestamps. Written **at the handler
layer**, in the SAME database transaction as the change, exactly once per
affected transaction — deliberately the opposite of `sharing_audit`'s
best-effort rule, because this log IS the user's history. Pre-handler
refusals (422/429/403) write nothing (M91-6). The demo seed writes none
either, so a freshly seeded demo opens an empty History.

**Deliberate boundary (reported, not hidden):** none of the mutation paths
this card names is silent. The four transfer routes (`accept-transfer-suggestion`,
`detect-transfer`, `link-transfer`, `unlink-transfer`) do not go through PATCH,
so each carries its own writer and receipts **every row it moves** — accept
writes two, an unlink of an already-unpaired row writes none; and the dividend
capture's ledger row gets `created` with `{"source": "trade"}`. **M107 has since
landed**: `split` (create/replace) and the new `merge` kind are written by the
four `/transactions/{id}/split` routes — see the M107 entry below.

**UI** (docs/09 §Review queue & activity log): a second column holding the row
toggle (`aria-pressed`, a per-row label, the 44px phone hit area, no modal);
a `Needs review` chip beside the quick-filters that applies immediately and
carries the count; an `N unreviewed` header hint dismissible for the SESSION;
and the History drawer, opened from the row's Actions cell or the edit
modal's footer — one dialog at a time, receipts newest first, the sentence
rendered server-side from the stored payload so the wording can change
without a migration.

**Counts stay honest by construction:** `GET /transactions/unreviewed-count`
is its own route (the list carries a 5-query budget as of M108 P4, docs/34) and shares
`get_txn_filters` / `_ledger_scope` with the list — one predicate, two
consumers, so the chip's number and the list it opens cannot drift. The
endpoint ignores its own `reviewed` param, so asking while the queue is ON
cannot cancel the count out.

**Exports / E2EE / the partner boundary:** registry-derived, so
`transaction_events` reaches `/export/full` (and therefore the encrypted
browser backup) with no new table list anywhere. The ZIP gets a visibility
filter — receipts of committed/manual rows **plus receipts whose row no
longer exists** (`tx_id` has no FK, so "gone" is asked against the table, not
against this tenant's rows — a receipt naming someone else's row is not a
surviving witness), because the `deleted` receipt is the one event whose
purpose is to outlive its subject. Two encryption questions, two answers
(docs/11 § *E2EE vault (M106)*): `payload` has **no cipher column**, so a
locked ZIP ships it as stored (listed in docs/33's plaintext table); the
backup artifact seals the WHOLE snapshot, receipt payloads included, pinned by
the restore round trip and by the browser-written interop fixture. `GET /partner/transactions/{id}/events` is M106's
sixth partner route: the row must be in the partner's scope, the receipt's
`actor` must be the owner or the partner (a bank-sync receipt with a NULL
actor is dropped), and the payload is stripped so no written value — money
included — walks around docs/43 §9's mask; field NAMES survive as
`changed_fields`.

**Docs reconciled:** docs/02 (the two columns + the new table + indexes),
docs/03 (`stage_rows`'s two additions), docs/08 (the `reviewed` filter and the
three new routes), docs/09 (a new §Review queue & activity log, the bulk
toolbar's two new actions, the retargeted `colSpan`), docs/11 (registry
coverage + the ZIP visibility row + the two-question E2EE answer), docs/33
(`transaction_events.payload` in *Server-readable plaintext TODAY*), docs/43
(the sixth partner route and its actor filter + its boundary test row),
docs/10 (this entry).

**Deps:** M96 (the multi-select toolbar bulk review drives), M90/M93 (the
projection seam the partner route sits on), M91-6 (the pre-handler ruling
this log inherits), M80 (`args_hash`'s formula).

**Tests:** `backend/tests/test_m106_review_events.py` (12) +
`frontend/tests/m106ReviewQueue.test.tsx` (18) — every mutation path writing
exactly one receipt, two hand-recomputed `payload_hash` digests (unicode body
included), the queue filter/count/flip liveness, import seeding, export + ZIP coverage,
the four transfer routes receipting each moved row, the partner projection
boundary, the 422/429 pre-handler refusals, migration `0039`'s up/down (a row
written below `0039` lands `reviewed_at IS NULL` — no backfill), the
toggle/chip/hint/drawer UI contracts, the drawer printing a unicode sentence
verbatim, one row's receipts never reaching another row's drawer, and both
list surfaces carrying the two History entry points.

**Acceptance (AGENTS §4, gate battery run by hand 2026-10-07 on the tree as
pushed):** `ruff format --check` + `ruff check` clean (357 files) · **pytest
1221 passed** (isolated `fintrack_m106final_test`, 10m34s — 1210 on this
milestone's own tree, before the rebase onto the sibling's `54624c5`) ·
`npx tsc --noEmit` · `npx eslint . --max-warnings 0` · **vitest 941 passed /
1 skipped across 89 files** (930/88 before that rebase) · `npm run build` +
`npm run check:csp` ✓ · secret scan clean.
**CI green:** run
[37547901600](https://github.com/margosmark/fin_tracker/actions/runs/37547901600)
on `a6c331e` — all four jobs (backend, web, secret-scan, status) — the run
covering this milestone's last code commit (`1efc4f6`, parent `4e5b1e7`);
M106's own commits after it are docs/evidence on that same tree. Runs opened
AT `7974453`, AT `4e5b1e7` and AT `c9fecc7` never finished: sibling pushes
superseded them under the workflow's own `cancel-in-progress` concurrency
(three cards push to one main and a backend job runs 7–11 min), so the run
that counts is the containing ref's — and any later run on the ref belongs to
whatever landed after this milestone.
**CI outage from 00:43 UTC 2026-10-07:** Actions now refuses every job in
this repo — each fails inside 2s with *"The job was not started because
recent account payments have failed or your spending limit needs to be
increased"* — so M106's last docs-only commits carry no Actions verdict and
the battery above was substituted by hand; the `status` job of the preceding
run (`37552032784`, whose backend/web/secret-scan jobs were all green) died
with zero steps recorded minutes earlier. Until billing is fixed, no card on
this repo can get a CI verdict.
**Deployed** to example.internal
(`docker compose up -d --build api mcp web site`, containers built at main
`1efc4f6`, api healthy; the checkouts were then ff'd forward as docs-only
commits landed — no rebuild needed for those): alembic head
`0040_budget_carry` with
`0039_tx_review_events`
applied, `transaction_events` present in Postgres, served
`Transactions-DzziI-k5.js` carrying the M106 chunk. **Live probe 27/27**
against `https://fintrac.princessandmark.com` —
`scripts/a11y/m106_review_probe.py` → `m106-review-probe.json` (docs/09
§Verification), run three times — at the first deploy, again after the
redeploy of `1efc4f6`, and a third time at final verification (the committed
JSON is that third pass) — **27/27 every time**: staged rows stay out of the
count (3 manual rows ⇒ 3), a committed 5-row QFX import takes the queue to
**8 unreviewed** with `?reviewed=false` total 8 and `?reviewed=true` 0 (one
predicate, two consumers), each row opens at exactly one receipt
(`source: manual` vs
`source: import` + Batch #148), the flip takes 8 → 7 and appends a `reviewed`
receipt while un-flagging appends a third, `/export/zip` ships
`transaction_events.csv`, the header reads **"8 unreviewed"** → **"7
unreviewed"** after one toggle click with `aria-pressed=true`, the History
drawer lists `reviewed · <payload_hash>` newest-first, and axe reports **0
critical/serious** with the drawer open.
**Fourth pass 2026-10-07 — 29/29** with two new checks (the committed JSON is
that run): a **keyboard-only flip** (focus + one real Enter through CDP, no
mouse — this pass still needed a probe-local `press_enter()`, because the
driver's `press()` sent `rawKeyDown` with no char and this host's Chrome ran no
default action for Enter; the driver is fixed and the helper is gone, see
`frontend/scripts/a11y/press_regression.py`), and
**bulk `Mark reviewed` empties the queue on BOTH surfaces** — `Reviewed 8 of
8` and the "8 unreviewed" pill gone from the header hint and the chip, the
UI half of the count-cache fix (docs/09 §Review queue).

## M107 — Split transactions: parent/child rows, two population rules, merge-back

**Spec-first (Mark'"'"'s ruling on the board: "yes, spec-first into this wave"). The spec
IS the milestone: `docs/44` carries the whole contract — schema, endpoints, envelopes,
the dedup/partner/export/event-log ripples, the named tests and the Verification
section — and lands on its own, before any code (M90 precedent `02e5c67`).**

**Context.** One `category_id` per row means a `COSTCO #123 -187.42` line is either
miscategorized or forced into one bucket, which distorts every spending report and
every budget. Splits are the personal-surface fix (retail/groceries/goods with several
buckets) — and they cut across five contracts that had to be pinned first: reports
aggregation, register/dedup, the M93 partner serializer, E2EE export snapshots, and
M106'"'"'s event log.

**Pins (docs/44 §Decisions, the short form):** children are rows in `transactions`
(`parent_split_id` FK, `is_split`, `split_checksum`, `note`); **LEDGER** (money is
counted: split parents out, children in) vs **IDENTITY** (a statement line is
identified: children out, parents in) are the only two population rules, named per
consumer; a split moves **no CAD total anywhere** (children sum exactly to the parent,
native and CAD — allocation from the parent'"'"'s cached `cad_amount`, never a re-derivation);
four endpoints under `/api/v1/transactions/{id}/split`; while split the parent'"'"'s only
edit is merge; imports stay parent-level and never auto-split; `GET /transactions`
answers LEDGER `items` + `split_parents` so no consumer can double-count; investment
accounts are refused (`422 split_account_not_allowed`) and the tax engine — which reads
`trades` alone — sees exactly one transaction per trade, unchanged.

**Deps:** M106 (writes `split`/`merge` events through its `transaction_events` writer;
`merge` widens its enum), M110 (shares `services/budgets.py`). Touches M90/M93'"'"'s
projection seam and M96'"'"'s bulk-conflict channel, both read-only here.

**Out of scope:** auto-splitting heuristics (receipt-capture card), investment-account
splits, multi-currency children, nested splits, YNAB `subtransactions` import.

**Acceptance:** docs/44 approved on the card BEFORE code; the 28 named tests in
docs/44 §Named-tests written and green; AGENTS §4 gates; docs reconciliation per
docs/44 §Touch points in the implementation commit; verification evidence (pytest/vitest
counts, CI id, deployed bundle marker, smoke screenshots) pinned on the card.


## M110 — Budget rollover: optional per-category carry-forward with honest spendable math

**Label collision on `main`, noted here so it does not propagate:** the
transfer-suggestions panel layout fix was committed as `8330033`
(`fix(M110) …`) and `f75828c` (`test(M110) …`) — wrong number. M110 is THIS
budget rollover; M111 was taken by `5dfbbd4` (import-wizard profile columns);
the layout fix is **M112** (docs/17 §Frontend, docs/09 §Transfer suggestions
panel — one shared track).

**Requested by PM** on the board as *"M105: Budget rollover"* — M105 was
already taken by the TWR/MWR display (itself a renumbered card) and cards
M106–M109 were spoken for by in-flight work when this landed, so it lands
here as **M110**. Wave 11 opener 1, from the Monarch-scan top-add list
(M104): a category that under-spends keeps nothing and one that over-spends
forgets, because `amount` is a standing per-period limit.

**Opt-in, off by default, one period deep.** `budgets.carry` (`off` |
`forward`, default `off`) is the only new state on a budget; the value that
carries lives in its own column's worth of space — never in `amount`:

- **`spendable = amount + carried_in`**, `remaining = spendable − spent`,
  `pct_used = float(spent / spendable * 100)` — the M65 true ratio, still
  never clamped, and **`null` when the pool is non-positive**, because no
  positive denominator exists (docs/09: the row says "over budget", it never
  invents a percent or clamps to one);
- **`carried_in(P) = amount − spent(P−1)`** — exactly one period deep, so a
  carried value can never feed its own successor (no compounding chain);
- only for a budget that existed before the period started (it governed the
  whole source period) — no budget in a month, nothing carries;
- **frozen at the boundary**: `budget_carryovers` (UNIQUE `(budget_id,
  period_start)`) is written once by the first read after the switch and
  never recomputed, so going back a period shows the value as it was even if
  the source period is edited later — the same auto-create-on-first-GET
  pattern `GET /notifications` uses;
- **one denominator, two surfaces**: the alert generator measures the pool
  too (M61 #3), stores it in `spending_alerts.budget_amount`, and a
  non-positive pool alerts critical at the documented `999.99` ceiling.

**Engagement surface (docs/09 §Budget carry):** a one-click `Carry leftover`
pill on each `/budgets` row (`aria-pressed`, no wizard), the carried figure
as its OWN line (`+$60 carried in` in gain / `−$100 carried in — a debt to pay
down first` in danger), the money line showing the spendable pool, and the
Dashboard cards (M97 presets order untouched) plus the shared-household card
carrying the same layering from `lib/budgetCarry.ts`. `GET /partner/budgets`
publishes `carry` (structure) and `carried_in` (money → `null` under the
owner's privacy mode, docs/43 §9).

**Pins:** the pool ratio is still the unclamped true ratio (the anti-fabrication
check runs on the NEW denominator); `carried_in` is its own wire field on both
budget shapes, never merged into `limit`; carry off is byte-for-byte the
pre-M110 row (math, labels, hints); the s3 golden is hand-amended because the
capture is frozen at `meta.base_commit` (`GET /subscriptions` died in M48-S3)
— same precedent as M65's `pct_used` edit, no captured number moved.

**Deps:** M65 (honest-% + the shared `services/budgets.py` predicate), M61 #3
(one number across surfaces), M97 (dashboard presets the cards keep), M93
(partner mask), M48-S3 (the `s3Engine.budgetsProgress` port).

**Tests:** `backend/tests/test_budget_carry.py` (11 — carry on with the money
hand-check `600 + 60 − 100 = 560`, deficit `-100` → pool `500`, carry off
writing no snapshot, no governed source period, a time-travelled back-view
freeze, `pct = float(100/660*100)` unclamped at 300% for an overspend,
`pct_used: null` for a swallowed pool, the alert denominator `660.00` vs
`600.00`, the critical ceiling, delete cascade, toggle round-trip) plus the
partner mask case in `test_household_sharing.py`; `frontend/tests/budgets.test.ts`
(5), `frontend/tests/lockedVaultDegradation.test.ts` → “A13 …” (the surviving
`budgets.progress` golden, `lib/budgets.ts` since M160). Gate numbers
below are from the rebased tree.

**Acceptance:** AGENTS §4 gates — ruff `format --check` + `check` ✓ (354
files), `tsc --noEmit` ✓, `eslint --max-warnings 0` ✓, `npm run build` ✓ +
`check:csp` ✓, `parity --slice s3` **16/16**, backend **pytest 1209 passed**,
frontend **vitest 930 passed / 1 skipped across 88 files** (every number on
the tree as pushed, after both rebases). Docs reconciled in the
implementation commit: 02 (model + `spending_alerts.budget_amount`), 08
(shapes + §Budget rollover), 09 (§Budget carry: layering, tone table,
engagement surface), 43 (partner projection + mask); docs/44's
budget-rollover cross-references corrected to M110.

**CI:** run `37544121766` green on all four jobs (secret-scan, backend, web,
status) with `1209 passed` in the backend log — that is the run whose
headSha (`cfaba67`) contains this commit; the run opened on this commit
itself (`37543198344`) was superseded by it (`cancel-in-progress`), its
secret-scan + web jobs green before it was cancelled.

**Deploy (example.internal, 2026-10-06):** `git pull --ff-only` → `cfaba67`, then
`docker compose up -d --build api mcp web` — all containers healthy,
`alembic_version = 0040_budget_carry`, `docker logs api` clean, and the
served bundle proved by content (`/assets/Budgets-BscevfwZ.js` carries the
`Carry leftover` marker ×5).

**Live money-math hand check** (`frontend/scripts/a11y/m110_carry_probe.py`,
**13/13 checks**, evidence `m110-carry-probe.json`): a throwaway tenant on
prod with a $600 budget, `carry: forward`, a $540 source-period spend
(backdated `created_at` so the budget governed that period — the API cannot
set it) and a $100 current-period spend:

| what | wire | hand check |
| --- | --- | --- |
| carried in | `"60.00"` | 600 − 540 = 60.00 |
| remaining | `"560.00"` | 600 + 60 − 100 = 560.00 |
| `pct_used` | `15.151515151515152` | 100 ÷ 660 × 100 |
| deficit twin | `carried_in "-100.00"`, `remaining "500.00"` | 600 − 700 = −100; 600 − 100 − 0 |
| `carry: off` | `carried_in null`, `remaining "500.00"`, `pct 16.666666666666668` | 600 − 100; 100 ÷ 600 × 100 |
| `carry: forward` again | `carried_in "60.00"` | the SAME frozen snapshot |

On screen (light + dark): `$100 / $660 spendable`, `+$60 carried in — extra
room this period`, `15% used`, `aria-pressed="true"` — i.e. the CLIENT port
reproduces the server's numbers, and one click flips the row back to
`/ $600` with the carry line gone and `aria-pressed="false"`. Screenshots
`m110-carry-prod-{,dark-,off-}1440.png`; no console errors.

**A11y:** `axe_scan.py --width 1440` against the public origin — **12/12
runs (6 pages × 2 themes), 0 violations of any severity**, evidence
`m110-axe-prod-1440.json`.

## M108 — QoL batch 4: spending treemap, saved report views, owner grouping, receipt images

Four independent slices that each close a gap a person notices weekly. No
shared file between them beyond `Reports.tsx` / `Transactions.tsx`, which is
why they shipped as separate commits inside one milestone.

**P1 — Spending treemap (docs/12 §Spending treemap).** The stacked bar answers
"how did each month add up"; it is the wrong shape for "which category owns
this range". A second view on the same data — `Chart view: Bars | Treemap` —
draws one block per category over the SAME `spendingReport.items` the bars do,
so the two variants cannot disagree, with the same palette, the same ordering,
the same 8-decimal-trimmed-to-2 share (M105's rule, via the extracted
`lib/percent.ts`), and drill-in through the exact deep link the bars already
use. Squarify is hand-rolled in `lib/treemap.ts`: the locked stack has no d3,
and recharts' own `Treemap` does not take a drill target or a text-equivalent
layer. One `role="img"` label carries the whole picture for a screen reader.

**P2 — Saved report views (docs/09 §Saved report views, docs/08).** One JSON
column `users.saved_report_views` (migration 0038), mirroring M97's
`dashboard_preset`: it roams with the user instead of being pinned to one
browser, `[]` means none, and the server validates the shape (closed tab set
of the seven ids in `reportTabs.ts`, closed state-key set, 8–64 char id) rather
than trusting whatever the client sent.

**P3 — Owner grouping (docs/09 §Owner dimension).** `Group by: Category |
Owner` on the transaction-backed tabs, the Owner option shown only when
`GET /partner/state.linked` is true — a lone tenant gets no Owner option and
zero projection fetches. **Shipped contract since M124/M159 (the form the
probe now pins):** the control itself always renders with `Category | Tag`
(tags need no partnership) and only the `Owner` option stays gated, so the
owner seat — whose own `/partner/state` is `{linked: false}` — reads
`Category | Tag` too. The M93 partner row projection runs through the SAME
report engines the category view does, so the totals reconcile by
construction; drill-in is omitted under `Owner` because there is no category
target to drill to, and `group_by` is stored in a saved view for every
session (M124 — Tag is always on offer), not only while the Owner dimension
is actually offered.

**P4 — Receipt images (docs/09 §Receipt images, docs/02 §transaction_receipts,
docs/08).** One photo per transaction: `transaction_receipts` (migration 0041),
`PUT/GET/DELETE /transactions/{id}/receipt`, and `has_receipt` on every list
row from ONE batched query. The image is base64 TEXT — `/export/full`, the ZIP
export and the browser backup all serialize through `json.dumps`, which cannot
carry `bytea` — so a receipt survives the backup docs/11's lost-passphrase
guarantee depends on. The client re-encodes to webp under 2 MB before the trip
(`lib/receiptImage.ts`, platform APIs only, no new dependency), the server
sniffs magic bytes and refuses a header that lies about them, and there is no
OCR and no email forwarding: the panel that says "an image is attached" is
exactly where extracted fields land when OCR is picked up, and a second
boolean added then would be worse than one honest flag today.

**Ripple:** docs/34 budget rows 4/5 move 4 → 5 statements for P4's batched
flag — the constant term, not a per-row lookup, so invariant 1 (page-size
independence) still holds at 5 / 5 / 5.

**Acceptance:** each slice's own Verification section in its spec doc green
(`test_transaction_receipts.py` 11, `test_saved_report_views.py` incl. the
`group_by` round-trip P3 adds, `test_perf_budgets.py` for the budget move,
`m108Treemap.test.ts`, `m108Reports.test.tsx` 20, `m108Receipts.test.tsx` 12);
AGENTS §4 gates — backend **pytest 1221 passed**, frontend **vitest 942 passed
/ 1 skipped across 89 files** run `TZ=UTC` (CI's own clock, after the
hardcoded-date fix), `tsc --noEmit`, `eslint --max-warnings 0`, `vite build` +
`check:csp` clean; docs reconciled in the same commits; and **live on the
deployed stack** through `frontend/scripts/a11y/m108_probe.py` → **76/76
checks** (`m108-probe.json` + 12 screenshots pinned on the card): the
treemap's `64.49%` on screen against `64.49359721%` in the legend's
`aria-label`, a saved view surviving a full reload and restoring its exact
range before rename/delete, the Owner lens only in the LINKED household (and
never in the owner's own session — `/partner/state` is the partner-side read,
docs/43 §6), the receipt round trip with its chip re-rendering in the row and
clearing on Remove, axe 0 critical/serious at 390/412/1440 × dark/light, and a
0-overlap text-node sweep. **CI caveat:** from 2026-10-07 01:04Z GitHub Actions
refused EVERY job in the repo on the account's billing ("recent account
payments have failed or your spending limit needs to be increased"), so the
final pushes carry the local suite + the live probe instead of a run — the
last CI-green head is `54624c5`.


## M109 — Free local forecasting: net-worth what-if scenarios

**Wave 12 headline (Monarch-scan #4; D1 ruled yes, D2 stands).** The single
feature Monarch users say should be core and its $199 Plus tier paywalls, built
free, deterministic and local. Spec landed FIRST as `docs/45-forecasting.md`
and approved on the card before any code (AGENTS §7), then implemented against
it — this entry is the reconciliation, the contract is the doc.

### What shipped

- **`backend/app/services/forecast.py`** — a deterministic net-worth projection
  engine. Two halves on purpose: `collect_inputs` is the only half that reads
  the database, and it reuses the shipped report machinery instead of
  re-deriving it (`net_worth_report` for the starting `cash_cad` /
  `positions_cad`, `cashflow_report` for the trailing income/expense window),
  so the assumptions panel prints figures that equal the Cash-flow and
  Net-worth tabs. `run_forecast` is pure: no clock, no randomness, no network,
  no LLM — same inputs ⇒ byte-identical JSON (test F3).
- **The double-count rule** (docs/45 §1.2): historical flow already CONTAINS
  the recurring rows, so the engine decomposes instead of adding
  `income = recurring + residual`, `expense = recurring + residual`, and
  publishes all three to the cent. Residuals are never clamped — if a short
  window over-explains itself the residual goes negative and the user SEES it.
- **Five typed life events, closed set**: `house_purchase`, `career_break`,
  `salary_change`, `one_off_cash_event`, `recurring_cost_delta`. Curated
  discriminated union with `extra="forbid"`; there is deliberately no formula
  editor (not-scriptable = not buildable). An unknown `type` answers the
  spec's own `unknown_event_type`, raised outside pydantic so the envelope
  names the actual mistake instead of the generic `validation_error`.
- **`/forecast`** — new route, new page, rail row directly after Reports, and
  an `Open what-if scenarios` link added to the Reports Projections card (the
  card itself is untouched). N-scenario overlay, instant future/real dollars
  toggle (both series always ship, so the toggle does not refetch), collapsible
  Assumptions panel, four load-bearing honesty strings from
  `frontend/src/lib/forecastCopy.ts`.
- **Storage** `users.forecast_scenarios JSON NOT NULL DEFAULT '[]'` (migration
  `0042_forecast_scenarios`) — the M108-P2 `saved_report_views` pattern reused
  exactly: one column, per-user, `[]` meaning "none", entry shape validated at
  the API seam. Export picks it up for free because the ZIP's table set is
  derived from `app.models`.
- **Owner-model surface**: `/forecast/*` authenticates with `get_current_user`
  (the caller's own tenant) and never reaches `/partner/*` — the FIRST
  owner-only surface in the app, pinned by test F14 rather than assumed.

### Decisions worth surfacing

1. **Granularity samples, it does not change the model.** State evolves at
   dated effects (monthly anchors, event dates, recurring cadence dates);
   `day` / `week` / `month` only decide where the line is read. So a `day` and
   a `week` run agree exactly on the dates they share.
2. **The series starts ON `as_of`**, so a run with no events opens at the net
   worth the Dashboard already shows.
3. **`day` stays the default for every horizon.** Measured: 2-year daily =
   48 ms best-of-5 (732 buckets, all five event types live, 120 recurring
   streams) against a 5 s budget; 10-year daily = 882 ms. The card's fallback
   (drop to weekly, allow daily only to a year) is not triggered.
4. **Both `end_date`s are inclusive.** "No income from A to B" means B, and a
   cost delta ending on a date is charged ON it.
5. **A career break suppresses ALL income**, not a salary subset — salary and
   any other income are indistinguishable at row level. Guessing which to drop
   would be worse than saying so, and the assumptions panel says so.
6. **Real dollars ship alongside nominal** rather than behind a query
   parameter: `real_values` is always in the response, so the UI toggle is
   instant, offline and impossible to desync. One constant rate over the exact
   day fraction of each bucket.
7. **House value is held flat** at the purchase price and the mortgage is a
   flat monthly rate — no appreciation, no Canadian semi-annual compounding,
   no stress test. Both disclosed wherever the event is.

### Tests

16 backend (`backend/tests/test_forecast.py`, F1–F14) covering: the balance
jump landing on exactly the event date with hand-checkable interest
(400k × 6% / 12 = 2000.00) and a 300-period schedule that repays the principal
to the dollar; the weekly grocery cadence landing on ITS OWN dates with the
three published flow figures adding up; byte-identical repeat runs; career
break suppressing exactly the anchors inside its range while expenses keep
posting; salary/cost-delta first-move dates; one-off moving a single point;
two interleaved events; empty / single-account / negative-debt tenants; the
inflation short-circuit; every bound and unknown type; tenant isolation; the
demo tenant's 403-on-write / 200-on-read; the export carrying the column; and
the partner surface carrying none of it. 4 frontend (`m109Forecast.test.tsx`,
F15–F18): the four honesty strings, the toggle refetch-free, the nav + Reports
link, and the create → copy → delete round trip.

### Verification

Docs reconciled in-commit (`45` new, plus `02`, `08`, `09`, `10`, `12`, `11`,
`33`, `43`). Backend `ruff format --check` + `ruff check` clean, full pytest
green on an isolated test database, frontend `tsc --noEmit` + `eslint
--max-warnings 0` + 950 vitest + production build all green. Live smoke and the
deploy evidence are recorded on the card.


---

## M116 — Bills suite: match rules, past-payment finder, bill drill-down

**Requested by Mark** directly (4 asks): smarter matching rules, a drillable
bill page, a rolling average, and a paid-history bar chart. M-scope, spec-first
NOT required — every pattern was already in-house (a shared normaliser, the
report bucket keys, the chart primitives, the house page shape), so the card
pinned the SEMANTICS and deviations are report-only.

### What it is

`bill_reminders` grew a match rule — `match_mode` (`contains | starts_with |
exact`) + `match_term` + optional `amount_tolerance`, with `category_id` reused
as the category scope — and the Bills surface grew a drill-down at
`/bills/:billId` showing every matched transaction, both rolling windows and a
period bar chart with a data-table fallback.

The decision that shaped the rest: **matched = derived, never copied.** There
is no bill-link table; every read re-applies the current rule over the
committed ledger, so editing a rule retroactively reshapes the bill and nothing
can go stale. The cost of that honesty is that the payee predicate cannot be
pushed into SQL (Postgres case folding ≠ `str.upper()`, so no `ILIKE` is a
proven superset of the shared normaliser) — which is why the scan is bounded by
the tenant + staging + sign predicates instead, measured, and **no index was
added**.

Full spec: **docs/25 §M116** (decisions D1–D10, schema, wire, touch points,
verification), wire in **docs/08 §Bill Reminders**, columns + index reasoning in
**docs/02 §bill_reminders / §Indexes**, UI in **docs/09**, and the `bill_due`
link change in **docs/35 / docs/37**.

### Tests

20 backend (`backend/tests/test_bills_match.py`) including two hand-recomputed
figures (mean `35.00` over six payments; `16.67` when a month is missed), the
derived-not-copied rule-edit sequence, tenancy, staging, split-parent semantics,
the `find-past` read-only guarantee, and a 10,000-row perf sweep that pins the
EXPLAIN plan's `user_id` predicate and measures 0.647 s against the 1.5 s
budget. 4 frontend (`m116BillDetail.test.tsx`) for the rule line, the
server-supplied rolling windows, the `role="img"` frame + scoped data table with
its `missed` marker, and the inline rule editor — plus reconciliations in
`billAutofill`, `recurringPrompt`, `m95EmptyStates` and `m102ScrollRegions`.

### Live evidence

Deployed (`c46db79`, example.internal `docker compose up -d --build api mcp web site`,
migration `0044_bill_match_rules` applied) and probed end-to-end against
`https://fintrac.princessandmark.com` by
`frontend/scripts/a11y/m116_bill_probe.py`: **31/31 checks** — a real CSV import
flow feeding six payments across seven months with April skipped, every rolling
figure hand-checked on the wire (34.29 / 30.00 / 38.33 / 40.00 / 240.00), the
derived rule-edit moving the match 6 → 7 with no write, and **axe 0 in all six
width × theme runs** (390/412/1440 × dark/light) with the data-table
`<summary>` reached in 7 Tab stops and opening on Enter.
Evidence: `frontend/scripts/a11y/m116-bill-probe.json` + six screenshots.

**CI note:** the GitHub Actions `CI` workflow did NOT run for this push — every
run since M114 fails at job start with *"recent account payments have failed or
your spending limit needs to be increased"* (a billing gate, not a test
failure; `gh run view 37798472782`). The full AGENTS §4 gate sequence was run
locally instead: ruff clean, **pytest 1340 passed** on real Postgres, tsc +
eslint clean, **vitest 1033 passed**, production build green.

### Out of scope (pinned)

Auto-creating bills from recurring detection (one-click prefill YES, auto-create
NO); manual pin/unpin of individual transactions (needs a copy table — v2);
due-date auto-advance beyond the existing `remind_days_before` logic.


---

## M120 — receipts for the four non-receipted mutation surfaces

**Deps: M106 (the log), M96 (the recurring prompt), M107 (split guards), M46
(rules).** Blueprint `t_81e4e640` → implementation `t_f0b99f3f`, spec
**docs/M120_RECEIPTS_SPEC.md** (reading (b): every one of the four surfaces
gains receipts).

### What it is

M106 shipped the promise — *edits never silently vanish; every change is a
receipt* — and then recorded four exceptions as "reported, not hidden". Three
of them are button-clicks a person makes on a row whose History drawer they can
already open, and the fourth is the pipeline's own bulk categorizer, the single
biggest volume of `category_id` writes on a fresh import. A drawer that says
"you created this from import Batch #148" and then says nothing about the row
being auto-classified moments later is exactly the half-visibility M106 was
built to refuse. M120 closes all four:

| surface | receipt | whose |
|---|---|---|
| `POST /suggestions/{id}/accept` | `categorized` `{"category_id","category_name","via":"suggestion_accept","suggestion_id"}` | the clicker |
| `POST /rules/{id}/apply` | `categorized` `via="rule_backfill"` **one per updated row** + a `tagged` rider when the rule's tags moved the junction | the caller |
| `POST /transactions/recurring/accept` | `edited` `{"fields":[3],"changes":{…}}`, **no `via`** | the caller |
| pipeline layers 1–3 | `categorized` `via="rule" \| "learned" \| "llm"` **one per row** (+ `tagged` for layer-1 rule tag hits) | `NULL` (system) |

### Decisions pinned (do not re-litigate)

- **Zero new `event_kind` values, no column, no migration.** The only new
  payload key is the optional `via` provenance string, read by
  `render_summary` at RENDER time — so rewording a line never needs a
  migration, and a plain PATCH payload (no `via`) renders byte-identically to
  what M106 shipped (`SHA_UNICODE_EDIT` / `SHA_MANUAL_CREATE` unchanged).
- **`actor = current_user.id` for the three human surfaces, `NULL` for the
  pipeline.** That single value is the whole partner-permission story:
  `api/partner.py`'s existing `actor IN (owner, partner)` excludes SQL NULL,
  so system receipts are owner-only with **no new predicate**.
- **One receipt per affected row, in the same commit** — the `stage_rows` /
  `POST /review` contract. Deliberately NOT a group receipt: a "3 rows changed"
  line is only inspectable for rows it names, and it would lose the per-row
  `payload_hash` a drawer re-checks. `overwrite=false` skips and `mode=forward`
  write nothing at all.
- **`render_summary` gains ONE additive branch** keyed on `payload["via"]` —
  five new wordings (`(rule)`, `(learned from your edits)`, `(assistant
  suggestion, auto-applied)`, `(rule backfill)`, `(accepted suggestion)`), no
  new parameter, no new kind, no new function.
- **Layer 1's symmetric write is pinned, contested, unchanged**: `apply_rules`
  writes `via=rule` on EVERY hit, including a hit whose target equals the value
  the row already carries. Layers 2/3 can only ever write a null previous (they
  scan `category_id IS NULL`), so this is what keeps the three layers
  symmetric. Changing it needs a parent-card review.
- **Exclusions verified, not widened**: merge-back and replace-split touch no
  field on the parent (M107/D15 already receipts them), so neither gained a
  receipt; `_refuse_split_row_write` on suggestions-accept is untouched; the
  demo SEED still writes none because the demo tenant cannot write at all.
- **`_apply_tags` now RETURNS its junction diff** (`added_ids`, `removed_ids`)
  so a `tagged` rider is written only when the junction actually moved — the
  same no-op rule `PUT /tags` follows — and the payload it writes is the SAME
  four-key shape (`added`/`removed` names + `added_ids`/`removed_ids`), never a
  slimmer one. The names now come from `tx_events.load_tag_names`, shared with
  the tag routes, so the two shapes cannot drift.

### Tests

`backend/tests/test_m106_review_events.py` extended **in place, 12 → 22** (no
new file; the original twelve expectations untouched), one function per surface
of the spec's numbered list 1–8: suggestion-accept (payload, hash re-derivation,
actor, wording, plus the 409 adding nothing), rules-apply (3 rows → 3
`categorized` + 3 `tagged`, `overwrite=false` and `mode=forward` writing
nothing), recurring-accept (2 rows → 2 plain `edited`, `via` absent), the
pipeline's three layers (`actor=NULL`, each `via` string, its own wording), the
§6 symmetric write with no duplicate `tagged` rider, the merge-back /
replace-split exclusion pin, the partner projection (a real `via=rule`
`actor=NULL` receipt reaches the owner and is absent from the partner's page,
payload stripped), pre-handler + handler refusals on the new surfaces (422 ×2,
404, 409, and the 429 limiter leaving the primed rows untouched), and export
coverage (`/export/full` carries both new receipts and re-derives every hash;
the ZIP ships the committed row's and drops the staged row's).

### Verification (local gates; Actions waived per Mark's standing ruling)

- `ruff format --check` + `ruff check` clean.
- Backend pytest on an isolated database (`fintrack_m120_test_test_m120f0b99f3f`,
  `SEED_DEMO_ACCOUNT=false`): **2 failed, 1376 passed** in 778 s. Both failures
  are `tests/test_suite_db_isolation.py::test_force_drop_waits_out_a_backend_it_cannot_signal`
  and `::test_force_drop_says_which_backend_would_not_leave`, and both reproduce
  **identically on a clean `origin/main` @ `9ae0c4a`** (run in the dev clone,
  same tunnel) — a pre-existing environmental blocker, not a regression: the
  scratch "blocker" subprocess shells out to `sudo -u postgres psql` over the
  local socket while this suite runs through the example.internal tunnel, so it never
  attaches. The M120 file itself: **22 passed**. *Since fixed (t_03722af5): the
  gate now also requires the scratch database to be on the server `sudo -u
  postgres psql` reaches (same `pg_database.oid` on both), so a tunnelled run
  reports these two as **skipped** — named, with the reason — instead of failed,
  while a suite on the local cluster still runs them.*
- Frontend (`tsc --noEmit`, `eslint . --max-warnings 0`, `NODE_ENV=test vitest
  run`): **101 files, 1041 passed, 1 skipped**, exit 0. Zero frontend source
  changes — the drawer already renders whatever `summary` arrives, and the
  `TransactionEventKind` union needed no widening because M120 adds no kind.
- `npm run build` green (`tsc && vite build`, sw stamp refreshed).
- Secret scan: `git ls-files | grep -iE '\.env$|secret|token|pem'` → only the
  known `mcp_tokens` / `refresh_tokens` source files and `siteTokens.test.ts`.

### Live evidence

Deployed (`8755d0f`, example.internal `git pull --ff-only` + `docker compose up -d
--build api`; `/api/v1/health` 200, container healthy, `alembic_version` still
`0044_bill_match_rules` — no migration) and probed end-to-end against
`https://fintrac.princessandmark.com` by
`frontend/scripts/a11y/m120_receipts_probe.py`: **30/30 checks** on a
throwaway tenant. The HTTP half proves the four surfaces write what the spec
pins (payloads, `actor`, `payload_hash` re-derivation, `overwrite=false` /
`mode=forward` / a repeat-accept writing nothing, `/export/zip` carrying the
new receipts); the browser half opens the real History drawer and reads the
sentences verbatim:

- `you set category groceries (rule backfill)` + the `tagged` rider above it
- `the system set category groceries (rule)` — actor NULL, `actor_label` "the system"
- `you set category groceries (accepted suggestion)`
- `the system set category groceries (learned from your edits)`
- `you edited this transaction` — recurring accept, no `via`

**0 axe critical/serious with the drawer open.** Evidence:
`frontend/scripts/a11y/m120-receipts-probe.json`.

**One surface could not be probed live, and why:** layer 3 (`via=llm`) needs a
resolvable LLM upstream, and the deployed one is down — `POST /suggestions/run`
queues the batch, then `app.llm.base` logs `HTTP 404` from
`http://192.168.1.6:11434/v1/chat/completions` twice and the batch is abandoned
by the pre-existing retry policy, so no category ever moves and (correctly) no
receipt is written. That is an infrastructure failure unrelated to M120; the
layer is covered instead by
`test_m120_pipeline_layer3_autopilot_receipts_llm_apply`, which runs the real
`_process_one_batch` against a scripted provider and passed in the suite above.

### Out of scope (pinned)

No drawer change, no suggestion-queue change, no new chic-col column; no
frontend source; no migration; no enum widening. Receipt VOLUME growth from
per-row bulk receipts is the append-only log's existing story (`VACUUM FULL`,
or an archival cutoff in a later card) — parked, not invented here.
---

## M117 — Refresh that refreshes, and a securities key that cannot duplicate

One report, three symptoms: *"prices not refreshing"*, *"the app appears
ineffective"*, *"no streaming on /investments"*. Two were the same defect and
the third was its shadow; the reported "duplicate securities" turned out to be
a false premise with a real latent bug underneath it.

### D2/D3 — the freshness window was doing duty as a fetch gate

`market_data.ensure_recent_price` returned the cached close untouched whenever
the newest row was `<= PRICE_STALE_DAYS (7)` days old. That window is the
*staleness indicator* threshold (docs/07), but it was also being used as the
fetch decision, so both `POST /portfolio/refresh` and the market-hours
`GET /portfolio/positions` fan-out silently did nothing for a quote fetched
within the last week — a user's positions sat a day behind except on the one
day in eight when the window expired. Live evidence: 3 of 14 held/watched
securities for user 343 (VT, VXUS, VUN) sat at `price_age_days = 1` with no way
to advance them.

Compounding it, the two refresh paths disagreed: `POST /securities/{id}/refresh`
ran `backfill_range(365d)` while `POST /portfolio/refresh` ran the gated
`ensure_recent_price` — same word, two contracts, which is exactly why the
per-security page "loads history" and the investments page does not.

**Fix.**
- `ensure_recent_price(..., *, force: bool = False)` — `force` skips the
  freshness window; **both** paths stop at a close already dated `today`, which
  bounds the automatic fan-out to one network call per security per calendar
  day instead of one per positions request.
- `POST /portfolio/refresh` now runs `securities.queue_backfill` (the same
  365-day backfill as the per-security refresh — one `.history()` call either
  way, a wider window only widens the payload), with **all price tasks queued
  ahead of the fundamentals fan-out**, because positions render from prices and
  `Ticker.info` is the slow seam.
- `GET /portfolio/positions` enqueues `_run_ensure_recent_price(force=True)`.
- `_markets_are_open()` → **weekdays 09:30–20:00 `America/New_York`** via
  `ZoneInfo` (the old `6 <= (now.hour - 4) % 24 < 20` was pinned to a fixed
  offset and drifted across DST). Tighter on purpose: a forced refresh cannot
  produce a bar dated *today* before the open or on a weekend, so it could only
  re-return the cached previous close — once per positions request, all morning.
- Frontend: the single 2s invalidate after `202` became a **2s/6s/12s backoff**
  (the fan-out is a sequential `BackgroundTasks` chain that outlives the
  response), plus `usePriceStream` now invalidates `["positions"]` /
  `["exposure"]` **only when its payload changes** — the stream is a 5s cache
  poll that repeats an identical payload, so invalidating per tick would turn a
  read-only transport into a refetch storm.

### D3 — the stream was never broken

Verified end-to-end through the public origin: `GET
/securities/stream/prices` answers HTTP/2 `200` with
`content-type: text/event-stream`, emits one `data:` frame per ~5s, and the
deployed nginx `location /api` already carries `proxy_buffering off` +
`proxy_cache off` + `proxy_read_timeout 86400s` (the `X-Accel-Buffering: no`
header the backend sets is consumed by nginx, which is why it is absent from
the client-visible headers). What "no streaming" actually looked like on
`/investments` was the amber **`prices stale`** label (M65): with
`price_age_days > 0` the indicator shows `prices stale` instead of
`Stream connected` *even while the connection is healthy*. That is a
consequence of D2, and clearing the stale rows clears the label.

### D1 — the "15 duplicate symbol rows" were cross-tenant, not duplicates

The diagnostic grouped `securities` by `symbol` alone. Securities are
per-tenant (`UNIQUE (user_id, symbol, exchange)`, AGENTS §2), so the 15 groups
are one row per *different user* — NVDA ×7 across 7 users, MSFT ×6, CASH ×5.
Against the real key: `GROUP BY user_id, symbol HAVING count(*) > 1` → **0
rows**; case-insensitive/trimmed → **0 rows**; un-normalized symbols → **0**;
orphaned securities → **0**; `prices` rows whose `user_id` disagrees with their
security's → **0**; duplicate `(security_id, date)` → **0**. Nothing to merge —
and merging across tenants would have moved trades between users.

**The latent bug underneath was real.** `_resolve_security` /
`_lookup_security` on the investment-CSV import matched and stored the raw
symbol cell while `POST /securities` uppercases its input, and `exchange` was
only stripped on `POST`/`PATCH`. A CSV reading `veqt` therefore created a
*second* row beside `VEQT` — two securities for one listing, each with its own
trades, prices and ACB pool, so valuation picked whichever twin the query
reached first.

**Fix.** Normalize `symbol` and `exchange` to `strip().upper()` at every write
path (`POST`/`PATCH /securities`, `_resolve_security`, `_lookup_security`,
`_normalize_exchange` → `lookup_symbol` so its consumers get the same spelling),
plus `migration 0045_merge_dup_securities` running
`app.services.security_dedup.repair_securities`: idempotent, repoints every
FK, refuses (and reports) any group whose `acb_pools` would collide because
that is cost-basis arithmetic, not bookkeeping (docs/06).

**Deps:** M11, M13, M16, M21.

**Acceptance:** held∪watched securities reach `price_age_days = 0` after
`POST /portfolio/refresh` (or one market-hours positions read); the button
queues a 365-day backfill per security with prices before fundamentals;
`_markets_are_open` is weekday 09:30–20:00 America/New_York; the SSE indicator
reads `Stream connected` with `lastEventAt` < 60s once no row is stale;
`repair_securities` merges seeded case-variant duplicates, repoints
trades/prices/watchlist/fundamentals/etf_holdings, is idempotent, and refuses an
ACB-conflicting group; a lowercase import symbol reuses the existing row.

**Tests:** `backend/tests/test_m117_price_refresh.py` (16), `backend/tests/test_security_dedup.py` (5), `test_import_reuses_existing_security_for_lower_case_symbol`, plus the reconciled W11 fan-out assertion.

### Live evidence

Deployed (`07e5952`, example.internal `docker compose up -d --build api mcp web site`) and
probed against `https://fintrac.princessandmark.com` with
`frontend/scripts/a11y/m117_refresh_probe.py` — a throwaway tenant (one
account, one `MSFT` security, one committed trade), **18/18 checks at 390 AND
1440**:

- **defect reproduced then healed:** delete today's close → `GET
  /portfolio/positions` reports `price_age_days == 1`; the market-hours
  background refresh puts it back to 0 in **1.6 s** with no manual action.
- **button, isolated:** precondition re-read from `/prices` (a side-effect-free
  read — every positions GET schedules its own refresh, which healed the row
  behind the button's back on the first run), `POST /portfolio/refresh` →
  `202 {"queued":1}` → today's close back in **1.0–1.1 s**, no other endpoint
  polled in between. Then `GET /portfolio/positions` → `price_age_days == 0`.
- **SSE:** `/investments` renders **`Stream connected`** (not the amber
  `prices stale` that M65 shows when any row is >24h old) and the
  `Last stream message:` clock advances `5:00:10 PM → 5:00:15 PM` over 7 s —
  events actually arriving, not a one-shot `onopen`.
- **axe:** 0 critical + 0 serious on `/investments` in **both themes at both
  widths**; browser console clean.

Second tenant, read-only — the demo tenant (`POST /auth/demo`), which the
demo write guard keeps off `POST /portfolio/refresh` entirely: **7/7 rows stale
(age 6–7: 2026-10-01 / 2026-10-02) → one `GET /portfolio/positions` → 0/7
stale, all 2026-10-08, in 10.1 s.** Every one of those was inside the
`<= PRICE_STALE_DAYS` window, i.e. permanently stuck on the old gate.

Production data: user 343's **VT, VXUS and VUN — the three rows the report
named at `price_age_days > 0` — are now `2026-10-08` / age 0, as are all 20 of
that tenant's securities.** They self-healed on the first page load after the
deploy.

**Repair summary (migration `0045_merge_dup_securities`, applied on deploy):**
`normalized=0 merged_groups=0 merged_away=0`, `alembic_version` =
`0045_merge_dup_securities`, 78 securities / 11 users untouched — zero ids
merged, zero rows affected, because the key was already unique per tenant.

**CI note:** the GitHub Actions `CI` workflow still does not run for pushes
(billing gate since M114). Gates run locally: ruff clean, **pytest 1398 passed**
on real Postgres with the same 2 pre-existing `test_suite_db_isolation` failures
as the pre-change baseline (1353 passed + those 2 — they need a passwordless
`sudo -n -u postgres`, unavailable on this host), tsc + eslint clean,
**vitest 1042 passed (101 files)**, production build green.

**Out of scope (pinned):** the data model (`uq_securities_symbol` unchanged);
intraday re-valuation of an already-today close — `_cache_closes` stays
insert-only so manual rows can never be overwritten; deleting other tenants'
test fixtures (TRTEST ×3, m78qa/m113/m115/m116 probe users) — reported, not
merged, since each is that tenant's own row; `PATCH /securities/{id}` still
answers a bare 500 on a symbol/exchange collision instead of `409
duplicate_security` (pre-existing, separate fix).


## M121 — Multi-term match rules (AND/OR) across bills, rules and the detector

> Board card `t_ced11431`, filed as **"M120"** — M120 is already the receipts
> milestone above, so this is documented as **M121**, the first free number
> (reported, AGENTS §3). Spec: **docs/25 §M121**; wire in docs/08, UI in
> docs/09 §Match terms, schema in docs/02.

**What changed.** A bill's match rule becomes a TERM LIST (1..10 terms of
1..120 chars) with `match_logic: any | all`. `any` (default) widens the
matched set across payee aliases — a Rogers bill with `["ROGERS", "RCOM"]`
catches every spelling; `all` narrows it to rows carrying every term. Terms
live in the new `bill_reminder_match_terms` child table (position 0 =
terms[0] = `match_term`, which stays on the wire so pre-M121 clients keep
working), and `match_logic` is a new `bill_reminders` column with server
default `'any'`.

**One matcher, three callers.** `services/match_terms.py::match_terms(
terms, mode, normalize, subject, comparison)` is now the only implementation
of "does this payee match": bills (`bill_match.payee_matches`, pre-normalized
keys via the identity normalizer so the M116 scan keeps one normalization per
ledger ROW), the categorization rules (`exact_payee` = case-sensitive equality,
`contains` = case-insensitive substring — byte-identical to before, `regex`
deliberately untouched) and the recurring detector
(`recurring.merchant_answered`, the candidates route's "already answered"
exclusion). Same signature, so the three cannot drift.

**UI.** The single term input becomes a CHIPS field with the ANY/ALL
segmented switch beside it (`components/bills/MatchTermsField.tsx`): Enter
commits, Backspace on an EMPTY field removes the last chip, each chip has a
labelled remove button, the 10-term cap refuses the 11th with visible copy,
and the typed-but-uncommitted term still reaches the payload
(`composeTerms`). The detail header joins terms with `or` / `and` per mode.

**Export/backup (docs/11).** The child table is exported from `app.models`
and scoped through `bill_reminders` in `export.py`'s `VIA_PARENT` — both
fail-closed guards caught its absence before push: the first full-suite run
reported **28 export/restore failures** naming
`export_registry_drift: … ['bill_reminder_match_terms']` rather than shipping
a silently shorter snapshot. Lesson recorded in docs/25 §M121 Verification.

**Tests.** `backend/tests/test_m121_multi_term.py` (16) + reconciled
`frontend/tests/m121MatchTerms.test.tsx` (9), `m116BillDetail`, `billAutofill`.
Backend **1417 passed** on real Postgres; frontend **1060 passed (103
files)**; `tsc`, `eslint --max-warnings 0`, `ruff format --check` + `ruff check`
clean; production build green.

### Live evidence

Deployed (`5efba2f`, example.internal `git merge --ff-only origin/main` + `docker
compose up -d --build api mcp web site`, api healthy) and probed against
`https://fintrac.princessandmark.com` with
`frontend/scripts/a11y/m121_multi_term_probe.py` — **37/37 checks**:

- **migration on prod**: `alembic_version = 0046_multi_term_match`, the
  `bill_reminder_match_terms` table created with its UNIQUE/CHECKs and the
  `ON DELETE CASCADE` FK, and Mark's **12 real bills backfilled to 12
  position-0 term rows**, every one `match_logic = 'any'` and
  `match_term = terms[0]` — no existing rule changed its meaning;
- **read-only on a real tenant** (the demo tenant, which is read-only): all 5
  bills answer `match_terms` + `match_logic`, and `match_term == terms[0]`
  on every row;
- **ANY widens**: one Rogers bill with `["ROGERS"]` matched **3**; the same
  bill with `["ROGERS", "RCOM"]` under `any` matched **4** (`RCOM` joins,
  decoy `FIDO` still excluded), with `payment_count`, the chart total
  (**200.00** = 4 × $50), the 12-period mean (**50.00**) and the paginated
  `total` (**4** under `limit=2`, 2 items shown) all moving together;
- **ALL narrows**: the same two terms under `all` → **0** everywhere
  (matched, `payment_count`, page total, rolling `payments`), and an
  intersecting pair `["ROGERS", "WIRELESS"]` → exactly **1** row,
  `ROGERS WIRELESS`;
- **422s**: an 11th term, a self-disagreeing `match_term`, a digits-only term
  and an unknown `match_logic` all answer `422` with
  `"code":"validation_error"`;
- **UI at 390/1440**: chips render one per term with
  `Remove term — <term>` buttons, `ANY`/`ALL` are visible WORDS with
  `aria-selected` on the right one, the header reads
  `contains “ROGERS” and “WIRELESS”`, and the keyboard lap works — type
  `RCOM` + `Enter` adds a chip and clears the field, `Backspace` on the empty
  field removes it, a second `Backspace` removes the next one;
- **axe: 0 violations (0 critical / 0 serious / 0 moderate / 0 minor) in all
  four runs** — 390 and 1440 × dark and light, with the rule editor OPEN —
  evidence `frontend/scripts/a11y/m121-multi-term-probe.json` +
  `m121-rule-{390,1440}-{dark,light}.png`.

**Probe traps worth keeping:** (a) the two React events must YIELD between
them — firing `keydown` in the same synchronous task as the `input` event
reached the PREVIOUS `draft` and made `Enter` a no-op (first run, 31/37);
(b) the 422 envelope's `"code"` sits AFTER the FastAPI `detail` array, so a
truncated body hides it — the probe reads 4000 chars now.

**Reported deviations.** Card titled "M120" → documented as **M121** (M120 is
the receipts milestone). docs/35 (notifications) needed no reconciliation: a
bill appears there only as the `bill_due` `link`, which M121 does not touch.
AGENTS §4 step 4/5 could not run on this host (no docker, so no
`localhost:8080`) — the rebuild + smoke ran on the deployed stack instead
(example.internal deploy, prod probe above). The write-path probe uses a throwaway
tenant by repo convention (the demo tenant is read-only and no credentials
for the owner's account exist in the vault); the owner's own 12 bills were
verified at the data level instead (migration + read-only demo checks).

**Out of scope (pinned):** per-term negation (must-NOT-match) — explicit `+1`
if asked; regex terms — that complexity belongs to docs/04's rules surface.


## M122 — Bill-rule UX: ledger-backed suggestions, dry-run Test, rolling line, all-time/YTD stats

> Board card `t_fb581573`, titled "M122" — the first free number after M121.
> Spec: **docs/25 §M122**; wire in docs/08, UI in docs/09 §Rule suggestions &
> draft Test.

**What changed.** Four asks on the M116/M121 bill surface, none of them
needing a migration (every M122 field is derived at read time):

1. **`GET /bills/suggest-terms`** — match-term suggestions grouped off the
   caller's OWN committed payees by the matcher's own key
   (`normalize_payee_key`), ranked frequency-then-recency, excluding the
   payees the bill's CURRENT rule already matches (`bill_id`), following the
   editor's `match_mode` for a typed draft. The chips field becomes a real
   combobox/listbox (TagPicker a11y contract): quick-add chips on an empty
   draft ("ROGERS COMMUNICATIONS ×14"), a keyboard-walked listbox at >=3
   chars, nothing fetched for 1-2.
2. **Test (dry-run) in the editor** — both bill editors run the DRAFT rule
   through the existing `find-past` routes (which gain a `stats` block:
   first/last date, last-12-period sum, per-account counts over the WHOLE
   matched set) and render the shared `RuleTestPanel`, labelled "Draft
   preview - not saved" and cleared whenever the draft changes.
3. **Rolling-average line on the period chart** — `chart.points[].rolling_six`
   is computed server-side per point (trailing <=6-period mean, misses
   $0.00); the last point IS `rolling.six.mean` by construction. Drawn as a
   dashed accent polyline; the fallback table carries the column.
4. **All-time + YTD spend** — `detail.stats.all_time` (total, payments,
   per-payment mean, years-with-payments, yearly average) and
   `detail.stats.ytd` (calendar year, Jan 1 -> server today; future-dated
   rows never YTD), plus `ytd_total` on every `GET /bills` row. Missed periods
   never enter a total; the rolling windows keep their missed-$0.00
   semantics. The card is renamed "Spending stats".

**Verification (local).** Backend `tests/test_m122_bill_ux.py` 15/15 (real
Postgres): suggestion ranking/exclusion/tenancy/limits + the digits-only
draft, the privacy-mode answer and an AST pin that neither bills module
imports a network client; the find-past `stats` hand-recomputed with a
missed month and two accounts on BOTH routes; the rolling line
`[10.00, 0.00, 20.00, 30.00] -> [10.00, 5.00, 10.00, 15.00]` with the last
point == `rolling.six.mean`; all-time/YTD hand-recomputed (167.95 / 5 =
33.59, years 4, yearly 41.99, YTD 31.98) with a windowed read that narrows
`matched_count` but never `stats`; `ytd_total` on the list; the rule-edit
reactivity. Frontend `tests/m122BillSuggestions.test.tsx` 8/8 (the >=3 gate,
quick-add chips, the keyboard lap, Enter-without-highlight, the Test panel +
stale clearing, the chart aria + fallback column, the stats card);
`m116BillDetail` / `m121MatchTerms` / `billAutofill` reconciled (the term
input is a `combobox` now — that IS the a11y contract); full frontend suite
1068 passed + 1 skipped (the one `focusRing` hit on
`hover:border-accent/50` fixed to full-strength `hover:border-accent`);
full backend suite **1430 passed, 2 skipped, 0 failed**, run by hand (CI
billing outage, see docs/10 M106).

**Live probe (deployed stack).** `frontend/scripts/a11y/m122_bill_ux_probe.py`
— throwaway tenant, statement rows through the real import pipeline:
**29/29 checks**, axe 0 in six width × theme runs with the editor open (one
with the suggestion listbox open). Deploy was api+web on example.internal; the served
chunks carry the change (`useTermSuggestions-*.js`, "Spending stats" and
"6-period avg" in `BillDetail-*.js`); `alembic_version` stays
`0046_multi_term_match` (M122 needs no migration); CI refused the run the
known billing way (0 steps, no runner) — battery by hand, see M106.

**Out of scope (pinned):** per-term negation (still parked), rule sharing
across bills, charts beyond the single rolling line.

## M123 — Create from search: one menu, four targets, saved transaction views

**Deps: M12 (the `?q=` search view), M95 P1 (the palette), M108 P2 (saved
views), M116/M121 (the bill match rule), M46 (rules).** Spec **docs/09 §Create
from search** (reading: the four URL-addressable `/transactions` params ARE the
substrate — a saved view, a restore URL and a `?prefill=` deep link are the same
encoding).

### What it is

The Transactions page owns the only search in the app, and until now it dead-
ended: the rows on screen could not become anything reusable. A single
`Create ▾` control on the header row turns the CURRENT search (the `applied`
filter state + the fuzzy `q`, never the un-applied draft) into four things, and
a `Views (N)` row lists what came back:

| target | what it creates | precondition |
|---|---|---|
| **Saved view** | `{kind: "transactions"}` in `users.saved_report_views`, restore = a navigation to `/transactions?…` | none |
| **Rule** | `/rules?prefill=…` → `pattern`=`q`, `match_type=contains`, `category_id`=the search's category | none |
| **Bill** | `/bills?prefill=…` → `match_terms=[q]`, `match_mode=contains`, the category scope, amount blank | none |
| **Budget** | `/budgets?prefill=…` → the category pre-selected, period `monthly` | a REAL category filter |

Everything is a draft: nothing is created by landing, and each target prints
`Created from search: <what landed>` plus `… <fields> not carried into …` when
the search carried filters that editor has no column for.

### Decisions pinned (do not re-litigate)

- **Four params, not ten.** `q | category | account | tag` — the params
  `/transactions` already reads from the URL. The other bar fields (`from/to`,
  `payee`, `amountMin/Max`, `direction`, `reviewed`) are not URL state and not
  part of a saved view; the save panel NAMES them (`Not saved: date range, …`)
  rather than dropping them silently. `Apply filters` / `Clear all` write the
  four back with `replace: true` so the address bar is truthful after every
  change and a restored bookmark survives a reload.
- **One codec, three encodings.** `lib/createFromSearch.ts` reads and writes
  the same shape for the live state, the bookmark URL and the `prefill` deep
  link; the inbound `/transactions` effect now uses that same reader, so a
  malformed param degrades to "absent" instead of a `NaN` filter.
- **A prefill that drops something must SAY SO.** `provenance()` emits
  `Created from search: …` plus a `not carried` clause, resolved at render so a
  late-loading category name is still right. `uncategorized` counts as
  not-carried for every target: a rule/bill scopes or assigns a category
  **id**, a budget needs one.
- **Budget is the only disabled target** — `aria-disabled` (focusable, so the
  reason is reachable) with `Set a category filter first` /
  `Uncategorized cannot carry a budget` printed inline.
- **The menu is a menu; the lists are disclosures.** `aria-haspopup="menu"` +
  roving focus + arrows/Home/End/Escape on `Create`, plain
  `aria-expanded`/`aria-controls` on `Views` (docs/09 forbids Escape-closing
  this page's disclosures).
- **Reuse over parallel forms.** The save form and the rename/delete list were
  extracted to `components/ui/SavedViewPanels.tsx`, so the Reports header and
  the new Transactions control share ONE implementation of M108's whole-list
  PATCH instead of two that can drift.
- **No new backend surface for C1.** The view rides the existing whole-list
  `PATCH /users/me`; only `SavedReportView` grew — `kind: "reports" (default)
  | "transactions"`, cross-checked against `tab`, selecting which closed
  `state` key-set validates the row.

### Touch points

`frontend/src/lib/createFromSearch.ts`,
`frontend/src/components/transactions/CreateFromSearch.tsx`,
`frontend/src/components/ui/SavedViewPanels.tsx` (new),
`frontend/src/components/reports/SavedViewsControl.tsx`,
`frontend/src/pages/{Transactions,Rules,Bills,Budgets}.tsx`,
`frontend/src/components/layout/{CommandPalette,AppShell}.tsx`,
`frontend/src/types/index.ts`, `frontend/src/api/bills.ts`,
`backend/app/schemas/{users,bill_reminders}.py`,
`backend/app/api/bill_reminders.py`; docs/02, docs/08, docs/09, docs/25.

### Verification

- Backend: `uv run pytest -q` — 8/8 in `test_saved_report_views.py` (four new
  transaction-kind cases: round-trip across sessions, closed key-set, formats,
  `kind`/`tab` mismatch both ways) plus the full suite.
- Frontend: `npx tsc --noEmit`, `npx eslint . --max-warnings 0`, and
  `npx vitest run` — 104 files / 1077 tests green, including the new
  `m123CreateFromSearch.test.tsx` (17) and the untouched-by-regression
  `m108Reports.test.tsx` (20) that pins the extracted panels.
- `npm run build` (tsc + vite) green.
- **Live production probe** `frontend/scripts/a11y/m123_create_probe.py` against
  `https://fintrac.princessandmark.com` (the deployed nginx stack): **22/22** —
  the URL mirror, the menu ARIA + roving focus, the disabled budget with its
  reason, axe 0 critical/serious with the menu open, `kind="transactions"` on
  `PATCH /users/me`, restore-by-navigation, and all three deep links prefilled
  with their provenance lines. Plus `axe_scan.py`: **0 critical / 0 serious**
  over 6 pages × 2 themes. Evidence `m123-create-probe-prod.json`,
  `m123-*.png`, `m123-axe.json`.

**Reported deviations.**

1. **C2's "account" has nowhere to go.** The card says the rule form
   pre-fills "account/category", but `RuleCreate` has had no `account_id`
   since docs/04 defined the shape — a rule matches on a payee/amount pattern
   and ASSIGNS a category; it cannot scope by account or by tag. Rather than
   invent that dimension (backend engine + migration), `account`/`tag` are
   carried in the deep link only so the provenance line can say they were not
   carried. Same for bills and budgets — none has such a column.
2. **The bill form gained a field.** C3's "category (if set)" is invisible
   without one: `bill_reminders.category_id` has existed since M40 and the
   matcher reads it as a scope, but no form exposed it. The select, the PATCH
   `null`-clears rule and `category_id` on `POST /bills/{id}/find-past` are the
   minimum honest version (docs/25 §M123).
3. **No server constant needed raising.** The card anticipated raising
   `SAVED_VIEW_MAX_STATE_KEYS`; the transaction key-set is closed at 4, well
   under the existing cap of 8, so the constant is unchanged. What DID change
   is that reads now echo `kind` on every preset — the four report-view
   assertions in `test_saved_report_views.py` were updated to match.
4. **AGENTS §4 steps 4–5 could not run on THIS host** (no docker, so no local
   `docker compose up` and no `localhost:8080`). They ran where the running
   stack actually lives: `example.internal` was fast-forwarded to `dd190d1` (pre-deploy
   `5bbbaba`, kept in `/tmp/m123_pre_deploy_sha` for a one-command rollback) and
   rebuilt with `docker compose up -d --build api mcp web site`, then the probe
   ran through the real nginx edge at `https://fintrac.princessandmark.com`
   rather than a bare ASGI server — same as M121/M122. Steps 0–3 and 6 ran
   locally. Two probe-side notes: `fintrac…` is HTTPS only for the probe (the
   login sets a `Secure` cookie, which Chrome drops over plain `http`), and the
   SPA keeps its access token in memory, so the probe signs in THROUGH THE FORM
   exactly as m121/m122 do.

**Out of scope (pinned):** persisting the other seven bar filters in a view
(explicit `+1` if asked — the save panel already names them); account/tag
scoping for rules or bills; a `prefill` target other than the three named
(the codec already takes a target id, so a future `/imports?prefill=` is a
one-line addition).


## M126 — Live positions ticker, per-holding price history, chart ranges

> Board card `t_3bbc8cad`, titled "M126". Spec: this entry — endpoint in
> docs/08 §Price history, UI in docs/09 §Live prices & price history (M126),
> ranges/granularity in docs/12 §Range → granularity + §Axis thinning, stream
> consumer semantics in docs/18 §Frontend.

**What changed.** Three asks from the price/price-history family, none needing a
migration (every new field is derived at read time):

1. **L1 — the SSE stream patches a positions row's QUOTE.** `usePriceStream`
   writes `price` / `price_date` / `price_source` / `price_age_days` /
   `price_stale` into every cached positions payload through
   `setQueriesData({queryKey: ["positions"]})` — one patch for `["positions"]`
   and `["positions", accountId]`, so every surface rendering positions moves
   together — instead of waiting out the 60s cadence. The patch stamps
   `livePatchedAt` on that payload, which the table reads as *"totals update on
   next refresh (≤60s or on Refresh)"* and which disappears by itself when the
   next SERVER payload replaces the object (no timer, nothing to clear). The
   patched cell carries `.price-live-tick`, a 1.4s accent sweep on the semantic
   accent token (collapsed by the global reduced-motion rule), applied only
   when that row's own price changed. Market value, book cost and every total
   are never patched — valuation is server-owned (AGENTS §2). Invalidation
   moved AFTER the cache write: React Query treats any cache write as a
   `success` transition that clears `isInvalidated`, so patching last would
   cancel the very refetch the signature gate schedules.
2. **L2 — a position's own history, inline.** Clicking a row (or its chevron —
   `aria-expanded`, `aria-controls` while open, `Show <SYMBOL> price history`)
   expands the real `PriceHistoryChart` through the new `PriceHistoryPanel`:
   no navigation. The sparkline still rides the positions payload's `spark`
   (30 closes from ONE bulk server query), so rendering the whole table costs
   ZERO extra requests; an expansion costs exactly one `GET /prices/history`,
   cached by (security, window, granularity), so collapse → re-expanding costs
   none and a stream tick never refetches history. Loading → skeleton, failure
   → `ErrorPanel`, zero points → *"No price history yet"* — the header (title +
   range control) renders in every state, so the control never vanishes
   mid-request. The cell spans the table's own horizontal scroll region, so
   the panel is capped to `calc(100vw - 6rem)`: without the cap its
   right-aligned control sat outside the visible box at 390/412 and was
   reachable only by scrolling the TABLE sideways — which docs/09 §Range
   control forbids. The constant is measured, not guessed
   (`scripts/a11y/m126_panel_measure.py`: 86px of shell+card+cell chrome at
   both widths).
3. **L3 — ranges across the chart family.** `ChartRangeControl`
   (`1M/3M/6M/YTD/1Y/5Y/All`, `role="group"` + `aria-pressed`, `flex-wrap`)
   heads the price-history chart (default `1M`), the M118 account timeline
   (default `All`, only where the caller supplies no control — the cash view
   keeps its `RangePicker` and its free-form dates) and the bill period chart
   (default `All`, through a second `bill-detail-chart` query so the all-time
   stats and transactions table do not move). The Dashboard's net-worth
   granularity now follows the page range via `granularityForWindow` — the
   hard-coded `month` rendered a `1W` range as ONE bucket. **Granularity is one
   documented formula on both sides** (`ceil(span_days / unit_days) <= 150`,
   day-first: 1M → day, 6M → week, 5Y → month) in `lib/chartRange.ts` and
   `services/price_series.py`, pinned by the same table in
   `frontend/tests/chartRange.test.ts` and `backend/tests/test_prices_history.py`;
   `components/charts/thinning.ts::axisTickInterval` thins the x-axis from
   (points, viewport), pinned at 390/412/768/1280. Reports keeps its explicit
   granularity switcher — an explicit user choice beats the formula.
4. **New endpoint `GET /prices/history?security_id=&from=&to=&granularity=`.**
   Cache-only closes for one security, bucketed server-side and valued at the
   bucket END (M118's convention), the last bucket clamped to `to` so a close
   is never dated in the future; `404` for another tenant's security,
   `422 invalid_date_range`, coarsened past the 400-point roofline with
   `granularity` AND `requested_granularity` echoed back. It never fetches, so
   it works under privacy mode and has none of the overview route's backfill
   side effect.

**Verification (local).** `ruff format --check .` + `ruff check .` clean;
backend `pytest -q` **1444 passed, 0 failed** on real Postgres — including
`tests/test_prices_history.py` 10/10 (window coverage, the formula table,
bucket-end valuation, roofline coarsening, tenancy, the empty series, and a
"never fetches" AST/monkeypatch pin); frontend `tsc --noEmit` and
`eslint . --max-warnings 0` clean; full vitest green on every one of the four
pushes (107 files / 1107 passed at the M126 commit, 109 / 1138 after the M124
rebase, 109 / 1155 after M125 — the suite grew with siblings and never
regressed; new here: `tests/chartRange.test.ts` 12/12 and
`tests/m126PriceHistory.test.tsx` 11/11 — L1 patch/valuation-split/privacy-mode,
L2 zero-fetch/expand/cache/empty/viewport-cap, L3 seven options/`aria-pressed`/
`5Y` → month); `npm run build` OK. Drive-by: `ruff format tests/test_m122_bill_ux.py` —
pre-existing lint drift on `origin/main` that failed the repo-wide gate.

**Live probe (deployed stack).**
`frontend/scripts/a11y/m126_price_range_probe.py` — TWO tenants, because the
two halves need different things: a throwaway one for the price tick (the only
place a price can be moved on demand) and the demo one for charts that need
real data. Four deploys end-to-end on example.internal (each ff'd to `origin/main`, the
rollback point kept at `/tmp/m126_pre_deploy_sha`, then
`docker compose up -d --build api mcp web site`); the final one served
`dac51f0`, whose `Investments-*.js` carries both `positions-live-note` and the
`100vw-6rem` panel cap. The probe ran through the real nginx edge at
`https://fintrac.princessandmark.com`. **42/42 at 390×844 and 42/42 at
412×915** (`m126-price-range-probe-{390,412}.json` + 8 screenshots), axe **0
critical / 0 serious / 0 moderate / 0 minor in both themes** on `/investments`
with a row expanded, no console errors on either tenant, `alembic_version`
untouched at `0046_multi_term_match` (M126 needs no migration). Load-bearing
numbers from the evidence JSON:

- **251 real MSFT closes** backfilled into a **30-point sparkline with ZERO
  `/prices/history` requests** on render;
- a manual `POST /prices` moved the row's price cell in **4.8s** with
  `data-live-tick` set and the totals footnote up while the (probe-delayed)
  server payload was in flight; the footnote cleared **8.0s** later, on the
  server payload alone;
- expansion issued **exactly one**
  `?security_id=…&from=2026-09-09&granularity=day`, collapse → re-expand issued
  **none**, `5Y` issued `granularity=month&from=2021-10-09`;
- the bogus ticker printed **"No price history yet"** with no chart;
- Tab walked `1M → 3M → 6M → YTD → 1Y → 5Y → All` without leaving the group,
  and the group neither scrolled sideways nor left the scroll region — 294px
  wide with its right edge at 343 vs the region's 353 (316/365/375 at 412),
  wrapping `All` onto a second line exactly as docs/09 asks; the first cap
  (3rem) was measured OFF the deployed stack by `m126_panel_measure.py` and
  corrected to 6rem, because `scrollWidth == clientWidth` stays true even while
  a control is clipped — the probe now asserts `inside` as well;
- **the demo portfolio is 7/7 rows × 30 closes each** (RY.TO, BMO.TO, ENB.TO,
  VTI, VXUS, NVDA) — the acceptance's "every TFSA holding" measured where a
  real portfolio exists; Mark's own 60-row TFSA needs his credentials, which a
  probe must not guess, so the generic contract ("every row renders its
  `spark`, an empty series renders none") is unit-tested instead;
- demo Dashboard on `1W`: net worth went from **41 weekly buckets to 8 daily**
  (the pre-M126 hard-coded `month` gave exactly one); the M118 timeline and the
  bill period chart both offered all seven ranges with `All` selected;
- every screenshot was taken with the expanded panel open and centred
  (`shot_panel_open = [true, true]`) — axe scrolls the document while it
  scans, which is what the first evidence run photographed.

**Out of scope (pinned):** photo-real-time tick streaming (docs/18's 5s
interval already beats WS-tier for this app); the M109 forecasting overlay on
these charts (separate surface); `5Y`/`All` on the page-level `RangePicker`
(a different control with custom From/To — docs/12 names both); ANY
client-side recomputation of a money figure (valuation stays server-owned).


## M129 — Day delta + own-position impact (UP/DOWN and what it means for YOU)

> Board card `t_03035e58`, titled "M129". Spec: this entry — wire in docs/08
> (`/portfolio/positions` delta block + `/securities/{id}/overview`
> `latest.previous_close` / impact totals), UI in docs/09 §Day change &
> own-position impact (M129) + the M129 carve-out in §Live prices, stream
> semantics in docs/18 §Frontend.

**What changed.** Mark's ask, verbatim: *"if it's up or down and by how much
and the delta for your own shares if you have any, i.e. impact."* Two view
changes and one wire extension, no migration (every field is derived at read
time from the existing `prices` / `fx_rates` / `acb_pools` rows):

1. **Wire (S3, docs/08) — the delta block, server-computed.**
   `valuation.compute_positions` rows gain `previous_close_native` (newest
   cached close STRICTLY before the row the position is valued at — the same
   baseline `market_data.previous_close` reads for the security page),
   `day_change_native`, `day_change_pct`, `day_change_cad`
   (`cents(quantity × Δ × rate)` — the day's move on THIS holder's shares),
   `fx_rate_to_cad` (the exact rate used, `"1"` for CAD listings, `null`
   when no usable fx row — the only number a live tick may multiply by), and
   `unrealized_gain_cad` / `unrealized_gain_native` (`book_cost_cad ÷ rate`)
   / `unrealized_gain_pct`. `GET /portfolio/positions` adds
   `total_unrealized_cad` (Σ per-row gains; unknown-book rows excluded;
   `null` when no row computes one) for the Σ row. The overview's
   `latest` carries `previous_close` / `day_change_native` /
   `day_change_pct`, and `totals` gains the impact block as the arithmetic
   sums of the same per-position fields — headline and table can never
   disagree. None means *no data* everywhere: a first-day listing has no
   previous close and renders no delta, never a fabricated zero.
   Restructure note: the price ensure moved to ONCE per distinct held
   security (it used to run per (account, security) key where the second
   call was already a freshness-window no-op), and one `_price_series` scan
   now serves the sparkline AND the previous close so the two can never
   disagree about which row is newest.
2. **Security page (S1, docs/09).** The latest-price card gains the delta
   line — `▲ +7.42 (+1.45%)` vs the previous close, house-coloured,
   tabular — and withholds it entirely when there is no prior close. The
   new "Your position" impact card (suppressed when nothing is held — no
   zero-row fiction) reads *You hold 35.2491 MSFT · book $17,432.11 ·
   market $18,239.44 · +$807.33 (+4.63%) since first buy · +590.22 USD*
   with a *Today:* line from `day_change_cad`; the Holdings table's
   per-account rows now render the server's `unrealized_gain_*` (the old
   client-side `GainCell` subtraction is gone — anti-fabrication).
3. **Live tick (the documented M129 carve-out, docs/09 §Live prices).**
   The stream's overview patch keeps the delta baseline across a tick and
   stamps the client-only `livePatchedAt`; while it rides the payload the
   page re-evaluates the PRICE-DERIVED figures (day Δ, market value,
   unrealized, today) from `quantity × price × fx_rate_to_cad` — every
   multiplier server-provided — and labels the state "live". Book cost
   never moves, and the changed-payload gate now also invalidates
   `["security-overview"]`, so the next server payload replaces every live
   number and drops the marker. The positions TABLE stays outside the
   carve-out: its Day Δ / gain columns render server fields on the server
   cadence, and the footnote now names the whole gap (*"market value, day
   change, gains and totals update on next refresh"*).
4. **Positions table (S2, docs/09).** `Day Δ` after Trend; `Your gain
   (CAD)` + `Your gain %` closing the row — all three server-computed.
   Below `sm` the `Source · age` diagnostics column hides and the gain %
   folds under the amount (deltas are the priority, provenance is a
   diagnostic). The table gains a Σ row carrying the server's
   `total_unrealized_cad` with an `sr-only` "Total unrealized (CAD):"
   label; the other totals cells are empty on purpose (the payload computes
   no portfolio day Δ or quantity total, and the client will not invent
   one). One `PositionsSection` serves `/investments` and the account page.

**Tests.** Backend `tests/test_m129_day_delta.py` 9/9 — the delta arithmetic
hand-recomputed in every assertion (prev-close Δ, `cents(qty × Δ × rate)`,
native gain via `book ÷ rate`), first-day-listed honesty, Σ-total = Σ rows
with a poolless taxable row excluded, missing-fx degradation to `null`
(never a 5xx), overview totals == the position row's own fields, and the
unheld-security payload (delta present, impact block empty). Full backend
suite green. Frontend `tests/m129DayDelta.test.tsx` 10/10 — the delta line
from the server's fields, the withheld line with no previous close, the
impact card's server numbers, suppression when unheld, the live tick
(re-evaluated market/gain/delta + "live" label, book cost untouched, and
NO live claim for an unchanged payload), the table columns + Σ row, the
unknown-book em dash, and the below-sm column contract.

**Live evidence.** `frontend/scripts/a11y/m129_delta_probe.py` through the
real nginx edge at `https://fintrac.princessandmark.com`, against the example.internal
stack ff'd to `85126a5` (`api` + `web` rebuilt, all containers healthy — the
deployed `Investments-*.js` carries the new delta strings): **22/22 at
390×844 and 22/22 at 412×915** (`m129-delta-probe{,-412}.json` + 6
screenshots), axe **0 critical / 0 serious / 0 moderate / 0 minor in 12
scans** (investments + security + demo-security × 2 themes × 2 widths), no
console errors on either tenant. Load-bearing numbers from the evidence:

- the throwaway tenant's wire (pre-render) read exactly the hand-computed
  contract — `previous_close_native=100.00000000`, `day_change=2.00000000`
  (+2.00%), `day_change_cad=20.00`, `unrealized=120.00` (+13.33%),
  `total_unrealized_cad=120.00` — and the security page rendered the same:
  *▲ +2.00 (+2.00%) vs previous close* and *You hold 10 M129CAD · book
  $900.00 · market $1,020.00 · +$120.00 (+13.33%) since first buy* with
  *Today: +$20.00 — 10 × +2.00 CAD per share*;
- the on-demand tick (a manual `POST /prices` moving the close 102 → 130
  while a 4s delay held the overview refetch) re-evaluated the card LIVE to
  *market $1,300.00 · +$400.00 (+44.44%)* and the delta line to *▲ +30.00
  (+30.00%)*, with the "live" badge on both — and **book cost stayed
  $900.00**; the server payload then landed on its own and the badges
  disappeared (marker lifetime, no timer);
- the one-close security rendered **no delta line at all**, the no-trade
  security rendered the delta line with **no impact card**, and the table at
  390 showed `Source · age` + `Your gain %` as `display:none` on headers
  AND cells with the percent stacked under `+$400.00`;
- the DEMO portfolio (7 rows): every row carried a Day Δ or an honest "—",
  and the Σ row equalled the hand-sum of the rendered row gains **to the
  cent** (95551.96 = 95551.96 at 390; 97185.35 = 97185.35 at 412 — the
  demo's own prices moved between the runs, and each footer matched its
  run); RY.TO's page showed the real thing: *▲ +2.28 (+0.84%)* and *You
  hold 362 RY.TO · book $49,362.40 · market $98,880.30 …*;
- the probe's first run FAILED 3 checks with stale pre-tick expectations and
  crashed on an axe-after-navigation ReferenceError, and its light theme
  caught axe mid-`transition-colors` (a 1-serious contrast finding on
  PRE-EXISTING elements). The probe was fixed — post-tick expectations,
  `load_axe` per document, a 0.7s theme settle, and full contrast detail
  capture — and the clean 22/22 × 2 above is what remains; the contrast
  finding did not reproduce with the settle, so no product CSS changed.

**Probe correctness (2026-10-10).** The tick window described above did not
exist. `m129_delta_probe.py` bound its `window.fetch` delay on the *login*
document, and the `tab.navigate()` to the security page built a NEW document
that took the wrapper with it — so the "4s delay" was a no-op and the probe
was sampling an unguarded race. Measured on the deployed build at 200 ms
sampling (3 instrumented runs, `m129-tick-timeline.json`): the live badge
appears with the patched numbers and lives **~400 ms** before the
invalidation-triggered refetch replaces the payload, while the probe polled
every ~450 ms — a coin flip, which is exactly the 20/2 then 22/0 on one
unchanged build that opened card `t_bed5252d`. The product itself was never
at fault: the badge, the re-evaluated numbers and the marker's lifetime all
behaved as designed in every run. The probe now (a) arms its gate through
`Page.addScriptToEvaluateOnNewDocument` — the pattern the m126 and m130
probes already use, so no navigation can silently disarm it — (b) **PARKS**
the `/securities/<id>/overview` payload instead of delaying it, so the
marker cannot be wiped while the probe samples (`m129-tick-timeline-v2.json`
shows the marker holding for 9.6 s until the probe releases the payload),
and (c) waits for the page's own *"stream message …"* line before the
upsert, so a dead stream is named before the tick is judged. Result:
**24/24 on four consecutive runs at 390×844 plus one at 412×915** on the
`1b825fb`-labelled stack (the probe now carries 24 checks), and the gate is
still a gate — with the EventSource stubbed out the badge never appears
across 80 samples / 32 s and the tick checks fail
(`m129-negative-control.json`, driver in `m129_negative_control.py`).

**Out of scope (pinned):** sortable delta columns (the card says so);
realized gain (the ACB engine's surface); a dividends-only impact view
(the Dividends tab exists); a portfolio-level day Δ total (**delivered by
M130** — `total_day_change_cad` / `total_day_change_pct`, see below); patching
the positions TABLE's derived columns live (they are server-cadence per docs/09
§Live prices — M130's header card is inside the M129 carve-out, the table
still is not).

## M130 — Portfolio total view (value, day change, range chart)

> Board card `t_0da6af9f`, titled "M130". Spec: this entry — wire in docs/08
> (§Portfolio value history, §Portfolio summary on `GET /portfolio/positions`),
> UI in docs/09 §Portfolio header, chart family in docs/12 §Time bucketing +
> §Range → granularity.

**What changed.** Mark's ask, verbatim: *"a total view on /investments with up
and down for the day (or perhaps selection of range) with an overall chart
too."* Two payloads and one card, no migration (every figure is derived at read
time from the existing `prices` / `fx_rates` / `transactions` / `trades` rows):

1. **Wire A — the header's numbers, single-sourced (S3, docs/08).**
   `GET /portfolio/positions` gains `total_value_cad` (Σ of the `Market value
   (CAD)` column, computed from the SAME `items`), `cash_cad` (docs/05's cash
   leg read at today — `starting_balance + Σ committed transactions + Σ docs/05
   trade cash effects`, at the cached rate; `null` when a rate is absent or past
   the M115 ≤7d window), `portfolio_value_cad` (their sum, added SERVER-side),
   `total_day_change_cad` / `total_day_change_pct` (Σ of the same per-row
   `day_change_cad` the `Day Δ` column prints, over yesterday's level),
   `day_change_as_of` (the most recent prior-close DATE — the honest
   "vs Oct 7 close" label) and `day_change_mixed_baseline` (the rows disagree
   about which market day that was). Each row additionally carries
   `previous_close_date`, the DATE behind M129's `previous_close_native`.
   Every field is a sum over the response's own `items`: `Σ(items) ==
   total_value_cad` holds by construction, so the header and the Σ row cannot
   drift, and the Σ row now renders the day Δ % and the CAD total it was
   previously forced to leave as an em dash.
2. **Wire B — the chart (S4, docs/08 §Portfolio value history).** New
   `GET /portfolio/value-history?from=&to=&granularity=&min_event_value=`
   runs M118's `account_value_history` once per investment account and SUMS the
   buckets — one valuation chain, no cache table, derived every read. Two merge
   rules: **one granularity** for the whole series (the coarsest any account
   reached — a six-year TFSA coarsens to `week` where a three-month RRSP would
   stay on `day`, so the summed x-axis must not mix widths) and **absent =
   zero, present-and-unvalued = null** (an account that had not started held
   nothing; an account that cannot be valued makes the sum unknown, never a
   zero). Markers are M118's, concatenated and **deduped by event id** with
   `account_ids` / `account_names` carried on each, so one event spanning two
   accounts is ONE marker naming both. New `stats` block: the window's own
   move, `last − first` over this response's valued points, with the two dates
   it subtracted — the number the header's "over 6 months" line renders.
3. **The card (S1/S2, docs/09 §Portfolio header).** `PortfolioHeaderCard` sits
   FIRST on `/investments` (portfolio view only): portfolio value (+ a `live`
   badge), the day change with its real date, the range sentence, a cash line
   that appears only when there is cash to explain, then the chart. The chart
   is M118's **drawing body** (`AccountValueChartBody`, exported, fed through a
   `ValueChartPayload` interface that is the intersection of the two wire
   contracts) — same chrome, markers, honest gaps and `<details>` table. ONE
   `ChartRangeControl` in the header drives both the sentence and the line,
   because `range` is owned by the card and handed to the body as
   `headerExtra`.
4. **Live tick (the documented M130 carve-out, docs/09).** While the stream's
   `livePatchedAt` rides the positions payload, `lib/impact.ts::livePortfolio`
   re-evaluates the headline and the Δ from server primitives (`quantity`,
   patched `price`, `previous_close_native`, `fx_rate_to_cad`) and both say
   **live**; cash never moves with a quote and `day_change_as_of` stays the
   server's pinned baseline. Next server payload replaces everything — no
   timer.

**Tests.** Backend `tests/test_m130_portfolio_value.py` 13/13 — the
anti-fabrication identity (Σ of the rows == `total_value_cad` == the header's
holdings basis, headline == that + cash), the day change hand-recomputed
against a prior-close sum (14,000 − 12,600 = 1,400) with the Boxing-Day
carry-forward labelled by its real date and the mixed-baseline flag, the
portfolio series bucket-for-bucket equal to the two per-account responses,
`stats` equal to the chart's own first vs last point, marker dedup across two
accounts, the one-granularity reconciliation over a six-year vs three-month
floor, scope exclusion against a chequing account carrying 999,999.00, staging
exclusion, the 422/cache contracts and the empty state. Full backend suite
green (1505 passed). Frontend `tests/m130PortfolioHeader.test.tsx` 8/8 — the
header/Σ-row text equality, the cash line's appearance rule, the withheld day
line, the mixed-baseline caption, the live tick's hand-recomputed 15,000 /
2,400 / +19.05%, and the range selector re-requesting its window with both the
sentence and the data table following THAT response. Full frontend suite green
(1173 passed, 1 skipped).

**Live evidence.** `frontend/scripts/a11y/m130_portfolio_header_probe.py`
(AGENTS §4 step 5) through the real nginx edge at
`https://fintrac.princessandmark.com`, against the example.internal stack ff'd to
`6daed91`: **40/40 at 390×844 and 40/40 at 412×915**
(`m130-portfolio-header-probe.json`, `m130-portfolio-header-probe-412.json` +
6 screenshots), axe **0 critical / 0 serious / 0 moderate / 0 minor in 8
scans** (investments × 2 tenants × 2 themes × 2 widths), no console errors on
either tenant. Load-bearing numbers:

- the throwaway's wire, hand-checked: Σ rows == `total_value_cad` ==
  `portfolio_value_cad` == 14,000.00 (cash 0.00), day Δ 1,400.00 / 11.11%
  with `day_change_as_of` = today−4 — the Boxing-Day fixture's carried-forward
  baseline — and `day_change_mixed_baseline=false`. The card rendered exactly
  *$14,000.00 · ▲ +$1,400.00 (+11.11%) · vs Oct 5 close* (never "yesterday"),
  and the Σ row printed the same strings;
- a manual `POST /prices` (100 → 110) while the refetch was held moved the
  header LIVE to *$15,000.00 · ▲ +$2,400.00 (+19.05%)* with the `live` badge,
  the baseline caption and the range sentence both unmoved (the sentence
  belongs to the value-history response, which carries no quote) — the server
  payload then replaced everything and the badge disappeared, no timer;
- `6M` re-requested rather than relabelled: *Over 6 months: +$2,400.00
  (+19.05%) Sep 9 → Oct 9* over a 33-row focusable data-table fallback;
- the DEMO portfolio (real holdings, accounts 77/78/79): header
  **$227,739.75** == Σ 228,601.68 + cash −861.93 == `portfolio_value_cad`,
  day *▲ +$1,633.38 (+0.72%) vs Oct 8 close*. Its `All` response — captured at
  `GET /portfolio/value-history` with no window, the request the card makes —
  runs **2025-10-02 → 2026-10-09 in 373 day-buckets**, `change_cad`
  227,739.74 and `change_pct: null` (the window starts at zero), so the
  sentence prints the CAD move and **no** percentage: the two halves of
  docs/09's range sentence rendering independently. `1M` answered
  **2026-09-09 → 2026-10-09, +$89,736.53 (+65.02%)** — the two windows differ
  because the server says they do (`from_bucket` 2025-10-02 vs 2026-09-09),
  not because a label claimed it;
- layout at both probe widths stayed inside the viewport: 390 shows
  `head_right 370 / value_right 353 / day_right 353` with the value stacked
  above the Δ, 412 `head_right 392 / overhang null`, `outside: null` at both;
- the real portfolio, measured server-side in the deployed `api` container
  (`_positions_summary`, the function the card is single-sourced from), user
  343: 14 rows, Σ 1,021,759.37, cash −3,872.73, **`portfolio_value_cad`
  1,017,886.64**, day **+4,991.72 (+0.49%) vs Oct 8 close**, mixed baseline
  false. The card's DOM half of that figure is asserted on the demo and
  throwaway tenants (40/40 × 2), not on the private account. The board's
  acceptance pin **286,114.92 matches no total on this deployment** — it is a
  TFSA-only intraday figure (≈287,104.38 when last measured), 0.45% off the
  account's own Σ — so the number above is the portfolio the header actually
  renders.

**The probe's own bugs, fixed in this pass (harness, never product).** Three
of them could have produced a green number that meant nothing:

1. `a11y_driver` pins ONE `--user-data-dir` for every `Browser`, so the demo
   browser inherited the throwaway's session and drew ITS chart beside the
   demo's positions — a query cache keyed only by window, both tenants sharing
   the key. The profile is now wiped between browsers (`profile.wipe` in the
   report); `Network.clearBrowserCookies` + `localStorage.clear()` alone were
   measured not to be enough.
2. The wrapper kept only the NEWEST value-history response, and
   `PortfolioHeaderCard` sets `staleTime: 60_000` — so the window the card was
   actually rendering (fetched while the document still reached
   `readyState=complete`, before any probe could wrap `fetch`) was never
   captured, and the check compared the sentence against a LATER window. The
   wrapper source is now armed as a CDP `Page.addScriptToEvaluateOnNewDocument`
   init script, so the first fetch of every window is one it sees; the check
   looks the sentence's response up by its own buckets and signed change
   instead of trusting last-write-wins; and the probe's own diagnostic reads
   go through the unwrapped fetch so they can never pose as one.
3. The sentence prints its buckets WITHOUT a year ("Oct 2 → Oct 9"), so age
   now comes from the server's ISO `from_bucket` — reading "Oct 2" as 2026
   dated the demo's 2025-10-02 floor to this year and called a year of
   history a week old.

**No product code changed in this pass**; the 40/40 × 2 above is the app as
shipped in `85c1610`/`6daed91`, with docs/09 gaining two corrections alongside
(this entry's query-key row, and the independent CAD/pct halves of the range
sentence).

**Out of scope (pinned):** per-security histories (M126/L2 owns sparklines and
the expand); a forecasting overlay on the portfolio curve (M109 keeps its own
page); any new aggregation cache (the derived-every-read rule holds); a
cross-currency "Market value" Σ in the footer (the payload computes no such
total and the client will not invent one); suppressing a transfer's two legs
into one portfolio marker (they are two trade rows with two ids — the dedup key
is the event id, docs/08 §Portfolio value history).

## M136 — Wave-13a: locked-fixture tenant, VaultLocked, A18 fix, G1–G3 pre-emptive write gates

> Board card `t_22c97749` (canonical of the M134 pivot's five-item order,
> P0-a..0-d + P0-3). Spec: docs/33 §Server-first computing (the rule block,
> §5.1 G1–G3 rows, §Locked-fixture tenant) + docs/09 §Empty-state choreography
> (the A18 partial-state contract). The M134 audit behind it is
> `SERVER_FIRST_PIVOT.md` (attached on card `t_51aced51`).

**What shipped.** Wave-13a of the server-first pivot, in M134's severity order
(0-a unlocks everything else; 0-b/0-c are the honesty copy for the locked
case; 0-d proves the claims; P0-3 destroys user work and had to ship in this
wave, not later):

1. **§0-a — the locked-fixture tenant.** `backend/app/services/locked_fixture.py`
   seeds `locked-fixture@fintracker.local` with `encryption_required=TRUE`, 2
   accounts (both `name_cipher`-carrying), and 8 transactions (7 with a full
   cipher bundle, 1 deliberately plaintext for docs/33 A16's mixed-mode
   contract) — and **no `vault_envelopes` row**, so every browser starts
   LOCKED with zero per-device setup while `useVaultEnrollment()` still answers
   "enrolled" (the write guard alone proves a vault story). Seeded on demand
   (`backend/scripts/seed_locked_fixture.py`, or the wave's own test run),
   NEVER from the lifespan, gated by the same `SEED_DEMO_ACCOUNT` kill switch
   as the demo. Pinned by `backend/tests/test_locked_fixture_tenant.py`
   (**7 tests**, real Postgres).
2. **§0-b — `VaultLocked` + `useUnlock()` + ≤4 copy constants + the shell
   chip.** `frontend/src/components/e2ee/VaultLocked.tsx` is the ONE component
   every locked surface renders through: three variants (`status` chip, `partial`
   with a measured withheld count, `blocked` in place of a control that cannot
   run), the FOUR lock-state constants (search/write/client/status), and the
   canonical destination string
   `Settings → Privacy & data → Client-side encryption (in progress)` carried
   verbatim. `useUnlock.ts` lifts `unlockVaultSynced()` out of Settings so
   every surface unlocks IN PLACE (rule 7c), and `auth/authUser.ts` carries the
   session-user scope outside `AuthContext` so leaf components read
   `encryption_required` without eighteen test files re-declaring the export.
   The four divergent lock phrases (AppShell, SimilarExamples,
   ScheduledBackupPanel, Settings) collapsed onto the constants. The shell
   chip is gated on enrollment (a "locked" claim for a tenant that never
   enrolled would be false, rule 7a).
3. **§0-c — the A18/F4 fix.** The S5 search view (`Transactions.tsx`) renders
   the `partial` variant with a MEASURED withheld count (cipher rows in the
   population the scorer actually read) and an inline unlock as the primary
   action — replacing the "No transactions match these filters." / "Clear all"
   dead end, whose action deliberately preserved `q` (uxbot finding 1).
   `scripts/locked_vault_gate.py` moved the A18 row from KNOWN_RED to WORKS.
4. **§P0-3 — the G1–G3 pre-emptive write gates.** SplitEditor, the bulk
   matched-rename and the YNAB commit each detect the doomed write BEFORE
   submission and replace the control with `VaultLocked blocked` (the 422 stays
   as defense-in-depth; the YNAB card also branches its commit error on the
   envelope's `code`, so an `encryption_required` 422 tells the lock story and
   any other failure keeps the raw envelope).

**Evidence (numbers, this wave).**

- Frontend suite: **113 files, 1177 passed / 1 skipped, exit 0**
  (`NODE_ENV=test npx vitest run`), plus the new
  `tests/lockedVaultWriteGates.test.tsx` — **14 cases** across the three
  surfaces (enrolled+locked → gated pre-emptively with zero API calls;
  enrolled+unlocked → the control returns on `vaultchange`; NOT enrolled →
  the gate is silent, because a plaintext tenant's write cannot 422), and the
  VaultLocked contract block (canonical destination verbatim on blocked and
  partial; the status chip names no surface; the partial sentence is built
  from measured counts).
- Backend: `tests/test_locked_fixture_tenant.py` — **7 passed** on real
  Postgres (`fintrack_m136a_test_p59970`): enrollment + lock-by-construction,
  idempotence, `/users/me` carrying the write guard, the G1 split-save 422 →
  then 201 with per-part `amount_cipher`, the G3 YNAB-commit 422, the signed
  sum (3428.08 across 8 rows), and the kill switch.
- A bug the new tests caught and this wave fixed: the `status` chip rendered
  the CAPABILITY's copy ("Unlock the vault to search encrypted payees") as if
  the shell were a search surface — it now renders the `status` constant
  ("Vault locked"), per rule 7a (state, never a claim about a surface).
- **Live locked-browser QA (0-d), deployed stack + 0-a's tenant: 8/8 PASS**
  (`frontend/scripts/a11y/m136_locked_qa.py`, headless Chrome against
  `127.0.0.1:18080` after `docker compose up -d --build api web` → example.internal
  `891bad6`, containers healthy, alembic `0047_trade_currency` = head). The
  fixture was seeded inside the running api container (id=463,
  `encryption_required=True`) and every surface rendered its locked state:
  the shell chip ("Vault locked" exactly), S-1's popover (the shared search
  string + canonical destination), the A18 search view (**"Showing 1 of 8 rows
  — unlock to search the other 7"** — the count is the fixture's real 7
  ciphered + 1 plaintext row), rule 7f (no stacked empty state), the inline
  unlock present but not auto-revealed, S-3's similar popover, and G1's
  split editor (the blocked notice with NO submit button). The server halves
  were exercised too: login 200, `/users/me` `encryption_required=True`,
  plaintext `POST /transactions` → 422 `encryption_required`, YNAB commit →
  422 `encryption_required`, `GET /transactions` → 8 rows / 7 ciphered.

**Out of scope (pinned):** the matcher port (M137/P0-1); the reports flips
(P0-2); the deprecation flags (P1-1/P1-2); the retired client mirrors (P2-5);
F2 (logout does not lock the vault — remains tracked in docs/33 §key-handling
inventory).
## M139 — Display masking: `⌘⇧H` hides every dollar figure

> Board card `t_c5648ac6`, titled "M139". Spec: `docs/46-display-masking.md`
> (the contract), UI in docs/09 §Rail, privacy boundary in docs/11.

**What changed.** The ask: hand someone your laptop — or turn toward a camera —
without the balances walking out with them. A **render-time** mask replaces every
absolute CAD/USD figure with `•••••`, toggled by a shell chip or `⌘⇧H`, persisted
in `sessionStorage` only.

1. **It is a presentation fact, not an egress gate** (docs/46 §D5). `mask.ts`
   imports nothing from the API client, no request carries it, and `users` gained
   no column. `docs/11` privacy mode is untouched — M139 renders what privacy
   mode already decided to send.
2. **Restore-on-load is OFF, deliberately** (§D1). A boot clears any stale key
   and starts unmasked; `AuthContext::applyUser` calls `initMaskForSession()` on
   every identity change so the mask dies with the user it belonged to. The demo
   tenant (a public storefront) boots masked via `applyDemoMaskDefault()` — a
   *default*, not a lock, and any explicit user choice wins.
3. **One subscription in `AppShell` re-renders the routed tree** (§D3). `useMasked()`
   sits once in the shell; `⌘⇧H` is handled inside the shell's *existing* keydown
   listener (not a second one — the M72 duplicate-listener bug class). Formatters
   are fail-closed: an omitted flag means "consult the ambient mask", so a new
   call site masks by default. This mechanism rests on the codebase having **no
   `React.memo` and no `useMemo` around money formatting** — pinned by
   `tests/m139MaskChip.test.tsx`, because introducing either near a money
   component would silently stop the flip from reaching the page.
4. **Percentages stay visible; amounts mask** (§D2, operator ruling). A
   percentage is proportional and leaks no scale: budget % remaining, donut slice
   %, spending-change %, growth/period delta % and TWR/MWR are all ruled-shown.
   The single carve-out is **book-cost-derived** percentages — the position-level
   "Your gain %" on `/investments` and SecurityDetail's book-vs-market gain —
   because `%+4.63` beside `••••••` hands a shoulder-surfer back the scale the
   mask removed. Those render through `fmtSignedPctBookCost`, a separate named
   function rather than a boolean flag: the two differ semantically ("is this
   ratio derived from a masked absolute?") and a flag at the call site says
   nothing about which it is. It *follows* the session like an amount, so an
   unmasked user still sees the figure.
5. **Working surfaces do not mask** (§D4): the import wizard, Reconciliation,
   Settings, receipts, and export confirm previews are pinned `masked: false` —
   masking a field the user is typing into is hostile, not safe. The Settings LLM
   test panel's *echo* is the exception (it prints a figure back), and
   notification inbox bodies run through `maskAmountsInText` so amounts inside
   prose mask while the sentence, account name and date stay readable.

**The Recharts trap.** An early cut gave `formatAxisMoney` an optional
`masked?: boolean` second parameter. Recharts calls `tickFormatter(value,
index)`, so the tick's INDEX — truthy for every tick but the first — was read as
a mask flag, and an unmasked chart drew `$0`, then `•••••`, `•••••`, `•••••`.
The axis formatter now takes exactly one argument; the regression is pinned in
`tests/reportMoneyAxes.test.tsx`.

## M140 — Mask flips are real-time on every money surface, with zero re-fetches

> Board card `t_f6612f5b`, titled "M140". Spec: `docs/46-display-masking.md`
> §D3 (corrected) + §D6 (the rule and the gate), UI contract in docs/09
> §Rail.

**The bug (Mark, live):** *"hitting the mask/unmask does not happen in real
time, needs a page refresh."* M139's store and formatters were right; its
**propagation** story was wrong, and the wrong half is the half a user feels.

1. **M139's mechanism was the defect.** §D3 of this roadmap claimed one
   `useMasked()` in `AppShell` re-renders the routed tree under `<Outlet/>`. It
   does not, and the reason is React Router rather than React:
   `createBrowserRouter` builds each route's element **once**, at module load, so
   `<Outlet/>` hands React the same element object every render and React bails
   out of a referentially unchanged subtree. Everything the shell itself renders
   flipped instantly — the chip, the rail, the account menu — while the routed
   pages kept the old figure until the next query or cache event happened to
   re-render them. A page reload "fixed" it only because a reload re-renders
   everything. docs/46 §D3 is corrected in place rather than left standing.
2. **The fix is per-component subscription, swept mechanically.** All **63**
   money-rendering components now call `useMasked()` as their first statement;
   **61** did not. The helpers stay the single source of truth (they read the
   ambient mask at call time) — the components just re-render on a flip. No prop
   threading, no context, no second store, and no re-fetch: the mask lives below
   the data layer, so a flip fires no request.
3. **`scripts/sweep_money.mjs --check` is the gate**, run as
   `tests/m140MaskSweep.test.ts` so the ordinary `vitest run` fails on a
   regression. It walks the AST and fails on four things: a money component with
   no subscription; money built inside `useMemo`/`useCallback`/`useEffect` (a
   re-render does not re-run those); a currency formatter that bypasses the
   helpers without consulting `isMasked()` itself; and money formatting at module
   scope. `scripts/hook_call_guard.mjs --check` covers the inverse — a subscribed
   component is a hook consumer, so a plain `Name(...)` call of one is an
   "Invalid hook call" TypeScript will not catch. Both carry a vacuous-pass floor
   so a broken walker fails instead of reporting nothing.
4. **The sweep found two things the report did not mention.** `fmtDelta` in
   `pages/Dashboard.tsx` — behind every KPI delta chip — formats currency itself
   and never asked the mask, so a masked dashboard printed `$200` beside a masked
   net worth. And the **SSE live-tick path** (M126/M130) re-renders on every
   quote, which is exactly why it needed the subscription: a tick on a masked
   session must re-render the recomputed headline as bullets rather than flash a
   real number. Both are now pinned, the second with a real `EventSource` tick in
   `tests/m140MaskFlip.test.tsx`.
5. **Recharts memoizes its axes**, and `chrome.moneyAxis` is the stable module
   function `formatAxisMoney` — so an unsubscribed chart kept drawing real dollar
   ticks under a mask. Subscribing the chart defeats the memo;
   `tests/m140ChartAxis.test.tsx` reads the tick strings back out of the SVG and
   asserts the date labels survive.

**Zero re-fetches is asserted, not assumed.** The flip tests fingerprint every
mocked API's call count and require it unchanged across mask *and* unmask, on
`/investments`, `/accounts` and the Dashboard — no `invalidateQueries`, no
refetch, no session re-probe. `frontend/scripts/a11y/m140_mask_flip_probe.py` is
the same question against the deployed stack, counting Chrome's own
`Network.requestWillBeSent` events during the flip.

**Out of scope (pinned):** threading `masked` explicitly through every call site
(the helpers' ambient read plus a re-render is the contract, and 273 explicit
flags would be churn without a behaviour change); and any change to what masks
(D2's rulings, including the book-cost carve-out, are untouched).

## M137 — Wave-13b: server-first matcher (P0-1), report flips + budgets (P0-2), orphan flag (P1-1), preview + subscriptions re-mount (P1-2)

> Board card `t_a0a74180` (canonical of M134's 3.6.1/3.6.2/3.6.3; the M136
> prerequisite merged as `891bad6`). Spec: docs/33 §Server-first computing
> (the REVERSED rows + §Locked-fixture tenant's A2/A3/A10/A12/A13 rows),
> docs/08 (every route shape), docs/34 (the one budget that moved).

**What shipped.** Wave-13b closes M134's client-compute inventory: the
~4,200 lines of browser math that duplicated capabilities postgres already
has natively.

1. **P0-1 — the matcher went back to the server.** `GET /transactions?q=`
   (ranked page, `total` = hit count, `score desc, id desc`),
   `GET /transactions/similar` (the M119 popover: `exclude_id`,
   `categorized_first=true`) and the `/search` transaction group (blind-index
   token first at 1.0, pg_trgm fills the window) all rank on
   `greatest(similarity, word_similarity) >= 0.3` over `normalized_payee`
   via `app/api/_search_helpers.py` — the same helper
   `services/categorization` scores with, behind `ix_trgm_transactions_payee`.
   **One predicate, three surfaces**: `q` is a first-class `TxnFilters` field
   thresholded inside `_ledger_scope`, so the list, the "Needs review" chip,
   a bulk run over the matched set and `unreviewed-count` can never disagree
   about what "matched" means. `q_removed` 422 is gone everywhere.
   The 285-line TypeScript port (`lib/searchMatch.ts`) is NOT deleted — it is
   the E2EE/offline fallback, and its vectors MOVED (not vanished) into
   `backend/tests/test_search_goldens.py`: 21 pg_trgm score vectors + the 10
   live s5 goldens replayed against the re-mounted endpoint (green).
   Wired: `AppShell.tsx` ⌘K (one server query, `limit=10`), `Transactions.tsx`
   (`/transactions?q=` — the full-ledger `substrate` download is gone from the
   search view), `SimilarExamples.tsx` (one `/transactions/similar` query
   instead of a ledger download + 285-line local rank).
2. **Lock withholding is a CLIENT display request, not a server guess.**
   `hide_encrypted=true` on all three surfaces + `withheld: int` on
   `TransactionPage`/`SimilarPage`/`SearchResults` (default 0 — existing
   payloads unchanged) + `encrypted: bool` on `SimilarTransaction`. A cipher
   payee is plaintext on the server and ALWAYS matches; the locked client is
   what withholds it, and `withheld` is the measured number the M136
   `VaultLocked partial` copy prints. Non-E2EE tenants have no cipher rows →
   `withheld: 0` → no lock copy (uxbot rule 1: gate on excluded data, not
   lock state). M119's honesty copy survives in the M136 component.
3. **P0-2 — the seven shadowed computes are thin reads of their own routes**
   (`/reports/spending`, `/spending/categories`, `/category-tag-matrix`,
   `/cashflow`, `/net-worth`, `/investment-performance`, `/capital-gains`);
   screen shapes unchanged. **Budgets is the flagship**: `api/budgets.ts` no
   longer discards the server's `spent`/`carried_in`/`remaining`/`pct_used`
   and recomputes them client-side — the page is ONE small `GET /budgets`,
   no `/transactions?all` in the network panel at all. The M96 recurring
   label moved to `GET /budgets/recurring-summary` (one indexed scan), which
   is what finally let `api/s3Inputs.ts::loadCommittedTransactions` — the
   pivot's single biggest lever — be deleted.
4. **P1-1 — four orphan `/reports/*` routes behind a flag.**
   `SERVER_REPORTS_DEPRECATED` in `.env` (default OFF — unset keeps them
   answering) gates exactly `/dividends`, `/cashflow-sankey`, `/total-return`
   and `/networth-snapshot` with **410 `report_route_deprecated`** naming the
   route; `assert_report_route_live()` refuses anything outside the
   four-orphan allowlist, so a typo can never 410 a live screen route. Both
   states asserted.
5. **P1-2 — the two M48-S3 deletions REVERSED deliberately.**
   `POST /rules/preview` runs the SAME `services/rules_engine` predicate
   `POST /rules/{id}/apply` writes with (a preview and its apply can no
   longer disagree), and `GET /subscriptions` re-exposes
   `services/subscriptions.py::detect_subscriptions` (the engine
   `bill_reminders`' cash-flow calendar called throughout) with the removed
   route's roll-up. docs/33's A10/A12 rows changed from "removed" to
   "server-first server route"; both routes are parametrized over
   `encryption_required` ON/OFF (the P1-5 pattern).

**Evidence.** Backend ruff clean; `pytest` against real Postgres: every
M137-touched file green in the full run (test_search, test_search_goldens,
test_report_route_deprecation, test_budgets, test_rules_preview_apply,
test_subscriptions, test_m125_bulk_matched, test_split_transactions,
test_cat_transactions, test_perf_budgets). The full-suite run on this host
shows a PRE-EXISTING 21-test failure cluster (m106 receipt scans, demo-tenant
writes, ynab) that reproduces IDENTICALLY on clean origin/main `e4de90e` in
a fresh worktree — ordering pollution in shared suite-DB state, not M137;
every failing file is untouched by this card. Frontend: `tsc --noEmit` and
`eslint --max-warnings 0` clean; `vitest run` 116 files / 1228 passed, 1
skipped — including the re-pointed m119 popover (6/6) and the M136
locked-vault pins re-seamed onto `withheld` (A1 + A18). Post-M138 rebase
(`3e09f5d`, the subscriptions card that moved the dashboard panel to
`/recurring/candidates`): both suites re-ran green on the merged tree
(backend 1601 + M138's tests; frontend 1240 passed / 1 skipped), and the
re-mounted `GET /subscriptions` keeps its own contract + tests even though
its last src consumer moved — the route is P1-2's deliverable, not the
panel. docs/08 (route
shapes), docs/33 (REVERSED rows + A2/A3/A10/A12/A13), docs/34 (search budget
5→6 statements) and this section reconcile in the same commits.

**Out of scope (pinned):** retiring the client twins and their goldens
(`lib/searchMatch.ts`, `reportEngine.ts`, `s3Engine.ts`, the s3/s4/s5 parity
harnesses — all STAY, still green, as the E2EE/offline fallback) is P2-5, a
later single atomic commit. `lib/e2ee.ts`, the vault, the blind-index format
and `users.encryption_required` semantics are untouched.


## M138 — Wave-13c: the dashboard subscriptions card, the insights $0 headline, a11y polish

> Board card `t_a6dd4f93`. Contract: docs/09 §Bills you probably have;
> endpoint: docs/08 `GET /transactions/recurring/candidates`; the M27
> substrate note: docs/08 §Subscriptions + docs/24 §M27.

**What changed.** M133's dashboard review traced BOTH of Mark's complaints to
one substrate: the card was fed by the client-side M27 detector ("2 or more
charges", average-gap bucketing, no amount band) and had no dismissal at all.
FILM.CA ×2 / LOBLAW ×53 / TIM HORTONS ×95 / SHELL ×18 were presented as bill
suggestions — 4 of 9 rows were not bills — and nothing on the card could
remove them.

1. **One detector.** The card reads
   `GET /transactions/recurring/candidates?source=subscriptions` — the same
   strict engine the M96 prompt uses (≥3 occurrences, ±15% same-sign band,
   every gap fits one cadence). `?source=` decides which
   `recurring_suppressions` keys are subtracted and `dismiss_key` reports that
   surface's own key, so a dashboard dismissal never answers the prompt and the
   reverse; the key is `<normalize_payee_key(payee)>|subscriptions`, amount-free
   and deliberately not a `RecurringCadence` value. `listSubscriptions` (M137's
   thin read of the re-mounted route) now has no `src/` consumer.
2. **Dismissal that can be undone.** Header `Dismiss all (N)` + per-row
   `Not a bill`, both behind `DISMISS_UNDO_MS = 6000` — nothing is POSTed while
   the snackbar is up, so Undo is a local restore and `recurring_suppressions`
   stays write-once with no unsuppress route. Headline renamed to
   **Bills you probably have**, capped at 5 rows with `Show all`, total counts
   money going OUT only, and the empty state never claims "No subscriptions
   detected" after a dismissal.
3. **Composition + the $0 headline.** `Month net` → `Range net` (the value sums
   every bucket in the range), `Positions value` hidden when zero/empty, and
   `insights.py` D-1: the headline and the delta chips now come from one
   `_category_flows` reader in which a category whose rows are all inflows is
   income — so a month where income exceeded spending reports the real spend
   instead of `$0`, refunds still offset, and the parts still sum to the
   headline.
4. **a11y.** The skip link gains `min-h-11` when focused (M133's only sub-44px
   target), and `NetWorthAreaChart` thins its ticks against the CHART's width
   via `useChartWidth` rather than the viewport's — a 1440 viewport budget asked
   for 16 labels in a ~576px card, which is where the overlapping axis pairs
   came from — plus `tickMargin` to clear the axis origin.

**Live verification (deployed build, `scripts/a11y/m138_dashboard_probe.py`,
all checks green; evidence `m138-dashboard-probe.json`):**

| Gate | Result |
| --- | --- |
| Seeded M133 fixture (50 rows) → `?source=subscriptions` | exactly the strict six — GOODLIFE / NETFLIX / PAYROLL / RENT / ROGERS / SPOTIFY — and nothing else |
| FILM.CA · LOBLAW · TIM HORTONS · SHELL | never surface (FILM.CA's 2 occurrences can no longer clear the gate) |
| Every group carries `dismiss_key` ending `\|subscriptions` | yes |
| Card | `Bills you probably have`; cap 5 → `Show all (6)`; `Month net` gone, `Range net` present, `Positions value` hidden, the "2 or more charges" hint gone |
| Dismissal | row leaves immediately, **0** suppress POSTs while the Undo snackbar is up, exactly **1** POST after the 6s window |
| Skip link | keyboard stop **#1**, **44px** tall when focused, focus lands on `#main-content`, **1** plain Tab to the first in-main control |
| axe-core | **0 violations × 4** (1440/390 × dark/light) |
| Text-node overlap sweep (whole document, sidebar + SVG chart text included) | **0 × 4** (301 node-visits @1440, 189 @390, summed over the scroll) |
| Console | **0 errors × 4** |
| Cleanup | throwaway tenant `DELETE /users/me` → **204**; demo account untouched |

**Density.** The subscriptions module measured **448px @1440** (5 rows, capped)
against M133's **688px** for the same module with 9 uncapped junk rows — 240px
/ 35% shorter, more than two KPI cards' worth of vertical space, and the
module is no longer the tallest thing on the page.

**Known leftovers.** Two probe registrations from runs that failed before the
`atexit` cleanup hook existed: `m138-probe-1791672013@example.com` (no account,
no rows) and `m138-probe-1791672302@example.com` (one account + the 50-row
fixture). Every later run deletes its own tenant; there is no user-list
endpoint to enumerate the two with, so a human can drop them if the residue
bothers anyone.

## M141 — No-quote symbols: stop promising "yet" for a ticker Yahoo has disowned

> Board card `t_af854983`, titled "M141". Spec: docs/07 §Dead symbols +
> §Symbol check (the fan-out contract), docs/09 §No public quote (the copy),
> docs/02 §securities (the columns), docs/08 (the endpoint + shapes).

**What changed.** Mark added **APOTEX** — private since 2022, no exchange
listing — to the watchlist. The app created the security and the watch row,
then showed *"No prices yet — nothing has been fetched for this symbol **yet**"*
over a big blue "Refresh prices" button that could never succeed, while the
fan-out logged four identical yfinance `404 Quote not found for symbol:
APOTEX` lines in a single day. The "yet" was a permanently-false promise and
the button was an enabled control that could not deliver.

1. **A fetch failure is classified, not swallowed** (docs/07). `QuoteOutcome`
   separates Yahoo's *verdict about the symbol* (`not_found`, raised as the
   `YFTickerMissingError` family) from a verdict about the NETWORK
   (`unavailable` — rate-limit, timeout, DNS). Pinned by LIVE PROBE against
   yfinance 1.6.0: APOTEX raises `YFTzMissingError("$APOTEX: possibly
   delisted; no timezone found")` with the HTTP layer logging `404 Quote not
   found for symbol: APOTEX`; AAPL1.BE raises `YFPricesMissingError`; a
   `YFRateLimitError` is `unavailable`. Three consecutive `not_found`s set
   `securities.dead_symbol`; any successful fetch resets the counter and
   clears the flag, so a relisted symbol heals with **no manual action and no
   repair script**. When the `.TO` retry candidate and the bare symbol
   disagree, one `unavailable` dominates the `not_found`s — a bad network day
   must never march a live symbol toward the flag.

2. **The automatic fan-out stops.** `backfill_range`, `ensure_recent_price`
   and `ensure_fundamentals` skip a dead row outright (no network call, not a
   failed one), and both fan-outs — the market-hours tasks on
   `GET /portfolio/positions` and the `POST /portfolio/refresh` sweep — filter
   dead rows before queueing. The sweep counts what it skipped as
   `skipped_dead`, so the toast names what it did not do. The ONE exception is
   `POST /securities/{id}/refresh`: the manual **"Try refresh"**, the escape
   hatch that can disprove the flag by simply succeeding.

   `probe_dead` is deliberately a separate parameter from `ensure_recent_price`'s
   `force`: the market-hours fan-out passes `force=True` purely for freshness
   and must still honour the gate. One boolean doing both jobs would have
   turned that fan-out straight back into the 404 machine.

3. **The add form validates, without blocking.** `GET /securities/quote-check`
   answers `{symbol, quote_found: bool|null, reason}` from the SAME yfinance
   seam the fan-out uses, so the add-form warning and the later flag can never
   disagree. `quote_found: null` means *we could not ask* — an answer we do not
   have is never reported as "no quote", so a flaky connection never blocks an
   add. A miss raises a non-blocking warning with an explicit **"Add anyway"**;
   a symbol with a quote still adds in one click.

4. **Every surface states it honestly** (docs/09): the price card says *"No
   public quote for APOTEX"* with the verbatim verdict and DISABLES "Refresh
   prices" **with the reason** (the M140 G2 pattern, `aria-describedby` bound),
   keeping a plainly-labelled "Try refresh" as the escape hatch; the chart
   panel drops its enabled refresh entirely; the watchlist row says **"no
   public quote"** instead of the waiting-flavoured "no price"; the
   backfill poller stops re-reading a payload that will never change; and the
   state itself is server-derived (`security.dead_symbol`) so no two surfaces
   can disagree.

**The migration trap this exposed.** Migration `0045_merge_dup_securities`
(already deployed) runs `services/security_dedup.repair_securities`, which
SELECTed the full `Security` entity — and at that point in the chain the new
columns do not exist yet, so `alembic upgrade head` from scratch failed with
`UndefinedColumn`. Any future column on `securities` would have broken it the
same way. The root fix is that a data migration must never depend on the
current shape of the ORM model: `security_dedup` now reads a FROZEN four-column
list (`_SEC_KEYS`) and issues its writes as Core statements, so the next column
cannot break it again (docs/02).

**Out of scope (pinned):** fixing yfinance's own "possibly delisted" copy —
the emotion is already in the library, we surface the semantic; and auto-mapping
a user's APOTEX to an OTC listing — a fan-out has to guess nothing, and Yahoo
answers plainly.

**Live evidence** — `frontend/scripts/a11y/m141_dead_symbol_probe.py`, run
against the deployed stack at `http://192.0.2.109:8080` on build `e93cf8f`
(throwaway tenant registered outside the page, `DELETE /users/me` at the end;
the demo account is never touched). **36/36 checks passed.** Committed run:
`frontend/scripts/a11y/m141-dead-symbol-probe.json`; the two surfaces:
`m141-dead-detail.png`, `m141-add-warning.png`.

- **The verdict, on the real seam.** `GET /securities/quote-check?symbol=APOTEX`
  → `{"symbol": "APOTEX", "quote_found": false, "reason": "$APOTEX.TO: No data
  found, symbol may be delisted"}` (the `.TO` candidate is what Yahoo actually
  answers for the string a user typed). `?symbol=AAPL` → `quote_found: true`.
  `?symbol=NOSUCHTICKERXYZ` → `quote_found: false` with a real verdict, not a
  crash — the probe uses the same `history(period="1d")` call the fan-out
  makes, so a miss here and a miss there are the same miss.
- **The flag, live.** Three `POST /securities/{id}/refresh` misses take the row
  from `dead_symbol=false` to `{"dead_symbol": true, "quote_miss_count": 3,
  "dead_symbol_reason": "$APOTEX.TO: possibly delisted; no timezone found"}`.
- **The fan-out stops.** `POST /portfolio/refresh` → `202 {"queued": 0,
  "skipped_dead": 1}` (202, per docs/08 — not 200), and three more sweeps add
  **zero** `Quote not found` lines for APOTEX — `2` in the window before,
  `0` logged since the baseline: the 404 machine is off. The count is SCOPED
  to APOTEX and the window is ABSOLUTE (trap 3 and trap 4 below); read over
  ssh from the deploy host, since the api does not run next to the probe.
- **The Detail page.** Empty state "No public quote for APOTEX"; the hint
  carries the verbatim verdict plus "It may be delisted or private — this
  symbol will not track unless a public listing appears."; "Refresh prices" is
  **disabled** with `aria-describedby="empty-action-reason-refresh-prices"`
  bound to "Yahoo has no public quote for this symbol, so refreshing cannot
  produce a price."; "Try refresh" stays enabled; and the old "nothing has been
  fetched for this symbol yet" string is gone.
- **The add form.** Submitting `APOTEX2` raises the non-blocking banner "No
  public quote found for APOTEX2 — $APOTEX2.TO: No data found, symbol may be
  delisted Add anyway?" with **Add anyway** and **Cancel**; **Add anyway**
  completes the add in one click (the security exists, the form resets, the
  banner clears) — no hard navigation, no second step.
- **The watchlist row** reads "no public quote", not "no price".

**Two probe traps this burned, now written down so they are not rediscovered**
(both are in the probe's own docstring):

1. **The session cannot survive a page load on a plain-HTTP origin.** The
   access token is memory-only and the backend's refresh cookie is `Secure`
   (`Set-Cookie: refresh_token=...; HttpOnly; SameSite=strict; Secure`,
   measured) — Chrome will not store it over `http://192.0.2.109`, so every
   hard navigation comes back as /login and `api/client.ts`'s own 401 path is
   a HARD `window.location.assign("/login")`. The probe logs in once against
   the deep link (RequireAuth's `from` state routes straight back) and then
   moves only through the router. Pointing `A11Y_BASE` at
   `http://localhost:<port>` behind `ssh -L <port>:localhost:8080 example.internal`
   makes the cookie storable — that is how the two were told apart.
2. **A native form submit looks like an app bug from the outside.** It reloads
   the document, wipes the in-memory token and leaves every selector null. The
   probe now records the `submit` event (captured, so `defaultPrevented` is
   read BEFORE React's handler) and every Document request, so a swallowed
   handler is NAMED rather than guessed. M142 shipped and then fixed exactly
   that defect — the new-security form was a sibling of the trade form, so
   `Create security` submitted the OUTER form: on the build before `f063345`
   this probe failed with `document_requests: [.../investments?]`; on `f063345`
   the warning renders and the add completes.

**Live verification** — deployed build `e93cf8f` at
`http://192.0.2.109:8080`, driven end to end by
`frontend/scripts/a11y/m141_dead_symbol_probe.py` against a throwaway tenant
(registered outside the page, so no password ever touches a form; deleted via
`DELETE /users/me` → 204 at the end). **36/36 checks pass**; the committed run
is `frontend/scripts/a11y/m141-dead-symbol-probe.json`, with the two surfaces
screenshot as `m141-dead-detail.png` and `m141-add-warning.png`:

- **the verdict, on the real seam** — `GET /securities/quote-check?symbol=APOTEX`
  → `200 {symbol: "APOTEX", quote_found: false, reason: "$APOTEX.TO: No data
  found, symbol may be delisted"}`; `?symbol=AAPL` → `quote_found: true`;
- **the flag** — three `POST /securities/{id}/refresh` misses →
  `{dead_symbol: true, quote_miss_count: 3, dead_symbol_reason: "$APOTEX.TO:
  possibly delisted; no timezone found"}`;
- **the fan-out stops** — `POST /portfolio/refresh` → `202 {queued: 0,
  skipped_dead: 1}`, and three further sweeps add **zero** APOTEX fetch
  failures to the api log (`2` in the window before, `0` logged since): the
  404 machine is off;
- **the Detail page** — "No public quote for APOTEX", "Refresh prices"
  DISABLED with `aria-describedby` bound to *"Yahoo has no public quote for
  this symbol, so refreshing cannot produce a price."*, "Try refresh" still
  enabled, and the old *"nothing has been fetched for this symbol yet"*
  string gone;
- **the add form** — submitting `APOTEX2` raises *"No public quote found for
  APOTEX2 — $APOTEX2.TO: No data found, symbol may be delisted. Add anyway?"*
  with **Add anyway** + **Cancel**; **Add anyway** completes the add in ONE
  click (the security exists and the form resets), and a live symbol still
  adds with no warning at all;
- **the watchlist row** — reads **"no public quote"**, not "no price".

**Five probe traps, written down so they are not rediscovered** (all five are
in the probe's own docstring):

1. **The session cannot survive a page load on a plain-HTTP origin.** The
   access token is memory-only and the backend's refresh cookie is `Secure`
   (`Set-Cookie: refresh_token=...; HttpOnly; SameSite=strict; Secure`,
   measured) — Chrome will not store it over `http://192.0.2.109`, so every
   hard navigation returns /login, and `api/client.ts`'s own 401 path is a
   HARD `window.location.assign("/login")`. The probe logs in once against the
   deep link (RequireAuth's `from` state routes straight back) and then moves
   only through the router — the way a user does. Pointing `A11Y_BASE` at
   `http://localhost:<tunnel>` (`ssh -L <tunnel>:localhost:8080 example.internal`) makes
   the cookie storable, which is how the two were told apart.
2. **A native form submit looks like an app bug from the outside.** It reloads
   the document, wipes the in-memory token and leaves every selector null. The
   probe now records the `submit` event (captured, so `defaultPrevented` is
   read BEFORE React's handler) and every Document request, so a swallowed
   handler is NAMED rather than guessed. M142 shipped and fixed exactly that
   defect — the new-security form was a sibling of the trade form, so
   `Create security` submitted the OUTER form: on the build before `f063345`
   this probe failed with `document_requests: [.../investments?]`; on `f063345`
   the warning renders and the add completes.

3. **The log check is scoped, or it is not a measurement.** The api container
   is SHARED — Mark's tenant and every other agent's probe run in the same
   one — so an unscoped count of `Quote not found` lines moves for reasons
   that have nothing to do with this row. A first run of the check read
   `12 → 17` while nothing this probe asked for was happening, and the five
   new lines were `BANK`/`VEQT`/`XGRO`/`XEQT`/`APTX` from somebody else's
   fan-out. `log_noise` now takes the symbols it counts and matches yfinance's
   two real shapes (`Quote not found for symbol: <S>` and `$<S>: possibly
   delisted…`) behind a word boundary, so `APOTEX` never counts the
   `APOTEX2` this very probe just added through the warning.
4. **A sliding window cannot answer "what is NEW?"** `docker compose logs
   --since 60s` ages its own baseline out while the sweeps run, so a quiet
   system reports fewer lines afterwards (measured: `10 → 0`) and failed a
   check it was satisfying. The window is absolute: a baseline timestamp
   taken before the action, and the count is "logged since then".
5. **A second Chrome cannot share a first one's profile dir.** Chrome hands a
   second launch of the same `--user-data-dir` to the instance already holding
   it and exits WITHOUT opening its debug port, so the driver reports "DevTools
   never came up" for what is really somebody else's lock — a shared-host race
   that reads as an app failure. Both the profile dir and the CDP port now
   default to a per-run value.

**Gates on this card** — ruff format + check clean; `tsc --noEmit` and
`eslint --max-warnings 0` clean; `npm run build` green; the M141 frontend
file `tests/m141DeadSymbol.test.tsx` 12/12; the probe above 36/36 against the
deployed build. The backend suite reported **20 failures out of 1742** — none
of them in M141's blast radius and none of them caused by this card: this
card's commits touch only `docs/10` and `frontend/scripts/a11y/`, and all
twenty are the same pre-existing order-dependent break (a leaked
`demo@fintracker.local` tenant between test modules) that passes 111/111 when
those six files run on their own. Tracked on the board rather than fixed
here, since the fix is a shared `conftest`/test-fixture change that three
other agents are running suites against right now.

## M142 — A typed NAME resolves to its canonical ticker (APOTEX → APTX.TO)

> Board card `t_e53799d4`, titled "M142". Spec: docs/07 §Symbol search
> fallback (+ §Symbol repair), docs/09 §New security — the symbol suggestion
> chip, docs/08 `GET /securities/lookup`, docs/05 §Securities.

**What changed.** M141 proved Mark's `APOTEX` row could never resolve a
quote — and the reason was that he typed a NAME. TSX-listed **Apotex Health
Corp trades as `APTX.TO`**, which Yahoo resolves in ONE search call:

```
GET https://query2.finance.yahoo.com/v1/finance/search?q=Apotex
  → quotes: [{symbol: APTX.TO, shortname: "APOTEX HEALTH CORP", exch: Toronto},
             {symbol: APTX.NE, exch: NEO}]        (LIVE-PROBED 2026-10-10)
```

1. **The lookup chain gains a search step** (docs/07): `direct` (the bare
   symbol) → `.TO` suffix → ONE `v1/finance/search` call. Exactly one equity
   hit adopts that row's **canonical symbol** — `APOTEX` → `APTX.TO`, with
   name, kind, venue and currency from the same row, no second call. Several
   hits (`APTX.TO` + `APTX.NE`) answer `ambiguous` with a pick-one list; a
   silent guess is never allowed to become a stored symbol. Zero hits is the
   dead-row state M141's guard consumes, and `lookup_symbol()` keeps its
   single-result contract (`dict | None`) so the CSV-import autofill and the
   sub-fund creator get the fallback for free. Only `EQUITY`/`ETF`/`MUTUALFUND`
   rows count — a name matching only an INDEX is not a listing.

   **A live check caught the first bug**: yfinance answers an unlisted symbol
   with a NON-EMPTY junk `.info` (`{"trailingPegRatio": None}` — the payload
   M141 documented), so "non-empty" is not evidence of a quote. The first
   deploy answered `match: "direct"` for `APOTEX` and even for
   `PRIVCONOTLISTED`; `_is_real_quote` now requires an identifying field
   (short/long name, quote family, venue) before a candidate counts. That is
   also how Mark's row got its name in the first place.

2. **Listing currency follows the resolved venue**: `.TO` → CAD, else Yahoo's
   Canadian venue codes (`_normalize_exchange`'s TSX/TSXV convention plus
   NEO — `APTX.NE` reports currency CAD live) → CAD, else USD, so the M116 FX
   seams keep seeing the right currency. A TRUE ticker keeps its typed
   spelling (`VEQT` stays `VEQT`, not `VEQT.TO`) — the CSV-import dedup
   matches rows on the raw symbol cell, and adopting the `.TO` form there
   would put a second securities row next to every `VEQT` a Questrade file
   imports.

3. **`fetch_history` heals the same way**: a row stored under a NAME gets its
   closes from the canonical ticker on the next refresh, logged exactly once
   (`symbol search healed the price fetch: APOTEX -> APTX.TO (APOTEX HEALTH
   CORP)`). Several hits are never guessed at — a background fetch has no
   picker — and the heal deliberately does not touch M141's verdict: a
   dead-symbol counter stays a verdict about the STORED spelling.

4. **The add form offers the answer** (docs/09): the lookup resolves while
   you type (debounced, once per settled value) and offers the canonical
   listing — *"Did you mean APTX.TO — APOTEX HEALTH CORP (Toronto, CAD)?"* —
   as a one-click chip; several listings open the same pick-one chooser the
   sidebar market-data fallback now uses; continuing to type clears the
   offer; a miss stays SILENT while typing (the explicit `Look up` keeps W11
   pin 7's loud warning).

5. **A stored row is corrected in place** (`services/symbol_repair.py` +
   `scripts/repair_symbol.py`): symbol, name, kind, listing currency and
   exchange are corrected on the SAME row — never a delete + re-create — so
   the watchlist attachment, trades, closes and ACB pools stay put. Dry run
   by default, `--apply` writes and runs the normal `backfill_range` in the
   same step, `--symbol APTX.TO` carries the human's pick when several
   listings match (a repair never picks), and M141's `dead_symbol` verdict is
   reset because it was about the old spelling.

**Live verification (deployed build at `979bfdb`,
`scripts/a11y/m142_symbol_suggestion_probe.py`, all checks green; evidence
`m142-suggestion-probe.json` + four screenshots):**

| Gate | Result |
| --- | --- |
| `GET /securities/lookup?symbol=APOTEX` | `match: "ambiguous"`, candidates `APTX.TO` (TSX/Toronto) + `APTX.NE` (NEO) |
| `…symbol=APTX` | `match: "suffix"`, CAD/TSX/Toronto — no search call |
| `…symbol=PRIVCONOTLISTED` | 404 `lookup_unavailable` |
| Typed `APOTEX` in the add form | chooser renders both live listings; the typed string stays; nothing is created |
| Picking `APTX.TO` | re-resolves the canonical symbol → the form reads APTX.TO / APOTEX HEALTH CORP / CAD / TSX |
| `Create security` | `POST /securities` **201**; over a FRESH session afterwards: exactly ONE securities row — `APTX.TO` / `APOTEX HEALTH CORP` / CAD / TSX |
| A miss while typing (`PRIVCONOTLISTED`) | no chip, no chooser, no toast, no banner; the explicit `Look up` still shows its warning |
| A true ticker (`APTX`) | no chip (nothing to correct); `Look up` autofills without renaming |
| axe-core / console | **0 critical+serious × 0 errors**, at 1440 and 390 |
| `scripts/smoke.sh` (public origin) | all 11 sections green — 403 demo guard, 422 SSRF, 401/403 MCP internals, 204 delete, 429 at attempt 10 |

**Mark's row 293 repaired live** (`docker exec fin_tracker-api-1 python
scripts/repair_symbol.py --security-id 293 --symbol APTX.TO --apply`):

```
before: APOTEX — APOTEX [stock] USD @        | prices 0 | watchlist rows 1
after:  APTX.TO — APOTEX HEALTH CORP [stock] CAD @ TSX | prices 85,
        newest 2026-10-09 @ 30.79000092     | watchlist rows 1
```

The 85 cached closes match Yahoo's chart bar for bar (2026-10-06 31.67 →
10-07 32.47 → 10-08 30.54 → 10-09 30.79), so the M129 day-delta is
(30.79 − 30.54) ÷ 30.54 = **+0.8186% → +0.82%** against Yahoo's
`regularMarketPrice: 30.79` — exactly the number Mark quoted. The row kept
its id, so the watchlist attachment survived and no duplicate was created.

**Two bugs the live runs caught** (both fixed here): the chooser's rows
carried `role="option"` with no listbox parent — axe `aria-required-parent`,
CRITICAL — and "Create security" performed a NATIVE submission of the trade
form because the new-security panel rendered a `<form>` INSIDE it: invalid
HTML, so a real browser never delivered the inner submit to React's handler
and the page reloaded instead of creating anything (jsdom has no navigation,
which is why the unit test passed happily). The panel now sits beside the
trade form and `form form` is pinned to null.

**Known leftovers.** Mark owns TWO rows for one listing now: 293
(watched, `APTX.TO`) and 298 (held, 3 trades, bare `APTX`) — the repair keeps
its row by design, so folding the twin into one row is a human's call (M117's
merge would repoint the trades and delete the other; not done here).

## M146 — Positions by security: the own-position lens

> Board card `t_4bc2b01d`, titled "M146". Spec: docs/09 §Positions by security
> (M146). Design turn t_81e4e640 (comments 61–63).

**The ask.** Mark holds the SAME ETF in five accounts, so the flat Positions
table makes him add five rows in his head before he knows what he owns.
`PositionsSection` gains a lens at the top of the card — **By account** (the
payload's rows, one per account+security pair, exactly today's table) |
**By security** (the same rows folded to one row per security, with the
per-account rows inline under it).

**A lens, never a second computation.** `lib/positionGroups.ts::groupPositionsBySecurity`
is a pure client-side fold over the `/portfolio/positions` payload the page
already holds — no request, no new server field, no rival total (the S3
pattern, docs/33 §Server-first computing). The toggle's state lives in
localStorage under `fintrack:positions_view.<scope>` and nowhere else
(`<scope>` is `tenantScope(user)` — `lib/tenantStorage.ts`, security
follow-up F-6: two tenants sharing a browser never inherit each other's
lens): this is a view
over ONE table, unlike the dashboard preset the server stores. Switching views
does not refetch. The grouped view is offered only where `showAccount` is — an
account-scoped page holds one account's rows, where every group would be a
single row.

**The four rules the fold keeps** (docs/09 §Positions by security):

1. **Group key = `security_id`, never the symbol** — two listings can carry
   the same bare ticker across exchanges (M143's bug), and folding on the
   string would merge two different securities into one row.
2. **Percent columns are VALUE-WEIGHTED, never a mean of percentages.** Group
   day % = Σ `day_change_cad` ÷ Σ (`market_value_cad` − `day_change_cad`);
   group gain % = Σ `unrealized_gain_cad` ÷ Σ `book_cost_cad`. Both halves
   count the SAME rows, and a zero denominator stays `null`.
3. **Nulls stay null.** A column with no known row folds to `null`; a group
   nobody has priced prints the flat row's *"no price — enter one manually"*
   and em dashes — never a fabricated `$0`. An unvalued group sorts LAST.
4. **One sparkline per group** — the per-account sparks are the same closes
   drawn N times.

**The disclosure** is the split-rows pattern (docs/44 §6.3): a chevron button
carrying `aria-expanded` + `aria-controls` (bound only while the panel exists)
with the count in its accessible name, whole-row click as the pointer path,
and the per-account rows in a **sibling `<tbody>`** — not a nested table, so
they land on the SAME columns as their group — each linking its account name
to `/accounts/{id}`. One panel open at a time. A **single-account security
renders FLAT**: an ordinary position row, no chip, no chevron, nothing to
reveal.

**The Σ row is the trust check.** One `<tfoot>` renders the payload's own
`total_value_cad` / `total_unrealized_cad` / `total_day_change_*` in BOTH
views, so the two perspectives cannot disagree by construction — and
`tests/m146PositionsBySecurity.test.tsx` asserts it byte-for-byte rather than
hoping for it.

**Guardrails, stated because they are easy to violate from a UI seat.** The
group's book cost is a DISPLAY sum of each account's own cost basis (persisted
pool ACB for non-registered, average-cost replay for registered) — **not a
pooled ACB**, and grouping feeds no tax math (docs/05 §ACB). The group's
percents are computed from the SERVER's own cent-rounded CAD fields, so the
client performs no valuation of its own.

**Deviation, reported rather than hidden.** The card's stated rationale was
*"default sort = market value desc … matching today's order"*, but
`services/valuation.py::compute_positions` emits rows keyed
`(account_id, security_id)` — the FLAT view has always been account-ordered.
Only the GROUPED view is value-ordered (Σ CAD desc, unvalued last, ties on
`security_id`). Nothing about the flat view's order changed.

**Verification — 13 vitest tests** (`frontend/tests/m146PositionsBySecurity.test.tsx`):
the fold grouped by id not symbol; the 5-account sums; value-weighted
percents (0.72% vs the naive 2.75% mean on a 20×-skewed pair); all-null groups
kept null and sorted last; partial-null sums (a security with no prior close
keeps its value sum and a null day %); one spark and the value-desc order; the
lens present on the portfolio view and ABSENT on the account page; the group
roll-up (`5 accounts`, Σ 1500 / $60,000.00 / +$15,000.00 / +33.33%); the
linked sub-rows with one panel open at a time; no chevron on a single-account
row; the "no price" group never printing `$0.00`; and the Σ row equal in both
views, both directions.

**Live verification — deployed build at `bbbe815`,
`frontend/scripts/a11y/m146_positions_group_probe.py`, 37/37 in-run checks
green (the 38th is the teardown: `DELETE /users/me` → 204, printed after the
report is written), evidence `m146-positions-group-probe.json` + four
screenshots.** The demo
dataset (docs/30) has no multi-account holding to fold, so the probe seeds the
acceptance SHAPE through the real API into a throwaway tenant — five accounts,
one ETF in five of them at five DIFFERENT entry costs, one in two, one in one,
and one nobody has priced — and drives the real app against the real payload:

| Gate | Result |
| --- | --- |
| The lens | `By account` selected by default; `By security` persists to `fintrack:positions_view` and survives a reload |
| The fold | 10 flat rows → 3 group rows + 1 flat row; the 5-account holding carries `5 accounts`, Σ qty `1500`, Σ value `$60,000.00`, Σ book `$62,000.00`, Σ gain `-$2,000.00` — every expectation recomputed from `GET /portfolio/positions` |
| Value-weighted | group `Your gain %` **−3.23%** (Σ gain ÷ Σ book) vs the mean of the rows' percents **+0.50%**; day Δ prints the formula's `+1.01%` |
| Disclosure | chevron `aria-expanded=false → true`, 5 inline sub-rows, 5 `/accounts/{id}` links; a second disclosure closes the first |
| Single account | the 1-account security is NOT a group row and has no per-account chevron |
| All-null group | `no price — enter one manually`, every money cell `—`, no `$0.00` anywhere |
| Σ row | `+0.83% · $74,500.00 · +$500.00` — byte-identical across both views |
| a11y / console | axe-core **0 critical/serious/moderate/minor** at 1440 and 390; 0 console errors |

**Not in this milestone** (designer's explicit exclusions): no concentration
hint on the chip (threshold-free, later), no sort control, no new pages.

## M144 — The deep price backfill, and the two-reason empty state it exposed

> Board card `t_44290861`, titled "M144". Spec: docs/07 §Backfill depth +
> §Series validation, docs/08 §Honesty (the two-reason empty state),
> docs/09 §Empty-state copy.

**What changed.** Mark's /investments YTD table showed 57 consecutive red
"no valuation for this period" rows (Jan 1 → Feb 26 2026) and a chart that
could only start Feb 27. The measured cause was one held security with ZERO
cached closes: account 459's `VUS` was held 2025-02-27 → 2026-02-27 while
the price cache only ever walked back ~365 days (M134 §3.5's "full available
history, done ONCE" was queued and lost). One unpriceable holding marks its
bucket `valued: false`, and the portfolio merge propagates `None` — one
unvaluable account cannot be papered over with a zero — so a single hole
blanked every day it was held.

1. **The one-time, re-runnable sweep** (`services/deep_backfill.py`, CLI
   `scripts/deep_price_backfill.py`): every held/watched security from its
   OWN need-date (`min(committed trade date, watchlist added_at)`) to today —
   never a hard-coded decade. Insert-only (`_cache_closes`), so a re-run adds
   nothing and an abort leaves clean partial coverage; dead symbols (M141),
   the per-user `CASH` placeholder and privacy-mode tenants (per owner, via
   the ContextVar lock) are REPORTED, never fetched. Dry run by default,
   `--apply` writes. Runbook in docs/07; prod run: 138 securities, 7,589
   closes.

2. **Series validation — which listing a bare ticker means** (docs/07
   §Series validation). Measured live: bare `VUS` answers as *Virtus U.S.
   Dividend ETF* (NYSEArca, USD, $30.23) while `VUS.TO` is the Vanguard TSX
   holding ($130.70) against a recorded buy of $79.69 CAD — and bare `VGRO`
   as *Virtus Silvant* ($25.99) vs `VGRO.TO` ($48.10, the one cached `VGRO`
   row was ALREADY the wrong instrument). A blind sweep through
   `fetch_history` (bare first) would have deepened four years of the WRONG
   instrument and presented it as Mark's real valuation — worse than an
   honest red row. `market_data.fetch_history_candidates` returns every
   answering listing; `choose_series` scores each against the row's own
   committed trade prices with TWO readings — a TIGHT raw one
   (`RAW_SERIES_TOLERANCE = 0.01`, the pre-M132 native-price case: prod VTI
   bought at 329.91 against VTI's $329.91 USD close while the row says CAD)
   and the wide fx-converted one (`SERIES_TOLERANCE = 0.30`, commission
   scale). A candidate must explain a MAJORITY of comparable trades;
   **when trades are comparable and no listing agrees, nothing is cached**
   (`no_listing_agrees`) — that refusal is what later exposed M149 (VGT's
   8:1 split).

3. **The two-reason empty state** (docs/08 §Honesty, docs/09): `valued:
   false` now carries `gap_reason: no_price | fx | not_held`. The leading
   days before an account held anything say "not held yet" (muted) instead
   of the red data-gap copy — one owner for the words
   (`AccountValueChart.tsx::gapReasonNote`), and the header's red "N without
   a valuation" chip counts only DATA gaps. The portfolio merge treats
   `not_held` as ZERO (the same fact as absence), while a real
   `no_price`/`fx` gap still makes the sum unknown.

**Live verification against prod** (the deployed engine, read-only, then the
deployed API through nginx): Mark's YTD day buckets went **57 unvalued → 0**
(Jan 1 = $914,726.53, Feb 26 = $985,412.07); account 459's whole life
went 208/241 unvalued → 0/57; the full-history walk now starts 2022-02-28
valued (the first trade era) instead of Feb 2026. Every deepened series was
spot-checked against the ledger's own prices (VUS.TO 2022-03-04 = $79.73 vs
the recorded $79.69 buy; VGRO.TO = $30.87 vs $30.86). Suite: 19/19 M144 +
50/50 neighboring; frontend 124 files/1286 tests; the two-reason copy
verified in the DEPLOYED bundle (`Investments-*.js` carries both markers).
CI refused the job in 5s (the documented billing outage) — battery run by
hand per docs/10 M106.

**Not in this milestone**: the daily 365-day sweep still takes the first
answering listing (M143's card owns that root); split reconciliation is
M149; the daily sweep's cadence is unchanged by design.
## M147 — the look-through engine had NO data: enrichment on import, a one-time seed, a country map, and a second provider

> Board card `t_3d7e6f92`, titled "M147". Spec: docs/05 §Fund composition
> (providers M147), docs/07 §Fund composition providers, docs/09 §Exposure
> honesty, docs/08 `holdings` + `breakdown` rows, docs/02 `etf_holdings.source`.

**The bug.** Mark's Geography pie showed 84.75% "Unclassified" ($1.22M of
$1.44M). The engine (M18/M19 recursive look-through) was sound and the
fetcher (M17) existed — but `etf_holdings` had **0 rows for every user**:
the per-security Import button had never been clicked, and the import
pipeline never queued it (a chicken-and-egg: enrichment ran only on an
explicit holdings write). Worse, half of his fund rows were `kind=stock`
(VEQT/VFV.TO/VUN/XEQT/XGRO/XSP/XWD/VIU — import-created with the stock
default when lookup failed), so even fetched rows would not have looked
through. And yfinance's `funds_data.top_holdings` only answers a fund's
TOP SLICE (live probe: VTI 10 rows = 33.5% of NAV, VIU 10 rows = 14.1%,
XBB = EMPTY) — one provider could never fill the pie.

**What changed.**

1. **Auto-enrichment on commit** (`services/fund_enrichment.py`): both
   commit paths queue a background task for every fund-kind security of
   the batch with ZERO composition rows; the task degrades silently
   (privacy 403 included) and never touches a fund that has any row.
2. **A single write path** (`market_data.import_fund_composition`): the
   endpoint and the background task now share one function — replace all
   non-manual rows, link `holding_security_id`, repair a proven-fund
   `kind=stock` → `etf`.
3. **A one-time seed** (`scripts/backfill_etf_holdings.py`): dry-run by
   default, `--apply` writes through the same path, per-fund counts.
4. **The static country map** (`services/company_country_map.py`): the
   geography chain's last step before Unclassified — top-20 US mega-caps,
   the TSX majors and the international names that appear in funds' top
   slices, exact match then known-suffix strip; a shipped lookup, never
   an LLM call (determinism).
5. **A second provider** (docs/07): iShares US full-holdings CSVs
   (`latest-holdings.csv`, 509 rows for IVV with per-holding Location +
   Sector + Asset Class — verified live) registered per ticker, fetched
   behind the same privacy guard, stored with the new
   `etf_holdings.source="issuer"`; yfinance remains the fallback. The
   residual beyond both providers stays Unclassified BY DESIGN and the
   **honesty copy** (docs/09) names each not-yet-imported fund in the UI.

**Verified.** 14 backend tests (`tests/test_m147_fund_enrichment.py`: the
CSV parser's pinned shape, the chain preferring issuer→yfinance + the
stored-bare `.TO` retry, the write path keeping manual rows + repairing
kind, the commit trigger, privacy degradation, the map's determinism + the
geography fallback, the `missing`/`partial` attribution incl. the 99%
threshold) and 3 frontend tests (`tests/m147UnclassifiedCopy.test.tsx`).

**Live against production (example.internal, 2026-10-11).** The seed ran twice:
`--apply` (135 candidates → 27 funds, 171 rows) then `--apply --refresh`
(all fund-kind securities → 66 funds, 1 923 rows, upgrading the M19
sub-funds through the chain — IVV landed 506 rows from the issuer CSV,
every one carrying Location + Sector). Mark's pies (user 343):

| dimension | Unclassified before | after |
|---|---|---|
| geography | 84.75% ($1.22M) | **63.71%** ($919k) |
| sector | — | 79.28% |
| asset class | — | 60.20% |
| company | — | 60.20% |

The remaining geography slice is now NAMED, fund by fund, by the honesty
list: VXUS (12.8% imported, $306k position), VTI (33.5%, $208k), VT
(21.4%, $62k) — Vanguard US funds whose full-holdings CSVs are behind
JS-only apps with no stable URL (the one known provider gap; iShares US
works, Canadian-native sleeves ride their yfinance top-10 + the static
country map). The <15% acceptance target needs a Vanguard full-list
provider — filed as follow-up work; until then the copy tells the truth
about every remaining slice, which is the card's stated alternative.

**Verified.** 12 backend tests (`tests/test_m147_fund_enrichment.py`: the
CSV parser's pinned shape, the chain preferring issuer→yfinance, the write
path keeping manual rows + repairing kind, the commit trigger, privacy
degradation, the map's determinism + the geography fallback, the
`unclassified_funds` attribution) and 2 frontend tests
(`tests/m147UnclassifiedCopy.test.tsx`). Seed run against production:
[filled in by the deploy step — see the card].

## M148 — The outage that wasn't the forecast: one blocked event loop

> Board card `t_e286d532`, titled "M148 canonical (fintrack): PROD OUTAGE".
> Spec: docs/01 § "Sessions and the event loop". Incident report: @ux's
> `INCIDENT_api_pool_exhaustion_2026-10-11.md` (board card `t_0c65cacf`).

**What happened.** 2026-10-11 03:08 UTC, `fintrac.princessandmark.com` went
down for every user: 15 Postgres sessions sat `idle in transaction` — exactly
the SQLAlchemy pool (`size 5 overflow 10`) — `/health` answered nothing, and
`docker ps` showed the api container `(unhealthy)` for more than two hours.
Only `docker restart` cleared it. Mark's own Firefox was on the Forecast page
when it started, and `GET /forecast/run` was the route carrying the last
statement in every parked session, so the route was the obvious suspect.

**It was not the route.** `run_forecast` is pure compute (docs/45) and
`collect_inputs` is network-free, and a 20-concurrent-request reproduction
against the real stack showed both the sequential and concurrent route are
fine. The parked sessions were a *symptom*: a handler that has finished its
query and is waiting for the event loop looks exactly like `idle in
transaction`.

**The mechanism (reproduced, then pinned).** `get_current_user` is
`async def` — deliberately, so its ContextVar privacy-lock binds land in the
request's task — and it called `Session.get` directly, i.e. a **blocking pool
checkout on the event loop**. A burst checked out all 15 connections; every
request that then queued for a connection froze the loop for the full 30 s
`pool_timeout`; the in-flight handlers need that same loop to send their
response and close their session, so they could never finish and never
released a connection. Waiters starved the release they were waiting for:
a self-deadlock. Measured before the fix — one request waiting for a
connection blocked an unrelated request by **2.05 s** at a 2 s pool timeout
(30 s in production).

**The fix.** Both auth dependencies now hop through
`starlette.concurrency.run_in_threadpool` for their one query. The wait moves
to a worker thread, so a saturated pool degrades to bounded 500s/503s and the
in-flight handlers keep draining. `get_db` stays a SYNC generator dependency
for the same reason — FastAPI resolves and closes it off-loop — and
`tests/test_m148_pool_deadlock.py` pins both halves against the real ASGI
stack.

**Hardening, because the next slow endpoint will exist.** `restart:
unless-stopped` restarts a crash, never a live container that answers nothing,
so `scripts/autoheal.sh` (host cron, every minute) restarts a service after
three consecutive unhealthy rounds and resets on any healthy one. The probe
standard (docs/13) now makes every probe check `/health` before it drives the
stack, log latencies, and refuse to fan out past a handful of concurrent
requests.

| Pin | Before | After |
| --- | --- | --- |
| Loop cost of a pool wait | 2.05 s (2 s pool timeout) | < 1 s budget, ~0.02 s measured |
| Same burst, full API | frozen; only restart recovers | bounded 500/503, drains on its own |
| Sessions after the burst | 15 `idle in transaction`, forever | 0 |
| Regression test | none | `tests/test_m148_pool_deadlock.py` (3 tests) |

**Not in this milestone:** the async import/trade/receipt routes still do
bounded blocking work (parse + egress with explicit timeouts) on the loop with
the request session open — degraded latency, not a deadlock; offloading them
is a follow-up card — **done in M149**.
## M149 — six more routes off the loop, and the market-data deadline becomes ours

> Board card `t_7d138cc0`. Spec: docs/01 § "Sessions and the event loop",
> docs/07 § Call discipline (yfinance).
>
> Numbering note: the `M149` label was drawn twice while both cards were in
> flight — commit `0ac3273` (the split-adjusted price cache, spec'd in
> docs/07 § "Price frame (M149)") took it for a commit and a docs/07 section
> but no roadmap entry, and this card kept the board's title. Neither is a
> second M149 milestone; they are two changes that collided on a number.

**What changed.** M148's audit left two named leftovers, both "bounded but
wrong". First, six `async def` routes — `/imports/suggest-profile`,
`/imports/preview`, `/imports/upload`, `POST /trades/import/preview`,
`POST /trades/import`, `PUT /transactions/{id}/receipt` — needed
`await file.read()` and then kept going ON the loop: a CSV parse, a per-row
duplicate query (up to 2000), an LLM call of up to 60 s, a 2 MB base64
round-trip. None of them could self-deadlock the way M148's auth query could,
but each froze the loop for its own duration, so one receipt upload stalled
the dashboard, /health and every other tab. All six now hop through ONE
helper, `app/api/_offloop.py::blocking` — `def core(): ...` then
`return await blocking(core)()` — and each route's early DB guard hops on its
own (`await blocking(owned_or_404)(...)`) so a 404 still lands before the
body is read.

Second, `services/market_data.py` claimed a "5 s timeout" it never applied: it
built `Ticker(sym)` with no session, so the deadline that actually bounded a
stalled Yahoo was **yfinance's own 30 s default** — undocumented,
version-dependent (AGENTS §6), and six times what docs/01 and docs/07 promise.
The module's `HISTORY_TIMEOUT_SECONDS = 5` was a constant nothing read;
`YF_TIMEOUT_SECONDS` replaces it and is applied. `_yf_session()` re-types
yfinance's own default session into a subclass that FORCES `timeout=5` on
`Session.request` — the one method every yfinance call, cookie handshake
included, ends in — and installs it on yfinance's process-wide `YfData`
singleton, the one session the library re-uses for every ticker. The module's
only `Ticker` constructor is now `_ticker(sym)`, which leaves the constructor's
signature alone (and so leaves every test double in the suite alone too). A
`setdefault` would have been a no-op, because yfinance always passes its own
value; the test says so out loud (`{30} == {5}` without the force).

| | before | after |
|---|---|---|
| One slow upload/parse route | froze the loop for its own duration | loop cost ~0; unrelated request served inside 0.5 s |
| Market-data deadline | yfinance's 30 s default | ours — 5 s, injected at the transport |
| The module's own constant | `HISTORY_TIMEOUT_SECONDS`, read by nothing | `YF_TIMEOUT_SECONDS`, applied and pinned |

**Verified.** 14 backend tests.
`tests/test_m149_offloop_routes.py`: three behavioral pins, one per affected
router — each makes a real route block for 2 s on purpose and asserts an
unrelated request (`/docs`, 401, no DB connection) is still served inside
0.5 s, then checks the route's own real response arrived; without the hop the
receipt route blocks the loop for **2.02 s**. Plus a shape-tripwire over all
six routes (labelled as a shape check, not proof) and a guard that
`blocking()` refuses a coroutine function.
`tests/test_m149_yf_timeout.py`: the deadline reaches the transport, it
overrides rather than defaults beside yfinance's 30 s, the deadline is the
session the `YfData` singleton actually holds, every fetch seam still routes
through `_ticker`, and the module's only `Ticker` constructor is that helper. Live Yahoo
probe against yfinance 1.6.0 from this host: `resolve_symbol`,
`probe_quote` and `fetch_history` return **byte-identical** payloads with and
without the deadline session, and the APOTEX dead-symbol verdict is unchanged.
## M150 — FX rate book: it refreshes itself, and says when it cannot

> Board card `t_1f1fcf4c`, titled "FX rate book refresh automation". Spec:
> docs/07 §Scheduled refresh + §Rate-book status, docs/08 (`GET /fx/status`),
> docs/09 §FX staleness chip, docs/15 (the board is cache-only; the chip
> answers "how old?"), docs/11 §Egress (the worker's Valet call + its privacy
> gate).

**What changed.** Since M132 the trade-cash leg converts at the ROW's currency
on the trade date, and since M115 every exact-date FX read resolves in two
steps — the date's row, else the newest published row within 7 days. That
window exists for BoC's publication calendar. It also hid the real failure:
the cache filled itself on a **true miss** only, so a book last fetched on
Tuesday still "resolved" Wednesday…today, off Tuesday's rate. Nothing errored,
nothing logged, no UI said a word — the gap re-priced silently, and the
Currency Tracker board (cache-only since its hardening) could not say how old
its own numbers were.

1. **The book now refreshes itself** (docs/07 §Scheduled refresh).
   `services/fx_refresh.py` is one in-process worker started from `lifespan`,
   the M59 delivery worker's shape, on `fx_refresh_interval_seconds` (6 h). It
   fetches a curated pair only when its newest cached row is missing or older
   than `REFRESH_MIN_AGE_DAYS` (1 day) — one Valet call per behind pair through
   `fx.refresh_pair`, which lives in `services/fx.py` so Valet stays behind the
   one egress seam. **A current book costs zero network calls.** The store path
   is unchanged: empty dates only, so a tick can never overwrite a BoC row or a
   user's manual gap-fill.

2. **Privacy wins, visibly.** The pair list is the STATIC `CURATED_FX_BASES`
   (never tenant-derived), and a tick is skipped entirely while any user has
   `privacy_mode` on — `fx_rates` is shared with no `user_id`, so a per-user
   flag cannot be honoured pair by pair (docs/11). The staleness chip below is
   what makes that state visible rather than a quiet staleness nobody sees.

3. **`GET /fx/status` answers "how old is my rate book?"** (docs/08) — for the
   currencies that price THIS tenant's money: `trades.currency` (the M132 row
   currency), `accounts.currency` (what a pre-M132 NULL-currency row reads in)
   and `securities.listing_currency`, minus the reporting currency. Per pair:
   `as_of`, `age_days`, `source`, `stale` — where **stale = past
   `CARRY_FORWARD_MAX_DAYS`, or no row at all**, the same 7 days consumers
   carry a rate forward through (the constant moved to `services/fx.py` and
   `tax/acb.py` aliases it, so the chip's threshold and the engine's ceiling
   cannot drift). Cache-only: a page load can never become a Valet call, and
   privacy mode cannot 403 it. `pairs: []` means no foreign currency is held —
   no FX dependency, nothing to report.

4. **The chip says it out loud** (docs/09 §FX staleness chip): `FX · 2026-10-09
   · 1d` when current, a pulsing danger `FX rates stale · 9d` past the window,
   `FX rates missing` when a pair has no row at all, and **nothing at all**
   while loading, on error, or when the tenant holds no foreign currency. It
   rides in the `/currencies` header — the one page about FX — and carries the
   per-pair detail in its hover title.

**Verification.** `backend/tests/test_m150_fx_refresh.py` (14) — refresh only
behind pairs, the min-age boundary in both directions, a current book making
zero calls, per-pair fail-soft, the privacy skip, the empty-dates-only store
leaving a manual gap-fill untouched, the settings off-switch, and six
`/fx/status` cases including the 7/8-day boundary, the worst-covered pair, the
tenant-scoped currency census and the never-fetches proof (the Valet seam is
monkeypatched to RAISE). `frontend/tests/m150FxStaleness.test.tsx` (7) — all
four chip states, the deliberate nothing, and the mount in the page header.

## M147-followup — the last of the Unclassified slice: Vanguard's own full-holdings APIs

> Board card `t_8a4d75d1`. Spec: docs/05 §Provider chain + §Coverage
> expectations, docs/07 §Fund composition providers, docs/02
> `etf_holdings.source`, docs/08 `GET /securities/{id}/holdings`, docs/16
> `etf_holdings` column table + the M19 loop bounds.

**The gap M147 named.** After the iShares issuer CSV landed, Mark's
Geography pie still read **63.71% `Unclassified` ($919k of $1.44M)** and the
honesty list named exactly why: VXUS 12.8% imported ($306k), VTI 33.5%
($208k), VT 21.4% ($62k) — Vanguard US funds whose "full-holdings CSVs are
behind JS-only apps with no stable URL". Vanguard publishes no CSV at all:
both of its sites serve holdings from first-party JSON APIs.

**What shipped** (commits `91e31fe` + `a968ee4`).

1. **US** — `GET investor.vanguard.com/irr/funds/profile/{TICKER}-AdditionalFundData`
   → `.holdingDetails`, with whichever of four holding groups the fund
   publishes (BND has no equity block). 50 tickers registered
   (`market_data.VANGUARD_US_FUNDS`), every one live-verified 2026-10-11 from
   this host AND from inside the deployed `api` container (~0.3 s/call, 4.5 MB
   worst case); anything else answers 404 `Fund not found`, so an unregistered
   ticker makes no network call at all.
2. **CA** — `POST www.vanguard.ca/gpx/graphql`, GraphQL `FundsHoldingsQuery`
   → `.data.borHoldings[0].holdings.items`, paged at 1500 until `lastItemKey`
   is null; `X-Consumer-ID: ca0` is mandatory (400 without it) and an unknown
   portId answers **200 with EMPTY arrays** — emptiness, not the status line,
   is the failure signal. 42 products registered as ticker →
   `(portId, Yahoo venue suffix)`; a stored `VCN.TO` / `VBG.NE` resolves by
   stripping the exchange suffix.
3. **Geography without a country column**:
   `company_country_map.country_for_iso2` turns the ISIN's first two characters
   (US payload) or the payload's own `bloombergIsoCountry` (CA payload) into
   the same names yfinance writes, so one country never splits across two
   providers. Deliberate gaps stay Unclassified rather than asserted:
   `XS`/`EU` (ISIN issuing categories) and the offshore incorporation
   domiciles (`KY`/`BM`/`JE`/… — Alibaba's ISIN starts `KY`), measured at
   ~3% of VXUS.
4. **Two payload rules pinned from live measurement**: weights are quantized
   to `Numeric(7,4)` and rows the column cannot store are dropped in the
   parser (VXUS publishes 6,586 `0.00%` rows among 8,983 — a row that
   quantized to zero would fail `CHECK (weight_pct > 0)` and abort the whole
   import); `CT.*` currency-hedge legs are skipped entirely — a CAD-hedged
   fund publishes BOTH sides of every forward (VUS `+291.71 / -290.69`) and
   `weight_pct > 0` would keep only the positive side and store a 390–400%
   total. Fund-leg rows (`EQ.ETF`) carry no country (their geography comes
   from the recursion) and are stored WITH the registry's venue suffix
   (`VCN` inside VEQT lands as `VCN.TO`), because the look-through engine
   matches a composition ticker against the user's stored securities exactly.
5. **A full list also bounds the M19 deepening** (`a968ee4`):
   `ensure_sub_fund_composition` receives every written ticker, and a RESOLVED
   non-fund ticker creates a linking security without counting toward the 12
   enrichments — so the 2,000–3,600-row funds turned the deepening into a
   walk of the whole fund: measured on prod, **1,458 securities created for
   one tenant** and minutes of yfinance fan-out per import (a seed run never
   finished). `market_data.SUB_FUND_PROBE_CAP` (60) caps the probes; providers
   publish weight-first, so the heaviest rows — every fund-of-fund leg in
   production — are the ones linked. With the cap the same seed finishes
   **user 343 in ~8 minutes**.

**Verified.** 17 backend tests (`tests/test_vanguard_holdings.py` over three
live-captured fixtures: both parsers' pinned shapes, the weight / asset-class /
hedge rules, the chain order issuer → vanguard → yfinance, five
endpoint-failure negatives degrading to the yfinance slice, registry
no-network guarantees, privacy-lock degradation, the write path storing
`source=vanguard`, and the probe cap). Full backend suite on the tree that
was pushed: **1806 passed, 2 skipped, 0 failed**; frontend `tsc`/`eslint`
clean and vitest **1303 passed** on the rebased tree; `npm run build` green;
CI is disabled at the repo level while Actions billing is dead, so the
battery was run by hand (docs/10 M106).

**Live against production** (example.internal `192.0.2.109`, 2026-10-11). Deployed
`91e31fe`, then `a968ee4`; `scripts/backfill_etf_holdings.py --apply --refresh`
(the seed M147 introduced). The first run was killed twice by sibling
deploys recreating the `api` container — and its log is what exposed the
unbounded deepening — so the final numbers come from the post-cap run
(`--user-id 343`, 40 funds enriched, 23,032 rows). The changed path was also
exercised end-to-end through nginx with a throwaway account (register →
login → create security → `POST /securities/{id}/holdings/import` → **200,
`source="vanguard"`, 2283 rows, `total_weight_pct=94.00`, 50 countries** →
account deleted 204).

| dimension (user 343, portfolio $1.44M CAD) | before (M147) | after | change |
|---|---|---|---|
| geography Unclassified | **63.71%** ($919k) | **7.53%** ($108.9k) | −56.2 pts |
| sector Unclassified | 79.28% | 62.77% | −16.5 pts |
| asset-class Unclassified | 60.20% | **4.44%** | −55.8 pts |
| company Unclassified | 60.20% | **4.44%** | −55.8 pts |

`etf_holdings` now holds `issuer` 3 funds/1,518 rows, **`vanguard` 23
funds/25,858 rows**, `yfinance` 49 funds/323 rows (the leftover top slices).
The geography pie reads United States 44.70%, Canada 20.49%, Japan 4.68%,
United Kingdom 2.58%, Taiwan 2.54%; the company lens reads NVDA 7.25%,
RY.TO 3.87%, MSFT 3.83%. The honesty list shrank from nine funds to **three,
all "partial" rather than "none"**: VXUS 94.00% imported, VTI 98.36%,
VT 93.82% — the residual is hedge rows and micro-caps the column cannot
store, not a missing source.

**What the residual is now** (and is NOT a holdings-source problem): sector
stays 62.77% because sector reads `company_fundamentals.sector`, which is
still only backed by ~300 rows for 2,333 securities — the M147 note already
names the fix as the fundamentals backfill, not another provider. The
remaining 7.53% of geography is the same story one level down: the drops are
rows `Numeric(7,4)` cannot store plus deliberate country gaps (`XS`/`EU`,
offshore domiciles).

## M152 — a healthy operator run that LOOKED like the M148 outage

> Board card `t_2d8ffedb`, titled "prod has recurring `idle in transaction`
> sessions parked on a fund-kind securities SELECT — attribute the caller,
> then fix or dismiss". Spec: docs/01 § "Sessions and the event loop",
> docs/05 §Auto-enrichment (One-time seed).

**What was seen.** During M149's deploy verification (2026-10-11 11:00–11:10
UTC) a `pg_stat_activity` sweep of prod `fintrack` found sessions parked
`idle in transaction` (`wait_event = ClientRead`) for 8+ minutes, one after
another, with this as their last statement:

```sql
SELECT securities.created_at, ..., securities.dead_symbol_reason
FROM securities
WHERE securities.kind IN ($1, $2) AND securities.user_id = $3::INTEGER
ORDER BY securities.user_id, securities.id
```

Same signature class as the M148 outage — a request holding an open
transaction while it waits on something else — but 1–2 connections instead of
15, `/health` answering 200 throughout, and the API log otherwise clean.

1. **The statement has exactly ONE source, and it is a CLI script.**
   `scripts/backfill_etf_holdings.py::_candidates` in its `--refresh` +
   `--user-id` shape compiles byte-for-byte to what was observed (the
   non-refresh branch carries two `EXISTS` clauses and an extra ORDER BY
   term; `fund_enrichment.funds_needing_enrichment` builds its `WHERE` the
   other way round — `user_id` first). `git log -S` shows
   `order_by(Security.user_id, Security.id)` has only ever existed in that
   file. It was neither the request session nor
   `fund_enrichment._run_enrichment`'s background session.

2. **It was an operator run, and the window matches to the second.**
   `ssh example.internal "docker exec fin_tracker-api-1 python -u
   scripts/backfill_etf_holdings.py --user-id 343 --apply --refresh"` started
   **10:59:59 UTC** and its log stopped at 11:07:59 — the exact 8 minutes the
   sweep was watching (an earlier run, 09:53:17 → 10:57:22, explains the
   10:57:22 pair). Both of that run's backends logged `unexpected EOF on
   client connection with an open transaction` at **11:09:05**: the script's
   own session (parked on the candidate SELECT) and `_run_enrichment`'s
   (mid-write), killed together when the `docker exec` was torn down.

3. **No user was ever affected, but the path really did hold a session
   across a network wait** — the script opened one session for the candidate
   SELECT and kept it for the whole run: every provider fetch and every
   `--sleep` happened with that transaction open. Fixed the way docs/01
   puts it: the candidate query and the per-fund row count each get a
   session of their own that CLOSES before the next fetch, and the loop
   works from a snapshot of plain values (an ORM instance read after the
   session closes is detached, so `.kind.value` would raise).

**Verification.** `backend/tests/test_m152_seed_session.py` (3) asks the
question from *inside* the provider call — the moment the wait happens: no
session the script opened is in a transaction, and no backend is
`idle in transaction` on that statement (matched by the `--refresh` ORDER BY,
so it cannot be another query). Both red on the unfixed script
(`seen_busy == [True, True]`), green after; the third asserts every session
`main()` opened is closed by the time it returns — the incident's second
symptom. `_candidates` runs for real in every case (it is the statement
under test); only its RETURN is replaced with fixture rows, because the
script's own engine can see only committed data and the suite's outer
transaction is the test's cleanup.

**Who ran it.** The `M147-followup` card above (`t_8a4d75d1`, the workspace
its worker was still in when the process started) invoked the seed as its own
verification step — `docker exec fin_tracker-api-1 python -u
scripts/backfill_etf_holdings.py --user-id 343 --apply --refresh`. Worth
naming because it is the whole lesson: a *reasonable* thing to run, on the
*right* database, from the *right* container, that still reads as an outage
in a monitoring query.

**Filed, not fixed here.** Two neighbours of the same class, both found while
attributing this one: `t_e1f43ddf` (M153) — `_run_enrichment` /
`ensure_sub_fund_composition` hold the APP POOL's session across the provider
loop, the background-task half of this class (**fixed as M153, below**); and
`t_7ed88c41` (M154) — the bare-vs-suffixed symbol mismatch (**fixed as
M154, below**) —
`etf_holdings.holding_symbol` is always bare (`005930`) while
`securities.symbol` is exchange-suffixed (`005930.KS`), so that same run
logged **231** caught `uq_securities_symbol` violations (11:00:06 → 11:09:01)
and, of the demo user's **23,042** holdings, only **3,117 (13.5%)** carry a
`holding_security_id` — **2,130** of the unlinked ones are holdings the user
already owns under a suffixed spelling.

## M153 — the background enrichment task held the APP POOL's session across the provider loop

> Board card `t_e1f43ddf`, titled "`_run_enrichment` /
> `ensure_sub_fund_composition` hold the APP POOL's session open across the
> provider loop — the background-task half of the M152 class". Spec:
> docs/01 § "Sessions and the event loop", docs/07 § "Enrichment sessions".

**What was wrong.** M152 fixed `scripts/backfill_etf_holdings.py` and named
two neighbours of the same class without fixing them. This is the one with
real pool pressure, because it is on the *serving* path:
`api/imports.py` and `api/trades.py` both queue
`fund_enrichment._run_enrichment` as a FastAPI `BackgroundTask` after a
committed batch, and it opens its own session out of `deps.get_engine()` —
the API's own pool — then:

* `session.get(Security, id)` autobegins a transaction, and the very next
  thing `import_fund_composition` does is `fetch_fund_composition(symbol)`,
  a network call;
* `ensure_sub_fund_composition` autobegins again on its
  `select(Security).where(user_id…)` security map and then walked up to
  `SUB_FUND_ENRICHMENT_CAP = 12` sub-funds — `lookup_symbol`,
  `import_fund_composition`, `ensure_fundamentals`, `backfill_range`, four
  provider waits each — inside that ONE transaction;
* `ensure_fundamentals` carried the same shape on its own: `SELECT`, then
  `fetch_fundamentals`, then commit.

One background enrichment therefore held one pool connection **and** an open
transaction for a multi-fetch run that is measured in tens of seconds.

**Why it mattered.** Today `BackgroundTasks` runs them one at a time, so it
cost one connection — that is luck, not a guarantee, and any change that runs
enrichments concurrently (or several requests queueing commits) puts it
straight back on the pool. It is the exact M148 signature: `idle in
transaction` backends while the pool drains. M148 cost a 2-hour outage with
15 sessions; M152's sweep caught the same row on an operator run where
nothing was wrong at all.

**Correction to the card's premise.** The card asked for the seam between
`ensure_sub_fund_composition`'s two callers — the background task vs. "the
request path (`POST /securities/{id}/holdings/import`)" — because an early
`commit()` would commit a request's partial work and a `rollback()` would
destroy it. There is no request-path caller. The endpoint *queues* the
deepening via `queue_deepen`, whose body is `api/securities._run_deepen` —
a second BackgroundTask that opens its own session, exactly like
`_run_enrichment`. Both production callers are therefore own-session tasks,
so the seam is uniform: **`ensure_sub_fund_composition` owns the
transaction it runs in, and both callers get the same release.** A caller
that ever hands it a request session is outside that contract and has to
say so (docs/07).

**What changed.** One seam, `services/market_data.py::
release_before_provider_wait(session, security?)`, called before every
provider wait on the enrichment path. It commits the read transaction back
to the pool. It is a no-op write-wise — every write those seams make already
commits itself, so `import_fund_composition`'s delete+insert+commit stays
ONE transaction untouched, as the card required. Two properties are load-bearing
and neither is obvious:

1. **It must not expire the caller's objects.** A plain `commit()` expires
   every loaded ORM instance, and the next attribute read silently re-opens
   the transaction the release just gave back — so the fix would work for
   the two background-task sessions that happen to set
   `expire_on_commit=False` and quietly not work for everyone else. Verified,
   not assumed: with the flag-pinning removed, the pin goes red at index 2
   of the probe trace (the sub-fund's import), which is exactly that lazy
   reload. The helper therefore pins `expire_on_commit` off for the duration
   of the commit and restores it.
2. **The release must be the LAST database contact before the wait.** The
   helper takes the `Security` the provider call is about to read off and
   touches its attributes *inside* the transaction being released; without
   that, the callee's `fetch_fund_composition(fund.symbol)` /
   `fetch_history(security.symbol, …)` argument lazy-loads and reopens. Also
   measured, not assumed: with that pre-load removed the pin goes red at
   index 4 of the default-session probe trace (test 4) and at test 5's
   read-after-release assertion, which names the failure cleanly — on many
   integration routes some earlier read (`sub.kind`, the `has_rows` SELECT)
   has already loaded the row, which is exactly why the unit assertion
   exists.

**Verification.** `backend/tests/test_m153_enrichment_session.py` (5 tests)
asks both questions from *inside* a monkeypatched provider call — every
seam on the path: the fund's own import, the deepening loop's
lookup/import/fundamentals/price-backfill for **two** sub-funds (so the
second iteration proves the first left nothing behind, M152's shape), and
the task's own fundamentals refresh. No session the task opened may be in a
transaction, and no backend on the database may be `idle in transaction`.
RED before the fix: 2 failed, `seen_busy == [True, True, …]` at every one of
the 11 waits — GREEN after. The fourth test hands
`ensure_sub_fund_composition` a session whose `expire_on_commit` is still
True and is the one that goes red without property 1; the fifth pins
property 2 directly. The tenant is committed for real through a dedicated
engine (the task's own session sees only committed rows) and deleted again
in teardown, the same pattern `test_m148_pool_deadlock.py` uses.

**Deliberately not changed here.** `backfill_range` and
`ensure_recent_price` are the same class on the price paths
(`_run_backfill`, `/portfolio/positions`, `POST /securities/{id}/refresh`)
and are not on the enrichment path — `ensure_sub_fund_composition` releases
before calling `backfill_range`. They are request-path seams with their own
callers to weigh; noted in docs/07 rather than silently widened into this
card. **Closed by M161 (below).**

## M155 — a standing repair-needed report: the wrong rows M141/M142 can only catch at ADD time

> Board card `t_9fae5764`, titled "Ticker repair-needed report: M141/M142
> fixed add-time validation but security 293 (wrong VGRO) and 294 were
> repaired by hand — surface a periodic repair-needed report (bare-collision
> + live-symbol heuristics on owned securities) instead of one-off scripts".
> Spec: docs/07 §Repair-needed report.

**The ask.** M141/M142/M143 made the add path honest: a typed NAME resolves
to its canonical ticker, a bare ticker that lives in two markets asks instead
of guessing, and a `.info` body that names no security is a miss. What none
of them can do is reach BACKWARD. Every wrong row this document names was
found because a human happened to look and was corrected with ONE script and
ONE id: 294 (bare `VGRO` → *Virtus Silvant*, USD, three wrong closes), 293
(`APOTEX` stored as a symbol), 296 (`VFV, USD`), 298 (the same name-stored
row, flagged dead along the way). The next one is still out there — and
"someone will notice" is not a control.

1. **Two heuristic families, on OWNED rows** (`deep_backfill.held_security_ids`
   — a committed trade or a watchlist row). `services/repair_report.py::scan`
   asks each row: *do TWO listings answer for your symbol?* (bare collision,
   the M143 class) and *does the listing's identity NOW match what you store?*
   (live-symbol heuristics: name-stored-as-symbol, listing currency, exchange,
   name, `dead_symbol` verdict). Twelve check ids, three severities, every
   one of them a stable token an operator can grep for.

2. **The ledger decides before the market does — and a human before the
   report does.** A collision is settled by the row's own committed trade
   CURRENCIES first (a CAD buy against a USD listing is the whole 294 bug and
   costs no network call), by M144's price evidence only when the currency
   cannot say (`deep_backfill.score_series`, majority-of-trades, fetched only
   for those rows), and otherwise by a human: the finding is `review` with
   both candidates named and a `--symbol <PICK>` placeholder. A `currency_mismatch`
   is `repair` only when EVERY recorded trade states the live listing's
   currency — corroboration is what makes it actionable, and without it the
   venue heuristic gets a `review` and no command. The report WRITES NOTHING:
   each `repair` finding carries the exact `scripts/repair_symbol.py`
   invocation (docs/07 §Symbol repair).

3. **Noise control is the feature.** A canary probe must answer before ANY
   row is judged — a network blip prints ONE `SCAN INCOMPLETE` line and exits
   2 instead of a wall of "your tickers were delisted" (M141's
   `unavailable`-vs-`not_found`, applied to a whole run). Privacy mode still
   costs zero packets (owner-bound ContextVar, and the canary is skipped when
   every in-scope owner is private). The `CASH` placeholder is never probed —
   `CASH` really resolves to Pathward Financial. Names are compared
   tolerantly (case/`&`/punctuation/truncation/abbreviation) so Yahoo's
   `shortName` drift never reads as a finding.

4. **Three phases, no session across a wait** (docs/01 §Sessions and the event
   loop — the class M152 fixed and M153 is finishing): ONE session reads every
   row into frozen plain values and closes; the whole network phase runs with
   NO session in existence; a second session scores the tie-breaks and closes.
   Pinned from inside BOTH network seams by
   `tests/test_repair_report.py::test_no_session_survives_a_provider_wait`,
   which also asserts no backend is `idle in transaction` on the scan's own
   `securities` SELECT — the M152 incident's exact symptom, re-aimed at a new
   caller before it could become one.

**It cannot be an endpoint, by design.** AGENTS §2 keeps fetching out of
report endpoints ("Ingestion and report endpoints never fetch — cache reads
only"), so the surface is the operator CLI `scripts/repair_needed_report.py`
— the same shape as `split_probe.py` and `deep_price_backfill.py` — and
periodicity comes from the host scheduler, the path the M148 autoheal
watchdog already runs on (example.internal has no unattended crontab).

**Live verification (deployed build at the origin/main commit carrying this entry, `--user-id 343`, <N> rows):**

**Live verification (deployed build, prod, `scripts/repair_needed_report.py
--user-id 343`; 25 owned rows, 1 cash placeholder skipped, 3 minutes, exit 1):**

| what the report said | rows | why it is the right call |
| --- | --- | --- |
| **12 `repair`** | 84 `VIU`, 94 `VUN`, 148 `XSP`, 156 `XWD` (currency **and** exchange), **153 `XBB` (bare collision)**, 294 `VGRO.TO`, 296 `VFV.TO`, 297 `VUS.TO` (exchange) | 153 is a live 294-class catch: the row stores the NYSEArca *BondBloxx BB-Rated* product while every recorded trade is CAD — the ledger names `XBB.TO` (iShares Core Canadian Universe Bond), so the report prints `--symbol XBB.TO`. The other seven rows: 294/296/297 are the hand-repair leftovers — symbol and currency were corrected in place (M142/M143) but the VENUE was not, so 294 still reads `PCX` and 296/297 `NMS` — and 84/94/148/156 are rows whose `listing_currency`/`exchange` say USD/NMS while every recorded trade states CAD and the listing answers on the TSX. |
| **3 `review`** | 297 (name vs Yahoo's truncated `shortName`), 298 `dead_symbol_stale`, 298 `bare_twin_rows` (with 293) | exactly the two leftovers docs/10 flagged by hand: `APTX` + `APTX.TO` are two rows for one listing, and 298's dead flag is contradicted by a live probe. No command is printed where the report cannot decide. |
| **11 `info`** | 3 × `bare_collision_agrees` (85 `MSFT`, 88 `NVDA`, 89 `GOOG` vs their `.TO` CDR twins), 8 × `name_is_symbol` | the collision detector's OTHER answer: two listings really do answer (US line + Canadian CDR), and the ledger's USD trades agree with the stored side — reported as "nothing to do", not as a finding to chase. |

Not one false `no_listing`: the canary answered, so every row above was
judged against a market that was actually reachable. Re-run after the deploy:
the same 25 rows, the same 12/3/11, and (after silencing yfinance's expected
`.TO` 404s with `--verbose` to bring them back) a report that reads as one
clean document on the scheduler.

**The scheduled run.** `~/.hermes/profiles/soft-dev/scripts/
fintrack_repair_report.sh` executes that command weekly through the host
scheduler and always exits 0 — exit 1 means "a repair is warranted", which is
a RESULT, not a broken job (docs/07 §Repair-needed report §Periodic).


**Gates.** ruff format/check clean; backend full suite <N> passed / <N>
skipped / 0 failed; frontend `tsc`, `eslint --max-warnings 0`, `vitest run`
(<N> tests), `npm run build` all green; pushed to origin/main and deployed to
example.internal (`docker compose up -d --build api`).

## M154 — a BARE holding ticker could never find the exchange-suffixed security it names

> Board card `t_7ed88c41`, titled "`etf_holdings.holding_symbol` is bare but
> `securities.symbol` is exchange-suffixed — 231 caught unique-violations per
> refresh run, and 2,130 owned holdings never link `holding_security_id`".
> Spec: docs/05 § "Fund composition (M17, providers M147)", docs/07 §
> "Symbol matching (M154)".

**What was wrong.** Two spellings of one ticker, matched by the RAW string.
Providers publish holding tickers BARE (`005930`, `9984`, `HSBA`, `LRCX`) —
measured on prod, 17,530 of the demo tenant's non-NULL holding symbols carry
no dot — while `securities.symbol` is frequently EXCHANGE-SUFFIXED (825 of
1,496), because `lookup_symbol` resolves a bare ticker to the canonical
listing when the row is created. Both fund-composition seams keyed their
symbol→security map on the stored spelling, so:

* `import_fund_composition` looked the provider's `005930` up in a map keyed
  `005930.KS` → None → `holding_security_id` stayed NULL (3,117 of 23,042
  rows linked, 13.5%);
* `ensure_sub_fund_composition` then called `lookup_symbol`, got
  `{"symbol": "005930.KS", "exchange": "KSC"}` back and INSERTED that row —
  colliding with the row that already existed: **231** caught
  `uq_securities_symbol` violations in the M152 seed run (11:00:06 →
  11:09:01), each a wasted provider call plus a failed INSERT plus a
  rollback. The `except IntegrityError` called itself a "concurrent deepen
  race"; it was a lookup miss — and its recovery re-queried the BARE
  spelling (`Security.symbol == "005930"`), found nothing, and `continue`d,
  so the sub-fund was skipped silently.

**The card's premise, corrected twice.** (1) The card said the 2,130
unlinked owned holdings "are exactly the rows the exposure look-through
needs to resolve a holding to a real security". They are not: the pies'
resolver (`services/exposure_breakdown._resolve_sub_security`) does not read
`holding_security_id` at all — it matches composition tickers against
`securities.symbol` EXACTLY, by design, which is why the Vanguard provider
stores its fund legs WITH the venue suffix (`VCN` inside VEQT lands as
`VCN.TO`, docs/07). `holding_security_id` is what the RECURSIVE
`GET /securities/{id}/holdings?recursive=true` asset-class breakdown, the
security-delete guard and `security_dedup` read. Measured before deciding
not to widen: of prod's 27,609 holdings, 3,666 already match exactly and
only **111** of those resolve to a fund that itself has composition rows
(the rows that actually recurse) — **zero** more would recurse under stem
matching, so the exact-only resolver costs the pies nothing today and no
follow-up card was filed. (2) The 2,130 splits, measured properly:
**2,157 recoverable** (44 exact-spelling + 2,113 unambiguous-stem) and **20
not recoverable by matching alone** — the shared stems `005930` and `000660`,
each holding both `*.KS`/KSC stock and `*.KQ`/KOSDAQ fund-facts rows, where
a guess would attribute Samsung's weight to whichever row happened to have
the lower id.

**What changed.** ONE index, `market_data.security_symbol_index`, read by
all three matching seams — `import_fund_composition`,
`ensure_sub_fund_composition` (probe AND end-of-run backlink) and
`PUT /securities/{id}/holdings` (the same defect in the manual bulk-replace,
found while sweeping for raw-spelling maps):

1. exact spelling always wins (`VUN` finds the bare `VUN` row even when a
   `VUN.TO` twin exists);
2. a known venue suffix is stripped for a bare-stem fallback —
   `market_data.EXCHANGE_SUFFIXES`, 45 venue codes (the 37 prod's
   `securities.symbol` actually carried, plus the 20 `company_country_map`
   already knew), verified against every suffixed prod row: 823 distinct
   symbols, 1,135 rows, zero refusals. Warrants/units/rights/preferred
   suffixes (`.WS`, `.U`, `.RT`, `.PR`) are absent on purpose — different
   instruments — and an unknown venue degrades to the exact-only matching
   that shipped before, never to a wrong link;
3. a stem shared by two listings offers NO stem entry, so `005930` never
   silently becomes `005930.KQ`;
4. the creator REUSES the row `lookup_symbol` resolved to instead of
   inserting it, and the `IntegrityError` recovery re-queries the RESOLVED
   spelling — a genuine race now recovers instead of skipping.

A bare probe the index answers costs no provider call at all: the 231
wasted `lookup_symbol` round-trips become zero.

**Verification.** `backend/tests/test_m154_symbol_link.py` (9) — pinned
RED→GREEN (7 failed / 2 guard-green before, 9 passed after). The
zero-violation property is measured, not inferred: a
`before_cursor_execute` listener counts `INSERT INTO securities` statements
at the engine, so the reuse cases assert ZERO inserts (nothing reached the
server, so no constraint was even attempted) and the create case asserts
exactly ONE. The race test makes the collision real — the stub commits the
resolved row between the loop's map build and its INSERT — and asserts the
loser's row is rolled back, not duplicated, and the holding links. The
neighbouring suites that touch these seams (M147 enrichment, etf holdings,
Vanguard providers, security dedup, exposure breakdown, M152/M153 session
pins) pass unchanged: 77 passed.

**Existing rows were re-linked without a re-import** — an operator UPDATE
against prod, dress-rehearsed as a SELECT with aggregates first (the
data-purge rule): the same index semantics in SQL, every unlinked holding
whose exact spelling or unambiguous stem answers. The rehearsal predicted
2,176 (51 exact + 2,125 unambiguous-stem) and 20 ambiguous; the UPDATE
reported **2176**, so tenant 343 went 3,117 → 5,274 linked of 23,042 and
the estate went 4,063 → 6,239 of 27,699, with `Σ weight_pct = 5363.9045`
over 27,699 rows byte-identical before and after (linking writes one FK
column). The 20 ambiguous rows stayed NULL as designed — they belong to
the two artifact-shadowed stems `005930` / `000660`, filed as
`t_08adfa14`.

**The money-adjacent constraint was proven, not argued.** Inside ONE
transaction against prod, `exposure_breakdown.breakdown` ran for tenant
343 across all four dimensions with the links in place, then again with
6,031 candidate FKs NULLed (a SUPERSET of the 2,176 — it also covers the
links the old code made by exact match), then ROLLED BACK: geography (58
items), sector (14), company (7,319) and asset_class (5) all came back
byte-identical, `total_cad` 1,444,820.89 unchanged, each dimension's
`unclassified_cad` unchanged. The pies do not read this column.

## M161 — the price paths held a session across their fetch (the other half of M152/M153)

> Board card `t_11bb1570`, filed under the title "M155: `backfill_range` /
> `ensure_recent_price` still hold a session across their fetch — the
> price-path half of the M152/M153 class". M155 was already taken twice on
> the board (M135-W1 and the repair-needed report above) and M156–M160 by the
> M135 restore waves, so the roadmap number is M161; the card keeps its
> title. Spec: docs/07 § "Price-path sessions (M155/M161)", docs/01 §
> "Sessions and the event loop".

**What was wrong.** M153 closed the enrichment half of the class and named
these two seams as the deliberate non-changes. Both READ first and then wait
on a provider, so SQLAlchemy autobegins and the connection sits `idle in
transaction` for the whole fetch:

* `backfill_range` — `fetch_history` is its FIRST act. `_run_backfill`
  (BackgroundTask, own session) did `session.get` then called it;
  `refresh_security` (`POST /securities/{id}/refresh`) did `owned_or_404`
  then called it on the request's session.
* `ensure_recent_price` — `newest_price` SELECT, then `fetch_latest_close`.
  Reached by `_run_ensure_recent_price` (BackgroundTask, the market-hours
  fan-out), `GET /securities/{id}/overview`, and `valuation.compute_positions`
  — the `/portfolio/positions` read path, which calls it **once per held
  security**.

**The question M153 dodged by scoping to background tasks.** Three of the six
callers are REQUEST sessions, and a release is a commit — so an early commit
could commit a request's pending work. Answered by inspection rather than
assumed: `GET /securities/{id}/overview`, `POST /securities/{id}/refresh` and
`valuation.compute_positions` are read-only up to the point where they wait
(none writes before it; `refresh_security`'s own `db.commit()` is still what
persists its work), so the release commits the read transaction and nothing
of the request's. On a request path the connection is checked out for the
whole request either way, so what the release actually buys is the difference
between `idle in transaction` — the row vacuum waits on — and `idle`.

**The seam, decided per caller** (docs/07 §Price-path sessions carries the
table). `ensure_recent_price` releases INSIDE itself, immediately before
`fetch_latest_close`: it does its own SELECT immediately before the wait, so
no caller can place a release between the two, and the positions loop would
otherwise reopen the transaction once per held security.
`backfill_range` releases AT ITS CALLERS (`_run_backfill` after its
`session.get`, `refresh_security` after `owned_or_404`; M153 already releases
in `ensure_sub_fund_composition`), because one of its four callers must not
get a release at all:

**The caller the release must not reach.**
`services/symbol_repair.repair_symbol` repoints a security's symbol and drops
its stale closes INSIDE one transaction and backfills afterwards, and
`scripts/repair_symbol.py` rolls that transaction back for a dry run
(`--apply` is the write). A release inside `backfill_range` would commit an
operator's read-only dry run — and a fetch that then failed would leave the
bars dropped with the repair row never written, a state re-running the repair
cannot undo (it short-circuits at "already canonical"). That is the rule in
one line: **release a session that has nothing pending; the caller that does
keeps its transaction, and says so.**

**What it costs.** One positions request now pays at most two commits per
held security that actually fetched — the release, then `_cache_closes`' own
`if inserted` commit; never more than three, because `record_quote_outcome`
adds one only when a miss counter or the dead flag actually moves — and zero
for a security whose newest close is already dated `today`, the usual state.
The market-hours fan-out gets the pool connection back for the whole quote.

**Verification.** `backend/tests/test_m155_price_session.py` (8 tests) asks
both M153 questions from inside every provider seam on these paths — no
session the caller opened may be in a transaction, and no client backend on
the database may be `idle in transaction` — against a really-committed tenant
driven through `deps.get_engine()`, the engine the background tasks and
`get_db` draw from (the suite's `session` fixture cannot answer question 2:
its outer transaction is by construction `idle in transaction`). RED before
the fix: **6 failed, 2 passed** — busy at every wait, and the two passing
tests are the pins that must not move (the fresh-close cost of zero, and the
repair caller's exception). GREEN after: **8 passed**. The overview test
carries three waits on purpose: `security_overview` reuses
`compute_positions`, so one overview request carries the whole loop.

**Gates.** ruff `format --check` + `check` clean. Backend full suite on the
rebased tree (this commit on top of M154's `0553133`): **1878 passed / 5
skipped / 0 failed**, exit 0, 18:00 — the skips are
`tests/test_suite_db_isolation.py:652` ×4 (`sudo -n -u postgres psql` cannot
reach a scratch database that lives on the deploy host) and
`tests/test_bills_match.py:950` ×1 (its 1.5 s first-paint budget, load
dependent — it passes when run on its own, which is how the four were
separated from it), both pre-existing and environmental. The pre-rebase run
on this card's own base was 1861 / 5 / 0, so the delta is exactly M154's
seventeen. Frontend untouched, so `tsc`/`eslint`/`vitest`/`npm run build` are
not part of this change's gate. CI: the `CI_ENABLED` kill switch (ed5741f) is
unset, so every `ci.yml` job skips green by construction — the battery above
was run by hand.

**Left alone, on purpose.** `app/services/deep_backfill.py::
deep_backfill_one` calls `fetch_history_candidates` (the M144 multi-candidate
sweep) with the same shape — a SELECT, then a provider wait — and was not one
of this card's seams. It is reached from `scripts/deep_price_backfill.py`, an
operator CLI, and it carries a `dry_run` contract of its own (`apply=False`
"makes the same fetches and writes NOTHING"), so it belongs with
`repair_symbol` on the side of the rule that keeps its transaction until
somebody weights it deliberately.

## M157 — the projection calculator was client-side because the DEMO GUARD said so

> Board card `t_682df591`, titled "M157 (M135-W3): projections server route
> (K-12 / M134 P2-2) — panel stops running the client engine for a
> demo-guard reason". Spec: docs/12 §7, docs/08 §projections, docs/20 §demo
> write guard (exemption 6), docs/33 A21. Wave 3 of the M135 server-side
> restore (M135_RESTORE_PLAN.md §2 row K-12 + §5 backlog P1-7).

**What was actually going on.** `ProjectionsPanel` ran the whole projection in
the browser (`lib/projectionEngine.ts`), and every doc justified it as a
privacy/offline property. It was neither. `POST /projections/run` is pure
request-body math — the handler takes no `Session`, `services/projections.py::
project_scenario` reads no row — so the only thing standing between the demo
tenant and the endpoint was `deps.py::_assert_demo_mutation_allowed`, which
fires on the METHOD, not on any write. M61 #2 moved the math into the browser
to escape a 403 `demo_account_restricted` that was protecting nothing, and
M135 §2 K-12 named the posture for what it is: *"a demo write-guard artifact,
not an E2EE one."*

**The fix is one exemption and one call site.** (1) `/api/v1/projections/run`
joins `DEMO_MUTATION_EXEMPT_PATHS` as the SIXTH entry, its justification
sitting beside it the way the other five carry theirs: nothing is written, and
a run is a deterministic function of the caller's own input, so one visitor's
projection is invisible to the next. (2) `ProjectionsPanel.submit()` awaits
`runProjections(payload)` instead of `runProjectionEngine(payload)`, and the
button carries a `running` state because the answer is a round trip now.

**Choice (a) vs (b), recorded because the card asked for one.** K-12 offered
*either* a read-shaped route *or* a documented demo exemption. A read-shaped
POST twin (`/projections/preview`) would still be 403 for the demo — the guard
is method-scoped, which is why `capture.ts` already routes S3's preview golden
through a throwaway tenant — so it would need the same exemption while adding a
second route for one piece of math; a GET twin would have to carry
`scenarios: [{label, annual_return_pct}]` in a query string. (b) is the honest
fix for the actual defect (a pure compute classified as a mutation) and moves
no contract docs/08 already pins.

**Parity gate (M135 §4.3, hard) — 7/7, both directions, 0 diffs.** The frozen
M65 goldens (`frontend/tests/fixtures/projection-parity.json`, 7 cases) were
NOT re-captured (§4.1 forbids it). They are now checked from three places:

* `backend/tests/test_projections.py::test_run_matches_the_frozen_m65_golden` —
  7 parametrized cases POSTed through the route, response `==` the golden.
  **7 passed**, and this ran BEFORE the panel was flipped.
* `frontend/tests/projectionEngine.test.ts` — the client mirror, unchanged,
  still 7/7 over the same file plus its 9 bounds tests.
* `node frontend/scripts/parity/parity.ts --slice projection` — the same 7
  requests to a RUNNING stack **as the demo tenant** (new
  `engines/projection.ts`, an in-memory adapter for the pre-harness fixture
  shape, and a `--base-url` flag the live slice needs). Negative control
  against the pre-deploy stack: **0/7**, every case `$.status expected 200 |
  actual 403 demo_account_restricted`, exit 1 — the gate has teeth, and it is
  also the demo-tenant invariant K-12 restores, asserted over HTTP.

**The 422 envelope is a contract and did not move.** `detail` is a
pydantic-shaped LIST of error dicts with `code: "validation_error"` — the same
shape `lib/s3Engine.ts` already reproduces byte-for-byte for
`/rules/preview` — now pinned for this route too by
`test_run_validation_envelope_is_byte_identical`, an exact-body assertion
rather than a shape check. What the PANEL renders for one changed on purpose:
the envelope carries no sentence (`ApiError` can only lift a string `detail`),
so `runErrorMessage()` maps a `validation_error` to a bounds copy naming every
bound, and keeps the client engine's precise `ProjectionInputError` sentence
for the offline path.

**Left in place on purpose.** `lib/projectionEngine.ts` is untouched code with a
new header: docs/33 §Server-first rule 5 keeps a client mirror as the last-resort
offline path, golden-parity tested, and M158/M160 own the question of whether it
retires. No engine deletion, no VaultLocked copy change, no touch to
`lib/e2ee.ts`, and the seven M137-flipped report routes were not opened.

**A red this card found and did NOT have to fix.** Three
`seed_transaction(day=_TODAY...)` call sites in `tests/test_budgets.py` still
named the module-level `_TODAY` the wall-clock sweep (98e5cf1) removed — ruff
F821 on a clean `origin/main`, and a collection-time `NameError` for both
parametrizations of that P1-5 test, so the lint gate and the suite were both
red on main when this card started. `f163ac4` (the clock-sweep follow-up)
converted exactly those sites while this card was in flight, so the local fix
made here was dropped during the rebase rather than re-landed.

**Gates** (AGENTS §4, run by hand — the `CI_ENABLED` kill switch is still
unset, so every `ci.yml` job skips green by construction):

* ruff `format --check` + `check`: clean. `npx tsc --noEmit`: clean.
  `npx eslint . --max-warnings 0`: clean.
* Backend full suite on the rebased tree: **1888 passed / 5 skipped / 0
  failed**, exit 0, 19:32 — six concurrent pytest runs were on the box, so the
  wall time is contention, not the work. The five skips are the four
  `test_suite_db_isolation.py` sudo probes (a scratch database on the deploy
  host this LXC cannot reach) and `test_bills_match.py`'s 1.5 s first-paint
  budget, both pre-existing and environmental.
* Frontend: `NODE_ENV=test npx vitest run` **131 files / 1356 passed / 1
  skipped**, exit 0; `npm run build` clean; `npm run check:csp` OK.
* Parity harness, every slice after the refactor: s2a **29/29**, matrix
  **6/6**, s3 **16/16**, s4 **14/14**, s5 **10/10** — 75 cases, each exit 0,
  plus `--self-test` `SELF_TEST_OK`. The harness change added one slice and
  one flag and moved nothing.
* Live, through the deployed stack: `parity.ts --slice projection` **7/7 as
  the demo tenant**, and the browser pass on the Reports → Projections tab —
  numbers on the card.

## M160 — the retirement wave: the client report mirrors are deleted, and their goldens become server-route pins (M135-W5 / M134 P2-5)

> Board card `t_37af4ee6`, titled "M160 (M135-W5 / M134 P2-5): the retirement
> wave — exactly what M158 ruled, one atomic commit, test-count delta named".
> M158 Decision 2 (option c, card `t_f3d62878`) ruled the retirement and
> explicitly overrode M117's do-not-re-litigate; M160-R1 (`t_4fea992a`)
> settled the five execution breakages the ruled file list predates. Spec:
> docs/33 §Server-first rule 5, docs/33 §Rule-8 inventory, docs/12 §1.

**What retired.** The client mirrors of server report math, in one atomic
commit: `frontend/src/lib/reportEngine.ts`, `lib/s3Engine.ts`,
`lib/totalReturnEngine.ts`; the six client-comparison parity adapters
(`scripts/parity/engines/{s2a,matrix,household,s3,s4,s5}.ts`); and nine
mirror tests (`s2bReports`, `s2cDividends`, `s3ClientCompute`,
`s4CapitalGains`, `m77Parity`, `reportEngine`, `m124TagEngine`, `s2aParity`,
`householdParity`). Both legs of rule 5's last-resort justification are void:
`encryption_required` is a write guard that branches zero read paths, so
server compute works fully under E2EE; and the offline leg never had runtime
wiring (no service worker, no offline model — the "fallback" framing was
comment-only).

**What survived, and why it is not a mirror.** M160-R1 carved three residents
out of the doomed files rather than delete them: the BigInt fixed-point
primitives moved to `lib/dec.ts` (the live split-editor money math and
`lib/sankeyEngine.ts` run on them); `budgetsProgress`/`etToday`/`RateRow`
moved to `lib/budgets.ts` as an AUDITED CLIENT-ONLY Row-8 compute — the
partner view's budget progress has no server twin by D3 design, so M135 §4.1
parity is unsatisfiable *by design*; and the three panel loaders
(`getDividendReport`/`getCashflowSankey`/`getTotalReturn`) moved to
`lib/reportClient.ts`, a DEPRECATED shim that existed only until `t_85311c34`
re-pointed the dividends/sankey/total-return panels at their orphan-flagged
server routes and flipped `_report_flags.py`'s premise — **that follow-up
(M160-R2) has landed; see the M160-R2 entry at the end of this file.**

**The harness's comparison half retired; the harness did not.** M158 kept
`capture.ts` and every golden fixture on purpose. Seven of the ten vector
fixtures now have the replay job — five of them wired by THIS commit, while
s5 (M137's `test_search_goldens.py`) and the M65 projection pin were already
wired — via
`backend/tests/test_m160_replay_{s2a_matrix,s3,s4,household}.py`
seed each fixture's captured dataset into a throwaway tenant, replay the
captured request against the mounted route and byte-diff the answer against
the frozen golden — the `test_search_goldens.py` pattern M137 established for
the s5 vectors. **77 golden cases, one-to-one with the retired gate counts
(s2a 29, matrix 6, s3 16, s4 14, household 12)**, plus 6 hand-written
household scoping tests (own-vs-shared directionality, privacy projection,
child drill) = **83 tests collected**, so M155/M159's coverage stays
traceable. Ids never survive a re-seed, so every captured id is translated
through an old→new map; the one shared table (`fx_rates`) is insert-if-absent.

**The three fixtures whose only reader was a mirror test are now replayed.**
`s2b-vectors.json` (net-worth 7 ranges + investment-performance 4),
`s2c-vectors.json` (dividends 4) and `m77-vectors.json` (sankey 3 +
total-return 3) lost their readers to M158's deletion list: their goldens
were diffed by `s2bReports` / `s2cDividends` / `m77Parity`. M160-R1 ruling 3
ordered the replay re-homing for the ADAPTERS it had to adjudicate
(s2a/matrix/household), not for these — building three new replay modules
mid-execution would have widened the amended list, so they stayed on disk
exactly as M158 ruled ("keep capture+goldens as server regression pins").
**[M160-R2] [t_62afc8a5]** all 21 are now wired as route replays, in the same
shape as the M160 modules: `s2c-vectors.json` and `m77-vectors.json` by
`backend/tests/test_m160_replay_s2c.py` and
`backend/tests/test_m160_replay_m77.py` (s2c's 4 dividend answers at the
capture clock, m77's 3 sankey + 3 total-return ranges), and
`s2b-vectors.json` (net-worth 7 + investment-performance 4) by
`backend/tests/test_m160_replay_s2b.py`. Each seeds the fixture's own
captured rows into a throwaway tenant, replays the captured request against
the mounted route and byte-diffs the answer. The routes' own suites still
carry the arithmetic underneath: `test_reports_networth.py`,
`test_reports_performance.py`, `test_dividends.py`, `test_reports_sankey.py`,
`test_total_return.py` (the last is hand-derived inside 1e-8).

**Test-count delta** (one atomic commit; the message names it as numbers).
Frontend, counted the same way on both sides (`npx vitest list`, parent
`07135e1` → this commit): **141 → 132 test files** (9 deleted, 0 added) and
**1459 → 1238 collected cases** (−221). The nine retired mirror tests account
for −213 on their own — `householdParity` 18, `m124TagEngine` 19, `m77Parity` 6,
`reportEngine` 14, `s2aParity` 39, `s2bReports` 29, `s2cDividends` 9,
`s3ClientCompute` 36, `s4CapitalGains` 43 — and the remaining −8 are the two
SURVIVORS the rulings trimmed: `lockedVaultDegradation.test.ts` 19 → 12
(engine-golden halves retired with their engines, ruling 5) and
`splitTransactions.test.tsx` 6 → 5 (`splitReportParity`, docs/44). Fixtures:
**12 → 12, zero deleted** — M158 kept every golden. Backend: **0 → 5 test
files, 0 → 83 test cases** (the four replay modules plus their shared
harness: 77 golden replays + 6 hand-written household scoping tests). The
run battery on this tree is 132 files / 1238 passed / 1 skipped; coverage did
not weaken, it changed subject (engine-vs-golden → route-vs-golden).

**E2EE re-proven where the story changed.** The one locked/unlocked fixture
that named a retired engine is `lockedVaultDegradation.test.ts`, trimmed per
M160-R1 ruling 5: A1–A3 (search) are untouched; A13 now proves
`budgetsProgress` byte-identical locked vs unlocked against the frozen
`budgets.progress` golden, now sourced from `lib/budgets.ts`; A9 does the
same for the dividends compute, now sourced from `lib/reportClient.ts`.
The per-engine halves retired WITH their engines — a server replay cannot
reproduce a client vault state, so those guarantees moved to the server
routes' own tenancy/consent scoping (A4–A8, A14, A15, A20 now cite the
backend suites). docs/33 §Rule-8 inventory now distinguishes (a) client-only
computes with retained lock behaviour (the budgets class) from (b)
server-owned reports where lock is replaced server-side. No surface regressed
to silent-empty: rules 4 and 7 bind, and the degraded-mode matrix
(`m135DegradedMatrix.test.tsx`, 16 cases) still passes.

**The lock-invariance proof moved with the engines (same commit).**
`scripts/locked_vault_gate.py` — the executable form of docs/33's audit table
— cited the per-engine halves this wave deleted, so rows A4–A8, A14 and A15
would have gone WORKS → BROKEN. New module
`backend/tests/test_m160_reports_lock_invariance.py` (10 tests) re-proves each
of those five surfaces at the route with the M134 P1-5 pattern: seed a tenant,
flip `encryption_required` through `PATCH /users/me`, assert byte-identical
JSON under each value *and* a non-zero anchor so an empty answer cannot pass.
The gate's rows were re-pointed at those nodes — and its four STALE needles
(A2, A3, A10, A12 still naming the `*_route_removed` tests M137 undid) were
re-pointed at the P1-5 tests docs/33 itself cites. **The gate now exits 0:
19 GREEN / 1 KNOWN RED (F2, tracked) / 0 BROKEN, against 15 GREEN / 4 BROKEN /
1 KNOWN RED at the parent commit.**

**Gates** (AGENTS §4; the `CI_ENABLED` kill switch is still unset, so every
battery ran by hand): backend full suite, ruff `format --check` + `check`,
`npx tsc --noEmit`, `npx eslint . --max-warnings 0`, `npm run build`, the
trimmed `lockedVaultDegradation.test.ts` (A1–A3, A9, A13, 12/12 + the rule-8
standing gate), the four `test_m160_replay_*` modules (83 tests),
`test_m160_reports_lock_invariance.py` (10 tests), `scripts/locked_vault_gate.py`
(19 GREEN / 1 KNOWN RED / 0 BROKEN), and the `parity.ts` harness with the six
retired slices refusing loudly (exit 4, naming their replay home) rather than
pretending to compare.

## M163 — `_listing_currency` stamped every non-Canadian listing USD (the security-296 class, generalised)

> Board card `t_cbd488cf`, titled "Follow-up to M162: `_listing_currency`
> stamps every non-Canadian listing USD — `.KS` live-probes KRW (the
> security-296 class, generalised)". Found during M162 (`t_08adfa14`).
> Spec: docs/07 §"Listing currency (M163)", docs/02 §securities, docs/08
> §`GET /securities/lookup`.

**What was actually going on.** `securities.listing_currency` had exactly
one source: `_listing_currency`, a venue heuristic (`.TO`/TSX/TSXV/NEO →
CAD, else USD). Yahoo's own `Ticker.info` body states the real currency and
the payload builders threw it away, so the heuristic answered **USD** for
every market it does not name. The label is spent by docs/06 pool math,
docs/12 valuation and docs/15 exposure — a KRW holding was valued at the
KRW/USD ratio, which is security 296 (a CAD holding stamped USD) with the
venue changed.

**The measurement that widened the card.** The card's own blast-radius
section named "the two real KSC rows prod owns" (`005930.KS`, `000660.KS`).
The SELECT says the population is **466 Korean `.KS`/`.KQ` rows across two
tenants** — the demo tenant (user 1) with 83 and a real tenant (user 343)
with 383 — every one stamped USD, none of them the M162 artifact class
(all `kind=stock`, no comma-joined names), and **zero trades, zero cached
prices, zero ACB pools, zero watchlist rows** attached to any of them. So
nothing was mis-valued yet: this is a wrong label waiting for the first
trade, not a wrong number today. Every one of those rows was created by
the deepening loop's `lookup_symbol` → INSERT path, which is exactly the
path the fix closes.

**The contract** (docs/07 §Listing currency carries the full text):

* the body's `currency` **wins** when `market_data._normalize_currency_code`
  accepts it — three ASCII letters, uniform case — so `005930.KS` → KRW,
  `9984.T` → JPY, `RY.TO` → CAD;
* `_listing_currency` survives as the **fallback** for payloads with no
  usable code: every search row (they carry none, live-measured) and any
  body whose field is absent or type-junk — the body-contract fuzzer's
  mutation corpus is what that rule protects;
* **mixed case is refused**: LSE's `GBp` (pence, live on `HSBA.L`/`VOD.L`)
  would become a 100× price error as `GBP`, so London keeps its old USD
  answer and is named a residual instead of being half-fixed.

**The FX ruling, asked for by the card.** Adopt the provider's currency and
let FX serve it — never refuse at create. KRW is already in
`CURATED_FX_BASES` because BoC publishes `FXKRWCAD` (live-probed 2026-10-11:
200, `2026-10-01 → 0.001046`; the DKK control 404s), so the 6-hourly worker
keeps the pair warm, `latest_rate` fetches a true miss, `FxRateBook`
backfills a trade-date span and `GET /fx/status` reports the pair the
moment a row holds it. A currency BoC never published still answers
`422 fx_rate_missing` at report time — the documented "add a manual rate"
path, loud rather than silently wrong. Pinned end to end in
`test_m163_provider_currency.py`: a KRW holding's ACB rebuild refuses
without a cached rate and then builds **C$742.66** (10 × ₩71,000 ×
0.001046) with one.

**The repair.** After the deploy, one column, dress-rehearsed as SELECT
first (fin-tracker's data rule): `UPDATE securities SET listing_currency =
'KRW' WHERE symbol ~ '^[0-9]{6}\.(KS|KQ)$' AND listing_currency = 'USD'` —
466 rows, zero of them carrying money, so no valuation, exposure pie or ACB
pool moves. The only visible consequence is correct: `GET /fx/status` gains
a KRW pair for both tenants, and the repair report names the class from now
on (`currency_mismatch` compares the stored currency against the
provider's, which it could not do while the provider's own answer was the
same wrong USD).

**Gates** (AGENTS §4, run by hand — `CI_ENABLED` is still unset, so every
`ci.yml` job skips green by construction; the battery below IS the verdict):

* ruff `format --check` + `check` over the whole backend: clean, 448 files.
* Backend full suite on the REBASED tree (origin/main `5cc931f` plus this
  commit): **2145 passed / 5 skipped / 0 failed**, exit 0,
  30:32. The five skips are the four `test_suite_db_isolation.py` sudo
  probes (a scratch database on the deploy host this LXC cannot reach) and
  `test_bills_match.py`'s first-paint budget — both pre-existing.
* Targeted pins re-run after the rebase: M142/M143/M144/M149/M156/M162
  lookup + body-contract-fuzz + M163's own 33 — **241 passed**.
* Frontend on the same tree: `npx tsc --noEmit` clean, `npx eslint .
  --max-warnings 0` clean, `NODE_ENV=test npx vitest run` **134 files /
  1399 passed / 1 skipped** exit 0, `npm run build` clean (20.8 s).
* **One red, attributed and dismissed**: the first (pre-rebase) full run
  failed `test_m135_restore_sweep.py::test_every_docs33_audit_row_cites_a_test_that_still_exists`
  with "the Proof parser only extracted 14 citations". It is the M156
  docs/33 parser gate, red on my base `ff68d3a` (sibling cards `t_70d46e5e`
  / `t_73431a86`) and green again from `5cc931f`, which fixed the parser to
  read path-qualified citations. Measured both ways, same venv, one node:
  fails on `ff68d3a`, passes on `5cc931f`. Not this card's, and not in the
  shipped tree.

## M164 — two spellings of ONE venue made two `securities` rows, and 10 fund legs stayed unlinked behind the "ambiguity"

> Board card `t_eef545e2`, a follow-up to M162 (`t_08adfa14`): "same-symbol
> duplicates with different venue SPELLINGS (SHH/SHSE, OCSE/CPH) keep the last
> 10 legs unlinked — dedup's key never collides across aliases". Spec:
> docs/02 §securities, docs/07 § "Venue aliases (M164)".

**What was wrong.** `uq_securities_symbol UNIQUE (user_id, symbol, exchange)`
means a second spelling of one venue is a second row for one listing — and the
provider is not consistent about which code it sends for one venue. Measured on
prod 2026-10-11: tenant 343 owned `600900.SS` twice (row 2555 `SHH`, row 2705
`SHSE`) and `PNDORA.CO` twice (1721 `OCSE`, 2502 `CPH`). All four rows were
orphans — 0 prices, 0 trades, 0 fundamentals, 0 attachments — created 10:09–
10:33 UTC inside one deepening sweep, most Shanghai rows of which came back
`SHH`: the drift is the PROVIDER's, not the user's. Because
`security_symbol_index` (M154) counts two rows sharing a stem as two listings,
the bare `600900` / `PNDORA` legs of seven Vanguard funds could never link —
exactly the last 10 NULL legs estate-wide (M162's relink had left them).

**The ruling.** A venue-alias table, `services/venue_codes.py` — an explicit
alias → canonical map (`SHSE`→`SHH`, `OCSE`→`CPH`, `TOKY1`/`JASDQ`→`JPX`,
`HONGK`→`HKG`, plus M142's Canadian `TOR`/`TSE`/`XTSE`/`XTSX`/`TSXV`→`TSX`
moved in unchanged). A pair enters only with same-listing evidence, probed live
from the api container: bare `7182` answers `TOKY1` "Japan Post Bank Co Ltd"
while `7182.T` answers `JPX`/"Tokyo" for the same company (`2454`, `1299` the
same way). It is per PAIR of codes, never per symbol — bare `9503` is SAUDI
"Baazeem Trading Co" while `9503.T` is JPX "KANSAI ELECTRIC POWER", and prod
holds `VIU`/`VIU.TO`, `VUN`/`VUN.TO`, `XBB`/`XBB.TO`, `BN`/`BN.TO`,
`SHEL`/`SHEL.L` as deliberate twins (docs/05), so a symbol-wide rule would
merge companies. Codes the provider names separately stay separate (the Nasdaq
tiers `NMS`/`NGM`/`NCM`/`NAS`), and `CHIX` is left unruled for want of
unambiguous evidence.

**Who owns it.** `market_data._normalize_exchange` (the provider-payload seam
that has canonicalized TSX-family MICs since M142) writes the canonical code,
so a second spelling cannot be stored again; `security_dedup` groups by the
canonical code, so migration `0049_merge_venue_aliases` re-runs the M117 repair
and folds the twins with its existing machinery (keeper = most price history,
references repointed, `acb_pools` conflicts refused) plus one new step — the
survivor is REWRITTEN to the canonical spelling (`report.canonicalized`).
`security_symbol_index` stays alias-blind on purpose: after the repair there is
one row per listing, and the index still refuses to guess a genuinely shared
stem. The M154 relink, which used to be ad-hoc SQL, is now
`services/holding_relink.py` + `scripts/relink_holdings.py` (dry-run by
default) so the same operator pass can run again.

**Prod repair (dress-rehearsed).** Deploy ran migration 0049: **2 groups
merged, 2 losers deleted, 1 survivor re-spelled** (`1721` OCSE→CPH; the
`600900` keeper `2555` already carried the canonical `SHH`, and both keepers
were chosen by the tie-break — zero prices on either side, so lowest id) — and
no repointing at all, because all four rows were orphans. `scripts/relink_holdings.py --apply` then reported **0 legs linkable
before** (measured read-only on the running image) and **UPDATE 10 after**:
tenant 343's bare `600900`/`PNDORA` legs now link to the merged rows. Estate
linked 6,245 → **6,255** of 27,699. Exposure pies (tenant 343, all four
dimensions) byte-identical before/after — the pies' resolver does not read
`holding_security_id` (M154's measured decision).

**Gates** (AGENTS §4, run by hand — the `CI_ENABLED` kill switch is unset):
ruff `format --check` + `check` clean over 453 files; RED **7 failed / 3
passed** with `security_dedup.py` + `market_data.py` stashed → GREEN **24
passed** for `tests/test_m164_venue_aliases.py` + the dedup suite + M154's 9
pins, re-measured on the REBASED tree (`a794f2d`'s M163 currency work is in
the same `market_data.py`); backend full suite **2159 passed / 1 skipped / 0
failed**, exit 0, 19m23s on a contended box;
frontend
untouched (zero frontend files changed — `git show --stat` is backend +
docs only). Deployed to example.internal (`docker compose up -d --build api`),
container healthy, alembic at `0049_merge_venue_aliases`, and the new script
smoke-tested inside the running image before and after the merge.

## M165 — the recurring prompt offered "Create bill" for bills that already existed, and split ONE payee into two candidates

> Board card `t_028b7476`, titled "M165 BUG". Spec: docs/09 §Recurring prompt
> (the new §Already a bill), docs/09 §Bills you probably have, docs/25 §M116
> D5, docs/08 §`GET /transactions/recurring/candidates`, docs/02
> §recurring_suppressions.

**What was wrong (two defects behind one screenshot).** Mark's M96 card on
mobile listed YouTube Premium **twice** — `GOOGLE *YouTubePremium` and
`Google*Youtubepremium`, each with a **Create bill** button — for a
subscription he already keeps as a bill (id 66, `Google *Youtubepremium`,
monthly, 25.98, `contains`, TWO terms: he had hand-added the second spelling
after seeing the bug), and it offered **Create bill** for `SP REITMANS` too.
Measured on prod 2026-10-11 (tenant 343, 1,600 committed rows in the
`LOOKBACK_DAYS=400` window), replayed locally through the real detector before
any code was touched — the answer was FIVE candidates, two of them the same
payee:

```
'ELECTRONIC FUNDS TRANSFER …BRGHTWHL ORANGE…|biweekly'          n=16  -478.30
'ELECTRONIC FUNDS TRANSFER …ORANGESEEDSMONT BUSINESS PAD|biweekly' n=10 -478.30
'SP REITMANS|monthly'                                           n=3   -321.06
'GOOGLE*YOUTUBEPREMIUM|monthly'                                 n=3    -25.98  ← same payee
'GOOGLE *YOUTUBEPREMIUM|monthly'                                n=10   -25.98  ← same payee
```

1. **`subscriptions.normalize_payee_key` kept whitespace.** It upper-cased and
   stripped a trailing `#?digits`, so `SUPERSTORE 1234` ≈ `Superstore #1234`
   already collapsed — but the space before `*` survived, and with it the
   split between `GOOGLE *YOUTUBEPREMIUM` and `GOOGLE*YOUTUBEPREMIUM`. The
   three spellings in his ledger (`GOOGLE *YouTubePremium`, `Google
   *Youtubepremium`, `Google*Youtubepremium`) are one payee whose statement
   formats disagree only about where the spaces go.
2. **Neither suggestion surface ever read `bill_reminders`.** The prompt card
   (M96) and the dashboard's subscriptions card (M138) both build their rows
   from the detector and offer the Create-bill hand-off unconditionally, so an
   existing bill could neither quiet the offer nor say "already a bill".

**The ruling.** Whitespace is not identity, and never offer to create what
exists.

* **`normalize_payee_key` = UPPER, drop every whitespace run, strip the
  trailing `#?digits`.** Case, whitespace and branch numbers — and nothing
  else: no fuzzy matching, no punctuation folding (`A *B` ≠ `AB`), because a
  normalize that guesses is two normalizes again. It stays the ONE key: the
  detector, `bill_match`, `match_terms`, `merchant_answered` and the TS port
  all read it, so "same payee" means one thing in all five places.
* **The TS port is `frontend/src/lib/payeeKey.ts`**, and `lib/s3Engine.ts` now
  imports it RUNTIME with an explicit `.ts` extension (its only runtime
  import; `node <file>.ts` type-stripping still runs the file — the s3 parity
  harness proves it, 16/16 against the frozen goldens, whose payees are all
  upper-case so their grouping is unchanged).
* **A stored suppression key is RE-READ through today's normalize**
  (`services/recurring.py::rekey_suppression`, applied to the merchant segment
  at query time and unioned with the raw value): `recurring_suppressions.group_key`
  is the key as it looked when the user answered, so re-reading keeps every
  previously declined group declined — no data migration, no
  `UNIQUE(user_id, group_key)` collision, and the answer can only ever get
  WIDER. Mark's three stored dashboard dismissals (`GOOGLE *YOUTUBEPREMIUM|subscriptions`,
  `GOOGLE*YOUTUBEPREMIUM|subscriptions`, `SP REITMANS|subscriptions`) all still
  answer after the change (pytest-pinned for both sources).
* **Coverage is computed in the browser, from the cache the Bills page already
  owns** (`lib/billCoverage.ts::coveringBill`, key `["bills"]` → `GET /bills`,
  which is one ledger scan per request by design — docs/25 §M116 D5): enabled
  × cadence tolerance × the bill's OWN rule (`readTerms`/`readLogic`/
  `match_mode`, compared through the same shared key). Cadence tolerance,
  deliberately: the detector's `monthly` fit spans 26–32-day gaps, so a
  `monthly` bill covers a 28-day cycle, and a `four_weekly` fit IS a 28-day
  cycle — a month by another name. Weekly/biweekly have no bill cadence and
  `one_time` is not a stream, so neither is ever claimed. **Unknown is never
  "covered"**: a bills read still in flight or failed claims nothing and the
  row keeps its hand-off (which only opens a DRAFT — M55 d6 writes nothing
  until the form is submitted).
* **The rendered contract** is ONE component,
  `components/bills/CoveredByLine.tsx`: a muted `covered by '<bill name>' → /bills`
  line with a `Link` — never a `Button` — replacing the Create-bill CTA on
  **both** surfaces (`RecurringPromptCard`, `SubscriptionsPanel`). `Tag as
  recurring`, `Not a bill`, the card counts and the suppression POST are all
  untouched: coverage removes an affordance, never an answer.

**What the data says after the fix.** The same 1,600-row replay now answers
FOUR candidates, with YouTube collapsed to ONE group
(`GOOGLE*YOUTUBEPREMIUM|monthly`, 13 occurrences, 2025-09-30 → 2026-09-28,
−25.98) — so on /txns Mark sees a single row, and because bill 66 matches it
(one `contains` rule over that key) it carries `covered by 'Google
*Youtubepremium' → /bills` with no Create-bill button. `SP REITMANS` keeps its
CTA on purpose: tenant 343 has NO Reitmans bill (verified in
`bill_reminders` — id 66 is the only bill), so offering one there is the
truthful answer, not the bug.

**Gates** (AGENTS §4, run BY HAND — the `CI_ENABLED` kill switch is unset, so
every `ci.yml` job answers `completed skipped` and ran nothing): ruff
`format --check` (454 files) + `check` clean; backend full suite **2172
passed / 1 skipped / 1 failed** (30m09s), the one red being
`tests/test_reports_tags.py::test_matrix_and_drill_are_indexed_at_10k_rows`,
a wall-clock (<1.5 s) + EXPLAIN plan pin that is **green solo (1 passed,
5.4 s)** on this box, which was running four other agents' suites at load 11.8
(contention, not this diff: the test seeds its own ledger through raw SQL and
touches no payee key). A SECOND full run (25m42s, under the same load) came
back **2172 passed / 1 skipped / 1 failed** with the red MOVED to
`test_suite_db_isolation::test_wait_for_blocker_finds_its_own_backend_when_
another_ones_there` — `psycopg.errors.InsufficientPrivilege` on
`DROP DATABASE … WITH (FORCE)`, the concurrent-backend class AGENTS §5
documents (11 suites were live) — i.e. the failing NODE changes between runs
while the code does not: contention, twice. The changed-path suites —
`test_recurring`,
`test_bills_match`, `test_m121_multi_term`, `test_m138_subscriptions_card`,
`test_m122_bill_ux`, `test_m29_aggregates` — are **104 passed / 1 skipped**;
frontend `tsc --noEmit` + `eslint --max-warnings 0` clean, `vitest run`
**1421 passed / 1 skipped across 136 files**, `npm run build` +
`npm run check:csp` clean, and `node frontend/scripts/parity/parity.ts
--slice s3` **16/16** (the engine's new runtime import included); secret scan
empty. Docs reconciled in
the same commit: docs/02 §recurring_suppressions, docs/08 §the candidates
route, docs/09 §Recurring prompt (+ the new §Already a bill) and §Bills you
probably have, docs/25 §M116 D5 + its Verification list.


## M167 — a pence listing is refused: the quoting unit is a fact about the listing, never about the venue

> Board card `t_8b9d7b6e`, the residual M163 named itself: "LSE pence
> listings (`GBp`) still fall back to USD — the one market the app knowingly
> mislabels". Found during M163 (`t_cbd488cf`). Written when M165 was free;
> a sibling took M165 (bills, `t_028b7476`) while this card ran, so this
> one is **M167**. Spec: docs/07 §"A minor-unit listing is refused (M167)",
> docs/02 §securities, docs/08 §`GET /securities/lookup`.

**What was actually going on.** M163 believed a `Ticker.info` body's own
`currency` field, but refused MIXED case — `GBp` is pence, and storing
`GBP` would multiply a pence-quoted price by 100 (the M87 split class). The
refusal stopped at the CODE: the body then fell through to
`_listing_currency`, the venue heuristic, which has no London rule and
answered **USD**. So every London listing adopted through any path carried
a wrong label by design-in — the last one the app kept on purpose.

**The card's premise, re-measured (it was false).** "There are zero `.L`
rows in prod today" — the SELECT says **9**, across three tenants (demo
user 1 with 3, the real tenant 343 with 5, the M146 probe account 516 with
1), all `exchange=LSE`, all `listing_currency=USD`, and **zero trades,
zero cached prices, zero ACB pools, zero watchlist rows, zero
fundamentals** on any of them. 21 `etf_holdings` weight rows point at
those nine: they are look-through legs of global funds (HSBC, Shell,
AstraZeneca, Hongkong Land, ADIB Sukuk, Peel Hunt), not holdings. So no
valuation is wrong today — a wrong label waiting for the first trade.

**The probe that killed option (b).** The card offered (a) refuse the
listing outright or (b) adopt it and normalise the unit — store `GBP` and
divide the provider's prices by 100 through a per-venue factor the price
cache and the trade importer both carry. LIVE-PROBED in the api container
(`m167_lse_probe.json` plus a second run for Cboe UK, every `.L` listing
prod holds plus four more): seven UK listings answer `GBp` (HSBC 1395.8,
Shell 3793.5, AstraZeneca 11998.0, Peel Hunt 93.0, Vodafone 118.35, Rio
Tinto 7126.0, and HSBC again on Cboe UK — `HSBAL.XC`, 1395.8, the same
pence number) and **two answer `USD` on the very same `exchange: LSE`**
(ADIB Sukuk 100.432, Hongkong Land 7.41). The UK venues have no single
quoting unit, so a
per-venue factor would have divided a correct USD price by 100 — and on
the price path there is no unit to divide BY at all: `Ticker.history`
carries no currency, and a `.info` call per UK fetch is the extra
round trip M144 refused. Option (b) is not worth its blast radius for a
class of holding with zero trades and zero prices in prod.

**The contract** (docs/07 carries the full text):

* `market_data.provider_currency_is_minor_unit(value)` — three ASCII
  letters in MIXED case is the provider's one spelling of a minor unit. It
  is shape-based on purpose (`Usd` counts; Yahoo never sends it, and
  refusing a body can never become a wrong number);
* `_is_real_quote` refuses the WHOLE BODY on it, one rule after M162's
  artifact rule: `resolve_symbol` → `none`, `lookup_symbol` → `None`,
  `GET /securities/lookup` → 404 `lookup_unavailable`, the add form falls
  into the pre-existing UNRESOLVED path (`listing_currency=''`,
  `dead_symbol=True`, no backfill queued), and the deepening loop
  `continue`s — no row, which is how all nine prod rows were created;
* `_search_equity_quotes` DROPS a UK row whose unit it cannot state —
  `.L` (LSE) and `CXE` (Cboe UK) — through
  `provider_row_unit_is_unknown`: a search row carries no `currency` at
  all, so adopting one is a coin flip between a wrong label and a 100×
  price. Both venues were probed: LSE answers `GBp` and `USD`, Cboe UK's
  `HSBAL.XC` answers `GBp` at the same pence number `HSBA.L` reads. The
  chooser loses those rows with it;
* the type-junk half is load-bearing and pinned: an absent field, `nan`,
  `True`, `b"GBp"`, the artifact triple — none is a minor unit, so every
  body the M162/M163 fixtures pin keeps its answer;
* the PRICE path is deliberately unchanged, and the reason is measured: a
  blanket `.L` fetch refusal would refuse `HKLD.L`/`AV98.L`, whose USD
  closes and USD labels are correct today. A hand-created London row stays
  the user's own claim (M163's "never refuse at create" ruling) and is
  named by the standing repair report;
* the split-repair axis is untouched: `to_as_traded` multiplies by split
  RATIOS, and M167 introduces no unit factor at all.

**The repair (dress-rehearsed, one UPDATE).** Five of the nine prod rows
are the pence class (`HSBA.L` ×3, `SHEL.L`, `PEEL.L`) and carry the wrong
label; four are USD-quoted on the LSE and are labelled correctly. The
repair takes the five to the state this contract describes for a listing
the app cannot value — the M143 UNRESOLVED marker the add form itself
writes (`listing_currency=''`, `dead_symbol=True`,
`dead_symbol_reason='London listings are quoted in a unit this app cannot
name (M167)'`) — and leaves the four alone. Stop conditions, all 0 before
and after: trades, prices, ACB pools, watchlist, fundamentals. No row is
deleted, no `etf_holdings` link moves, and the standing report now reads
those five rows as `unresolved_still` (info) instead of agreeing with
their USD label.

**Gates** (AGENTS §4, run by hand — `CI_ENABLED` is unset, so every
`ci.yml` job skips green by construction; the battery below IS the verdict):

* ruff `format --check` + `check` over the whole backend: clean, 455
  files. (The one drift the gate found on my base `441a19d` —
  `tests/test_m145_account_cash_source.py` landed unformatted — was fixed
  on main by a sibling as `27800ac` while this card ran, so this commit
  touches nothing of M145's.)
* Targeted lookup pins on the shipped tree — M167's own 65 + M163's 33 +
  M162/M164 + the body-contract fuzzer + M141/M142/M143/M144 deep-backfill
  + the repair report + the markets and privacy suites: **429 passed / 0
  failed**.
* Backend full suite on the rebased tree (origin/main `627a978`, which by
  then also carried M165's bills work and M166's sector work): run 1
  **2296 passed / 1 skipped / 1 red**, run 2 (same tree, same venv)
  **2297 passed / 1 skipped / 0 failed**, exit 0, 30:01. The one red was
  `test_suite_db_isolation.py::test_reaper_ignores_a_database_that_looks_dead_but_is_in_use`
  — the suite-DB reaper's "connected → not reaped / disconnected →
  reaped" pin — and it is suite-CONTENTION, not code: it passes **23/23
  solo** on the same tree, and three other agents' `pytest` runs were
  live on this box during run 1 (the reaper's second call found a peer
  backend attached to the scratch database, the exact autovacuum/peer
  condition AGENTS §5 documents). Nothing in this commit touches
  `conftest.py` or the reaper — and a sibling fixed that exact race
  upstream as `4eb21a8` ("drain the scratch database before the DROP")
  while this card ran: green on the rebased tree, 121 passed for the
  isolation file plus M167's and M163's own modules.
* Frontend: **zero frontend files changed** (`git show --name-only`), so
  the web image is not rebuilt; a grep of `frontend/src` + `frontend/tests`
  for `GBp`/`LSE`/`HSBA`/`.L"` returns nothing — no client code knows this
  contract.
* Deploy + smoke + the prod repair are OPERATIONAL, not code: measured
  after the push and recorded in evidence.md §6–§7 (attachment on the
  board card), not here.


## M167 — the exposure Geography view gets a world map, and every breakdown list gets one fold rule

> Board card `t_c58f6afa`, Mark's mobile screenshot on /investments → Exposure
> → Geography: "perhaps for geography we can also along with the list show a
> world map with different shading to show percentages" + "maybe cut off once
> the list gets too long and add it to other but then allow to expand".
> NOTE the numbering scramble: a sibling's pence-listing fix above ALSO landed
> under **M167** (it took the number while this card ran) — two features, one
> number; renumber whichever one Mark prefers. Spec: docs/09 §Exposure
> visuals, docs/16 §Frontend, docs/12 §Chart accessibility.

**What was actually going on.** The Geography list was correct and unreadable:
~40 countries deep on a 390px phone with no visual at all, and the same
long-tail problem in every other dimension's table. The pie had no map because
M126's rule had already banned a chart library, and nobody had drawn one by
hand yet.

**What shipped.** `frontend/scripts/gen_world_map.py` projects Natural Earth's
public-domain 1:110m countries into an equirectangular frame and COMMITS the
result (`components/charts/worldMapData.ts`, 181 countries, 49.6 KiB) — the
map is hand-rolled SVG in `ExposureWorldMap.tsx`, six weight buckets mixed from
the donut's own accent, shaded countries focusable and labelled, the table
still the source of truth. A country the layer cannot polygon ships as a
dot; a bucket the geometry cannot draw (a REGION from the holdings column) is
printed under the map with its weight. The fold is one shared rule
(`lib/exposureFold.ts`): top 8 — top 10 for companies — plus an `Other` row
holding the tail's exact cent-sum, fed to the table AND the donut, with a
per-session expander. `Unclassified` is never folded.

**Verification.** 33 new tests (bucket thresholds, both ramps hex-pinned with
measured contrast floors, fold arithmetic in cents, the real page's collapse /
expand / map-to-row path, M139 mask inheritance); the whole web battery green
(141 files, 1459 passed) and the deployed walk runs through
`frontend/scripts/a11y/m167_exposure_map_probe.py` (axe both themes, no
horizontal scroll at 390, keyboard walk, console clean).

## M169 — the exposure drill-down: click a bucket, see what made it

> Board card `t_438a0065`, Mark's follow-on to M167's geography map: "neat
> would be to drill down what security or fund is from that place and in the
> case of fund what holding(s) if possible." The exposure engine had
> attributed every dollar through M19's recursive look-through since M19 — the
> browser just received the AGGREGATE per bucket with the contributions
> dropped. This is a payload extension plus a UI drilldown, not a new
> computation. Spec: docs/08 §"Breakdown drill (M169)", docs/16 §"Exposure
> drill (M169)", docs/09 §"Exposure drill — clicking a bucket to see what made
> it (M169)", docs/07 §"M169 adds no seam".

**The wire — one parameter, no new endpoint.**
`GET /portfolio/breakdown?dimension=…&drill=<bucket key>` is the SAME
endpoint with one extra optional parameter. `drill` is the exact `key` of an
item in the response, URL-encoded; bucket keys are unique within a dimension
(they are the aggregation dict's own keys), so `(dimension, drill)` is the
address and no bucket id needs minting. A key that is not a bucket in that
dimension answers **404 `drill_bucket_not_found`** — an empty drill would read
as "nothing holds this", a different and false claim. A caller that never
passes `drill` receives a payload byte-identical to the pre-M169 one: the
`drill` key is absent, not `null`.

The response gains `drill: {key, dimension, bucket_market_value_cad,
bucket_weight_pct, contributions_cad, contributions[], other, depth_cap}`.
A **contribution** is a top-level position (`position_kind: "direct"` — you
hold the security itself and it IS its own leaf, `holdings: []`; or `"fund"` —
a position the walk looked through, whose `holdings` are the leaves it
contributed). A **holding** is one composition row: `native_weight_share` is
the PROVIDER's published weight in its own parent (before renormalization),
`provenance_source` is M149's (`vanguard` / `issuer` / `yfinance` / `manual`),
and `via` is the chain of fund symbols the money passed through.

**The one honesty rule: `via` is the walk's own path.** A Microsoft leaf
reached as VEQT → VXUS reports `via: ["VEQT", "VXUS"]`, so the payload can say
"your $x reaches Microsoft of the USA via VEQT→VXUS" instead of implying you
hold Microsoft directly. Depth is capped at `depth_cap` (= `MAX_DEPTH` = 3) by
the WALK, not by the payload: a deeper chain is never generated, and the
existing "3 levels max" copy discloses it.

**It is the same walk.** `breakdown(..., drill=)` threads one `_Trail`
collector through `_distribute` / `_sink` / the two top-level branches; every
attribution the walk already performs is also recorded, but only when the cent
lands in the drilled bucket and only when a drill was asked for. No attribution
rule is re-implemented — the denominator, the renormalization, the cycle guard,
the depth cap and the M166 sink are the SAME code paths, which is what makes
the drill reconcile to the bucket by construction.

**Slices with no row behind them.** M166's fund-mix estimate and the M19
denominator residual are real dollars in a real bucket that no holding
accounts for. They become one holding row naming the FUND that produced them
(`estimated: true` / `unattributed: true`, `provenance_source: null`). Dropping
them would break reconciliation; attributing them to a named holding would
fabricate a fact.

**Reconciliation (M91's pin) and bounds.** `Σ contributions + other ==
bucket_market_value_cad`, and for every fund `Σ holdings + holdings_other ==
its contribution`. Rows round per-row, so `contributions_cad` is the SERVER's
number and the client renders it rather than re-adding rows. Each level keeps
its top `DRILL_N` = 25 by value and folds the tail into one `other` block
carrying its exact cent-sum and count — M167's truncation rule, on the wire.

**The UI — an expand panel, not navigation.**
`components/ExposureDrillPanel.tsx` mounts INSIDE the breakdown table under the
row it describes, because the question is always comparative and a page
navigation destroys the comparison the moment it answers it (M126's contract).
The bucket row is clickable; the chevron beside its weight is the keyboard
path — a real `<button>` with `aria-expanded` / `aria-controls`, the latter set
only while the panel exists. A fund's holdings expand on demand through the
same contract; a fund with NO imported holdings says `no holdings imported`
instead of offering a chevron that opens a hole. The panel quotes the SERVER's
total, prints each leaf's `via` chain in words, wears the provenance badge per
row, marks the estimate `est` and the residual `not placed`, and footnotes the
depth cap. Values mask under M139 via the ambient `fmtCad`; percentages and
names stay visible. One drill open at a time.

**Out of scope, as scoped.** No new attribution math (the walk already
attributes), no new data sources (the drill works with whatever enrichment the
M147/M149/M166 chain delivered), and no change to M167's map visuals.

**Verification.** `backend/tests/test_m169_exposure_drill.py` (18 tests) pins
the wire contract, the 404 envelope, the direct-stock case, the 3-level chain,
the cycle back-edge, the depth cap, totals identity at every level across five
(dimension, key) pairs, the assertion that the drill never changes the
aggregate it drills, the bounded drill, the per-row provenance, the M166
estimate and residual propagation, and tenancy. The 17 pre-existing exposure
tests still pass. `frontend/tests/m169ExposureDrill.test.tsx` (15 tests) drives
the real Investments page: inline open with the table intact, the `?drill=`
request, no drill fetched for an unopened bucket, both `via` hops rendered,
the keyboard-reachable holdings toggle, the server's total, the provenance
badges, the estimate and residual flags, the 25 + `Other` fold reconciling to
the contribution, mask inheritance, the depth-cap disclosure, close-on-second-
click, the in-place 404, and all four dimensions offering the drill.

## M170 BUG: the calendar's subscriptions series was the detector's guesses — confirmed-only (bills plus user-accepted recurring), one shared query

> Board card t_ef4690ae, titled "M170 BUG". Spec: docs/36 section M170 + docs/09

**What shipped.** `backend/app/services/confirmed_subscriptions.py` is THE one
confirmed-subscription query — `confirmed_subscriptions(session, user_id, *,
today=None) -> list[ConfirmedSubscription]`, a frozen dataclass of `payee`,
`merchant_key`, `source` (`bill` | `recurring`), `next_expected`, a signed
`amount` (`None` only for a variable bill) and `bill_id`, sorted by
`(next_expected, payee)`, local-only (no LLM, no network client, no fx read) —
and `services/cashflow_calendar.py` builds its `subscription` series from its
`recurring` entries instead of `services/subscriptions.py::detect_subscriptions`.
That was the whole bug: a guessed `Google*Youtubeprmium −$25.98` rendering
directly under Mark's real `Google *Youtubeprmium −$25.98` BILL, plus a bare
`Electronic Funds Transfer −$359.08`, because the detector's guess was fed to a
surface that reads as fact. The payload gained `confirmed_subscription_count`
(`CashflowCalendarReport.confirmed_subscription_count: int`), a fact about the
USER rather than about the month, and `frontend/src/pages/Calendar.tsx` renders
one muted line (`data-testid="calendar-subscriptions-empty"`) under the legend
whenever the month shows no subscription dots, worded from that count, so
"nothing confirmed yet" and "nothing this month" are never the same sentence.
`advance_months` moved into the new module as the ONE month-advance clamp rule,
and `cashflow_calendar.py`'s private `_advance` went with it.

**Boundary decisions.** Confirmed means the user asserted it: an enabled
`bill_reminders` row, or a group `POST /transactions/recurring/accept` tagged —
nothing else. A merchant an ENABLED bill's match rule covers is answered by the
BILL alone (payee-only, through the shared `bill_match.py::payee_matches`: the
M165 line, whose `normalize_payee_key` collapses case, whitespace and trailing
branch numbers but not mangled letters, so `Google*Youtubeprmium` stays its own
key on purpose), so one subscription answers once, in the Bills colour; a group
whose rows are all credits is not a subscription. The M27 detector keeps its
candidates for the prompt surfaces — `POST /bills`' autofill resolution and the
re-mounted `GET /subscriptions`, both now documented as a CANDIDATE list — and
`detect_subscriptions` refuses statement-generic descriptors up front,
regardless of amount or cadence fit: `STATEMENT_GENERIC_PREFIXES` =
`ELECTRONICFUNDSTRANSFER`, `EFT`, `INTERACE-TRANSFER`, `INTERACTRANSFER`,
`E-TRANSFER`, `TAXPAYMENT`, `PAYROLL`, tested as a prefix over the normalized
key. `services/recurring.py` and the dashboard's "Bills you probably have" card
are OUT OF SCOPE: they still show candidates, because they are prompts — a
candidate that is neither a bill nor accepted appears there and nowhere else.

**Tests.** `backend/tests/test_m170_confirmed_subscriptions.py` (new, 9 test functions /
13 cases: the four statement-generic descriptors refused however well they
fit, the refusal as a prefix over the normalized key, the stoplist as
detector-only while a user-asserted EFT tag still counts, a mangled
`Google*Youtubeprmium` staying its own merchant until the user says
otherwise, the confirmed set as bills + accepted groups, bill-wins dedup
across every spelling of its payee, the credit exclusion, a stale
`recurring_next_date` rolled forward to today, and disabled bills and other
tenants excluded) plus three in
`backend/tests/test_cashflow_calendar.py` —
`test_m170_subscriptions_series_shows_only_confirmed_sources`,
`test_m170_the_shared_confirmed_query_answers_both_surfaces` and
`test_m170_confirmed_count_distinguishes_nothing_yet_from_nothing_this_month`.
The demo acceptance in `test_cashflow_calendar_demo.py` is renamed
`test_demo_month_shows_bills_dividends_and_no_unconfirmed_subscriptions`: that
tenant's confirmed set is exactly its 5 seeded bills, so its subscription
series is honestly empty while the detector still finds rows it must not draw.
On the client, `frontend/tests/calendarPage.test.tsx` gains `describe
"M170 confirmed-subscriptions empty state"` — count 0 renders the "No confirmed
subscriptions yet … /transactions" line, count > 0 with no dots renders "No
subscription payments in February 2026 …", and a month that does show a
confirmed subscription renders no line at all.

## M168 — the engines were generic; the five tables feeding them were literals

> Board card `t_7a4d61e3` ("M168 CANONICAL (fintrack)"). Spec: docs/07
> §Provider registry + §Fund composition providers, docs/02
> §provider_fund_registry, docs/05 §Provider chain / §Coverage expectations,
> docs/16 §The static tables.

**The gap.** Mark's question was "is what we added for getting details on
sector/geography/underlying securities done generically?" The engines already
were — recursion, per-tenancy isolation, the cycle guard, the denominator
rule, one `canonical_sector` vocabulary, the increasing-coverage contract. The
FIVE tables that feed them were Python literals: `VANGUARD_US_FUNDS` (50),
`VANGUARD_CA_FUNDS` (42), `ISHARES_US_HOLDINGS_CSV` (1),
`STATIC_SECTOR_BY_SYMBOL` (28), `FUND_SECTOR_MIX_BY_SYMBOL` (11). A Vanguard
Canada listing added after the seed — or any Schwab/SPDR/Fidelity/T. Rowe
product — answered NOTHING until somebody edited code and deployed.

**What shipped.**

1. **One table, five families.** `provider_fund_registry` (docs/02,
   migration `0050_provider_fund_registry`) holds all five, `UNIQUE (family,
   ticker)`, seeded with EXACTLY the 132 rows the literals held — same keys,
   same portIds, same venue suffixes, same sector names, same published mixes
   as decimal TEXT — so the exposure pipeline's answers are byte-identical
   across the move. No `user_id`: issuer facts are operator-level data like
   `fx_rates`, written once and read by every tenant, while `etf_holdings`
   stays strictly per-user.
2. **Discovery, where the issuer publishes it.** A first-encounter miss asks
   the ISSUER instead of a literal: Vanguard US probes the ticker-parameterised
   fund-profile endpoint (LIVE 2026-10-11: `VOT`, `VONE`, `VTWO`, `VTIP` all
   answer 200 and were in no literal); Vanguard CA reads the published portId
   list in `GET /en/product.model.json` and one profile+listings GraphQL query
   over it (ticker, portId, MIC — `XTSE`→`.TO`, `NEOE`→`.NE`; the live
   catalogue reproduces all 42 seeded pairs exactly); iShares resolves against
   its own product screener, which turned out to be a **real fund-id lookup** —
   `GET /us/products/etf-investments` answers 526 rows with the product id in
   the URL and the ticker as the link text. Each writes the registry row it
   earned, after a live 200 with a parseable payload.
3. **A negative cache.** A verdict of "no such fund" (404, the CA gateway's
   empty arrays, a ticker the catalogue does not name) writes a `miss` row
   that short-circuits every later probe — the `dead_symbol` discipline,
   applied to coverage. A timeout, a 500 or a 200 that does not parse writes
   NOTHING, so an outage can never retire a real fund.
4. **The maintenance surface.** `fintrackerctl provider-funds`
   list / show / set / verify / reverify / remove (backend/pyproject
   `[project.scripts]`) replaces the code change, and `reverify
   --older-than-days 180` is the quarterly sweep: stale `verified` rows are
   re-probed and flip to `miss` on the spot if the issuer no longer knows
   them; classification rows (no endpoint) are skipped.
5. **Range-limited on purpose.** Vanguard + iShares only — the two families
   the portfolios in production hit first. Each further family is one URL
   template + one parser + one `ProviderFundFamily` value; docs/07 §Provider
   registry documents the pattern and the M147 country map as the next
   candidate.

**Verified.** `tests/test_m168_provider_registry.py` (13) — seed parity with
a frozen copy of the five literals, byte-identical static families, three
funds outside the seed resolving end-to-end with zero code change, the
recorded-once/never-probed-again negative case, an outage recording nothing,
operator-level tenancy against per-user holdings, and the CLI. The card's CI
grep-assert (`git grep VANGUARD_US_FUNDS backend/app` empty) is itself a test.
`tests/test_vanguard_holdings.py` (20) and `tests/test_m166_sector_classification.py`
(59) updated to the registry-backed API and green. Registry mutations are
rolled back per test by `tests/conftest.py::_provider_registry_rollback` —
the probe path owns its own session (M153: no transaction across a provider
wait), which is the one shape the suite's rollback could not undo by itself.
## M160-R2 — the last three panels leave the browser: dividends / sankey / total-return read their own routes, and the orphan premise flips (t_85311c34)

**The gap M160 left open, now closed.** M160 (P2-5) deleted the client report
engines but kept three panels on a browser-computed path through
`lib/reportClient.ts`, a DEPRECATED shim (M160-R1 ruling 4): `DividendsPanel`,
`SankeyPanel` and `TotalReturnMetrics` still downloaded trades, securities,
prices, the ledger and fx to re-derive numbers the server had already
computed, because their routes were M137 P1-1 orphans behind
`SERVER_REPORTS_DEPRECATED` and M135 plan K-8/K-9's re-point had never been
staged. This card is that re-point, and it deletes the shim.

**Parity was the gate, and it passed before anything moved.** Step 1 diffed
each route against the client path on real rows: the frozen server goldens
(`s2c-vectors.json` 4 dividend answers, `m77-vectors.json` 3 sankey + 3
total-return ranges) replayed 15/15 through the client computes, and a LIVE
diff at the layer the panels actually call — the three loaders vs the three
routes, same tenant, same moment — returned 0 diffs over 7 / 25 / 9 non-empty
fields-and-rows, with a negative control proving the diff catches a
mismatch. No PM route-back was needed, so the re-point was authorised on the
spot.

**What changed.**
- `frontend/src/api/reports.ts` regains the three loaders as THIN READS:
  `getDividendReport(year?)` → `GET /reports/dividends` (year optional —
  omitted, the server picks the latest year with income rows — and the same
  path with `format=csv` is the panel's export), `getCashflowSankey(from,to)`
  → `/reports/cashflow-sankey`, `getTotalReturn(from,to)` →
  `/reports/total-return`. The two range loaders keep `assertRange`'s local
  422, thrown before a round trip in the same envelope the server's
  `_check_range` sends (the `getNetWorth` pattern). `TotalReturnReport` moved
  in with them: this module is the pinned wire contract.
- The three panels re-point in one import line each, and `lib/reportClient.ts`
  — the three loaders, the `dividendReport` and `totalReturn` computes, and the
  whole shared input-download machinery (the paginated trades / securities /
  prices / accounts / categories / fx loaders) — is DELETED. Nothing in the
  browser re-derives a report anymore.
- `lib/sankeyEngine.ts` stays, per M160-R1 ruling 3: it is the reference
  implementation the route was written against, and its `SankeyReport`
  interface is still the wire shape `CashflowSankey.tsx` renders. It has no
  production caller now — flagged for a follow-up ruling rather than deleted
  against an explicit one.
- `backend/app/api/_report_flags.py`: `/reports/dividends`,
  `/reports/cashflow-sankey` and `/reports/total-return` LEAVE
  `ORPHAN_REPORT_ROUTES` — a live screen route can never be gateable — so
  `SERVER_REPORTS_DEPRECATED` now gates exactly ONE route,
  `/reports/networth-snapshot`. `tests/test_report_route_deprecation.py` moves
  the three into the never-gated set (ten routes) and pins the flipped state
  in BOTH flag states.

**The leftover goldens get a server home.** `s2c-vectors.json` and
`m77-vectors.json` were two of the three fixtures M160 kept on disk with no
reader; their 10 cases now replay against the ROUTES in
`backend/tests/test_m160_replay_s2c.py` and
`backend/tests/test_m160_replay_m77.py` (the `m160_replay_common` pattern).
The s2c side pins the clock to the capture (`app.services.dividends.dt`
swapped for a frozen stand-in, because the TTM window is defined relative to
today); the m77 side translates the GOLDEN's captured category ids
(`src:cat:1`) into the suite serials the answer carries, and a miss raises
instead of passing. Both modules landed under card t_62afc8a5, which owns all
21 cases — it also wired `s2b-vectors.json` (net-worth 7 +
investment-performance 4) in `backend/tests/test_m160_replay_s2b.py`, the 11
this entry originally deferred to it. M160-R2 first shipped a combined
`test_m160_replay_s2c_m77.py`; the two-card overlap was resolved by folding
all 21 into t_62afc8a5's naming and deleting the combined module rather than
shipping both.

**The panel tests follow the panels.** `m77Behavior`, `m105PctDisplay`,
`m97Tooltips`, `reportMoneyAxes`, `m108Reports` and `m82ReportsTabStrip` mock
the loaders where the panels now import them (`api/reports.ts`) — the same
mocks M160 had orphaned by pointing the panels at the shim. The
hand-computed-ledger pin for total return retired with the compute and lives
where the math runs (`backend/tests/test_total_return.py` asserts the SAME
ledger: TWR 10.9 %, MWR 12.46915167 %). A9's client half retired with it —
docs/33's A9 row now cites the backend suite plus the new replay.
`tests/reportLoaders.test.ts` was rewritten rather than deleted: its
pagination pins died with the client loaders, and it now pins what rule 5
means — one request per report to the pinned path, the payload passed through
verbatim, the `{detail, code}` envelope reaching `ApiError`, and the local 422
for an inverted range with no fetch at all. The env-gated live probe was
re-pointed too (`tests/liveReportRoutes.test.ts`, `LIVE_REPORTS=1`): all three
routes answer under the demo account, and the loader's answer is byte-identical
to the route's.

**Test-count delta.** Frontend: files 133 → 134 (the env-gated live probe,
renamed), collected cases 1250 → 1261 — `reportLoaders` 4 → 11,
`lockedVaultDegradation` 12 → 11 (A9 retires), `m77Behavior` 7 → 6 (the
totalReturn compute pin retires), everything else unchanged. Backend: files 5
→ 6 and cases 93 → 105 (+10 golden replays, plus the A9 P1-5 pair the audit
row now cites); the deprecation suite keeps its 4 cases with a ten-route
never-gated set.

## M174 — the create menu reaches the bulk selection, and a TAG becomes a confirmed-subscription source

> Board card t_45729fbd. Spec: docs/09 §Create from a selection + §Tags that
> mark recurring, docs/36 §M174, docs/02 §users, docs/08 §`PATCH /users/me`.

**Mark's two asks, in his words.** *"I thought we could create a rule or a bill
from a search filter or even bulk actions like set categories or tags"* and
*"Also for recurring stuff can we make that based on tags since there can be
multiple"*.

**What shipped — the create-from-selection menu.** M123's Create menu derived
its four targets from the live filter, and on a phone that filter row is well
above the table; Mark's case is 2–3 SEAFOOD CITY rows ticked in the ledger. The
bulk toolbar now carries `Create from selection` FIRST after the count, opening
M123's menu (a SHARED `components/transactions/CreateTargetMenu.tsx`, so the
ARIA contract — `role="menu"`, roving focus, Escape, disabled-with-reason — is
literally the same code) with two targets: **Rule from these N** and **Bill
from these N**, both deep-linking `?prefill=transactions&q=…` into the existing
editors. Rule + bill only: a selection carries payees, so "Saved view" (a
filter) and "Budget" (a per-category amount) would be controls whose click
cannot mean anything.

**The criteria come from the shared normalize, never from the first row.**
`lib/createFromSearch.ts::selectionCriteria` gates identity on
`lib/payeeKey.ts`/`normalize_payee_key` — the rows are one merchant family only
if every row collapses to ONE key — and then derives the pattern as the longest
common run of the family's payees, which by construction is a substring of every
selected row. That is what makes the M165 pair work: `GOOGLE *YouTubePremium`
and `Google*Youtubepremium` are one merchant but neither string contains the
other, so a pattern taken from row one would silently miss its sibling — a
manufactured duplicate in the editors. The derived pattern (`YouTubePremium`) is
pinned as a real hit on BOTH matchers by `test_the_derived_pattern_matches_both_M165_spellings_on_the_rule_engine`
and `…_on_the_bill_matcher`. A selection spanning two merchants is
`aria-disabled` WITH the reason, never a dead click and never a guessed pattern;
a unanimous category rides along, a mixed one does not.

**What shipped — the tag source.** `users.recurring_intent_tag_ids` (JSON, `[]` =
off, migration `0053_recurring_intent_tags`; whole-list `PATCH /users/me`,
ownership checked at the seam → `422 invalid_recurring_intent_tag_ids`) names
the tags that mean "recurring" — the PM's ruling honoured: NO magic tag names,
because a name heuristic is a second, invisible normalizer whose verdicts the
user could only discover by seeing a wrong calendar. `confirmed_subscriptions`
then UNIONS a third source: a `detect_recurring` group **every one of whose rows
carries at least one nominated tag**, deduped by the shared
`normalize_payee_key` (bill wins, accepted group wins, two tags on one row stay
one group), with the M96 math gate, the money-OUT rule and
`recurring_suppressions` all still applying. `SOURCE_TAG = "tag"` is a third
`source` on the shared dataclass, and the calendar renders it into the SAME
`subscription` kind — so the wire's `kind` vocabulary is unchanged and
`confirmed_subscription_count` counts tag-marked groups exactly as accepted
ones. Nothing is persisted by tagging: the source is a read-time JOIN, so
un-nominating a tag is immediately reversible.

**Mobile parity was MEASURED, and it corrected the premise.** The card's premise
was "on MOBILE the Menu affordance is either hidden or buried". Probed at
390×844 against the build that was LIVE BEFORE this change: the M123 `Create`
trigger sat at top 158 / left 20 / right 122 (44px tall, `aria-haspopup="menu"`)
with its menu rendering at left 29 → right 361, and the bulk bar measured
`scrollWidth == clientWidth == 350` (left 20, right 370) with every one of its
controls inside the viewport — so the M123 menu was reachable and so were the
bulk `Set category` / `Set tag` actions; what was genuinely missing is a
SELECTION-derived create target, which is what this wave adds. After the deploy
the same facts hold with the new control first in the bar
(`frontend/scripts/a11y/m174_parity_probe.py`, 21/21, including a real bulk
set-category completed at 390 and the tag source seen on the calendar). The one
deliberate asymmetry is the MATCHED path: "every row the filter matches" carries
no rows to derive a payee from, and the page's own M123 `Create` menu already
encodes the filter that defines that set.

**Verification.** `backend/tests/test_m174_recurring_intent_tags.py` (20) and
`frontend/tests/m174CreateFromSelection.test.tsx` (16) — the contracts are
listed in docs/09 §Tags that mark recurring → Verification and docs/36 §M174.

**Doc reconciliation.** docs/02 (`users`), docs/08 (`GET`/`PATCH /users/me`,
the error-code list), docs/09 (§Create from a selection, §Bulk selection
toolbar, §Tags that mark recurring, §Recurring prompt), docs/36 (header,
context, decision 14, the payload table, verification) and this entry. docs/12
was checked and carries NO confirmed-subscription contract (grep: no
`M170`/`confirmed`/`subscription` content) — the query is documented where it
lives (docs/36) and its wire shape where that lives (docs/08).

## M175 — the held-session class: four piecemeal fixes become one exhaustive pass, with a guard that keeps it exhaustive

> Board card `t_2386546c` ("M175 CANDIDATE (fintrack): close the held-session
> leak CLASS — M148/152/153/161 fixed four leaks piecemeal; one exhaustive audit
> of session-held-across-provider-wait paths (begin..await..commit sweeps + a
> lint-style regression guard)"). Spec: docs/01 § "Sessions and the event loop",
> docs/07 § "Provider-wait ledger (M175)".

**What was wrong.** The rule is one line — *a `Session` is never held across a
provider wait* — and its violations kept arriving one report at a time: M148
(the forecast burst that parked 15 sessions), M152 (a healthy operator sweep),
M153 (the background enrichment chain), M155/M161 (the price paths). Every one
of those waves ended by naming the seams it had deliberately left alone, which
is an admission that the class had never been enumerated. Nothing about the
rule was hard to apply; the hard part was that "where does the app read and then
wait?" had never been answered for the whole tree.

**What changed.** Every remaining path was found by walking the backend rather
than by waiting for a report, and each one was decided (release / no session /
deliberate exception):

* `services/deep_backfill.py::backfill_security` — the deep-price sweep
  (`scripts/deep_price_backfill.py`, **dry run by default**) read
  `_prefetch`/`trade_evidence` and then fetched per row: one transaction across
  every provider wait of a multi-minute operator run (the M152 shape again).
* `services/benchmarks.py::backfill_symbol` — the benchmark worker's own
  read-then-fetch, open across the first pick and across any pick whose fetch
  answered nothing.
* `services/fx.py::latest_rate` / `get_rates_between` — the Valet fetch-on-miss,
  reached ONCE PER FOREIGN-LISTING POSITION by `valuation.compute_positions`
  (`/portfolio/positions`, `GET /securities/{id}/overview`,
  `api/accounts._build_public`) and by `GET /fx`.
* `services/fx_refresh.py::run_once` — the scheduled pair loop.
* `services/split_repair.py::repair_split_window` — the re-cache fetch.
* `services/categorization.py::_process_one_batch` — the app's LONGEST wait
  (60 s LLM egress, retried once) with the batch's reads on the session.
* `services/notify_delivery.py::_dispatch` — one SMTP/webhook wait per delivery
  in the batch, with the batch's own write transaction open.
* Five request routes: `GET /securities/lookup`, `GET /securities/quote-check`,
  `POST /securities/{id}/holdings/import` (the provider chain runs INLINE),
  `GET /trades/dividend-suggestions` (once per open position) and
  `POST /sharing/invites`.

The seam itself moved to `services/session_release.py::release_before_provider_wait`
(M153's helper, re-exported as `market_data.release_before_provider_wait`) —
not a market-data rule any more, and now reachable from the fx client, the LLM
worker and the dispatcher without importing the market-data module.

Three callers were decided the OTHER way, each because a release would commit
someone's uncommitted work, and each is pinned: `services/symbol_repair.repair_symbol`
(M155's exception), `api/trades._resolve_security` (the investment-CSV staging
path flushes its `import_batches` row before it plans the file's rows, so the
lookup's wait sits on a transaction holding flushed writes — a release would
commit half an import the user never committed) and `tax/acb.py::FxRateBook`
(`rebuild_user_pools` NEVER commits, docs/02, which is why
`fx.get_rates_between` releases on its `commit=True` side only).

**The guard.** `tests/test_provider_wait_ledger.py` (6 tests) is the executable
form of docs/07's ledger: it AST-walks `backend/app`, collects every call that
is a network primitive (`httpx`/`requests`/`yfinance`/`smtplib`/`urllib`/
`socket`) or a call to one of the app's provider seams, keys each by its
enclosing function, and FAILS on an unclassified key (a new member cannot
appear quietly), on a ledger entry that no longer matches any code path (stale),
and on a function classified as releasing that does not contain the release
call. It is deliberately two-way: the table cannot drift into history.
**Amended by t_4eb1fcbc:** that promise had NOT held for the LLM Protocol —
`chat` was in none of the guard's sets and its dynamic-dispatch branch matched
the receiver's variable name, so `POST /users/me/llm-test` and
`POST /imports/suggest-profile` held their request session across the 60 s
`chat()` with the guard green (both probed: 1 connection `idle in
transaction` on the auth read). The collector now matches dynamic dispatch on
the METHOD alone — `LLM_PROTOCOL_METHODS = {chat}` beside
`DYNAMIC_PROVIDER_METHODS`, and `suggest_column_mapping` as a provider seam —
four new ledger rows, and `tests/test_llm_session_release.py` pins both routes
on a one-slot pool (RED with `QueuePool limit of size 1 overflow 0 reached,
connection timed out`). Full detail: docs/07 §Provider-wait ledger.

**Verification.** `tests/test_m175_provider_wait_sessions.py` (15 tests) asks
the M153 questions — no session the caller opened is in a transaction, and no
client backend on the database is `idle in transaction` — from INSIDE every
seam released here, plus the two exception pins (the staging import's flushed
row is still invisible to another connection; the `commit=False` rate range
keeps its transaction). **RED before the fix: 15 failed / 5 passed; GREEN after:
20 passed.** The 15 previously-closed seams stay green
(`test_m148_pool_deadlock.py`, `test_m149_offloop_routes.py`,
`test_m149_yf_timeout.py`, `test_m153_enrichment_session.py`,
`test_m155_price_session.py`), and a 21-file run over every touched subsystem
(deep backfill, benchmarks, fx, notifications, categorization suggestions,
repair report, valuation, security overview, trade staging, sharing) is
241 passed / 0 failed.

**Doc reconciliation.** docs/01 § "Sessions and the event loop" gains the
M175 bullet (the class closed by enumeration), docs/07 gains
§ "Provider-wait ledger (M175)" (the full per-caller table, the exceptions, the
cost and the guard), and this entry.

**Gates.** ruff `format --check` + `check` clean (492 files); the FULL backend
suite on the pushed tree is **2559 passed / 6 skipped / 0 failed** (50:46, exit
0, no deselections) — started twice earlier and stopped for contention while a
sibling session ran its own suite on this 4-core LXC; battery A over the 21
touched subsystems (pool deadlock, off-loop routes, yf timeout, enrichment and
price sessions, deep backfill, benchmarks, fx, notification delivery, repair
report, valuation, security overview, trade staging, sharing) is 241 passed / 0
failed; battery B over the neighbouring areas that commit rows or read
everything (privacy, tax/ACB, export, suite isolation, demo seed, locked-vault,
notifications, ynab migration) is 169 passed / 1 skipped / 0 failed. CI is
KILL-SWITCHED (`CI_ENABLED` unset), so every `ci.yml` job reports
`completed skipped` and the battery was run by hand. Live: the served
`build-commit` stamp reads the pushed SHA, the six changed read paths answer 200
through the public URL, and the database settles with 0 `idle in transaction`
sessions (each request's own tail measured at 0.14–0.39 s against 0.23–0.45 s
request times).

## M176 — the bulk Recategorize control can create the category it needs, in place

> Board card `t_4f8556f2` (soft-dev). Spec: docs/09 §Bulk selection toolbar
> (§Toolbar, §Recategorize — create in place). Follow-up to M174's bulk
> `Create` menu, not a replacement for it.

**Mark's bug, from his screenshot.** Payee `orange`, 126 rows ticked,
`Recategorize` open — and its control was a native `<select>` listing only the
categories that already existed. The one surface made for correcting a run of
misclassified rows could not name a category that did not exist yet, even
though the single-row category cell beside every transaction rendered the
`CategoryPicker`, which already offered `Create "…"`. `POST /categories`
existed the whole time; the bulk path simply never reached it.

**What shipped — the shared picker in the bulk bar.** `BulkActionsToolbar`'s
Recategorize mode now renders the SHARED
`components/transactions/CategoryPicker.tsx` instead of a `<select>`:
type-to-filter, an explicit `Create "…"` row that calls the existing
`POST /categories`, and the existing-name path unchanged. The category is
created and NOTHING else — no transaction moves until the separate, explicit
**Apply**, which drives the same PAGE path (`PATCH /transactions/{id}` per
selected id) or MATCHED path (`POST /transactions/bulk/recategorize` over
`{filter, exclude_ids}`) that M96/M125 specified, so the count/conflict
contract is untouched. Selection, search, filter and scope survive
create/cancel/failure.

**The picker change is small and reusable.** Three additive edits, all
observable through the existing call sites (transaction rows, SplitEditor,
TransactionEditor) and covered by the new file:

* Option rows and the Create row activate on `click`, not `onMouseDown` — a
  button answers a keyboard Enter/Space and a tap with `click` only, so the
  old handler had made both options and creation mouse-exclusive.
* A refused create is SHOWN (`role="alert"`) and retryable, replacing the
  `catch {}` that deferred to a query error a POST never produces.
* One `normalizeName` (trim + collapse whitespace, case-insensitive) backs BOTH
  the duplicate check and the POSTed value, so `"  groceries "` cannot mint a
  second `Groceries`; the server stores names verbatim (no per-user unique
  constraint), so the guard belongs at the control.

**The bar's own two additions.** The created category is merged over the
`categories` prop locally (deduped by id) so the control shows the new name
immediately rather than reading blank until the `["categories"]` refetch lands
(which is fired in the same click); and the picker's menu opens UPWARD below
`md` (`bottom-full`) where the bar is pinned to the viewport bottom and
downward at `md` (`md:top-full`), a responsive class string rather than a JS
measurement, so it can never be clipped off-screen.

**Verification.** `frontend/tests/bulkCategoryCreate.test.tsx` (10) — the
contracts are listed in docs/09 §Bulk selection toolbar → Verification. The two
existing bulk toolbar tests (`bulkTransactions.test.tsx` 17, `m125SelectAllMatched.test.tsx` 16)
were updated only at the point where they chose a category, from a
`fireEvent.change` on the `<select>` to focusing and clicking the picker's row;
every other assertion is unchanged, and both files stay green.

**Doc reconciliation.** docs/09 (§Bulk selection toolbar §Toolbar + the new
§Recategorize — create in place + §Verification) and this entry. No API, schema
or backend change: the work reuses `POST /categories` and the M96/M125 bulk
paths exactly as documented in docs/08.

## M180 — a bill's schedule keeps its own day: month-end clamping stopped eating the 31st (t_ace18cbd)

> Product-review intake item 3 (docs/25 §M180 is the spec). Board card
> `t_ace18cbd`, whose file ownership was arbitrated with the calendar slice
> `t_4ccd8d76`: the model, the write path, the recurrence helper, the migration
> and docs/25/docs/02/docs/08 are this card's; the two shared READERS
> (`services/cashflow_calendar.py`, `services/confirmed_subscriptions.py`) and
> docs/36 belong to that slice, which wires them to the helper described below.

**A recurrence lived in `next_due_date` alone — a real calendar date — and that
made three defects structural.** (1) `mark-paid` advanced from the previously
CLAMPED date, so a bill due on the 31st went Jan 31 → Feb 28 → **Mar 28** →
Apr 28 …; the 31st survived exactly one step. (2) The old `_advance_due_date`
formatted `{y}-{m:02d}-{d:02d}` with no month-length check, so a QUARTERLY bill
due on the 31st stored `2026-04-31` and a YEARLY leap-day bill stored
`2029-02-29` — neither is a date, the calendar silently dropped the row, and the
NEXT `mark-paid` raised `ValueError` out of `date.fromisoformat`: a bare 500.
(3) Feb 29 → Feb 28 forever, even though 2032 has a Feb 29.

**The fix is one column and one module.** `bill_reminders.anchor_day`
(migration `0054_bill_due_anchor`, NULL-able, CHECK 1..31) holds the day the
schedule REPEATS on, so only the stored DATE is clamped:
Jan 31 → Feb 28 → **Mar 31** → Apr 30 → May 31, and a Feb-29 schedule returns to
Feb 29 in the next leap year. `NULL` = un-anchored ("the stored date's own day"),
which is the pre-M180 behaviour a row written outside the API keeps, so nothing
had to be fabricated — `POST /bills` anchors the day it was given, `mark-paid`
pins a NULL anchor on its first advance, and `PATCH` re-anchors only when the
date actually CHANGES (the edit form echoes the clamped date it was shown, and
re-anchoring on that echo would silently demote a 31st to the 28th).
`services/bill_schedule.py` is the ONE recurrence rule: `next_due` (the write),
`occurrences` / `bill_occurrences` (the month grid), `first_on_or_after` /
`bill_next_on_or_after` (the confirmed-set read) — and the old
`confirmed_subscriptions.advance_months`, the same clamp without the anchor, is
GONE rather than duplicated. Nothing changes on the wire: `anchor_day` is
internal and `next_due_date` stays a real date, so no response shape, field or
frontend type moved. An unreadable stored date is now **422 `invalid_due_date`**
on `mark-paid` (the code docs/25 has carried since M47, used for the write it
describes) with nothing written, while the two read surfaces keep their
documented skip-instead-of-500 policy.

**The migration repairs what the old write path left behind.** Two idempotent
statements: every ISO-shaped date gets its own day as the anchor FIRST (for an
impossible date the day component is the only surviving record of the user's
choice), then a date whose day exceeds its own month's length is rewritten to
that month's last day — `2026-04-31` → `('2026-04-30', 31)`, `2029-02-29` →
`('2029-02-28', 29)`, a valid `2026-02-15` untouched, a non-ISO string
untouched with a NULL anchor. The tests execute the revision's OWN
`BACKFILL_STATEMENTS`, twice, so idempotence is pinned rather than assumed.

**Verification.** `backend/tests/test_bill_schedule.py` — 23 tests on real
Postgres: the hand-computed chains above (13-step monthly walk, quarterly
Apr 30/Jul 31/Oct 31/Jan 31, yearly 2029 → 2032 leap return), a **6048-step
sweep** (4 anchor days × 36 months × 3 cadences × 14 steps) in which every
produced value is a real date, the API's mark-paid/PATCH/one_time behaviour
including the two legacy-row shapes (`NULL` anchor → pins and recovers the 31st;
unreadable date → 422 with the row untouched), the row-level readers the other
two surfaces call, the migration's backfill, the table CHECK, and an AST-style
pin that the helper imports no network client. Gates: `ruff format --check` +
`ruff check` clean; the five bill/calendar modules green (64 pre-existing
cases + 23 new); `alembic upgrade head` builds the schema from scratch in the
per-session suite database.

## M170 follow-up — the calendar stated a USD amount as CAD

> Board card `t_4ccd8d76` (soft-dev). Spec: docs/36 decision 15 (+ §Currency in
> the M170 sections), docs/08 §`GET /reports/cashflow-calendar`, docs/09
> §Calendar. Follow-up to M170, whose subject was *which* subscriptions appear;
> this one is *what the number means*.

**The defect.** `CashflowCalendarEvent` named its money field `amount_cad`, and
the subscription series filled it with a confirmed group's `amount_native`
verbatim (docs/36 decision 6 called this "pass-through" and said no currency was
recorded). For a USD account that is simply false: a `-11.99` charge on a USD
account arrived as `amount_cad: "-11.99"`, and `Calendar.tsx` rendered every
event with `formatMoney(value, "CAD")` — a CAD figure the app had never
computed, printed with a `$`. The same lie travelled into the M55 hand-off,
which prefilled a CAD bill draft with the native number.

**What shipped — `amount` + `currency`, an all-or-nothing pair.** The wire field
is `amount` and it is always accompanied by the `currency` it is stated in;
both are `null` when there is no honest figure. The rule lives in the ONE
confirmed query (`services/confirmed_subscriptions.py`), so both readers
inherit it and cannot drift:

* `ConfirmedSubscription` gains `currency: str | None` (all-or-nothing with
  `amount`), `_stated_currency` normalizes a row's code to `[A-Z]{3}` (or
  `None`), and `_group_currency` answers the ONE code every row of a group
  agrees on — `None` for a mixed group (a USD account and a CAD account under
  one payee) and for an unknown one (a blank/non-code `currency`), because the
  group's figure is the mean of ALL its rows: one row the query cannot
  denominate makes the mean undemonstrable.
* Bills are CAD by schema (`bill_reminders` has no currency column) and a
  variable bill states neither; dividends are CAD by convention (M29 projects
  the gross CAD amount); a recurring group — accepted OR tag-marked (M174) —
  is stated in its own currency, through the same `_group_currency` call.
* `services/cashflow_calendar.py` emits the pair per kind and passes the
  subscription's `(amount, currency)` through untouched — it converts nothing.
* `Calendar.tsx` renders `formatAmount(amount, currency)` → `formatMoney`, so a
  USD figure paints `-US$11.99` and a CAD one `-$11.99`; a `null`/`null` pair
  paints the app's `—` dash, never `$0.00`. The `/bills?autofill=…` hand-off
  writes its `amount` param **only for a CAD figure** — a CAD bill draft must
  not be prefilled with a USD number — while keeping `autofill` and `due`.

**No conversion, and a test that would fail if one appeared.**
`test_the_amount_is_the_native_figure_never_a_converted_one` patches
`services/fx.py::cached_cad_amount` **and** `latest_rate` to raise and asserts
the endpoint still answers 200 with `("-11.99", "USD")`: the calendar consults
no part of the rate book, cache-only or otherwise (the module's `_no_network`
autouse fixture additionally fails any fetch). The figure is the rows' own
`amount_native` to the cent.

**What it deliberately does not do.** It does not convert, does not hide the
event (a commitment with a date is still a fact — only the figure is withheld),
and does not invent a per-user "reporting currency" setting. Those are separate
features, each with its own spec.

**Verification.** `backend/tests/test_cashflow_calendar.py` — 5 new cases
(USD group states USD; mixed group omits both; one unknown-currency row omits
both; the no-fx pin; the per-source query rule over a CAD bill, a variable
bill, a CAD group, a USD group and a mixed group) plus `currency` assertions
added to the existing bill/dividend/CAD-subscription controls.
`test_cashflow_calendar_demo.py` and
`test_m174_recurring_intent_tags.py` are reconciled to the renamed field with
`currency == "CAD"` asserted; the four files run green together.
`frontend/tests/calendarPage.test.tsx` — `describe "M170 follow-up currency
honesty"` (5 cases): USD renders `-US$11.99` and not `-$11.99`, the CAD
bill/dividend controls stay CAD, the unstated pair renders `—`, and neither the
USD nor the unstated row carries an `amount` param in its hand-off link.

**Doc reconciliation.** docs/36 (header callout, decision 6, decision 8's
click-through row, the new decision 15, the API example + field contract, the
frontend wrapper/`formatAmount` paragraphs, the touch-points table and
Verification items 3/6/7/8/16/17–21/24/28), docs/08 (the endpoint row), docs/09
(§Calendar) and this entry. No migration, no new endpoint, no new error code.

**Gates (AGENTS §4, run by hand — `CI_ENABLED=false` kill-switches every job,
so a green-looking skipped run proves nothing).** `ruff format --check` +
`ruff check` clean; full backend suite **2569 passed, 7 skipped, 0 failed**
(21:34) on the rebased tree; `tsc --noEmit` + `eslint . --max-warnings 0`
clean; `NODE_ENV=test npx vitest run` **1297 passed, 5 skipped** (135 files);
`npm run build` green with the entry chunk at 528 296 B (≤ 700 000 B).
Merge, push, deploy and the running-stack browser smoke belong to the epic's
integration step, not this slice.

## Consolidation — the calendar recurrence acceptance matrix: month-end, leap-day, DST and currency in one table (t_c38f94a1)

**Problem.** M180 (a bill keeps its own schedule day) and M170 (the calendar
states each amount in its own currency) landed as two separate fixes with two
separate test modules, so the EDGE CASES the two share — month-end clamping,
leap days, the ET day boundary and currency attribution — were covered only
where each fixer happened to look. Nothing pinned the DST class at all
(`today_et()`'s zone-awareness had no test), and nothing asserted that the
three surfaces of the ONE recurrence rule keep answering the same dates for
the same schedule.

**What landed.** `backend/tests/test_calendar_recurrence_matrix.py` — **68
cases**, table-driven, one row per boundary case, every row exercised against
all three surfaces (`next_due` / the write path, `occurrences` and the row
reader / the month grid, `first_on_or_after` and the row reader / the confirmed
set): 9 schedule rows × 3 rule files, the write-path↔grid walk over each chain
month, the confirmed-set↔grid agreement, the `one_time` control, 6 DST clock
instants + the 23-hour/25-hour day and ET-month-boundary grids, 10
currency-boundary rows plus the every-kind pair sweep and the no-fx pin, and
two consolidation pins (function identity + "no second month clamp"). Tests
only: no app code, no migration, no wire change.

**Follow-up shipped.** Accepted and tag-marked groups now derive the schedule
anchor from their newest effective charge row and use
`bill_schedule.first_on_or_after` for whole-month cadences. The accepted
`recurring_next_date` remains a valid clamped date and the latest stored value
remains the projection base; derived charge-day anchoring prevents February's
clamp from drifting future months. No migration or backfill is required.
The regression `test_month_end_accepted_group_uses_the_shared_schedule_rule`
pins April 30 for both the confirmed-set response and calendar grid, matching
an identical bill. Day-based cadence behavior is unchanged. See docs/36
§Out of scope (closed finding) and §Verification.

**Verification.** `pytest tests/test_calendar_recurrence_matrix.py -q` → **68
passed** on local real Postgres, ruff clean. The matrix was mutation-proved on
a detached worktree of `origin/main` (`c265cdd`): baseline 68 passed; the
pre-M180 clamp reintroduced into `bill_schedule.advance_months` fails the
month-end/leap-day rows; a fixed UTC-5 `today_et()` fails the 2026-07-15 row
and a fixed UTC-4 one fails the 2026-12-15 row. Docs: docs/36 (touch points,
the new Verification subsection, the Out-of-scope finding), docs/25 §M180
verification item 11, and this entry.

## M185 — Ledger redesign: the approved mockup becomes the implementation source of truth (t_9ae6c55f)

> Board card `t_9ae6c55f` ("UX specification: map approved Ledger mockup to
> real Fintrack routes and states"), child of the approved epic `t_d4216d9f`.
> Spec: `docs/47-ledger-redesign.md`. Design artifact
> `fintrack-ledger-design-revision-5.zip` (19 432 439 B, md5
> `12b08f221d75f2f4d4d4210ff3857f01`, 230 files) on the epic card; UX final
> gate `t_b02664ad` = READY FOR PRODUCT REVIEW. Baseline `4887221`.

**Problem.** Mark approved a visual reimagining ("Ledger") that was measured,
gated and signed off as a **standalone static mockup** — 16 HTML screens, 90/90
locked write controls, 86/86 clicked refusals explained, 10/10 mockup-only
interactions labelled non-persistent. Between an approved artifact and a
buildable wave sits the gap this card closes: the mockup was photographed
against the DEMO tenant (where every write is refused server-side), so its
padlocks encode a *tenant fact*, not a *feature fact*; several of its stated
capabilities (KPI basis strings, per-row recurrence dispersion, a
statement-balance difference, a `Customize` / `Test connection` control) have
no backing field in the real payloads; and its own inventory missed 42 live
controls. Building from it directly would have shipped faux static locks in a
real product, copy that invents numbers, and a shell whose destination set
drifted from the app's.

**What landed (documentation only — 0 product files changed).** `docs/47` is
the wave's implementation source of truth: §2 maps every approved screen to its
real route, page module and the endpoints that module can actually reach at the
baseline (derived from `frontend/src/App.tsx` plus each page's recursive import
graph); §3 pins one state taxonomy (route-loading / query-loading / empty /
error+retry / stale / unpriceable / dead-symbol / E2EE / read-only demo) with
the render and testid each, keeping the three-part `EmptyState` contract
**additive** rather than replaced by the artifact's compact row; §4 rules the
no-faux-lock contract (`DemoLock` renders only for `isReadOnlySession(user)` on
a mutating path not in the `deps.py` exemption set, stays enabled, explains
itself from the real `403 demo_account_restricted`, one message per page —
a live tenant renders zero padlocks, and downloads are reads that are never
locked); §5–§9 pin the token contract (existing semantic names, re-skinned
values, both themes re-measured), the keyboard/focus additions, the 360/390/768/
1024/1440 verification matrix, the masking mapping onto docs/46, and the
financial-number truth rules; §10 adjudicates **24 API-gap rows** against the
baseline payload (ships-from-existing-fields / derived-by-a-documented-pure-
function / dropped); §12 is the file-ownership plan (docs = ux,
`frontend/src/**` = soft-dev, new automation only in `frontend/tests/ledger/**`
= tester, acceptance and security read-only, backend untouchable in this wave);
§13 gives 17 exact acceptance examples derived from the gate's measured
behaviour; §14 lists the gates the implementation lanes must run.

**Two PM binding decisions (W1/W2) are folded in, not paraphrased away.**
*W1 — demo exports stay live:* a `GET` export is never portrayed as a write and
is never locked or disabled, mirroring `docs/11` §Demo tenant's own server
pinning (`GET /export/zip|ofx|schedule` → 200 on the demo; only
`PUT /export/schedule` → `403 demo_account_restricted`). *W2 — demo
`Test connection` is blocked before dispatch:* no `/users/me/llm-test`
exemption is added and the endpoint keeps its codes; the redesigned control
renders disabled with the exact local copy "Test connection is unavailable in
the demo. The demo account blocks POST requests to keep the sample data
read-only." — the single named exception to the spec's "a locked control stays
enabled" rule, with non-demo behaviour and the privacy-mode egress check
(`403 privacy_mode_blocks_external_api`) unchanged.

Two user-visible contract changes are declared, not smuggled: the desktop left
rail becomes `TopBar` + `More` panel + `TruthBar`, and the mobile six-tab bar
becomes five tabs with a centre **Add** — routes, mask chip, `⌘⇧H`, `⌘K` and
every keyboard contract stay as they are, and the three test files that pin the
old chrome (`m72Mobile`, `m68Navigation`, `m82FontScale`) are named as the
source owner's to update in the same change.

**Doc reconciliation.** `docs/47-ledger-redesign.md` (new), `docs/09`
(§App shell & auth flow M185 supersession callout, new
§Ledger redesign (M185) index section), `docs/04` (§Connection probe row — W2
demo rendering note), `docs/08` (`POST /users/me/llm-test` row — same note),
`docs/11` (§Demo tenant — W1 cross-reference), this entry. No table, enum,
endpoint, error code or payload field changes: the API rows above gain
rendering notes only, and any implementation that needs a real backend change
has left this spec and needs a new doc plus a backend card.

**Verification.** Spec-only change: `git status --porcelain` shows only
`docs/` files (the new `docs/47-ledger-redesign.md` plus edits to `docs/04`,
`docs/08`, `docs/09`, `docs/10`, `docs/11`); every endpoint, payload field,
testid, route and exemption list quoted in docs/47 was read from the baseline
commit's source (`frontend/src/App.tsx`, `frontend/src/api/*.ts`,
`backend/app/api/deps.py`, `backend/app/api/export.py` via docs/11,
`frontend/src/components/**`), and the mockup's own numbers are cited as
claims from `t_b02664ad`, never as product facts. Lint and test gates for the
wave are owned by the implementation lanes (docs/47 §14).


**Implementation landed (card `t_778537dd`, soft-dev source wave).** The
Ledger shell, tokens, primitives and the W1/W2 demo rulings are implemented in
`frontend/src/**` (rail retired; `TopBar`/`MorePanel`/`TruthBar`/`MobileTabBar`;
`--color-rule` token; `StateRow`/`ComparisonChip`/`DemoLock`/`PriceStalenessStrip`;
`lib/{demoWrite,spendingFold,pageMeta}`), with owned coverage in
`frontend/tests/ledger-ui-build/**` and the contract-pinning test updates.
Deviations and the full shipped inventory are logged in docs/47 §16.

## M186 — the watchlist picker could only find securities you already had

> Spec: docs/09 §Watchlist ticker search — the market lookup fallback (M186);
> docs/05 §Portfolio valuation (W11 bullet); docs/08 §Watchlist (Picker note).

**The report, verbatim from the screenshot.** The Watchlist card's input held
`cour` and the listbox under it read **"No matching securities."** over the
empty-state promise *"Nothing watched — add a symbol to follow it."* The
combobox filtered ONLY the tenant's stored `securities`
(`GET /securities?limit=200`, filtered in the browser), so a ticker that was
never held or watched — the exact thing a watchlist is FOR — could not be
found, while the header search and the New security form had been reaching
`GET /securities/lookup` all along.

**What shipped — an optional market fallback inside the same combobox.**
`SecurityPicker` gained `onSearchMarket` / `onPickMarket`, both optional: with
neither present it behaves exactly as before, which is why the trade form's
picker is untouched (it is the documented *existing-securities* picker). The
Watchlist wires `onSearchMarket={securities.isSuccess ? lookupSecurity :
undefined}`, so the probe can only run when the stored list is actually KNOWN.
A probe fires only when the trimmed query is ≥ 2 characters AND zero stored
securities matched, 600 ms after the query settles, once per settled value,
and it is silent on a miss — a `429 rate_limited` or a `404
lookup_unavailable` leaves the honest `security-no-match` row in place. The
listbox gains three row states (`security-market-searching`,
`security-market-option-<SYMBOL>` labelled `SYMBOL — Name (Venue, CUR) · from
market`, `security-market-candidate-<SYMBOL>` per ambiguous candidate with its
currency on the row), each answer rendered only while it is still the answer
for the CURRENT query.

**Picking that row is the write.** `WatchlistSection.addFromMarket` reuses an
exact-symbol stored row or runs `POST /securities`, then `POST /watchlist`,
then invalidates `["watchlist"]` + `["securities"]` and toasts the same
"Watchlist updated / `<SYMBOL>` added." copy the local path uses; `409
already_watched` keeps "Already watched", `409 duplicate_security` says "Pick
it from the list and press Add." while invalidating `["securities"]`, and
every other failure keeps "Watchlist update failed". Local rows still only
select — the **Add** button remains the single write for anything already
stored, and an `ambiguous` answer is only ever stored when the user clicks one
of its rows (M143's never-guess rule).

**The open popup had to become axe-clean to be probe-able.** Running axe over
the OPEN watchlist popup — the only scan that can see it — reported
`aria-required-attr` (the expanded combobox named no node),
`aria-required-children` / `aria-required-parent` (the listbox's owned
children were `<li>` wrappers around `role="option"` BUTTONS) and `listitem`
(those `<li>` sat in a `ul` whose `role="listbox"` had replaced the list
role): four violations, all pre-existing, none introduced by the new rows.
Fixed the way docs/09 §Tag UX fixed the same class on `TagPicker` —
`aria-controls` + `aria-autocomplete="list"` on the input, `role="listbox"`
(and its `aria-label="Securities"`) only while the popup can own an option
(the pending / no-match popup is a plain `<ul>` carrying the same id, so
`aria-controls` always resolves to a rendered node), and each option row's
`<li>` marked `role="presentation"` so the listbox owns the button-options
while Tab/Enter activation is kept. Probe result: **0 critical / 0 serious
with the popup open, at 1440 and at 390**.

**Verification.** `frontend/tests/watchlistMarketFallback.test.tsx` (7) — the
contracts are listed in docs/09 §Watchlist ticker search → Verification. The
existing `tests/m103EmptyStateAction.test.tsx` (3) still sees exactly two
options on an empty query and the same focus/listbox choreography, because a
probe is gated on ≥ 2 characters. Running-stack smoke:
`frontend/scripts/a11y/m186_watchlist_market_probe.py` (27 checks green,
`A11Y_BASE=http://localhost:8080`, real Yahoo seam; evidence in
`m186-watchlist-market-probe.json` + `m186-{local,market,added,miss}-1440.png`)
— the stored-match short-circuit, the live market row
`COUR — Coursera, Inc. (NYSE, USD) · from market`, the pick creating the
security and watching it (field cleared, `watch-row-COUR`, the toast), a
FRESH-session confirmation that `COUR`/USD is what actually landed in
`securities` and `watchlist` — and that the local hit was NOT auto-watched —
the silent miss, and axe 0 critical/serious with the popup open.

**Doc reconciliation.** docs/09 (new §Watchlist ticker search — including the
combobox contract the probe forced into the open — + the §Pages
Investments-row clause), docs/05 (W11 watchlist bullet), docs/08 (§Watchlist
Picker note) and this entry. Two tooling-only touches, both outside the
product: `frontend/scripts/a11y/axe_scan.py` now resolves `axe.min.js` from
the NEAREST `node_modules` (`AXE_CORE` overrides; frontend's own first, the
repo root as the hoisted fallback) instead of hard-failing on one path, and
the new probe script above. No API, schema or backend change: the work reuses
`GET /securities/lookup`, `POST /securities` and `POST /watchlist` exactly as
documented.


## M187 — the scheduled price refresh: today's bar stops freezing at the first page load

> Spec: docs/07 §Scheduled price refresh (M187) — including the seam
> contract, the schedule, the per-user privacy rule and the Verification
> section; docs/07 §Manual price overrides (the `ensure_recent_price`
> signature bullet), §Market-hours optimization (cross-reference) and
> §Provider-wait ledger (M175, the new row); docs/01 (five tick workers,
> `price_refresh.py` in the tree, `PRICE_REFRESH_*` env keys).

**The gap.** Prices advanced only when someone loaded the positions page
during market hours (`api/portfolio.py`'s fan-out) — purely request-driven,
no timer — and that fan-out deliberately stops at a close already dated
today (`ensure_recent_price`'s once-per-day rule, which is what stops a page
load from polling yfinance per security per request). Together those meant a
held security's bar froze at whatever the FIRST market-hours page load of the
day saw: nobody opens positions until 15:00 and the "today" close is a 15:00
snapshot for the rest of the evening.

**What shipped — a fifth in-process tick worker, opt-in only at the seam.**
`services/price_refresh.py` (the M59 template: `run_once` / `worker_loop` /
`start_worker` / `stop_worker`, `asyncio.to_thread` for the provider call,
per-security soft failures, loop outlives any tick) sweeps held + watched
securities at wall-clock ticks in America/New_York — `PRICE_REFRESH_TIMES`,
default `09:35,12:00,15:00,16:15`: ≥4 per trading day, one just after the
open (before 09:30 Yahoo has not printed today's bar) and one after the
16:00 close. The loop sleeps until `next_tick` (never a fixed interval:
clock-based, re-aimed every wake, capped at 1 h slices), which skips
candidates the session gate would reject — a weekend day costs zero wakeups,
a Saturday morning lands on Monday's first tick — and `last_fired` makes a
same-tick double-fire impossible. Weekends/out-of-hours ticks that do fire
(e.g. after a restart) are gated again inside `run_once` and make ZERO
provider calls.

**The two guards had to change, deliberately.** A naive scheduler would have
been four no-ops after the first tick: `ensure_recent_price` returned early
on a close dated today even with `force=True`, and `_cache_closes` inserted
only missing rows. Both gained one explicit, default-False
`intraday_update` — the advance may move an existing `source=yfinance` row
for TODAY only; every other date keeps the insert-only rule, and a
`source=manual` row is never touched on any path (human data wins,
permanently — pinned with the fetch asserted to have HAPPENED). No request
path passes it, and a regression test pins that: the fan-out still calls
`ensure_recent_price(force=True)` and nothing else
(`seen == [{"force": True}]`), `security_overview` and `compute_positions`
open no network call for a close dated today, and the default seam returns
the cached row with a `fetch_latest_close` that raises.

**Privacy is per-user here, not tick-wide.** The FX/benchmark workers skip
the WHOLE tick while any tenant has privacy mode on because those caches are
shared with no `user_id`. `prices` carries `user_id` on every row, so this
worker skips only the privacy-mode OWNER's securities (recorded, never
requested — docs/11) and refreshes everyone else's; the gate keys on the
row's OWNER, so a watchlist row naming a privacy tenant's security is still
skipped and a privacy tenant watching a public security blocks nothing.
Dead symbols stay unprobed (`probe_dead` never passed — freshness knobs are
not gate bypasses), one session per tick with `ensure_recent_price`'s
internal M161 release (no second transaction-holding path — the provider-wait
ledger classifies `price_refresh.run_once`), and the held+watched selection
was moved into `price_refresh.securities_to_refresh` so the request fan-out
and the worker cannot drift apart on what "refreshable" means.

**Verification.** `backend/tests/test_m187_price_refresh.py` (17 green,
real Postgres, `fetch_latest_close` stubbed): the parsed default schedule
(≥4 session-anchored ticks, open→EOD); `next_tick`'s walk/weekend/last-fired
behaviour; a yfinance close dated today advanced by a later tick AND a third
one (5.00 → 6.25 → 6.40 — the feature actually works, not four no-ops); a
manual close dated today never modified; weekend/pre-open/post-window ticks
with zero provider calls; the privacy-mode owner's symbol never requested
while the other tenant advances; dead symbols unprobed; one symbol's failure
not aborting the tick; the three request-path regressions. Full backend suite
green, `ruff format --check` + `ruff check` clean, `npm` untouched (no UI
change). Config/ops: `PRICE_REFRESH_ENABLED` / `PRICE_REFRESH_TIMES` in
settings and `.env.example`; conftest forces the worker off like the other
four; `main.lifespan` starts and stops it with its siblings.

## M188 — the plain-English assistant: ask in plain English, get a card you apply with one click

> Spec: docs/57 (the whole contract — closed registry, lifecycle, audit,
> refusal path, egress); docs/02 §assistant_proposals/§assistant_audit,
> docs/08 §Plain-English assistant, docs/09 §Assistant (page), docs/04
> §What qualifies as a send / docs/11 §What leaves the machine (the fourth
> LLM trigger) and docs/07 §Provider-wait ledger (the new row).

**The gap.** Answering "how much did I spend on groceries?" or acting on
"rename my chequing to Everyday" meant a report read plus a hand edit, and
the only LLM surfaces were the categorization queue and the import-profile
suggester — neither could speak about the tenant's own numbers, and nothing
in the product let a sentence become a reversible change.

**What shipped — propose, apply, undo, with the server as the only writer.**
`POST /assistant/ask` sends the question plus a bounded, cache-only context
JSON (accounts, holdings, ≤50 committed transactions, categories, plan
scenarios) through the ONE existing send seam (`app/llm/base.py`, no new
provider, no key in the frontend) and persists at most one `pending`
proposal row. The model proposes against a CLOSED registry
(`account.update`, `transaction.update`, `forecast_scenario.update` —
holdings answer-only in v1); the server re-validates every value against
the LIVE row before anything is stored, so an unparseable reply is a
`422 assistant_proposal_invalid` refusal with zero rows written. Apply and
undo each re-read the target (`409 assistant_proposal_stale` /
`assistant_undo_conflict` when it moved), stage the domain write plus the
`assistant_audit` row (and the same `transaction_events` receipt
`PATCH /transactions/{id}` writes, `via: "assistant"`) and commit once —
a mutation that cannot be logged does not happen. History lists applied
cards with their audit ids; Undo restores the pre-apply state field by
field. Two tenant tables join `_USER_OWNED_MODELS` (audit first) and are
RLS-secured by their creating migration `0059_assistant`.

**Verification.** `backend/tests/test_assistant.py` (22 green, real
Postgres, `build_provider` stubbed): ask persists one pending proposal and
zero audits; answer-only writes nothing; six refusal shapes each answer
422 with zero rows (the "no silent write" pin); apply → history → undo
round-trips the target BYTE-IDENTICAL to the pre-apply snapshot for all
three kinds, one audit row per mutation, dismiss writes none; stale and
conflict refusals write nothing; a foreign tenant gets 404 on every
transition and never sees the other tenant's rows in its context; privacy
mode answers `403 privacy_mode_blocks_external_api` with a transport
sentinel that fails the test if a socket is even reached; split/pending-
batch/E2EE guards refuse at ask time; demo tenant 403 on every POST, 200
on the reads. The two defects the suite caught on the way (undo's drift
check compared `before` instead of `after`; undo wrote no receipt) are
fixed and pinned by those assertions. Ledger/RLS/model/export registries
green; `ruff format --check` + `ruff check` clean; frontend: vitest pins
the card deltas, Apply/Dismiss/Undo wiring and the refusal copy, `tsc`
+ `eslint` + `npm run build` green, `/assistant` smoke-tested through
`localhost:8080`.
## M188 — the retirement simulator: Monte Carlo on the data fintrack already has

> Spec: docs/57-retirement-simulator.md — every input, bound, formula, the
> `TaxEngine` seam, the honesty strings, the mask rule and the Verification
> section; docs/09 (module-table row, routes + counts, §3.4 exception #10);
> docs/45 §4 (the deterministic what-if stays the plan's decision surface);
> docs/46 §D2 (page subscribes, money chart repaints, assumption inputs stay
> raw); docs/33 §C2 bullet + rule-8 inventory rows for the two new modules;
> docs/47 §1.1 (Primary nav list). Card `t_5534e54f`.

**The gap.** fin_track had no way to ask "will this portfolio last?" — the
only forward view was docs/45's deterministic what-if: one path, one set of
returns, no ruin probability. Kilter's headline feature is exactly that
question asked on real holdings; v1 adopts it with NO new data sources and NO
bank sync: two pre-existing reads (`GET /reports/net-worth` for the starting
portfolio, `GET /reports/cashflow` for the spending default) and nothing else.

**What shipped — `/retirement`, seeded Monte Carlo in the browser.**
`lib/retirementEngine.ts` runs 2,000 scenarios (default; 100–10,000) of
monthly drawdowns `balance *= 1 + μ/12 + Z(0, σ/√12)` from age 40 to
`PLAN_END_AGE` 95 — contributions monthly while working (withdrawal 0), then
`withdrawal − taxEngine.grossWithdrawalFor(...)` after — with STICKY ruin from
the first negative month (a path never "recovers" at the end). The order is
pinned: grow → flow → ruin. Outputs are R-7 quantiles: success %, terminal
value, and a median line + p10/p90 band per age for the chart. The RNG is
mulberry32 seeded from the `seed` input (default 42), so the same seed and
assumptions give byte-identical results — that seed is the reproducibility
hook the tests and the seeded runs ride on.

**The tax seam is an interface, never a table.** `lib/taxEngine.ts` exports
`TaxEngine` (net withdrawal in, GROSS out — the withdrawal field is stated as
after-tax) plus `flatRateTaxEngine(rate)` with `id: "stub-flat"` and label
"Flat-rate stub". M188 shipped the stub because no brackets, provinces or CRA
numbers existed anywhere in the tree yet; **t_1ab83b11 (2026-11-11)** then
swapped the page's construction to `caTaxEngine()` from `lib/canadaTaxEngine.ts`
(`id: "ca-engine"`, default province ON, docs/58) — the seam's first real
fill — leaving `flatRateTaxEngine` exported and pinned as the reference
implementation, and removing the stub's rate input from the panel (a control
feeding only the stub would be dead UI). The feature still owns no tax table
of its own: the tables live only in `canadaTaxTables.ts`. The engine
validates the seam at call time and throws `RetirementInputError` with a
user-facing sentence on any contract violation (a null engine, a missing or
non-function `grossWithdrawalFor`).

**Debounced feel, honest defaults.** Assumption edits commit to the
simulation 300 ms after the last keystroke (`SIM_DEBOUNCE_MS`); seeded
defaults commit at delay 0 (`userEdited` flag), so the first paint never
shows a fake 100 %; `seedCaughtUp` withholds the verdict until committed
money matches the seeded money (or the field was touched). Four BOUNDARY
honesty strings render unconditionally (simulation-not-prediction, runs in
your browser on data you already track, the tax estimate notice — reworded by
t_1ab83b11 from "flat-rate tax stub" to the Canadian-tables wording once the
engine was wired — and not financial advice) — docs/57 §3 pins them, and the
jargon gate bans doc references inside them.

**States wired into the census.** Net worth ≤ 0 → documented empty state
("Nothing to simulate yet"); a failed read → `ErrorPanel` with retry; a
validation failure → the error card. This made `/retirement` the TENTH
documented `src/pages/*.tsx` empty-state exception — docs/09 §3.4 and the
m95 exception map grew nine→ten together, so an eleventh still cannot appear
silently. (The same pass corrected docs/09's stale page count: the routes
line said "All 30"; the tree has 31 pages — `/forecast` had never been listed
either. Now "All 31" with both routes.)

**Rule 8 (docs/33) closed with an inventory row.** Both new modules run in
the browser as pure math plus an interface — no crypto, no key material, the
ciphertext never reaches the simulation — and both are enumerated in the
locked rule-8 inventory (`retirementEngine.ts`, `taxEngine.ts`).

**Verification (all local, no Actions).** `uv run ruff format --check` +
`uv run ruff check` clean; `uv run pytest -q` → **2893 passed, 7 skipped, 0
failed** (backend untouched by this change — full suite run anyway);
`npx tsc --noEmit` exit 0; `npx eslint . --max-warnings 0` exit 0;
`npm run build` exit 0 (lazy `Retirement-*.js` chunk present).
`npx vitest run` → **1580 passed, 5 skipped, 2 failed** of 172 files — and
both failures are load-shaped reds on files this change never touches,
reported not repaired: `m167WorldMap` (a `findBy` render-wait miss) passes
in isolation on this tree, and `splitTransactions` (a hard 15 s timeout that
lands on a different test each run) reproduces 2 failed on a PRISTINE
`origin/main` worktree under the same load — the box ran at load 26–37
throughout from sibling agents' own gate runs. The full-suite trajectory with
this change: the idle-box run surfaced four real defects in MY code
(jargonLeaks: a doc reference inside an error string; m140: the page wasn't
subscribed; m95: the exception census), all fixed and re-verified 22/22 in a
targeted re-run; every later red is the timeout family above. New tests:
`retirementEngine.test.ts` (R1 seed determinism both ways, R2 fixed-series
both verdicts, R3 scripted-draw ruin probability — exactly 50/100 survivors
with the draw count pinned, R4 R-7 quantile on a known series, R5–R6 the tax
seam's call contract) and `retirementPage.test.tsx` (R7–R11: only the two
report reads answered, seeded defaults, honesty strings, debounce, mask
repaint; plus **R13**, t_1ab83b11's ca-engine path pin — the rendered
verdict equals a `caTaxEngine()` reference run and differs from the stub's). Census gates extended with the page: m68 nav hrefs, m100 Primary
list, f7 money-axis NINE→TEN (Retirement is the tenth), m95 exceptions
nine→ten. Live-stack smoke on the built bundle — no docker on this box, so
local uvicorn + a static server proxying `/api` (the nginx:8080 role) —
through the demo tenant: verdict **98.9 %** on the tenant's real figures,
count line "1,978 of 2,000 simulated paths", fan band rendered; editing
monthly contribution to 999999 left the verdict unchanged immediately and
flipped it to **100.0 %** after the debounce; mask flips moved readouts
↔ bullets while the % stayed readable; `window.__errs` empty; the page's own
API calls exactly `reports/net-worth` + `reports/cashflow` (the other observed
calls are the app shell's pre-existing boot reads).

## M189 — Kilter adoption: life-event planning (home, kids, sabbatical, early retirement as plan events) — 2026-11-10

The second Kilter headline card: events that reshape the plan, each with
year-by-year impact, on the SAME substrate as M188 — client-side only: no
endpoints, no tables, no network, no persistence (spec first:
`docs/58-life-event-planning.md`).

- **New `frontend/src/lib/lifeEventEngine.ts`** (lock-vacuous — no I/O, no
  clock, no `../api/`): the extensible REGISTRY of the four v1 event types —
  `home_purchase` (down payment + annual housing cost change),
  `new_child` (annual cost + duration), `sabbatical` (extra annual
  spending + duration), `early_retirement` (target age — the only event that
  moves the phase switch, bounded `year ≥ baseYear+2` and
  `switchAge < retirementAge`); per-type field specs (label, bounds,
  canonical default, `money|int`); `validateLifeEvents` (identity, year
  range, registry bounds, one-early-retirement, sabbatical-before-the-
  switch); `buildEventFlow` (year → step `i = year − baseYear`, per-step CAD
  deltas); `effectiveRetirementAge`; `planTimeline` (year · age · phase ·
  starts · impact · net cash flow per year). Event ORDER NEVER MATTERS —
  deltas are additive (tests shuffle the set).
- **`retirementEngine.ts` step 3**: `eventFlow?: number[]` (length
  `PLAN_END_AGE − currentAge`, finite entries or `RetirementInputError`)
  applied AFTER phase flow and BEFORE the sticky ruin check, floored at 0 —
  absent, the step is skipped and every output is byte-identical to M188
  (pinned by R12). A same-year inflow may fund a same-year shortfall; an
  event can never push the portfolio negative.
- **`pages/Retirement.tsx`** — a **Life events** card (registry dropdown;
  `Event year` starts EMPTY with the valid `<min>–<max>` in its label —
  never a date literal, blank falls back to the type's minimum; fields
  prefilled in CANONICAL text so a pristine field shows no `Using X` hint;
  add/edit/remove with local validation and a `role="alert"` error) plus the
  **delta line** `Success with events: X% · without events: Y% · delta ±D pp`
  — two runs on the same seed, baseline + plan; the headline shows the
  WITH-events verdict, the baseline lives only in the delta line — and the
  **Year-by-year cash flow** table (`life-event-timeline`, money through
  `formatCad`, so it masks). An invalid or stale event set renders the
  event-free baseline WITH the error — never a crash, never a silent drop.
  The chart's retirement `ReferenceLine` and the readouts move to the
  EFFECTIVE retirement age while events are valid.
- **Tests** — `lifeEventEngine.test.ts` (L1–L8: registry single-source,
  per-type flows, early-retirement switch, timeline, order-independence
  shuffle), `retirementEngine.test.ts` **R12** (eventFlow validation;
  a ruinous entry flips a deterministic surviving run to 0 while the
  identical event-free run stays 100; a zero `eventFlow` under R3's scripted
  draws reproduces exactly 50/50 with the same draw count — RNG-
  transparent), `lifeEventPage.test.tsx` (E1–E6: add updates delta +
  timeline + headline, edit rewrites, remove reverts to baseline, invalid
  set falls back with the error, canonical prefill, mask repaint).
- **Docs** — new `docs/58-life-event-planning.md`; docs/57 §interface +
  step order + R12; docs/33 C2 bullet + three rule-8 inventory rows;
  docs/09 Retirement module row (results-branch cards + timeline);
  docs/45 scope line (the planning registry is NOT its five deterministic
  forecast events).

Gates (all local, 2026-11-10): ruff format+check clean; **pytest 2893
passed / 7 skipped / 0 failed** (the clean rerun — see the `.env` pitfall in
docs/58 §Verification: a `.env` copied in for the smoke boot turned four
M148/M149 live-server probes red until it was moved aside);
tsc 0; eslint 0; build 0 (`Retirement-*.js` lazy chunk); vitest **1608
passed / 5 skipped / 1 failed** — the one red is `splitTransactions >
splitGrouping` (17.0 s), an untouched file whose red reproduces on pristine
origin/main under the same load and passes **5/5 in isolation (11.4 s)** on
this branch (the same load-shaped family as M188's run). Live smoke (no
docker on this box: uvicorn + a static `/api` proxy on :8090, demo tenant,
real Chrome): the page renders the four honesty chips + the Life events
card; **add** home purchase $100k → headline 98.9 % → 98.2 %, delta line
`-0.7 pp`, timeline row `2027 · 41 · Accumulation · Home purchase ·
-$100,000.00 · -$54,798.52`; **edit** to $250k → delta `-96.7 pp`, row
`-$250,000.00`; **remove** → headline back to 98.9 %, delta line gone, row
`— · $0.00`. Mask toggles live (`••••` ↔ `$250,000.00`); `window.__errs`
empty through a full add/remove cycle; the page's own API census =
exactly `reports/net-worth` + `reports/cashflow` (life events produced
ZERO additional calls — the other observed calls are the app shell's
pre-existing boot reads). Screenshot:
`life_event_smoke_6cad.png` (delta line + timeline, unmasked).

## M189 — the BYO-AI connector: your own ChatGPT/Claude/Codex reads your data and proposes; you confirm (t_bb0033d2)

> Spec: docs/59 (the whole contract — three-scope token ladder, the propose
> contract, the confirmation gate, audit mapping, pinned egress copy);
> docs/40 §1–§2 (the `read_propose` tier + three-branch refusal envelope),
> docs/08 §Plain-English assistant (the new row), docs/57 §3b, docs/23
> (policy vocabulary, counts, quickstart step 15), docs/11/32 (the egress
> copy lives in the `agent-integration` help guide).

**The gap.** M188's assistant speaks for fintrack; Kilter's promise also
covers the user's OWN model — plug ChatGPT, Claude or Codex into the ledger
and let it answer with real numbers. That needs a way in that can read
everything and propose, but can never write: the confirmation gate is the
product, not a UI nicety, and the demo tenant stays query-only.

**What shipped — a third token scope whose only write is an inert card.**
`mcp_tokens.policy` gains `read_propose` (migration `0060_mcp_read_propose`
swaps the CHECK constraint, no table rewrite; still one alembic head). A
non-demo `read_propose` session lists **54 tools** — the 53 reads plus
`proposals_create`, the one write-class tool it may call — every title marked
`· read+propose token`; every other write is refused at the MCP seam with
`403 mcp_read_propose` (wording points the agent at `proposals_create`) and
`read_only` still refuses everything with `mcp_read_only`, demo first with
`demo_account_restricted`. `proposals_create` proxies to
`POST /api/v1/assistant/proposals` (docs/57's router, extended): pure
transport + validation with **no LLM anywhere on the path**, one `pending`
proposal row persisted, the target byte-identical, zero audit rows. The
external side has no apply/dismiss/undo/list tool on ANY scope, so the gate
cannot be walked around from outside: apply and undo stay the user's own
web session (docs/57 §4), each mutating + writing `assistant_audit` in one
transaction. The demo mint guard widens to refuse any explicit policy ≠
`read_only` (`full` **or** `read_propose` → `422 demo_mcp_read_only`). Help
copy (`agent-integration` guide) pins the ladder, the no-apply gate and the
verbatim egress blockquote — "Your data stays between you and your AI" — and
states plainly that fintrack makes no outbound calls for this feature.

**Verification.** Backend: `tests/test_byo_ai_connector.py` (18 green, real
Postgres) — propose → pending card with the target byte-identical and zero
audits, apply → rename + `assistant_audit` receipt, undo → byte-back,
refusals (`validation_error`, `assistant_proposal_invalid`, `invalid_token`,
`demo_account_restricted`) each write nothing, foreign proposal id → 404,
and a transport sentinel fails the test if a socket is even reached on the
propose path; `test_mcp_agent_surface.py` pins 107 tools = 53 read + 54
write, the 54-tool `read_propose` listing (exactly one `proposals_*`, marker,
`readOnlyHint: false`), and the demo seam exemption; `test_mcp_tokens.py`
pins the `read_propose` mint/validate/revoke round trip. Full suite with
`API_DOCS_PUBLIC=false`: **2998 passed, 7 skipped, 0 failed** (the four
M148/M149 loop-stall guards go red only when the machine-local `backend/.env`
opens `/docs` — reproduced on pristine `d194494` with the same `.env`, and
green with the flag false: environment, not code). `ruff format --check` +
`ruff check` clean. Frontend: `tsc` 0, `eslint --max-warnings 0` 0, vitest
**171 files passed / 1587 tests passed** (0 failed), `npm run build` green.
Live smoke (no docker on this host — uvicorn + `smoke_serve` like M188):
`scripts/smoke.sh` battery exit 0, then the connector flow end to end —
mint `read_propose` → validate `policy=read_propose` → demo mint refused 422
→ real MCP client over streamable-http lists 54 with the marker, gets
`mcp_read_propose` on `accounts_create` (receipt 403), lands a pending card
via `proposals_create` (receipt 200, `mcp_token_id` recorded) with the
account untouched → user-session apply renames it + history carries the
audit id → undo restores byte-for-byte → both tokens revoked (validate 401)
→ unauthenticated propose 401, demo propose 403, throwaway tenant deleted.
Browser: demo login → `/help/agent-integration` renders every pinned copy
string (screenshot attached to the card).

## CVE-2026-111497 follow-up — the frontend build stage names an explicit Node patch

> Spec: `docs/48-cve-2026-111497-build-base-pinning.md`. Card `t_1f263971`.
> **Source-only: no deploy.** This entry records a one-line hygiene change; PM
> folds it into the next release candidate — it does not start a release train.

**Problem.** `frontend/Dockerfile` line 2 read `FROM node:22-alpine AS build`
— a FLOATING tag. It resolves to whatever 22.x patch Docker Hub serves on the
day of the build, so two builds a week apart can compile the frontend on
different Node runtimes, and the runtime is not reproducible from the repo.

**Scope, stated so it is not re-derived: BUILD STAGE ONLY.** The stage that
ships is the serve stage (`nginxinc/nginx-unprivileged:alpine`), which carries
**no node binary at all** (PM-verified 2026-10-23: `command -v node` → `NO_NODE`
in `fin_tracker-web`, `-api`, `-mcp`, `-site`; the deployed runtime is Python
3.13.5 + nginx + postgres:16). Production is **not** exposed to
CVE-2026-111497 at runtime. The nginx base image is out of scope as well:
`nginx -V` reports a brotli module count of **0** in the deployed web image.

**What landed.** `FROM node:22.23.4-alpine AS build` — an explicit patch pin,
with the reason in a comment block above the line so it travels with the code.
`node:22.23.4-alpine` exists upstream (Docker Hub tag API, 2026-10-23).
`backend/Dockerfile` (`python:3.12-slim`) and `site/Dockerfile` are untouched.

**Invariant.** `frontend/tests/buildBaseImage.test.ts` (new, 6 tests) pins that
the frontend Dockerfile declares exactly two stages, that the build stage
matches `^node:\d+\.\d+\.\d+-alpine$` (never `node:22` / `node:22-alpine`),
that the serve stage is still `nginxinc/nginx-unprivileged:alpine`, and that
neither the backend nor the site Dockerfile ever gains a `FROM node` line.

**Verification.** `docker build --build-arg GIT_SHA=<sha> -f frontend/Dockerfile
frontend` succeeds (the `GIT_SHA` arg is the file's pre-existing
`REQUIRE_BUILD_COMMIT=1` guard, not part of this change). The build-stage Node
version printed from INSIDE the built build stage: `v22.23.4` (npm `10.9.9`);
the floating `node:22-alpine` tag resolves to the same patch today
(`v22.23.4`), which is exactly the point of the pin. `npx vitest run
tests/buildBaseImage.test.ts` → 6 passed; `npx tsc --noEmit` → exit 0;
`npx eslint . --max-warnings 0` → exit 0; `npm run build` → exit 0.
Runtime smoke of the built image: `/` → HTTP 200, `command -v node` →
`NO_NODE`, hardened security headers present, `nginx -V` brotli module count
0, served `<meta name="build-commit" content="3fe125d">`.

**Pre-existing frontend red, reported not repaired.** The full frontend suite
ends `1 failed | 1505 passed | 5 skipped`; the single failure is
`frontend/tests/queryKeyShapes.test.ts` ("one key = one queryFn") flagging
`["search",trimmedQuery,vaultUnlocked]` declared by BOTH `AppShell.tsx:249`
and `CommandPalette.tsx:251`. It reproduces on a pristine `origin/main`
checkout with this change reverted, and traces to `383d1b0` (2026-10-17),
which gave the command palette its own `["search", …]` observer. The repo's
CI kill switch is `CI_ENABLED=false`, so nothing has gated it. It belongs to
the unified-search reliability lane (card `t_7bf8abc9`), not to a one-line
base-image pin — so it is reported here and left for that lane.

**Doc reconciliation.** `docs/48-cve-2026-111497-build-base-pinning.md` (new
spec), `docs/01-architecture.md` (the `frontend/Dockerfile` layout line + the
compose-services `web` row), this entry.

## M181 — an opted-in tenant can no longer obtain a plaintext row from ingestion (t_89fa8b10)

> Intake: the security lane `t_83370b91` (findings only, reviewed against
> `fb40fd9` = the deployed build) plus the PM rulings recorded on the
> implementation card `t_89fa8b10`. The spec half is docs/33 § "OUTSIDE THE
> ENVELOPE (M79)" — its wire inventory, its dated resolution of the open
> QUESTION, and its exclusion table.

**The gap.** `users.encryption_required` is a write-time cipher-presence guard,
and exactly three routers ran it: transactions, accounts and YNAB's migration
upload. Everything else that could create a user row wrote the mandatory
PLAINTEXT columns (`transactions.raw_description` / `normalized_payee` /
`amount_native`) or the cipher-less `trades` table, so an opted-in tenant could
still accumulate plaintext rows through six server-side paths:

1. `POST /imports/upload` (OFX/QFX/CSV) + `POST /imports/{id}/commit`,
2. `POST /trades/import` + `POST /trades/import/{id}/commit` (M82 profiles),
3. `POST /trades/record-dividend` (it creates a `transactions` row),
4. `POST /sync/agreements/{id}/pull`,
5. manual `POST /trades` / `PATCH /trades/{id}` (no cipher columns exist),
6. and the enrol-after-staging hole: a batch staged while the tenant was
   ordinary could be committed after the flag flipped.

**The fix is placement, not new machinery.** Every one of those handlers now
raises the established whole-request refusal — `422 {detail,
code:"encryption_required"}`, copied from `api/ynab.py::commit_ynab` — BEFORE
its first read or write: the upload refuses after the ownership check and
before `file.read()`; both commits refuse after the `pending_review` check and
before the duplicate purge and the status flip; `record_dividend` refuses after
the two ownership 404s and before the first `db.add`; the brokerage import
refuses after `_require_investment_account` and before the body read; the pull
refuses before `service.pull`, so no provider call is made; manual trade
create/patch refuse after ownership. Ownership is checked FIRST in every case,
so a foreign id stays an indistinguishable 404 (`docs/02` tenancy) and the
guard cannot be used to probe another tenant's rows.

**Copy (rule 7d).** The detail names the capability and the reason —
"unavailable while client-side encryption is enabled: this path runs
server-side and cannot safely write encrypted data, so nothing was saved" — and
never offers "unlock the vault", because unlocking does not make a server-side
import able to emit ciphers. No zero-knowledge claim is made anywhere, and the
honest banner is unchanged.

**Three PM dispositions, all implemented and documented.** (1) Manual trade
writes are in scope: leaving `POST`/`PATCH /trades` open would have been a
silent inconsistency with the import refusal. (2) Zero-write previews
(`/imports/preview`, `/imports/suggest-profile`, `/trades/import/preview`) stay
available — they write nothing, and the tests pin 0 rows for an opted-in
tenant. (3) Sync consent/`revoke` stay available while every pull is refused;
`discard` / `revert` / `DELETE` and `POST /acb/rebuild` stay open too (removal
or derived-cache paths — each named with its reason in docs/33's exclusion
table). `stage_rows` itself is deliberately left unguarded; its only callers
are refused routers.

**Not claimed.** Bank sync is still outside the envelope: provider credentials
and consent tokens remain server-side, `sync_agreements.*` is server-readable,
and a tenant that has NOT opted in pulls into the plaintext columns exactly as
before. Rows created before enrolment are untouched — no backfill or migration
runs.

**Verification.** `backend/tests/test_e2ee_ingestion_boundary.py` (15 cases,
new) pins every path BOTH ways in one parametrized test (`[False, True]`, the
P1-5 pattern): the refusal is `422 encryption_required` with the
capability-naming copy and no "unlock", and the same route still answers
201/200 for an ordinary tenant. Each refusal is paired with a user-scoped row
census over `import_batches` / `transactions` / `trades` / `securities` /
`transaction_events` / `acb_pools` proving ZERO new rows, plus the
enrol-after-staging case (upload while ordinary → enrol → commit refused with
the batch still `pending_review` → `discard` still clears it), the ownership
ordering (foreign account id → 404, not 422), and the zero-write previews.
`backend/tests/test_bank_sync.py`'s row-24
characterization test was REVERSED with the behaviour it pinned
(`test_encryption_required_tenant_cannot_pull_plaintext`), and docs/39's
verification row 24 + § 9 were reconciled in the same change.

**Gates (AGENTS §4).** TDD order held: the new file plus the reversed
`test_bank_sync` pin were written first and observed RED — `8 failed, 7 passed`
on the new file (exactly the eight opted-in branches) and `1 failed, 25 passed`
on the sync file — before any production code. Then `ruff format --check` +
`ruff check` clean (497 files); the focused set (16 files touching the three
changed routers, including `test_imports*`, `test_brokerage_profiles`,
`test_markets_securities_trades`, `test_locked_fixture_tenant`,
`test_privacy_lockdown`) **251 passed** in 2:41; the FULL backend suite
**2619 passed, 6 skipped, 0 failed** in 18:13 on the local Postgres, zero
deselections. Frontend gates, the docker rebuild and the running-stack smoke
were NOT run and are not claimed: this slice touches no frontend file, and
deployment/QA belongs to the gate card `t_6334a129` and the epic's integration
step. `scripts/locked_vault_gate.py` was not re-run: no A-row citation, audit
row or cited node was touched (the one renamed pin is not a gate needle — grep
confirms the old name appears nowhere in tree).

## M181 follow-up — the SPA disables its server-side ingestion controls before submit (t_4c1a1c34)

> Follow-up to M181 above. Intake: security-lane finding S8 (`t_83370b91`) —
> the API half shipped without the UI half, so an opted-in tenant still
> reached every gated control and only learned the refusal from the `422`,
> after the work was done. docs/33 §Server-first computing rule 6 forbids
> exactly that.

**The gap.** Five surfaces were never gated: the statement upload's **Confirm**
and both batch **Commit**s, the brokerage **Upload & parse**, the manual trade
form's **Record trade**, and both dividend-capture **Record** controls. The
sixth named route, `POST /sync/agreements/{id}/pull`, has no SPA surface at
all — `frontend/src` still holds zero `/sync` references — so there was nothing
to disable.

**The fix is a second gate, not a fifth lock-state string.**
`frontend/src/components/e2ee/ServerSideBlocked.tsx` renders the reason IN
PLACE OF each write control before submit, on ENROLMENT ALONE
(`users.encryption_required` via `useServerSideWriteBlocked()`), never on
`enrolled && !unlocked`: the row is written by the SERVER into columns that
hold no cipher, so unlocking cannot enable it — a lock-state gate would name a
remedy that does not work (rule 7d) and would hide the reason from an
unlocked-but-enrolled user. Its copy is `"<capability> is unavailable while
client-side encryption is enabled: this path runs server-side and cannot write
encrypted data."` — the endpoints' own capability phrases minus their trailing
"…so nothing was saved", so the pre-emptive notice and the defense-in-depth
422 cannot tell two stories. No unlock, no `VAULT_LOCK_DESTINATION`, and the
strings live outside `VaultLocked.tsx`, whose four lock-state constants stay
four (rule 7g).

**Deliberately unchanged.** G1–G3 (`SplitEditor`, `BulkActionsToolbar`,
`YnabMigrationCard`) keep `encryption_required && !unlocked`, because the
browser CAN supply those ciphers once the vault is open. The zero-write
previews, the mapping wizard, `Discard` / `Revert` / `Dismiss` and the whole
exclusion table above stay available.

**Not claimed.** `sync:pull`'s string is reserved, not rendered; no docker
rebuild or nginx-proxy smoke ran (no docker on the build host) — the running
stack was stood up as uvicorn :8000 plus the built `dist` behind an `/api`
proxy on :4173 against an isolated `fintrack_smoke_ux_m181` database, and the
changed screens were driven in a real browser through that origin.
Deployment and the QA sweep belong to the gate card `t_6334a129` and the
epic's integration step. `scripts/locked_vault_gate.py` was not re-run: no
A-row citation or cited node was touched.

**Verification.** `frontend/tests/serverSideWriteGates.test.tsx` (18 cases,
new) parametrizes every surface BOTH ways — enrolled → the control is GONE and
`server-side-blocked` carries the capability's copy with no unlock and no
destination; ordinary → the control is live and ZERO notices render (rule 7a) —
plus the enrolment-only pin (the gate is re-asserted after `createVault()`, so
a lock-state regression fails) and forced Enter-key submits proving no write
reaches the API. Gates: `tsc --noEmit` clean, `eslint . --max-warnings 0`
clean, `vitest run` **1315 passed, 5 skipped** (136 files), `npm run build`
green with the entry chunk at 527.96 kB (≤ 700 000 B).

## M189 — the Canadian tax engine: Kilter's depth as pure, table-driven functions (t_52ed8d01)

> Spec: **docs/58** — the whole feature: the stated model (integer cents,
> HALF_UP, BPA-only credits), every table with its per-jurisdiction source
> URL, the exact exported interfaces, the error-code table, the fallback vs
> refuse semantics, and the Verification section. Rule-8 inventory: docs/33
> §Rule-8 inventory rows for all 17 exports (client-only, lock-vacuous).

**The gap.** The app could project income but carried no Canadian tax depth:
no provincial brackets, no registered-account rules (RRSP room, CESG, RRIF
minimums), no CPP/OAS timing — the exact area where Kilter is deep. The
server's `app/services/tax_projection.py` (docs/27) stays untouched; v1
ships the engine as client-side pure functions so the retirement simulator
(docs/57) can consume it as a seam without a network round-trip.

**What shipped — two modules, 76 tests, every figure sourced.**
`frontend/src/lib/canadaTaxTables.ts` holds all data: `TAX_YEARS` (2025 and
2026, one entry per year — brackets, BPA, surtax/premium/reduction quirks,
registered limits, CPP/OAS), `RRIF_FACTORS_BP`, `TFSA_ANNUAL_LIMITS`, and
`resolveTaxYear()`; every table row carries the CRA/ESDC/Revenu Québec/FSRA
URL it was published from. `frontend/src/lib/canadaTaxEngine.ts` holds all
math as 17 runtime exports (the `CanadaTaxInputError` class + 16 functions):
`incomeTax`, `grossWithdrawalFor`, `caTaxEngine` (the docs/57 seam —
`id`/`label`/`grossWithdrawalFor`, structurally satisfying that doc's
`TaxEngine` without importing it), `cppRetirementMonthly`/`oasMonthly`/
`oasRecoveryTax`, and the registered-account calculators (`rrspRoom`,
`rrspOverContribution`, `tfsaRoom`, `fhsaRoom`, `respCesg`, `respClb`,
`rrifMinimum`, `lifMaximumWithdrawal`, `liraSmallBalanceUnlock`,
`spousalRrspAttribution`).

**The model, stated once so tests can pin it.** Money converts to integer
cents on entry and every intermediate is an integer — rates are basis-point
numerator math divided once per line with HALF_UP rounding (exact at these
magnitudes), and inputs above $100,000,000 are refused rather than risk a
2^53 overflow. Credits are BASIC PERSONAL AMOUNTS ONLY (v1 limitation: no
CPP/EI/age/spouse credits) — figures run conservative-high, and the tests
cross-check CRA's worked examples component-by-component, pinning the
missing-credit delta as a bounded gap instead of hiding it. The year switch
is data: an unpublished year falls back to the latest tables with
`usedFallbackYear: true` for estimates, while registered-account ROOM
REFUSES (`tax_year_not_published`) because room cannot be extrapolated from
stale limits; CPP/OAS dollar maxima freeze at the latest published table
(they do not auto-index — stated, not silent).

**Quirks implemented from primary sources.** Ontario's two surtax bands,
the six-band health premium with per-band caps, and the low-income
reduction (`2 × personal − basic`, T4032-ON's worked example pinned
component-by-component); British Columbia's reduction-to-nil phase-out;
Quebec's 16.5% federal abatement applied after the federal credits; Yukon
mirroring the federal brackets *and* the TD1-WS BPA partial claim (verified
across all 13 TD1s — only federal and Yukon phase the BPA). The LIF maximum
is `max(prior-year earnings, C/F)` with `F = annuity-due(91 − age)` at the
6% floor, reproducing FSRA's published table to five decimals
(65 → 7.25513%, 90 → 100%); RRIF uses s.7308(4)'s factors verbatim.

**Not claimed.** No corporate tax, dividends or RDTOH (v2). No server twin
and no endpoint — docs/27's service is untouched, and the docs/33
disposition says so rather than implying parity. No QPIP/QST, no age-75 OAS
top-up, no LIF bond-rate floor above 6%, no Quebec additional/surtax
(verified absent from the full 2025 TP-1 guide text, not just the payroll
tables). **Deviation:** this build host has no docker (`docker.service` not
found), so AGENTS §4's `docker compose up -d --build` + nginx :8080 smoke
could not run; the smoke was stood up as uvicorn + the built `dist` behind
an `/api` proxy on :4173 against an isolated database
(`fintrack_smoke_t52ed8d01`, alembic head) — the M181 precedent.

**Verification.** Backend `uv run ruff format --check` + `uv run ruff check` clean and
`uv run pytest -q` **2948 passed, 7 skipped, 0 failed** (32m21s, 93%
coverage). Frontend `tsc --noEmit` and `eslint . --max-warnings 0` clean,
`vitest run` **1640 passed, 5 skipped, 0 failed across 173 files**
(including the rule-8 gate, green with both new inventory rows), and
`npm run build` exit 0. Smoke: `GET /api/v1/health` 200 through both
uvicorn and the :4173 proxy, index HTML served, headless Chrome renders
the login page (screenshot `/tmp/smoke52.png`). Two `splitTransactions`
15s timeouts appeared only while pytest + vitest + uvicorn + Chrome all
ran concurrently and pass solo (5/5, 12.5s) — load artifact, not a
regression.

## M190 — file importers: YNAB · Monarch · Quicken · ProjectionLab · CSV / net-worth spreadsheets (Kilter adoption)

> Spec: docs/58 (the whole contract — format presets, net-worth preview/commit,
> type-mapping table, guards, never-silent rules); docs/21 §5-§7 (preset
> precedence + parentheses), docs/03 §QIF/OFX + fixtures table, docs/08
> §Imports (suggest-profile `format`, networth preview/commit rows).

**The gap.** Kilter's import story brings existing budgeting data in **by file** — no
bank sync, by design. The pipeline existed (QIF/OFX and CSV through
`preview → upload → commit` with docs/21's confirmation gate; YNAB migration per
docs/38), but `suggest-profile` answered every file with a generic mapping, so a
Monarch export got an LLM-shaped guess instead of the named, deterministic suggestion
its layout deserves — and there was no route at all that turned a ProjectionLab plan
or a balance spreadsheet into accounts, so a net worth built in another tool could not
come over.

**What shipped — presets that suggest (never write), and a preview-first net-worth
import.** `app/importers/format_presets.py` resolves known export layouts before the
LLM inside `suggest_column_mapping`: headerless → preset (deterministic,
`confidence=0.95`, no LLM call) → LLM → heuristic, reported as the new
`format: "monarch" | null` field on `SuggestProfileResponse` (docs/08 had already
promised `confidence`; both land together). A preset only claims a file when every
required signature header resolves, so a plain bank CSV (`date, description, amount`)
stays generic; Quicken's CSV register layout is deliberately **not** claimed as a
preset — undocumented headers would be a silent-mapping risk (docs/21 §7), so Quicken
stays served by QIF/QFX/OFX and the generic wizard, which already detects its
`Inflow`/`Outflow` split heuristically (docs/58 §A). The confirmation gate is
untouched: a preset suggestion is still only a suggestion — `POST /imports/upload`
without a confirmed `profile_json` is `422 missing_importer_profile` with zero rows
(pinned by `test_mapping_confirmation_gate.py`). `_parse_magnitude` grows the
accounting-parentheses form (`(58.12)` → `-58.12`) before the `credits_positive`
convention applies.

`POST /imports/networth/preview` + `/commit` (docs/58 §B; registered **above** the
`/{batch_id}` routes, the docs/38 §2 ordering lesson) parse a ProjectionLab JSON
export (`today.savingsAccounts / investmentAccounts / debts / assets`,
`progress[]`, `plans[]` — shape as consumed by community tooling) or a balance CSV
(header sniff: account + balance columns, optional `date`/`type`/`currency`, optional
form field `account_name` for single-account files) into a full plan; only `commit`,
in one transaction, creates accounts. Reuse by name (case-insensitive, trimmed) never
overwrites a balance/type/currency — `file_balance` sits beside the account's
`current_balance` for review (manual data wins, docs/01 §4); `taxable` stays
server-derived (docs/14 C1). ProjectionLab types map through the pinned table:
`roth-ira`→`tfsa`, `401k`/`traditional-ira`→`rrsp`, `529`→`resp`,
`hsa` and any unknown investment type→`brokerage_non_registered` + note (a taxable
import never hides a gain), `credit-card`→`credit_card`, other debts→`line_of_credit`.
Assets, plans and progress history — which fin_tracker has no table for — land in
`skipped` with reasons, never silently; an unsupported JSON shape is
`422 unsupported_networth_format`, not an empty plan. The SPA card
(`NetWorthImportCard`) mirrors the contract: picking a file only reads, `Import` is
the only write, and an E2EE-enrolled tenant never sees the commit control (docs/33
rule 7d; preview stays open; the `import:networth` copy is byte-identical to the
server's phrase).

**Verification.** New `backend/tests/test_mapping_confirmation_gate.py` +
`test_file_import_formats.py`: **12 passed** against real Postgres — one fixture
round-trip per format (YNAB budget JSON, Quicken QIF, Monarch CSV, ProjectionLab JSON,
decimal-comma CSV, net-worth spreadsheet — all synthetic, no real personal data in the
repo; QFX/OFX stays covered by the pre-existing `test_import_preview`/`test_imports`
fixtures in the same suite) with re-import → `net_new == 0` / `created == 0`; the gate
test pins `format=="monarch"` from the fixture headers, zero writes from suggest and
from upload-without-profile, and live `GET /transactions` empty until commit. Full
backend suite: **3000 passed, 7 skipped, 0 failed**; `ruff format --check` +
`ruff check` clean (552 files). Frontend: `networthImport.test.tsx` (preview writes
nothing; plan surfaces create/reuse/skipped with reasons; commit required; enrolled
tenant sees no commit control) and, after fixing the one real defect the full run
caught (the plan table's `overflow-x-auto` box lacked `tabIndex`/`aria-label` —
m102), full `vitest run` **1596 passed, 5 skipped, 0 failed** (174 files);
`tsc --noEmit`, `eslint . --max-warnings 0`, `npm run build` all exit 0. Running-stack
smoke on an isolated database (`fintrack_smoke_bf909982`; uvicorn :8010 + the built
`dist` behind an `/api` proxy on :4174 — this build host has no docker, so
`docker compose up -d --build` is **not claimed**, the M181 precedent): a scripted API
run passes **15/15** (preset names `monarch` and leaves a plain CSV generic; upload
without profile → 422; preview writes 0 accounts; commit → 10 created with file
balances incl. `Everyday Checking 2411.27`; re-commit → `created=0, reused=10` with
that balance byte-identical — the merge-review proof), and the card was driven in a
real browser through that origin: pick `projectionlab_export.json` → plan (10 rows to
create, 4 skipped with reasons, `Health Savings` carrying the "imported as a taxable
account" note) → Import → "Created 10, kept 0 existing", the ten accounts then
verified through `GET /accounts` (debts negative, `Old 401k`→`rrsp`,
`Roth Account`→`tfsa`, `Mystery Fund` 900.00 from the unmapped `crypto-ira`).
