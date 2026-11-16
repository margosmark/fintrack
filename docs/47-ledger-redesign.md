# 47 — Ledger redesign (M185) — the approved mockup mapped to real routes, sources and states

**Status: implementation source of truth.** Every later Ledger card (soft-dev
source, tester automation, security review, acceptance) reads THIS doc. Where
the design artifact and this doc disagree, this doc wins; where this doc and
`docs/01`–`docs/46` disagree, the more specific older spec wins and this doc is
wrong until reconciled (AGENTS §0).

## Context

- **Design artifact (approved):** `fintrack-ledger-design-revision-5.zip`,
  19 432 439 bytes, md5 `12b08f221d75f2f4d4d4210ff3857f01`, 230 files,
  CRC-clean: 16 `mockups/*.html` + `assets/ledger.css` + `evidence/` +
  `REVISION-5.md` + `ANNOTATIONS.md` + `TOKENS.md` + `PROPOSAL.md`. Attached to
  the parent epic `t_d4216d9f`.
- **UX final gate:** `t_b02664ad` — **READY FOR PRODUCT REVIEW**. 90/90 write
  controls carry a real padlock + bound reason; 86/86 clicked locked controls
  explain themselves; 10/10 mockup-only demo interactions are labelled
  non-persistent; B1–B6, D1, D2, D3, M1, R1–R3 re-measured green at 1440 and
  360. The gate approved **the mockup for product review**; implementation is
  approved by `t_d4216d9f` as a docs-first, independently-gated UI wave.
- **Baseline:** `origin/main` `4887221bed5a9bae3f4de6996d452f711a0533b1`
  (`fix: preserve dividend history on quote miss`). Every route, endpoint and
  payload field named below was read from that commit's source, not from the
  mockup.
- **Milestone number:** **M185**. Claimed by this spec; QA-acceptance and
  security-integration cards in the same wave must reference M185 rather than
  minting a second number.

### What the mockup is, and is not

The artifact is a **static, self-consistent design of the DEMO tenant's view of
the product** — the designer browsed `https://fintrac.princessandmark.com`
through "Explore the demo", where every write is refused server-side
(`403 demo_account_restricted`, `backend/app/api/deps.py`). That is why 90 of
its write controls are drawn locked. The mockup therefore encodes **two
different things that must not be conflated in the product**:

1. **Visual/interaction design** (surfaces, tokens, states, hierarchy) — taken
   as approved, with the rulings in §2 and §3.
2. **The read-only demo boundary** (padlocks, refusal bar) — a property of the
   demo tenant, translated into the product's real guard by §4. It is NOT a
   feature-availability signal and must never be drawn for a live tenant.

The mockup's own `README §5` "assumed capabilities" list is treated as a set of
**claims to verify**, and every claim is adjudicated in §10 (API gap decision
table). Nothing in the mockup that the real payload cannot support ships as
copy.

### PM binding decisions carried into this spec

Recorded by the operator mid-spec and folded into §4, §10 and §13:

| | Decision |
| --- | --- |
| **W1** | **Demo export stays live.** A `GET` export is never portrayed as a write and is never locked or disabled; `docs/11` §Demo tenant's GET contract is unchanged. Only `PUT /export/schedule` is a write and may carry `DemoLock`. |
| **W2** | **Demo `Test connection` is blocked before dispatch.** No `/users/me/llm-test` exemption is added; the control renders disabled with the exact local copy *"Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only."* Non-demo behaviour and privacy-mode egress checks are unchanged. This is the single named exception to §4's "a locked control stays enabled" rule.

## Scope

- **In scope:** the frontend presentation layer of the existing routes —
  shell/navigation surfaces, tokens/theme values, page-level composition of the
  already-specified features in `docs/09`–`docs/46`, and the state/lock
  taxonomy. Documentation (`docs/09`, `docs/10`, this doc).
- **Out of scope (ratified from `PROPOSAL.md` "Not now"):** no mega-menu, no
  webfont (the editorial face is dropped — system stack only), no
  canvas/SVG chart-library rewrite, no glass/blur reintroduction, no backend
  endpoint, migration, error code or payload change, no demo-tenant change, no
  per-row inline editing of server-computed totals, no client-side report
  computation (server-first rule, `docs/33`).
- **Hard limits for this wave:** frontend source only; **no backend work may be
  created** — every gap in §9 is either shipped from fields the payload already
  carries, derived client-side by a documented pure function, or dropped.

## 1 · Surface model and navigation (D1)

The artifact's signature surface is a **top tab bar + persistent truth bar**
on desktop and a **bottom tab bar + drawer** on mobile (`README §1`). Mark
approved that surface model, so the product's left rail is retired in favour of
it. Routes are unchanged; only the chrome that reaches them changes.

### 1.1 Desktop (≥ 768 px)

