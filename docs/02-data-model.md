# 02 — Data Model

All tables are SQLModel classes in `backend/app/models/`, migrated via Alembic. Every domain table carries `user_id: FK → users.id` from day one (tenancy lock, docs/01). Money is `Decimal` end-to-end: cash `Numeric(18,2)`, share quantities `Numeric(18,6)`, rates/prices `Numeric(20,8)`.

**Tenancy is indexed, and the index is asserted.** `user_id` is the predicate on essentially every domain query, so a table that carries the column but no index on it answers with a sequential scan that grows with the TABLE. `migration 0050_tenancy_idx` adds `ix_acb_pools_user_id`, `ix_prices_user_id`, `ix_rules_user_id` and `ix_import_batches_user_id`; the models declare `index=True` so autogenerate agrees. `acb_pools` is the one that mattered — `app/tax/acb.py::rebuild_user_pools` opens with `SELECT … WHERE user_id = ?` and runs on every trade create/update/delete, so the single sanctioned write path for that table (docs/02, AGENTS.md §2) is now also its best-indexed statement; its only other index leads with `account_id` and could never serve it.

Two tables that look like the same gap deliberately are **not** indexed, verified against `pg_indexes` rather than assumed from the model source: `securities` (`uq_securities_symbol (user_id, symbol, exchange)`) and `learned_payees` (`uq_learned_payees_user_payee (user_id, normalized_payee)`) both already have `user_id` as the LEADING column of a unique constraint, so `WHERE user_id = ?` is served and a second index would only add write amplification to the hottest upsert path.

`backend/tests/test_models.py` **derives** the model list from `app.models` rather than hand-listing it (it used to name 20 of the 49 tables, so the tenancy invariant silently exempted 29). Every table model must carry `user_id` and `TimestampMixin` except an explicit, justified allowlist: `fx_rates` (the ORIGINAL shared cross-tenant cache — the reason `POST /fx` is rate limited), `provider_fund_registry` (M168: issuer coverage shared by every tenant), `benchmark_prices` (M173: index closes shared by every tenant, docs/28), `users` (the principal, self-referencing via `id`), and the three FK-cascading child/join tables `bill_reminder_match_terms`, `rule_tags`, `transaction_tags`, which inherit tenancy through their parent row.

Conventions:

- **Money rounding has ONE implementation, and its mode is always stated.** `app.tax.acb.cents(value)` is the single canonical entry point (AGENTS §2) and defaults to `ROUND_HALF_UP` — the money convention pinned by docs/06, docs/12 §money and `backend/tests/test_reports_money.py`. No module defines a private `.quantize(...)` for money and no call omits `rounding=`: a bare `.quantize()` silently reads the ambient decimal context, so which way a `.xx5` tie rounded depended on the process rather than on the spec, and two surfaces could print the same money differently. Three surfaces pass `cents(value, rounding=ROUND_HALF_EVEN)` **deliberately**, each pinned by a different kind of evidence: `services/valuation.py` and `services/exposure_breakdown.py` (docs/08 §Portfolio value history F-M130-1 and docs/16 §4, with the exact tie hand-computed in `tests/test_m130_portfolio_value.py`), and `services/budgets.py::spent_between` / `carry_in` (`frontend/src/lib/s3Engine.ts` mirrors the mode and `frontend/tests/fixtures/s3-vectors.json` is a golden captured from that function — docs/33 §S3 parity). Everything else, including `api/accounts.py::_format_cash` (every `cash_balance` / `investment_value` / `total_value` / `starting_balance` / ledger `running_balance`) and `api/securities.py`'s overview totals, is half-up. `backend/tests/test_money_rounding_single_source.py` pins both halves: the helpers are the canonical object, and the three half-even surfaces stay half-even.
- Enums are Python `StrEnum` columns via `sa.Enum(native_enum=False)` (VARCHAR + check constraint) — no native PG enums, so widening an enum never needs a type migration.
- Every table carries `created_at`/`updated_at` (`DateTime`, server default `now()`, `onupdate=now()`) via a shared mixin; omitted from the listings below.
- Tenancy exception: three tables carry **no** `user_id`, each a fact about the world rather than about a tenant — `fx_rates` (a USD/CAD rate on a date is universal), `provider_fund_registry` (M168: issuer coverage), and `benchmark_prices` (M173: an index close on a date). Every other domain table carries `user_id` from day one; `refresh_tokens` is auth plumbing scoped by `user_id`.

## ERD

```
users ─┬─< institutions ──< accounts ─┬─< transactions >─ categories (self-ref parent_id)
       │                              │        │
       │                              │        ├─< import_batches
       │                              ├─< trades >──── securities
       │                              │        │           │
       │                              │        │      acb_pools (non-registered only)
       │                              │        └──────────┴─ fx_rates (by trade date)
       ├─< rules >──────────────── categories
       ├─< mcp_tokens (MCP server auth)
       ├─< mcp_audit (MCP tool-call receipts, M80)
       ├─< sharing_links · sharing_audit (household sharing, M90 — docs/43)
       ├─< totp_factors · recovery_codes · webauthn_credentials · oidc_links (M74, docs/38)
       ├─< llm_suggestions >────── transactions, categories
       ├─< budgets >─────────────── categories (unique per user+category)
       ├─< dividend_history >───── securities (M184 raw provider events)
       ├─< dividend_history_refresh > securities (M184 refresh state)
       ├─< goals >───────────────── accounts (optional link)
       ├─< bill_reminders ───────── categories (optional link)
       │                      └─── bill_reminder_match_terms (M121, ordered terms)
       ├─< debts ────────────────── accounts
       ├─< spending_alerts >─────── budgets
       ├─< budget_carryovers ─────── budgets (M110, frozen carried_in per period)
       ├─< reconciliation_items >── accounts, transactions
       ├─< tags ─────────────────── transaction_tags >── transactions
       │                      └─── rule_tags >────────── rules
       ├─< notification_settings (alert_type + config JSON)
       ├─< notification_inbox_reads (item_key + read_at, M51)
       ├─< recurring_suppressions (declined recurring candidates, M96 — docs/09)
       ├─< transaction_events (ACTIVITY receipts, M106 — tx_id has NO FK)
       ├─< transaction_receipts (receipt IMAGE, M108 P4 — unique per tx)
       └─< tax_projections (per-year province/income/deductions)
prices (manual + yfinance cache) >──── securities
```

ERD note (M107 — docs/44 §1): `transactions.parent_split_id` is a **second
self-reference on `transactions`** (a split child → its parent), not drawn in
the tree above so the tree stays readable. It behaves exactly like
`transfer_pair_id`: a self-FK the export/restore dependency graph already
tolerates (docs/11), but with `ON DELETE CASCADE` (a batch discard/revert or
an account deletion takes the children with it) and
`DEFERRABLE INITIALLY DEFERRED` (a snapshot whose children precede their
parents still restores). FK name `fk_transactions_parent_split`.

## Tables

### users
`id PK`, `email` (unique), `hashed_password`, `is_admin: bool = false` — no other role system until the SaaS phase.
| Optional `name` (`str | null`, display name, max 255) — returned by `GET /users/me` and editable via `PATCH /users/me/profile` (docs/20); null until the user sets one.

Server-side preferences (persisted on the row so they roam across devices; columns created in the M1 initial migration, consumed by M7 — docs/11):

- `privacy_mode: bool = false` (consumed by docs/11, milestone M7)
- `currency_tracker_enabled: bool = true` — per-user feature flag controlling visibility of the Currencies page + Tools sidebar section (milestone M23, docs/15)
- `llm_provider` enum `ollama | openai_compatible` default `ollama`, `llm_base_url: str nullable`, `llm_model: str nullable`, `llm_api_key: str nullable` — **write-only** (accepted on write payloads, never returned by any read, like `hashed_password`)
- Non-null user-row values override the env defaults (docs/01) at runtime

Scheduled-backup schedule (M56 — docs/11 § Schedule model; five columns added by migration `0029_backup_artifacts`, roams across devices like every other preference, range-CHECKed in the DB):

- `backup_frequency` enum-without-pg-enum `off | daily | weekly | monthly` (`VARCHAR(16) NOT NULL DEFAULT 'off'`, `BackupFrequency` StrEnum) — `off` is a value, never NULL, so the schedule always has a complete shape
- `backup_hour SMALLINT NOT NULL DEFAULT 3` (0–23), `backup_weekday SMALLINT NOT NULL DEFAULT 0` (0–6, **0 = Monday**, weekly only), `backup_day_of_month SMALLINT NOT NULL DEFAULT 1` (1–28, monthly only), `backup_keep_last_n INTEGER NOT NULL DEFAULT 5` (1–20 retention) — all in the **local time of the machine running the tab** (docs/11 due semantics)
- CHECKs `ck_users_backup_frequency` / `_hour` / `_weekday` / `_day_of_month` / `_keep_last_n`; `PUT /backup/schedule` returns 422 `backup_invalid_schedule` for the same ranges

Scheduled-export schedule (M75 — docs/11 § "Scheduled export"; five columns added by migration `0032_export_schedule`, an **independent** setting from `backup_*` so a user can run one and not the other):

- `export_frequency` enum-without-pg-enum `off | daily | weekly | monthly` (`VARCHAR(16) NOT NULL DEFAULT 'off'`, `ExportFrequency` StrEnum) — `off` is a value, never NULL
- `export_hour SMALLINT NOT NULL DEFAULT 3` (0–23), `export_weekday SMALLINT NOT NULL DEFAULT 0` (0–6, **0 = Monday**, weekly only), `export_day_of_month SMALLINT NOT NULL DEFAULT 1` (1–28, monthly only), `export_keep_last_n INTEGER NOT NULL DEFAULT 5` (1–20 local retention) — all in the **local time of the machine running the tab**
- CHECKs `ck_users_export_frequency` / `_hour` / `_weekday` / `_day_of_month` / `_keep_last_n`; `PUT /export/schedule` returns 422 `export_invalid_schedule` for the same ranges. The resulting archive is stored client-side (IndexedDB), never server-side — no table is added for it

M97 dashboard layout preset (docs/09 § Dashboard layout presets) — one column, no table:

- `dashboard_preset` enum-without-pg-enum `overview | import_first | budget_first` (`VARCHAR(16) NOT NULL DEFAULT 'overview'`, `DashboardPreset` StrEnum; CHECK `ck_users_dashboard_preset`, migration `0036_dashboard_preset`) — which of three FIXED orders the dashboard renders its existing cards in. `overview` is the first-visit default because the G1 tour teaches on that layout. It stores an ORDER only: no card is created, removed, hidden or configured, which is why there is no per-card state and no new table (docs/10 §M97 rejected scope).

M108 P2 / M123 saved VIEWS (docs/09 §Saved report views, §Create from search) — ONE `JSON` column, no table:

- `saved_report_views JSON NOT NULL DEFAULT '[]'` (migration `0038_saved_report_views`) — the user's saved views: M108's report presets, and since M123 the Transactions search views too. `[]` is the real "none saved" shape, never NULL, for the same absent-vs-empty reason as `dashboard_preset = overview` and `backup_frequency = off`. **Per-USER, never per-account**: a report preset stores a date range, a granularity, a chart variant, a year selector and a sort; a transaction view stores the four URL-addressable search params. Neither carries account scope, so there is no per-account variant to drift.
- Each element is `{id, title, tab, kind, state}` and is validated on WRITE by `app/schemas/users.py::SavedReportView` (not by a CHECK constraint — the tab set is an application contract shared with `frontend/src/lib/reportTabs.ts`):
  - `id`: client-generated token, **8–64** chars of `[A-Za-z0-9_-]`, unique within the list — rename and delete address a preset by it (the floor keeps a stub id from being a usable handle).
  - `title`: 1–80 chars, non-blank after trimming (whitespace-only is a 422, so two look-alike empty names cannot accumulate).
  - `kind`: `reports | transactions`, **default `reports`** — so every row written before M123 still validates unchanged, and reads always echo it (a client never infers the contract from `tab`).
  - `tab`: the restore target, CROSS-CHECKED against `kind` — one of the seven `REPORT_TABS` ids (`spending | categories | cashflow | networth | performance | dividends | projections`) when `kind=reports`, exactly `transactions` when `kind=transactions`. Claiming one kind with the other's `tab` is a 422.
  - `state`: **closed key set, selected by `kind`** — an unknown key is a 422, so a stale client cannot store a field nothing restores. For `kind=reports`: `from`, `to`, `granularity`, `view`, `year`, `sort_key`, `sort_desc`, `group_by` — `from`/`to` must be `YYYY-MM-DD`; `granularity` ∈ `day | week | month | year` (docs/12); `view` matches `^[a-z][a-z0-9_-]{0,31}$`; `year` is an integer in 1900–2200 (`true` is rejected — it is an `int` subclass); `sort_key` matches `^[a-z_]{1,32}$`; `sort_desc` is a boolean; `group_by` ∈ `category | tag | owner` (M108 P3 + M124, docs/12 §Owner dimension / §Tag dimension). For `kind=transactions`: `q`, `category`, `account`, `tag` — the four URL-addressable `/transactions` params verbatim (docs/09 §Create from search), so a view IS a bookmark: `q` non-blank ≤ 240 chars; `category` a digits id or the literal `uncategorized`; `account` a digits id; `tag` a comma list of digits ids. Every value non-blank, at most 8 keys per `state` (a transaction view uses at most 4).
  - List cap: **50 presets** (`SAVED_VIEW_MAX_COUNT`), duplicate `id` → 422.
- Write path: `PATCH /users/me {saved_report_views: [...]}` — the WHOLE list in one write; rename and delete are client-side list edits, so there is no per-preset CRUD. Like every `users` write it is `403 demo_account_restricted` on the shared demo tenant.

M109 saved what-if scenarios (docs/45 §2) — the SAME reasoning, one `JSON` column:

- `forecast_scenarios JSON NOT NULL DEFAULT '[]'` (migration `0042_forecast_scenarios`) — the user's saved forecast scenarios. `[]` is again the real "none saved" shape, never NULL, and it is per-USER only: a scenario is a decision the owner makes about their own money, never an account-scoped thing.
- Each element is `{id, name, events[]}`:
  - `id`: client-generated token, **8–64** chars of `[A-Za-z0-9_-]`, unique within the list — copy, rename and delete address a scenario by it. Server-minted on `POST /forecast/scenarios/{id}/copy` when the client omits one (`secrets.token_urlsafe(12)`).
  - `name`: 1–80 chars, non-blank after trimming.
  - `events`: **0–12** entries, each `{id, type, …}` where `type` is one of exactly five v1 shapes — `house_purchase`, `career_break`, `salary_change`, `one_off_cash_event`, `recurring_cost_delta`. Validated on WRITE by `app/schemas/forecast.py` (a discriminated union with `extra="forbid"`, NOT a CHECK constraint — the set is an application contract shared with `frontend/src/api/forecast.ts`, and an unknown `type` is the deliberate 422 `unknown_event_type`).
  - List cap: **25 scenarios** per tenant (`MAX_SCENARIOS_PER_USER`) → 409 `scenario_limit_reached`.
- Write path: dedicated per-scenario CRUD (`POST/GET/PATCH/DELETE /forecast/scenarios[/{id}]` + `POST …/copy`), unlike `saved_report_views`' whole-list PATCH — a scenario is a document with a lifecycle, not a view state. Still `403 demo_account_restricted` on the demo tenant, because the guard is on `get_current_user` and every route depends on it.
- E2EE/export: no cipher column exists or is claimed (docs/33 inventory unchanged); it is ordinary server-readable plaintext user content and travels inside `users.csv` automatically, because the export's table set is DERIVED from `app.models` (docs/11 §File list).

M174 recurring-intent tags (docs/09 §Tags that mark recurring, docs/36 §M174) — a THIRD `JSON` column under the same reasoning:

- `recurring_intent_tag_ids JSON NOT NULL DEFAULT '[]'` (migration `0053_recurring_intent_tags`) — which of the user's OWN tags mean "this is a recurring commitment". `[]` is the real "the feature is off" shape, never NULL, matching `dashboard_preset = overview` / `saved_report_views = []` / `backup_frequency = off`. Per-USER, so it roams across devices with the rest of the row.
- Each element is a `tags.id`. **No FK and no CHECK**: the values live in a JSON document, so the database cannot express the reference. Ownership is enforced at the write seam instead — `PATCH /users/me` answers **422 `invalid_recurring_intent_tag_ids`** for any id the caller does not own, and the schema rejects non-positive ints as `validation_error` before that. List cap **20** (`RECURRING_INTENT_MAX_TAGS`): the confirmed-subscription query reads this list on every cash-flow calendar call.
- **No magic tag names.** Nothing anywhere pattern-matches a tag's NAME (`/sub|recurring|monthly|bill/i` would be a second, invisible normalizer — the user could only discover its verdict by seeing a wrong calendar). The tag source exists exactly when this list is non-empty.
- Read path: `GET /users/me` always carries the field (`[]` when off), so a client never infers it from the calendar's silence.
- Write path: `PATCH /users/me {recurring_intent_tag_ids: [...]}` — the WHOLE list in one write, like `saved_report_views`; today's only writer is Settings → Tags that mark recurring. Like every `users` write it is `403 demo_account_restricted` on the shared demo tenant.
- E2EE/export: no cipher column exists or is claimed (docs/33 inventory unchanged) and it travels inside `users.csv` with the rest of the row.

### refresh_tokens
`id PK`, `user_id FK`, `token_hash` (SHA-256 hex of an opaque 32-byte token, unique), `expires_at`, `revoked_at nullable`. Issued at register/login, **rotated on every** `POST /auth/refresh` (presented row revoked), revoked at logout. Reuse detection (security-hardening): presenting an ALREADY-REVOKED token revokes every unrevoked row for that user before the 401 — a replayed chain kills all its sessions. Never exported (M7 plan).

### mcp_tokens
`id PK`, `user_id FK`, `name` (VARCHAR 100), `token_hash` (SHA-256 hex, unique, indexed), `policy` enum-without-pg-enum `full | read_only` (`VARCHAR(16) NOT NULL DEFAULT 'full'`, `McpTokenPolicy` StrEnum, CHECK `ck_mcp_tokens_policy`), `expires_at`, `last_used_at`. Opaque bearer tokens for MCP server auth — maps token to user. Created via `POST /auth/mcp-tokens` (token shown once; demo tenant is forced to `read_only`, M81 docs/40 §1); validated by MCP server middleware via `POST /auth/validate-mcp-token`; revoked via `DELETE /auth/mcp-tokens/{id}`. Migration 0015; migration 0026 added **`expires_at` = created_at + 90 days** (backfilled for existing rows) and **`last_used_at`** (stamped on each validation): expired → `401 mcp_token_expired`, at most **10 active** per user → `400 mcp_token_cap`, creation **10/min** → `429 rate_limited` (audit M12). Migration 0034 added **`policy`** (M80, docs/40): `read_only` sessions get the write tools filtered out of `tools/list` and refused at the MCP layer (`403 mcp_read_only`) — the policy is read back through the validate response (`{policy, token_id, is_demo}`), never re-derived anywhere else.

### mcp_audit
`id PK`, `user_id FK` (indexed), `mcp_token_id INT NULL` (**no FK** — revoking a token must not delete its receipts; NULL = the call arrived without a token), `tool` (VARCHAR 100), `args_hash` (VARCHAR 64, sha256 hex of the canonical MCP arguments JSON), `response_code INT`, `duration_ms INT`, `created_at`/`updated_at` (`updated_at` is schema bookkeeping only — rows are append-only). One receipt per `tools/call` the MCP server handled, **including the ones it refused** (M80, docs/40 §3): written by the MCP server through `POST /auth/internal-mcp-audit` (best effort — a failed post is logged and swallowed), read back via `GET /mcp-audit` (own rows only, newest first). Arguments are hashed, never stored: a login call carries a password. Index `ix_mcp_audit_user_created (user_id, created_at DESC)`. Tenant data with no secrets: exported with everything else, wiped by a deleted account and by the demo reset (it joins `_USER_OWNED_MODELS`). Migration 0034.

### totp_factors
`id PK`, `user_id FK` UNIQUE (`uq_totp_factors_user` — one factor per user), `secret TEXT NULL`, `pending_secret TEXT NULL`, `last_used_step BIGINT NULL` (M74 — docs/38). Both secrets are **Fernet ciphertext** (`encrypt_secret`, key derived from `JWT_SECRET`, same treatment as `users.llm_api_key`), stored as base32: `secret` = the ACTIVE factor, `pending_secret` = the one awaiting `POST /auth/totp/confirm`. `enabled ⇔ secret IS NOT NULL`, `pending ⇔ pending_secret IS NOT NULL`; setup only ever writes `pending_secret`, so re-enrollment cannot take the account's live factor offline. `last_used_step` is the highest accepted LOGIN timestep — the replay guard (a code at step `<= last_used_step` is rejected `401 invalid_totp_code`). It is reset to NULL on every `confirm`, because it belongs to the secret that was active at the time.

### recovery_codes
`id PK`, `user_id FK` (indexed), `code_hash` (SHA-256 hex of the normalized 10-char code), `used_at TIMESTAMP NULL`. `UNIQUE (user_id, code_hash)` (`uq_recovery_codes_user_hash`). Exactly 10 live rows after each `POST /auth/totp/confirm` (previous rows deleted first); consumed by `POST /auth/login/recovery` and `POST /auth/totp/disable` through a conditional `UPDATE … WHERE used_at IS NULL` — the database picks the winner, so one code can never be spent twice.

### webauthn_credentials
`id PK`, `user_id FK` (indexed), `credential_id BYTEA UNIQUE` (`uq_webauthn_credentials_credential_id`), `public_key BYTEA` (COSE key as verified), `sign_count BIGINT` (monotonic assertion counter), `transports TEXT NULL` (comma-joined hint), `name TEXT` (user label, default `Passkey`), `last_used_at TIMESTAMP NULL`. At most **10** per user (`400 passkey_cap`); a sign-count regression (`0 < new <= stored`) is rejected `401 invalid_passkey` and logged as a clone signal.

### oidc_links
`id PK`, `user_id FK` (indexed), `issuer TEXT`, `subject TEXT`, `UNIQUE (issuer, subject)` (`uq_oidc_links_issuer_subject`). Binds one provider identity to exactly one local account; created only after the explicit confirmation at `POST /auth/oidc/confirm`. Email is the MATCHING key for creating the link (exact match against `users.email`), never the identity — and an SSO login can never CREATE a user (link-only, docs/38).

### institutions
`id PK`, `user_id FK`, `name`, `importer_profile JSON` (column-mapping profile for CSV; see docs/03).

