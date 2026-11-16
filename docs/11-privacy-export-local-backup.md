# 11 — Privacy Export & Local Backup

## Context

The current architecture stores all user data in PostgreSQL behind a FastAPI backend, with a React SPA frontend (docs/01). This plan adds an optional privacy layer: users can download a full local copy of their data and keep an encrypted copy in browser storage, while cloud-dependent features (cloud LLM providers, external market-data APIs) are disabled when privacy mode is active. The server remains the canonical data store; this is not a local-first rewrite.

Milestone anchor: **M7** (docs/10). Depends on the M1 schema and the M6 Settings page.
Extended by **M56** (scheduled encrypted backup + restore) and **M75**
(one-click ZIP/CSV export, OFX, scheduled export, the published portability
promise — see § "One-click export (M75)" below).

## Approach

1. **Expose the existing privacy/LLM preference columns via the API**
   - No schema change: `privacy_mode`, `llm_provider`, `llm_base_url`, `llm_model`, `llm_api_key` already exist on the `users` table — created in the M1 initial migration, consumed by M7 (docs/02 users).
   - Expose `privacy_mode` plus the LLM preference fields (write-only `llm_api_key` — docs/02) in current-user read/update payloads (`GET /users/me`, `PATCH /users/me`) following existing auth user-schema patterns.
   - Rationale: privacy mode must persist across sessions and devices, so it lives server-side on the `users` row.

2. **Add a full-data export endpoint**
   - Create `backend/app/api/export.py` and mount it in `backend/app/main.py` at `/api/v1/export`.
   - Implement `GET /api/v1/export/full`, authenticated via existing JWT dependency.
   - Query every table in the **model registry** (`app.models` / `SQLModel.metadata`) filtered by `current_user.id` — the key set is derived (registry minus `export.py EXCLUDED_TABLES`), never hand-listed, so budgets, goals, debts, tags, etf_holdings, fundamentals, bill_reminders, notification_settings, reconciliation_items, rule_tags, spending_alerts, tax_projections, transaction_tags, watchlist, mcp_audit (M80 receipts — no secrets in them: arguments are sha256 hashes), sharing_links + sharing_audit (M90 — `token_hash` is the sha256 digest of a single-use capability and receipts carry `args_hash` digests; neither is a secret, docs/43 §10), transaction_events (M106 — per-transaction receipts: `payload_hash` is a sha256 digest and `payload` is the user's own edit history, so the table is content, not a secret), transaction_receipts (M108 P4 — receipt images: `data_base64` is TEXT, not `bytea`, precisely because this step runs `model_dump(mode="json")` + `json.dumps` and neither can encode bytes; the photo is the user's own content, not a secret) and any future table export automatically (audit M13). Junction tables (`rule_tags`, `transaction_tags`, and M121's `bill_reminder_match_terms`, scoped through its owning bill — docs/25 §M121) carry no `user_id` and are scoped through their owning rule/transaction/bill; a table with no scoping rule fails closed (`500 export_missing_scoping`), and registry drift fails closed (`500 export_registry_drift`). Export `fx_rates` UNFILTERED (shared reference cache, no `user_id` — docs/02). Excluded: `refresh_tokens` and `mcp_tokens` (auth hashes), the four M74 auth-plumbing tables `totp_factors`, `recovery_codes`, `webauthn_credentials`, `oidc_links` (second-factor secrets, recovery-code hashes, passkey key material, live SSO links — docs/38), plus alembic bookkeeping, plus alembic bookkeeping, `backup_artifacts` (the artifact store — a backup never nests backups) and `provider_fund_registry` (M168, docs/07 §Provider registry: issuer verdicts shared by every tenant — server-side coverage configuration, not the caller's content, and a snapshot that could write it would hand every tenant a way to edit the fund registry the whole server reads).
   - Return a single JSON object keyed by table name with row arrays; preserve primary keys and foreign keys so the snapshot can be re-imported later. The encrypted browser backup stores this SAME response — its coverage inherits the registry (there is deliberately no second table list in the frontend to drift).
   - Exclude auth secrets: `hashed_password`, `llm_api_key`, refresh-token hashes, MCP-token hashes, and any internal credentials.
   - Keep the endpoint independent of LLM or market-data services so it always works.

3. **Add Settings UI for downloading the export**
   - In `frontend/src/pages/Settings.tsx`, add an "Export my data" section with a download button and the timestamp of the last export.
   - On click, call `GET /api/v1/export/full`, convert the response to a Blob, and trigger a browser download named `fin-tracker-export-<iso-date>.json`.
   - Use existing TanStack Query loading/error patterns.

4. **Add an optional encrypted browser backup**
   - Create `frontend/src/lib/privacyBackup.ts`:
     - Use IndexedDB (native API or `idb`) with an object store `backups`, key `'user_<userId>'`.
     - Encrypt the snapshot with Web Crypto: PBKDF2 key derivation from a user passphrase, random 16-byte salt, random 12-byte IV, AES-GCM.
     - Persist only `{ salt, iv, ciphertext, createdAt }`; never store the passphrase.
     - Expose `storeBackup(snapshot, passphrase)`, `loadBackup(passphrase)`, `deleteBackup()`, and `hasBackup()`.
   - In Settings, add a "Keep encrypted backup in this browser" toggle.
     - Enabling prompts for a passphrase, then fetches and stores an encrypted backup.
     - Disabling calls `deleteBackup()`.
     - Display the backup timestamp from `createdAt`.
   - If `crypto.subtle` or IndexedDB is unavailable, show a message and fall back to download-only; do not block the download path.

5. **Add privacy-mode toggle and lock down cloud LLM**
   - In Settings, add a "Privacy mode" toggle that `PATCH`es `privacy_mode` on `/users/me`.
   - When `privacy_mode` is true:
     - The LLM provider selector shows only `ollama`; if the current provider is `openai_compatible`, automatically switch to `ollama` with base URL `http://localhost:11434/v1`.
     - Hide `LLM_API_KEY` and cloud base URL inputs; keep the model input for Ollama.
   - Backend validates: `PATCH /users/me` rejects `privacy_mode=true` combined with `llm_provider=openai_compatible` with `409 privacy_mode_requires_local_llm`. This code is raised inline at the call site in `backend/app/api/users.py:update_me` (the error envelope has no code registry — codes are inline `api_error(...)` literals, not constants in `app/core/errors.py`). The backend performs **no** auto-switching; the auto-switch to `ollama` described above is frontend-only (`frontend/src/pages/Settings.tsx`).
   - Show a privacy-mode indicator in the Settings page (and optionally the app header) so the user knows cloud LLM is disabled.
   - **LLM egress contract (TARGET of the 2026-09-23 privacy/security-fix
     wave — this doc is the spec; verify code against it at integration):**
     - **Privacy ON:** enforced at send time at the LLM egress seam
       (`app/llm/base.py`), NOT by the `llm_provider` enum — the enum alone is
       not the check. Every LLM base URL must resolve to a loopback host
       (`localhost` / `127.0.0.1` / `::1`, http or https). A non-loopback URL
       fails closed with an error envelope and **no request is sent**.
       Enabling privacy mode from Settings **clears stored cloud base URLs**
       (resets `llm_base_url` to the loopback default), so no cloud
       destination survives the toggle.
     - **Privacy OFF:** a user-supplied `llm_base_url` must be `http` or
       `https` AND resolve to either a loopback host or a **public** host.
       Private / link-local / RFC1918 / cloud-metadata addresses are denied
       unless (a) the host matches the operator's own env `LLM_BASE_URL`
       host, or (b) the operator sets `LLM_ALLOW_PRIVATE_HOSTS=true`.
     - **Env `LLM_API_KEY` is forwarded only to the operator's own host** (the
       env `LLM_BASE_URL` host) — never to a user-supplied endpoint; user
       endpoints receive only the user's own stored key. (Implemented: `llm/base.py`
       selects the key with `select_api_key`, which resolves the env key only for
       that host.)
     - **LLM errors are generic 502 `{detail, code}` envelopes** — no upstream
       HTTP status, request URL, or transport message is echoed to the client.
       (Implemented: `app/api/users.py` raises
       `502 llm_upstream_error` with a fixed message; upstream status and URL are
       logged server-side through `safe_target` only.)
     - **fx/market-data keep the existing ContextVar lock**: `privacy_mode` is
       bound per-request in `app/api/deps.py:83-84,100-101` and enforced by
       `_guard_external_fetch` (`app/services/fx.py:107`,
       `app/services/market_data.py:54`) — unchanged by this wave.

6. **Disable external market-data features in privacy mode**
   - On the Investments and Settings pages, gate external-fetch UI behind `currentUser.privacy_mode`:
     - Hide or disable `/securities/lookup` yfinance symbol lookup; show the manual security entry form instead.
     - Hide or disable automatic `/fx` Bank of Canada Valet fetching; show the manual rate input instead.
   - Backend blocks external-fetch paths when `privacy_mode=true`, returning `403 privacy_mode_blocks_external_api`: `GET /securities/lookup`; any BoC Valet fetch behind `GET /fx` (a cache HIT still returns the cached row; a MISS — or a range that would require a fetch — returns the error envelope); yfinance lookups/backfills behind `/securities` and `/portfolio` valuation. The code is raised inline at the fetch seams — `backend/app/services/fx.py:_guard_external_fetch` and `backend/app/services/market_data.py:_guard_external_fetch` — not from a registry constant in `app/core/errors.py`.
   - Manual rates/prices and cached rates remain usable, preserving functionality without external calls.