| Element | Contract |
| --- | --- |
| `TopBar` (new `components/shell/TopBar.tsx`) | brand → primary tabs **Dashboard · Transactions · Imports · Investments · Reports** (the five the artifact draws) → `More` button → right cluster: search (`⌘K`/`Ctrl K`), the mask switch, notifications bell, account menu |
| `More` panel (new `components/shell/MorePanel.tsx`) | one `role="dialog"`-less **popover**: `aria-expanded` + `aria-controls="more-panel"` on the button, focus moves in on open (`useDialogFocus`), **Escape closes through the shell's existing single keydown listener** (order stays palette → inbox → search → More panel → account menu → drawer, innermost first — `docs/09` §Escape routing gains consumers, never a second listener), focus returns to the button on close, Tab advances and never traps |
| `More` groups | **Primary**: Forecast · Assistant · Retirement · Calendar · Taxes · Tax Projection · Shared household · Settings · Help — **Tools**: Currencies · Tags · Rules · Budgets · Goals · Bills · Debts — **Accounts**: `All accounts` → `/accounts`, then one row per account (name · type label · own-currency balance) → `/accounts/{id}`. Every destination is a real `<a href>`; zero placeholder hrefs and zero dead fragments (`docs/09` §Sidebar nav rows' full `NAV_ITEMS` set must remain reachable) |
| Truth bar (new `components/shell/TruthBar.tsx`) | directly under the `TopBar`, one persistent line — see §1.3 |
| Session controls | the mask switch keeps `data-testid="mask-chip"` + `aria-pressed` + `⌘⇧H` (`docs/46`, `docs/09` §Keyboard contract); the account menu (avatar button, `aria-label="Account menu: …"`) keeps the theme switch and the second mask row (`mask-toggle-row`); that menu and the notifications bell are **disclosures** on the shell's popover contract — `aria-expanded` + `aria-controls`, focus in on open / back to the trigger on close, no `role="menu"` (§6.9) |

The rail's existing affordances do **not** disappear: `RailTip`'s job (a
labelled collapsed icon) becomes the `More` panel's own tooltips; the
notifications inbox panel, the search overlay and the ⌘K palette keep their
current z-order contracts.

### 1.2 Mobile (< 768 px)

| Element | Contract |
| --- | --- |
| Bottom tab bar (new `components/shell/MobileTabBar.tsx`, replaces `MobileNav`) | five tabs: **Home · Activity · Add · Portfolio · More**. `Add` is the centre control and is a **real** control (see below), never a demo stub |
| `Add` | opens the shared `TransactionEditor` as a modal over the current route; the tenant has no accounts → the editor's own empty state renders (`docs/09` §Pages). **Reported deviation allowed:** if the editor cannot mount globally in this wave, `Add` navigates to `/transactions` with the editor open, and the deviation is reported in the completion card — the control may never be a no-op |
| Drawer | the existing `AppShell` drawer (one `<nav>`, `inert` while parked, focus-in on open, Escape closes, scrim click closes, route change closes — `docs/09` §Navigation) now carries `More`'s three groups plus the footer session controls, i.e. the artifact's `m-nav.html` hierarchy |
| Tabs | `min-w-11 flex-1`, 44 px floor, `min(0.75rem, 14px)` capped label, `pb-[env(safe-area-inset-bottom)]` — the M82 F7 pixel-constant rules survive unchanged (`docs/09` §Navigation, pinned by `tests/m82FontScale.test.tsx`) |

**This is a user-visible contract change** documented here and in `docs/09`:
six tabs (Home · Txns · Import · Invest · Reports · Settings) become five, with
`Add` promoted to the centre. Rationale: adding a transaction is the dominant
mobile job, Import/Invest/Reports/Settings stay one tap away in the drawer, and
the six-label row is already at its 360 px legibility limit (M82 F7). The
existing tests that pin six tabs (`tests/m72Mobile.test.tsx`,
`tests/m68Navigation.test.tsx`, `tests/m82FontScale.test.tsx`) are updated by
soft-dev **in the same change as the source** — the spec change is this section,
not a test edit made in isolation.

### 1.3 Truth bar (new) — what it may print

The truth bar is the direction's trust claim, so it is **bounded by the
payload**: every segment renders only when a real field backs it, and it never
prints a vendor name (`docs/09` §Naming & semantics: the market-data vendor
renders as "Market data").

| Segment | Source | Rendered when |
| --- | --- | --- |
| Active range | the page's own `from`/`to` state (every money page owns a `RangePicker`/`ChartRangeControl`) | always, on money pages |
| FX | `GET /fx/status` → `as_of`, `age_days`, `stale` (`api/fx.ts::FxStatus`) | `pairs` non-empty (no FX dependency ⇒ nothing to say) |
| Prices | the page's own payload — on Investments the newest `price_date` across `GET /portfolio/positions` items, and its `price_source` | when at least one row carries a `price_date` |
| Privacy | the mask store (docs/46) — "Amounts masked" / "Amounts visible" | always |
| Read-only demo | `isReadOnlySession(user)` → "Read-only demo — changes are not saved" (`data-testid="demo-banner"`) | demo tenant only |

Forbidden in this bar (and everywhere): `cache yfinance 1d`, `BoC`/vendor
names not present in the payload, a "last updated" time derived from the wall
clock, and any number the payload does not carry. Below 768 px the bar renders
**one truncated line** (range + privacy + demo) with the full text in a
`HelpHint`-style popover — ruling on `PROPOSAL` open question 2.

## 2 · Source–route map (the build/QA index)

One row per approved artifact screen. "Reads"/"Writes" are the endpoints the
named route module can actually reach at the baseline commit (derived from
`frontend/src/App.tsx` + the recursive import graph of each page module, not
guessed). Endpoint names are the frontend client paths; `api/v1` is the prefix.

| Artifact screen(s) | Route | Page module | Reads (representative) | Writes the screen owns | Responsive |
| --- | --- | --- | --- | --- | --- |
| `dashboard.html`, `dashboard-mixed.html` | `/dashboard` | `pages/Dashboard.tsx` | `GET /reports/net-worth`, `GET /reports/cashflow`, `GET /reports/spending/categories`, `GET /reports/insights`, `GET /budgets`, `GET /bills`, `GET /transactions`, `GET /transactions/recurring/candidates`, `GET /notifications/inbox`, `GET /users/me` | `PATCH /users/me` (layout preset), `POST /transactions/recurring/suppress`, `POST /notifications/inbox/{id}/read`, `DELETE /transactions/{id}/split` (un-merge) | single column; KPI strip becomes a 2-up card grid at <768 |
| `dashboard-empty.html` | `/dashboard` | same | same, zero rows | — | same |
| `dashboard-loading.html` | `/dashboard` | same | route chunk (`RouteFallback`) + in-flight queries | — | same |
| `transactions.html` | `/transactions` | `pages/Transactions.tsx` | `GET /transactions`, `GET /transactions/{id}/split`, `/tags`, `/events`, `/receipt`, `GET /transactions/similar`, `GET /transactions/unreviewed-count`, `GET /transactions/recurring/candidates`, `GET /accounts`, `GET /categories`, `GET /tags`, `GET /suggestions`, `GET /vault/envelopes` | `POST /transactions`, `PATCH|DELETE /transactions/{id}`, `POST /transactions/{id}/review`, `POST /transactions/bulk/*`, split CRUD, tag attach/detach, transfer detect/link/unlink, `POST /transactions/recurring/accept|suppress`, `POST /suggestions/{id}/accept|reject`, `PUT|DELETE /transactions/{id}/receipt`, `POST /categories`, `POST /tags` | table → card rows below `md`; filter bar → bottom sheet (`m-transactions-sheet`) |
| `imports.html` | `/imports` (+ link to `/reconciliation/{accountId}`) | `pages/Imports.tsx` | `GET /imports`, `GET /imports/{id}`, `GET /imports/{id}/rows`, `GET /imports/preview`, `GET /imports/upload`, `GET /imports/suggest-profile`, `GET /imports/ynab/preview`, `GET /institutions`, `GET /accounts`, `GET /accounts/{id}/reconciliation` | `POST /imports/{id}/commit|discard|revert`, `POST /imports/ynab/commit`, `POST /trades/import/{id}/commit|discard`, `POST /institutions`, `PATCH|DELETE /institutions/{id}`, `POST|PATCH|DELETE /accounts` | 3-step rail stacks vertically; batch table scrolls horizontally (`TableScroller`) |
| `investments.html`, `investments-by-security.png` | `/investments` | `pages/Investments.tsx` | `GET /portfolio/positions`, `GET /portfolio/exposure`, `GET /portfolio/breakdown`, `GET /portfolio/value-history`, `GET /accounts/{id}/value-history`, `GET /prices/history`, `GET /reports/total-return`, `GET /portfolio/benchmarks`, `GET /securities`, `GET /securities/lookup`, `GET /securities/quote-check`, `GET /trades`, `GET /trades/dividend-suggestions`, `GET /watchlist`, SSE `GET /api/v1/securities/stream/prices` | `POST /portfolio/refresh`, `POST /trades`, `POST /trades/record-dividend`, `POST /securities`, `POST|DELETE /watchlist` | lens segmented control goes full-width; grouped rows become cards; exposure keeps its tabs |
| `reports.html` | `/reports` | `pages/Reports.tsx` | `GET /reports/spending`, `GET /reports/spending/categories`, `GET /reports/cashflow`, `GET /reports/cashflow-sankey`, `GET /reports/net-worth`, `GET /reports/investment-performance`, `GET /reports/dividends`, `GET /reports/capital-gains`, `GET /reports/category-tag-matrix`, `GET /reports/dividend-calendar`, `GET /reports/total-return`, `GET /projections/defaults`, `GET /partner/state` + `?format=csv` variants | `POST /projections/run` (demo-exempt, `docs/20 §6`) | tabs scroll horizontally; charts full-width |
| `settings.html`, `settings-dark.png` | `/settings` | `pages/Settings.tsx` | `GET /users/me`, `/categories`, `/accounts`, `/institutions`, `/notifications`, `/sharing`, `/sharing/audit`, `/partner/state`, `/vault/envelopes`, `/tags`, `/goals`, `/debts`, `/imports`, `GET /backup/schedule`, `GET /backup/artifacts`, `GET /export/schedule`, `GET /export/full|zip|ofx` | `PATCH /users/me` (+`/profile`, `/change-password`), `DELETE /users/me`, `POST /users/me/llm-test`, category/account/institution CRUD, `PATCH /notifications`, sharing writes, vault-envelope writes, `PUT /backup/schedule`, `POST /backup/artifacts`, `DELETE /backup/artifacts/{id}`, `PUT /export/schedule` | `.masonry`-style balanced 2-column wall → 1 column below 900 px |
| `states.html` (all six panels) | not a route — the shared state vocabulary | `components/ui/*` | — | — | every route |
| `overlays.html` | not a route — palette / popover / dialog / toast / sheet / drawer inventory | `components/ui/*` + shell | — | — | every route |
| `m-dashboard.html`, `m-dashboard-360*.png` | `/dashboard` <768 | `pages/Dashboard.tsx` | as above | as above | 390 and 360 |
| `m-transactions.html`, `m-transactions-sheet.html` (+`-360`) | `/transactions` <768 | `pages/Transactions.tsx` | as above | as above | sheet is `role="dialog" aria-modal="true"`, focus-trapped, Escape closes |
| `m-portfolio.html` | `/investments` <768 | `pages/Investments.tsx` | as above | as above | 390 and 360 |
| `m-nav.html` | the shell's drawer <768 | `components/shell/*` | `GET /accounts` (drawer account rows) | — | 390 and 360 |

Routes with **no artifact screen** (Accounts, Account detail, Security detail,
Calendar, Taxes, Tax projection, Forecast, Assistant, Retirement, Bills, Bill detail, Debts, Budgets,
Goals, Tags, Rules, Currencies, Reconciliation, Shared household, Sharing
accept, Help, Login/Register/OIDC, NotFound) keep their current design. The
Ledger shell and tokens apply to them for free; their inner layouts do not
change in this wave.

## 3 · State taxonomy — every rendered state is one of these (D2)

The artifact's `states.html` is the visual reference; this table is the rule.
A surface may render exactly one page-level state at a time; a panel may render
its own inline state.

| State | Trigger (real, never simulated) | Render | Pin |
| --- | --- | --- | --- |
| **Loading — route chunk** | lazy import pending | `RouteFallback` shimmer, `role="status" aria-label="Loading page"` | `data-testid="route-fallback"` |
| **Loading — query** | `query.isPending` in a panel/row | one compact skeleton row (`StateRow`, new `components/ui/StateRow.tsx`) — never a wall of shimmer, never a spinner | `data-testid="state-row-loading"` |
| **Empty** | a successful read with no rows | the existing three-part `EmptyState` contract (**title + preview capture + one real action**) — see the ruling below | `empty-state`, `empty-state-title`, `empty-state-hint`, `empty-state-preview`, `empty-state-action`, `empty-state-action-reason` |
| **Error** | `query.isError` / rejected mutation | `ErrorPanel` (`role="alert"`, envelope `detail` verbatim) **plus a `Try again` bound to the query's `refetch()`** (new, additive) — F-RETRY: the button is offered only when a repeat can change the answer (`lib/retry.ts::isRetryableError`: no envelope, 5xx, 408, 429). A **policy refusal** (`demo_account_restricted`, `privacy_mode_blocks_external_api`, `encryption_required`) or any deterministic 4xx renders the panel with **no** retry — that state's own affordance owns the way back. While a retry is in flight the control is disabled and reads `Retrying…` (`aria-busy` + a polite `role="status"`); a surface that keeps the panel mounted (its gate is not `isError`-only) keeps the envelope `detail` on screen through the flight | `data-testid="error-panel"`, `error-panel-retry` |
| **Stale (money/price/fx)** | `price_stale`, `price_age_days`, `GET /fx/status{stale}` | the **real number stays**, plus the as-of date and a warn-tone badge; `docs/09` §FX staleness chip and the positions staleness badge keep their copy | existing badge testids; `data-testid="stale-strip"` (new) on the Investments strip |
| **Unpriceable** | `price === null` / `market_value_cad === null` / `spark: []` | `—` plus the "no price — enter one manually" honesty line; **never `$0.00`, never an estimate** | `docs/09` §Empty-state choreography |
| **Unavailable (dead symbol)** | `securities.dead_symbol` | the M141 Watch-section copy + `disabledReason` on the action it would otherwise offer, with a separately-labelled `Try refresh` | `try-refresh`, `empty-state-action-reason` |
| **E2EE** | vault state | `VaultLocked` — `variant="blocked" \| "partial" \| "status"`, carrying `withheld_count`/`total_count` so a locked surface reports **"Showing X of Y — unlock to see the rest"** and never a bare empty result while rows are withheld (`docs/33` A18/M136) | existing `VaultLocked` ids |
| **Read-only demo** | `isReadOnlySession(user)` **and** the action is a genuinely blocked write | the `DemoLock` affordance + the shell banner + one `toast.info` on refusal — §4; the one exception is `Test connection`, disabled **before** dispatch with local copy (§4 W2) | `data-testid="demo-lock"`, `demo-refusal`, `demo-banner`, `llm-test-disabled-reason` |
| **Session lost** | 401 mid-flight | the existing refresh-and-continue path; never a redirect loop (`docs/38`, `docs/09`) | existing |

**Ruling — the artifact's `state-row` does not replace `EmptyState`.** The
product's three-part empty-state contract (what lives here / a real capture /
the one action that fills it, type-enforced at 47 call sites) is a *stronger*
user-visible contract than the artifact's compact row. The compact row is
therefore **additive**: it is the in-panel loading skeleton, the error row with
its retry, and the stale/refresh strip. Removing `preview` or `action` from any
`EmptyState` call site is a contract regression and fails this spec.

## 4 · The demo guard — no faux locks in product code (D3)

This is the single most load-bearing decision of the wave: the artifact's 90
padlocks are a **demo-tenant fact**, and shipping them as static decoration
would (a) tell a live user their product is read-only and (b) create exactly
the dead-control class the gate spent four revisions removing.

**`DemoLock` renders iff ALL of the following hold** (implemented once in
`components/ui/DemoLock.tsx` + `src/lib/demoWrite.ts`):

1. `isReadOnlySession(user)` is true (`src/auth/readOnlySession.ts` — the
   client mirror of the server's boundary);
2. the control's action is a **mutating** request (`POST`/`PUT`/`PATCH`/
   `DELETE`) on a path that is **not** in `DEMO_MUTATION_EXEMPT_PATHS` /
   `DEMO_MUTATION_EXEMPT_PREFIXES` (`backend/app/api/deps.py`: `/api/v1/acb/rebuild`,
   `/api/v1/auth/demo/reset`, `/api/v1/auth/mcp-tokens`(+prefix),
   `/api/v1/projections/run`) — the client mirror carries a comment naming
   that file and the exemption list, so a backend change breaks loudly here;
3. the result cannot be produced honestly without the write — i.e. the rule in
   `docs/09` §Read-only sessions #1 ("a write boundary must never hide a
   calculation") does not apply;
4. **and** no PM-recorded named exception applies. There are exactly two such
   records — **W2** (`Test connection`) and the Settings → Security card's
   disabled-with-notice treatment (**`docs/38` decision 10**) — and both are
   disabled **before** dispatch with local copy: they get **no** `DemoLock`, no
   request and no refusal. The **treatment registry** below is the full list of
   which control gets which treatment; adding another exception requires a PM
   decision recorded there, not a per-screen judgement.

**Everything else never wears a lock.** A live tenant renders zero padlocks in
the entire product. Reads — including **downloads** (`GET /reports/*?format=csv`,
`GET /export/zip`, `GET /export/full`, `GET /export/ofx`) — are never locked:
they are demo-legal, and the artifact itself classified `CSV` inconsistently
(gate observation 3). That ledger-write-vs-download distinction is recorded
here explicitly so it is never re-litigated per screen.

