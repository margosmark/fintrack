# 30 — Demo Dataset (M45)

## Context

The demo account (`demo@fintracker.local`, seeded by
`backend/app/services/demo_seed.py::ensure_demo_account`) is the product's
front door: every evaluator lands there. It seeds institutions, accounts,
securities, prices, banking transactions, and brokerage trades — but none
of the lifestyle tables (bill reminders, budgets, debts, goals). `fx_rates`
is **never seeded** (audit H7; docs/07 source split): fx gaps stay empty
until a BoC fetch or manual entry fills them, and the demo's USD trades
defer their ACB pool rebuild on the documented `422 fx_rate_missing` until
then (`POST /acb/rebuild` after gaps fill). Those pages
render permanent empty states for the demo user, and two real bugs survived
for the same reason (no consumer ever exercised null-`posting_date` rows;
see docs/02 date-field rule).

M45 extends the deterministic demo dataset to cover the lifestyle pages.

## Approach

`_ensure_lifestyle(session, user)` runs inside `ensure_demo_account` on BOTH
paths:

- fresh seed: called at the end of `_seed_dataset` (accounts + categories
  already flushed, ids available);
- backfill: called on the early-return path when the user exists and already
  has transactions (upgrades pre-M45 demos, e.g. production, without a wipe).

Every insert is guarded per-table so re-runs are no-ops:

| Table | Guard (skip when present) | Rows |
| --- | --- | --- |
| `bill_reminders` | `(user_id, name)` exists | 5 monthly bills mirroring `_bank_rows` payees |
| `budgets` | `(user_id, category_id)` exists (unique constraint) | 3 monthly budgets |
| `debts` | `(user_id, name)` exists | 2 debts |
| `goals` | `(user_id, name)` exists | 2 goals |
| `rules` | `pattern` exists for the user | 2 enabled `contains` rules (NETFLIX → entertainment, GROCERY MART → groceries) so the M46 dry-run UI has rows to act on |

### Exact rows

Dates are RELATIVE TO TODAY (same convention as the rest of the seed) so
due dates and dashboard widgets stay alive. `next_due_date` = the next
occurrence of the pinned day-of-month strictly after today, `YYYY-MM-DD`.

Bills (`frequency="monthly"`, `remind_days_before=3`, `enabled=true`):

| name | amount | category | due day |
| --- | --- | --- | --- |
| Mapleview Rent | 1400.00 | housing | 1 |
| Fibrestream Internet | 79.99 | utilities | 5 |
| Airlink Mobile | 65.00 | utilities | 18 |
| Spotify Canada | 11.99 | entertainment | 9 |
| Netflix | 16.99 | entertainment | 14 |

Budgets (`period="monthly"`): groceries 600.00, dining 250.00,
entertainment 150.00. Amounts sit ~15-25% above mean seeded monthly spend
for the category so progress bars show partial fill, never 0% or 100%.

Debts: `{name: "RBC Credit Card", account_id: <card>, debt_type:
"credit_card", minimum_payment: 35.00, payment_frequency: "monthly"}`,
`{name: "Student Loan", account_id: <chequing>, debt_type: "student_loan",
original_amount: 8500.00, interest_rate: 5.50, minimum_payment: 120.00,
payment_frequency: "monthly"}`.

Goals: `{name: "Emergency Fund", target_amount: 10000.00, target_date:
<today+12mo YYYY-MM-DD>, account_id: <savings>}`, `{name: "Japan Trip",
target_amount: 3000.00, target_date: <today+8mo YYYY-MM-DD>,
current_amount_override: 750.00}`.

### Invariants

- Deterministic: no RNG in lifestyle rows (fixed amounts/days).
- Category links resolve by name from the user's seeded categories; a missing
  category skips that row (never a FK violation).
- Account links resolve by account type (card → credit_card, chequing,
  savings); a missing account skips that row.
- Money is `Decimal` end-to-end (AGENTS.md §2); amounts serialize as strings.
- Tenancy: every row carries the demo `user_id`.

## Reset & fingerprint (M50)

Two helpers make the seed re-runnable and provable (docs/20 carries the route
contract for `POST /api/v1/auth/demo/reset`):

### `reset_demo_tenant(session) -> User | None`

Returns `None` when `SEED_DEMO_ACCOUNT` is false (the route answers `404
demo_disabled`); otherwise, in **one transaction**:

1. `delete_user_rows(session, demo_user, keep=(RefreshToken, McpToken))` — the
   FK-safe child→parent cascade of `app/api/users.py::_USER_OWNED_MODELS`
   (docs/20 order), with the junction rows (`rule_tags`, `transaction_tags`)
   cleared first by subquery. Kept on purpose: the `users` row (the tenant
   identity, so live sessions keep resolving) and the two credential tables
   (`refresh_tokens` = other visitors' sessions, `mcp_tokens` = operator-issued
   MCP access) — credentials are not demo *data*.
2. `_add_default_categories(session, user)` — the ten `is_system` categories
   were wiped in step 1; they are re-created before the seed runs so every
   seeded transaction/budget/bill/rule resolves a category exactly as a fresh
   seed does (this is the same helper `ensure_demo_account` uses when it
   creates a user).
3. `ensure_demo_account(session)` — the user exists and now has **zero**
   transactions, so the re-seed guard does not fire and the full M10 dataset is
   generated (fresh RNG seed `_SEED`, dates relative to *today*) and committed
   once.

Anything that raises before that commit rolls the whole thing back: there is
no reachable state where the demo is wiped but unseeded. `fx_rates` is never
read or written here (H7): a reset does not create fx rows, so the demo's USD
trades still defer their ACB rebuild on `422 fx_rate_missing` until a BoC fetch
or manual gap-fill covers them — exactly as after a cold seed.

### `seed_fingerprint(session, user_id) -> str`

The M10 acceptance property ("identical fingerprints across fresh seeds",
`docs/10`) made checkable: a `sha256` hex digest over the tenant's content
rows. Determinism rules (the reason it is not a naive dump):

- **Serial ids are excluded, FK ids are resolved to natural keys.** Sequences
  are not rewound by a wipe, so a re-seed's `id`s differ by construction;
  references map to `account -> name|type`, `category -> name`, `security ->
  symbol`, `institution -> name`, `import_batch -> account|file`,
  `transaction -> account|date|description|amount`, `budget ->
  category|amount|period`, `rule -> pattern`, `tag -> name`.
- **Excluded columns:** `id`, `user_id`, `created_at`, `updated_at` (server
  generated), plus `users.hashed_password` (bcrypt salt differs per hash) and
  `users.llm_api_key` (a secret; never worth reading into a digest input).
- **Coverage:** `users` + every model in `_USER_OWNED_MODELS` except the two
  credential tables `refresh_tokens` / `mcp_tokens` (random per login, not seed
  state), plus `_RESET_EXTRA_MODELS` (M63/M63b — `backup_artifacts` and
  `vault_envelopes`, wiped by the reset but outside the cascade) — i.e.
  exactly the set `reset_demo_tenant` wipes — plus the two junction tables,
  whose rows are content too.

Rows are serialized as sorted-key JSON per row, sorted as a whole, then hashed.

## Touch points

- `backend/app/services/demo_seed.py`: `_ensure_lifestyle` + both call sites;
  `reset_demo_tenant`, `seed_fingerprint`, `_add_default_categories`.
- `backend/app/api/auth.py`: `demo_reset` route.
- `backend/app/api/users.py`: `delete_user_rows` (shared FK-safe wipe).
- `backend/tests/test_demo_seed.py`: pin counts + backfill + idempotency +
  reset/fingerprint contract.
- `docs/10-roadmap.md`: M45 entry (below).
- Frontend: untouched (pages already render these shapes).

## Verification

- `uv run pytest tests/test_demo_seed.py -q` green on real Postgres,
  including: bills == 5, budgets == 3, debts == 2, goals == 2, rules == 2
  after a fresh seed; second `ensure_demo_account` changes nothing; a user with
  transactions but empty lifestyle tables gets exactly the M45 rows.
- **Reset (M50):** `seed_fingerprint` before → mutate the demo through the API
  → fingerprint differs → `POST /api/v1/auth/demo/reset` → 204 and the
  fingerprint equals the pre-mutation digest; `SEED_DEMO_ACCOUNT=false` → 404
  `demo_disabled`; a non-demo caller → 403 `forbidden`.
- Live: `POST /api/v1/auth/demo` then `GET /bills`, `/budgets`, `/debts`,
  `/goals` return the pinned rows; `/bills`, `/budgets`, `/debts`, `/goals`
  pages render data instead of empty states.
