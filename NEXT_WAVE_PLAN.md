# NEXT_WAVE_PLAN.md — proposed next wave (M48–M52)

**Status: M48–M52 have shipped (milestone markers below).** These are roadmap entries with acceptance criteria, not spec docs:
each milestone writes or extends its `docs/NN` spec BEFORE code lands
(AGENTS.md §7), then builds under the §4 gates. Owner: @pmbot (PM), written
2026-09-27.

## Verified baseline (2026-09-27, this clone)

- M1–M47 shipped (`docs/10` + `docs/24`). Last CI run 36214932480: **backend,
  web, secret-scan all success**. Backend suite 750 tests / 88 files. Working
  tree clean at plan time.
- E2EE Phase 3 in flight: S1 search (2026-09-25) and S2a
  spending/breakdown/cashflow (2026-09-26) shipped; `docs/33` is the ledger.
- Frontend entry bundle `dist/assets/index-BVcX3a8M.js` = **1,212,611 bytes,
  one chunk**; `App.tsx` imports all 22 page components eagerly (0 `lazy`,
  0 `Suspense`). (Count corrected 2026-09-28: `src/pages/` has held 22 `.tsx`
  page modules since 2026-09-03; the entry measured 1,250,336 B at `38b0fec`
  just before M49 — see `docs/09` bundle facts.)
- **Notification surface is decorative (verified by code):**
  `notification_settings` (M31) carries `large_transaction`, `low_balance`,
  `dividend_received`, `price_alert`, `monthly_summary` — no generator
  produces any of them and no code reads `enabled` (only default-seeding in
  `api/users.py`). Real generators that DO exist (`spending_alerts` M41,
  `/bills` M40, `/taxes/alerts` M26) are absent from the enum. No send path
  (smtp/email/webhook) exists anywhere in `backend/app`. (Corrected 2026-09-28:
  M51 shipped — `budget_threshold`, `bill_due`, `tax_loss` are now seeded
  settings types produced by their existing generators and read by the new
  `GET /notifications/inbox`; the OTHER five M31 types still have no generator
  and nothing reads their `enabled`, and there is still no send path — see
  `docs/35`.) **Second correction 2026-09-28: M59 (`docs/37`) adds the send
  path — SMTP + signed webhook behind `notification_channels` +
  `notification_settings.delivery_enabled`, off by default, privacy-mode
  in-app only unless the user opts in per channel.**
- **Demo had no reset path** (as documented in `docs/20` before M50):
  `POST /auth/demo` → `ensure_demo_account` returns early once seeded. Seed
  dates are relative to seed day (docs/30), so an old demo shows stale
  windows, and any future gap in the demo write guard causes permanent
  damage (manual psql wipe is the only recovery today). (Corrected 2026-09-28:
  M50 shipped — `POST /auth/demo/reset` is the sanctioned recovery path, so a
  guard gap is no longer permanent; `docs/20` + `docs/08` document it.)
- **Doc-numbering debt:** `docs/10` and `docs/24` BOTH define an M29 (review
  remediation vs dividend calendar), and ~20 references cite "M47" although
  no M47 milestone entry exists anywhere. Needs one reconciliation pass.

## Priority & waves

### Wave 1 — M48: finish E2EE Phase 3 (in flight; `docs/33` owns the spec)

Why first: reports/search are mid-migration (some compute client-side, some
server-side); new UI built on a half-migrated path gets reworked twice.
Slices (disjoint file ownership, parity recipe in the fin-tracker skill's
`references/e2ee-slices.md`):

- S2b — net-worth + investment-performance client-side ✅ shipped 2026-09-27 (offline diff 11/11 ranges, 0 diffs)
- S2c — dividends client-side ✅ shipped 2026-09-27 (offline diff 4/4 goldens, 0 diffs)
- S3 — rules / subscriptions / budgets-progress client-side ✅ shipped 2026-09-27 (offline diff 16/16 cases, 0 diffs; `POST /rules/preview` removed → 405, `GET /subscriptions` removed → 404, `GET /budgets` kept as definition read path)
- S4 — capital-gains/ACB client-side ✅ shipped 2026-09-27 (offline diff 14/14 goldens, 0 diffs; `GET /reports/capital-gains` JSON computed in the browser, `GET /acb` kept as the fetched-input read path)
- remove the last server content scan `GET /transactions?q=` ✅ shipped 2026-09-28 (M48-S5: faithful pg_trgm port + client-side search page; `q` → **422 `q_removed`**; offline diff 10/10 live goldens)

**Acceptance (each slice):** offline node diff = 0 diffs vs server JSON on
demo inputs (golden vectors first, not shape asserts); post-deploy network
count shows 0 calls to the replaced endpoints; `docs/08` + feature doc +
`docs/33` ledger updated in the same commit; AGENTS §4 gates green.