> **PM binding decision W1 — demo exports stay live.** A `GET` export is never
> portrayed as a write and is never locked or disabled, on any tenant, in any
> screen. `docs/11` §"Demo tenant" already pins the server truth this mirrors:
> "All four paths are reads, so the demo user gets them: `GET /export/zip`,
> `GET /export/ofx` and `GET /export/schedule` return **200** with real rows;
> `PUT /export/schedule` returns **403 `demo_account_restricted`**" — the same
> method rule as `GET /export/full` and every `?format=csv` report download.
> The write half (`PUT /export/schedule`) **may** carry `DemoLock`; the read
> half never does. Backend pin `tests/test_export_zip_demo.py`; nothing in
> `docs/11` changes.

**Behaviour of a locked control (demo tenant):**

- It stays **enabled** — never `disabled`, never `aria-disabled`. An inert
  control cannot explain itself, and the gate's own `lock_click` probe (86/86)
  is the standard the product must keep: activating it produces a **local,
  visible explanation** bound to that control (`aria-describedby` → a reason
  in the same panel/unit) **and** one `toast.info("<Action> — the demo account
  is read-only")` from the real `403 demo_account_restricted` response. The
  refusal element is `role="status"` (`data-testid="demo-refusal"`). The
  reason element is **`DemoReason` and it is VISIBLE** —
  `data-testid="demo-reason"`, non-zero box, never `sr-only`/clipped: a
  sighted demo user must be able to read why the glyph is there without hover
  or assistive tech (security follow-up F-4 measured an `sr-only` reason at
  1×1 px on the M185 candidate).