7. **Ensure export and backup remain usable when cloud features are off**
   - Verify the export endpoint and backup flow have no dependency on LLM, yfinance, or BoC Valet.
   - The step-4 browser backup stays write-only (its passphrase is the user's
     second secret and is never stored, so the UI cannot read it back).
     **Restore exists for the scheduled artifacts of step 8** (`POST
     /backup/restore`) — the "deferred import-from-backup" note below is
     superseded by M56 for that path only.

## Scheduled backup & restore (M56)

Milestone anchor: **M56**. Extends steps 2–4: a user-set schedule
(daily/weekly/monthly) produces an **encrypted artifact the server stores but
cannot read**, retention keeps the last N artifacts, and a restore path puts a
tenant back after a wipe. The step-4 browser backup (passphrase-derived key,
one slot, IndexedDB) is unchanged and remains a separate, local-only copy —
see "Two backups, two keys" below.

### Honest claims (docs/33 wording rule — binding on UI copy and docs)

- The artifact is encrypted **in the browser under the vault DEK** before it
  leaves the machine: the server stores an opaque `ciphertext` string it
  cannot open, and never receives the passphrase, the KEK or the DEK
  (docs/33 key-holding model). An attacker who reads `backup_artifacts` gets
  the cadence, the size and ciphertext — not your data.
- That is **not** a new privacy guarantee over your data overall: the same
  rows sit in plaintext columns and are served by the API, so the honest
  status line stays **client-side encryption in progress — the server can
  still read your data** (docs/33 Goal). Never write "zero-knowledge" for
  this feature, the storage choice below, or anything else (product decision
  2026-09-26).

### Storage decision (both readings stated, then decided)

| | (a) browser-triggered download only | (b) server-side blob of the already-encrypted artifact — **DECIDED** |
|---|---|---|
| What the server holds | nothing | an AES-GCM envelope it cannot open |
| Survives losing the browser/OS | only if the user filed the downloaded file | yes — listed and downloadable from any signed-in device |
| Runs while no tab is open | no (missed period; catch-up on next open) | no either: encryption is client-side, so a run still needs an open tab |
| Retention (keep-last-N) | enforced in IndexedDB, dies with the browser profile | enforced server-side, survives the browser |
| Restore after total loss (new device, wiped DB) | needs the user's offline file in hand | artifact list → decrypt → restore; offline file works too |
| New readable data on the server | none | none — ciphertext + `period` + `created_at` (metadata, accepted) |

Both readings require an open tab at run time (there is no server-side
scheduler and there cannot be one: only the browser holds the DEK). The
difference is where the artifact *lives* afterwards, and a backup that dies
with the browser profile is not a backup. **Decision: (b)**, with the
downloadable encrypted file kept as the offline escape hatch (a user who
wants (a) verbatim just deletes server artifacts after downloading them).

### Two backups, two keys (do not conflate)

| | Step 4 browser backup (M7) | Scheduled backup (M56) |
|---|---|---|
| Key | PBKDF2-SHA256/600k from a dedicated passphrase (docs/33 KEK parameters) | the E2EE vault DEK (`vault.ts`, wrapped per device — Phase 4) |
| Where | IndexedDB, one slot, this browser | `backup_artifacts`, keep-last-N, any device |
| Needs | its own passphrase | an unlocked vault (enrollment per docs/33) |
| Restore | none (write-only) | `POST /backup/restore` |

M56 deliberately does **not** re-key the step-4 backup: its passphrase is
chosen independently of the encryption passphrase, so folding them would
either break the "lost encryption passphrase still has a copy" property or
invent a second key hierarchy — both forbidden here.

### Schedule model (server-side, roams across devices like `privacy_mode`)

Five columns on `users` (migration `0029_backup_artifacts`), each with a DB
CHECK so the invariants survive any client:

| Column | Type | Invariants |
|---|---|---|
| `backup_frequency` | `VARCHAR(16) NOT NULL DEFAULT 'off'` (`BackupFrequency` StrEnum, `sa.Enum(native_enum=False, length=16)`) | one of `off`, `daily`, `weekly`, `monthly` |
| `backup_hour` | `SMALLINT NOT NULL DEFAULT 3` | 0–23, **local time of the machine running the tab** |
| `backup_weekday` | `SMALLINT NOT NULL DEFAULT 0` | 0–6, 0 = Monday (used by `weekly` only) |
| `backup_day_of_month` | `SMALLINT NOT NULL DEFAULT 1` | 1–28 (used by `monthly` only; 28 keeps every month valid) |
| `backup_keep_last_n` | `INTEGER NOT NULL DEFAULT 5` | 1–20 (retention, below) |

**Due semantics (computed client-side, local time, injected clock):**

- `nextDue(after)` = the first local instant strictly after `after` matching
  the frequency: daily → every day at `hour:00`; weekly → the next local date
  whose weekday equals `backup_weekday` at `hour:00`; monthly → the next local
  date whose `getDate() === day_of_month` at `hour:00`.
  **Weekday encoding is pinned here because the two conventions differ**: the
  column and the Settings picker use `0 = Monday … 6 = Sunday`
  (`WEEKDAY_LABELS = ["Mon" … "Sun"]`), while JavaScript `Date.getDay()` is
  `0 = Sunday … 6 = Saturday`. `scheduledBackup.ts::nextDue` converts with
  `(weekday + 1) % 7` before comparing, so choosing **Mon** runs on a Monday
  (pinned by `frontend/tests/scheduledBackup.test.ts` — the unconverted
  comparison shipped a one-day-off schedule and is exactly the bug that test
  exists to catch).
- The runner ticks on AppShell mount and every `TICK_MS = 300_000` ms.
  `latest` = the newest artifact (`GET /backup/artifacts`, first item).
  Run iff `frequency !== "off"` **and** (`latest === null` or
  `now >= nextDue(latest.created_at)`). **No artifact yet ⇒ due immediately**
  (first run as soon as the schedule is on and the vault is unlocked).
- Due but vault locked ⇒ outcome `vault_locked`, **no artifact**, retried on
  the next tick. There is deliberately no server-side fallback: the server
  cannot encrypt with a key it does not have.
- Missed periods (no tab open at the due instant) are **not** backfilled one
  by one: the next open produces one artifact for the then-current period,
  and `exported_at` inside it is the real run time.
- The period label (below) is the idempotency key, so two open tabs racing
  produce one row (upsert), not two.
- DST: instants are built with local `Date` construction; the hour a DST jump
  skips or repeats is accepted as-is (documented, not handled specially).

### Artifact format (pinned)

Plaintext before encryption — one JSON object, **all of it inside the
envelope, so a hostile server cannot alter period or metadata undetected**:

```json
{
  "v": 1,
  "kind": "fintrack-backup",
  "period": "2026-09-28",
  "exported_at": "2026-09-28T03:12:44.123Z",
  "snapshot": { "accounts": [ … ], "transactions": [ … ] }
}
```

- `snapshot` is **exactly** the `GET /export/full` response body (docs/11
  step 2 — registry-derived keys, secrets stripped). Deliberately no second
  table list anywhere in the client.
- `period` regex `^\d{4}-(?:\d{2}-\d{2}|W\d{2}|\d{2})$`: daily `YYYY-MM-DD`,
  weekly ISO week `YYYY-Www` (e.g. `2026-W39`), monthly `YYYY-MM` — all in
  the runner's local time.

Ciphertext = `encryptField(JSON.stringify(payload), dek)` from
`frontend/src/lib/fields.ts` — the **existing** field envelope
`v1.<base64(iv)>.<base64(ciphertext)>`, AES-GCM-256, fresh 96-bit IV, key =
the session DEK from `vault.ts::getDEK()`. No new cipher, no second key
hierarchy; the DEK is never sent anywhere.

Wire object (`POST /backup/artifacts` body, and what a downloaded
`.ftbackup.json` file contains):

```json
{ "period": "2026-09-28", "ciphertext": "v1.YWJjZGVm....ZW5k=" }
```

`MAX_ARTIFACT_CHARS = 8_000_000` (length of `ciphertext`); the request body
stays under nginx `client_max_body_size 10m`
(`frontend/Dockerfile`). Raising the ceiling = raise both together.

### Endpoints (router `backend/app/api/backup.py`, prefix `/api/v1/backup`)

Every handler depends on `get_current_user` (demo write guard + privacy locks
apply unchanged) and `get_db`. All request bodies are `extra="forbid"`.