### accounts
`id PK`, `user_id FK`, `institution_id FK nullable`, `name`, `type` enum:
`chequing | savings | credit_card | line_of_credit | brokerage_non_registered | rrsp | tfsa | fhsa | resp | lira | rdsp`,
`currency`, `taxable: bool` (true for `brokerage_non_registered`, false for registered plans incl. `lira`/`rdsp`; locked-in withdrawal rules are out of scope), `llm_autopilot: bool = false` (drives the high-confidence autopilot gate in docs/04; exposed through the standard accounts PATCH), `starting_balance: Decimal(18,2) nullable` (balance before earliest imported transaction; NULL treated as 0.00; `cash_balance` in API responses = committed LEDGER transactions (**split parents excluded** since M107 — their money lives in their children, docs/44 §2) + starting_balance — except **investment** accounts, which since M145 report the docs/05 §Portfolio value trade-chain cash the value chart draws, because a trade row IS an investment account's money and the ledger alone draws a false $0 (see docs/08 §cash_balance); migration 0009).
| The API's `AccountPublic` also returns two derived fields: `investment_value` (market value of held securities in the account's native currency) and `total_value` (`cash_balance` + `investment_value`). `cash_balance` / `starting_balance` are as above (`backend/app/api/accounts.py:_build_public`).
`last_sync_at TIMESTAMPTZ nullable` and `provider_status enum(ok | degraded | auth_required) nullable`
(migration 0033, docs/39 § status-page groundwork): NULL `provider_status` means "not linked to a live
bank-sync agreement" — a state, not missing data. `last_sync_at` survives a revoke as history.
**M118 adds no schema.** `GET /accounts/{id}/value-history` (docs/08 §Account value history) is
derived on every request from `transactions` + `trades` + `prices` + `fx_rates` through docs/05
§Portfolio value's chain, scoped to the one account: no table, column, index or cache row, and its
event markers are re-derived from the committed rows rather than stored — the same
derived-not-copied rule docs/25 pins for bill match rules.

### categories
`id PK`, `user_id FK`, `name`, `parent_id FK → categories` (self-referencing hierarchy), `is_system: bool`.
Seeded defaults: groceries, dining, housing, transport, utilities, health, entertainment, income, transfers, uncategorized.
Seeding runs inside the register transaction — each user gets an owned copy; `is_system` rows may be renamed but not deleted.