Out of scope: Phase 4 (wrapped-DEK sync, hardening audit) — planned
separately after S4.

### Wave 2 — improvements (M49–M51; independent, parallel-safe with disjoint files)

**M49 — route code-splitting (frontend only)**
`React.lazy` + `Suspense` per route in `App.tsx`; shell/auth stay eager.

✅ shipped 2026-09-28: all **22** page modules dynamic (`route(pages.*)` wraps
each dynamic import in `RouteLoadError` + `Suspense` + the shimmer
`RouteFallback`, `App.tsx`), shell/auth eager; entry chunk **1,250,336 →
443,573 B** (target ≤ 700,000), 57 JS chunks; gates green (tsc, eslint,
vitest 260 tests, build); browser smoke 22/22 routes render through the
fallback with no blank state; `docs/09` bundle facts reconciled.

Acceptance: build emits per-route chunks (all 21 page modules dynamic);
**entry chunk ≤ 700,000 bytes** (from 1,212,611); gates green (tsc, eslint
`--max-warnings 0`, `NODE_ENV=test vitest`, build); browser smoke — every
route renders through the documented fallback, no blank/missing state;
`docs/09` bundle facts reconciled.

**M50 — demo tenant reset (backend only)**
Reset path (new `POST /auth/demo/reset`, rate-limited like `/auth/demo` at
5/min) wipes demo-tenant domain rows in FK-safe order and re-runs the M10
seed → fingerprint-identical restore.

✅ shipped 2026-09-28: `POST /auth/demo/reset` (Bearer, 5/min, same bucket as
`POST /auth/demo`) wipes the demo tenant through the shared FK-safe cascade
`users.py::delete_user_rows(db, user, keep=...)` — keeping the `users` row,
`refresh_tokens`, `mcp_tokens`, never touching shared `fx_rates` — re-creates
the default categories and re-runs the M10 seed in ONE transaction, with
`seed_fingerprint()` (serial ids excluded, FK ids resolved to natural keys) as
the proof mechanism; third/last exemption in `DEMO_MUTATION_EXEMPT_PATHS`
(rationale at the guard); 6 new backend tests; branch suite **804 passed**;
`docs/20` + `docs/08` + `docs/30` reconciled. Running-stack smoke of the route
is owned by the Wave 2 integration card (this milestone did not rebuild
containers).

Acceptance: mutate the demo via API → reset → seed fingerprint equals a
fresh-seed fingerprint (M10 idempotence property); `SEED_DEMO_ACCOUNT=false`
still returns 404 `demo_disabled`; the demo write boundary is NOT weakened —
ordinary-router `demo_account_restricted` regression tests stay green, and
the reset becomes the third, explicitly justified, documented exemption
(comment at the `get_current_user` guard, per the fin-tracker skill rule);
covers ≥3 mutated tables (transactions, budgets, bills) in a regression test;
`docs/20` + `docs/08` reconciled in the same commit.

**M51 — notifications that actually fire (backend + small frontend)**
Add generator-backed settings types `budget_threshold`, `bill_due`,
`tax_loss`. New in-app inbox: `GET /notifications/inbox` (union of the three
sources, tenancy-scoped, cursor-paginated), `POST /notifications/inbox/{id}/read`,
`POST /notifications/inbox/read-all`; unread badge in `AppShell`; the
Dashboard critical-alert banner respects the `budget_threshold` toggle.

✅ shipped 2026-09-28: the three real generators' types (`budget_threshold`,
`bill_due`, `tax_loss`) are now `notification_settings` values seeded enabled;
new `notification_inbox_reads` table (migration 0028, chained onto M53's
0027); tenancy-scoped
cursor-paginated `GET /notifications/inbox` + idempotent
`POST /notifications/inbox/{id}/read` + `POST /notifications/inbox/read-all`;
AppShell bell (unread badge + dropdown) and the Dashboard critical-alert
banner read the same query and are gated SERVER-side by
`notifications.py::_enabled_types()` (generation keeps writing
`spending_alerts` audit rows). Branch gates: backend **812 passed**, vitest
**263 tests / 28 files**, build green; `docs/35` pinned BEFORE the code, then
`docs/02`/`08`/`24`/`26` reconciled in the same commit. Browser smoke of the
bell/badge/banner on the running stack is owned by the Wave 2 integration card.

Acceptance: toggle off → that type is excluded from the inbox AND the banner
(generation keeps writing audit rows — the surface is gated, not the data);
toggle on → items visible again; inbox tenancy-strict (foreign id → 404);
demo reads stay open; invalid cursor → 422 envelope with a pinned code; new
`docs/35` spec pins every path/payload/error code BEFORE code lands, and
states plainly that the 5 legacy types still have no generator (no hiding).