| Method/Path | Contract |
|---|---|
| `GET /backup/schedule` | → 200 `{frequency, hour, weekday, day_of_month, keep_last_n}` — the caller's five columns, defaults for a fresh user (`off`, 3, 0, 1, 5). |
| `PUT /backup/schedule` | `{frequency, hour, weekday, day_of_month, keep_last_n}` → 200 the same shape. Out-of-range/unknown `frequency` → **422 `backup_invalid_schedule`** (message names the field); wrong types/unknown key → 422 `validation_error`. Prunes artifacts immediately when `keep_last_n` is lowered. |
| `GET /backup/artifacts` | → 200 `{items: [{id, period, created_at, size_bytes}]}` — **metadata only, never `ciphertext`** (a list stays cheap at 8 MB/artifact), newest first (`created_at DESC, id DESC`), caller-scoped. |
| `POST /backup/artifacts` | `{period, ciphertext}` → **201** `{id, period, created_at, size_bytes}`. **Upsert on `(user_id, period)`**: a second artifact for the same period replaces the first (idempotent scheduler, and "Back up now" refreshes today's slot). Then prunes to `keep_last_n`. |
| `GET /backup/artifacts/{id}` | → 200 `{id, period, created_at, size_bytes, ciphertext}` — the row needed to decrypt/restore. |
| `DELETE /backup/artifacts/{id}` | → **204**. |
| `POST /backup/restore` | `{confirm: "restore", payload: {…decrypted artifact…}}` → 200 `{restored: {table: rows}, absent_tables: [...], skipped_tables: [...], fx_rates_inserted: n}`. |

Error codes (new; everything else is inherited — 401 `invalid_token`, 403
`demo_account_restricted`, 404 `not_found`, 422 `validation_error`):

| Status | `code` | When |
|---|---|---|
| 422 | `backup_invalid_schedule` | `PUT /backup/schedule` value out of range / unknown `frequency` |
| 422 | `backup_invalid_period` | `period` fails the regex above |
| 422 | `backup_too_large` | `len(ciphertext) > MAX_ARTIFACT_CHARS` |
| 422 | `backup_invalid_envelope` | `ciphertext` is not a 3-part `v1.…` field envelope |
| 404 | `not_found` | unknown id **or** another user's id (indistinguishable by design, same as `/vault/envelopes/{id}`) |
| 422 | `restore_confirm_required` | `confirm !== "restore"` |
| 422 | `restore_invalid_payload` | not `{v: 1, kind: "fintrack-backup", …}`, `snapshot` missing/empty/not an object of row arrays |
| 422 | `restore_unknown_table` | a `snapshot` key that is not a registered table |
| 422 | `restore_forbidden_table` | a `snapshot` key in `{refresh_tokens, mcp_tokens, alembic_version, backup_artifacts, totp_factors, recovery_codes, webauthn_credentials, oidc_links, provider_fund_registry}` — auth secrets, second-factor/SSO state, the artifact store itself, or the server-wide fund registry (M168) |
| 422 | `restore_invalid_row` | a row that fails its model's validation (message names table + index) |
| 422 | `restore_integrity_error` | FK/NOT NULL/unique violation while inserting (rolled back; nothing was destroyed) |

**Validation runs entirely before the first write**, so every 422 above
leaves the tenant untouched.

### Retention (keep-last-N, pinned semantics)

- `keep_last_n` (1–20, default 5) is read from the caller's schedule row
  **server-side at write time** — the client cannot ask for more retention
  than it configured, and a stale client cannot defeat pruning.
- Prune points: after every `POST /backup/artifacts`, and after a
  `PUT /backup/schedule` that lowers `keep_last_n`. Keep the newest
  `keep_last_n` rows by `(created_at DESC, id DESC)`; delete the rest.
- Deletion is permanent (no recycle bin) — including `DELETE` by hand. The
  response says so in the UI copy.
- Retention never touches anything else: `backup_artifacts` rows are the only
  rows M56 deletes.

### Restore (the step-7 "future import-from-backup" feature, for artifacts)

`POST /backup/restore` body: `{ "confirm": "restore", "payload": {v, kind,
period, exported_at, snapshot} }` — i.e. the **decrypted** artifact, produced
in the browser (the server cannot decrypt anything). Algorithm, in one
transaction:

1. **Validate first** (shape → registry keys → forbidden keys → every row's
   model validation). Any failure → one of the restore 422s, zero writes.
2. **Wipe** with the shared FK-safe cascade
   `app/api/users.py::delete_user_rows(db, user, keep=(RefreshToken,
   McpToken))` — the same helper the demo reset uses. Full-replacement
   semantics: a domain table the snapshot does not contain comes back
   **empty** and is named in `absent_tables`.
3. **Insert** the snapshot tables in FK-dependency order computed from
   `SQLModel.metadata` (deterministic alphabetical tie-break; a cycle fails
   closed with `restore_integrity_error`), each row rebuilt through its
   SQLModel class (`model_validate`) so ISO dates/Decimals/enums are coerced
   exactly as the API coerces them. **`user_id` is remapped to the caller's
   id on every row** — a snapshot taken before a re-registration (or from
   another account) still lands in the caller's tenant and never references
   another user's rows.
4. **Gap-fill `fx_rates`** with `INSERT … ON CONFLICT (base, quote, date) DO
   NOTHING`: shared reference data (docs/02 tenancy exception) is never wiped
   and never overwritten — manual rows keep winning, matching the standing
   fx rule.
5. **Re-advance sequences**: `setval(pg_get_serial_sequence(<table>, 'id'),
   max(id))` for every table that received rows, so the next API write does
   not collide with a restored primary key.

**Split children restore in any order — the children-first test (M107, docs/44 §10).**
`transactions.parent_split_id` is a self-FK named `fk_transactions_parent_split`,
`ON DELETE CASCADE` **and** `DEFERRABLE INITIALLY DEFERRED` (migration
`0042_split_transactions`). Rows are inserted in array order inside the one transaction
and the FK is only checked at COMMIT, so a **children-first** snapshot — the
`transactions` array with every child ordered before its parent — still restores, and
the relationships come back identical. Self-references were already tolerated here
(`transfer_pair_id`, migrations `0008`/`0010`): step 3's dependency ordering fails on a
*table* cycle, not on a self-FK.

Required test, written into the spec and implemented:
`backend/tests/test_split_transactions.py::test_export_restore_roundtrip_reconstructs_relationships`
takes the exported `transactions` array, moves every row with a non-null
`parent_split_id` to the front, restores → **200** (`restored.transactions` equals the
reordered length), re-exports, and asserts that the
`(id, parent_split_id, is_split, amount_native)` tuples **and** the whole array are
equal to the pre-restore snapshot.

Skipped by design, reported in `skipped_tables`:

| Table | Why |
|---|---|
| `users` | the account row is the identity you are restoring *into*; credentials and preferences are not content (the snapshot never carries `hashed_password`/`llm_api_key` anyway) |
| `vault_envelopes` | device enrollment is live security state: replaying an old snapshot would **un-revoke a revoked device** (docs/33 revoke runbook) |

Kept alive, never in the snapshot: `refresh_tokens`, `mcp_tokens` (auth
secrets, excluded by step 2; `keep=` above) — a restore does not sign you out
or kill MCP access. The four M74 tables (`totp_factors`, `recovery_codes`,
`webauthn_credentials`, `oidc_links`) are in the same `keep=` set
(`RESTORE_KEEP_MODELS`): they are excluded from the snapshot entirely, so
preserving the LIVE rows is the only way they survive a restore, and a
restore must never silently drop a second factor, a passkey, or an SSO link
(docs/38). `backup_artifacts` is excluded from export entirely (see
below), so your backups are never nested inside a backup.

**Demo reset (M63).** `backup_artifacts` is the one content table outside
`delete_user_rows`'s cascade — the invariant above is about *restore*, which
must keep its own source, and that reason does not apply to
`POST /auth/demo/reset`: it returns the shared demo tenant to the published
seed, so a visitor's leftover ciphertext must not outlive the reset
(cross-reset custody is not intended). The reset therefore deletes
`backup_artifacts` explicitly through
`app/services/demo_seed.py::_RESET_EXTRA_MODELS`, in the same transaction as
the cascade, and `seed_fingerprint` reads the table too — so the M50
fingerprint-equality acceptance fails if the wipe ever disappears.
Pinned by `tests/test_demo_seed.py::test_reset_clears_stale_backup_artifacts`;
the same file's
`test_every_user_owned_table_is_classified_for_demo_reset` keeps every future
`user_id` table in a named bucket (wiped, credential, extra-wiped or
deliberately surviving with a reason) instead of silently orphaning.

**Demo reset (M63b).** The `vault_envelopes` line in the table above is a
*restore* rule, not a reset rule: replaying a snapshot would un-revoke a
revoked device, whereas `POST /auth/demo/reset` wipes rather than replays. The
demo tenant cannot enrol a server-side device at all (inherited `403
demo_account_restricted`; docs/33 keeps demo enrolment local-only), so no
legitimate demo row exists — and a stray one would outlive the reset forever
while staying invisible to `seed_fingerprint`, which reads only what the reset
wipes. The reset therefore deletes `vault_envelopes` through the same
`_RESET_EXTRA_MODELS` list, in the same transaction as the cascade
(`app/services/demo_seed.py`), and `seed_fingerprint` reads the table with the
rest. **Export and restore are unchanged:** the snapshot still carries
`vault_envelopes` (it is a caller-scoped export key — the coverage invariant
below subtracts exactly `users`, `vault_envelopes` and `fx_rates` from the
export keys to get the tables restore inserts), restore still
skips it (`RESTORE_SKIP_TABLES`, reported in `skipped_tables`), and it stays
outside `delete_user_rows`'s cascade. Pinned by
`tests/test_demo_seed.py::test_reset_clears_stale_vault_envelopes`.

Coverage invariant (pinned by test): the tables restore inserts are exactly
`export keys − {users, vault_envelopes, fx_rates}` = the tables
`delete_user_rows` wipes. A future user-owned table that reaches the registry
but not `_USER_OWNED_MODELS` fails that test instead of silently producing a
duplicate-key 500 at restore time.

### Touch points

- `backend/alembic/versions/0029_backup_artifacts.py` — five `users`
  columns + CHECKs, `backup_artifacts` table (+ `uq_backup_artifacts_user_period`).
- `backend/app/models/backup.py` (+ `app/models/__init__.py`) — `BackupArtifact`.
- `backend/app/api/backup.py` — the router above, mounted in `app/main.py`.
- `backend/app/api/export.py` — add `backup_artifacts` to `EXCLUDED_TABLES`
  (reviewed list, pinned by `tests/test_export_registry.py`): exporting the
  artifact store would nest every backup inside the next one.
- `frontend/src/lib/backupArtifact.ts` — payload build/encrypt/decrypt on top
  of `fields.ts` + `vault.ts` (no new crypto).
- `frontend/src/lib/scheduledBackup.ts` — period/due math + the run step,
  injectable clock and deps.
- `frontend/src/api/backup.ts` — request helpers.
- `frontend/src/components/ScheduledBackupPanel.tsx` (mounted from
  `Settings.tsx`) — schedule form, status line, artifact list
  (Download / Restore / Delete), restore-from-file input, "Back up now", and
  the primary **"Save backup schedule"** action (M92: the name is unique on
  Settings — its export twin reads "Save export schedule", docs/09 naming
  contract).
- Runner mounted in `AppShell` (ticks with the app open).

### Verification

1. **Spec completeness (AGENTS §9)** — every path, payload field, error code,
   column, enum and invariant above exists verbatim in the code, and
   `docs/02` + `docs/08` + `docs/33` are reconciled in the same commit.
2. **Restore round trip (the acceptance test)** —
   `backend/tests/test_backup.py::test_restore_roundtrip_after_wipe`: seed a
   multi-table tenant (two tenants + `_seed_post_m7_tables` + a
   `vault_envelopes` row) → `GET /export/full` → encrypt the payload exactly
   as the client does (AES-GCM under a random DEK, `v1.` envelope) →
   `POST /backup/artifacts` → `GET` it back → **wipe the tenant's rows in the
   test DB** (shared cascade, credentials kept) → decrypt →
   `POST /backup/restore` → `GET /export/full` again → **semantic equality on
   the sampled tables** (`accounts`, `transactions`, `trades`, `categories`,
   `budgets`, `goals`, `tags`, `rules`, `prices`, `institutions`,
   `securities`, `rule_tags`, `transaction_tags`, `transaction_events`
   (M106 — a seeded receipt, unicode payload included), `vault_envelopes`,
   `fx_rates`) and the full snapshot must match byte-for-byte.
3. **Failure leaves data intact** — every restore 422 is asserted to happen
   with the tenant's rows still present (validation before wipe; rollback on
   `restore_integrity_error` / `restore_invalid_row`).
