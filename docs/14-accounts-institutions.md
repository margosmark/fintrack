# 14 — Accounts & Institutions

Plan for real-world account breadth: many accounts, same or different institutions, cash and registered investment types. Milestone anchor: **M9** (docs/10). Backend schema already supports multi-account/multi-institution tenancy; this plan closes the type-coverage gap and builds the missing management UI.

## What already works (no change needed)

- **N institutions per user**, each carrying its own CSV import profile (`institutions.importer_profile`, docs/02).
- **N accounts per institution** and accounts with `institution_id NULL` (e.g. cash-only tracking).
- Every account carries its own `type` and `currency` — a USD chequing at one bank alongside CAD chequing/savings at another is just rows.
- All aggregation paths are user-scoped by design: positions, exposure, net worth, spending, cash flow sum across ALL accounts; portfolio/positions reports per (account, security).
- Registered plans already exclude themselves from tax math (`taxable=false` → no `acb_pools`, docs/05/06); net worth counts them (docs/12).

## Gaps

| # | Gap | Impact |
| --- | --- | --- |
| G1 | Account type enum lacks `lira` and `rdsp` | Locked-in and disability plans can't be represented |
| G2 | No UI creates/edits/archives accounts; `api/accounts.ts` exposes list only | Fresh users hit an empty account picker on Imports; API-only workaround |
| G3 | No consolidated view of accounts grouped by institution | Users can't see their structure or balances at a glance |

## Changes

### C1 — Widen account type enum (G1)

Add `lira` (Locked-In Retirement Account) and `rdsp` (Registered Disability Savings Plan) to the `AccountType` StrEnum (docs/02 conventions make this a constraint rebuild, not a type migration):

- `backend/app/models/accounts.py`: two new members.
- Alembic revision `0002_widen_account_types`: drop + recreate the CHECK constraint with the full value set (native_enum=False VARCHAR — no table rewrite, docs/02).
- Taxability rule unchanged and exhaustive: `taxable=true` ONLY for `brokerage_non_registered`; every registered plan (rrsp, tfsa, fhsa, resp, lira, rdsp) is `taxable=false`. LIRA withdrawal restrictions / locked-in rules are OUT OF SCOPE (informational tracker, like all tax features — docs/00 non-goals).

### C2 — Accounts & Institutions manager (G2, G3)

New **Settings → Accounts** section (same page as category manager — no new route, docs/09):

> **SUPERSEDED in part by M100 (C10 below).** The *manager* still lives here,
> unchanged — but the "**no new route**" half of this decision was wrong: a
> control labelled "Accounts" needs a destination that IS accounts, and the
> absence of one is exactly how Mark clicked Accounts and landed on Settings'
> Security. `/accounts` now exists as a read-only index (C10); this section
> remains the create/edit/archive home.

- Grouped list: institutions as headers (name + profile badge), accounts nested under each, ungrouped ("No institution") last.
- Per-account row: name · type chip · currency · computed cash balance (Σ committed transactions, native currency — reuse the imports/valuation cash-sum helper) · trade/position count · edit · archive.
- Create/edit form: name, institution select (+ inline create-institution), type dropdown (full widened enum, grouped Cash / Credit / Registered / Non-registered), currency (CAD/USD + free-text ISO), taxable shown derived-from-type (read-only hint, set server-side).
- Archive over delete: DELETE stays guarded (`account_in_use`) exactly as today (docs/08); archiving = rename convention deferred — v1 keeps hard-delete-with-guard only. (Decision: no soft-delete column; churn risk not justified yet.)
- Imports page: account picker gains "+ New account" inline affordance posting to the existing POST /accounts, so first-run users never leave the wizard.

### C3 — Complete the accounts API client

`api/accounts.ts` gains create/update/delete typed helpers matching the existing backend routes (docs/08 core CRUD) — backend needs NO shape changes beyond accepting the widened enum values.

## Touch points

- docs/02: accounts type enum line gains lira, rdsp; indexes/conventions untouched.
- docs/09: Settings row mentions Accounts manager; account detail page; sidebar accounts section.
- docs/10: M9 (original), M20 (new features below).
- docs/00: feature-list row.

## Verification

1. Migration up/down clean on a DB holding rows of every old type; new types accepted post-upgrade.
2. CRUD round-trip through UI-level API calls: create institution + two accounts (one chequing CAD at it, one TFSA USD ungrouped), archive-guard fires `account_in_use` after a transaction lands.
3. Net worth / positions unchanged when accounts share an institution vs differ (sums identical to hand calc).
4. Imports wizard: create account inline mid-flow, appears selected in picker.