Out of scope: email/webhook delivery (no SMTP infra, conflicts with privacy
mode) — future milestone. **Shipped as M59 (`docs/37`)**: the privacy conflict
is resolved there (in-app only by default, explicit per-channel opt-in, five
field payload), so this line is historical.

### Wave 3 — M52: cash-flow calendar (feature; spec-first, lands after M49 so it is lazy)

`GET /reports/cashflow-calendar?month=YYYY-MM` →
`{month, days: [{date, events: [{kind: bill|dividend|subscription, label,
amount_cad|null, source_id, status}]}]}`, zero-filled days, `today_et()`
day boundary (docs/29 rule), reuses `/bills`, dividend-calendar (M29) and
subscription (M27) projection math — no new detection logic, no network
(reports never fetch, docs/07). UI `/calendar`: month grid, kind-colored
dots, day popover, click-through to source pages.

✅ shipped 2026-09-28: `docs/36` pinned before code; endpoint
`GET /reports/cashflow-calendar` in `api/reports.py` + the new
`services/cashflow_calendar.py` — `month` optional (absent = `today_et()`'s
month), `month=2026-13` → **422 `invalid_month`**, zero-filled `days`,
`today` from `today_et()` drives `status`, signed `amount_cad` (dividend +,
bill/subscription −, `null` for a variable bill), `source_id` = bill id /
security id / detector payee, bills projected **forward only** with the day
of month anchored on the original date (2026-01-31 → Feb 28, Apr 30), no fx
read anywhere in the path. `/calendar` = `pages/Calendar.tsx` wired the M49
way (`pages.calendar` + `route(pages.calendar)`), query key
`["cashflow-calendar", month ?? "current"]`, Monday-first grid, portaled day
popover with Escape/backdrop close, today ring from the response (the client
derives neither "today" nor the current month). Gates green: ruff; pytest
**872 passed / 0 failed** on the isolated suite DB (25 new tests, including a
hermetic demo-month test that pins bill/dividend/subscription events on their
own source dates with the fetch seams poisoned); tsc + eslint; vitest **34
files / 332 tests**; build — entry **448,848 B** (≤ 700,000), `Calendar`
chunk **8,465 B**. `docs/08`/`docs/09`/`docs/10` reconciled. Deliberately
absent (recorded in docs/36 "Out of scope"): a sidebar row (`AppShell.tsx`
owned elsewhere) and an MCP tool; the running-stack smoke belongs to the
integration card.

Acceptance: demo month shows bill events on due dates, dividend events on the
M29 next-payment dates, subscription events on next occurrence; empty month →
zero-filled cells, no error; `month=2026-13` → 422 with pinned code; tenancy
strict; privacy mode unchanged; new `docs/36` spec before code.

### Candidate backlog (not scheduled)

- LLM per-send consent dialog — **resolved by M54 (struck, not built)**:
  `docs/33` carries the `[STRUCK 2026-09-28, M54]` ledger entry with the
  rationale (a consent field is asserted by the same principal the route
  trusts; MCP tools are scripted callers of all three send routes), and
  `docs/04` "Consent & disclosure model" now pins the model that IS enforced.
  No contradiction remains.
- Subscription → bill draft autofill (M27 → M40 bridge).
- Scheduled client-side-encrypted backup (docs/11 extension).

## SDLC: stages, owners, exit criteria

| Stage | Owner | Exit criteria |
|---|---|---|
| 1 Requirements / plan | @pmbot | this doc + per-milestone acceptance criteria |
| 2 Spec | @devbot | `docs/NN` written/extended before any code (AGENTS §7) |
| 3 Implement | @devbot | wave code lands, §4 gates green |
| 4 Acceptance / QA | @qabot | every acceptance item above exercised against the running stack |
| 5 Deploy & verify | @devbot | pull → rebuild → smoke → prod-URL verify (fin-tracker skill) |
| 6 Doc reconciliation | wave owner | same commit as the code (AGENTS §9) |

Sequencing rules: Wave 1 before Waves 2–3 (half-migrated compute path).
Wave 2 file ownership is disjoint: `App.tsx` → M49; `auth.py`,
`demo_seed.py`, docs/20 → M50; notifications routers, `AppShell.tsx`,
docs/35 → M51. Every slice runs against its OWN test database (never
shared). Wave 3 starts only after M49 lands.

Risks: M48 parity ports are the highest defect-density class (recipe is
mandatory — golden vectors before code); M50 touches the demo security
boundary (exemption must be justified + regression-tested); M51 must not
pretend the 5 legacy settings types have generators.