- **One message per page** (`docs/09` §Read-only sessions #3) — the banner, the
  reason element and the toast are three renderings of ONE state, not three
  independent failures; no `ErrorPanel` is raised for a demo refusal, and
  nothing navigates.
- The write path itself is unchanged: the request really goes out and the
  server really refuses, so the UI can never drift from the boundary.
- Confirm dialogs on destructive actions still open, and their Confirm is the
  same locked-but-enabled control (the artifact's mixed-signal observation 5 is
  resolved this way: the refusal names the demo, so the preview dialog and the
  bar agree).

> **PM binding decision W2 — `Test connection` is disabled before dispatch.**
> The first named exception to the "stays enabled" rule above (the treatment
> registry below is the complete list; the only other is `docs/38` decision 10).
> `Test connection`
> is `POST /users/me/llm-test` (`docs/04` §Connection probe, `docs/08` endpoint
> row): a live probe of an **external** provider, so on the demo tenant the
> control renders **disabled with visible local copy and no network dispatch**
> — exact string: **"Test connection is unavailable in the demo. The demo
> account blocks POST requests to keep the sample data read-only."** — bound to
> the disabled control with `aria-describedby` and rendered as the M141
> `disabledReason` pattern, **not** as a `DemoLock` padlock (no 403 is fired,
> so there is no refusal to explain; the reason element carries
> `data-testid="llm-test-disabled-reason"`, distinct from the empty-state
> contract's `empty-state-action-reason` because this is not an empty state). There is no
> `DEMO_MUTATION_EXEMPT_PATHS` entry for `/users/me/llm-test` and none is
> added. On a non-demo tenant the control is unchanged (dispatch, result
> rendering, `400 llm_test_invalid_config` / `502 llm_upstream_error` /
> `422 invalid_llm_base_url` / `403 privacy_mode_blocks_external_api` all
> untouched — the privacy-mode egress check in `app/llm/egress.py` still runs
> and still renders its envelope).

**The refusal SHAPE is absolute (security finding F-2).** A
`403 demo_account_restricted` NEVER renders `role="alert"` and NEVER renders
an `error-panel` — on any surface, in any dialog, on every demo write.
`components/ui/Toast.tsx` renders an **error tone AS `role="alert"`**, so a
single unguarded `toast.error(...)` for that one code is itself the violation
(F-2's measured repro). Every mutation error path therefore runs through the
page's `useDemoRefusal()` first:

- `demo.onError(action, error)` returns `false` for every other code (the
  caller's error path is untouched) and, for THIS code, sets the page's ONE
  refusal element (`data-testid="demo-refusal"`, `role="status"`) and fires
  exactly ONE `toast.info("<Action> — the demo account is read-only")`;
- hosts that do not own the refusal element (the transaction editor's three
  page hosts, a `deferDelete` caller) call
  `lib/demoWrite.ts::isDemoRefusalError(error)` — the single code check — and
  stay silent for that code and nothing else;
- **ONE refusal element per page** (`docs/09` §Read-only sessions #3): a page's
  mutations share ONE `useDemoRefusal` instance. A portal dialog (the
  transaction editor, the split editor) renders that instance's element
  **instead of** the page's — never both — which is why a host passes it in as
  the dialog's `demoRefusal` prop rather than mounting a second one.

**The two verdict helpers (same verdict, two call shapes).**
`useDemoWriteLock(method, path)` is the hook form — `useAuth()` plus the
decision below — for ordinary call sites. `demoLocks(user, method, path)` is
the identical NON-hook function for two shapes a hook cannot reach: controls
inside a `rows.map(...)` (rules-of-hooks) and portal/dialog leaves, which read
the M136 optional user scope (`useOptionalUser()`, `auth/authUser.ts`: outside
an `AuthProvider` there is no session, so there is no lock — the honest
degradation, never a throw). `AuthProvider` wraps the whole app
(`src/App.tsx`), so in product code both resolve to the same user and the two
can never disagree.

**Treatment registry — lock vs disabled-with-reason, decided per control.**
Not a blanket rule: which treatment a control gets is recorded here, and
adding a row needs a PM decision (the same bar as W1/W2).

| Control | Treatment | Record |
| --- | --- | --- |
| Every control on `/transactions`, `/accounts/{id}`, `/bills/{id}` whose activation dispatches the blocked write (review toggle, delete, category patch/create, quick-tag attach/create, transfer detect/link/unlink, split save, tag attach/detach, receipt attach, starting balance, account update, bill rule save + dry-run `POST /bills/{id}/find-past`) | **lock + refusal** — stays enabled, the real 403 renders the refusal | §4 general rule, this rollout (F-2 residual) |
| `TransactionEditor` Save, `SplitEditor` Save (portal dialogs) | **lock + refusal**, the reason sits in the dialog beside Save | §4 general rule |
| Dashboard layout preset picker (`PATCH /users/me {dashboard_preset}`) | **lock + refusal** | §4 general rule |
| Settings: Save profile, Change password, confirm Delete, LLM Save, privacy-mode switch, Currency Tracker switch, category Add/rename/delete, encryption backfill run + "Require encryption" switch | **lock + refusal** (the Account card's `disabled={isDemo}` gating was removed for these: §4 says a locked control must stay enabled, and an inert form cannot explain itself) | §4 general rule |
| `Test connection` (`POST /users/me/llm-test`) | **disabled before dispatch** with visible `llm-test-disabled-reason`, no request, no 403, **no lock** | **W2** (above) |
| Settings → Security card (TOTP enrol/disable, passkeys) | **disabled with the card's visible `DEMO_NOTICE`**, no request, no lock | **`docs/38` decision 10** — its own PM record; `tests/securitySection.test.tsx` pins it. This is the one other place a control is disabled for demo, and it stays only because that record exists |
| Settings privacy-backup panel (enable/disable/remove a local backup) | **no lock, no guard**: its only server call is `GET /export/full` (a W1 read) and the enable/disable writes are local IndexedDB — there is no demo write to refuse, so a padlock would claim something false | §4 condition 2 |
| Every download / read, incl. `GET /export/*` and `?format=csv` | **never locked** | **W1** (above) |
| Affordances that only OPEN a picker, modal, drawer or file dialog (row `Edit`, row `+` tag trigger, row paperclip, `Split`, `Link`, history) | **no lock** — §13 ex.2: an affordance that dispatches no write never wears one; the write-bearing control INSIDE (the editor's Save, the popover's picker input, the link modal's candidate rows) is what locks | §13 ex.2 |

**Other unavailability never borrows the demo lock:** E2EE uses `VaultLocked`;
dead symbols use `disabledReason`; missing data uses `EmptyState`; provider
failure uses `ErrorPanel`. A padlock means exactly one thing in this product —
*the demo tenant cannot save this* — and its copy says so.

**Mockup-only interactions are deleted, not ported.** The artifact's ten
`demo`-classified controls (`Mark reviewed`, four `Not this one`, `Dismiss
all`, `Try again`, `Refresh prices`, `Unlock vault`, lens switch) each map to a
REAL product action here — `POST /transactions/{id}/review`,
`POST /transactions/recurring/suppress`, `GET …refetch()`,
`POST /portfolio/refresh`, the `useUnlock()` flow, the M146 lens — or they do
not ship. **No "local mockup interaction" copy, no control that only mutates
the DOM, exists anywhere in the product.**

## 5 · Visual tokens (D4) — designer owns values, this doc owns the contract

| Rule | Pin |
| --- | --- |
| Token **names** | unchanged — components keep consuming `--color-surface`, `--color-surface-raised`, `--color-border`, `--color-text`, `--color-muted`, `--color-accent`, `--color-accent-soft`, `--color-accent-ink`, `--color-veil`, `--color-warn`, `--color-gain`, `--color-danger`, `--color-overlay`, the glass/shadow/skeleton/scrim tokens (`docs/09` §CSS tokens). Ledger **re-skins the values** of `:root` (Ledger Night) and `:root:not(.dark)` (Ledger Daylight) in `frontend/src/index.css` |
| New tokens | only what the artifact's component set needs that no existing token names — the hairline rule is the one expected addition (`--color-rule`, both themes, added to `frontend/tests/contrastPairs.test.ts` in the same change). No component may hardcode a colour utility |
| Colour discipline | accent is never money, money is never accent; `--color-gain`/`--color-danger` are positive/negative money and destructive emphasis only; `--color-clay`-equivalent (brand/eyebrow) never touches money; **chart series are tints of accent/warn/rule** so a donut can never be misread as gain/loss |
| Contrast | every pair in `docs/09` §Contrast audit table must be re-measured for both themes and the table updated in the same change; the artifact's own measurement (1966 pairs/theme, 0 failures, min 4.51 light / 4.95 dark) is the target, not the evidence — the product's numbers must come from `tests/contrastPairs.test.ts` + `tests/focusRing.test.ts` + the channel-A axe run |
| Focus ring | `2px solid var(--color-accent)`, offset 2, full strength (no `/alpha`), on **every** stop — the artifact measured `rgb(11,79,184)`; the product's value is whatever `--color-accent` resolves to, and it must clear 3:1 on the new surfaces (pair 14 / WCAG 1.4.11) |
| Typography | system stack only (no webfont); money and every figure render `font-variant-numeric: tabular-nums`; type scale may follow the artifact's (H1 26/22, KPI 34/30, H2 17, body 15, table 14, meta 12.5) but stays inside the existing Tailwind utility vocabulary |
| Elevation vs glass | Ledger surfaces use hairlines + a pop shadow for overlays; `backdrop-filter` stays **below md only** (M101) and no new blur is introduced; `.glass` keeps its name and its md+ behaviour for surfaces outside the Ledger scope, so the token contract is not forked |
| Grid | page content `max-w-[1344px] px-6`; 12 columns / 16 px gutter; the **no-orphan grid law** (auto-fit, 240 px minimum: N=1 → 12, N=2 → 6+6, N=3 → 4+4+4, N=4 → 3×4, N=5 → 4+4+4 left-aligned); settings wall is a balanced 2-column flow collapsing at 900 px — **DOM order equals visual order** (no `order:` tricks that break keyboard/AT traversal) |
| Motion | transform/opacity only, ≤ 250 ms on colour/border, `EASE` from `components/ui/motion.ts`; `MotionConfig reducedMotion="user"` plus the CSS `prefers-reduced-motion` block stay; no infinite decorative motion (the artifact's `reduced_motion_css: false` is a mockup-tooling artifact — the product contract is unchanged) |
| Charts | keep the product's chart stack (`docs/09` §Chart palette): `useChartPalette()`, `LEGEND_COLOR` legend text, `formatAxisMoney` money axis, `<details>` data-table fallbacks. No canvas/SVG library rewrite (PROPOSAL N4) |

## 6 · Keyboard, focus and semantics (D5)

Everything in `docs/09` §Keyboard contract, §Naming & semantics contract and
§Accessibility acceptance bar continues to hold verbatim (skip link, landmarks,
`dialogA11y`, one shell Escape listener, `⌘⇧H`, `⌘K`, unique accessible names,
no `aria-label` on generic elements, every field named). Ledger adds:

1. **`More` popover** — the focus contract in §1.1; it is the shell Escape
   listener's fifth consumer, never a sixth listener.
2. **Filter chips** — every removable chip exposes its own labelled ✕
   ("Remove filter: Account — RBC Main", `aria-label` naming the target);
   `Clear all` is a button whose name is unique on the page (M92); removing a
   chip changes the result set in the same paint (no Apply-then-hope).
3. **Truth bar** — plain text, not a landmark; the demo banner inside it is
   `role="status"`; the mask switch inside it keeps `aria-pressed` and its three
   state channels (icon, text, colour) so state never rides on colour alone.
4. **`DemoLock`** — padlock glyph is decorative (`aria-hidden`); the reason text
   is the accessible story, bound by `aria-describedby`; the refusal element is
   `role="status"`, never `role="alert"` (a refused demo write is not an error).
5. **Comparison chips** — a delta chip's accessible text is the full sentence
   ("Net worth up $2,140.55 versus September 13"), not "▲".
6. **Inline keyboard hints** (`Ctrl K`, `E`, `R`, `Esc`) are informational text
   printed where they apply and are **never the only path** to an action.
7. **Charts** keep a text summary + data-table fallback (M58), and a masked
   chart keeps its shape with `•••••` ticks (`docs/46` §D2).
8. **Route-focus handoff** (M185 hardening, card `t_23b8d18e`) — leaving the
   chrome through a primary tab, the `More` panel or the bottom tab bar moves
   focus into `#main-content` on the commit AFTER the shell has closed its
   surfaces (so each popover's own focus restore lands first and cannot win).
   Excluded on purpose: first paint (the skip link stays the first stop),
   focus already inside the content, and an open modal — the transaction
   editor and the ⌘K palette keep focus. `focus({ preventScroll: true })`,
   because the shell does not reset scroll on a route change. `docs/09`
   §Keyboard contract owns the contract; `tests/ledgerShellFocus.test.tsx`
   pins it.
9. **The right cluster's two popovers are disclosures.** The notifications
   bell and the account menu both carry `aria-expanded` + `aria-controls`
   (the `More` button's shape, no `aria-haspopup` promising a popup type the
   panel does not deliver), move focus in on open and hand it back to the
   trigger on close through `useDialogFocus`, and register **no** keydown
   listener of their own. The account menu's borrowed `role="menu"` is gone:
   the panel is an email line, a pressed toggle and two plain buttons, so a
   menu promised `menuitem` children plus roving arrow-key focus this surface
   never had — that is precisely what axe's `aria-required-children` reports
   on the OPEN menu, and the live run over it is pinned.
10. **Focus order is the paint order, and no surface roves.** brand → five
   primary tabs → `More` → right cluster inside the bar; the popover between
   its toggle and the right cluster; the bottom tab bar after the routed
   page. Every stop is a native link or button (no roving `tabindex` — arrow
   keys inside a landmark belong to a screen reader's browse mode), the
   active tab's `aria-current="page"` rides on the tab link itself (NavLink)
   with no nested span to announce it twice, and every control that drops the
   global focus outline replaces it with a full-strength ring (or the
   sanctioned `.field-ring` input treatment).

## 7 · Viewports (D6)

| Width | Verification |
| --- | --- |
| **360** | every Ledger screen: page `scrollWidth === innerWidth` (no horizontal page scroll), every state-row CTA is fully visible without clipping (`client width == scroll width`, `clipped=false`), touch targets ≥ 44 px, tab bar fully hit-testable |
| **390** | the same as 360 plus the artifact's mobile compositions (drawer, filter sheet, portfolio lens) |
| **768** | the `md` boundary: rail→top-bar crossover is settled **at** 768 (product breakpoint is authoritative; the artifact's `≥1100` desktop threshold maps to the content grid's full 12 columns, not to a second breakpoint) |
| **1024** | grid compresses, top tabs may wrap — no overlap, no clipped labels |
| **1440** | the reference composition: `.txt`-class preview geometry, zero overlap between a preview figure and its CTA, heading deltas 0, whole-screen collision sweep empty |

Breakpoints stay Tailwind's (`md` 768, `lg` 1024, `xl` 1280). Adding a custom
breakpoint is out of scope.

## 8 · Display masking (D7) — the artifact's eye toggle is the existing chip

`docs/46` is unchanged and governs: session-scoped, restore-on-load OFF, demo
tenant boots masked, every money component subscribes itself (`useMasked()`) — and for a chart
whose points are memoized that subscription alone does not reach Recharts'
axis memo, so the axis must receive a prop that changes with the mask
(docs/46 §D3; `tests/ledger-ui-build/b1-axis-mask-repaint.test.tsx`),
nothing on the wire, percentages visible **except** book-cost-derived ones
(`fmtSignedPctBookCost`), and the working surfaces (Imports, Reconciliation,
Settings, receipts, export previews) are exempt. Ledger's mapping:

| Artifact element | Product element |
| --- | --- |
| the header privacy button (`data-privacy`, `aria-pressed`) | the existing `MaskChip` (`data-testid="mask-chip"`) in the `TopBar` right cluster, plus the account-menu row (`mask-toggle-row`) — one store, so they cannot disagree |
| `•••••` money cells | the existing mask-aware formatters; no new masking code |
| the masked KPI trio on `states.html` | the same components with `isMasked()` true — no special "masked card" component |
| privacy-scope sentence | the truth bar's privacy segment + the chip's own label (`docs/46` §D2 wording) |

The artifact's own gate found four screens where its `$`-heuristic counted no
money text (`m-transactions` renders `−86.42` with no currency marker). That is
a **mockup-instrument** artifact; the product always renders through the
currency-aware formatter, and the tester's masking acceptance asserts on the
rendered strings, not on a `$` count.

## 9 · Financial-number truth (D8)

1. **Server-authoritative.** Every figure is the payload's Decimal string
   rendered through the mask-aware formatters. No total, ratio or sum is
   re-derived in the browser except the two documented pure folds: M146's
   `lib/positionGroups.ts` (positions by security) and this wave's new
   `lib/spendingFold.ts` (§10 G2).
2. **Unknown is unknown.** `—` + reason for unpriceable; a real number + as-of
   date for stale; `null` stays `null` through a fold (never `$0.00`,
   never `0%`).
3. **A comparison never appears without its basis** (`ComparisonChip`, new
   `components/ui/ComparisonChip.tsx`): value + signed delta + basis text in one
   pill, the basis derived from **payload dates only** — `vs <previous bucket
   label>` for dashboard KPIs (the two cashflow/net-worth buckets already on
   screen), `vs {previous_close_date} close` for the portfolio header (server
   field, `docs/09` §Portfolio header). Never "yesterday", never a wall-clock
   derivation. **The runs are visibly separated too:** the amount group and the
   basis are children of ONE flex row carrying the chip's `gap-1.5` — when an
   `ariaLabel` sentence rides along, the `aria-hidden` visible wrapper is
   `inline-flex items-center gap-1.5`, never a plain inline span, because an
   inline wrapper takes its children out of the chip's flex layout, the gap
   stops applying, and the two runs print glued ("$2Kvs Nov 1"). The sr-only
   sentence carries the same words with spaces. `frontend/tests/dashboardSectionRhythm.test.tsx`
   pins the chip's group contract and the dashboard's section rhythm.
4. **No invented scope claims.** "Transfers excluded" may only be printed where
   the engine actually excludes them — true on the **cash-flow** report
   (`docs/12` §8 excludes `transfer_in|transfer_out`), **not** applicable to the
   transaction-level spending donut, where inter-account movement is not a
   transaction at all (`docs/12` §"Transaction-level transfers do not exist in
   the schema"). The donut subhead states the real scope instead: the
   `spend_total_cad` denominator and the household/own `group_by` scope.
5. **Percentages vs amounts** follow `docs/46` §D2 exactly.
6. **Live ticks** stay inside the M130 carve-out: a stream tick may
   re-multiply by the payload's own `fx_rate_to_cad` and nothing else.
7. **Vendor-free copy**: `yfinance` → "Market data" (`sourceLabel`), no internal
   enum tokens, no spec references (`docs/09` §Naming & semantics M68).

## 10 · API gap decision table (D9)

Every capability the artifact's `README §5` assumed, plus the gate's
observations, adjudicated against the baseline payload. **No backend work is
authorised by any row.**

| # | Artifact capability | Payload at baseline | Verdict | Decision for M185 |
| --- | --- | --- | --- | --- |
| G1 | KPI basis string ("vs Sep 13") | dashboard deltas are computed client-side from two buckets of `GET /reports/{net-worth,cashflow}`; the portfolio header has server `previous_close_date` | partial | derive the dashboard basis from the two bucket dates already on screen; print the server's date verbatim on Investments |
| G2 | Category deltas in dollars, `$50` floor, max 3 chips | `GET /reports/spending/categories` returns one bucket per call (`items[].cad_total`, `spend_total_cad`, `category_id: null` = uncategorized) | partial | issue the **same endpoint twice** (current period + prior period of equal length) and diff client-side in `lib/spendingFold.ts` (pure, cent-quantised); cap 3 chips; floor `$50.00` (ruling on `PROPOSAL` Q3: absolute, so the chip set is stable and testable); a category with no dollar delta renders nothing |
| G3 | Donut excludes transfers | transaction-level transfers do not exist (`docs/12`) | false premise | do **not** print "Transfers excluded" on the spending donut; print the real denominator + scope. Keep it on Cash flow, where it is true |
| G4 | `Uncategorized` as a link with an amount | `category_id: null` slice with `cad_total` | exists | link to `/transactions?category=uncategorized` (the URL key already exists — `lib/createFromSearch.ts::CRITERIA_KEYS`) |
| G5 | Legend folds to `Other (n)` with exact cents | `CategoryBreakdown.items` | exists client-side | `lib/spendingFold.ts`: fold everything after the top 6 slices **except** `category_id: null`, which is never folded; the `Other (n)` legend line carries the exact folded remainder; tooltip lists folded names. Mirrors M167's fold discipline |
| G6 | Recurring evidence `12 charges · ~30d ±2d` | `RecurringCandidate{occurrence_count, cadence, first_date, last_date, next_expected_date, monthly_total}` | partial | render `N charges · <cadence label> · <first>–<last>`; **no ± interval** — the payload has no dispersion field and inventing one is forbidden |
| G7 | Per-row `Not this one` + reversible `Dismiss all` | `POST /transactions/recurring/suppress`, `suppressed_count` | exists | keep the M138 contract (per-row + `Dismiss all (N)` confirm + 6 s Undo toast); **session undo only** (ruling on `PROPOSAL` Q4: no persistent dismissed-suggestions list this wave) |
| G8 | Positions by account / by security, identical totals, expand-to-account | `lib/positionGroups.ts` M146 fold over `GET /portfolio/positions` | exists | adopt as-is; both lenses keep the byte-identical Σ row; **no third lens** (ruling on `PROPOSAL` Q5 — asset class already lives on the Exposure card) |
| G9 | Per-holding as-of + source | `price_date`, `price_source`, `price_age_days`, `price_stale` | exists | render on the row and in the stale strip; `sourceLabel` for the vendor |
| G10 | `Refresh prices` reports what it revalued | `POST /portfolio/refresh` → `{queued, skipped_dead}` | partial | report the two counts ("N prices refreshed", "M symbols have no listing and were skipped"); **no per-symbol list** (not in the payload) |
| G11 | E2EE `non_enrolled` / `locked` / `unlocked` + excluded-row counts | `VaultLocked` variants + `withheld`/`total_count` on the hiding reads | exists | adopt as-is; a locked surface that withholds rows must show the count, never a bare empty result |
| G12 | Import new / duplicate / unresolved + commit **and** revert | `ImportBatch{imported, duplicates_skipped, unresolved}`, `POST /imports/{id}/commit|discard|revert` | exists | adopt as-is; the 3-step rail states "Nothing is live until commit" and the account page keeps its M113 pending banner |
| G13 | Reconciliation "statement balance vs ledger, $0.00 difference" | `ReconciliationResponse.summary{total, matched, unmatched, matched_sum, unmatched_sum}` | partial | the Imports card renders `N of M rows cleared` + **`Unmatched total`** (`unmatched_sum`) — both real fields. A "statement balance difference" is **not** rendered: no payload field carries a statement balance, and deriving one would fabricate a number. `/reconciliation/{id}` keeps its existing summary cards |
| G14 | Server-side filter match count + removable chips | `TransactionPage.total`, chip bar + `Clear all` | exists | adopt; the counter copy stays the product's own "Showing X–Y of N" plus the matched-total line the bulk toolbar already uses — do **not** weaken it into the artifact's "3 of 370 rows match" if the two disagree in wording; both must show the same `total` |
| G15 | Toast + Undo for reversible bulk actions | M95 P3 (5 s) / M138 (6 s) undo contracts | exists | adopt as-is |
| G16 | Command palette indexing pages, actions, payee row-counts | M95 P1 palette | partial | ship the palette unchanged; **payee row-counts are out of this wave** (needs an aggregation no payload carries) |
| G17 | Definition popovers on KPIs | `HelpHint` | exists | adopt; popover copy may only restate documented math (`docs/12`, `docs/05`, `docs/06`) |
| G18 | Truth bar (as-of + source + FX + privacy) | `GET /fx/status`, per-row `price_date`, mask store | partial | §1.3 field list is exhaustive — a segment with no field does not render |
| G19 | `Test connection` control | **exists**: `POST /users/me/llm-test` (`docs/04` §Connection probe, `docs/08` endpoint row) — the Settings LLM provider probe; the artifact's own inventory called it "an external call, not a ledger write" | exists, demo-blocked | **PM binding W2 (§4):** on the demo tenant the control is disabled **before** dispatch with the exact local copy "Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only." (`llm-test-disabled-reason`), no padlock, no request, no exemption added; non-demo behaviour and the privacy-mode egress check (`403 privacy_mode_blocks_external_api`) are unchanged. A **bank**/"provider connection" test surface does not exist anywhere in `frontend/src` and is not invented here |
| G20 | `Customize` control | absent from `frontend/src` (grep, per gate) | does not exist | **drop** (mockup chrome) |
| G21 | `reports.html CSV` vs `transactions.html Export` classified inconsistently (gate observation 3) | CSV/export are `GET` reads, demo-legal (`docs/11` §Demo tenant) | resolvable | **PM binding W1 (§4):** downloads are **never** locked or disabled; the artifact's per-screen inconsistency is resolved in favour of the method, not the screen. Only `PUT /export/schedule` is a write and may carry `DemoLock` |
| G22 | Reduced-motion honoured (artifact's own run reported `false`) | `MotionConfig reducedMotion="user"` + CSS block | exists | keep; the tester asserts it on the product, not the artifact |
| G23 | Five locked destructive controls also open a confirm dialog (gate observation 5) | — | design | resolved by §4: the Confirm button is the same enabled-but-refusing control, and the refusal names the demo |
| G24 | The gate's observation 1 (their CSV missed 42 controls: 11 search buttons + 31 form fields) | product has real search + form fields | n/a | the product's control inventory is the DOM's; tester automation enumerates the product, never the artifact CSV |

## 11 · Component inventory

**Reused unchanged (do not fork):** `EmptyState`/`ErrorPanel`, `RouteFallback`,
`Skeleton`, `Badge`, `Button`, `Card`, `SegmentedControl`, `TableScroller`,
`RangePicker`/`ChartRangeControl`, `HelpHint`, `AnimatedNumber`, `Toast`,
`PullToRefresh`, `SSEStatusIndicator`, `FxStalenessChip`, `dialogA11y`,
`TransactionEditor`, `SimilarExamples`, `BulkActionsToolbar`,
`SplitEditor`, `SubscriptionsPanel`, `VaultLocked`/`useUnlock`, `MaskChip`,
`positionGroups.ts`, `createFromSearch.ts`.

**New (frontend source — soft-dev owns):**

| Component | Encodes |
| --- | --- |
| `components/shell/TopBar.tsx` | desktop chrome + right cluster (§1.1) |
| `components/shell/MorePanel.tsx` | the three-group destination panel (§1.1) |
| `components/shell/TruthBar.tsx` | the bounded truth line (§1.3) |
| `components/shell/MobileTabBar.tsx` | five tabs + centre `Add` (§1.2) |
| `components/ui/StateRow.tsx` | one compact row for in-panel loading / error+retry / stale strip (§3) |
| `components/ui/ComparisonChip.tsx` | value + delta + basis in one pill (§9.3) |
| `components/ui/DemoLock.tsx` + `src/lib/demoWrite.ts` | the demo guard affordance + the exemption mirror (§4) |
| `components/investments/PriceStalenessStrip.tsx` | stale count + `Show as-of times` + refresh outcome (G9/G10) |
| `lib/spendingFold.ts` | the pure donut fold + the two-period category diff (G2/G5) |

Names are pinned so the ownership table and the tester's selectors are stable;
internals are soft-dev's.

## 12 · File ownership plan (D10)

| Area | Owner | May touch |
| --- | --- | --- |
| `docs/47-*`, `docs/09`, `docs/10` (Ledger sections) | **ux** | nothing else |
| `frontend/src/**` (all Ledger source, including the existing test files that pin a contract this spec changes: `tests/m72Mobile`, `tests/m68Navigation`, `tests/m82FontScale`, `tests/contrastPairs`, `tests/focusRing`) | **soft-dev** | source + the contract-pinning tests that fail because of it |
| `frontend/tests/ledger/**` (new directory) | **tester** | nothing outside it |
| acceptance | read-only | may not edit source or tests |
| security integration | read-only | may not edit source or tests |
| backend, migrations, `pyproject`/`uv.lock`, nginx, compose | **nobody in this wave** | any change here is a spec violation, not a shortcut |
| pixel/token values, mockup regeneration | designer (external artifact) | never the product repo |

Disjointness rule: soft-dev and tester never write the same file in the same
change; the tester's automation lands after soft-dev's wave 1 and asserts the
testids pinned in §3/§4.

## 13 · Acceptance examples (derived from the UX gate's measured behaviour)

Each example is a concrete input → observable output, runnable by the tester in
`frontend/tests/ledger/**` and by acceptance against the running stack
(`http://localhost:8080`, not `TestClient`).

1. **Live tenant, zero locks.** Sign in as a non-demo user on `/transactions` →
   the DOM contains **no** `data-testid="demo-lock"` and no `aria-disabled`
   write control; every row `Edit`/`Categorize` performs its real request.
2. **Demo tenant, lock is honest.** Sign in through *Explore the demo* →
   `/dashboard` renders `demo-banner` ("Read-only demo — changes are not
   saved"). The Dashboard's recent-ledger rows are **read-only display** —
   the artifact's inline row `Edit` does not ship (§4 "Mockup-only
   interactions are deleted, not ported"), and on `/transactions` the row
   `Edit` only OPENS the editor (client state, no request), so per §4
   condition 2 an affordance that dispatches no write never wears a lock.
   The controls §4 locks are the ones whose ACTION is the blocked write; on
   this build: `Refresh prices` (header **and** staleness strip —
   `/investments` and `/accounts/{id}`), `Save export schedule` +
   `Save backup schedule` (Settings), and `Back up now` while the vault is
   unlocked. Each carries `demo-lock` with `aria-describedby` → a **visible**
   reason in the same panel that names the demo
   (`data-testid="demo-reason"`, never `sr-only`).
3. **Refusal is local and singular.** Click `Save export schedule` on the
   demo → the real `PUT /export/schedule` fires and is refused `403
   demo_account_restricted`; `demo-refusal` (`role="status"`) appears with
   that control's reason, exactly one `toast.info` fires, **no `error-panel`
   and no `role="alert"` appears**, no navigation occurs, and the schedule is
   unchanged after refetch. Pin for every control named in ex.2/ex.4/ex.18: *a
   `demo_account_restricted` response never renders `role="alert"` or an
   `error-panel` — only `demo-refusal` + one `toast.info`.* (The pin is
   ABSOLUTE: `Toast` renders an error tone AS `role="alert"`, so an unguarded
   `toast.error` for that code already fails it — §4 "The refusal SHAPE is
   absolute".)
4. **Downloads are never locked (W1).** On the demo, `Reports → Spending → CSV`
   downloads a file (`GET …?format=csv`, 200); `Settings → Download ZIP`
   downloads (`GET /export/zip`, 200); neither renders `demo-lock` nor
   `disabled`. The write siblings do: `Back up now` (`POST /backup/artifacts`
   — shown while the vault is unlocked; vault-locked it is `disabled` with
   `VaultLocked`'s own reason instead, since E2EE unavailability never borrows
   the demo lock) and `Export schedule → Save` (`PUT /export/schedule`) render
   `demo-lock`.
5. **Exempt path never locks.** Demo → Reports → Projections → Run renders no
   `demo-lock` on Submit and the chart draws (M157, `docs/20 §6`).
6. **Mask flips in place.** On `/dashboard` masked, press `⌘⇧H` → every money
   string on the page becomes `•••••` within one paint, percentage labels stay
   readable, chart ticks go `•••••` while the line keeps its shape, **zero**
   network requests fire, and the `mask-chip` label flips. The same holds on
   `/investments`, whose portfolio-value axis is the chart that does NOT get
   there by accident (B1 above): both directions, same mount, same axis node.
7. **Mask never crosses a boot.** Mask on `/dashboard`, navigate to
   `/investments` (still masked), hard-reload → the mask is off (tenant
   default), and a demo session reloads masked (`docs/46` §D1).
8. **Basis travels with the number.** On a live portfolio, the header KPI reads
   the server's `previous_close_date` as `vs <Oct D> close`; a first-day
   listing renders no delta at all rather than a fabricated one.
9. **Unknown stays unknown.** A holding with `price: null` renders `—` in every
   money cell plus the "no price" line; `tests` assert no `$0.00` appears in
   that row; a `price_stale: true` row renders the real number with its as-of
   date and a warn badge.
10. **Dead symbol never promises.** A `dead_symbol` security's price panel shows
    the M141 copy, `Refresh prices` carries `empty-state-action-reason`
    (disabled + reason), and the separate `Try refresh` remains.
11. **Locked vault reports the count.** With a locked vault and
    `hide_encrypted=true` on `/transactions`, the page shows
    `Showing X of Y` with `X + Y = total + withheld` and a `VaultLocked`
    `partial` variant — never "No transactions match these filters" while
    `withheld > 0`.
12. **Filters are visible and reversible.** Set account + date filters on
    `/transactions` → removable chips render with named ✕ buttons, the matched
    total updates from `total`, removing one chip changes the result set in the
    same paint, `Clear all` restores the unfiltered set, and the URL round-trips
    (`?account=…&category=…`).
13. **Keyboard parity.** On `/transactions` at 1440: `Tab` reaches skip link →
    top bar → More → chips → rows; `Enter`/`Space` on a chip ✕ removes it; the
    `More` panel opens with focus inside, `Escape` closes it (one press, one
    layer) and returns focus to the button; every stop paints the 2 px accent
    ring.
14. **Touch floor + no squeeze at 360.** Every control on the five mobile
    screens has ≥ 44 px hit area; each state-row CTA's client width equals its
    scroll width with `clipped=false`; `document.scrollWidth ===
    innerWidth` on every Ledger screen at 360 **and** 1440.
15. **Empty vs loading vs error are distinct.** A cold `/reports` shows
    `route-fallback`; an in-flight panel shows exactly one
    `state-row-loading`; a 500 shows `error-panel` with the envelope `detail`
    verbatim and a working `error-panel-retry`; a successful empty read shows
    the three-part `empty-state` (title + preview + one action).
16. **No faux-live control.** No element in the product renders the artifact's
    "local mockup interaction" copy; a source scan greps `frontend/src` for it
    and finds zero matches.
17. **`Test connection` on the demo (W2).** Settings → LLM provider → `Test
    connection` renders **disabled** with `llm-test-disabled-reason` reading
    verbatim *"Test connection is unavailable in the demo. The demo account
    blocks POST requests to keep the sample data read-only."*; the network log
    shows **no** request to `/users/me/llm-test`; no `demo-lock` renders on it.
    On a live tenant the same control is enabled, dispatches, and a privacy
    mode blocking a non-loopback target still surfaces
    `403 privacy_mode_blocks_external_api` in the panel.

18. **Every write surface refuses the same way (F-2's residual rollout).** On
    the demo tenant each control below wears `demo-lock` with
    `aria-describedby` → a **visible** `demo-reason`, STAYS ENABLED, and
    dispatches for real (the server is still the boundary):
    - `/transactions` — the row review toggle, row `Delete`, the row category
      picker (its select **and** its `Create` row), the quick-tag popover's
      picker input, the link modal's candidate rows, the transaction editor's
      Save, the split editor's Save;
    - `/accounts/{id}` — the starting-balance `Save`, the activity rows (the
      same set as `/transactions`) and the editor's Save;
    - `/bills/{id}` — `Save rule` **and** `Test` (the dry-run is
      `POST /bills/{id}/find-past`, so the boundary really refuses it);
    - `/dashboard` — every segment of the layout preset picker;
    - `/settings` — `Save profile`, `Change password`, the confirm `Delete`,
      LLM `Save`, the privacy-mode switch, the Currency Tracker switch,
      category `Add` / row rename / row delete, the encryption backfill run and
      its "Require encryption" switch.
    For each one, a refused write renders **exactly one** `demo-refusal`
    (`role="status"`) **+ one** `toast.info("<Action> — the demo account is
    read-only")`, **zero** `role="alert"`, **zero** `error-panel`, no
    navigation, and the target row/setting is unchanged after refetch; the
    two disabled-with-reason controls (ex.17, and the Security card under
    `docs/38` decision 10) dispatch nothing and keep their own copy. A live
    tenant renders **zero** `demo-lock` and **zero** `demo-reason` elements on
    every one of them. Pinned by
    `frontend/tests/demoRefusal{Ledger,Dashboard,Settings,Details}.test.tsx`
    (32 tests, outside `tests/ledger-ui-build/**`).

## 14 · Verification

Gates, in the order `AGENTS §4` requires, run by soft-dev for the source wave:

0. **Doc reconciliation** — this doc + `docs/09` (shell, mobile nav, Ledger
   section) + `docs/10` (M185 entry) updated in the same change.
1. **Lint** — `cd backend && uv run ruff format --check && uv run ruff check`;
   `cd frontend && npx tsc --noEmit && npx eslint . --max-warnings 0`.
2. **Tests** — `cd backend && uv run pytest -q` (must stay **0 failed**; this
   wave changes no backend), `cd frontend && NODE_ENV=test npx vitest run`
   (existing suites green except the three contract-pinning files named in §12,
   updated in the same change); new Ledger acceptance lives in
   `frontend/tests/ledger/**`.
3. **Build** — `cd frontend && npm run build`; entry chunk stays within its
   documented budget (`docs/09` §Bundle facts).
4. **Rebuild/restart** — `docker compose up -d --build web` (frontend-only
   change), then the running-stack smoke below.
5. **Smoke on `http://localhost:8080`** (headless Chrome, `--headless=old`):
   the 16 acceptance examples of §13, at 360 / 390 / 1440, light and dark,
   demo **and** a live tenant, plus the three accessibility channels of
   `docs/09` §Audit method (axe 0 critical/serious per page × theme; token
   contrast pairs; the scripted keyboard lap + reduced-motion emulation) — and
   `frontend/scripts/a11y/shell_focus_probe.py`, the standing keyboard/focus
   walk of the chrome itself (§19), against a local `vite preview` build.
6. **Date-sweep** — run the web suite under a shifted clock
   (`faketime '<future date> 09:00' env NODE_ENV=test npx vitest run`) so no
   Ledger test locks a clock-derived date to a literal (`AGENTS §6`).

Explicitly **not** evidence: the artifact's own PNGs, its `tools/*.py` output,
or its `control_inventory.csv`. The product's DOM is the measurement surface
(gate observation 24).

## 15 · Touch points and reconciliation log

| Doc | Change |
| --- | --- |
| `docs/47-ledger-redesign.md` | this file (new); §4 then gained the absolute refusal-shape rule, the two verdict helpers and the per-control **treatment registry** (F-2 residual rollout), §13's ex.3 pin was extended to the new ex.18, and §17 logs the rollout |
| `docs/09-ui-ux.md` | §Read-only sessions #3 gains the shipped element contract — ONE `demo-refusal` (`role="status"`) + ONE `toast.info`, and a `demo_account_restricted` response never renders `role="alert"`/`error-panel` — with a pointer to docs/47 §4 for the padlock rules and the treatment registry |
| `docs/09-ui-ux.md` | §App shell & auth flow gains the "M185" supersession callout (this doc pins the contract: the rail and the six-tab mobile bar are superseded on implementation by `TopBar` + `More` + `TruthBar` and the five-tab bar; the mask chip's new home is the top bar's right cluster with its testid, `aria-pressed` and `⌘⇧H` unchanged, and the existing `MaskChip` paragraph keeps describing the pre-M185 placement until implementation); a new `## Ledger redesign (M185)` index section points here |
| `docs/10-roadmap.md` | new `## M185 — Ledger redesign … (t_9ae6c55f)` entry: spec-first, baseline `4887221`, gates owned by the implementation lanes |
| `docs/04-categorization-llm.md` | §Connection probe's "Settings → **Test connection**" row gains the M185 demo-rendering note (disabled before dispatch, docs/47 §4 W2); the endpoint, its codes and its writes-nothing property are unchanged |
| `docs/08-api-spec.md` | the `POST /users/me/llm-test` row gains the same one-line cross-reference; status codes unchanged |
| `docs/11-privacy-export-local-backup.md` | §Demo tenant gains one cross-reference sentence: the Ledger UI renders those reads as **live** controls and locks only the `PUT /export/schedule` write (docs/47 §4 W1). The server contract itself is untouched |
| `docs/47-ledger-redesign.md` | §18 (new): the shell visual-regression matrix — probe path, the 12-cell axes, `PIN_SPEC`, the baseline refresh rule, the pre-redesign SKIP rule and its measured verification numbers; this row added in the same change |
| `docs/13-agent-execution.md` | the probe-runner command block gains the `shell-visual` suite line (docs/47 §18); the cache contract itself is unchanged |
| `docs/09-ui-ux.md` | §Keyboard contract gains the **route focus handoff** and the **shell popovers** (disclosure) bullets; §Escape routing's layer order is corrected to the six surfaces the code actually routes (palette → inbox → search → More → account menu → drawer — it still listed four) and its "one listener" pin now cites `tests/ledgerShellFocus.test.tsx`; the M95 palette verification line records the handoff for the navigating close |
| `docs/47-ledger-redesign.md` | §1.1's More-panel Escape order gains the account menu; §1.1's Session-controls row records the two disclosures; §6 gains items 8–10 (route focus handoff, disclosure popovers, focus order / no roving); §15 gains these rows; §19 is this card's implementation log |
| `docs/02`, other `docs/08` rows | **unchanged** — no table, enum, endpoint, error code or payload changes in this wave (any implementation that needs one has left this spec and needs a new doc + a backend card) |

## 16 · Implementation log (M185 source wave, card `t_778537dd`, soft-dev)

The wave landed as one frontend-source change against this spec. What shipped,
and the handful of deviations, are recorded here so the QA/test/security lanes
read the implemented contract, not only the intended one.

**Landed (source).** Token re-skin in `src/index.css` (Ledger Night `:root` /
Ledger Daylight `:root:not(.dark)`; names unchanged, values the artifact's;
`--color-rule` added both themes + Tailwind mapping + the `contrastPairs`
existence row). New: `components/shell/{TopBar,MorePanel,TruthBar,MobileTabBar,
navGroups}.tsx`, `components/ui/{StateRow,ComparisonChip,DemoLock}.tsx`,
`components/investments/PriceStalenessStrip.tsx`, `lib/{demoWrite,spendingFold}.ts`,
`lib/pageMeta.tsx`. Reworked: `components/layout/AppShell.tsx` (rail retired —
TopBar + TruthBar one sticky z-40 block; appbar + drawer + five-tab bar below
md; the shell Escape router gains More and the account menu as surfaces, still
ONE listener). Deleted: `components/layout/MobileNav.tsx`. Pages: Dashboard
(publishes its range; KPI deltas are `ComparisonChip`s carrying the previous
bucket's payload date as the basis), Investments (publishes the newest
`price_date`; mounts the staleness strip with the shared refresh mutation),
Transactions (`?new=1` opens the create editor — the phone `Add` hand-off),
Settings (W2). `ErrorPanel` gains `data-testid="error-panel"` and an optional
`Try again` bound to the caller's `refetch`.

**Landed (tests).** `frontend/tests/ledger-ui-build/**` (owned directory):
`ledger-folds.test.ts` (G2/G5 folds + the demo-write mirror), `ledger-primitives.test.tsx`
(StateRow, ErrorPanel retry, ComparisonChip, DemoLock + refusal), `ledger-shell.test.tsx`
(TruthBar segments, MobileTabBar five tabs, MorePanel focus/href contract),
`ledger-demo-w2.test.tsx` (PM W2 both sides: demo makes NO request, live still
calls the typed endpoint), `b1-axis-mask-repaint.test.tsx` and
`b2-recurring-tenant-scope.test.tsx` (the two QA blockers, below).
Contract-pinning updates: `m68Navigation`,
`m72Mobile`, `m82FontScale` (six→five tabs), `m95CommandPalette`,
`m100AccountsNav` (rail→More panel/drawer/account menu), `m109Forecast`,
`m135DegradedMatrix`, `m139MaskChip`/`m139RestoreMask`/`m140MaskFlip`,
`m140MaskSweep` (MorePanel subscribes), `m167Choropleth`/`m167WorldMap`
(re-derived ramp hexes), `chartChrome`/`calendarPage` (cited figures moved with
the tokens), `contrastPairs` (`--color-rule` row), `tour` (mark title).

**QA blockers closed (`t_fe3547f4`, candidates `42c9ac7`).** Two release
blockers from the acceptance run on `t_b29beeaa`, both fixed without touching
the redesign's contract:

1. **B1 — the `/investments` money axis never repainted on a mask flip.**
   `AccountValueChartBody` subscribed (`useMasked()`) but Recharts memoizes
   `YAxis` on `axisPropsAreEqual` and `chrome.moneyAxis` is a stable module
   function, so the tick layer saw identical props and bailed in both
   directions. The body now hands Recharts a `tickFormatter` built from
   `useMasked()`'s value for that render — a prop whose identity changes with
   the mask — which invalidates the stale axis layer. Same mount, same axis
   node, zero network requests, Dashboard behaviour untouched (docs/46 §D3
   "Recharts memoizes its axes", rewritten for this case).
2. **B2 — cross-tenant leak in `fintrack.recurring-candidates.v1`.** The
   session cache was unscoped and never cleared, so one tenant's candidate
   rows (payee, amount, transaction ids) rendered for the next tenant in the
   same tab and survived a reload. Entries are now stamped with the identity
   `AuthProvider` binds to the tab, a mismatching read deletes the entry, and
   `scopeCandidatesTo` runs on the same seams as the mask boot contract
   (restore, every explicit identity change, failed boot; `null` clears).
   docs/09 §Recurring prompt carries the contract.

**F-7 closed (`t_c3e65cd5`) — the same frozen axis, as a class.** The security
review of `42c9ac7` (F-7, P2) noted B1 was not one chart: NINE other call sites
pass `chrome.moneyAxis` to Recharts as `tickFormatter`, and three of them
reproduced frozen ticks live (Forecast never subscribed at all and memoizes its
`chartRows`; DividendsPanel subscribes but `data.by_month` keeps its
query-cache identity; UnrealizedBarChart froze on the unmask direction) while
the other six repainted only by the accident of a per-render `data` array —
the incidental repaint docs/46 §D3 warns about, not a contract. The reactive
axis contract now lives in `useChartChrome()` itself (the hook subscribes and
hands back a per-render `moneyAxis` closure), so every consumer gets it by
construction (docs/46 §D3). Pinned per chart by
`tests/f7MoneyAxisMaskRepaint.test.tsx`: all nine, both directions, one mount,
same axis node, frozen fetch log.

**F-9 (`t_adae8886`) — same review, not a Ledger blocker.** The same
re-verification found the currency-exposure donut printing raw amounts in its
`aria-label` and its hover tooltip while masked. Display privacy on the user's
own screen only — no tenant, authz, credential or network exposure, and the
figures never leave the client — so it did not gate B1/B2. Fixed and pinned
under **docs/46 §D2** ("A chart's TEXT ALTERNATIVE is a money surface too"),
which owns the contract; this log records only that it was found here.

**Deviations (reported, not hidden).**
1. `Add` takes the allowed §1.2 deviation path: it navigates to
   `/transactions?new=1`, where the existing editor opens; the param is
   consumed with `replace`. The control is never a no-op; a global editor
   mount is future work, not a blocker.
2. The truth bar's privacy segment is a **status line**, not a second switch:
   §1.1/§8 pin the switch (and its `mask-chip` testid) in the top-bar cluster,
   and a second `mask-chip` in the bar would break `getByTestId` consumers.
   §6.3's "the mask switch inside it" reads as vacuous under that pin.
3. `index.html` / `public/manifest.json` still carry the pre-M185
   `theme-color`/`background_color` literals (`#090c12`) — outside this card's
   file ownership (`frontend/src/**` + `frontend/tests/ledger-ui-build/**`).
   `ThemeProvider` (in `src`) writes the new surfaces after mount; the two
   config-level literals are a follow-up for whoever owns them.
4. The 12 categorical `SERIES_COLORS` slots keep the audited M62/M67 set
   (the CVD floors in `tests/chartPaletteCvd.test.ts` are the evidence); the
   re-skin changed the token-mirroring palette constants only.

**Gates run by the source wave.** `tsc --noEmit`, `eslint . --max-warnings 0`,
`NODE_ENV=test vitest run` (full suite), `npm run build`, plus the running-stack
browser evidence at 1440/390/360 recorded on the card. §14's remaining lanes
(the 16 acceptance examples end-to-end, axe channels, the faketime sweep) are
the QA/test lanes' evidence, not this wave's.

### F-RETRY follow-up (card `t_129646fb`)

The source wave shipped the `Try again` prop and nothing in `frontend/src`
passed it, so §3's Error row and acceptance example 15's "working
`error-panel-retry`" were unimplemented — tester finding F-RETRY. Closed by
wiring every redesign-owned error surface (the §2 routes: Dashboard,
Transactions, Imports, Investments, Reports, Settings, plus the components they
mount):

- `lib/retry.ts` owns the decision and the action: `isRetryableError()` answers
  from the envelope's `code`/status (never `detail`) and `useRetryAction()`
  runs the caller's own re-run with a one-press/one-request guard.
- `components/ui/QueryErrorPanel.tsx` binds an `ErrorPanel` to a query's own
  `refetch()`. It also keeps the last live failure as the panel's record,
  because TanStack v5 clears `query.error` (and `isError`) the moment a refetch
  starts — a surface whose gate holds the panel open (Settings,
  ScheduledBackupPanel) would otherwise drop both the detail and the button for
  the whole flight.
- `ErrorPanel` gains `retrying`: disabled + `Retrying…` + `aria-busy` + a
  polite `role="status"` while the round trip runs. On the `isError`-gated
  surfaces the retry hands off to that surface's own in-flight row instead
  (§3's loading state), and the control's absence is what makes a duplicate
  request impossible there.
- Mutations re-run only where the inputs are still on screen and the request is
  safe to repeat (Imports upload, YNAB preview, Projections run). A failed
  commit deliberately has **no** retry — the batch may have partially landed
  (Imports revert is the honest recovery) and the E2EE `encryption_required`
  refusal is a vault state whose remedy is unlocking, not a second POST.
- Tests: `frontend/tests/ledger-ui-build/ledger-error-retry.test.tsx` (11) —
  the retryability table, the pending/announce contract, and the full
  transient-failure → one refetch → recovery / repeated-failure / no-retry-for-
  refusal loop on the real `TaxAlertsPanel` and on a held-open query surface.

## 17 · Implementation log (F-2 residual rollout, card `t_a51c9b96`, soft-dev)

`tests/ledger-ui-build/**` belongs to another card, so every pin here lives
outside it.

**Landed (source).** The refusal-shape rollout across the five surfaces
security finding F-2 still listed as violating its absolute pin. Shared:
`lib/demoWrite.ts` gains `isDemoRefusalError(error)` (the one code check, now
used by `useDemoRefusal` itself); `components/ui/DemoLock.tsx` gains
`demoLocks(user, method, path)` — the NON-hook twin of `useDemoWriteLock` for
`.map()` call sites and portal leaves; `components/ui/deferDelete.ts` passes the
raw rejection as an optional second `onError` argument (the formatted message
alone could not be told apart from any other failure); `ui/Toggle` gains
`ariaDescribedBy` so a locked switch binds its reason ON the switch;
`ui/SegmentedControl` gains `ariaDescribedBy` (every segment); `CategoryPicker`
gains `locked` / `lockReasonId` / `onError`; `TagPicker` gains `locked` /
`hintId`; `QuickTagPopover` gains `locked`; `TransactionEditor` and `SplitEditor`
gain `demoRefusal` + `onWriteError` and lock their own Save.

**Landed (pages).** `Transactions` (one page instance, ~14 guarded mutations,
row locks sharing ONE visible reason, the editor/split dialogs render the
instance's element INSTEAD of the page's via `refusalPlace`), `Dashboard`
(preset picker lock + guarded `changePreset`/mark-read/split-merge),
`Settings` (Account card converted from `disabled={isDemo}` to lock+refusal —
its notice copy updated accordingly; LLM Save; privacy + currency switches;
category add/rename/delete; encryption backfill; the privacy-backup panel
deliberately unguarded — see the registry), `AccountDetail` (two instances:
starting-balance card + activity rows), `BillDetail` (rule save + dry-run).

**Deviations (reported, not hidden).**
1. The Settings `Account` card's inputs are no longer `disabled` on the demo
   tenant. §4 requires a locked control to stay enabled and forbids a
   disabled-before-dispatch treatment without a PM record; the card had only
   product copy, not a record. Its validation gates (`!canSaveProfile`,
   `!canChangePassword`, `!canDelete`) are untouched, so the demo form cannot
   dispatch until it is genuinely submittable.
2. `Settings` privacy-backup panel: no lock, no guard (registry row) — its only
   server call is a `GET`; a padlock there would claim the demo tenant cannot
   save a backup that IS saved, locally.
3. Components that existing tests render WITHOUT an `AuthProvider`
   (`CategoryManager`, `LlmForm`, the detail pages) take their verdict from
   `demoLocks(useOptionalUser(), …)` instead of `useDemoWriteLock` — same
   decision, the scope `auth/authUser.ts` documents. In the app
   `AuthProvider` wraps the route, so the padlocks render exactly as specified.

**Landed (tests).** `frontend/tests/demoRefusalLedger.test.tsx` (8),
`demoRefusalDashboard.test.tsx` (3), `demoRefusalSettings.test.tsx` (13),
`demoRefusalDetails.test.tsx` (8) — 32 pins: the refusal shape (exactly one
`demo-refusal` + one `toast.info`, zero `role="alert"`, zero `error-panel`,
the write really dispatched), the padlock + VISIBLE reason on each control
named in ex.18, and zero padlocks for a live tenant (ex.1).

## 18 · Visual-regression matrix — shell × mask × state (card `t_47b050ba`, soft-dev)

**Context.** `42c9ac7` replaced the shell wholesale — `AppShell`, `TopBar`,
`TruthBar`, `MobileTabBar`, `MorePanel`, `DemoLock` among 49 files and 3,779
insertions — and jsdom lays nothing out, so no unit test can say the chrome is
still where this spec put it. §14's smoke is per-wave, one-off. This section is
the standing claim, measured: screenshots plus element rectangles, in a real
headless Chrome, against a real build.

**Instrument.** `frontend/scripts/a11y/shell_visual_matrix_probe.py` — 12 cells
(2 viewports × 2 mask states × 3 shell states):

| axis | values |
| --- | --- |
| viewport | `1440×900` — desktop chrome: `app-topbar` + `truth-bar`, no bottom bar. `390×844` — mobile chrome: fixed appbar + `app-nav` drawer + `mobile-tab-bar` |
| mask | `masked`, `unmasked`, driven through `mask-chip`; the cell's `aria-pressed` must match |
| state | `demo` = `/dashboard`; `empty` = `/transactions?q=zzz-matrix-no-match` (a real `EmptyState`); `more` = the opened chrome — `more-panel` on desktop, the nav drawer on mobile |

Per cell it writes one viewport PNG
(`frontend/scripts/a11y/matrix/shell-<width>-<state>-<mask>.png`, pinned to the
viewport's own pixel dimensions; an `empty` cell scrolls its subject into view
first — the filtered table's empty state sits ~1,260 px down on desktop and
~2,340 px on mobile) and measures the shell's element rectangles.

**The geometry pin.** `shell-visual-matrix-baseline.json` stores, per cell id
(`1440x900|demo|masked`, …), the pinned fields of each element, compared at
**±2 px**; a MISSING or an unexpected element fails the cell as loudly as a
moved one. Which fields are pinned lives in the probe's `PIN_SPEC`: heights
that follow page content (`main-content`, `more-panel`, `empty-state`) are
deliberately NOT pinned — pinning them would pin the demo tenant's data volume
rather than the shell. (`topbar-tabs` is `flex-1`, so its width tracks the
right cluster; that is why it differs between mask cells — the chip's own label
does.)

**Invariants asserted on every cell, independent of the baseline** (the full
list is in the probe's docstring; the load-bearing ones):

- no horizontal overflow — `documentElement.scrollWidth <= innerWidth`;
- exactly ONE shell per breakpoint: `app-topbar` + `topbar-tabs` at ≥768 px,
  `appbar` + `mobile-tab-bar` + `app-nav` below it, never both;
- MASKED cells carry **zero** standalone dollar figures, with Recharts'
  off-screen `aria-hidden` `#recharts_measurement_span` (`top:-20000px`)
  EXCLUDED per the PM addendum (uxbot's audit correction — it is not a
  user-visible mask leak); UNMASKED money cells must yield dollars, so a masked
  zero can never be a dead detector;
- the demo banner carries its copy; `empty` cells show a real `EmptyState` and
  populated cells show none; the More panel/drawer is open exactly where the
  baseline says it is (and closed where it is not);
- no error surface at rest: zero `error-panel`, zero `demo-refusal`.
  `role="alert"` is deliberately NOT swept — the dashboard's breached-budget
  alert card carries that role by design (docs/26), so a blanket zero would pin
  the demo tenant's data, not the shell's health;
- the PNG exists at the viewport's own pixel dimensions.

**Running it**

```bash
# verify — the committed baseline IS the gate:
python3 frontend/scripts/a11y/shell_visual_matrix_probe.py --base <url>
python3 scripts/run_probes.py shell-visual --base <url>   # same probe, cached runner   # production <url> needs PROBE_ALLOW_PROD=1

# refresh the baseline — only at the tree under test; it REFUSES to write
# unless every invariant passes, so a broken state can never become it:
python3 frontend/scripts/a11y/shell_visual_matrix_probe.py --base <url> --update-baseline
```

**Baseline rule (PM).** The file records `captured.at`, `base`,
`served_build_commit` and `repo_head`: a baseline describes the tree it was
captured on. Refresh it at the then-current `origin/main`, and never carry a
geometry observation from an older tree forward as a defect without
re-testing the served/current SHA.

**A build from BEFORE the redesign is SKIPPED, not failed.** The probe reads the
served `build-commit` meta and asks `git merge-base --is-ancestor <sha>
42c9ac7`: if the build predates the shell there is nothing to measure (today's
production stack, `8c8ca3f`, is 20 commits behind `origin/main` and was never
rebuilt after M185), so the run exits 0 with `status: "skipped"` and a printed
reason — no Chrome launched. A POST-redesign build with no shell markers FAILS:
that is a regression, not a stale deploy. The session/load traps behind the
probe's retries are in its own docstring (the demo button exists before
hydration, so one click is not evidence; a full page load can drop the session,
so a login page is re-entered rather than measured as "the shell is missing").

**Touch points.** New: `frontend/scripts/a11y/shell_visual_matrix_probe.py`,
`shell-visual-matrix-baseline.json`, `shell-visual-matrix.json` (report),
`matrix/*.png` (12 captures), `suites.json` gains the `shell-visual` suite (the
probe still matches `live`'s `*_probe.py`, and skips against a pre-redesign
deploy). Untracked local helper: `frontend/vite.verify.config.ts` (preview +
API proxy, never committed — the skill's frontend-only verification pattern;
this LXC has no docker, so §4 steps 4–5 ran as local build + headless Chrome).
No `src/` file changes.

**Verification (2026-10-16, soft-dev; base = local `vite preview` build of
`f858719` — `GIT_SHA` stamped into `dist/index.html` — proxying `/api` to the
deployed backend):**

1. baseline capture (`--update-baseline`): **134 passed / 0 failed**, 12/12
   cells, written only after every invariant passed;
2. verification against that baseline: **158 passed / 0 failed**, exit 0,
   12/12 cells `OK` (146 invariants + 12 geometry pins);
3. the pin is proven to BITE, not merely to pass: perturbing two baseline
   values by +5 px gave **156 passed / 2 failed**, exit 1, naming exactly
   `app-topbar.x: 0 vs baseline 5.0 (Δ5.0px > 2.0px)` on
   `1440x900|demo|masked` and `mobile-tab-bar.y: 787 vs baseline 792.0 …` on
   `390x844|more|masked`, the other ten cells green; baseline then restored
   byte-identical (sha256 `7b897506…`) and re-verified green;
4. prod preflight: `--base https://fintrac.princessandmark.com` → `SKIPPED:
   served build 8c8ca3f6bdbb3053880bd7e28906ffcc1d58b37a predates the M185 shell
   (42c9ac7)`, exit 0, in <1 s, no Chrome;
5. gates on this tree: `tsc --noEmit` 0 · `eslint . --max-warnings 0` 0 ·
   `NODE_ENV=test npx vitest run` 159 files / 1457 passed / 5 skipped, exit 0 ·
   `npm run build` OK (stamp `f858719`) · backend `pytest -q` →
   **1 failed / 2738 passed / 7 skipped** (48 min on a box at load 10–16): the
   single failure, `test_receipt_upload_stalls_off_loop`, passes solo in 6.5 s
   and the backend tree here is byte-identical to `origin/main`, so it is
   order/environment, not this change — reported, not hidden.

## 19 · Implementation log (keyboard & focus journey, card `t_23b8d18e`, soft-dev)

Released by PM (unblocked 2026-10-16) for an accessibility hardening slice
over the three surfaces `42c9ac7` introduced — `TopBar`, `MobileTabBar`,
`MorePanel`. Every pin lives in `frontend/tests/ledgerShellFocus.test.tsx`
(21), outside `tests/ledger-ui-build/**`.

**Landed (source).**

- `AppShell` — the **route-focus handoff** (§6.8): `prevPath` plus a
  two-phase `routeFocus` effect. Phase 1 (on `location.pathname`) closes the
  drawer / `More` panel / account menu and records that a navigation happened;
  phase 2 runs one commit later and focuses `#main-content` — after every
  surface's `useDialogFocus` restore has landed, so the `More` button cannot
  win the race back. Guards: first paint, focus already inside `main`, an open
  `[role="dialog"]` / `[aria-modal="true"]`, and `focus({ preventScroll: true })`
  so the handoff does not smuggle in a scroll reset the shell never had.
- `AppShell` — the **notifications bell and the account menu become
  disclosures** (§6.9): both panels gain `ref` + `id` + `data-testid` +
  `tabIndex={-1}` and run `useDialogFocus`, so Escape from inside them returns
  focus to the trigger instead of dropping it on the body; both triggers drop
  `aria-haspopup="menu"` for `aria-expanded` + `aria-controls` (the `More`
  button's shape); the account panel's `role="menu"` is removed — it never had
  `menuitem` children, roving focus or arrow keys.
- `TopBar` — the tab label's nested `aria-current="page"` span is gone;
  NavLink already marks the active tab link, and a nested marker announces
  "current page" twice for one control.

**Measured RED before the source change** (the same file, 10 of 20 failing):
**seven product defects** — the three hand-offs parked focus in the chrome
(the `More`-panel case ended on the `More` button, which is exactly the
restore-order proof), the account menu had no focus contract to restore to,
the account panel still carried `role="menu"` + `aria-haspopup="menu"`, and
the bell advertised a menu it never renders. The other three failures were
harness corrections, not defects, and are reported as such: the brand link
shares the active tab's `href`, `.field-ring` is a sanctioned focus indicator
the pin did not yet accept, and the `aria-current` pin counted the brand as a
second current-page link.

**Negative control.** Restoring `role="menu"` on the live panel and re-running
axe reports `aria-required-children` — so the zero-violation run above proves
the fix, not a rule nobody saw fire.

**Retargeted (deliberate contract change, not a silent pass).**
`tests/m95CommandPalette.test.tsx`'s "…goes, closes, restores focus" pinned
focus returning to the palette trigger AFTER a route change; under §6.8 that
stranding is the defect, so it now pins `#main-content`, and a companion test
keeps the opener-restore pin for the non-navigating close. docs/09's M95
verification line was corrected in the same commit.

**Deliberately NOT changed.** The mobile drawer's background (appbar, `main`
and, at equal `z-40`, the bottom tab bar) stays in the focus order while the
drawer is open. `docs/09` §Navigation chose "no focus trap, Tab walks the DOM
order" for that surface, and inerting it would also block tapping the centre
`Add` over the scrim. That is a product decision for the drawer's own card,
not something to change inside a focus-order sweep.

**Evidence (real headless Chrome, not jsdom).**
`frontend/scripts/a11y/shell_focus_probe.py`, run against a `npm run build`
served by `vite preview` with `/api` proxied to the deployed backend (one
origin, demo session): **21 passed / 0 failed in 45.6 s, zero console
errors** — `frontend/scripts/a11y/shell-focus-probe.json`. It measures what
jsdom cannot: the Chrome tab lap is `skip-link → brand → Dashboard →
Transactions → Imports → Investments → Reports → More → search input → ⌘K →
mask chip → bell → avatar → routed page` (12 chrome stops), every one of
those stops paints a **2 px ring** (`rgb(11, 79, 184) 0 0 0 2px`) while its
`outline-none` keeps the baseline transparent, `More` opens with focus on
`#more-panel`, Tab lands on `Forecast` (ring visible), Escape lands back on the
`More` button, the account menu mirrors that onto the avatar, and all three
handoffs (primary tab / a link INSIDE the More panel / the mobile tab bar)
end with `document.activeElement === MAIN#main-content`. Exit 0 only if every
check passes; its own CDP port and profile keep it off the box's shared
browser. The preview build carried no `GIT_SHA`, so the report's
`served_build_commit` reads `unknown`; the src it served is byte-identical to
the commit that carries this evidence.

**Landed (tests + docs).** `frontend/tests/ledgerShellFocus.test.tsx` (21) —
focus order (3), Escape/focus-return incl. the axe run and its negative
control (6), visible focus + native stops + `aria-current` (5), handoff (6);
`tests/m95CommandPalette.test.tsx` +1 companion. `docs/09` §Keyboard contract
(2 new bullets) + §Escape routing's stale layer order + the M95 verification
line; `docs/47` §1.1 (2 rows), §6 (items 8–10), §15, §18.