4. **Interoperability** — a fixture artifact produced by the *real* client
   code (`frontend/tests/backupFixture.test.ts`, `WRITE_BACKUP_FIXTURE=1`
   `npx vitest run tests/backupFixture.test.ts`) decrypts in Python:
   PBKDF2-SHA256/600k → unwrap DEK → `v1.` envelope → payload equals the
   recorded expectation
   (`backend/tests/test_backup.py::test_client_produced_fixture_decrypts_in_python`
   reads `frontend/tests/fixtures/backupArtifact.v1.json`; that file also
   carries `passphrase`, `salt`, `wrappedDEK` so the unwrap is the thing under
   test). M106 puts a `transaction_events` row in that snapshot: the Python
   side asserts the unicode payload and re-derives `payload_hash` from what it
   decrypted, so the receipt — nested, hashed, non-ASCII — is the cell the
   interop check exercises.
5. **Schedule math** — `frontend/tests/scheduledBackup.test.ts`: period
   labels (daily/weekly ISO-week boundary/monthly), `nextDue` for all three
   frequencies, no-artifact ⇒ due, `off` ⇒ never, vault locked ⇒ `vault_locked`
   with no POST, upsert body shape, export failure ⇒ retried next tick.
6. **Gates + stack** — AGENTS §4 in order, then the running-stack smoke:
   schedule round trip through `:8080`, artifact upload/list/download/delete,
   and a browser-level Settings walk (screenshot).

## One-click export (M75) — full export, OFX, scheduled export, portability promise

Milestone anchor: **M75**. Extends step 2 (`GET /export/full`) with the file a
human carries away. The position this feature exists to answer, stated
explicitly: the loudest anger in the user research is shutdown trauma —
"DOWNLOAD YOUR FULL TRANSACTION HISTORY OR YOU WILL NEVER HAVE ACCESS AGAIN"
(r/mintuit, 2024). The reply is not a reassurance, it is a button: one click
produces an open, documented, self-describing archive that imports into
something else, and the promise below is that the file list and schema are
published, versioned, and stable.

Four things land here, all reads, all pure Postgres (no LLM, no yfinance, no
BoC Valet — so every one works under privacy mode and on the demo tenant):

1. **One-click ZIP** — `GET /export/zip`: one CSV per table + `manifest.json`.
2. **OFX statement** — `GET /export/ofx`: one account's transactions, round-trips through our own importer.
3. **Scheduled export** — `GET|PUT /export/schedule` + a client-side runner, the M56 pattern.
4. **Portability promise** — published in `/help` (`guides/portability`) and pinned below.

### Endpoints (router `backend/app/api/export.py`, prefix `/api/v1/export`)

Every handler depends on `get_current_user` (demo write guard + privacy locks
apply unchanged) and `get_db`.

**The export surface is the caller's OWN tenant — never the household
(t_98e06dd0).** An export is a snapshot of the caller's rows, so it is not a
report population and takes no `scope` parameter: the M93 household projection
(`GET /partner/transactions`, docs/43 §6) is **not** an export source, and a
"household export" would ship exactly the rows the owner's allowlist governs.
A linked partner's `/export/full`, `/export/zip` (the one-click file AND the
payload the scheduled runner produces), and `/export/ofx` therefore carry their
own tenant only, and the owner's un-allowlisted rows are unreachable even
through the one account they CAN read elsewhere: `GET /export/ofx?account_id=`
answers **404 `not_found`** for an account the caller does not own, shared or
not. Pinned by
`backend/tests/test_reports_household.py::test_the_export_surface_is_the_callers_own_tenant`;
the Owner lens' disposition table (docs/12 §Owner dimension) records it next to
the report routes it rules out.

| Method/Path | Contract |
|---|---|
| `GET /export/full` | Unchanged by M75: the machine snapshot, **every** row (staged included), JSON object keyed by table name, `Content-Disposition: attachment; filename="fin-tracker-export-<YYYY-MM-DD>.json"`. Backups and scripting use this; humans use the ZIP. |
| `GET /export/zip?vault={locked\|unlocked}` | → **200** `application/zip`, `filename="fin-tracker-export-<YYYY-MM-DD>.zip"`, response header `X-Export-Masked-Cells: <int>` (cells written as the E2EE marker). Body = `manifest.json` first, then one `<table>.csv` per exported table. **Committed rows only** (below). |
| `GET /export/ofx?account_id=<int>&vault={locked\|unlocked}` | → **200** `application/x-ofx`, `filename="fin-tracker-<slug>.ofx"` (`slugify(account.name)` with fallback `account-<id>`). One account per statement. Unknown or another tenant's id → **404 `not_found`**. |
| `GET /export/schedule` | → 200 `{frequency, hour, weekday, day_of_month, keep_last_n}` — the caller's five columns, defaults for a fresh user (`off`, 3, 0, 1, 5). |
| `PUT /export/schedule` | `{frequency: str, hour: int, weekday: int, day_of_month: int, keep_last_n: int}` (`extra="forbid"`) → 200 the same shape. Unknown `frequency` or an out-of-range value → **422 `export_invalid_schedule`**; wrong type / unknown key → 422 `validation_error`. Full replace — a partial patch cannot express intent. |

`vault` is the session's vault state **as the caller knows it** (the client
passes it; the server does not guess). Omitting it means `locked` — the
fail-closed default, so a client that never says can never turn an opted-in
E2EE user's first export into a silent plaintext dump.

### File list (published)

```
fin-tracker-export-<YYYY-MM-DD>.zip
├── manifest.json          ← always first, describes everything below
├── <table>.csv            ← one per exported table, table name = file stem
└── …
```

- The table set is **derived**, never hand-listed: every class in
  `app.models` that maps a real `SQLModel.metadata` table, minus
  `EXCLUDED_TABLES`. A table added by a later migration is in the next export
  with no code change; `manifest.json.files[]` is the authoritative list for
  any given file.
- Excluded (the complete reviewed list, pinned by `tests/test_export_registry.py`):

| Table | Why |
|---|---|
| `refresh_tokens` | SHA-256 refresh-token hashes — auth secrets |
| `mcp_tokens` | MCP bearer-token hashes — auth secrets |
| `backup_artifacts` | client-side-encrypted backup blobs; exporting them would nest every backup inside the next one |
| `alembic_version` | alembic bookkeeping, not a model |

`mcp_audit` is deliberately **not** on that list: a receipt carries a tool
name, a status, a duration and an argument hash — no token, no password, no
argument value — so it travels with the rest of the tenant's data in both
directions (export and restore) and is wiped with the account (M80).

M90's two tables hold up the same way: `sharing_audit` is another hash-only
receipt (event, actor id, response code — never an address), and
`sharing_links.token_hash` is a sha256 digest of a capability that exists raw
only in the create `201` body and the email; once status ≠ `pending` the
capability is dead (docs/43 §10).