## M20 features — starting balance, account detail, transfer suggestions

### C4 — Starting balance column

Add `starting_balance: Decimal(18,2) | None` to `accounts` (migration 0009, nullable → existing rows get NULL, code treats NULL as `Decimal("0.00")`):

- `backend/app/models/accounts.py`: new field with `sa_type=Numeric(18, 2)`.
- `backend/app/schemas/accounts.py`: `starting_balance` in `AccountCreate`, `AccountUpdate` (optional), `AccountPublic` (serialized string).
- `backend/app/api/accounts.py`: `_public()` offsets `cash_balance` by `starting_balance` (`cash_balance = committed_transactions + starting_balance`).
- `backend/app/api/accounts.py:_build_public` also returns derived `investment_value` (account-native market value of the account's positions — `cad_total` converted from the reporting currency back to the account's native currency) and `total_value` (`cash_balance` + `investment_value`), in addition to `cash_balance` / `starting_balance` (docs/08). The account detail page / sidebar render `total_value`, not `cash_balance` alone (docs/09).

### C5 — Per-account ledger endpoint

`GET /accounts/ledger?account_id=` returns all committed transactions for an account ordered by `date ASC, id ASC`, each with a `running_balance` (starting_balance + cumulative sum). Response: `{items: [{transaction_id, date, description, amount, running_balance}], starting_balance, current_balance, total}`.

### C6 — Transfer suggestion detection

`GET /transactions/transfer-suggestions?account_id=` finds unpaired transactions (`transfer_pair_id IS NULL`) with opposite `amount_native`, `abs(amount)` equal, date within ±10 days. Deduplicated (each pair returned once by `source_id < candidate_id`). Optional `account_id` filter.

`POST /transactions/accept-transfer-suggestion` with `{source_id, candidate_id}`: validates both exist, belong to user, are unpaired; finds "transfers" system category; sets `category_id` and `transfer_pair_id` bidirectionally on both.
- Per-transaction transfer pairing endpoints (M22):
  - `POST /transactions/{id}/detect-transfer` — auto-pairs the given transaction with the opposite-amount transaction in a different account within ±3 days; `404 no_matching_transfer` when none matches, and `404 transfers_category_not_found` if the `transfers` system category (`is_system=True`) is missing.
  - `POST /transactions/{id}/link-transfer` with `{pair_id}` — manually links the two transactions as a transfer pair (any prior pair on either side is unlinked first).
  - `POST /transactions/{id}/unlink-transfer` — clears `transfer_pair_id` on both sides.

### C7 — Account sidebar

