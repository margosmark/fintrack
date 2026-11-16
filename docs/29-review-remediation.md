# 29 — Review Remediation (M47)

Fixes for the 2026-09-19 full-application review (4 reviewer slices over API
routers, services/tax/core, frontend, infra/migrations + gate runs). This doc is
the binding contract for the remediation wave; each fix below is pinned as
code-behavior + error code + verification. Existing owning docs are updated in
the same change (docs/18, 20, 24, 25, 26, 27, 28). Milestone: **M47** in
docs/10.

## Context

Two earlier security reviews (2026-08-24/25) were fully resolved. The current
review found the *newer* features (M25–M46: budgets, spending alerts, tax
projection, benchmarks, bills, goals, debts, reconciliation, MCP server)
violating standing invariants: pending-import transactions counted in
aggregates, incomplete FK guards on delete paths, float leakage into money
math, missing input validation, and a fail-open MCP token fallback.

## Decisions (locked)

1. **Committed-only is a shared predicate.** `app/services/txn_filters.py` owns
   `committed_tx_filter(user_id)` (the outerjoin predicate copied from
   `services/reports.py::_committed_transactions`) and `today_et()` (the single
   "today" for spending/insight/alert services, `America/Toronto` via
   `zoneinfo`). No other file may inline a second copy of the predicate.