- Everything else on the `users` row travels, including the JSON preference
  columns — `saved_report_views` (M108 P2) and `forecast_scenarios` (M109,
  docs/45) both ride inside `users.csv` with **no registry change**, which is
  the derived-table-set promise doing its job: a later column needs no export
  code. Scenarios therefore leave and re-enter through the ordinary
  export/restore path like any other user content.
- Stripped from the `users.csv` header (not merely blanked — a column that is
  always empty is a lie about the schema): `hashed_password`, `llm_api_key`.
- NOT in the ZIP: `GET /export/ofx` statements (one per account, fetched
  separately — a ZIP spanning every account could never re-import clean) and
  anything staged (next section).

### Manifest schema (`manifest.json`, `v: 1`, `kind: "fintrack-export"`)

```json
{
  "v": 1,
  "kind": "fintrack-export",
  "generated_at": "2026-09-30T12:00:00.000Z",
  "user": { "id": 7, "email": "you@example.com" },
  "vault": "locked",
  "visibility": "committed",
  "format": {
    "csv_encoding": "utf-8", "csv_delimiter": ",",
    "csv_line_terminator": "\n", "csv_null": "",
    "boolean": "true | false",
    "date": "ISO-8601 (YYYY-MM-DD)",
    "decimal": "exact string (no float rounding)"
  },
  "markers": { "e2ee": "E2EE:<v1 base64(iv).base64(ciphertext)> — …" },
  "masked_cells": 0,
  "visibility_filters": { "transactions": "committed batches + manual rows only (staged rows excluded)" },
  "excluded_tables": { "refresh_tokens": "…" },
  "files": [ { "name": "accounts.csv", "table": "accounts", "rows": 6, "columns": ["id", "…"] } ],
  "ofx": { "endpoint": "GET /api/v1/export/ofx?account_id={account_id}", "note": "…" }
}
```

Field types: `v` int, `generated_at` ISO-8601 string, `vault` `locked|unlocked`,
`visibility` `"committed"`, `masked_cells` int, `files[]` objects with
`name`/`table` strings, `rows` int, `columns` string array (the exact CSV
header, in order). The manifest is the file's own documentation: a reader who
has only the ZIP can reconstruct the schema without this doc.

### CSV dialect (pinned)

RFC-4180 writer, LF line terminator, UTF-8, no BOM.

| Cell | Rendering |
|---|---|
| `null` | empty |
| `true` / `false` | `true` / `false` |
| dict / list | compact JSON (`ensure_ascii=false`) |
| date/datetime | ISO-8601 (`YYYY-MM-DD`, `…Z`) |
| `Decimal` | **exact string** (`"12.34"` — never a float) |
| other | `str(value)` |

Header = the model's own field order (its column order), minus
`STRIPPED_COLUMNS`. Rows are produced by `model_dump(mode="json")`, so
Decimals, dates and enums arrive already in JSON form — full ledger precision
survives the file.

### Visibility (what the ZIP filters out)

The ZIP mirrors what the app shows (docs/03 staging); `/export/full` keeps
every row because a backup restores what the database holds. The split is
deliberate, and the filters are published in the manifest:

| Table | Filter | Single source |
|---|---|---|
| `transactions` | committed batches + manual rows — **unchanged by M107: split parents AND their children both ship** (the round trip needs both sides) | `services/txn_filters.py::committed_tx_conditions` |
| `trades` | committed batches + manual rows | `services/trade_filters.py::committed_trade_conditions` |
| `import_batches` | `status = committed` | `ImportBatchStatus.committed` |
| `transaction_tags` | tags of committed/manual transactions only | subquery over the same predicate |
| `transaction_events` | receipts of committed/manual transactions **+ receipts whose row no longer exists** (M106) | `tx_id IN (visible)` OR `tx_id NOT IN (any transaction)` — i.e. the row is gone *everywhere*, not merely "not this tenant's": `tx_id` has no FK, so a receipt naming a row that exists (staged here, or owned by another tenant) is not a surviving witness and must not ship. The `deleted` receipt is the one event whose whole purpose is to outlive its subject, so dropping it here would quietly stop "edits never vanish" at DELETE |
| `transaction_receipts` | receipt **images** of committed/manual transactions only (M108 P4) | `tx_id IN (visible)` — deliberately NOT the `transaction_events` rule: a picture of a ledger line has no purpose once the line is gone, and unlike an activity receipt it was never the record that the row existed |

**Splits ship whole (M107, docs/44 §10) — no split population rule applies here.**
M107 deliberately left `committed_tx_conditions` untouched for exactly this reason, so
the `transactions` rows in the ZIP **and** in `/export/full` carry split PARENTS and
CHILDREN together: a restore that dropped either side would orphan `parent_split_id`,
and `is_split` / `split_checksum` come back with the rows they describe. Consequence
worth stating plainly, because docs/44's touch-point table says "CSV → LEDGER": **this
codebase has no separate transaction-CSV route.** Every CSV ships inside
`GET /export/zip` as `transactions.csv`, rendered from this same snapshot — a faithful
table dump (its `is_split` / `parent_split_id` columns included), not a report. The
LEDGER rule (`countable_tx_conditions`) governs the **report** CSVs
(`/reports/…&format=csv`, docs/12), which sum children under their own categories. The
one export that does pick a population rule is `GET /export/ofx`, and it picks IDENTITY
(parents only), so a split statement keeps its line count and re-imports to `0 net-new`.

**E2EE vault (M106).** `transaction_events` needs no special handling for POPULATION:
`GET /export/full` is registry-derived, so the table's rows ride into the
encrypted browser backup (`lib/backupArtifact.ts`, `lib/privacyBackup.ts`) with NO
visibility filter — a backup restores what the database holds, receipts included. The ZIP
applies the filter above instead: a staged row's receipts are as invisible as the row, a
receipt whose row is GONE still ships, and a receipt naming a row that exists anywhere
else does not.

Two different "encryption" questions get two different answers, stated separately so
neither is taken for the other:

- **Field envelope (`vault=locked`): NOT covered, by design.**
  `transaction_events.payload` has **no ciphertext column**, so `CIPHER_FIELDS` cannot
  withhold it and a locked ZIP ships it exactly as stored — `changes.amount_native`,
  `normalized_payee` and `note` values included. That is docs/33's *server-readable
  plaintext TODAY* posture (the entry is listed there), not an exception to the marker
  rule: only cipher-backed fields are ever masked, and a cell is never fabricated.
  `transactions.amount_native` masked next to a receipt that quotes the same number is
  therefore possible — which is precisely why the PARTNER projection (docs/43 §6)
  strips payloads outright rather than relying on the mask.
- **Backup artifact: covered — the WHOLE snapshot is the plaintext.**
  `encryptArtifact` seals every table the registry emits, receipts included. Pinned by
  `tests/test_backup.py::test_restore_roundtrip_after_wipe` (seeds a receipt, uploads the
  artifact, decrypts it server-side with an independent implementation, wipes the tenant,
  restores, byte-compares the snapshot) and by
  `::test_client_produced_fixture_decrypts_in_python`, which unwraps the fixture the real
  browser code wrote (`frontend/tests/fixtures/backupArtifact.v1.json` — its snapshot
  carries a `transaction_events` row with a unicode payload) and checks the payload and
  its `payload_hash` survive the envelope.

**Receipt images (M108 P4) ride the same way.** `transaction_receipts` is
registry-derived like everything else, and its `data_base64` column is TEXT
rather than `bytea` for exactly the reason this section exists: `/export/full`,
the ZIP manifest and the encrypted browser backup all render rows through
`model_dump(mode="json")` + `json.dumps`, and a `bytea` column would turn a
fresh export — and therefore the docs/11 restore guarantee — into a 500. The
cost is ~1.37x the bytes on disk; the alternative, a per-column special case in
`_jsonable` plus a matching one on the restore path, is a branch that drifts.
The image therefore ships in **both** directions: it is in the backup the
user takes, and it is in the ZIP, under the committed-only filter above.

Scoping is the step-2 contract, unchanged: `user_id` filter where the column
exists → `fx_rates` shipped UNFILTERED (shared reference cache, docs/02) →
junction tables (`rule_tags`, `transaction_tags`, `bill_reminder_match_terms`)
through their owning parent (rule / transaction / bill).
A table with no scoping rule fails closed with `500 export_missing_scoping`;
a metadata table no model class resolves to fails closed with `500
export_registry_drift` — never a silently shorter file.

### Export & the vault (E2EE): ciphertext + marker, never fake plaintext

`vault=locked` (the default) withholds a **cipher-backed** field's plaintext
and writes the marker instead; `vault=unlocked` ships the stored plaintext.
The mapping (`services/export_bundle.py::CIPHER_FIELDS`) is exhaustive — a
field not in this table has no ciphertext column and always ships as stored:

| Table | Plaintext column → ciphertext column |
|---|---|
| `transactions` | `raw_description` → `raw_desc_cipher` |
| `transactions` | `normalized_payee` → `payee_cipher` |
| `transactions` | `amount_native` → `amount_cipher` |
| `transactions` | `cad_amount` → `cad_amount_cipher` |
| `accounts` | `name` → `name_cipher` |

Rules, all binding:

- A cell becomes `E2EE:<stored ciphertext>` **only** when `vault=locked` AND
  that row's cipher column is non-empty. Never a placeholder, never a
  fabricated value, never a downgrade the reader could mistake for a payee.
- `vault=unlocked` ships plaintext — docs/33's honest status: the server can
  still read today's plaintext columns, so there is no server-side decryption
  step and none is pretended.