Global left sidebar section in `AppShell.tsx`: accounts fetched via `listAccounts(200)` under the `["accounts"]` key, grouped by type using the `TYPE_GROUPS` constant (Cash / Credit / Registered / Non-registered) — since M100 the constant lives in `lib/accountGroups.ts` and is shared with the `/accounts` index. Each account is a `<NavLink to={/accounts/${id}}>` showing name + `total_value` (cash + positions in the account's native currency — this paragraph said `cash_balance`, which the code has not rendered since M20/C4).

> **SUPERSEDED by M100 (C10):** the original last line read *"Collapsed
> sidebar: wallet icon link to settings."* That was true of the CODE but wrong
> as a DESIGN — it is why the collapsed ACCOUNTS control navigated to Settings'
> Security (never-worked since M21, pinned verbatim here and in docs/09). The
> collapsed control is now `<NavLink to="/accounts" aria-label="Accounts">`,
> the expanded `Accounts` HEADING is a `<NavLink to="/accounts" end>` too, and
> both render even when the account list is empty.

### C8 — Account detail page

`/accounts/:accountId` route, `AccountDetail.tsx` page:

- **Header**: account name, type badge, currency, cash_balance.
- **Running balance chart**: recharts `AreaChart` from ledger data, theme-aware palette, glass tooltip.
- **Starting balance**: inline editable field, save on Enter/blur via `PATCH /accounts/{id}`.
- **Transactions table**: full filter bar (date range, payee, amount, direction), inline category dropdown, transfer detection buttons, paginated (50/page). Uses `listTransactions` with `accountId` filter.
- **Transfer suggestions panel**: `getTransferSuggestions(accountId)`, accept/dismiss actions.
- **Two-column layout**: Account Info sidebar on wide screens (type, currency, starting balance, transaction count).
- Styled to match Transactions page: glass filter bar, sticky uppercase headers, row stagger animation.

### C9 — Clickable account names

Account names in `Transactions.tsx` and `AccountsSection.tsx` wrapped in `<Link to={/accounts/${id}}>` for navigation to account detail page.

## M100 — accounts index

### C10 — Accounts index page

**Context.** The collapsed rail's icon labelled *Accounts* has pointed at
`/settings` since M21 (b7d03a7), with this doc and docs/09 pinning that
sentence as a design decision. Mark clicked it on prod and landed on Settings'
Security card. PM ruling on M100: build the real destination — a control's
label and its route must agree, and the "no new route" half of C2 was a
spec-legacy mistake, not a design.

**Approach.**

- **Route (client-side only — no new API surface).** The existing
  `GET /api/v1/accounts` list endpoint is unchanged and is what the page reads;
  what is new is the FRONTEND route `/accounts`, declared in `App.tsx`'s route
  table as `{ path: "accounts", element: route(pages.accounts) }` and
  lazy-loaded like every other page module (`src/pages/Accounts.tsx`). Static
  `accounts` outranks the dynamic `accounts/:accountId`, so both live in the
  table; no hash, no anchor, no redirect.
- **Data** `listAccounts(200)` → `GET /api/v1/accounts?limit=200&offset=0` →
  `AccountPage {items: Account[], total, limit, offset}` (docs/08), cached
  under `["accounts"]` — the SAME key the sidebar and the Settings manager
  use, so the three surfaces share one request and one invalidation. Loading →
  `SkeletonRows`; error → `ErrorPanel`.
- **Rendering** groups come from `lib/accountGroups.ts::TYPE_GROUPS` (Cash /
  Credit / Registered / Non-registered), each group a `Card` with an `h2`
  heading and a per-account `<Link to={/accounts/${id}}>` row: name · type
  label (`TYPE_LABELS`) · `formatMoney(total_value, currency)`. **No total
  across accounts is ever computed** — summing native amounts across
  currencies without the cached rate is forbidden (AGENTS §2), so the page
  fetches no FX rate at all.
- **Empty state** the standing three-part contract: title "No accounts yet" +
  a real capture of this page populated (`/help/accounts.png`) + one action,
  `Add an account` → `/settings` where the C2 create form lives.
- **Wire-up** expanded rail: the `Accounts` section heading is
  `<NavLink to="/accounts" end>` (renders even with zero accounts; `end` keeps
  its active pill off `/accounts/:id`, where the per-account row owns the
  highlight). Collapsed rail: `<NavLink to="/accounts" aria-label="Accounts">`
  with the Wallet glyph, active pill, `min-h-11` and a `RailTip` label bubble
  (docs/09 §Rail label bubble). ⌘K: `page-accounts` in the palette registry.
  `/settings` itself is untouched — Account + Security cards unchanged.
- **Accessibility** every collapsed rail control now carries
  `aria-label` == the expanded row's visible label, a 44px floor, and the
  existing `focus-visible` ring; the native `title` (hover-only, invisible to
  keyboard) is gone from the rail.

**Touch points.** `frontend/src/pages/Accounts.tsx` (new),
`frontend/src/components/layout/RailTip.tsx` (new),
`frontend/src/lib/accountGroups.ts` (new — `TYPE_GROUPS` moved out of
`AppShell`, `TYPE_LABELS` moved out of `AccountsSection`), `App.tsx`,
`AppShell.tsx`, `CommandPalette.tsx`, `public/help/accounts.png` (new
capture), docs/09 (pages table, routes, sidebar section, §Tooltips,
§Empty-state preview sources, §Touch-target floor, §Command palette),
docs/10 (M100), docs/14 (this section, C2/C7 markers).

**Verification.** `tests/m100AccountsNav.test.tsx` (16 tests): the regression
test `collapsed_accounts_icon_goes_to_accounts_page`; the full rail audit —
every collapsed and every expanded link's `href` and every click's resulting
route, run in BOTH themes (4 combinations); `aria-label` == expanded label for
every collapsed control, `title` absent; tab-order sweep + activation lands on
the named destination; the label bubble (text, portal, Escape-only dismissal,
absent when expanded); axe-core zero violations with a bubble open; the page's
list/empty/error states (including the preview capture existing on disk);
⌘K navigation to `/accounts`; the phone drawer's entry. Plus the three
existing suites this change touches — `m68Navigation` (the rail's href list now
ends `/accounts`), `m95CommandPalette` (registry route + default-8 order),
`m95EmptyStates` (48 call sites, the capture exists).