### transactions
The unified ledger row for all account types.
`id PK`, `user_id FK`, `account_id FK`, `date`, `posting_date nullable`, `raw_description`, `normalized_payee`, `amount_native Numeric(18,2)`, `currency`, `cad_amount Numeric(18,2)`, `category_id FK nullable`, `import_batch_id FK nullable`, `dedup_hash: unique per account`, `is_duplicate bool`, `force_include bool`, `cleared VARCHAR(16) nullable` (YNAB's `uncleared`/`cleared`/`reconciled` preserved on import — NULL means the source carried no clearing state, NEVER "uncleared"; migration 0033, docs/38), `transfer_pair_id FK nullable → transactions` (self-referential; links two transactions as a transfer pair; migration 0008). `is_duplicate` / `force_include` drive the manual-duplicate-include flow (docs/03): staged duplicates carry a salted `dedup_hash` and `is_duplicate=true`; `force_include` is set at commit for rows the user chose to keep.
Recurring tagging (M96, docs/09 §Recurring prompt): `is_recurring bool default false` (the flag the badge reads), `recurring_cadence enum(weekly | biweekly | four_weekly | monthly) nullable` (`native_enum=False`, length 16 — the cadence the passive detector fitted; NULL until the user accepts a candidate group), `recurring_next_date DATE nullable` (the group's NEXT expected occurrence, stored identically on every tagged row of that group — one number to read, not one per historical row). Migration 0037. Detection itself writes nothing: a decline lands in `recurring_suppressions` instead.
`cad_amount` fills at ingest time (import or manual create) from the currency→CAD `fx_rates` row of the transaction's date; if no rate resolves (fetch down, no manual entry) it stays NULL and the UI flags it unresolved — never silently backfilled.
Fuzzy search (M12): `normalized_payee` carries a GIN trigram index (`pg_trgm` extension, migration 0004). As of docs/33 S1+S5 no endpoint scores transaction payee text anymore: `GET /transactions/similar` is removed, `GET /search` resolves transactions by exact `payee_token` only, and `GET /transactions?q=` is removed (422 `q_removed`; the search view ranks in the browser via `lib/searchMatch`). The index still serves the one remaining server-side scorer — `services/categorization.py` picks few-shot examples with `_payee_score` for the LLM pipeline — plus its historical migrations. The `/search` metadata groups trigram-rank their own columns (categories/accounts/securities/tags), not this one.
Review state (M106): `reviewed_at TIMESTAMPTZ nullable`, `reviewed_by INT nullable` (**no FK** — the `mcp_audit.mcp_token_id` rule: deleting the reviewer's account must not blank the record of who signed off). **NULL `reviewed_at` IS "unreviewed"** — there is deliberately no companion boolean, because two columns that both mean "reviewed" eventually disagree. Import batches therefore seed review = FALSE for free: `services/import_staging.py` writes NEITHER column, so every import leaves a queue behind (`stage_rows` sets both to `None` explicitly, so a future column default cannot quietly pre-review a stranger's statement). Written ONLY by `POST /transactions/{id}/review` (docs/08) — flipping to the state a row already has is a no-op and writes no receipt. Partial index `ix_transactions_user_unreviewed (user_id) WHERE reviewed_at IS NULL`, so the queue count is an index probe rather than a scan of the tenant's ledger. Rules carry no review semantics (docs/04) and M106 adds none: this is manual review state only.

Split transactions (M107 — docs/44 §1, migration `0042_split_transactions`): **four columns on this same table, no side table** (D1 — every existing reader stays one predicate away from correctness).

- `is_split BOOLEAN NOT NULL DEFAULT false` — true **iff** this row has ≥1 child. Denormalized so every LEDGER predicate is one boolean test instead of an anti-join. Near-constant-false, so it deliberately gets **no index of its own**: `(user_id, date)` and `(user_id, account_id, date)` already answer every scan.
- `parent_split_id INT NULL` — **children only**. Self-FK `fk_transactions_parent_split → transactions.id`, `ON DELETE CASCADE` + `DEFERRABLE INITIALLY DEFERRED` (ERD note above; docs/11 §Restore — the children-first snapshot test).
- `split_checksum Numeric(18,2) NULL` — **parents only**: the exact amount the children must sum to (set to `amount_native` at split time, cleared at merge). The D3 sum invariant made checkable by a constraint rather than by a test alone.
- `note VARCHAR(500) NULL` — free text on a row. The v1 UI exposes it on children and in the split editor only; the API accepts it on any row. **Plaintext**, like `raw_description` — listed in docs/33 §Server-readable plaintext, never in a cipher column.

Two named CHECKs pin the shape a row may hold: `ck_transactions_split_flag` (`is_split = false OR (parent_split_id IS NULL AND split_checksum IS NOT NULL)` — a row is never both parent and child, and a parent always carries its checksum) and `ck_transactions_split_checksum` (`is_split = false OR split_checksum = amount_native` — while split, the parent's amount cannot drift away from the checksum). One named index: `ix_transactions_parent_split_id (parent_split_id)` — the group fetch, the cascade and the "does this row have children" probe.

Two population rules live in `services/txn_filters.py` and are reused by every reader (docs/44 §2): **LEDGER** = `committed_tx_conditions` + `is_split = false` (money/category is counted → split parents out, children in) and **IDENTITY** = `committed_tx_conditions` + `parent_split_id IS NULL` (a statement line is identified/matched/re-imported → children out, split parents in). `committed_tx_conditions` itself is deliberately unchanged — the export snapshot's visibility filter must keep seeing *every* row or a restore would lose children (docs/11, docs/03).

Child rows copy the parent's **identity** (`user_id`, `account_id`, `date`, `posting_date`, `currency`, `raw_description`, `normalized_payee`, `payee_cipher`, `raw_desc_cipher`, `payee_token`, `import_batch_id`, `cleared`) and own their `amount_native`, `category_id`, `note`, tags and `cad_amount` (allocated from the parent's cached value, never re-derived from FX — docs/44 §3.3). Children also copy the parent's **`reviewed_at` / `reviewed_by`**: the statement line was reviewed and its parts are the same money under different buckets, so defaulting them to NULL would silently re-queue a row the user already signed off on — re-queueing is not a thing a split does. Remaining child fields: `dedup_hash` is salted (`"<canonical>:<uuid4 hex>"`, so no matcher can ever reach a part — docs/03), `is_duplicate=false` / `force_include=false`, `is_recurring=false` with NULL cadence/next date (the badge stays on the parent), `transfer_pair_id=NULL`, `split_checksum=NULL` and `is_split=false` (a child can never itself be split → `422 split_nested`).

Date-field rule (pinned 2026-09-03 after the subscriptions-500 incident): `date` (booking date, non-nullable) is the single field that gates period membership everywhere — reports engine, `_budget_spent`, `networth_snapshot`, subscription gap math. `posting_date` (bank posting date, nullable) is display/metadata only: consumers must fall back to `date` when it is NULL and must never put a `posting_date` predicate in a WHERE clause over an outer join (NULL rows vanish and empty accounts drop out). Subscription detection uses effective date `posting_date ?? date`.

### securities
`id PK`, `user_id FK`, `symbol`, `name`, `kind` enum `stock | etf | mutual_fund`, `listing_currency`, `exchange`. Unique `(user_id, symbol, exchange)`.

**Where `listing_currency` comes from (M163, docs/07 §Listing currency):** the
provider's own `currency` field on the `Ticker.info` body that resolved the
listing — a well-formed ISO-4217 code, so `005930.KS` stores `KRW` and
`9984.T` `JPY`. The `.TO`/Canadian-venue heuristic (`market_data._listing_currency`)
is the FALLBACK for payloads that carry no such field (every search row) or
whose field is unusable. It is spent by every FX conversion of the row's
prices and trades (docs/06 pool math, docs/12 valuation, docs/15 exposure),
so a wrong label is a wrong valuation, never a cosmetic defect.

**A listing whose provider quotes it in a MINOR unit is never adopted
(M167, docs/07 §A minor-unit listing is refused):** a `Ticker.info` body
whose own `currency` is three letters in MIXED case (`GBp` = pence, the LSE
minor unit) is a MISS — `resolve_symbol` answers `none`, the name-search
fallback drops a UK row entirely (a search row carries no currency and
both UK venues are ambiguous — LSE answers `GBp` and `USD`, Cboe UK
(`CXE`) answers `GBp`), and the
deepening loop creates no row. A London listing the provider quotes in USD
still adopts normally. Such a row, if one is ever hand-created, carries the
M143 UNRESOLVED marker (`listing_currency = ''` + `dead_symbol`) — the
state `POST /securities` itself writes when a lookup finds nothing.

**Dead-symbol bookkeeping (M141, migration `0048_securities_dead_symbol`, docs/07 §Dead symbols):** three more columns —
`quote_miss_count INT NOT NULL DEFAULT 0`, `dead_symbol BOOLEAN NOT NULL DEFAULT FALSE`,
`dead_symbol_reason TEXT NULL`. The first two are the automatic fan-out's stop
signal; the third is the last hard "quote not found" verdict verbatim, which the
UI shows instead of a forever-"yet" promise. The counter advances ONLY on a hard
`QuoteOutcome.not_found` — a rate-limit, a timeout or a cached read leaves it
alone, so `3` really does mean "Yahoo said no, three times". Any successful fetch
resets the counter and clears the flag (auto-clear; no manual action, no repair
script). The migration backfills nothing: a pre-existing row reads `0 / FALSE /
NULL` and keeps being fetched exactly as before.

**A data migration must not SELECT this table through the ORM.** Migration
`0045_merge_dup_securities` runs `services/security_dedup.repair_securities`
at a point in the chain where later columns do not exist yet, so that service
reads a FROZEN four-column list (`security_dedup._SEC_KEYS`) and issues its
writes as Core statements. A full-entity `select(Security)` there would break
`alembic upgrade head` from scratch the moment anyone adds a column — M141 hit
exactly that, and the fix is what keeps the next column from doing it again.

**Both key parts are normalized on every write, without exception:** `symbol` and `exchange` are stored `strip().upper()`. Enforced at `POST /securities`, `PATCH /securities/{id}`, `_cash_security`, the sub-fund creator, and — since M117 — `_resolve_security` / `_lookup_security` on the investment-CSV import, which used to match and store the raw cell. That asymmetry let a CSV reading `veqt` sit beside the `VEQT` row created through the API: two rows for one listing, each with its own trades, prices and ACB pool, so valuation picked whichever twin the query reached first. A distinct `exchange` is still a distinct security (`VT`/TSX ≠ `VT`/NYSE) — normalization is about case and whitespace, not about collapsing listings.

**One venue gets ONE spelling (M164, docs/07 §Venue aliases).** `exchange` is a venue CODE, and the provider is not consistent about which code it sends for one venue: live 2026-10-11 `600900.SS` answered `SHH`/"Shanghai" while prod row 2894 `603986.SS` stores `SHSE`, and a bare `7182` answers `TOKY1` while `7182.T` answers `JPX` (the same Japan Post Bank listing). `services/venue_codes.py` owns the alias table (`VENUE_ALIASES`, alias → canonical) and `canonical_exchange()`; `market_data._normalize_exchange` writes the canonical code for every PROVIDER payload, so a second spelling cannot enter the table that way. A USER-typed code is still stored exactly as typed — this column records the user's statement of venue as well as the provider's, and the repair below is what folds a twin if one appears. Codes the table does not know pass through untouched: `SHOP`/NYSE and `SHOP`/TSX stay two listings, because the rule is per PAIR of venue codes and never per symbol.

`migration 0045_merge_dup_securities` runs `app.services.security_dedup.repair_securities` on deploy: groups rows whose *normalized* `(user_id, symbol, exchange)` collides, keeps the row with the most price history (tie → lowest id), repoints `trades` / `prices` / `watchlist` / `fundamentals` / `etf_holdings` onto it (the keeper's copy wins any contested slot), deletes the losers, then normalizes the survivors. A group whose `acb_pools` would collide on `(account_id, security_id)` is **refused and reported**, not merged — two pools for one account+security mean cost basis was computed twice and choosing a winner would corrupt ACB (docs/06); its rows stay un-normalized too, since rewriting them would violate the unique key. Idempotent: a second run reports `merged_groups=0`. Tests: `backend/tests/test_security_dedup.py`.

Since M164 the grouping key's venue part is `venue_codes.canonical_exchange` — the venue-alias rule above — so rows that spell one venue two ways are one group, and a survivor of such a group is REWRITTEN to the canonical code (`report.canonicalized`) so the next provider answer collides instead of duplicating. Migration `0049_merge_venue_aliases` re-runs this repair (data only, no schema) to fold the twins migration 0045 could not see; it prints the merge summary to `alembic upgrade`'s stdout, exactly like 0045. Tests: `backend/tests/test_m163_venue_aliases.py`.

### trades
Linked to an account + security.
`id PK`, `user_id FK`, `account_id FK`, `security_id FK`, `action` enum:
`buy | sell | dividend | distribution | return_of_capital | drip | fee | contribution | withdrawal | transfer_in | transfer_out | corporate_action_adjustment`,
`date`, `quantity Numeric(18,6)`, `price_native Numeric(20,8)`, `commission Numeric(18,2)`, `currency VARCHAR(3) NULL` (M132), `fx_rate_used Numeric(20,8)`.
Import/staging columns (see docs/03): `dedup_hash VARCHAR indexed`, `is_duplicate bool`, `force_include bool`, `import_batch_id FK → import_batches` (NULL for manually entered trades) — the investment-CSV import reuses the same dedup/force-include flow as transactions. `trade.dedup_hash` carries **no** UNIQUE constraint (salted for staged duplicates, kept distinct).

Input conventions: for `dividend | distribution | return_of_capital | fee | contribution | withdrawal | transfer_in | transfer_out`, `quantity=0` and `price_native` holds the gross cash amount in native currency; `commission` applies only to `buy | sell | drip`. `corporate_action_adjustment` semantics are defined in docs/06. `fx_rate_used` is a display copy captured at entry — pool math NEVER reads it (docs/06 FX precedence).

**Per-row currency** (M132, pinned): `trades.currency` is the currency
`price_native` and `commission` are STATED in — the broker's own per-row code,
captured by the importer from the file's currency cell (blank cell → the target
ACCOUNT's currency, never a hard-coded `USD`), and by `POST /trades` as the
ACCOUNT's currency (the create form carries no currency field). A `CHECK`
`ck_trades_currency_iso_code` keeps "an ISO-4217 code" true in the table
(`currency IS NULL OR currency ~ '^[A-Z]{3}$'`), not only at the API seam.
`NULL` is a real state, not a missing default: a pre-M132 row reads in its
ACCOUNT's currency — byte-identical to the behaviour before the column existed.
The migration backfills NOTHING, because `securities.listing_currency` is NOT a
sound proxy (real account 731's VIU rows are CAD while the security says USD);
rows gain their currency from a future import or from
`backend/scripts/backfill_trade_currency.py`, whose re-parse of the SOURCE FILE
is the authority. Consumers convert at it — docs/05 §Portfolio value pins which
day each conversion uses.

**Signed-quantity convention** (pinned): `buy` and `drip` store a POSITIVE `quantity`; `sell` stores a NEGATIVE `quantity` (the normalizer forces `sell → -abs(qty)`). Cash-only and corporate-action rows are stored as given. Valuation, positions, and ACB all rely on the negative-sell sign (docs/05, docs/06).

**Signed-amount convention for cash rows** (M131, pinned): a cash OUTFLOW (`fee | withdrawal`) stores `price_native` at its NEGATIVE native sign — money leaving — while a cash INFLOW (`dividend | distribution | return_of_capital | contribution`) stores the file's (positive) sign. The docs/05 cash-effect table turns that into cash with `-abs(price_native)` for outflows and `+price_native` for inflows, so the LEDGER is identical whether a legacy outflow row was stored positive or negative, while the stored number is honest on every surface that renders it directly (transactions list, exports, event log). A cash TRANSFER (`transfer_in | transfer_out`, docs/03 M113) is the one exception: it stores a MAGNITUDE because its direction already lives in the action. A `contribution` with a NEGATIVE `price_native` is a contradiction and is refused **422 `negative_contribution`** by every write path (the importer per row, `POST /trades`, `PATCH /trades/{id}`) — a withdrawal is the honest spelling of money leaving.

**Cash-reimbursement rows** (M132): a POSITIVE `AdministrativePayment /
TRANSFER_FEE_REIMBURSEMENT` ("CAD credited") is money **in**, so it is stored as
a cash `transfer_in` (`quantity=0`, `price_native` at the credit's own sign),
never as a `fee` — whose docs/05 effect is `-abs`, always money out. M131 mapped
the profile key to `fee` and stored the credit negative, which counted 145.75
ARRIVING as 145.75 LEAVING; on real account 731 that single rule was 583.00 of
the -582.73-vs-WS-0.27 cash gap (row-by-row, the only disagreement in 91). A
NEGATIVE reimbursement is money out and keeps the `fee` mapping.

### acb_pools
One per **non-registered** account + security: `id PK`, `user_id FK`, `account_id FK`, `security_id FK`, `quantity Numeric(18,6)`, `total_acb_cad Numeric(18,2)`, `updated_at`.

### watchlist
Securities tracked but not held (W11): `id PK`, `user_id FK`, `security_id FK`, timestamps; UNIQUE `(user_id, security_id)`. Prices/fx for watched securities flow through the same caches as held ones; refresh via `POST /portfolio/refresh` (docs/08).

### fx_rates
`id PK`, `base`, `quote` (e.g. USD/CAD), `rate Numeric(20,8)`, `date`, `source` (`boc_valet` or `manual`). Cached forever once fetched; see docs/07.

### import_batches
`id PK`, `user_id FK`, `account_id FK`, `institution`, `file_name`, `status` enum `pending_review | committed | discarded | reverted` (`reverted` is set by `POST /imports/{batch_id}/revert`, which deletes the batch's transactions **and** trades — `discard` deletes both legs too; see docs/03), summary counts (imported / duplicates_skipped / unresolved). `institution` is a free-text source label: `investment_csv` on the legacy Wealthsimple trade path, the **brokerage profile label** (`Questrade`, `Interactive Brokers`, …) when a M82 profile imported the file (docs/41), and the institution's name for bank CSV/OFX imports. Trade batches also carry `trades.import_batch_id`; see docs/03 for the committed-only read rule.

### rules
Categorization rules (docs/04): `id PK`, `user_id FK`, `match_type` enum `regex | exact_payee | contains`, `pattern`, `priority int`, `category_id FK`, `enabled: bool`. Ordered by priority, first match wins.
Priority sorts ascending (lower number wins, ties broken by `id`); evaluation stops at the first match.

### llm_suggestions
`id PK`, `user_id FK`, `transaction_id FK`, `proposed_category_id FK`, `confidence float`, `status` enum `suggested | accepted | rejected`, `model_used`.

### learned_payees
Learning-loop map (docs/04): `id PK`, `user_id FK`, `normalized_payee` (unique per user), `category_id FK`, `hit_count int default 1`. Written ONLY by the suggestion-accept path (upsert; repeats bump the count); consulted after the rules engine. No CRUD routes.

### prices
Price history per security/date — feeds position valuation and the net-worth trend (docs/05, docs/09): `id PK`, `user_id FK`, `security_id FK`, `date`, `price Numeric(20,8)`, `source` enum `manual | yfinance`, `fetched_at TIMESTAMPTZ nullable`. Unique `(security_id, date)`. Standard CRUD under `/prices` (docs/08). `fetched_at` is real provider-response arrival evidence, stamped only when `_fetch_one_history` returns and `_cache_closes` persists that provider result; cache-only reads never stamp it. Migration 0057 adds the nullable column with NO BACKFILL, so existing rows remain NULL (no evidence). Manual edits, demo seed rows and split-repair-derived rows are NULL. `price` is stored **as-traded** — unadjusted for stock splits, in the same frame as the ledger's own quantities — because Yahoo only serves split-adjusted closes; `_fetch_one_history` un-adjusts them before they land (docs/07 §Price frame), and `POST /securities/{id}/split-repair` re-derives a window that was cached before that seam existed.

### dividend_history and dividend_history_refresh (M184)
`dividend_history` stores only raw per-share yfinance events: `id PK`, `user_id FK`, `security_id FK`, `ex_date DATE`, `amount Numeric(20,8)` (positive, listing currency; CHECK `ck_dividend_history_amount_positive`), `provider_order INTEGER` (the provider response's exact order, including duplicate dates), `source VARCHAR(16)` (`yfinance` only; CHECK `ck_dividend_history_source`), timestamps; unique `uq_dividend_history_order` on `(user_id, security_id, source, provider_order)`. Index `ix_dividend_history_user_security_date` on `(user_id, security_id, ex_date)` supports tenant history reads. `dividend_history_refresh` stores `(user_id, security_id)` refresh state: `id PK`, nullable `last_success_at` and `last_attempt_at`, `state` enum `pending | refreshing | available | empty | unavailable` (`VARCHAR(24)` + CHECK `ck_dividend_history_refresh_state`), `consecutive_failures INT NOT NULL DEFAULT 0` (M186 bounded retry: consecutive provider failures since the last success; drives the exponential backoff window, reset to 0 by any successful refresh including a quote-verified empty), timestamps; unique `uq_dividend_history_refresh` on `(user_id, security_id)` and index `ix_dividend_history_refresh_pending` on `(state,last_attempt_at)` for worker recovery. `error_code VARCHAR(64) nullable` is one of `provider_unavailable`, `invalid_provider_payload`, `privacy_or_ownership`, `dead_symbol`, or `quote_not_found` (M186: a hard Yahoo quote miss — a verdict about the symbol, distinct from a network failure); successful refreshes clear it. Both tables are tenanted. Migrations `0055_dividend_history` and `0056_dividend_retry_backoff` are additive. Provider failure, including Yahoo's hard quote miss, retains last-good raw events and success time; that miss also advances the owning security's `quote_miss_count` through `record_quote_outcome`. A destructive `empty` success requires a positive quote probe, then replaces the stored event set with none. No computed suggestions, share balances, or financial totals are cached. Provider response order and same-date duplicates are retained for suggestion ordering/parity.

### fundamentals
Per-security research snapshot (M16): `id PK`, `user_id FK`, `security_id FK UNIQUE`, `trailing_eps Numeric(20,8) nullable`, `pe_ratio Numeric(20,8) nullable`, `market_cap Numeric(30,2) nullable`, `dividend_yield_pct Numeric(10,4) nullable`, `beta Numeric(10,4) nullable`, `sector VARCHAR(80) nullable`, `industry VARCHAR(120) nullable`, `country VARCHAR(80) nullable`, `fifty_two_week_high Numeric(20,8) nullable`, `fifty_two_week_low Numeric(20,8) nullable`, `source` enum `yfinance | manual`, `fetched_at date`, timestamps. One row per security. `source="manual"` wins permanently — the yfinance refresh path skips manual rows (docs/07); served through the overview payload and edited via `PUT /securities/{id}/fundamentals` (docs/08).

### etf_holdings
Per-fund composition rows (M17): `id PK`, `user_id FK`, `security_id FK` (the FUND), `holding_symbol VARCHAR(20) nullable`, `holding_name VARCHAR(160) NOT NULL`, `asset_class` enum `stock | bond | cash | commodity | crypto | other` NOT NULL DEFAULT other, `region VARCHAR(80) nullable`, `country VARCHAR(80) nullable`, `sector VARCHAR(80) nullable`, `weight_pct Numeric(7,4) CHECK (weight_pct > 0 AND weight_pct <= 100)` named `ck_etf_holdings_weight` (7 int digits so exactly 100.0000 is storable), `source` enum `yfinance | manual | issuer | vanguard` (M147: `issuer` = the fund's own full-holdings CSV; M147-followup: `vanguard` = full holdings from Vanguard's own first-party APIs — the US fund-profile JSON and the Canadian `gpx/graphql` gateway, docs/07 §Fund composition providers; the column is plain `VARCHAR(16)` — 0006 created it without a CHECK, so widening needs no constraint rebuild), timestamps. NO unique beyond id — the same underlying company may appear twice via different share classes. Manual-first: PUT replaces only `source="manual"` rows; a provider import replaces ALL non-manual rows together (`source != manual` — one provider's list supersedes the previous one, so weights never double-count across providers) (docs/05).
| `holding_security_id FK → securities` (nullable) — the linked security when a holding row resolves to another fund; drives the M19 recursive look-through exposure (added in migration 0007). When set, the exposure engine descends into that security's own `etf_holdings` (docs/05, docs/16).

### provider_fund_registry
**Operator-level coverage data (M168)** — the provider registries and the two static sector tables as ROWS instead of Python literals (docs/07 §Provider registry, docs/16 §Sector chain): `id PK`, `family` enum `vanguard_us | vanguard_ca | ishares_us | static_sector | fund_sector_mix` (`VARCHAR(32)`, enum-without-pg-enum), `ticker VARCHAR(32)`, `status` enum `verified | miss` (`VARCHAR(16) NOT NULL DEFAULT 'verified'`, CHECK `ck_provider_fund_registry_status`), `port_id VARCHAR(16) nullable`, `venue_suffix VARCHAR(8) nullable` (`.TO`/`.NE` — what a CA fund-leg ticker is stored WITH), `endpoint VARCHAR(255) nullable` (the iShares product stem), `sector VARCHAR(64) nullable`, `sector_mix JSON nullable` (fund → `{"Technology": "40.90"}` — decimal TEXT, because JSON has no Decimal and the value must round exactly as the old literal did), `miss_count INT NOT NULL DEFAULT 0`, `last_probe_at TIMESTAMPTZ nullable`, `verified_at TIMESTAMPTZ nullable`, `verified_by_run VARCHAR(64) nullable` (`seed:0050` / `discovery:<path>` / `cli:set` / `cli:verify`), timestamps. **UNIQUE `(family, ticker)`** (`uq_provider_fund_family_ticker`).

**No `user_id`** — issuer facts about the world, shared by every tenant, exactly like `fx_rates` (AGENTS §2's operator level): one probe writes one row and every tenant reads it, while `etf_holdings` stays strictly per-user. Not exported, not wiped by an account deletion, not touched by the demo reset.

`status='miss'` is the NEGATIVE cache: the issuer's own answer that there is no such fund, so the chain never probes it again (`miss_count`/`last_probe_at` record the verdict). A `verified` row's `verified_at` is what `fintrackerctl provider-funds reverify` reads to find rows the 180-day sweep should re-probe. Seeded by migration `0050_provider_fund_registry` with exactly the 132 rows the five code literals held (50 + 42 + 1 + 28 + 11); discovery writes everything after that.

### benchmark_prices

**The vs-Benchmark panel's index series (M173, docs/28 decision 5, docs/07 §Benchmark series):** `id PK`, `symbol VARCHAR(32)` (the BENCHMARK key — `SPY`/`XIU`/`XBB`/`XAW`, NOT the venue ticker), `date`, `close Numeric(20,8)`, `currency VARCHAR(3)` (the venue's quote currency: USD for SPY, CAD for the three TSX funds), `source` enum `yfinance | manual` (reuses `PriceSource`), timestamps. **UNIQUE `(symbol, date)`** (`uq_benchmark_prices_symbol_date`), plus `ix_benchmark_prices_symbol` for the per-symbol scans. Created by migration `0052_benchmark_prices`, which seeds **no** rows — closes come from the sweeper and the operator backfill, so no price is ever frozen into a migration.

**No `user_id`** — an index close on a date is universal, the same operator level as `fx_rates`. **Not a `securities` row, on purpose:** a benchmark is not held, and a security for `SPY` would put a fund in every tenant's picker that no trade explains.

`close` is the provider's **split-adjusted** close, not `prices`' as-traded one — a benchmark has no ledger quantities to match, and un-adjusting a price index would read a 2:1 split as a −50% return (docs/07 §Price frame vs §Benchmark series). Writes are insert-only. **No second index on `symbol`:** the unique constraint's LEADING column is `symbol`, so `WHERE symbol = ?` (and the per-symbol `ORDER BY date`) is already served — the same reasoning that keeps `securities` and `learned_payees` un-indexed above. Not exported and refused by a restore (`app/api/export.py` deletes it from the snapshot; `app/api/backup.py::RESTORE_FORBIDDEN_TABLES` names it), because accepting it would let one tenant's crafted payload write the closes every tenant's panel reports as the market's own answer.

### assistant_proposals (M188, docs/57)

`id PK`, `user_id FK` (indexed), `question` (VARCHAR 2000 — the ask), `answer` (TEXT — the model's reply, discarded with a refusal), `kind` enum-without-pg-enum `account.update | transaction.update | forecast_scenario.update` (`VARCHAR(32) NOT NULL`, `ProposalKind` StrEnum — the CLOSED registry of docs/57 §2; widening means editing docs/57 first, then this code constant, never a DB constraint), `target` (VARCHAR 64 — `"<prefix>:<id>"`, prefix checked against `kind` at write time), `target_label` (VARCHAR 255 — the human line the card and History render), `summary` (VARCHAR 2000), `before` JSON, `after` JSON (the changed fields only — complete values for exactly those keys, never a full-row dump), `status` enum-without-pg-enum `pending | applied | dismissed | undone` (`VARCHAR(16) NOT NULL`, `ProposalStatus` StrEnum), `created_at`, `applied_at`/`undone_at`/`dismissed_at` nullable, `updated_at`.

Lifecycle: one row per ask (`pending`), decided exactly once — `pending → applied | dismissed`, then `applied → undone` exactly once; any other transition is `409 assistant_proposal_not_pending` / `409 assistant_proposal_not_undoable`. **No DB CHECK on `kind` or `status`**: both are application contracts (like `forecast_scenarios`' event types), so a registry change is a code+docs change, not a constraint rebuild. The row stores the ask/answer and the delta the user was shown — nothing in it is re-parsed into ORM values by a client (`before`/`after` are for rendering, docs/57 §2). `pending` rows are the open list (`GET /assistant/proposals`), `applied`/`undone` rows are History. Tenant data with no secrets: exported with everything else, wiped by account deletion and the demo reset (joins `_USER_OWNED_MODELS`, audit first for cascade order), RLS-secured by its creating migration (`secure_registered_table`, docs/55). Indexes `ix_assistant_proposals_user_id (user_id)` and `ix_assistant_proposals_user_created (user_id, created_at DESC)` (the open-list/History ordering). Migration `0059_assistant`.

### assistant_audit (M188, docs/57)

`id PK`, `user_id FK` (indexed), `proposal_id FK → assistant_proposals.id` (**no cascade delete from the proposal side is relied on for reads** — the row is the durable receipt), `action` enum-without-pg-enum `apply | undo` (`VARCHAR(8) NOT NULL`, `AuditAction` StrEnum), `kind`, `target` (denormalized from the proposal so an audit row still names its change if the proposal row ever goes), `before` JSON, `after` JSON (the COMPLETE payload for that direction: apply = the pre-apply state → the applied state; undo = the applied state → the restored state), `created_at`, `updated_at` (schema bookkeeping only — **append-only**, no UPDATE/DELETE path exists in `app/`).

Exactly one row per mutation, written in the SAME transaction as the domain write it logs — a mutation that cannot be logged does not happen (docs/57 §4). `dismiss` writes no audit row (nothing was mutated). Reads: the History list resolves each item's `apply_audit_id` / `undo_audit_id` from this table. Tenant data with no secrets: exported, wiped with the account (joins `_USER_OWNED_MODELS` **before** `assistant_proposals` so the cascade order is right), RLS-secured by migration `0059_assistant` (`secure_registered_table`). Indexes `ix_assistant_audit_user_id (user_id)`, `ix_assistant_audit_proposal_id (proposal_id)` (the History audit-id lookup) and `ix_assistant_audit_user_created (user_id, created_at DESC)`.

## Indexes

### Additional tables (M25–M44)

#### budgets
Per-category monthly/yearly spending limits (M25/M37). `id PK`, `user_id FK`, `category_id FK`, `amount Decimal(12,2)`, `period str` (`monthly | yearly`), `carry str` (`off | forward`, **default `off`** — non-native enum per AGENTS §2, `VARCHAR(8) NOT NULL DEFAULT 'off'` + CHECK `ck_budgets_carry`, migration `0040_budget_carry`, M110 docs/09 §Budget carry). UNIQUE `(user_id, category_id)`. `amount` stays the ASSIGNED figure; a rollover's `carried_in` never lives in this column (it has its own field and its own table, below).

#### budget_carryovers
The audit ledger behind M110 rollover: the `carried_in` a budget carried INTO one period, written ONCE when that period is first read after the boundary and never recomputed (docs/09 §Budget carry — a back-view returns the value as it was, even if the source period is edited later). `id PK`, `user_id FK`, `budget_id FK`, `period_start date` (first day of the TARGET period), `amount Decimal(12,2)` **signed** (a deficit carries as a debt). UNIQUE `(budget_id, period_start)` — one frozen row per budget per period. Rows are child-of-`budgets`: `DELETE /budgets/{id}` removes them, and `_USER_OWNED_MODELS` deletes them before the budget (docs/20 cascade).

#### goals
Financial savings targets with progress tracking (M33/M36). `id PK`, `user_id FK`, `name`, `target_amount Decimal(12,2)`, `target_date YYYY-MM-DD nullable`, `account_id FK nullable` (optional link to an account for auto-tracking), `current_amount_override Decimal(12,2) nullable` (manual override). Progress is computed server-side: `current_amount` from override → account balance → 0; `status` is `on_track | behind | ahead | completed`.

#### bill_reminders
Upcoming bill tracking with due dates (M40) **plus its match rule (M116, migration `0044_bill_match_rules`)**. `id PK`, `user_id FK`, `name`, `amount Decimal(12,2) nullable`, `category_id FK nullable`, `frequency str` (`monthly | quarterly | yearly | one_time`), `next_due_date YYYY-MM-DD`, `remind_days_before int default 3`, `enabled bool default true`.

**M180 column (migration `0054_bill_due_anchor`):** `anchor_day INTEGER nullable` — the day-of-month the schedule REPEATS on, so month-end clamping is a property of the projection instead of the stored date (docs/25 §M180 D1): `Jan 31 → Feb 28 → Mar 31`, and a Feb-29 yearly bill returns to Feb 29 in the next leap year. `NULL` = un-anchored — "the stored date's own day is the anchor", exactly the pre-M180 behaviour, which is what a row written outside the API (a fixture, a seed, a hand-edited row) keeps. CHECK `ck_bill_reminders_anchor_day` (`anchor_day IS NULL OR anchor_day BETWEEN 1 AND 31`) puts the bound in the TABLE. `next_due_date` itself is UNCHANGED — still the clamped, always-real date the client renders — and no index is added (nothing queries `anchor_day` alone). The recurrence rule lives in `services/bill_schedule.py` (docs/25 §M180 D2).

M116 columns: `match_mode str(16)` **NOT NULL, server default `'contains'`** (`contains | starts_with | exact`, stored non-natively per AGENTS §2), `match_term VARCHAR(120)` **NOT NULL** (backfilled from `left(name, 120)` by 0044, and derived from `name` on every `POST /bills` that omits it) and `amount_tolerance NUMERIC(12,2) nullable` (**`NULL` = the bill's `amount` must match exactly**). CHECK constraints `ck_bill_reminders_match_term_len` (`length(match_term) BETWEEN 1 AND 120`) and `ck_bill_reminders_tolerance_nonneg` (`amount_tolerance IS NULL OR >= 0`) keep both bounds true in the TABLE, not only at the API seam.

M121 column (migration `0046_multi_term_match`): `match_logic str(16)` **NOT NULL, server default `'any'`** (`any | all` — how the bill's TERM LIST combines, docs/25 §M121 D2; the default is the widening mode so every pre-M121 row keeps the answer it had). The terms themselves are rows of the child table below, NOT columns here and NOT JSON (docs/25 §M121 D4); `match_term` stays as the mirror of terms[0] so the pre-M121 wire field never lies.

**No link table.** A bill's transactions are the rule re-applied over the committed ledger on every read (docs/25 §M116 D3), so there is nothing that can go stale and nothing for `DELETE /bills/{id}` to cascade.

#### bill_reminder_match_terms
One row per term of a bill's match rule, in order (M121, docs/25 §M121, migration `0046_multi_term_match`). `id PK`, `bill_reminder_id FK` → `bill_reminders.id` **`ON DELETE CASCADE`** (deleting a bill takes its terms at the database, so no orphan row can survive a raw `DELETE`), `position int` (0-based: `position 0` = terms[0] = `bill_reminders.match_term`), `term VARCHAR(120)`, `created_at`/`updated_at`. UNIQUE `(bill_reminder_id, position)` (`uq_bill_reminder_match_terms_bill_position` — also the index the term read uses), CHECK `ck_bill_reminder_match_terms_term_len` (`length(term) BETWEEN 1 AND 120`) and CHECK `ck_bill_reminder_match_terms_position` (`position >= 0`). Bounds live in the TABLE, not only at the API seam; the list is capped at 10 terms by the API (docs/25 §M121 D7). Migration backfills position 0 from `match_term` with an idempotent `NOT EXISTS`, and the read path still falls back to `[match_term]` for a row with no terms at all.

#### debts
Mortgage, loan, credit card tracking with payoff projections (M44). `id PK`, `user_id FK`, `account_id FK`, `name`, `debt_type str` (`mortgage | credit_card | loan | student_loan | other`), `original_amount Decimal(12,2) nullable`, `interest_rate Decimal(5,2) nullable`, `minimum_payment Decimal(12,2) nullable`, `payment_frequency str nullable`, `start_date YYYY-MM-DD nullable`, `end_date YYYY-MM-DD nullable`, `notes str(500) nullable`.

#### spending_alerts
Budget threshold notifications (M41). `id PK`, `user_id FK`, `budget_id FK`, `alert_type str` (`warning | critical`), `threshold_pct Decimal(5,2)`, `actual_pct Decimal(5,2)`, `spent Decimal(12,2)`, `budget_amount Decimal(12,2)`, `acknowledged bool default false`. `budget_amount` is the DENOMINATOR the threshold was measured against — since M110 that is the spendable pool (`budgets.amount + carried_in`), identical to `amount` while carry is off, so the stored row still reproduces `actual_pct`; `actual_pct` keeps its documented `999.99` ceiling, which is also what a non-positive pool stores (there is no ratio to store, docs/08 §Budget rollover).

#### reconciliation_items
Bank statement matching items (M32/M39). `id PK`, `user_id FK`, `account_id FK`, `transaction_id FK nullable`, `statement_date YYYY-MM-DD`, `statement_amount Decimal(12,2)`, `statement_description str(500) default ""`, `status str` (`matched | unmatched`) default `unmatched`.

#### tags
User-created labels for transactions beyond the single category FK. `id PK`, `user_id FK`, `name str(100)`. UNIQUE `(user_id, name)`.

#### transaction_tags
Many-to-many join between transactions and tags. `id PK`, `transaction_id FK`, `tag_id FK`. UNIQUE `(transaction_id, tag_id)`.

#### rule_tags
Many-to-many join between rules and tags. `id PK`, `rule_id FK`, `tag_id FK`. UNIQUE `(rule_id, tag_id)`.

#### notification_settings
User-configurable alert settings (M31). `id PK`, `user_id FK`, `alert_type str(50)` (`large_transaction | low_balance | dividend_received | price_alert | monthly_summary | budget_threshold | bill_due | tax_loss`), `enabled bool default true`, `delivery_enabled bool not null default false` (**M59**, migration 0030 — the second, per-type switch for outbound delivery; seeded as a **copy of `enabled`**, so the legacy five read `false` and the generator three read `true`), `config JSON nullable` (threshold amounts, etc.). UNIQUE `(user_id, alert_type)`. Auto-created on first `GET /notifications` **and on first `GET /notifications/inbox`**, for any type the row is missing for: the **five legacy types default `enabled=false`** (M31) and the **three generator-backed types default `enabled=true`** (M51, docs/35).

The five legacy `alert_type`s have **no generator**: nothing in the backend produces those events and nothing but `GET`/`PATCH /notifications` reads their `enabled`. The three M51 types are the inbox's only sources (`spending_alerts`, `bill_reminders`, computed tax-loss positions — docs/35).

#### notification_channels
Per-user outbound transport (M59, docs/37). `id PK`, `user_id FK`, `channel str(20)` (`email | webhook`), `enabled bool not null default false`, `target str(320) nullable` (SMTP recipient address / webhook URL), `privacy_opt_in_at timestamptz nullable`, `created_at`/`updated_at`. UNIQUE `(user_id, channel)` (`uq_notif_channel_user_kind`). Exactly two rows per user, both seeded **disabled** — no transport is on until a user turns one on. **Holds no secret**: SMTP credentials come from env (`NOTIFY_SMTP_*`) and the webhook HMAC key is derived at send time from `NOTIFY_WEBHOOK_SECRET`/`JWT_SECRET` (docs/37). `privacy_opt_in_at` is written **only** by an explicit `privacy_opt_in: true` PATCH — that is the whole privacy-mode resolution: `privacy_mode` users are in-app only unless they opted in per channel.

#### notification_deliveries
Delivery outbox, one row per inbox item per channel (M59, docs/37). `id PK`, `user_id FK`, `channel str(20)`, `alert_type str(50)`, `item_key str(120)` (the inbox item `id` verbatim), `severity str(20)`, `occurred_at timestamptz`, `link str(50)` (the payload snapshot taken at enqueue — retries re-send identical bytes), `status str(20) default 'pending'` (`pending | sent | dead | cancelled`), `attempts int default 0`, `next_attempt_at timestamptz default now()`, `last_error text`, `last_error_code str(50)`, `sent_at timestamptz`, `dead_at timestamptz`, `retracted_at timestamptz`, `created_at`/`updated_at`. The inbox union is untouched by any of this: a failed send changes nothing in `spending_alerts`, `bill_reminders` or read state.

#### notification_delivery_attempts
Audit log of real send attempts (M59, docs/37). `id PK`, `user_id FK` (denormalised from the delivery so export scoping and account deletion both cover it), `delivery_id FK`, `attempt int` (1-based), `outcome str(20)` (`sent | retryable | fatal`), `error_code str(50)`, `http_status int`, `detail text` (sanitized, ≤500 chars, never the payload), `created_at`/`updated_at`. A dispatch blocked before any I/O writes **no** attempt row — it lands on the delivery as `status=cancelled` + `last_error_code=delivery_not_eligible`.

#### notification_inbox_reads
Read state for one in-app inbox item (M51, docs/35). `id PK`, `user_id FK`, `item_key str(120)` (the inbox item `id` verbatim: `budget_threshold:<spending_alerts.id>` | `bill_due:<bill_reminders.id>` | `tax_loss:<security_id>:<account_id>`), `read_at timestamptz default now()`, `created_at`/`updated_at`. UNIQUE `(user_id, item_key)` — makes mark-read idempotent. Independent of `spending_alerts.acknowledged`, which still gates only `GET /budgets/alerts`.

#### tax_projections
Estimated tax liability by year (M42). `id PK`, `user_id FK`, `year int`, `province str(2) default "ON"`, `estimated_income Decimal(12,2) default 0`, `deductions Decimal(12,2) default 0`, `capital_gains_override Decimal(12,2) nullable`, `dividend_income_override Decimal(12,2) nullable`.

#### vault_envelopes
Wrapped-DEK sync across devices (E2EE Phase 4, M53 — docs/33). One row per browser profile enrolled in the encryption vault. `id PK`, `user_id FK`, `device_id str(64)` (32 lowercase hex, client-generated), `label str(64) nullable` (plaintext browser · OS metadata), `wrapped_blob TEXT nullable` (opaque passphrase-locked bundle the server never decodes), `revoked_at TIMESTAMPTZ nullable`. CHECK `ck_vault_envelopes_state`: active (`revoked_at IS NULL`) ⇔ `wrapped_blob IS NOT NULL`, revoked ⇔ blob dropped — revocation deletes the copy the database held. There is deliberately no "re-enroll a revoked identity" route (docs/33 Phase 4).

#### backup_artifacts
One row per backup period the caller uploaded (M56 — docs/11). `id PK`, `user_id FK ON DELETE CASCADE`, `period str(16)` (idempotency key: daily `YYYY-MM-DD`, weekly ISO `YYYY-Www`, monthly `YYYY-MM`), `ciphertext TEXT` (the `v1.<base64(iv)>.<base64(ciphertext)>` field envelope produced **in the browser** under the vault DEK — the server stores it verbatim and never decodes it), `size_bytes int` (`len(ciphertext)`), `created_at`/`updated_at`. UNIQUE `(user_id, period)` = `uq_backup_artifacts_user_period`, which makes two racing tabs upsert into one row and makes "Back up now" refresh today's slot. Deliberately **not** in `_USER_OWNED_MODELS` (a restore must not delete the backups it restores from) and **not** in `GET /export/full`'s registry (a backup must never nest the backups) — both pinned by `tests/test_backup.py::test_restore_coverage_matches_the_export_registry`. `ciphertext` is metadata the DB reader can see alongside cadence and size; the honest claim for the feature is docs/11 § Honest claims.

#### sync_agreements
Open-banking consent records (M79 — docs/39). `id PK`, `user_id FK`, `provider str(32)` (registry slug),
`consent_id str(128)` (the provider's own reference — **not** a credential; this table stores no token,
no secret, nothing a third party can replay), `status enum(active | auth_required | revoked)` (`native_enum=False`,
length 32; `revoked` is terminal), `account_refs JSON not null` (provider account ref -> `accounts.id`; JSON
rather than a junction table because it is read/written only through `services/sync` and tenancy + export
scoping ride on `user_id`), `scopes JSON not null`, `last_sync_at TIMESTAMPTZ nullable`,
`provider_status enum(ok | degraded | auth_required) nullable`, `last_error str(255) nullable`,
`revoked_at TIMESTAMPTZ nullable`, `created_at`/`updated_at`. UNIQUE `uq_sync_agreement_consent`.
Dangling `account_refs` values (an account deleted between pulls) are skipped at pull time, never staged into.

#### sharing_links
Household-sharing consent (M90 — docs/43 §3). `id PK`, `user_id FK` (the OWNER — the data subject), `partner_user_id FK NULL` (the PARTNER, `ON DELETE CASCADE` — a deleted partner drops the link while the owner's receipts survive), `email str(320)` (plaintext **by design** — the owner's record of whom they invited), `token_hash str(64)` UNIQUE (sha256 hex of the raw invite token, **never nulled** so a replay answers `409 sharing_invite_used` instead of looking unknown; the raw token exists only in the create `201` body and the email), `status enum(pending | active | revoked | expired | cancelled)` (`native_enum=False`, length 16, CHECK `ck_sharing_links_status`; terminal states never transition — a new relationship is a new row), `account_ids JSON not null` (the owner's allowlist of `accounts.id`, order-preserving, de-duplicated — the `sync_agreements.account_refs` rule: written only through the sharing router, tenancy + export scoping ride on `user_id`), `expires_at TIMESTAMPTZ` (= `created_at` + 7 days), `accepted_at`/`revoked_at TIMESTAMPTZ nullable`, `created_at`/`updated_at`. Partial UNIQUE `uq_sharing_links_active_owner (user_id) WHERE status='active'` and `uq_sharing_links_active_partner (partner_user_id) WHERE status='active'` (I2 — the **database**, not the application, decides the accept race). Joins `_USER_OWNED_MODELS`. Migration 0035.

#### sharing_audit
Consent receipts (M90 — docs/43 §3), the `mcp_audit` shape applied to the sharing router. `id PK`, `user_id FK` (the OWNER — export/deletion scope), `actor_user_id INT NULL` (**no FK** — the actor may be the partner; deleting that account must not delete receipts), `invite_id INT NULL` (**no FK** — link rows die with their users), `event str(64)` (`invite_create | invite_cancel | invite_accept | scope_update | revoke | leave`), `args_hash str(64)` (sha256 hex of the canonical request-body JSON — the invite body carries the invitee's address, so it is hashed, never stored), `response_code INT` (the status actually returned, refusals included), `duration_ms INT`, `created_at`/`updated_at` (append-only bookkeeping). Best effort: a receipt that fails to write is logged and the response stands. No secrets — exported with the rest of the tenant, wiped by account deletion and the demo reset. Migration 0035.

#### recurring_suppressions
"One of these is enough" for the recurring prompt (M96 — docs/09 §Recurring prompt): a declined candidate group writes exactly one row so the same group can never re-prompt. `id PK`, `user_id FK`, `group_key str(120)` (`"<merchant_key>|<cadence>"`, amount-free on purpose: an amount drift inside the ±15% band must not mint a new key and re-ask a question the user already answered), `created_at`/`updated_at`. UNIQUE `uq_recurring_suppressions_user_group (user_id, group_key)` — the tenancy boundary AND the idempotency key in one constraint, so a repeated decline is a no-op. Read by `GET /transactions/recurring/candidates`, written by `POST /transactions/recurring/suppress`; there is deliberately no delete route (the decision is final — docs/09). Migration 0037. **M165: a stored key is re-read through TODAY's `normalize_payee_key`** (`services/recurring.py::rekey_suppression`, applied to the merchant segment at query time, alongside the raw value) — the normalize learned that whitespace is not identity, and re-reading keeps every previously declined group declined without rewriting rows (a rewrite could collide with the UNIQUE above). Pure and idempotent, so a current-form key is unchanged by it.

#### transaction_events
Append-only, per-transaction receipts (M106) — the trust feature Monarch's
users demanded after their 11k-edits/day data-loss incident: **edits never
silently vanish; every change is a receipt.** Three logs exist and they never
mix: `mcp_audit` is "what did the bot do" (M80), `sharing_audit` is "what did
the owner consent to" (M90), and this one is "what happened to THIS row" —
per-transaction and user-facing.

`id PK`, `tx_id INT` (**no FK** — deleting a transaction must NOT delete the
receipts it produced, or `deleted` would be the one event that vanishes; a
dangling `tx_id` is expected and is exactly what the delete receipt points
at), `user_id FK` (the OWNER — export/deletion scope, joins
`_USER_OWNED_MODELS`), `event_kind enum(created | edited | categorized |
tagged | reviewed | split | merge | deleted)` (`native_enum=False`, length 16;
`split` was reserved ahead of M107 and `merge` (docs/44 §11) was added with
it — **both are written only by the four `/transactions/{id}/split`
routes**; the column ships as `VARCHAR(16)` with no server-side CHECK
(migration 0039), so the widening needed no migration at all),
`payload JSON not null` (the human detail the drawer renders —
`{"fields": [...], "changes": {...}}`, `{"category_id": 7,
"category_name": "Groceries"}`, `{"added": ["pantry"], "removed": []}`,
`{"reviewed": true}`, `{"source": "import", "batch_id": 412, ...}`,
`{"fields": ["transfer_pair_id", "category_id"], "changes": {...},
`"transfer_pair_id": 87, "category_name": "transfers"}`,
`{"fields": [...], "changes": {...},
"learned": {"normalized_payee": "NETFLIX", "category_id": 7}}` — the
learning-loop marker (docs/04
§Learning loop): written ONLY when the edit itself moved `category_id` or
`normalized_payee`, on the PATCH route AND on `bulk/recategorize` /
`bulk/payee` (the same correction as N rows), so the drawer can render
"learned from you"; every other receipt carries no `learned` key),
`payload_hash str(64)` (unsalted sha256 over `payload` in canonical form —
sorted keys, tight separators, `default=str`, the **byte-identical formula**
to `mcp_audit.args_hash`; hash and detail are written together, so a receipt
can be re-verified rather than trusted), `actor INT NULL` (**no FK** — the
`sharing_audit.actor_user_id` rule; `NULL` = a cloud-service/system action,
which is what a bank-sync pull stages with **and** what every pipeline apply
writes, M120 — so those receipts are owner-only through the partner route's
existing `actor IN (owner, partner)` filter), `created_at`/`updated_at`
(`created_at` IS the event's `at`; `updated_at` is schema bookkeeping only —
rows are append-only and there is no route that mutates one).

**Written at the handler layer only** (the M91-6 ruling, docs/43 §3): exactly
one receipt per mutation path per affected transaction, in the SAME database
transaction as the change itself — so a mutation without its receipt cannot
land, which is the deliberate opposite of `sharing_audit`'s best-effort rule
(that log answers "did the invite work?"; this one IS the user's history).
Pre-handler refusals (`422` body validation, `429` limiter, `403` demo guard)
never reach a handler and therefore write nothing.

| path | kind | when it writes NOTHING |
|---|---|---|
| `POST /transactions` | `created` (`{"source": "manual"}`) | duplicate → 409 |
| `services/import_staging.py::stage_rows` (file upload, YNAB, bank sync) | `created` (`{"source": "import" \| "sync", "batch_id", "file_name", "source_label"}`) | the batch is rejected (`413 too_many_rows` fires before any row exists) |
| `PATCH /transactions/{id}` | `categorized` when `category_id` moved, otherwise `edited` — both additionally carry `learned` when the correction taught the learned map (docs/04 §Learning loop) | the PATCH re-sends values that are already there |
| `PUT/POST/DELETE /transactions/{id}/tags[...]` | `tagged` (one receipt carrying `added` + `removed`) | the junction did not move (same set re-PUT, re-attach, detach of an absent link) |
| `POST /transactions/{id}/review` | `reviewed` (`{"reviewed": true\|false}`) | asked for the state the row already has |
| `DELETE /transactions/{id}` | `deleted` (payee/date/amount/currency/batch — the receipt outlives the row) | — |
| `POST /transactions/{id}/split` (create) and `PUT /transactions/{id}/split` (replace) | `split` on the **parent** (`tx_id = parent.id`; payload `{"op": "create", "parent_id", "child_ids", "amounts"}` / `{"op": "replace", "parent_id", "before", "after"}`) | any refusal in docs/44 §4.2 — every check runs before anything is inserted, so a rejected split writes **zero rows** and zero receipts |
| `DELETE /transactions/{id}/split` (merge-back) | `merge` on the **parent** (`tx_id = parent.id`; payload `{"op": "merge", "parent_id", "child_ids"}` — the child ids outlive the children they name) | `404 not_found`, `409 transaction_in_pending_batch`, `409 not_split` |
| `POST /transactions/accept-transfer-suggestion`, `POST /transactions/{id}/detect-transfer`, `POST /transactions/{id}/link-transfer`, `POST /transactions/{id}/unlink-transfer` | `edited` — **one per row the call moves**, since none of the four goes through PATCH (`transfer_pair_id` sits at the payload top level as well as inside `changes`, so `render_summary` reads `42 → "you linked a transfer"` / `null → "you unlinked a transfer"`; `accept` also carries `category_name: "transfers"`) | the unlink of an already-unpaired row (no row moved) |
| `POST /trades/record-dividend` (the ledger row it creates) | `created` (`{"source": "trade"}` → `"<actor> created this from a dividend capture"`) | — |
| `POST /suggestions/{id}/accept` (M120) | `categorized` (`{"category_id", "category_name", "via": "suggestion_accept", "suggestion_id"}` → `"<actor> set category Groceries (accepted suggestion)"`) | `404 not_found`, `409 suggestion_not_pending`, `409 transaction_is_split` (M107) |
| `POST /rules/{id}/apply` (M120, docs/31) | `categorized` (`{"category_id", "category_name", "via": "rule_backfill"}`) **one per row it updates**, plus a `tagged` rider carrying the SAME `added`/`removed`/`added_ids`/`removed_ids` shape `PUT …/tags` writes when the rule's tags moved the junction — all in the apply's one commit | `mode=forward` (returns before the engine), `overwrite=false` rows already categorized, a rule matching nothing |
| `POST /transactions/recurring/accept` (M120) | `edited` (`{"fields": ["is_recurring", "recurring_cadence", "recurring_next_date"], "changes": {...}}`) — **one per row**, no `via` key, so it renders like any other multi-field PATCH | a pre-write `404 not_found` / `409 transaction_in_pending_batch` on any id in the group |
| `services/categorization.py` — pipeline layer 1 `apply_rules`, layer 2 learned map, layer 3 LLM autopilot, driven by import/sync or `POST /suggestions/run` (M120) | `categorized` (`{"category_id", "category_name", "via": "rule" \| "learned" \| "llm"}`) **one per row**, `actor NULL`; layer 1 additionally writes `tagged` when a rule's tags moved the junction | layers 2/3 only scan `category_id IS NULL`, so they cannot no-op; layer 1 writes on EVERY hit, including a hit that confirms the value already there (the symmetric-write reading, pinned by docs/M120_RECEIPTS_SPEC §6) |

**Deliberate boundary (reported, not hidden):** nothing that creates, edits
or deletes a transaction row is silent — the four transfer routes, the three
receipt-writing split routes (POST/PUT `…/split`, `DELETE …/split`) and the
dividend capture's ledger row are receipted in the table
above, because they were named as mutation paths. Splitting and merging are
**no longer reserved**: M107 landed (docs/44 §11) and writes exactly one
`split` receipt per create/replace and one `merge` receipt per merge-back.
The one remaining gap is deliberate: **child rows write no `created`/`deleted`
receipt of their own** (D15) — their lifecycle IS the parent's `split`/`merge`
receipts, so one mutation path still yields one row in the log no matter how
many rows moved. A later edit to a child writes an ordinary receipt with
`tx_id = child.id`, exactly like any other row.

**Receipt scope (v2, M120):** the path table above is the spec, is exhaustive
as written, and **every mutation path in the product now writes**. The four
exceptions M106 recorded as "reported, not hidden" are lifted by M120
(`docs/M120_RECEIPTS_SPEC.md`): `POST /suggestions/{id}/accept`,
`POST /rules/{id}/apply`, `POST /transactions/recurring/accept` and the
pipeline's own applies (rules engine, learned map, LLM autopilot — docs/04 no
longer says "applies silently") are rows in the table above. What is left of the
old boundary is only what never moved a field in the first place: child rows
still write no `created`/`deleted` of their own (D15, above), and the demo SEED
still writes none because the demo tenant cannot write at all. M120 added **zero**
`event_kind` values and **no** migration — the only new payload key is the
optional `via` provenance string (`rule | learned | llm | rule_backfill |
suggestion_accept`) on `categorized` receipts, which `render_summary` reads at
render time. A plain PATCH payload carries no `via`, so its sentence is
byte-identical to what M106 shipped. Pipeline applies also carry `actor NULL`,
so the partner projection's existing `actor IN (owner, partner)` filter drops
them without a new predicate (docs/43 §6).

Read by `GET /transactions/{id}/events` (owner) and
`GET /partner/transactions/{id}/events` (docs/43 §6 — owner/partner actors
only, payload stripped; the owner's own route applies no actor filter, so the
owner sees system and third-party receipts too). Exported with everything
else; the ZIP's visibility filter follows the row (`VISIBILITY_FILTERS`:
receipts of committed/manual transactions **plus receipts whose row no longer
exists anywhere** — the `deleted` receipt is the one event whose whole purpose
is to outlive its subject, and `tx_id` has no FK, so "gone" is asked against
the table, not against this tenant's rows). `payload` has no cipher column
(docs/33 *Server-readable plaintext TODAY*), so it is NOT masked by a locked
export; the encrypted browser backup seals the whole snapshot, receipts
included (docs/11 § *E2EE vault (M106)*).
Wiped by account deletion and the demo reset. The demo SEED writes no
receipts (the handler-layer rule), so a freshly seeded demo's rows open an
empty History — an import or an edit fills it. Migration 0039.

#### transaction_receipts
The ONE receipt **image** per transaction (M108 P4) — a photo of a paper
receipt hung off a ledger row. Deliberately kept apart from
`transaction_events` (M106), which stores per-transaction **activity**
receipts: this table holds a picture, that one holds history, and they share
no column, route, constant or helper. Read "receipt image" here and
"activity receipt" there.

`id PK`, `user_id FK` (the OWNER — export/deletion scope; joins
`_USER_OWNED_MODELS`), `transaction_id FK` **UNIQUE**
(`uq_transaction_receipts_transaction` — one-to-one IS the v1 contract: a
second upload REPLACES rather than accumulating a gallery), `content_type
str(64)` (exactly one of `image/webp`, `image/jpeg`, `image/png`,
`image/gif`, re-sniffed from the body's magic bytes at upload and echoed back
unchanged on read), `size_bytes INT` (≤ `RECEIPT_MAX_BYTES` = 2 MB, checked
against `Content-Length` BEFORE the body is read and against `len(raw)` after,
so a chunked request cannot slip past), `data_base64 TEXT not null`, `created_at` /
`updated_at` (tz-aware, `server_default now()` — TimestampMixin).

**Why base64 TEXT and never `bytea`.** `/export/full`, the ZIP export and the
encrypted browser backup all render rows through
`api/export.py::_jsonable` (`model_dump(mode="json")` + `json.dumps`), and
neither step can encode bytes — a `bytea` column would turn a fresh export
into a 500 and the docs/11 restore guarantee with it. This is the same shape
`backup_artifacts.ciphertext` and `vault_envelopes.wrapped_blob` already use.
The cost is ~1.37x the bytes on disk; the alternative (a per-column special
case in `_jsonable`, plus a matching one on the restore path) is exactly the
kind of bespoke branch that drifts.

**Never rides the list.** `GET /transactions` sets `has_receipt: bool` from
ONE batched `SELECT transaction_id … WHERE user_id = <owner> AND
transaction_id IN (page)` — a constant statement per request, so docs/34
budget rows 4/5 moved 4 → 5 while invariant 1 (page-size independence) still
holds. The `user_id` term is the M93 tripwire's R3 (every statement touching a
tenant table owns its predicate explicitly) and binds the OWNER on both
surfaces `_transaction_page` serves. The base64 itself travels
only `GET /transactions/{id}/receipt`, owner-scoped, `Cache-Control:
private`.

Read by the three receipt routes (docs/08). Included in `/export/full` and the
ZIP export with **committed-only visibility** — a receipt on a staged import
row stays out until the batch commits, the same rule as `transaction_tags`.
Wiped by account deletion and the demo reset, ordered **before** `transactions`
whose FK would otherwise refuse (M107's ordering rule). Migration 0041
(`0041_transaction_receipts`, revising M110's `0040_budget_carry`).

## Indexes

- `transactions`: `(user_id, account_id, date)`, `(user_id, category_id)`, `(user_id, date)`, `(user_id, payee_token)`, `(account_id, dedup_hash)` UNIQUE, `normalized_payee` GIN (`gin_trgm_ops`), `(parent_split_id)`, `user_id WHERE reviewed_at IS NULL`
  - **M116 measured:** the bill scan (`services/bill_match.py::_rows_statement`) is bounded by `user_id` + committed staging + `amount_native < 0` (+ optional category/date) and deliberately carries **no** payee predicate — the payee test runs in Python against `normalize_payee_key`, because Postgres's case folding is a different function (`'straße'.upper()` is `STRASSE`, `upper('straße')` is `STRAßE`), so no `ILIKE`/`upper()` over the raw column is a proven *superset* of the shared normalizer and a prefilter that can be narrower silently loses rows. The `pg_trgm` GIN index therefore still serves the one remaining server-side scorer it has always served (`services/categorization.py::_payee_score`, docs/02 §transactions) and NOT bills. EXPLAIN + the 10k-row timings are captured by `backend/tests/test_bills_match.py::test_detail_first_paint_is_under_the_1_5s_budget_at_10k_rows` (its `m116_perf_evidence.json`), which also pins that the plan carries the `user_id` predicate — drop it and the test fails. **No index was added for M116.**
- `trades`: `(user_id, account_id, security_id, date)`
- `acb_pools`: `(account_id, security_id)` UNIQUE
- `fx_rates`: `(base, quote, date)` UNIQUE
- `fundamentals`: `(user_id)`, `security_id` UNIQUE
- `llm_suggestions`: `(transaction_id)` where status = suggested
- `etf_holdings`: `(user_id)`
- `mcp_tokens`: `(user_id)`, `token_hash` UNIQUE
- `mcp_audit`: `(user_id)`, composite `(user_id, created_at DESC)` — `ix_mcp_audit_user_created`
- `totp_factors`: `user_id` UNIQUE (M74)
- `recovery_codes`: `(user_id)`, UNIQUE `(user_id, code_hash)` (M74)
- `webauthn_credentials`: `(user_id)`, `credential_id` UNIQUE (M74)
- `oidc_links`: `(user_id)`, UNIQUE `(issuer, subject)` (M74)
- `vault_envelopes`: `(user_id)`, `device_id`; partial UNIQUE `(user_id, device_id) WHERE revoked_at IS NULL` (`uq_vault_envelopes_active` — one active envelope per device, a revoked row does not block re-creation)
- `notification_inbox_reads`: `(user_id, item_key)` UNIQUE
- `backup_artifacts`: `(user_id)`, UNIQUE `(user_id, period)` (`uq_backup_artifacts_user_period` — the scheduler's idempotency key)
- `notification_channels`: `(user_id, channel)` UNIQUE
- `notification_deliveries`: partial UNIQUE `(user_id, channel, item_key) WHERE retracted_at IS NULL` (`uq_delivery_active` — one active row per inbox item per channel; a retracted row keeps the audit trail but stops blocking a later re-delivery), partial `(next_attempt_at) WHERE status = 'pending'`
- `notification_delivery_attempts`: `(delivery_id)`, `(user_id)`
- `sync_agreements`: `(user_id)`, UNIQUE `(user_id, provider, consent_id)` (`uq_sync_agreement_consent`)
- `sharing_links`: `(user_id)` (`ix_sharing_links_user`), `(partner_user_id, status)` (`ix_sharing_links_partner_status` — the partner's single indexed read), `(user_id, created_at DESC)` (`ix_sharing_links_user_created`), UNIQUE `token_hash`; partial UNIQUE `(user_id) WHERE status='active'` (`uq_sharing_links_active_owner`) and `(partner_user_id) WHERE status='active'` (`uq_sharing_links_active_partner`) — one active seat per side (I2)
- `sharing_audit`: `(user_id)`, composite `(user_id, created_at DESC)` (`ix_sharing_audit_user_created`)
- `bill_reminder_match_terms`: UNIQUE `(bill_reminder_id, position)` (`uq_bill_reminder_match_terms_bill_position` — the leading `bill_reminder_id` IS the lookup index, so "the terms of bill N, in order" needs no second B-tree, M121)
- `recurring_suppressions`: UNIQUE `(user_id, group_key)` (`uq_recurring_suppressions_user_group` — the leading `user_id` is the scan's own index, M96)
- `transaction_events`: `(tx_id)`, `(user_id)`, composite `(tx_id, created_at DESC)` (`ix_transaction_events_tx_created` — the drawer's query: one row's receipts, newest first)
- `transaction_receipts`: `(user_id)` (`ix_transaction_receipts_user_id` — account deletion and the demo reset sweep by user), UNIQUE `(transaction_id)` (`uq_transaction_receipts_transaction` — the one-image contract, enforced by the DB rather than only by the handler). No plain `transaction_id` index: it would be a second identical B-tree answering the same query the UNIQUE already serves (docs/39)
- `transactions`: partial `(user_id) WHERE reviewed_at IS NULL` (`ix_transactions_user_unreviewed` — the "Needs review" queue and its count, M106)
- `transactions`: `(parent_split_id)` (`ix_transactions_parent_split_id` — the split group fetch, the `ON DELETE CASCADE` sweep and the "does this row have children" probe, M107). Deliberately **no** index on `is_split`: it is near-constant-false and adds nothing to the `(user_id, …)` composites above
- `accounts`: `last_sync_at` / `provider_status` are status columns only — no index (docs/39)

## Invariant

**`acb_pools` is always rebuildable from `trades` alone** (plus cached/manual `fx_rates`). The pool table exists as a cache for query speed; any write path to `acb_pools` must go through the rebuild function in `backend/app/tax/acb.py`, never direct mutation. Trade writes auto-rebuild ALL of the user's non-registered pools inside the same DB transaction (superficial loss is cross-account; a pair-scoped rebuild computes stale denials), and the engine evaluates over the complete trade history at rebuild/report time. `POST /acb/rebuild` remains for repairs/backfills.