- A table with **no** cipher column ships as stored whether the vault is locked
  or not — `transaction_events` (receipt payloads), `trades`, `budgets`,
  `forecast_scenarios` … docs/33 § *Server-readable plaintext TODAY* is the
  honest inventory of those fields; nothing in that list is masked here, and
  none of it is described as encrypted.
- The withheld-cell count is reported twice: response header
  `X-Export-Masked-Cells: <n>` and `manifest.masked_cells`.
- The OFX `<NAME>` carries the same rule (below).
- An unknown `vault` value → `422 export_invalid_vault`.

### OFX round trip

`GET /export/ofx` renders an **OFX 1.02 (SGML) bank statement** of one
account's committed transactions:

- Header: `OFXHEADER:100 / DATA:OFXSGML / VERSION:102 / SECURITY:NONSEC /
  ENCODING:UTF8…` then one `<BANKMSGSRSV1><STMTTRNRS>` statement.
- `<ACCTID>fintrack-<account_id>`; `<CURDEF>` = account currency;
  `<DTSTART>`/`<DTEND>` = min/max transaction date (`YYYYMMDD`).
- `<ACCTTYPE>`: `chequing→CHECKING`, `savings→SAVINGS`,
  `credit_card→CREDITCARD`, `line_of_credit→CREDITLINE`, **everything else
  `OTHER`** — OFX 1.02's DTD has no brokerage/registered value, so this is a
  documented deviation, not a lie about the account kind (our importer
  ignores `ACCTTYPE` entirely).
- Per transaction: `TRNTYPE` `CREDIT`/`DEBIT` from the amount's sign,
  `DTPOSTED`, `TRNAMT` signed to 2 places, `FITID` =
  `fintrack-<account_id>-<transaction.id>`, `NAME` = `raw_description`
  collapsed to single spaces (whitespace-neutral for `build_dedup_hash`, so
  the dedup hash recomputes byte-identical on re-import) — or the
  `E2EE:<cipher>` marker when `vault=locked`.
- `<`, `&`, `>` are written as SGML entities; `ofxparse` decodes them back to
  the exact characters, so the round trip reproduces the stored description.
- Round trip (the acceptance test): `detect_importer(raw)` → `OfxImporter`
  → `POST /imports` upload → `imported: 0`, `duplicates_skipped: N`, every
  preview row `duplicate: true` → commit → the committed-id set is unchanged.

### Scheduled export

The M56 pattern mirrored exactly — five `users` columns that roam across
devices, enforced again by DB CHECKs — except the artifact is a **plaintext
ZIP stored on the user's own machine** (IndexedDB), never a server-side copy.
Migration `0032_export_schedule` (5 columns, 5 CHECKs, no table):

| Column | Type | Invariants |
|---|---|---|
| `export_frequency` | `VARCHAR(16) NOT NULL DEFAULT 'off'` (`ExportFrequency` StrEnum, `sa.Enum(native_enum=False, length=16)`) | one of `off`, `daily`, `weekly`, `monthly` (`ck_users_export_frequency`) |
| `export_hour` | `SMALLINT NOT NULL DEFAULT 3` | 0–23, **local time of the machine running the tab** (`ck_users_export_hour`) |
| `export_weekday` | `SMALLINT NOT NULL DEFAULT 0` | 0–6, **0 = Monday** — the column's encoding, not JS `getDay()` (`ck_users_export_weekday`) |
| `export_day_of_month` | `SMALLINT NOT NULL DEFAULT 1` | 1–28 (monthly only) (`ck_users_export_day_of_month`) |
| `export_keep_last_n` | `INTEGER NOT NULL DEFAULT 5` | 1–20, local retention (`ck_users_export_keep_last_n`) |

The schedule is an **independent setting** from `backup_*`: a user can run one
and not the other, but the two shapes are pinned identical so the Settings
pickers and this doc read side by side.

**Due semantics** (shared with M56 via `frontend/src/lib/scheduleMath.ts`, so
the weekday-encoding bridge exists exactly once):

- `nextDue(after)` = first local instant strictly after `after` matching the
  frequency at `hour:00`. `isDue` = `frequency !== "off"` AND
  (`latest === null` OR `now >= nextDue(latest.createdAt)`).
  **No record yet ⇒ due immediately.**
- Runner: `ScheduledExportRunner` mounted in `AppShell` whenever a user is
  signed in, re-ticking every `EXPORT_TICK_MS = 300_000` ms.
- `latest` = the newest record in this browser's local store (metadata only).
- A **locked vault does not block a run**: the runner fetches
  `/export/zip?vault=<this session's state>`, so a locked vault produces an
  honest ciphertext-marker archive — exactly what the app itself would render
  for that session. (M56 differs by necessity: it cannot encrypt without the
  key, so its outcome is `vault_locked`.)
- Missed periods are not backfilled one by one: the next open tab produces
  one archive for the then-current period.
- Period key = idempotency key, so two open tabs converge on one record
  (upsert): daily `YYYY-MM-DD`, weekly ISO week `YYYY-Www` (e.g. `2026-W39`),
  monthly `YYYY-MM` — all in the runner's **local** time.
- Outcomes: `off`, `skipped`, `stored`, `export_failed`, `store_failed`,
  `storage_unsupported`. The first two write nothing; `export_failed` /
  `store_failed` are retried on the next tick; `storage_unsupported`
  (IndexedDB missing/blocked) is terminal for the tab and the panel says so.

**Storage & retention (local, pinned):** IndexedDB database
`fintrack-exports`, store `exports`, one record per user under key
`user:<id>` holding that user's array sorted newest-first (a shared browser
never shows one account's archives under another's session). `putLocalExport`
upserts by `period` then keeps the newest `keep_last_n`; `deleteLocalExport`
is permanent — no recycle bin, and the UI copy says so. Nothing about the
scheduled archive reaches the server.

**UI** (`frontend/src/components/ScheduledExportPanel.tsx`, mounted from
Settings under Privacy): the schedule form (frequency / hour / weekday /
day-of-month / keep-last-N) + **"Save export schedule"** (M92: unique on
Settings — the backup form's twin reads "Save backup schedule", docs/09 naming
contract), an **Export now** button (force run), and the archive list with
per-item Download and Delete. The lock-status line reads the ONE session
subscription — `components/e2ee/useUnlock.ts::useVaultUnlocked`, which follows
the `vaultchange` event `lib/vault.ts` dispatches — rather than keeping a
private `vaultchange` listener of its own (docs/33 rule 7c), so lock/unlock
updates it instantly with no network call. `vaultState()` (the same
`isUnlocked()` the export engine reads) stays: it is what a run stamps into
`X-Export-Masked-Cells`.

### Error table (M75)

Everything else is inherited (401 `invalid_token`, 403
`demo_account_restricted`, 404 `not_found`, 422 `validation_error`):

| Status | `code` | When |
|---|---|---|
| 422 | `export_invalid_vault` | `vault` not one of `locked`, `unlocked` (`/export/zip`, `/export/ofx`) |
| 422 | `export_invalid_schedule` | `PUT /export/schedule` unknown `frequency` or value out of range (message names the field) |
| 500 | `export_registry_drift` | a `SQLModel.metadata` table no importable model class resolves to — it could not be selected, so it would ship missing |
| 500 | `export_missing_scoping` | a table with no `user_id`, not in `UNSCOPED_TABLES`, not a junction — refusing to export it unfiltered |

### Demo tenant

All four paths are reads, so the demo user gets them: `GET /export/zip`,
`GET /export/ofx` and `GET /export/schedule` return **200** with real rows;
`PUT /export/schedule` returns **403 `demo_account_restricted`** (the demo
write guard covers mutations only) — pinned by
`tests/test_export_zip_demo.py`. **M185 (Ledger, docs/47 §4 W1):** the product
UI mirrors this exactly — a `GET` export control is never locked, disabled or
portrayed as a write on the demo tenant; only the `PUT /export/schedule`
write may carry the read-only affordance. Server contract unchanged.

### Touch points

- `backend/app/api/export.py` — `/zip`, `/ofx`, `/schedule` + `EXCLUDED_TABLES`,
  `UNSCOPED_TABLES`, `VIA_PARENT`, `VISIBILITY_FILTERS`, `STRIPPED_COLUMNS`,
  `collect_rows(committed_only=…)`.
- `backend/app/services/export_bundle.py` — `render_csv` / `render_manifest` /
  `render_ofx` / `build_zip` / `E2EE_MARKER` / `CIPHER_FIELDS` / `slugify` /
  `validate_vault` (pure: no session, no network).
- `backend/app/schemas/export.py` — `ScheduleRead` / `ScheduleUpdate`.
- `backend/app/models/users.py` — `ExportFrequency` + five `export_*` columns.
- `backend/alembic/versions/0032_export_schedule.py` — columns + CHECKs.
- `frontend/src/api/export.ts` — `downloadZipExport`, `fetchOfx`/`downloadOfxExport`,
  `getExportSchedule`/`putExportSchedule`.
- `frontend/src/lib/scheduleMath.ts` — shared `periodFor` / `nextDue` / `isDue`
  (extracted from `scheduledBackup.ts`, which re-exports them).
- `frontend/src/lib/scheduledExport.ts` — the run step (injected clock/deps).
- `frontend/src/lib/localExports.ts` — IndexedDB store + keep-last-N.
- `frontend/src/components/ScheduledExportPanel.tsx` — panel + runner; mounted
  from `Settings.tsx` (Privacy) and `AppShell.tsx` (runner).