2. **Benchmarks placeholder stays.** docs/28 decision 5 locked
   `benchmark_return_pct = 0` as an explicit placeholder ("no excess-return
   claims"). The fix is Decimal discipline + string serialization only — the
   placeholder is NOT replaced with fabricated or null values.
   *(Superseded by M173, docs/28 decision 5: the placeholder is gone because
   the comparison is now REAL — a cached index series with an FX-inclusive
   return, and `null` where no honest number exists. That is a deliberate
   contract change with its own pins, not a relaxation of this one: M29's rule
   was "never fabricate", and `null` fabricates nothing.)*
3. **SSE stream is cache-only (docs/18 as written).** The generator's
   `fetch_latest_close` call (network + retry loop) violated the locked
   decision "each tick reads the cached latest close". The fix restores the
   documented contract: a DB read of the newest `Price` row per security. No
   network, no market-hours gate needed inside the stream.
4. **Mixed CSV profiles auto-resolve.** docs/03 + docs/21 lock
   `_resolve_mode_conflict` behavior; the stale `test_csv_split.py:147`
   expectation is updated to assert auto-resolution, not the old w9 422.
5. **Float percentages stay float.** `pct_used`/`progress_pct`/`confidence`/
   display ratios keep the existing codebase convention (money values remain
   Decimal-as-string; documented here so reviewers stop re-flagging).

## Fixes

### F1 — Committed-only aggregates (High)

All transaction aggregates for the M25+ features join `ImportBatch` and accept
only committed batches (or `import_batch_id IS NULL`) via the shared predicate:

- `app/api/budgets.py` (spent computation)
- `app/api/spending_alerts.py` (alert evaluation)
- `app/services/insights.py` (current/previous month, burn rate, projections)
- `app/services/spending_comparison.py`
- `app/services/networth_snapshot.py` (cash balances)
- `app/services/subscriptions.py` (detection)
- `app/api/goals.py` (account-linked progress)

**F1b — net-worth snapshot starting balance.** `networth_snapshot` initializes
each account's balance from `Account.starting_balance or 0` before summing
transactions, matching `api/accounts.py` cash math.

**F1c — dividend calendar tenancy + window.** `dividend_calendar.py` Security
select gains `Security.user_id == user_id`; the trade history scan is bounded
to the trailing 13 months (aligned with frequency math).

**F1d — one "today".** spending alerts / insights / subscriptions / dividend
calendar / tax alerts use `today_et()` instead of `datetime.now(UTC)`-derived
dates (reports.py keeps `date.today()`; unifying the whole app is out of scope
here).

**F1e — committed-only TRADES (the twin predicate).** The investment-CSV path
stages `trades` through the same `import_batches` lifecycle as transactions
(`trades.import_batch_id`, migration 0011), but F1 covered only the
transaction side. A batch sitting in `pending_review` — or one already
`discarded` — moved positions, ACB pools, dividends, tax alerts, benchmarks,
the dividend calendar and the security-detail page BEFORE the user committed
it, because every trade-derived live read selected `Trade.user_id` alone.

`app/services/trade_filters.py` is the trades-side twin of
`txn_filters.committed_tx_conditions`: `committed_trade_conditions(user_id)`
returns `[Trade.user_id == user_id, (ImportBatch.status == committed) |
Trade.import_batch_id.is_(None)]`, and the caller owns the outerjoin — the same
contract, shape, and "no second inline copy" rule as decision 1. Applied to
every trade-derived live calculation:

- `app/tax/acb.py::_load_rows` — ACB replay, pools, disposals, ROC, and the
  superficial-loss rule (this also fixes the `rebuild_user_pools` `trades`
  count, which is the replayed row count rather than a raw `COUNT(*)`)
- `app/services/valuation.py::compute_positions` — positions and, through
  them, the registered-account average-cost book-cost replay
- `app/services/dividends.py::dividend_report`
- `app/services/dividend_calendar.py` (also fixes a latent crash: the calendar
  read `trade.price`, which does not exist on `Trade`; the gross CAD amount
  lives in `price_native` per docs/02)
- `app/services/tax_alerts.py::_superficial_loss_risk` — a staged buyback is
  not a repurchase yet
- `app/api/securities.py::security_overview` — `recent_trades`
- `app/api/portfolio.py::_held_security_ids` — the two `Trade.security_id`
  reads that decide which securities get price-refreshed/streamed

`services/benchmarks.py` needs no change: it is entirely derived from
`valuation.compute_positions`.

**Deliberately NOT filtered — the import review surface.** `POST /trades/import`
(preview), `POST /trades/import/{id}/commit`, `POST /trades/import/{id}/discard`
and the batch-row lookups keep selecting staged rows by `import_batch_id`. They
are the only window into a pending batch, exactly as `GET /imports/{id}/rows`
is for transactions (docs/03). Hiding them would leave the user reviewing
nothing.

`GET /trades` is ALSO unfiltered, but for a different reason: it is the general
paginated trade ledger the Trades page reads, not a review surface. Leaving it
unfiltered means a pending import's rows are visible in the list while every
computed view (positions, ACB, reports) correctly ignores them — a deliberate
trade-off so the user can see what they imported before deciding. If a
review-only surface is ever needed, it is the batch-row lookup, not this list.
`GET /trades/dividend-suggestions` IS filtered: it is a live calculation, and
without the filter a staged buy makes a phantom position look open (F1e).

### F2 — Tax-loss alerts actually fire (High)

`app/services/tax_alerts.py` resolves `taxable` from the position's account
(`Account.taxable` for `pos["account_id"]`, loaded once per call) instead of
reading a `pos["taxable"]` key that `compute_positions` never emits.
`GET /taxes/alerts` returns real positions at loss in brokerage non-registered
accounts; accounts are taxable iff `type == brokerage_non_registered`
(docs/02).

### F3 — Delete-path FK guards (High)

Schema has no ON DELETE rules; every delete path must pre-check ALL referencing
tables and raise 409 `*_in_use` (or clean children where the feature demands
it). Guards are user_id-scoped.

- `users.py::_USER_OWNED_MODELS` (docs/20 cascade order) appends: `McpToken`,
  `RuleTag` + `TransactionTag` (junctions have no `user_id` — deleted by
  subquery on the user's rules/transactions before `Rule`/`Transaction`),
  `Budget`, `SpendingAlert` (before `Budget`), `TaxProjection`,
  `NotificationSetting`, `BillReminder`, `Goal`, `Debt`, `ReconciliationItem`
  (before `Account`/`Transaction`/`Category` parents they reference).
  Smoke-tested ordering correction: `Transaction` precedes `ImportBatch`
  (`transactions.import_batch_id` FKs the batch); the delete-guards regression
  seeds a linked batch to pin it.
  **Exact order is enforced by tests:** deleting a user with budgets, bills
  (category FK), debts/goals (account FK), reconciliation items (account +
  transaction FK), spending alerts (budget FK), tags/rules junctions, and MCP
  tokens returns 204 and leaves zero rows in any `user_id`-carrying table for
  that user.
- `accounts.py::delete_account` extends the 409 `account_in_use` check to
  `Trade`, `AcbPool`, `ImportBatch`, `Debt`, `Goal`, `ReconciliationItem`
  (each user_id-scoped).
- `securities.py::delete_security` extends `security_in_use` to `WatchlistItem`
  (cascade-clear watched rows — watch semantics is "not held", so deletion of
  the security removes its watchlist entries), and 409 for `Fundamentals`,
  `EtfHolding` (either FK column), `AcbPool`, `DividendHistory` and
  `DividendHistoryRefresh`.
- `categories.py::delete_category` adds `Budget` and `BillReminder` to the
  `category_in_use` checks and adds `user_id` scopes to all reference selects.
- `transactions.py::_purge_transaction` (the single-row delete path) removes the
  transaction child set — `ReconciliationItem`, `TransactionTag`,
  `LlmSuggestion` — in the same transaction before the delete. The set itself
  now lives in `services/txn_children.py::unlink_transaction_children`, which
  the BULK paths call too (`api/imports.py` commit/discard/revert and
  `services/batch_hygiene.py`): before M112 those unlinked only tags, so a
  revert of a committed batch whose transactions carried LLM suggestions hit an
  FK violation (schema has no ON DELETE) and 500'd instead of reverting.

### F4 — MCP client fails closed (High)

`backend/mcp_server/client.py::request`: when an MCP token resolves to a
user_id and `_get_token_for_user` returns None (backend 403, network error,
unset secret), raise — never fall back to `self.token` (env JWT or auto-login
credentials), which would execute another tenant's context. The unauthenticated
path (no per-user token resolution at all) keeps using `self.token`.

### F5 — SSE stream restored to spec (High)

`app/api/_price_stream.py`:

- Per tick: one cache-only query selects the newest `Price` row with
  `Price.date <= today` per owned security (`Price.user_id == user_id`).
  Future-dated rows are never presented as current.
- Payload: `{"security_id": int, "symbol": str, "price": str, "date": str,
  "source": "manual" | "yfinance", "age_days": int, "stale": bool}` —
  **Decimal serialized as string**, e.g. `"192.45"`. The extra fields preserve
  quote freshness in the cache without treating message receipt as the price
  date.
- `fetch_latest_close` / yfinance / market-hours logic removed from the stream
  path. Privacy-lock close and the `POLL_INTERVAL_SECONDS = 5` loop unchanged.
- Frontend: `PriceUpdate.price: string`; `usePriceStream.ts` uses the string
  directly (no float `.toString()`).

**F5b — client reconnect.** `usePriceStream.ts` stops relying on EventSource
auto-reconnect with the baked-in URL: on `error`, close the EventSource, wait
for `restoreSession()` to settle, then rebuild from `tokens.access` after a
short backoff only when restoration succeeds; failed restoration leaves the
indicator Offline. Status cycles error → connecting.

**F5c — login 401 handling.** `client.ts::withAuthRetry` skips the
refresh/redirect dance for `POST /auth/login`, `/auth/register`, `/auth/demo`,
`/auth/refresh` (401 propagates to the caller). Wrong password shows the
error panel; no redirect, no token clearing. **M74 (docs/38) widened
`AUTH_401_SKIP_PATHS` to every login-adjacent route** — `/auth/login/totp`,
`/auth/login/recovery`, `/auth/totp/{setup,confirm,disable}`,
`/auth/passkeys{,/options}`, `/auth/passkeys/login{,/options}`,
`/auth/oidc/{preview,confirm}`: a 401 there is a DOMAIN failure of an
in-progress sign-in (`invalid_totp_code`, `invalid_recovery_code`,
`invalid_passkey`, `invalid_or_expired_challenge`, `oidc_pending_expired`),
not an expired token, and falling into the refresh path would hard-redirect
mid-sign-in and wipe the error panel. Read-only authed routes
(`/auth/totp/status`, `/auth/passkeys` list, `/users/me`) are deliberately
NOT listed — a 401 there really is an expired session and must still
refresh.

### F6 — Benchmarks Decimal (Medium)

`services/benchmarks.py`: sums and returns stay Decimal; response fields
`portfolio_start`, `portfolio_end`, `portfolio_return_pct` serialize as
strings. *(M29: `benchmark_return_pct` stayed the documented `0` placeholder,
docs/28 decision 5 — no float touches market-value math. **Superseded by
M173:** the field is now a computed FX-inclusive return, or `null` where no
honest number exists, and the excess is computed server-side; the Decimal
discipline this item pins is unchanged and still enforced.)*

### F7 — Input validation gaps (Medium)

New/changed error-envelope codes:

| code | HTTP | where |
|------|------|-------|
| `category_not_found` | 404 | bill_reminders create/update with foreign or missing category_id |
| `invalid_due_date` | 422 | next_due_date not `YYYY-MM-DD` calendar-valid |
| `invalid_province` | 422 | tax projection province not 2 A-Z chars |
| `invalid_year` | 422 | tax projection year outside 2000–2100 |
| `invalid_statement` | 422 | reconciliation upload items failing the typed schema |

- `bill_reminders.py` + `schemas/bill_reminders.py`: category_id ownership
  check on create AND update (mirror `create_budget`); `next_due_date` typed as
  `date` (Pydantic parses → 422 on garbage); `_advance_due_date` can no longer
  raise. (M180 retired that function into
  `services/bill_schedule.py::next_due` — the typed-date guarantee is unchanged
  and the advance can no longer store an impossible date; docs/25 §M180.)
- `goals.py::update_goal`: PATCH `account_id` re-validates ownership exactly
  like `create_goal`.
- `schemas/debts.py::DebtUpdate`: mirrors create constraints (`debt_type`
  pattern of 5 values, `interest_rate` 0–100, `minimum_payment` ≥ 0).
- `schemas/reconciliation.py`: `ReconciliationUpload.items` becomes
  `list[StatementEntry]` with `date: str (YYYY-MM-DD)`, `amount: Decimal`,
  `description: str | None`, max 2000 items.
- `api/tax_projection.py`: `province` Query constrained to 2 uppercase A-Z
  chars (unknown 2-char codes keep the documented Ontario fallback, docs/27);
  `year` constrained 2000–2100.
- `api/reconciliation.py::match_item`: the matched transaction must belong to
  the same account (409 `transaction_account_mismatch` otherwise); item and
  unmatched selects gain `user_id` scopes.

### F8 — Throttle + secret hardening (Medium)

- `@limiter.limit("10/minute")` on `POST /users/me/llm-test`;
  `@limiter.limit("30/minute")` on `GET /securities/lookup`.
- `core/config.py::_reject_default_secret` also rejects
  `change-me-long-random-string` (the `.env.example` placeholder) and enforces
  ≥ 32 chars.
- `auth.py::internal_issue_token` compares with `secrets.compare_digest`.

### F9 — Frontend UX (Medium)

- `TaxProjection.tsx`: form prefills from the last saved values; save sends
  only changed fields (PATCH semantics unchanged — `exclude_unset` on the
  backend means omitted fields keep stored values); failed saves toast via
  `apiErrorMessage`.
- Mutation `onError` toasts (toast.error + apiErrorMessage) added to: Bills
  update/remove/mark-paid, Budgets update/remove, Goals remove, Debts remove,
  TaxProjection save — mirroring the create mutations.
- `Reconciliation.tsx`: "Difference" card shows `unmatched_sum` (statement
  value not yet explained) via `formatMoney`; `parseCsv` keeps NaN rows out
  (`Number.isFinite`) instead of uploading them as 0.
- `useResolved.ts`: resolution cache key includes a ciphertext hash so
  re-encrypted rows resolve fresh; map bounded implicitly by the new key.
- `Currencies.tsx`: `useQuery` is called unconditionally (gating via
  `enabled:`), removing the conditional-hook-order crash risk.

### F10 — Performance / schema metadata (Low)

- `tax/superficial_loss.py::_holdings_on` replaced by a per-pass sorted sweep:
  trades sorted once per security, running share count updated as disposals
  walk forward — O(n log n) per pass instead of O(n²). Identical results
  (pinned by existing ACB/superficial-loss tests).
- Model metadata matched to migrations: EtfHolding `ck_etf_holdings_weight`
  CHECK; TaxProjection `uq_tax_proj_user_year`; NotificationSetting
  `uq_notif_user_type`; `Transaction.transfer_pair_id` FK `ondelete="SET NULL"`.
- `subscriptions.py` detection bounded to a trailing 13-month window.

### F11 — Infra (Low)

- `docker-compose.yml`: healthchecks for web (nginx TCB port), mcp, site;
  `POSTGRES_PASSWORD`/`DATABASE_URL` parameterized via `${POSTGRES_PASSWORD:-fintrack}`.
- `frontend/Dockerfile`: nginx unprivileged (listen 8080 inside container;
  compose maps 8080:8080), site Dockerfile likewise where trivially possible.
- `.github/workflows/ci.yml`: eslint with `--max-warnings 0`, pre-push
  secret-scan step (`git ls-files | grep -iE '\.env$|secret|token|pem'`
  must be empty), `concurrency:` group cancelling superseded runs.

### F12 — Gate hygiene

- `test_csv_split.py::test_invalid_profile_signed_and_split_columns_together_is_422`
  renamed/rewritten: a mixed profile is accepted and auto-resolves to split
  mode (`amount_native` removed, docs/03).
- All 59 ruff errors fixed (E501 long lines in migrations 0017–0021 reformatted
  per `ruff format`, F401 unused imports removed, F841/E712/E402/F811 cleaned);
  `ruff format` applied to the 20 drifted files.

## Error envelope summary (this wave)

All new failures use `app.core.errors.api_error` — codes listed in F3/F7 plus
existing `account_in_use`, `category_in_use`, `security_in_use`,
`transaction_in_use` (transactions.py delete path), `tag_in_use`. No bare 500s
for domain failures.

### F13 — Calculation-accuracy wave (High)

The F1 wave made the aggregates *correct about which rows* they read. This wave
fixes **what they do with the values** — five user-visible money defects where
the arithmetic, not the filter, was wrong. Every fix is pinned by a regression
test added first (red → green) in the owning suite.

**F13a — Signed reporting-currency spending (budgets + insights).** Both summed
`abs(amount_native)`, so (i) a refund INFLATED spend instead of offsetting it,
(ii) any positive row booked into a budgeted category (interest, rebate, payroll
mis-categorised) counted as spending, and (iii) foreign-currency rows were
billed at native face value. Spend is now the **signed net outflow in the
reporting currency, clamped at 0**: the stored `transactions.cad_amount` mirror
is authoritative, a row without one prices at the newest cached `fx_rates` rate
on or before its booking date (`cached_cad_amount` mirrors only the exact date,
so manual weekend rows arrive NULL), and an unresolvable rate is EXCLUDED —
never counted raw. FX semantics come from the shared `FxCache` in
`services/reports.py`; these endpoints never fetch. Period membership also moves
onto `today_et()` so the budget window matches the alert window in
`api/spending_alerts.py` instead of drifting against a UTC clock.

**F13b — Goal progress counts the starting balance.** An account-linked goal
summed transactions only, so an account opened with a $5,000 opening balance
and no activity read as $0 saved. It now seeds from
`Account.starting_balance or 0` — the same math as `api/accounts.py` and the
net-worth snapshot.

**F13c — Debt payoff: 0% non-divisible and biweekly frequency.**
`app/api/debts.py` amortized every debt monthly and, in the 0% branch, reported
`total_paid = payment × months` — a phantom overpayment (13 × $1,000 =
$13,000 for a $12,345.67 balance) that also contradicted the interest-bearing
branch's `balance + interest`. `payment_frequency = biweekly` was stored but
never read, so biweekly plans projected double the time and roughly double the
interest. Now: period rate is `rate/100/periods_per_year` (12 or 26), `months`
is whole calendar months, and `total_paid = balance + interest` in both
branches. The loop guard became `MAX_AMORTIZATION_YEARS = 50` scaled by
frequency — a flat 600-period cap is only ~23 years of biweekly payments and
would have reported a 30-year biweekly mortgage as `months=-1, "infinite"`.
See docs/29-debt-tracking §Payoff calculation.

**F13d — Negative debt inputs rejected.** `original_amount` and
`minimum_payment` had no lower bound on create or update: a `-500` payment was
stored and projected as "paid off in 0 months". Both are now `>= 0`, and
`payment_frequency` is constrained to `monthly | biweekly` on PATCH as well as
create (F7 was only half-applied). Violations are 422.

**F13e — Net-worth snapshot total is reporting-currency.** `total_cash` added
every account's balance at face value, so a USD account inflated the CAD total
1:1. Per-account `balance` stays native (it is the account-page display copy);
the roll-up converts each account at the newest cached rate on or before
`target_date` and excludes an unresolvable one. The response now also carries
`reporting_currency`.

**F13f — Tax projection is year-aware.** The service hardcoded the **2024** CRA
tables while every caller asked for `year=2026`, so a 2026 user was silently
priced with 2024 numbers (the 2026 federal first band is 14%, not 15%). Bracket
tables are now keyed by tax year with 2026 and 2025 shipped from the CRA's
published pages (URLs pinned in the module docstring; adding a year is a
data-only change). **A year with no published table is never silently priced
with another year's numbers** — it falls back to 2026 and says so:
`bracket_year`, `brackets_fallback`, `brackets_fallback_year`. See docs/27
decision 2.

## Touch points

- F13: `app/api/{budgets,goals,debts,tax_projection}.py`,
  `app/services/{insights,networth_snapshot,tax_projection}.py`,
  `app/schemas/debts.py`; tests `test_{budgets,goals,debts,insights,networth_snapshot,tax_projection}.py`.
- Backend: `app/services/txn_filters.py` (new), `app/api/{budgets,spending_alerts,goals,users,accounts,securities,categories,transactions,bill_reminders,reconciliation,tax_projection}.py`,
  `app/services/{insights,spending_comparison,networth_snapshot,subscriptions,dividend_calendar,tax_alerts,benchmarks}.py`,
  `app/api/_price_stream.py`, `app/tax/superficial_loss.py`,
  `app/models/{etf_holdings,tax_projections,notifications,transactions}.py`,
  `app/schemas/{bill_reminders,debts,reconciliation}.py`,
  `app/core/config.py`, `app/api/auth.py`, `app/api/users.py` (limiter),
  `backend/mcp_server/client.py`.
- Frontend: `hooks/usePriceStream.ts`, `api/securities.ts`, `api/client.ts`,
  `pages/{TaxProjection,Reconciliation,Bills,Budgets,Goals,Debts,Currencies}.tsx`,
  `components/e2ee/useResolved.ts`.
- Infra: `docker-compose.yml`, `frontend/Dockerfile`, `.github/workflows/ci.yml`.
- Tests: `tests/test_csv_split.py` (rewrite stale test), new regression tests
  per fix (delete guards, committed-only aggregates, MCP fail-closed, SSE
  cache-only payload, validation 422s).
- F13 docs: docs/08 (budgets/goals/debts/insights/networth-snapshot response contracts), docs/27 (year-aware brackets + fallback), docs/29-debt-tracking (payoff math + frequency).
- Docs updated in the same change: docs/18 (SSE cache-only + string price +
  reconnect), docs/20 (cascade order), docs/24 (M34 starting balance, committed
  wording), docs/25 (validation + error codes), docs/26 (period semantics),
  docs/27 (province/year validation), docs/28 (Decimal serialization).

## Verification

- Release-gate status: lint, focused stream/privacy/security-overview suites, and
  the full frontend suite are green. A complete backend run remains
  environment-blocked: the example.internal yfinance path leaves a transaction idle while a
  fundamentals/price insert waits on its lock. The identical stall reproduces
  against clean `origin/main`; it is not evidence of a regression in this diff.
- `cd frontend && npx tsc --noEmit && npx eslint . --max-warnings 0 && npm run build` — clean.
- `docker compose up -d --build` then smoke through `http://localhost:8080`:
  - Budget with a pending-review import: `GET /budgets` spent excludes the pending rows; after commit the numbers grow.
  - User with budgets+bills+goals+debts+tagged transactions+matched reconciliation: `DELETE /users/me` → 204, zero orphan rows.
  - `GET /taxes/alerts` returns a loss position for a taxable account holding a depreciated security.
  - `/investments` stream: events carry `"price": "192.45"` strings; privacy mode closes the stream.
  - Wrong-password login renders the error panel without a page reload.
- Secret scan before push: `git ls-files | grep -iE '\.env$|secret|token|pem'` empty.

## F14 — Second-wave review findings (post-fix-wave adversarial review)

An independent adversarial review of the fix-wave diff found four real defects
that the wave's own tests could not catch (they pinned only the sites that had
been fixed). All four are fixed here, each with a regression test proven red
against the pre-fix code.

**F14a (High) — the committed-trade filter was missed in three report paths.**
`services/reports.py::cashflow_report`, `net_worth_report` and
`investment_performance_report` still selected `Trade.user_id` alone, so a
staged import moved REPORTED money while `compute_positions` (correctly)
ignored it. A pending buy of 1000 reported a 1010-share position on
`/reports/investment-performance` and a $10,100 holding on `/reports/net-worth`,
while `/portfolio/positions` showed 10.00 — the same user, two different
portfolios, depending on the screen. All three now apply
`committed_trade_conditions`; `tests/test_trade_staging_scope.py` pins each one,
including an explicit cross-check that the report agrees with positions.

**F14b (Medium) — `GET /trades/dividend-suggestions` was unfiltered.** It built
its open-position timeline and its "already recorded" dedup set from ALL trades,
so a staged buy made a non-existent holding look open (and the endpoint went to
yfinance for it), and a staged income row suppressed the suggestion for the real
dividend the user records after committing. Both queries now apply the predicate.

**F14c (Medium) — per-category insights could contradict the headline total.**
`_category_sums` clamped each category at zero, so a refund booked to one
category was discarded when its category netted negative. With a $40 purchase
(mirrored) and a separate category of -$50 spend +$70 refund, the breakdown
summed to $40.00 while the headline said $20.00. Per-category values are now
signed and unclamped; the headline remains `max(Σ, 0)`. A category that nets to
a credit reports a negative value — that IS the reconciliation, and reporting
`0.00` would hide the credit. The pre-existing tests missed this because they
always put a refund in the SAME category as the purchase, which a per-category
clamp happens to handle.

**F14d (Medium) — a retried expired refresh token logged the user out
everywhere.** `_claim_refresh_token` stamps `revoked_at` as its reuse check, and
the cascade branch fired on any already-stamped row. The FIRST presentation of
an expired token is fine, but a SECOND (stale browser tab retrying, or a proxy
replaying) found it stamped, read that as "this token was spent", and revoked
every other session for the user — a self-inflicted DoS from a client that
merely retried, defeating the guard the wave added. The cascade now skips
expiry-failed claims; reuse detection still fires on a genuine replay
(`test_refresh_reuse_revokes_all_user_sessions`).

**Not defects (checked and cleared):** no test was weakened to pass — every
edited stale test gained assertions or a stronger precondition (net test delta
+2,900 lines, additive). `get_current_user_with_query_token` skipping the demo
guard is correct (its only caller is a read). `committed_trade_conditions`
excluding `reverted` batches matches docs/03. The demo exemption list is a
literal path, so a future mount-prefix change fails CLOSED (blocks a repair
path) rather than open.


---

## F15 — Account-existence oracles closed (M83 F-7 → M87 S1)

**Decision record, as required by the M87 card.** Two responders answered
differently for an address that had an account and one that did not:

| Route | Was | Cost |
|---|---|---|
| `POST /auth/register` | `201 {access_token, …}` + cookie vs `409 duplicate_email` | anonymous existence oracle on the open registration path |
| `POST /auth/passkeys/login/options` | `200 {challenge_token, options}` vs `404 no_passkeys` | the same oracle, surviving on the passwordless path |

### The choice on register: `200 always` (not `409 always`)

The card offered two patterns — always `409`, or always `200` "after
sending-if-exists email verification". We take **always `200` with the account
still created on the free branch**, for one reason: *what the recovery flow
supports*. This install runs with **no SMTP at all** as a supported
configuration (docs/37 — `SMTP_*` may be entirely unset), so a
send-if-exists verification email is not a flow we have; `409 always` would
have needed one to be usable at all. The recovery path that does exist offline
is: the caller signs in with the password they just chose.

```
POST /api/v1/auth/register  →  200  {"status": "accepted", "detail": "..."}
```

Contract (all pinned by `test_register_responder_is_consistent_for_free_and_taken_addresses`
and `test_register_is_uniform_and_seeds_categories`):

- **Byte-identical** body and status on both branches — asserted as
  `free.json() == taken.json()`, not a shape comparison.
- **No credential on either branch**: no `access_token`, no `Set-Cookie`.
  Issuing a session on the free branch would have re-created the oracle with
  a `200` instead of a `201`.
- Free branch still creates the user + `DEFAULT_CATEGORIES` (unchanged).
- Taken branch writes nothing and calls `_dummy_bcrypt`, spending the same
  one bcrypt verify `hash_password` costs — timing parity, same as login's
  existing L1 defence.
- Branch-INDEPENDENT failures keep their codes: `422 validation_error`,
  `422 password_too_long`, `429 rate_limited` (5/min).
- `409 duplicate_email` **survives on `PATCH /users/me/profile`** (docs/20) —
  authenticated, and the caller already owns that address.

The register route previously documented the opposite
(`test_register_duplicate_email_is_intentional_409` — "enumeration tradeoff
kept on purpose … clear duplicate-email UX beats masking"). The PM's
own-the-edge decision resolves that tradeoff: with no PIN in front, masking
wins. The test was rewritten, not deleted, to assert the closed oracle.

### Passkey options

Always `200 {challenge_token, options}`; the keyless cases (account with no
credential, and no account at all) receive a server-generated throwaway
challenge — random 32-byte descriptor so `allowCredentials` stays present and
same-shaped, token bound to `THROWAWAY_USER_ID = 0` (`users.id` is a serial
starting at 1, so it resolves nothing). The divergence point moves to
`POST /auth/passkeys/login`, which answers `401 invalid_or_expired_challenge`
or `401 invalid_passkey`. `404 no_passkeys` is retired as a code.
SEC-1 (t_4476a739) later closed the same oracle *inside* the token:
`challenge_token` is signed, not encrypted, so the `passkey_login` subject is
now sealed (`security.py::BLINDED_SUBJECT_METHODS`) and a keyless reader
sees identical claim sets, types and byte lengths for all three cases —
no positive-integer `sub` (docs/42 §S1b).

Known cost: the sign-in button now stops silently (browser `NotAllowedError`
→ `getPasskey` returns `null`) for a keyless email instead of rendering
"no passkey registered for this email". Silence is the point — and the
password path still explains itself normally.

**Full shapes, timings, touch points and evidence: docs/42.**