- `frontend/src/pages/helpGuides.ts` + `site/guides/portability.html` — the
  published promise (docs/32 copy-drift rule: edit both).

### Verification (M75)

1. **Spec completeness (AGENTS §9)** — every path, payload field, error code,
   column, enum and invariant above exists verbatim in the code; `docs/02`,
   `docs/08` and `docs/32` are reconciled in the same commit.
2. **ZIP contents** — `tests/test_export_zip.py::test_zip_has_manifest_plus_one_csv_per_exported_table`
   + `::test_zip_row_counts_match_the_database`: the archive has
   `manifest.json` plus exactly one CSV per exported table, and each file's
   row count equals the committed, tenant-scoped DB count.
3. **Staging** — `::test_zip_trades_exclude_staged_rows`: a `pending_review`
   batch's trades are absent; the committed sibling's are present.
   `::test_zip_transaction_events_follow_the_row_and_keep_the_model_shape`
   (M106) does the same for receipts: staged and other-tenant receipts absent,
   the `deleted` receipt (row gone) present, the header equals the model's
   field order, and the `payload` cell parses back to the exact object —
   unicode and `payload_hash` intact — while `/export/full` still carries all
   of them.
4. **E2EE honesty** — `::test_locked_vault_ships_the_marker_never_the_plaintext`:
   with `vault=locked` every cipher-backed cell reads `E2EE:…` and the
   plaintext string appears nowhere in the archive; `masked_cells` matches the
   `X-Export-Masked-Cells` header. `::test_unknown_vault_state_is_a_422_envelope`
   pins the envelope code.
5. **OFX round trip** — `tests/test_export_ofx.py::test_ofx_round_trip_through_our_importer_creates_zero_net_new`:
   export → `detect_importer` → our parser → upload → `imported: 0`,
   `duplicates_skipped: 2` → commit → committed-id set unchanged.
6. **Schedule** — `tests/test_export_schedule.py`: defaults
   (`off`/3/0/1/5), round trip per tenant, 422 `export_invalid_schedule` for
   unknown frequency and every out-of-range field, `validation_error` for an
   extra key, 401 without a token.
7. **Demo** — `tests/test_export_zip_demo.py`: zip/ofx/schedule GET all 200
   with real rows, schedule PUT 403.
8. **Client math & storage** — `frontend/tests/scheduledExport.test.ts`
   (off ⇒ never, no record ⇒ due, due ⇒ exactly one fetch+store, locked vault
   still runs, export failure ⇒ retried next tick) and
   `frontend/tests/localExports.test.ts` (upsert by period, keep-last-N,
   per-user keying, delete).
9. **Gates + stack** — AGENTS §4 in order, then a running-stack smoke:
   Download ZIP through `:8080` and count files/rows against the DB, OFX
   download → re-upload ⇒ 0 net-new, schedule PUT/GET round trip, and a
   browser-level Settings walk (screenshot).

## What leaves the machine when privacy mode is off (complete egress inventory)

Authoritative list, derived from a repo-wide grep of
`httpx|requests|urlopen|yfinance|socket` across `backend/` (2026-09-23;
destinations 4-5 added 2026-09-28 with M59; destination 4 gained the M90 invite mail 2026-10-02;
destination 2b (fund-issuer hosts) and the conditional destination 7 (OIDC IdP) added 2026-11-10 after
the trust-page privacy review found the original six-item list incomplete).
Outbound HTTP exists in exactly these places — AGENTS.md §2 carries the same
invariant (its "only `services/fx.py` and `services/market_data.py`" line needs
the `services/notify_delivery.py` and `services/oidc.py` amendments, pending a human edit of
AGENTS.md). **M75 adds no destination**: `/export/zip`, `/export/ofx` and
`/export/schedule` are pure Postgres reads and the scheduled archive is
written to the browser's own IndexedDB. With privacy mode ON, destinations 1, 2 and 2b are sealed by the
ContextVar lock, destination 3 by the loopback-at-send-time rule in step 5,
and destinations 4-5 by the channel gate below, destination 6 by the sync
egress guard — and destination 7 (OIDC sign-in) exists only where an operator
configured `OIDC_ISSUER`, runs before any session exists and carries no
financial data — nothing else leaves.

1. **Bank of Canada Valet** — `GET https://www.bankofcanada.ca/valet/observations/FX{pair}/json` (the `/valet/rates/…` path 404s — live-probed 2026-09-24)
   with `start_date` / `end_date` params (`backend/app/services/fx.py:55,146-153`).
   Leaves: currency pair + date range. Guarded by
   `services/fx.py:107 _guard_external_fetch`. **M150 adds a second caller
   that is not a request**: the in-process rate-book refresh worker
   (`services/fx_refresh.py`) hits the same URL on a timer for the STATIC
   `CURATED_FX_BASES` list — deliberately never derived from tenant rows, so
   the tick's egress still carries no tenant data. It runs with no request
   context, so the ContextVar lock does not apply to it; instead it is skipped
   entirely while **any** user has `privacy_mode` on (`fx_refresh.privacy_blocks`
   — `fx_rates` is shared with no `user_id`, so a per-user flag cannot be
   honoured pair by pair, and privacy wins over freshness). Operators can also
   set `FX_REFRESH_ENABLED=false` to silence this destination outright; the
   UI's FX staleness chip (docs/09) is what makes either state visible.
2. **Yahoo Finance (via the `yfinance` library)** — symbol autofill and price
   history (`backend/app/services/market_data.py:94,104,125-131`), reached
   through `/securities/lookup`, price backfills and refreshes. Leaves: ticker
   symbols + requested date ranges (plus whatever the `yfinance` library
   itself transmits). Guarded by `services/market_data.py:54 _guard_external_fetch`.
2b. **Fund-issuer hosts — iShares, Vanguard US, Vanguard CA** (M168 registry
   discovery, `backend/app/services/market_data.py`) — first-encounter fund
   look-ups for a ticker the local `provider_fund_registry` has never
   answered (a registered hit or miss makes **no** network call):
   `GET https://www.ishares.com/us/products/etf-investments` plus the
   per-product `/latest-holdings.csv` (`market_data.py:1907,1950,2038`);
   `GET https://investor.vanguard.com/irr/funds/profile/{TICKER}-AdditionalFundData`
   (`:2145,2575`); `GET https://www.vanguard.ca/en/product.model.json` plus
   `POST https://www.vanguard.ca/gpx/graphql` (`:2190,2266,2626`).
   Leaves: ticker symbols and fund identifiers (portIds) only — the same
   class of request as destination 2; never amounts, holdings or account
   data. Guarded by the same `market_data.py:54 _guard_external_fetch`, so
   privacy mode seals it exactly like destinations 1-2.
3. **The user-configured LLM endpoint** — `POST {base_url}/chat/completions`
   (`backend/app/llm/base.py:59-64`) with an `Authorization` header carrying
   the applicable API key (user key, else env `LLM_API_KEY` — `llm/base.py:80`).
   Payload per trigger:
   - `POST /suggestions/run` → full category-name list, ≤10 few-shot
     `normalized_payee => category` examples, and ≤25
     `transaction_id: normalized_payee` pairs
     (`backend/app/services/categorization.py:34,37,220-242`);
   - `POST /imports/suggest-profile` → CSV headers + up to 5 sample data rows
     verbatim (`backend/app/services/column_mapping.py:56-57,345-400`);
   - `POST /users/me/llm-test` → the fixed ping text
     (`backend/app/api/users.py:108-111`);
   - `POST /assistant/ask` → the question (1–2000 chars) plus the bounded
     per-tenant context JSON: accounts, holdings, ≤50 recent committed
     transactions, categories and plan scenarios
     (`backend/app/services/assistant.py::build_context`, docs/57 §3) — the
     fourth and last LLM trigger; **no new destination**, same endpoint and
     same `assert_send_allowed` seal as 1–3.
   Sealed under privacy mode by the step-5 loopback rule.
4. **Notification delivery — SMTP relay** (M59, `services/notify_delivery.py`)
   — `smtplib` conversation with `NOTIFY_SMTP_HOST:NOTIFY_SMTP_PORT` carrying
   the email channel's `target`. Leaves: the constant subject
   (`fin_tracker: you have a new notification`), `alert_type`, `severity`,
   `occurred_at`, `item_key`, `link`, and the recipient address — **never**
   `title`, `body`, `amount_cad` or any budget/bill/security name (docs/37
   "What is sent"). **Sealed by default under privacy mode**: this destination
   is unreachable unless the user explicitly enabled the `email` channel, and a
   `privacy_mode` user must additionally have `privacy_opt_in_at` set on that
   row (`409 privacy_opt_in_required` otherwise). The **M90 household invitation**
rides this same destination (`services/notify_delivery.py::send_invite_email`):
subject + the one-time `accept_url` + the recipient address — and ONLY when the
OWNER's `privacy_mode` is off. The caller gates it (not a channel row), so
`email_sent: false` covers privacy mode; SMTP-unconfigured downgrades the same
way and is never an error (docs/43 §9).
5. **The user's own webhook target** (M59, `services/notify_delivery.py`) —
   `POST {target}` with the same five-field body, HMAC-SHA256-signed over
   `timestamp.body` with a per-user key derived from `NOTIFY_WEBHOOK_SECRET`
   (fallback `JWT_SECRET`). Target is validated against the address policy at
   write *and* send time (loopback always allowed; RFC1918/link-local/etc.
   only with `NOTIFY_WEBHOOK_ALLOW_PRIVATE=true`). **Same default seal as
   destination 4**: an enabled `webhook` channel plus, under privacy mode, the
   per-channel opt-in.
6. **Bank sync (M79, docs/39)** — a registered provider adapter's HTTP calls,
   reached ONLY through `services/sync/service.py::_guard_sync_egress`, which
   reuses the same request-scoped `privacy_mode` ContextVar that seals
   destinations 1 and 2. `list_institutions`, `create_agreement`, `pull` and
   `revoke` all run it; `GET /sync/providers` is a registry read and does not.
   Leaves: the opaque tenant id, the local account ids + currency + type, the
   consent it was issued, and whatever date range the adapter asks for — never
   email, account names or PII (docs/39 § Data that crosses the seam). No
   credential is stored today (no accredited adapter exists). The shipped
   provider is a **no-network stub** (`services/sync/stub.py` — zero outbound
   call sites in `services/sync/`), so destination 6 makes no network calls
   at all today; the guard and the seam exist for when an accredited adapter
   lands.
7. **The configured OIDC identity provider** (conditional, M74, docs/38,
   `backend/app/services/oidc.py`) — only on instances where the operator set
   `OIDC_ISSUER` (empty by default; unset on the hosted demo). Three calls:
   `GET {issuer}/.well-known/openid-configuration` (discovery, cached an
   hour), `POST` to the discovery document's `token_endpoint`
   (authorization-code exchange carrying the code + `client_id`/
   `client_secret`), and `GET {jwks_uri}` (signing keys). Leaves: the
   sign-in code exchange and standard OIDC parameters — never financial
   data. Not a privacy-mode destination: it runs at sign-in, before any
   request identity exists, and is unreachable without operator config.
8. **Not external**: `backend/mcp_server/` speaks HTTP only to
   `FIN_TRACKER_URL` — the backend itself, default `http://localhost:8000`
   (`mcp_server/client.py:23`, `mcp_server/auth.py:29-31,45-53`). MCP tools
   that trigger market/LLM fetches do it through destinations 1-3 in the
   backend, behind the same guards.

## Critical files & anchors

- `backend/app/models/users.py` — step 1 exposes the existing preference
  columns (no schema change for step 1); **M56 adds five `backup_*` columns**
  (migration `0029_backup_artifacts`).
- `backend/app/api/export.py` — `GET /export/full` endpoint and router; M56
  adds `backup_artifacts` to `EXCLUDED_TABLES`.
- `backend/app/api/backup.py` (M56) — schedule, artifact list/upload/download/delete,
  restore.
- `backend/app/models/backup.py` (M56) — `BackupArtifact`.
- `frontend/src/pages/Settings.tsx` — privacy toggle, export button, backup controls, LLM provider gating; mounts `ScheduledBackupPanel` (M56).
- `frontend/src/lib/privacyBackup.ts` — IndexedDB + Web Crypto backup utilities (step 4, unchanged by M56).
- `frontend/src/lib/backupArtifact.ts` + `frontend/src/lib/scheduledBackup.ts` (M56) — artifact envelope + schedule/run math.
- `frontend/src/api/client.ts` (or existing API layer) — `downloadExport()` helper.
- **M75**: `backend/app/services/export_bundle.py` (CSV/manifest/OFX/ZIP
  renderers, `E2EE_MARKER`, `CIPHER_FIELDS`), `backend/app/schemas/export.py`,
  `backend/alembic/versions/0032_export_schedule.py` +
  `users.export_*`/`ExportFrequency`;
  `frontend/src/api/export.ts`, `frontend/src/lib/{scheduleMath,scheduledExport,localExports}.ts`,
  `frontend/src/components/ScheduledExportPanel.tsx` (Settings + AppShell runner),
  `frontend/src/pages/helpGuides.ts` + `site/guides/portability.html`
  (the published promise).

## Verification

1. **Backend export smoke test**: authenticate, create one institution, one account, and one transaction, call `GET /api/v1/export/full`, assert the response contains the transaction under `transactions` and the caller's row under `users` WITHOUT `hashed_password` or `llm_api_key`.
2. **Privacy-mode LLM lockdown**: enable `privacy_mode` via `PATCH /users/me`, attempt to set `llm_provider=openai_compatible`, assert `409` with code `privacy_mode_requires_local_llm`; set `llm_provider=ollama` and assert success.
3. **Frontend E2E in browser**: log in, open Settings, click "Export my data", verify a `.json` file downloads and contains the expected table keys; enable "Keep encrypted backup", enter a passphrase, refresh the page, verify the toggle remains enabled and the timestamp is recent; disable the toggle, refresh, verify backup removed.
4. **UI inspection with privacy mode on**: confirm the LLM provider dropdown shows only Ollama, cloud key/base fields are hidden, and external market-data buttons are disabled.
5. **LLM egress contract (target — run at integration)**: (a) privacy ON →
   `POST /users/me/llm-test` with `llm_base_url=https://api.openai.com/v1`
   (and again with any stored cloud URL after enabling privacy) must fail with
   an error envelope while a loopback listener records **zero** connections;
   enabling privacy via Settings must clear the stored cloud `llm_base_url` in
   `GET /users/me`. (b) privacy OFF → `llm_base_url=http://10.0.0.1/v1` (or
   any RFC1918/link-local host) is rejected; accepted only with
   `LLM_ALLOW_PRIVATE_HOSTS=true` or when the host equals the operator's env
   `LLM_BASE_URL` host. (c) With no user key set and a non-operator
   destination, assert the outbound `Authorization` header never carries the
   env `LLM_API_KEY`. (d) Induce an upstream 500 → response is a generic 502
   `{detail, code}` envelope with no status/URL echoed.
6. **Scheduled backup & restore (M56)**: the six items in
   § "Scheduled backup & restore (M56) → Verification" — restore round trip
   with wipe → restore → semantic equality, failure-leaves-data-intact,
   client↔Python interop fixture, schedule math, error codes, running-stack
   smoke.
7. **One-click export, OFX, scheduled export (M75)**: the nine items in
   § "One-click export (M75) → Verification" — ZIP file list + row counts
   against the DB, staged rows excluded, locked-vault marker with no
   plaintext leak, OFX round trip through our own importer ⇒ 0 net-new,
   schedule error codes, demo reads 200 / schedule write 403, client due
   math + IndexedDB retention, and the running-stack smoke through `:8080`.

## Assumptions & contingencies

- **Assumption**: the JSON snapshot (`GET /export/full`) stays the *machine*
  format — it is what the encrypted backup stores and what `POST
  /backup/restore` consumes, staged rows included. The human formats added by
  M75 are separate and additive: the CSV/JSON **ZIP** (committed rows, this
  doc § "One-click export (M75)") and the per-account **OFX** statement. No
  format selector is needed because each format has one job; a SQLite export
  would be future work if a consumer ever asked for it.
- **Assumption**: The browser backup is per-browser and per-user, not synced across devices. The downloadable export is the durable copy; the browser copy is a convenience backup.
- **Contingency**: If a snapshot exceeds IndexedDB single-object limits, split the ciphertext into chunked records keyed by table. Implement only if smoke testing with a large dataset fails.
- **Contingency**: If Web Crypto is unavailable (non-secure context or very old browser), allow only unencrypted download/export and show an explanatory message; do not disable the export feature.
- **Assumption (M56)**: the schedule needs an open tab — there is no
  server-side scheduler and there cannot be one while encryption is
  client-side. Missed periods produce one catch-up artifact, not a backlog
  (see the due-semantics rules).
- **Assumption (M56)**: `MAX_ARTIFACT_CHARS = 8_000_000` covers real
  tenants (≈6 MB of export JSON). A tenant that outgrows it fails closed with
  `422 backup_too_large` and the documented lever is raising it together with
  nginx `client_max_body_size`.
- **Contingency (M56)**: restoring an artifact taken before a schema change
  that added a required column fails with `422 restore_invalid_row` /
  `restore_integrity_error` — rolled back, data intact. Re-taking the backup
  on the current schema is the fix; silent partial restore is deliberately
  not offered.
- **Assumption (M75)**: the scheduled export needs an open tab, exactly like
  M56 — there is no server-side scheduler, and there cannot be one that
  writes a *plaintext* archive somewhere the user does not control. Missed
  periods produce one catch-up archive, not a backlog.
- **Assumption (M75)**: archives stored by the scheduler are **local and
  disposable** — IndexedDB is wiped by "clear site data" and is per-browser.
  The durable copy is the ZIP the user downloads; the stored list is a
  convenience. That asymmetry is why `keep_last_n` is enforced locally and
  why the panel labels deletion as permanent.
- **Assumption (M75)**: `E2EE:` marker cells are only produced for fields
  with a real ciphertext column (`CIPHER_FIELDS`). A tenant that has not
  enrolled E2EE has empty cipher columns, so `masked_cells` is 0 and every
  cell is its stored value — the marker never appears "just in case".
- **Contingency (M75)**: OFX 1.02 has no brokerage/registered `ACCTTYPE`, so
  non-bank accounts export as `OTHER`. If a target tool refuses `OTHER`, the
  lever is the mapping table in `services/export_bundle.py::_OFX_ACCOUNT_TYPES`
  — never a false `CHECKING` claim.
- **Contingency (M75)**: IndexedDB unavailable (private mode, storage
  pressure) ⇒ outcome `storage_unsupported`, the panel says so, and the
  one-click Download ZIP button is unaffected — the scheduler is an
  accelerator, never a gate on export.
