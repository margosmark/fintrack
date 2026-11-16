# 33 — End-to-end encryption (opt-in mode; universal zero-knowledge cutover cancelled 2026-09-26)

## Goal

Target: the server (and its operators) cannot read user financial data. All PII and
values — payees, amounts, account names, symbols, notes — are AES-GCM
ciphertext to the server. Decryption and all computation happen in the browser.

**Current status (the honest claim — Phase 4 shipped sync and the hardening
audit, neither of which changes this): client-side encryption in
progress — the server can still read your data.** Today the server stores and
serves plaintext: `transactions.raw_description`, `normalized_payee` and
`amount_native` are mandatory plaintext columns
(`backend/app/models/transactions.py:38-40`), the API always returns them
(`backend/app/schemas/transactions.py:70-72`), and `encryption_required`
only verifies that the Phase-2 cipher columns are *present* — never that
plaintext is absent (`backend/app/api/transactions.py:76-82,411-419`).
`dedup_hash` is unsalted SHA-256 over `account|date|amount|raw_description`
(`backend/app/importers/base.py:52-60`) — dictionary-reversible back to
descriptions and amounts. UI copy and docs must say "client-side encryption
in progress — server can still read data", never "zero-knowledge" —
permanently, per the product decision below.

## Product decision (2026-09-26): encryption is an opt-in mode, not a cutover

Decided by the operator: **there is no universal encryption mandate.**
Plaintext columns stay for everyone; encryption remains a per-user opt-in
(Settings → Encryption); the honest banner stays up indefinitely instead of
flipping to a "zero-knowledge" claim. What the option buys is real but
scoped: for opted-in users, feature compute runs in the browser on locally
decrypted data (Phase 3 slices S1–S5), and features that need decryption
**degrade while the vault is locked** — e.g. fuzzy search falls back to exact
blind-index hits and shows an unlock hint. Accepted tradeoffs of the option,
not bugs.

The old Phase 4 endgame (drop plaintext columns globally, passphrase
onboarding for every account) is **cancelled**. If a stronger guarantee is
ever wanted later, the path is per-user and reversible: stop *serving*
plaintext in API responses for users who opt into a strict mode (columns
remain as rollback data) — which the Phase-3 client-side compute makes
possible without touching anyone else's experience.

## Threat model

- Trusted: the user's browser, the shipped frontend bundle, the user's memory
  (encryption passphrase, session-only).
- Untrusted: the API host, the database and its backups, the network, the
  operators. Honest-but-curious server: it follows the protocol but reads
  everything it can.
- Explicitly out of scope: compromised frontend bundle (supply chain), malware
  on the user's machine, forgetting the passphrase (no recovery — the server
  has nothing to recover *with*; the downloadable export + local backup are
  the safety net).
- **Status vs threat model**: today the honest-but-curious server *can* read
  everything (see Goal) — the phases below are the plan to actually deny it.

## Key hierarchy

- **Encryption passphrase**: separate from the login password, never sent to
  the server, never persisted. Entered once per session to unlock the vault;
  the vault locks on logout or explicit lock.
- **KEK** (key-encryption key): PBKDF2-SHA256, 600k iterations (reuse
  `privacyBackup.ts` parameters), derived from passphrase + random salt.
- **DEK** (data-encryption key): random 256-bit, generated once per user,
  stored **wrapped** (AES-GCM under KEK). Phase 1: wrapped DEK in IndexedDB
  (per-browser, same precedent as docs/11 backup).
  **[SHIPPED 2026-09-28, Phase 4 / M53]**: the wrapped DEK is also synced
  server-side as an opaque blob, one row per device (`vault_envelopes`), so a
  second authorized browser enrolls with nothing but the passphrase — see
  “Phase 4 (M53)” below for the full contract. The server stores a blob it
  cannot open; it never sees the KEK, the passphrase or the DEK.
  **[SHIPPED 2026-09-28, M56]**: the same session DEK also encrypts
  **scheduled-backup artifacts** before upload — no second key hierarchy was
  introduced; see “Backup artifacts (M56)” below.
- **Index key**: separate random 256-bit (wrapped alongside the DEK), used
  only for HMAC blind indexes — never for encryption.

## Field envelope (v1)

Every encrypted field stores ASCII `v1.<base64(iv)>.<base64(ciphertext)>`.
Plaintext is the UTF-8 JSON of the value (numbers serialized canonically —
`Decimal` as string, matching the money-invariant in AGENTS.md §2). AES-GCM-256,
fresh 96-bit IV per field. Wrong key / tampering fails closed (GCM auth).

## What is encrypted vs plaintext

### SHIPPED (Phase 2) — ciphertext stored ALONGSIDE still-mandatory plaintext

| Data | Treatment | Evidence |
|---|---|---|
| `transactions.payee_cipher`, `raw_desc_cipher`, `amount_cipher`, `cad_amount_cipher`, `payee_token` | Ciphertext columns, **nullable, added alongside plaintext** (plaintext NOT NULL remains) | `backend/app/models/transactions.py:38-40,67-71` |
| `accounts.name_cipher` | Same pattern; `accounts.name` stays plaintext NOT NULL | `backend/app/models/accounts.py:48,57-58` |
| `users.encryption_required` | 422 `encryption_required` if `payee_cipher`/`amount_cipher` missing on create/update — **presence check only** | `backend/app/api/transactions.py:76-82,411-419` |
| Blind index `transactions.payee_token` | Stored exact-match token, filterable (format: Blind indexes below) | `backend/app/models/transactions.py:71` |
| `posting_date`, `user_id`, row IDs, category IDs | Plaintext (needed for range fetch + joins; timing/volume metadata leaks accepted) | `backend/app/models/transactions.py:33-37` |
| `categories/tags` names | Plaintext (server-side grouping) | — |

### Server-readable plaintext TODAY (no cipher column exists)

| Data | Where |
|---|---|
| `transactions.raw_description`, `normalized_payee`, `amount_native`, `cad_amount` — mandatory, served by the API | `models/transactions.py:38-42`, `schemas/transactions.py:70-72` |
| `dedup_hash` — unsalted SHA-256 over `account|date|amount|raw_description`; dictionary-reversible | `importers/base.py:52-60` |
| `learned_payees.normalized_payee` | `models/learned_payees.py:19` |
| `rules.pattern` (regex / exact_payee / contains) | `models/rules.py:32` |
| `institutions.name` | `models/institutions.py:14` |
| `securities.symbol`, `name`, `kind`, `listing_currency`, `exchange` | `models/securities.py:25-31` |
| `sync_agreements.consent_id`, `account_refs`, `scopes`, `last_error` — no credential column exists at all (docs/39) | `models/sync_agreements.py` |
| `accounts.name` | `models/accounts.py:48` |
| `import_batches.file_name` | `models/import_batches.py:25` |
| `transactions.note` — plaintext free text ≤ 500 on any row (M107 split children + the split editor in v1). **No cipher column exists**, so it is readable by a DB reader and served by the API like `raw_description`; deliberately listed here because omitting it would overstate the posture (docs/44 §1, §10) | `models/transactions.py` (`note`, migration `0042_split_transactions`), `schemas/transactions.py` (`TransactionUpdate.note`, `SplitChildIn.note`) |
|| `users.forecast_scenarios` — saved what-if scenarios (M109): house prices, salary deltas, cost deltas. Ordinary plaintext user content like `saved_report_views`; **no cipher column exists and none is claimed**, and the entry shape is validated at the API seam, not encrypted at the application layer | `models/users.py`, `schemas/forecast.py` |
|| `transaction_events.payload` — the M106 activity receipt's human detail: `changes` is the exact set of values a `PATCH` wrote, so `amount_native`, `normalized_payee` and `note` values are in there verbatim, next to `payload_hash` (an unsalted sha256 over them). **No cipher column exists and none is claimed.** It ships as stored in `/export/full`, in the ZIP even under `vault=locked`, and in the drawer API — so a receipt can quote the same number a masked `transactions.amount_cipher` hides. That is why docs/43 §6 strips payloads from the PARTNER projection outright instead of trusting the mask, and why docs/11 § *Export & the vault* answers the two encryption questions separately: the FIELD envelope does not cover this column; the BACKUP ARTIFACT encrypts the whole snapshot, receipt payloads included | `models/transaction_events.py` (`payload`), `services/tx_events.py` |

### OUTSIDE THE ENVELOPE (M79) — bank sync

**Decided 2026-10-12 (M181, t_89fa8b10): the unguarded ingestion paths fail
closed.** The QUESTION this section carried since M83 is resolved in favour of
enforcement, not of documenting the exemption as intended — an opted-in
tenant's own request must never silently write the plaintext columns the user
opted out of. What is *not* claimed: bank sync is still not end-to-end
encrypted. Provider credentials and consent tokens remain server-side state the
passphrase never protects, and `sync_agreements.*` stays server-readable. The
disclosure below is unchanged except for the pull.

The disclosure — **docs-only today**, see "Where this disclosure lives" below:

> **Bank sync is not covered by end-to-end encryption.** Provider credentials
> and consent tokens live server-side, and the agreement metadata is
> server-readable. A pull is refused for an opted-in tenant (`422
> encryption_required`): the provider path cannot produce ciphertext, so it
> stops instead of writing plaintext rows.

**Where this disclosure lives (M83/F-9, corrected 2026-10-02 — this used to
read "Claim (UI copy + docs, verbatim)", asserting a shipped UI copy that does
not exist).** In the **docs only**: `docs/39 § 9` is the source of the wording
and this section repeats it. There is no SPA copy anywhere — M79 shipped the
backend seam alone (`backend/app/api/sync.py`), `frontend/src/api/` has no
`sync.ts` and `frontend/src` contains **zero** `/sync` route references, and
`GET /api/v1/sync/providers` answers only `{slug, display_name}`
(`backend/app/schemas/sync.py:13-15`) — no disclosure field, no metadata. The
UI copy ships with the sync **status UI**, which `docs/39 § 5` records as
future groundwork ("Both feed a future status UI"). Until that lands, neither
this doc nor `docs/39 § 9` may describe the wording as shipped UI text
(AGENTS §9).

- No credential is stored *today* — `sync_agreements` has no token or secret
  column, because no accredited adapter exists yet (docs/39). When one lands,
  its tokens will be server-side state the passphrase never protects; the
  disclosure is made now rather than after the fact.
- **A tenant that has not opted in is unaffected**: a pulled row is written to
  `raw_description` / `normalized_payee` / `amount_native` with `payee_cipher =
  raw_desc_cipher = amount_cipher = NULL`. It renders as plaintext whether the
  vault is locked or unlocked, exactly like a legacy `v0` row (A16 mixed-mode);
  the `🔒` marker never appears for it. Client-side backfill (A17) is the only
  path that would give it ciphertext, and it fails closed while locked.
- **An opted-in (`encryption_required`) tenant gets no pull at all** (M181): the
  router refuses with `422 encryption_required` *before* `service.pull` runs, so
  no provider call is made and no `import_batches` / `transactions` /
  `transaction_events` row is created or committed. The reason is stated, not
  implied: the pull runs server-side and cannot produce the ciphers the write
  guard demands. This is a capability refusal, not a lock state — unlocking the
  vault does not enable it, and no copy may offer unlock as the remedy (rule
  7d, below). Consent setup and `revoke` stay available; the only cleanup left
  for rows staged *before* enrolment is the ordinary one (`discard` /
  `revert`).
#### `encryption_required` guard — full wire inventory (M83/F-10 correction 2026-10-02; ingestion gap closed 2026-10-12, M181)

The wording that stood here said *the* ingestion paths — file upload **and**
sync pull — do not run the guard. Wrong on both sides: YNAB's file upload
**does** refuse, and the unguarded client write surface was wider than CRUD.
As of M181 every path **in this inventory** that would create a plaintext row
for an opted-in tenant refuses the whole request with `422 encryption_required`.
The scope of that claim is the table below, not the whole API: two write routes
deliberately OUTSIDE M181 still create plaintext rows for an enrolled tenant —
`POST /backup/restore` and `POST /accounts/{id}/reconciliation/upload` — and
they are carried as the table's last row, marked **open, outside M181 scope**,
rather than quietly left out (security finding F1, 2026-11-07). Wire inventory
— `grep -rn encryption_required backend/app` hits ten files; the six that carry
a guard are the routers listed here (`api/transactions.py`, `api/ynab.py`,
`api/accounts.py`, `api/imports.py`, `api/trades.py`, `api/sync.py`), the rest
being the flag's own column (`models/users.py`), its schemas, and
`services/locked_fixture.py` (the locked-fixture tenant helper):

| Write path | Runs the guard? | Evidence |
| --- | --- | --- |
| `POST /transactions`, `PATCH /transactions/{id}` | yes — 422 unless `payee_cipher` + `amount_cipher` present | `api/transactions.py:76-82` (`_require_tx_ciphers`, called at `:132`), `:411-419` |
| `POST /accounts`, `PATCH /accounts/{id}` | yes — 422 unless `name_cipher` present | `api/accounts.py:55-61`, called at `:215`, `:250` |
| `POST /imports/ynab/commit` — YNAB migration upload | **yes — refuses the whole upload** with 422 before it parses a byte | `api/ynab.py:36-58` (`_refuse_when_encrypted`, called at `:197`) |
| `POST /imports/upload` + `POST /imports/{batch_id}/commit` (OFX/QFX/generic CSV) | **yes — 422 before the file is read; the commit is refused 422 before any row is written or deleted** (M181) | `api/imports.py::upload` (after `owned_or_404`, before `file.read()`), `::commit_batch` (after the `pending_review` check, before the duplicate purge and the status flip) |
| `POST /trades/import` + `POST /trades/import/{batch_id}/commit` (M82 brokerage profiles) | **yes — 422 before the body read, and 422 on the commit** (M181); `/import/preview` writes no row and stays available | `api/trades.py::import_trades`, `::commit_trade_batch`; `::import_trades_preview` deliberately unguarded |
| `POST /trades`, `PATCH /trades/{id}` — manual trade writes | **yes — 422** (M181). `trades` has **no cipher columns at all** (`models/trades.py`), so a hand-entered trade is exactly as unwritable as an imported one — the alternative was a silent inconsistency | `api/trades.py::create_trade`, `::update_trade`; `::delete_trade` unguarded by design (removal only) |
| `POST /trades/record-dividend` (client-initiated, but it *creates* a `Transaction` row) | **yes — 422 after the two ownership 404s and before the first `db.add`** (M181) | `api/trades.py::record_dividend` |
| `POST /sync/agreements/{id}/pull` — sync pull | **yes — 422 before `service.pull`** (M181); consent setup/revoke stay available | `api/sync.py::pull` |
| `POST /transactions/{id}/split`, `PUT /transactions/{id}/split` — M107 split create/replace | **yes — 422 `encryption_required` unless EVERY child carries `amount_cipher`** (docs/44 §4.2 step 7, raised in `_validate_split_legs`). `payee_cipher` / `raw_desc_cipher` / `payee_token` are copied server-side from the parent (same plaintext, same key — nothing new leaks), and `cad_amount_cipher` keeps today's create rule: optional, A17 backfill fills it later | `api/transactions.py::_validate_split_legs` (step 7) |
| `DELETE /transactions/{id}/split` (merge-back), `GET /transactions/{id}/split` | **no** — merge writes no cipher (it deletes children and restores the parent's own columns); GET is a read | `api/transactions.py::merge_split` / `get_split` |
| `POST /backup/restore`, `POST /accounts/{id}/reconciliation/upload` | **no — open, outside M181 scope** (both still write plaintext for an opted-in tenant; carried here so this table cannot be read as "every write path") | `api/backup.py::restore_backup` (`:338`) re-imports every snapshot table except `RESTORE_SKIP_TABLES = ("users", "vault_envelopes")` (`:78`, applied at `:378`) — `transactions` rides along as plaintext; `api/reconciliation.py::upload_statement` (`:55`) writes plaintext `reconciliation_items.statement_description`. Neither file mentions `encryption_required` |

All `transactions`-staging sources — CSV/OFX/QFX upload, YNAB and the sync pull
— funnel through the shared `services/import_staging.stage_rows()`, which
builds `transactions` rows with plaintext `raw_description` / `amount_native`
(`import_staging.py:160`). [M181] every one of them is stopped at its own
router *before* it can reach that pipeline; that is also why the guard is a
whole-request refusal and not a per-row check — the pipeline has no way to
produce a cipher. `record_dividend` builds its row inline the same way, and
`POST /trades/import` stages into `trades`, a table with **no cipher columns at
all** (`models/trades.py`) — outside the E2EE envelope entirely, so "enforce it
like YNAB" there means *refuse the file*, never *require a cipher*.

**`stage_rows` is deliberately left unguarded** as the single funnel: every
caller that can reach it is a refused route (upload, sync pull), so a second
check inside the service would be dead code that can drift from the routers.
The invariant is pinned from the outside instead — the behaviour tests assert
zero new `import_batches` / `transactions` / `trades` / `securities` /
`transaction_events` / `acb_pools` rows after each refusal.

**Gap closed 2026-10-12 (M181).** Where this section used to carry "known gap,
tracked: an `encryption_required` tenant can hold plaintext rows created by
import, by a recorded dividend or by a sync pull", that is now false for new
rows: each of THOSE ingestion paths fails closed with `422
encryption_required`. (Not "every write path in the app" — the two routes in
the inventory's **open, outside M181 scope** row above are the known
exception.) Rows created *before* the tenant enrolled are untouched
and stay readable — no data migration runs, and none is claimed.

**Resolved 2026-10-12 (M181) — the QUESTION this section used to carry**
("should the unguarded ingestion paths enforce `encryption_required` the way
YNAB already does, or should this section state the exemption as intended?").
Ruling: **enforce**, with three dispositions (operator/PM, recorded on card
`t_89fa8b10`):

1. **Manual trade writes are in scope.** `POST /trades` and
   `PATCH /trades/{id}` write the same cipher-less `trades` rows the import
   refusal blocks; leaving them open would have made the inconsistency silent.
2. **Zero-write previews stay available.** `POST /imports/preview`,
   `POST /imports/suggest-profile` and `POST /trades/import/preview` persist no
   row (docs/08 pins "zero writes" for each), so the write guard has nothing to
   refuse there — the behaviour tests pin `0` rows written for an opted-in
   tenant instead.
3. **Sync consent stays, the pull does not.** Agreement setup/consent/revoke
   remain available; every pull is refused, because the provider path cannot
   write the ciphertext contract. The refusal says exactly that and never
   implies that unlocking the vault would enable a server-side pull.

**Intentional exclusions — what an opted-in tenant CAN still call, and why:**

| Route / route group | Disposition for an `encryption_required` tenant | Why |
| --- | --- | --- |
| `POST /imports/preview`, `POST /imports/suggest-profile`, `POST /trades/import/preview` | available | zero writes (pinned by tests: 0 rows before/after) |
| `POST /imports/{id}/discard`, `POST /imports/{id}/revert`, `POST /trades/import/{id}/discard` | available | removal/cleanup only — the ONLY way to clear rows staged before enrolment |
| `POST /sync/agreements` (consent), `GET /sync/**`, `POST /sync/agreements/{id}/revoke` | available | consent/metadata, no row content |
| `DELETE /trades/{id}`, `DELETE /transactions/{id}`, `DELETE /transactions/{id}/split` (merge-back) | available | removal only; merge-back deletes children and restores the parent's own columns, writing no cipher |
| `POST /acb/rebuild` | available | derived cache recomputed from the tenant's own existing trades (`rebuild_user_pools` is its only write path); it creates no new plaintext content |
| `POST /imports/ynab/commit` | refused since M79 | the model this guard copies — listed here so the exclusion table cannot be read as re-opening it |
| `POST /backup/restore`, `POST /accounts/{id}/reconciliation/upload` | available — **open, outside M181 scope** | they still WRITE plaintext for an opted-in tenant (a restore re-imports `transactions`; the reconciliation upload stores `statement_description`) — a known gap deferred WITH M181, not a deliberate E2EE exclusion; the inventory row above carries the evidence |

**UI half — shipped (M181 follow-up, card `t_4c1a1c34`).** The SPA used to
offer those controls to an opted-in tenant, which discovered the refusal from a
`422` only after the work was done. It no longer does:
`frontend/src/components/e2ee/ServerSideBlocked.tsx`
renders the reason IN PLACE OF each write control before submit (rule 6), and
it gates on ENROLMENT ALONE — `users.encryption_required`, never `enrolled &&
!unlocked` — because unlocking the vault cannot enable a server-side write: a
lock-state gate here would both name a remedy that does not work (rule 7d) and
hide the reason from an unlocked-but-enrolled user. Its capability strings are
the endpoints' own capability phrases (`_refuse_when_encrypted(...)` in
`api/imports.py` / `api/trades.py`, and `api/sync.py`'s inline copy) with the
trailing "…so nothing was saved" dropped — nothing has been submitted yet — so
the pre-emptive notice and the defense-in-depth 422 cannot tell two stories.

Gated: the bank statement's **Confirm** (`import:statement`), both batch
**Commit**s (`import:statement-commit`, `import:trades-commit`), the brokerage
**Upload & parse** (`import:trades`), the trade form's **Record trade**
(`write:trade`), and both dividend **Record** controls (`write:dividend`).
Left live, unchanged from the table above: the zero-write previews, the
column-mapping wizard, `Discard` / `Revert`, and the YNAB gate (G3) and the
split/bulk-rename gates (G1/G2), which stay on `enrolled && !unlocked` because
the BROWSER can produce those ciphers once the vault is open.

There is still no sync surface to gate: `frontend/src` holds zero `/sync`
references (above), so `SERVER_SIDE_BLOCK_COPY["sync:pull"]` is reserved for
the status UI when `docs/39 § 5` lands, so that UI cannot invent its own
wording for the one sync route that is refused. `frontend/tests/serverSideWriteGates.test.tsx`
pins every surface both ways and pins the enrolment-only rule itself by
re-asserting a gate with the vault UNLOCKED.

### SHIPPED (M56) — backup artifacts: ciphertext at rest, honestly scoped

`backup_artifacts.ciphertext` is the first table whose *entire* payload the
server stores as opaque ciphertext: one `v1.<base64(iv)>.<base64(ciphertext)>`
field envelope (docs/33 field envelope) AES-GCM-256 under the **session DEK**,
produced by `frontend/src/lib/backupArtifact.ts` in the browser *before*
upload. Key path: passphrase → PBKDF2 KEK → unwrap → same DEK that encrypts
fields (`vault.ts::getDEK()`); **no second key hierarchy, no new cipher, no
server-side key material** (docs/11 § "Two backups, two keys" — the older M7
browser backup keeps its own independent passphrase and is untouched).

What a database reader gets: `period` (cadence), `created_at`, `size_bytes`
and the ciphertext itself. What they do not get: the export snapshot inside
the envelope. That is a **scoped** win, not a privacy upgrade — the same rows
sit in plaintext columns and are served by the API, so the status line stays
**client-side encryption in progress — the server can still read your data**,
and "zero-knowledge" is never written for this feature, its storage decision,
or anything else (product decision above; UI copy in
`ScheduledBackupPanel.tsx`).

Two honest details that must not be glossed:

- **A run needs an open tab.** Only the browser holds the DEK, so there is no
  server-side scheduler; a missed period produces one catch-up artifact, not a
  backlog (docs/11 due semantics).
- **Restore sends plaintext back.** `POST /backup/restore` carries the
  *decrypted* payload, because the server cannot decrypt anything — that
  transmission is not a new exposure (the API already serves those same rows
  in plaintext), but it is stated rather than implied: the artifact is the
  encrypted thing, the restore call is not.

### PLANNED (not implemented — do not claim as shipped)

The previous version of this table listed `institutions.*` and
`securities.*` as ciphertext. **No such cipher columns exist**
(`models/institutions.py:14` is plaintext `name`; `models/securities.py:25-31`
has no cipher fields) — audit finding M14. Planned only:

| Data | Planned treatment | Blocked on |
|---|---|---|
| `institutions.*` | Ciphertext columns | New migration — none exists today |
| `securities.*` symbol/name/quantities/prices | Ciphertext columns | New migration — none exists today |
| `learned_payees.normalized_payee`, `rules.pattern` | Blind-index HMAC tokens (server matches on tokens) | Phase 3 |
| `dedup_hash` | Keyed HMAC under the index key, not bare SHA-256 | Phase 3/4 |
| `transactions`/`accounts` plaintext columns | Stay (2026-09-26 decision). Optional per-user strict mode may later stop *serving* them | Product decision above |
| `categories/tags` names | Revisit (plaintext in v1) | Hardening phase |

## Blind indexes

`indexToken(value, purpose) = HMAC-SHA256(indexKey, purpose ‖ normalized(value))`.
Used for exact payee search and rule `exact_payee` matching. Fuzzy/contains
matching cannot use blind indexes — it runs client-side over decrypted rows
(personal-finance scale: thousands of rows, trivial for the browser).

## Migration (client-side backfill — the server can never do it)

1. Legacy plaintext rows stay readable during transition (`v0` = plaintext marker).
2. When the user unlocks the vault, the client backfills: fetch legacy rows,
   encrypt field-by-field, PUT ciphertext + index tokens back.
3. Server rejects new plaintext writes for migrated tables once the user
   enables "encryption required" (per-user flag); mixed-mode reads handle
   `v0` + `v1` during transition.
   **Current reality (2026-09-23):** `encryption_required` checks cipher
   *presence* only (`backend/app/api/transactions.py:76-82,411-419`); the
   plaintext columns remain NOT NULL (`backend/app/models/transactions.py:38-40`)
   and are still served by the API (`backend/app/schemas/transactions.py:70-72`).
   Plaintext-write rejection ships with Phase 4, not Phase 2.

## Compute moves client-side (per feature)

- Canadian tax engine v1 (`lib/canadaTaxEngine.ts` + `lib/canadaTaxTables.ts`,
  docs/58): federal/provincial income tax, registered-account room and
  CPP/OAS timing as pure functions over static, CRA-cited tax-year tables.
  No server twin in v1 (the server's `app/services/tax_projection.py` is
  untouched — parity is future work, stated in docs/58), no row reads and no
  `../api/` payloads, so lock state cannot move it — **no A-row** (new feature
  t_52ed8d01, outside the S1–S5 audit); rule-8 inventory rows are in the table
  below.
- Reports/aggregations: fetch date-range rows → decrypt → aggregate in browser.
  **[SHIPPED 2026-09-26, S2a partial]**: spending, spending/categories and
  cashflow compute in the browser (`lib/reportEngine.ts`, pinned response
  shapes; server routes kept for CSV).
  **[RETIRED 2026-10-11, M160 / P2-5]**: `lib/reportEngine.ts` is deleted
  (M158 Decision 2 option c, the "later wave" rule 5 named). These three
  surfaces are server-first reads and have no client compute left; the s2a
  goldens are now server-route regression pins
  (`backend/tests/test_m160_replay_s2a_matrix.py`, covered by A4/A5/A6). The
  rule-8 table above lists what survived the retirement and why.
  **Evidence (M155, 2026-10-11)**: offline node diff vs captured live server
  JSON = **29/29 parity cases byte-identical** —
  `node frontend/scripts/parity/parity.ts --slice s2a` over the new
  `tests/fixtures/s2a-vectors.json` (14 demo cases + 15 from a throwaway
  tenant seeded with the shapes the demo cannot carry: `group_by=tag`, the
  `category_id` / `uncategorized` drills and their tag composition, a
  multi-tag row, a parent/child category pair and a USD row priced from the
  cache-only fx range). **[M160]** that client-comparison reader
  (`tests/s2aParity.test.ts`, 39 tests incl. 4 hand-computed anchors) retired
  with the adapters it compared; the same 29 cases are replayed at the mounted
  route by `backend/tests/test_m160_replay_s2a_matrix.py`. M135 §4.4-1 — these
  rows had a client engine with NOTHING to diff it against until this capture
  existed; that is why M137's flips kept the client engines as fallback.
  **[SHIPPED 2026-09-27, S2b]**: net-worth and investment-performance compute
  in the browser too — `netWorthReport` + `investmentPerformanceReport`, the
  latter including the ACB replay and superficial-loss fixpoint
  (`app.tax.acb` / `app.tax.superficial_loss`) in exact BigInt fixed-point
  math mirroring Python `Decimal`. Report input loaders are committed-scope
  (`GET /trades?committed_only=true`, new opt-in — the default list still
  shows staged rows, keeping the review window intact per docs/03).
  Evidence: offline node diff of the shipped engine vs captured live server
  JSON = 11/11 ranges byte-identical. **[M160]** `tests/s2bReports.test.ts`
  (28 tests) retired with the mirror; the vectors stay on disk as M158 ruled,
  and wiring them as a route replay is follow-up card `t_62afc8a5`.
  **[SHIPPED 2026-09-27, S2c]**: dividends compute in the browser too —
  `dividendReport`, an exact port of `app/services/dividends.py` (per-row
  HALF_UP cents on the cache-only trade-date rate, unresolved rows excluded
  from every CAD sum, TTM window from an injected UTC `today` so goldens stay
  frozen). `GET /reports/dividends` stays for `format=csv`.
  Evidence: offline node diff vs captured live server JSON = 4/4 goldens
  byte-identical (default year, both available years, empty year). **[M160]**
  `tests/s2cDividends.test.ts` (9 tests) retired with the mirror; the vectors
  stay on disk as M158 ruled, and wiring them as a route replay is follow-up
  card `t_62afc8a5`.
  **[SHIPPED 2026-09-27, S4]**: the annual capital-gains summary computes in
  the browser too — `capitalGainsReport`, an exact port of
  `app/tax/reports.py::capital_gains_report` over the S2b tax state (per-row
  HALF_UP cents, a zero-excess `return_of_capital` creates no row, denial
  add-backs land after the ROC pass and only on buckets that already exist,
  `str(Decimal)` scale preserved so an empty year serializes as `"0"`).
  `GET /reports/capital-gains` stays mounted for `format=csv` / `format=html`
  and non-browser consumers.
  Evidence: offline node diff vs captured live server JSON = **14/14 parity
  cases byte-identical** — the 13 tax years plus `GET /acb?limit=200`.
  **[M160]** `tests/s4CapitalGains.test.ts` (43 tests) retired with the mirror;
  the same 14 cases are replayed at the mounted route by
  `backend/tests/test_m160_replay_s4.py`.
  **Not moved by S4: `GET /acb`** (the `acb_pools` read path) — it stays
  mounted because the Taxes page renders its two database-assigned columns and
  because non-browser consumers need it (the MCP `acb_list` tool,
  `backend/mcp_server/server.py:1181`; unlike the report routes it has no
  `format=csv|html`). Its rows carry a serial `id` and an `updated_at` (last
  `rebuild_user_pools` write), neither of which any client computation can
  derive from trades, and replacing the response would change the page shape,
  which this slice forbids. The parity scope matches that boundary:
  `capture.ts` records the same single response both as the `acb.pools` golden
  and as the frozen input `datasets.demo.acbPools`, and the S4 adapter takes
  **only** `id` + `updated_at` from it — the pool set, the other six fields per
  row, the sort order and the `total`/`limit`/`offset` envelope are computed,
  so `node frontend/scripts/parity/parity.ts --slice s4` exits 0 at 14/14 while
  still gating every derivable number (PM scope decision A). The ACB *math*
  that response serves was browser-side from S2b until M160 retired the client
  pool math; `backend/tests/test_m160_replay_s4.py` now asserts the rebuilt
  pools equal every derivable field of `GET /acb?limit=200` against the frozen
  `acb.pools` golden.
  **[CAPTURED 2026-10-11, M124 R3 matrix]**: `GET /reports/category-tag-matrix`
  still ships SERVER-computed — the Categories tab fetches it and this wave
  re-points nothing (M135 gates only) — but its client mirror
  `categoryTagMatrixReport` finally has a golden to stay identical against:
  `node frontend/scripts/parity/parity.ts --slice matrix` exits 0 at **6/6**
  over the new `tests/fixtures/matrix-vectors.json` (2 demo cases + 4 from a
  12-tag throwaway tenant that exercises the top-8 column cut, the `Other
  tags` spill, the always-last `Untagged`, the once-counted `total_cad` and
  the RAW — not root-rolled — category rows). Closes M135 §4.3's "capture
  required (no fixture today)". **M160 note (the passage above is history):**
  the `matrix` slice's ENGINE retired with the client mirrors (P2-5), so
  `parity.ts --slice matrix` now exits 4 by design and the frozen
  `matrix-vectors.json` is replayed against the MOUNTED ROUTE by
  `backend/tests/test_m160_replay_s2a_matrix.py` (see audit row A20).
  **Lock behaviour — audit rows A4, A5, A6, A7, A8, A9, A14, A15 and A20** (every
  one of them asserts the engine is lock-invariant and pinned to a golden).
- Search: exact payee via token; everything else client-side post-decrypt.
  **[SHIPPED 2026-09-25, S1]**: `GET /search` answers only exact `payee_token`
  equality (plus the plaintext-by-design metadata groups); fuzzy matching runs
  in the browser (`lib/searchMatch`, pg_trgm-parity, golden-tested), fed by the
  committed-row fetch + mixed-mode resolution (`useSearchRows`). The
  `GET /transactions/similar` content-scan endpoint was removed (M12).
  **[SHIPPED 2026-09-28, S5]**: the LAST server-side content scan,
  `GET /transactions?q=`, is gone — the Transactions search view fetches its
  substrate with the structured filters and ranks it in the browser
  (`lib/searchMatch::searchPage`), and the endpoint answers **422
  `q_removed`** if a stale client still sends `q`. The port had to become
  faithful pg_trgm first (`trgmArray`/`wordSimilarity` reproduce
  `trgm_op.c`: per-word padding, ordered duplicate trigrams, float4 Jaccard
  windows) — S1's whole-string-padding heuristic disagreed with live
  postgres on real payees (`q=gro`: server 49 hits, browser 0), which the
  S5 golden capture caught. Evidence: `parity.ts --slice s5` = 10/10 live
  goldens (344-row demo substrate), plus the pg_trgm vectors in
  `tests/searchMatch.test.ts`.
  **[REVERSED 2026-10-10, M137 P0-1 — §Server-first computing]**: the
  matcher went back to the SERVER, because the faithful port was 285 lines
  reproducing a capability postgres already has natively and already uses
  elsewhere (`services/categorization`). `GET /transactions?q=`,
  `GET /transactions/similar` and the `/search` transaction group all rank on
  `greatest(similarity, word_similarity) >= 0.3` over `normalized_payee`
  behind `ix_trgm_transactions_payee`. **The port is NOT deleted** — it is the
  E2EE/offline fallback P2-5 retires, and its vectors MOVED (not vanished)
  into `backend/tests/test_search_goldens.py`: 19 `PG_GOLDEN` score vectors +
  the 2 S5 pairs, and the 10 live goldens replayed against the re-mounted
  endpoint. `hide_encrypted`/`withheld` carry the locked-vault half: a cipher
  payee is plaintext server-side and always matches, a locked CLIENT is what
  withholds it.
  **Lock behaviour — audit rows A1, A2 and A3**, plus finding **F4**/**A18** for
  the search view's own unlock hint.
- Rules engine: runs in browser on decrypted rows (same predicate semantics).
  **[SHIPPED 2026-09-27, S3]**: the rule *dry-run* computes in the browser —
  `lib/s3Engine.ts::previewRule`, a line-for-line port of
  `api/rules.py::preview_rule` + `services/rules_engine.py::rule_matches`,
  including the pydantic-shaped 422 envelopes (`invalid_regex`,
  `pattern_too_long`, `invalid_amount`, `validation_error`). Preview and
  `POST /rules/{id}/apply` still agree on what "matches" because both use
  that predicate. `POST /rules/preview` is deleted and answers **405**;
  apply still writes server-side.
  **[REVERSED 2026-10-10, M137 P1-2]**: `POST /rules/preview` is MOUNTED
  again, running the SAME `services/rules_engine` predicate `apply` writes
  with — so the preview and the apply are one engine rather than a server
  write gated by a browser re-implementation of it. The client
  `lib/s3Engine.ts::previewRule` twin stays (offline fallback, same goldens),
  and both are parametrized over `encryption_required` ON/OFF (P1-5 pattern)
  because the matcher reads only the mandatory plaintext columns.
  **Lock behaviour — audit rows A10 and A11**.
  **[RETIRED 2026-10-11, M160 / P2-5]**: `lib/s3Engine.ts::previewRule` is
  deleted with the engine — the server route is the one engine, and the s3
  goldens are now server-route regression pins
  (`backend/tests/test_m160_replay_s3.py`, covered by A10). A10/A11 stand on
  the backend suite, not on a client twin.
- Subscriptions, budgets progress, ACB tax math: browser-side on decrypted data.
  **[SHIPPED 2026-09-27, S3]**: `lib/s3Engine.ts::subscriptionsReport` and
  `::budgetsProgress`. **[M138]** the Dashboard's subscriptions card left this
  substrate: it reads the strict detector server-side
  (`/transactions/recurring/candidates?source=subscriptions`, docs/09 §Bills
  you probably have). `subscriptionsReport` stays for the removed route's
  golden and the A12 lock-invariance pin below — a locked vault still changes
  nothing about it. `GET /subscriptions` is gone (router unmounted from
  `app/main.py` → **404**) because detection only ever scored rows the browser
  holds; `GET /budgets` is KEPT because it is the only read path for budget
  *definitions* — the browser discards its progress fields and recomputes
  them locally. Both paths return byte-identical shapes to the removed
  computation (the UI never changed). `app/services/subscriptions.py`
  survives as the reference implementation the port mirrors (docs/24).
  **[REVERSED 2026-10-10, M137 P0-2 + P1-2]**: `GET /subscriptions` is
  MOUNTED again (the engine it always mirrored was never removed —
  `bill_reminders`' cash-flow calendar called it throughout), and `GET
  /budgets` is no longer a definitions-only read: the browser renders the
  server's `spent`/`carried_in`/`remaining`/`pct_used` instead of discarding
  them, so the Budgets page stops downloading the ledger. The M96 recurring
  label moved to its own `GET /budgets/recurring-summary`, which is what
  finally let `api/s3Inputs.ts::loadCommittedTransactions` — the pivot's
  single biggest lever, a paginated download of EVERY committed row — be
  deleted. Both client twins stay as the offline fallback.
  **[RETIRED 2026-10-11, M160 / P2-5]**: `lib/s3Engine.ts` is deleted.
  `subscriptionsReport` went with it (the route is authoritative; the s3
  goldens are now server-route pins in `backend/tests/test_m160_replay_s3.py`,
  covered by A12). `budgetsProgress` is the ONE resident that survived — it
  moved to `lib/budgets.ts` as an AUDITED CLIENT-ONLY Row-8 compute (no
  server twin by D3 design; see the rule-8 table and A13).
  **[ROUTE FLAG, M137 P1-1]**: the `/reports/*` routes with zero UI consumers
  are gated by **`SERVER_REPORTS_DEPRECATED`** in `.env`, defaulting **OFF** —
  unset keeps them answering; `=1` answers **410 `report_route_deprecated`**
  with a body naming the route. **[M160-R2, t_85311c34]** three of the four
  (`/dividends`, `/cashflow-sankey`, `/total-return`) LEFT that set — their
  panels read them now, and a live screen route can never be gated.
  `/reports/networth-snapshot` is the one orphan left. Every route the screens
  consume is refused by the allowlist guard in BOTH states, and
  `backend/tests/test_report_route_deprecation.py` asserts both states plus
  the untouched screen routes.
  Evidence: offline node diff vs captured live server JSON = 16/16 cases
  byte-identical. **[M160]** `tests/s3ClientCompute.test.ts` (33 tests) retired
  with `s3Engine`; the same fixture now drives
  `backend/tests/test_m160_replay_s3.py` (16 route replays) and the surviving
  `budgets.progress` lock-invariance pin in
  `frontend/tests/lockedVaultDegradation.test.ts`. Remaining: ACB tax math (S4).
- Sorting/filtering the transaction list: browser-side (data already local).
- LLM suggestions — **what actually leaves the machine.** There is NO per-send
  consent dialog, and **[STRUCK 2026-09-28, M54]** the visible "sending N rows
  to \<provider\>" confirm an earlier revision of this section promised is not
  coming: it was never implemented (0 code hits at strike time) and the strike
  is a decision, not an omission. Rationale: (a) the only shape it could take
  is a `consent` field on the three request bodies below, asserted by the same
  authenticated principal the route already trusts — any script can set it, so
  a `403` on it proves nothing; (b) every one of the three routes has a
  legitimate scripted caller — the MCP tools `suggestions_run`
  (`backend/mcp_server/server.py:1334-1348`), `imports_suggest_profile`
  (`server.py:1207-1219`) and `users_test_llm` (`server.py:189-206`), none
  with a UI in the loop — which either break under server-side enforcement or
  demonstrate that the field cannot mean "a human clicked";
  (c) "per-send" is undefined on the run path, which fans out to ≤25-row
  batches via `BackgroundTasks` with one retry
  (`services/categorization.py:275-276,348-350`), so a pre-trigger dialog is
  consent-at-trigger and its N describes no actual send; (d) the binding
  control already exists and is server-enforced at the one send point —
  `llm/base.py:49,73` → `422 invalid_llm_base_url` / `403
  privacy_mode_blocks_external_api` (`llm/egress.py:181,190`), plus `409
  privacy_mode_requires_local_llm` at provider-write time (`api/users.py:168`)
  — set once in Settings rather than per request. The consent/disclosure model
  that IS implemented is pinned in docs/04 "Consent & disclosure model" (M54);
  if a consent gate ever ships, that section and this ledger change together
  (AGENTS.md §9). Mechanics of a send: an authenticated user calls one of
  three endpoints, with no consent field on any request schema:
  - `POST /suggestions/run` (`backend/app/api/suggestions.py:41-57`;
    `SuggestionRunRequest` carries only `transaction_ids`/`from`/`to`,
    `extra="forbid"` — `backend/app/schemas/suggestions.py:10-22`) →
    batches of ≤25 transactions (`services/categorization.py:34,276`)
    containing the full category-name list, up to 10 few-shot
    `normalized_payee => category` examples, and the batch's
    `transaction_id: normalized_payee` pairs (`services/categorization.py:37,220-242`).
  - `POST /imports/suggest-profile` (`backend/app/api/imports.py:109-134`) →
    CSV headers plus up to 5 sample data rows verbatim
    (`services/column_mapping.py:56-59,321,386`).
  - `POST /users/me/llm-test` (`backend/app/api/users.py:83-138`) → the fixed
    ping text `"Respond with ok set to true."` (`api/users.py:122`).
  Destination is the user-configured endpoint (`users.llm_base_url`, else the
  env default — `llm/base.py:120-137`); the `Authorization` header carries the
  user's key, or the env `LLM_API_KEY` only when the effective destination is
  the operator's own env endpoint (`llm/egress.py:122` `select_api_key`).
  Under privacy mode every LLM send is blocked — loopback-only at send time,
  provider enum alone is not the check (docs/11 step 5).
- **[SHIPPED 2026-11-10, M188]**: the retirement simulator's Monte Carlo runs
  in the browser — `lib/retirementEngine.ts` (seeded paths over the starting
  portfolio and the editable assumptions) plus its tax seam
  `lib/taxEngine.ts`, driven only by the two report reads the page already
  makes (`GET /reports/net-worth`, `GET /reports/cashflow`). The seam shipped
  with the flat-rate stub; **t_1ab83b11** swapped the page's construction to
  `caTaxEngine()` (docs/58 §Relationship to the server) — still no endpoint
  and no network call, so this disposition is unchanged. **No A-row
  applies**: the compute issues no endpoint of its own and reads only
  mandatory-plaintext aggregates (`net_cad`, `income_cad`, `expenses_cad`)
  plus user-typed numbers — no cipher column and no key material in sight, so
  lock state cannot move it. The rule-8 inventory rows for both modules (§Rule-8
  inventory, below) carry the disposition; pinned by
  `tests/retirementEngine.test.ts` (R1–R6) and
  `tests/retirementPage.test.tsx` (R7–R11, R13). Spec: docs/57.
- **[SHIPPED 2026-11-10, M189]**: the life-event planner's registry,
  validation, per-step cash flows and year-by-year timeline run in the
  browser — `lib/lifeEventEngine.ts`, pure math over the page's committed
  assumptions (its `eventFlow` is consumed by `lib/retirementEngine.ts`; no
  endpoint of its own, the page still reads only `GET /reports/net-worth`
  and `GET /reports/cashflow`). **No A-row applies**: user-typed numbers
  only — no cipher column and no key material, so lock state cannot move it.
  The rule-8 inventory rows below carry the disposition; pinned by
  `tests/lifeEventEngine.test.ts` (L1–L8), `tests/lifeEventPage.test.tsx`
  (E1–E6) and `tests/retirementEngine.test.ts` (R12). Spec: docs/58.

## Phases (each ships working, gates per AGENTS.md §4)

- **Phase 1 (this doc + build)**: `vault.ts` (unlock/lock/wrap/unwrap, session
  memory only) + `fields.ts` (envelope encrypt/decrypt, blind index tokens) +
  unit tests + Settings "Encryption" section (set passphrase, unlock, lock).
  No schema changes; nothing encrypted at rest yet.
- **Phase 2**: transactions + accounts ciphertext columns (nullable, alongside
  plaintext), client backfill, mixed-mode reads, "encryption required" flag.
  [SHIPPED: migration 0024, `payee_cipher`/`raw_desc_cipher`/`amount_cipher`/
  `cad_amount_cipher` columns + `payee_token` + its filter, `accounts.name_cipher`,
  `users.encryption_required` with 422 enforcement (cipher-presence only —
  plaintext stays mandatory, see "Current status"), `lib/e2ee.ts` display
  resolvers + `useResolved*` hooks, `lib/backfill.ts` client backfill, Settings
  backfill + enforcement UI, editor dual-write on save.]
- **Phase 3**: client-side reports/search/rules/subscriptions/budgets/ACB;
  server endpoints become ciphertext-blind stores.
- **Phase 4 (re-scoped 2026-09-26 — see Product decision; no global
  cutover)**: wrapped-DEK server sync for multi-browser enrollment,
  hardening review (nonce reuse audit, token scope audit), optional per-user
  strict mode (stop serving plaintext responses), docs/11 + docs/08
  reconciliation. **Cancelled:** dropping the plaintext columns / mandatory
  passphrase onboarding.
  **[SHIPPED 2026-09-28, M53]** the first two items: wrapped-DEK sync
  (`vault_envelopes` + `GET/POST/PUT /vault/envelopes`, revoke + passphrase
  rotation runbooks) and the hardening audit (two tables at the end of the
  Phase-4 section above — locked-vault degradation A1–A17 with a cited test
  per row, key-handling inventory F1–F9/K1–K6 with a disposition per row).
  `docs/08` gained the endpoint contract in the same commit.
  **Deferred, not shipped by M53** (each is an inventory row, not a silent
  omission): optional per-user strict mode; DEK rotation (**F5**);
  `dedup_hash` keyed HMAC (**F7**); blind indexes over `rules.pattern` /
  `learned_payees` (**F8**). `docs/11` reconciliation — **[SHIPPED 2026-09-28,
  M56]** the scheduled-backup extension landed with its contract pinned in
  docs/11 § "Scheduled backup & restore (M56)"; the key/claim side of that
  feature is recorded in "Backup artifacts (M56)" below.

## Phase 4 (M53) — wrapped-DEK sync across devices + hardening audit

**[SHIPPED 2026-09-28, M53]** two deliverables: (a) the wrapped DEK syncs
across devices/sessions, (b) a hardening audit of everything Phase 3 shipped
(S1, S2a/b/c, S3, S4, S5) — the audit tables are at the end of this section
and every row cites the test that proves it.

### Context: what Phase 1 actually gave us, and why sync fixes it

Phase 1 keeps the wrapped bundle in IndexedDB **only**, and `createVault()`
mints a brand-new DEK every time it runs. So *every browser enrolled its own
unrelated key hierarchy*: ciphertext device A writes is unreadable by device B,
which is useless the moment a user has a laptop and a phone. Phase 4 gives the
account **one DEK**, wrapped once **per device**, and syncs the wrapped form as
an opaque blob the server cannot open. A second authorized session then needs
exactly two things it already has: a login session and the passphrase.

### Threat-model additions for sync

- **The passphrase is the secret; the session is not.** Whoever holds the
  passphrase can enroll a *new* device identity, and no server-side check can
  distinguish a new legitimate browser from a revoked one — the server cannot
  verify possession of the passphrase or of the DEK (it has neither), so any
  “proof of key” it could check would be a secret it already knows. Revocation
  is therefore enforced at the *fetch boundary* (below), and the passphrase
  remains the real control.
- **Session-token theft alone yields no keys.** `GET /vault/envelopes` hands
  out only passphrase-locked blobs the server could not have produced.
- **A hostile server can deny, not read.** It can withhold envelopes or serve a
  stale one. Both fail closed: no envelope → the vault stays locked; a stale
  blob unwraps to a key that no longer matches current ciphertext → GCM auth
  failure on the first field (`Field decryption failed — wrong key or tampered
  data.`), never plaintext.
- Unchanged and still out of scope: a compromised frontend bundle, malware on
  the user's machine, a forgotten passphrase (no recovery by construction).

### Key-holding model (Phase 4)

| Secret | Where it lives | Lifetime | Ever sent to the server? |
|---|---|---|---|
| Encryption passphrase | a local variable inside `enrollVaultSynced` / `unlockVaultSynced` / `rotatePassphrase`, plus the Settings form's React state while a form is open (cleared by `reset()` on success or Cancel; kept only so a failed call can be retried inside the same session — never persisted) | the single async call | **never** — no request body carries it |
| KEK | non-extractable `CryptoKey` returned by `deriveKEK()` | that wrap/unwrap call only | never |
| DEK + index key | module-scope `sessionDEK` / `sessionIndexKey` in `frontend/src/lib/vault.ts` | until `lockVault()` (Settings → Lock) or a page reload | never |
| Wrapped bundle `{v, salt, wrappedDEK, wrappedIndexKey, createdAt}` | IndexedDB (`fintrack-vault`, key `user_<id>`) **and** one `vault_envelopes.wrapped_blob` row per device | until the user rotates the passphrase or revokes the device | yes — **the blob only**, opaque, ≤ 8000 chars |
| `device_id` (32 hex chars) | `localStorage["fintrack-device-id"]` | this browser profile; clearing site data mints a new one | yes — not a secret |
| `label` (≤ 64 chars) | derived once from `navigator.userAgent`, sent on create | as long as the row lives | yes — **accepted plaintext metadata** (inventory row K6) |

**Session keys are extractable on purpose.** `unwrap()` imports the raw DEK and
index key with `extractable: true`. They must be exportable because (a)
`indexToken()` re-imports the index key as an HMAC signing key and (b) key
rotation re-wraps the same key bytes under a new KEK. A non-extractable import
breaks both — that was **audit finding F1**. The threat model already excludes a
compromised bundle, and a non-extractable `CryptoKey` would not stop an attacker
who can run JavaScript in the page; it only breaks legitimate callers.

### Table `vault_envelopes` (migration `0027_vault_envelopes`)

| Column | Type | Invariants |
|---|---|---|
| `id` | `INTEGER` PK | |
| `user_id` | `INTEGER NOT NULL` FK → `users.id` `ON DELETE CASCADE`, indexed | tenancy: every query filters on it |
| `device_id` | `VARCHAR(64) NOT NULL` | matches `^[0-9a-f]{32}$` (enforced by the schema, not the column) |
| `label` | `VARCHAR(64) NULL` | plaintext metadata |
| `wrapped_blob` | `TEXT NULL` | opaque; the server never decodes it |
| `revoked_at` | `TIMESTAMPTZ NULL` | soft revoke — rows are kept for audit |
| `created_at` / `updated_at` | `TIMESTAMPTZ NOT NULL` default `now()` | `TimestampMixin` |

Constraints (both created by the migration):

- `ck_vault_envelopes_state` CHECK: `(revoked_at IS NULL AND wrapped_blob IS NOT NULL) OR (revoked_at IS NOT NULL AND wrapped_blob IS NULL)` —
  an active envelope always carries its blob, a revoked one never does (the blob
  is dropped on revoke, so revocation also removes the copy the database held).
- `uq_vault_envelopes_active` UNIQUE `(user_id, device_id) WHERE revoked_at IS NULL` —
  at most one **active** envelope per device; a revoked row does not block
  re-creation, so history accumulates as separate rows.

### Demo reset (M63b) — what `POST /auth/demo/reset` does to this table

The table **is exported but never restored** (docs/11: the snapshot carries
your enrolment rows, and restore skips them because replaying an old snapshot
would un-revoke a revoked device), and `delete_user_rows` does not cover it
either, so the demo reset used to reach neither: because the reset keeps the
`users` row by design, the FK's `ON DELETE CASCADE` never fires, and a stray
envelope would have outlived every reset forever *and* stayed invisible to
`seed_fingerprint` (which reads only what the reset wipes).

Decision: **the reset wipes it.** Two reasons make that a no-op in the intended
state and a cleanup in every other one:

1. The demo tenant can never hold a legitimate server-side envelope —
   `POST /vault/envelopes` inherits `get_current_user`'s
   `403 demo_account_restricted` (table below), so demo enrolment stays
   local-only by construction.
2. The restore rule is about *replaying* old security state; a reset *wipes*
   rather than replays, so it cannot un-revoke anything — it can only fail to
   remove something that should not exist.

Implementation: `VaultEnvelope` sits in
`app/services/demo_seed.py::_RESET_EXTRA_MODELS` (next to M63's
`backup_artifacts`) and is deleted in the **same transaction** as the rest of
the reset; `seed_fingerprint` reads it, so the M50 fingerprint-equality
acceptance fails if the wipe ever disappears. Nothing about the crypto, the
endpoint contract or export/restore changed — this is the reset's data
lifecycle only. Client impact of a wiped demo row: the next unlock sees `items`
empty and takes the documented local-mirror path (`{ok:true,
source:"local"}`) or `no_envelope` → Enroll → the `POST` 403s and the vault
stays local-only — nothing bricks, because no demo device ever had server
state it could lose.

Pinned by `backend/tests/test_demo_seed.py::test_reset_clears_stale_vault_envelopes`
and `::test_every_user_owned_table_is_classified_for_demo_reset` (a future
`user_id` table with no documented reset fate fails the latter).

### Endpoint contract (documented in `docs/08` § Vault key sync (M53))

Router `backend/app/api/vault.py`, `APIRouter(prefix="/vault", tags=["vault"])`,
mounted in `app/main.py` under `/api/v1`. Every handler depends on
`get_current_user` (so the demo write guard and privacy/LLM locks apply
unchanged) and on `get_db`.

**Limits (module constants, pinned):** `MAX_ACTIVE_ENVELOPES = 10`,
`MAX_BLOB_CHARS = 8000` (same ceiling as the Phase-2 `invalid_cipher` check).

#### `GET /api/v1/vault/envelopes?device_id=<32 hex>` → 200

```json
{
  "items": [
    { "id": 1, "device_id": "3f2a…", "label": "Chrome · Linux",
      "wrapped_blob": "eyJ2IjoxLCJzYWx0IjoiLi4uIn0=", "revoked_at": null,
      "created_at": "2026-09-28T02:10:00Z", "updated_at": "2026-09-28T02:10:00Z" }
  ],
  "revoked_own_device": false,
  "active_count": 1
}
```

- `items`: **active** envelopes only (`revoked_at IS NULL`) for the caller,
  ordered `created_at ASC, id ASC` — deterministic, so enrollment always picks
  the oldest envelope. Includes *other* devices' blobs; that is the point of
  sync and is safe because every blob is passphrase-locked.
- `active_count`: `len(items)`.
- `revoked_own_device`: `true` iff there is **no active** row for `device_id`
  **and** at least one row (of any state) exists for it. This is the signal that
  makes revocation enforceable client-side (a device that merely has no row is
  “new”, a device that has only revoked rows is “cut off”).
- Query errors: missing/malformed `device_id` → **422 `validation_error`**;
  no/bad bearer token → **401 `invalid_token`**.

#### `POST /api/v1/vault/envelopes` → 201

```json
{ "device_id": "3f2a…", "label": "Chrome · Linux", "wrapped_blob": "eyJ2Ijox…" }
```

`extra="forbid"`. Response: the created envelope (201).

| Status | `code` | When |
| --- | --- | --- |
| 401 | `invalid_token` | missing/expired/bad bearer token (inherited) |
| 403 | `demo_account_restricted` | demo tenant — **inherited** from `get_current_user`; the demo cannot sync (it is shared plaintext sample data), so demo enrollment stays local-only |
| 422 | `validation_error` | wrong shape: missing field, unknown field, `device_id` not `^[0-9a-f]{32}$`, `label` longer than 64 |
| 422 | `envelope_too_large` | `len(wrapped_blob) > 8000` (checked in the handler, mirroring `invalid_cipher`) |
| 409 | `device_exists` | an **active** envelope already exists for `(user_id, device_id)` |
| 409 | `too_many_devices` | creating would exceed `MAX_ACTIVE_ENVELOPES` active envelopes |

#### `PUT /api/v1/vault/envelopes/{envelope_id}` → 200

```json
{ "wrapped_blob": "eyJ2Ijox…", "label": "Chrome · Linux (renamed)" }
```

`extra="forbid"`; `label` is applied only when present in the body
(`model_dump(exclude_unset=True)`). Used by **passphrase rotation** to re-wrap a
device's blob. Response: the updated envelope.

| Status | `code` | When |
| --- | --- | --- |
| 404 | `not_found` | unknown id **or** another user's id (tenancy indistinguishable by design) |
| 409 | `envelope_revoked` | the row is revoked — a revoked row is a tombstone; re-enrollment is a `POST`, never a `PUT` |
| 422 | `envelope_too_large` | `len(wrapped_blob) > 8000` |
| 422 | `validation_error` | wrong shape |
| 403 | `demo_account_restricted` | inherited (moot: the demo has no rows) |

#### `POST /api/v1/vault/envelopes/{envelope_id}/revoke` → 200

Sets `revoked_at = now()` and `wrapped_blob = NULL` (CHECK keeps the pair
consistent). **Idempotent**: revoking an already-revoked row returns 200 with the
same tombstone. Response: the envelope with `wrapped_blob: null`.

| Status | `code` | When |
| --- | --- | --- |
| 404 | `not_found` | unknown id or another user's id |
| 403 | `demo_account_restricted` | inherited |

There is deliberately **no** “re-enroll a revoked device identity” endpoint and
no “delete every envelope” endpoint: the first would make revocation a no-op,
the second would brick every browser (see the runbooks).

### Client contract (`frontend/src/lib/vaultSync.ts`)

Module constants: `DEVICE_ID_KEY = "fintrack-device-id"`, `MAX_BLOB_CHARS = 8000`
(client-side mirror of the server limit).

- `getDeviceId(): string` — reads `localStorage[DEVICE_ID_KEY]`; when absent,
  writes `crypto.getRandomValues(new Uint8Array(16))` hex (32 chars, lowercase).
- `deviceLabel(): string` — small UA parse → `"Chrome · Linux"`, `"Safari · iOS"`,
  …, falling back to `"Browser"`; truncated to 64 chars.
- `encodeBundle` / `decodeBundle` — `WrappedVault` ⇄ base64url of a JSON object
  `{v: 1, salt, wrappedDEK, wrappedIndexKey, createdAt}` (binary fields base64).
  `decodeBundle` validates the shape and throws `Not a wrapped vault bundle.`
  on anything else — a server that hands back garbage must fail closed.

`unlockVaultSynced(passphrase, deps) → UnlockResult`

```
UnlockResult = { ok: true,  source: "own" | "enrolled" | "local",
                  syncError?: string }      // sync failed, unlock did NOT
             | { ok: false, reason: UnlockFailure }
UnlockFailure = "wrong_passphrase" | "device_revoked" | "sync_unreachable" | "no_envelope"
```

1. `GET /vault/envelopes?device_id=` — a network/5xx failure returns
   `{ok:false,"sync_unreachable"}`: **unlock is fail-closed, there is no offline
   unlock in Phase 4** (without the API the app renders no data anyway, and
   fail-open would let a revoked device unlock from its stale local mirror).
2. `revoked_own_device === true` → drop the local mirror, return
   `"device_revoked"`.
3. Own active envelope present → `decodeBundle` + `unlockVault(passphrase, …)`;
   GCM failure → `"wrong_passphrase"`; success → refresh the local mirror,
   return `{ok:true, source:"own"}`.
4. No own envelope but `items` is non-empty → **enrollment**: unwrap the
   OLDEST envelope with the passphrase (wrong passphrase →
   `"wrong_passphrase"`), re-wrap the now-session keys under a **fresh salt** via
   `wrapSessionVault()`, `POST` the new envelope, refresh the local mirror,
   return `{ok:true, source:"enrolled"}`. This is what “sync” buys: one
   passphrase, two browsers, same DEK. **If that `POST` fails**, the local
   mirror is still written and the result carries `syncError`: unlike
   `enrollVaultSynced`, this path re-wraps a DEK that already exists, so a
   local-only mirror creates NO second key hierarchy — and the next unlock
   simply retries the `POST`. (Pinned by
   “refuses to enroll when the account already has a vault”, which proves the
   converse.)
5. `items` empty **and** a local mirror exists → `unlockVault(passphrase, mirror)`
   → `{ok:true, source:"local"}` (Phase-1 local-only vaults and the demo tenant,
   which can never `POST`). Wrong passphrase → `"wrong_passphrase"`.
6. `items` empty and no mirror → `"no_envelope"` (the panel offers Enroll).

`enrollVaultSynced(passphrase, deps) → EnrollResult`

```
EnrollResult = { ok: true, synced: true }
             | { ok: true, synced: false, code: "demo_account_restricted" }
             | { ok: false, reason: "device_revoked" | "sync_unreachable"
                                  | "already_enrolled_elsewhere"
                                  | "sync_failed", message: string }
```

1. `GET` first — **a new DEK may only be minted when the server positively
   reports zero active envelopes.** A failed `GET` returns
   `"sync_unreachable"`; a `GET` that reports `items.length > 0` returns
   `"already_enrolled_elsewhere"` (the caller must use unlock, never mint a
   second DEK — two DEKs would make each device's ciphertext unreadable to the
   other); `revoked_own_device` → `"device_revoked"`.
2. `createVault(passphrase)` (session unlocked, bundle in hand).
3. `POST` the envelope. Success → `storeVault()` → `{ok:true, synced:true}`.
   `403 demo_account_restricted` → `storeVault()` anyway →
   `{ok:true, synced:false, code:"demo_account_restricted"}` (the demo is
   plaintext-by-design and permanently local-only). Any other `POST` failure →
   `lockVault()` (nothing is enrolled, the ephemeral keys are dropped) and
   `{ok:false, reason:"sync_failed", message}`.

`rotatePassphrase(newPassphrase, deps) → RotateResult` where
`RotateResult = { ok: true, replaced: number, active: number }`
| `{ ok: false, replaced, active, reason: "vault_locked" | "sync_unreachable"
| "sync_failed", message?: string }` — see the runbook below.

`revokeEnvelope(envelopeId, deps)` — thin `POST …/revoke` wrapper.
`listVaultEnvelopes(deps)` — the Settings device list reads through this.

All of them take an injectable `VaultSyncDeps` (`deviceId` / `userId` / `label` /
`list` / `create` / `replace` / `revoke` / `loadLocal` / `storeLocal` /
`clearLocal`) with `defaultSyncDeps(userId)` wired to
`frontend/src/api/vault.ts` + `lib/vault.ts`, so
the multi-device tests run against an in-memory fake that enforces the same
rules as the router (same error codes, same `revoked_own_device` semantics).

### Runbook — lost device (revoke)

1. On a surviving device: Settings → Encryption → Devices → **Revoke**.
   The row becomes a tombstone (`wrapped_blob` dropped, `revoked_at` set) and
   disappears from `GET /vault/envelopes`.
2. The revoked browser's next unlock fetches, sees `revoked_own_device: true`,
   **deletes its own IndexedDB mirror** and fails with `device_revoked`. It can
   no longer unwrap — not from the server, not from its own disk.
3. Escape hatch (deliberate): clearing this site's data mints a **new**
   `device_id`, which is indistinguishable from a new browser and takes the
   normal enrollment path. That is not a hole, it is the analysis in
   “Threat-model additions”: a holder of the passphrase can always enroll
   somewhere. Revoke cuts a device *identity*, not the passphrase.

### Runbook — rotate the passphrase

`rotatePassphrase(newPassphrase)` re-wraps **every active envelope** under a KEK
derived from `newPassphrase` + a **fresh per-device salt**, leaving the DEK, the
index key and every ciphertext byte untouched (so no data is rewritten).

Order, which makes the operation recoverable at every step:

1. `GET` the envelope list (same fail-closed rules as unlock).
2. Wrap the session keys under `newPassphrase` + fresh salt for **this** device
   and `PUT` it first. If that fails, abort — nothing else was touched, every
   device still unlocks with the old passphrase.
3. `storeVault()` the new local mirror (so a reload mid-operation still unlocks).
4. `PUT` the remaining active envelopes. A failure here leaves those devices on
   the old passphrase — **recoverable**: the rotating device still unlocks and
   `rotatePassphrase` can be re-run (each `PUT` is stateless and idempotent in
   effect). Return `{replaced, active}` so the UI can say exactly how many
   devices are still on the old passphrase.

What passphrase rotation does **not** buy, stated plainly: the DEK is unchanged,
so a blob copied out of the database *before* the change still unwraps to the
same DEK under the *old* passphrase. Closing that requires **DEK rotation**
(new DEK + index key + re-encrypting every cipher column and every blind-index
token client-side) — tracked as audit finding **F5**, not implemented here
(non-atomic data migration; see the inventory table for why it was not bundled
into this card).

## Server-first computing (M134/M136, standing directive)

Architecture default for ALL new and migrated features:

1. Aggregations, counts, groupings, rankings, chart data and list envelopes are
   ALWAYS computed server-side and shipped in their final shape. The browser
   renders; it does not derive.
2. E2EE changes what is STORED, not what can be computed. `encryption_required`
   stays a write-time cipher-presence guard. Non-content compute (sums, counts,
   groupings, rankings over the mandatory plaintext columns) MUST work in full
   under E2EE — a surface that degrades there is a BUG, logged and fixed, not an
   accepted tradeoff.
3. Content resolution (decryption) stays client-side: `lib/e2ee.ts` is the only
   place a payee, description or amount becomes text.
4. Graceful degradation is a contract, not an accident:
   - a surface that WITHHOLDS encrypted rows announces it (capability-named,
     unlock in place, `role="status"`) — never a silent empty list;
   - a surface UNAFFECTED by the lock carries NO lock copy — a caveat on an
     unaffected surface is a false claim;
   - the withheld-vs-unaffected classification is part of the feature's
     acceptance criteria, tested with an ENROLLED (`encryption_required=true`)
     fixture account, not eyeballed.
5. Client-side mirrors of server math are a LAST-RESORT degradation path for
   E2EE tenants and offline rendering. While a mirror exists it MUST stay
   output-identical to the server (golden vectors, zero drift) and the parity
   harness stays alive. Neither the mirrors nor the harness retire until a
   later wave says so.
   - **[EXERCISED 2026-10-11, M160 / M134 P2-5 — the mirrors retire.]** M158
     Decision 2 (option c) ruled the retirement and M160-R1 (t_4fea992a)
     settled the five execution breakages. Both legs of the last-resort
     justification are void: server compute works fully under E2EE
     (`encryption_required` is a write guard only — it branches zero read
     paths), and the offline leg never had runtime wiring (no service worker,
     no offline model; the "fallback" framing was comment-only). Deleted:
     `lib/reportEngine.ts`, `lib/s3Engine.ts`, `lib/totalReturnEngine.ts`,
     the six client-comparison parity adapters and nine mirror tests.
   - **What survived the retirement, and why it is not a mirror.** Two
     residents were never mirrors and keep their own homes: the fixed-point
     primitives (`lib/dec.ts` — the split-editor money math and
     `lib/sankeyEngine.ts` still run on them) and the partner-view budget
     progress (`lib/budgets.ts` — an AUDITED CLIENT-ONLY Row-8 compute with
     no server twin by D3 design, see the rule-8 inventory). The third
     resident — the three panel loaders behind dividends/sankey/total-return
     (`lib/reportClient.ts`) — was always temporary: **[M160-R2,
     t_85311c34]** those panels were re-pointed at their own routes and the
     shim deleted, so NO report the UI renders is computed in the browser
     anymore.
   - **The harness did not retire; its comparison half did.** The goldens and
     `capture.ts` survive as SERVER-ROUTE REGRESSION PINS: M158 kept them
     precisely so the captured live-stack JSON keeps gating the mounted
     routes. The offline engine-vs-golden adapters are gone; the replay
     adapters seed the captured inputs into a throwaway tenant, replay the
     captured request against the route and byte-diff the answer
     (`backend/tests/test_m160_replay_*.py`, on the `test_search_goldens.py`
     pattern). The `projection` slice is unchanged — it was already a live
     server replay.
6. Write paths obey the same contract as read paths: a client-initiated write
   that cannot produce its ciphers (vault locked) is DISABLED WITH REASON
   before submit — never a post-hoc 422 on a save the user already performed.
7. Honesty copy (adopted verbatim from the M134 UX review; the four lock-state
   constants and the canonical destination live in
   `frontend/src/components/e2ee/VaultLocked.tsx`):
   a. gate the notice on EXCLUDED DATA, not on lock state — a surface the lock
      does not affect carries NO lock copy at all;
   b. name the capability, not the mechanism ("…to search encrypted payees",
      never "resolve cipher rows" / "DEK" / "index key");
   c. unlock IN PLACE — inline passphrase, one interaction, zero navigation;
      the canonical destination string, until inline unlock ships, is
      "Settings → Privacy & data → Client-side encryption (in progress)";
   d. a remedy named in the copy must actually restore the feature at that
      moment — "disable E2EE" is BANNED until a decrypt flow exists;
   e. scope the outage honestly — "encrypted payees are hidden", never the
      whole surface when only part of it is down;
   f. a lock hint REPLACES an empty state, never stacks with it;
   g. one string per capability: ≤4 lock-state constants in one module.
8. Standing gate: every NEW client compute ships with its docs/33 inventory
   row + disposition, and one locked/enrolled case in the degradation test
   family. A client compute without a disposition is a failed gate.

### §Rule-8 inventory — every client compute, with its disposition (M156)

Rule 8 above was **unenforced** until this milestone: at the time it was
filed, `grep -rn 'disposition' frontend/tests/ frontend/scripts/parity/engines/`
returned **0 hits** — the rule existed only as prose (M134 P1-4, M135 P2-11,
qabot G6). This table is rule 8 made machine-readable, and
`frontend/tests/lockedVaultDegradation.test.ts` → *"rule 8 standing gate —
every client compute carries a docs/33 row + disposition"* reads it back. Two
checks, both derived from the tree and from this document, so neither has to
be remembered:

**C1 — the enumeration (the table below).** A *client compute* is

1. any `export function` declared in a `frontend/src/lib/*Engine*.ts` file —
   `projectionEngine`, `sankeyEngine` (M160 retired the other three:
   `reportEngine`, `s3Engine`, `totalReturnEngine`; their surviving
   non-mirror residents moved to `lib/dec.ts` and `lib/budgets.ts`, and the
   temporary `lib/reportClient.ts` shim was deleted by M160-R2 when its three
   panels were re-pointed at the server routes); or
2. any exported function in `frontend/src/api/*.ts` whose body either
   references a symbol imported from `../lib/…` (it runs a browser engine) or
   transforms a value with `.map(` / `.filter(` / `.reduce(` / `.sort(` (it
   reshapes data rather than only issuing the HTTP call); or
3. any `frontend/src/lib/*.ts` module — Engine-named or NOT, which is the
   whole point of this clause: `positionGroups`, `exposureFold` and `impact`
   shipped as live browser computes precisely BECAUSE they omitted "Engine"
   from the filename and walked past clause 1 (t_2d75948c) — that imports
   from `../api/…` (value or type: the payload reaches it through its wire
   types) and whose EXPORTED function body transforms a value with `.map(` /
   `.filter(` / `.reduce(` / `.sort(` — clause 2's transform test, pointed at
   `lib/` instead of `api/`, per export like every other clause; or receives
   its payloads with NO `../api/` import at all, which text matching cannot
   see, so this document names those modules here and the gate pins their
   computes by name: `lib/impact.ts` (`dayDelta`, `liveImpact`,
   `livePortfolio` — M129/M130, mirror interfaces declared locally) and
   `lib/spendingFold.ts` (`foldCategoryDonut`, `diffCategories` — M185,
   docs/47 §9.1). A receive-only fold not yet named here is added to this
   clause IN THE SAME CHANGE that adds its row.

Every one of them must have a row below, named exactly. A missing row, a
duplicated one, or a blank `Disposition` fails the gate as loudly as the
other. An exported constant, interface or error class is not a compute, and a
pure fetch wrapper is not one either — `getSpending()` asks the server for a
report; `getSpendingOver()` computes one, which is why only the second is
here.

The clause-3 boundary is written, not implied. A pure formatting helper is
not a compute even when it lives in `lib/` and its input is money-shaped:
`format.ts` and `percent.ts` (display glue over numbers somebody else
computed), `mask.ts` (the session mask itself), and `impact.ts`'s
`fmtCadSigned` / `fmtSigned` / `fmtSignedPct` / `fmtSignedPctBookCost` /
`deltaArrow` / `toCents` (format or quantize an input; derive nothing).
Neither is a non-money lens over payload-shaped values: `treemap.ts`
(squarified layout — areas from given values, re-derives no dollar figure),
`tagRecency.ts` (tag-id ordering), `accountGroups.ts` (static label tables),
`localExports.ts` (export-file metadata), `createFromSearch.ts` (URL
criteria encoding), `webauthn.ts` (WebAuthn plumbing), and `billCoverage.ts`
(a boolean payee-match decision — the browser port of
`services/match_terms.py` — whose `.map` is the term-hit scan inside a
module-private helper; it re-derives no figure). `vaultSync.ts` / `fields.ts`
byte↔hex glue over their OWN key material is out for the same reason: no
payload is in sight. `searchMatch.ts` is out under A3 — server-first since
M137 P0-1, retained as the E2EE/offline fallback. Where a clause does
mechanically match a module that genuinely reshapes nothing money-bearing,
the row still ships and says so in one sentence — that is how
`api/reports.ts::toSlices` and `api/bills.ts::describeTerms` already read.
Over-capture costs an honest sentence; escape costs the audit.

**C2 — the prose half (§"Compute moves client-side").** That section is the
prose inventory of what runs in the browser, so it must not be able to drift
from the audit above it: every bullet there that names a client module must
also name an audit row (`A1`…`A20`) or an inventory finding (`F1`…`F10`) that
covers it, or say **in the bullet itself** that no A-row applies and why. The
gate checks that mechanically; the alternative is two independent inventories
that disagree silently, which is the exact rot this section exists to stop.

The dispositions below are the classification M135 §2 already ruled — **server
is authoritative / the mirror is the rule-5 E2EE-offline fallback / client-only
legitimate** — one row per compute, so "what happens to this path, and under a
locked vault" is answerable without re-deriving it.

| Module (`frontend/src/`) | Compute | Disposition |
| --- | --- | --- |
| Module (`frontend/src/`) | Compute | Disposition |
| --- | --- | --- |
| `lib/projectionEngine.ts` | `ProjectionInputError` | Error type, not a compute — mirrors the server's 422 validation envelope for `POST /projections/run`. Lock-vacuous. |
| `lib/projectionEngine.ts` | `validateProjectionRequest` | Request validation mirroring the server's 422 envelope — rejects bad scenario input before any math runs. Pure rate input, lock-vacuous; pinned by `projection-parity.json`. |
| `lib/projectionEngine.ts` | `runProjectionEngine` | dual-mode (M135 §2) — the server twin `POST /projections/run` exists and is unused; the browser run is pure rate math, pinned by `projection-parity.json`. Lock-vacuous (no row content). |
| `lib/dec.ts` | `EngineError` | Error type, not a compute — carries the server's `{detail, code}` shape so a client-raised failure renders exactly like a server one. Lock-vacuous. Extracted from `lib/reportEngine.ts` by the M160-R1 ruling; the report engines it served are retired. |
| `lib/dec.ts` | `pow10` `dec` `dzero` `dadd` `dsub` `dmul` `dabs` `dneg` `dcmp` `disZero` `dround` `dstr` `dcents` `quantizeCents` `cents` | The fixed-point (`Dec`) primitive cluster — exact BigInt decimal math mirroring the server's Python `Decimal`. Never touches a row, so lock state is vacuous. Moved verbatim out of `lib/reportEngine.ts` (M160-R1) for the surviving client computes: the split-editor money math (`SplitEditor.tsx`, M107/docs/44), `lib/sankeyEngine.ts`, and the deprecated `lib/reportClient.ts` shim. `lib/budgets.ts` deliberately keeps its OWN ROUND_HALF_EVEN `dquantize`/`ddivSig` — merging the two rounding conventions would move `pct_used`'s golden. |
| `lib/dec.ts` | `utc` `iso` `bucketStarts` `bucketEnd` `isoAfterDays` `utcToday` | Date/bucket primitives — no row data, so lock state cannot move them. Moved verbatim out of `lib/reportEngine.ts` (M160-R1). |
| `lib/dec.ts` | `countOnOrBefore` | Position/day count over plaintext `date` — non-content compute (rule 2), lock-vacuous. Moved verbatim out of `lib/reportEngine.ts` (M160-R1). |
| `lib/dec.ts` | `groupPrices` | Price grouping over cache-only reads — non-content compute over mandatory plaintext columns (rule 2), lock-vacuous. Moved verbatim out of `lib/reportEngine.ts` (M160-R1). |
| `lib/dec.ts` | `ratesAsc` | fx-range sort over the cache-only `fx_rates` read — non-content compute over mandatory plaintext columns (rule 2), lock-vacuous. Moved verbatim out of `lib/reportEngine.ts` (M160-R1). |
| `lib/budgets.ts` | `budgetsProgress` | **AUDITED CLIENT-ONLY (Row-8 class, M160-R1 ruling 2).** There is NO server twin of this math: `GET /partner/budgets` deliberately returns budget definitions only (D3 / docs/43 §9 — the server must not read the owner's hidden accounts into the partner's answer), so `api/sharing.ts::loadSharedView` computes `spent`/`carried_in`/`remaining`/`pct_used` over the PARTNER-visible rows it already holds. M135 §4.1 parity is unsatisfiable BY DESIGN, not by defect, so no parity pass is claimed here. Lock behavior: lock-vacuous — it reads only mandatory plaintext columns (`cad_amount`, `amount_native`, `currency`, `date`), so an enrolled+locked and an unlocked vault produce byte-identical output; pinned by `tests/lockedVaultDegradation.test.ts` → “A13” and by the frozen golden `s3-vectors.json::budgets.progress`. |
| `lib/budgets.ts` | `etToday` | Date primitive (`today_et()`, America/Toronto) for the budgets path — no row data, lock-vacuous; the injected clock the A13 golden pins against. Carved out of the retired `lib/s3Engine.ts` (M160-R1 ruling 2). |
| `lib/sankeyEngine.ts` | `cashflowSankey` | **server-owned since M160-R2 (t_85311c34), engine retained as the reference implementation.** `SankeyPanel` reads `GET /reports/cashflow-sankey`, so no production caller runs this compute in the browser anymore; M160-R1 ruling 3 kept the module in its own home (it was never on M158's deletion list) and it still owns the `SankeyReport` wire shape the chart renders. Golden `tests/fixtures/m77-vectors.json`, now replayed against the ROUTE by `backend/tests/test_m160_replay_m77.py`; lock-vacuous (reads plaintext columns). |
| `lib/retirementEngine.ts` | `RetirementInputError` | Error type, not a compute — carries the offending field name for the page's local input validation (docs/57 §2), mirroring the `{detail, code}` envelope's spirit without a server call. Lock-vacuous: raised from caller-supplied numbers only. |
| `lib/retirementEngine.ts` | `validateRetirementInputs` `quantile` | Bounds validation plus the R-7 percentile over a caller-supplied sorted series — pure number functions, no payload, no rows, no clock. Lock-vacuous; pinned by `tests/retirementEngine.test.ts` R4 (quantile) and R6 (bounds). |
| `lib/retirementEngine.ts` | `runRetirementSimulation` | **AUDITED CLIENT-ONLY (M188, docs/57 §2).** The retirement Monte Carlo: seeded paths (mulberry32 + Box–Muller, test-seamable `drawNormal`) over the starting portfolio and the editable assumptions, with the `TaxEngine` seam called once per retirement year per path. There is NO server twin BY DESIGN — the simulation is the feature and must run in the browser on data the app already fetched (`GET /reports/net-worth`, `GET /reports/cashflow`); no A-row applies because the compute issues no endpoint of its own. Lock behavior: lock-vacuous — inputs are mandatory-plaintext aggregates (`net_cad`, `income_cad`, `expenses_cad`) and user-typed numbers; an enrolled+locked and an unlocked vault produce byte-identical output. Pinned by `tests/retirementEngine.test.ts` (R1–R6, incl. the scripted-draw ruin probability) and `tests/retirementPage.test.tsx` (R7–R11, incl. R13's ca-engine path pin). |
| `lib/lifeEventEngine.ts` | `LifeEventInputError` | Error type, not a compute — carries the offending event/field name for the page's local validation (docs/58 §4). Lock-vacuous: raised from caller-supplied numbers only. |
| `lib/lifeEventEngine.ts` | `lifeEventType` `validateLifeEvents` | Registry lookup plus full event-set validation (identity, year range, registry field bounds, the one-early-retirement and sabbatical-before-the-switch cross-rules) over caller-supplied numbers — reads only the module's own registry table, no payload, no clock. Lock-vacuous; pinned by `tests/lifeEventEngine.test.ts` L1–L5. |
| `lib/lifeEventEngine.ts` | `buildEventFlow` `effectiveRetirementAge` `planTimeline` | **AUDITED CLIENT-ONLY (M189, docs/58 §3).** The per-step CAD deltas (`eventFlow`), the early-retirement phase-switch age and the year-by-year display table — pure folds over validated events and the page's committed assumptions. There is NO server twin BY DESIGN — the plan IS the browser compute, consuming the two report reads the page already makes; no A-row applies because the compute issues no endpoint of its own. Lock-vacuous: user-typed numbers only, no cipher column and no key material — an enrolled+locked and an unlocked vault produce byte-identical output. Pinned by `tests/lifeEventEngine.test.ts` (L4–L8) and consumed through `retirementEngine`'s `eventFlow` (`tests/retirementEngine.test.ts` R12), page behaviour in `tests/lifeEventPage.test.tsx` (E1–E6). |
| `lib/taxEngine.ts` | `flatRateTaxEngine` | The `TaxEngine` seam's reference implementation (docs/57 §3): `gross = netSpend / (1 − rate/100)` over caller-supplied numbers — deliberately NO tax table, no rows, no payload. Since t_1ab83b11 the `/retirement` page constructs `caTaxEngine()` instead (docs/58 §Relationship to the server); the stub stays exported as the seam's fallback and is still pinned by `tests/retirementEngine.test.ts` R5/R6. Lock-vacuous: nothing encrypted is in sight. |
| `api/accounts.ts` | `filterInvestmentAccounts` | Presentation filter over the account list `GET /accounts` already returned — no aggregation, no ledger read; lock-vacuous. |
| `api/bills.ts` | `describeTerms` | Shapes the server's match-term result for display — no ledger compute; lock-vacuous. |
| `api/notifications.ts` | `criticalBudgetItems` | Filters the server's inbox payload for display — no ledger compute; lock-vacuous. |
| `api/reports.ts` | `toSlices` | Presentation reshape of the SERVER's spending payload (M137 P0-2 thin read) into chart slices — no aggregation, no user data; lock-vacuous. |
| `api/reports.ts` | `breakdownSlices` | Presentation reshape of the SERVER's category-breakdown payload (M137 P0-2 thin read) into donut slices — no aggregation, no user data; lock-vacuous. |
| `api/s3Inputs.ts` | `loadFallbackRates` | Cache-only fx fallback filter over the shared `fx_rates` cache (no network, docs/11) — lock-vacuous. Its `RateRow` type import now points at `lib/budgets.ts` (M160-R1 ruling 2). |
| `api/sharing.ts` | `loadSharedView` | D3 partner view: runs `lib/budgets.ts::budgetsProgress` over PARTNER rows only with the owner's category metadata (docs/43 §6) — the audited client-only Row-8 class above. Server-first read; lock-vacuous (plaintext columns, A19). |
| `lib/positionGroups.ts` | `groupPositionsBySecurity` | **Presentation fold over a server payload, not report math (t_2d75948c; clause 3).** M146's BY-SECURITY lens over `GET /portfolio/positions`: groups the rows the page already holds and sums the SERVER's own cent-quantized fields — no re-valuation, no client fallback, nulls stay null (docs/09), and the portfolio-level Σ row deliberately stays the payload's own totals. The server route stays authoritative; under strict mode (see the Product decision at the top of this document) the served payload is the contract — a masked or absent value must stay masked/absent through the fold, never re-materialized from a stale local figure. Reads plaintext today (mandatory columns) — lock-vacuous until strict mode exists; disposition: extend-as-lens, flag at strict-mode enablement. Pinned by `tests/m146PositionsBySecurity.test.tsx`. |
| `lib/exposureFold.ts` | `foldRows` | **Presentation fold over a server payload, not report math (t_2d75948c; clause 3).** M167's ONE top-N + `Other` rule behind every Exposure card (docs/09 §Exposure visuals): sorts the server's breakdown rows, keeps the head, and gives `Other` the exact cent-sum of the rows it folds (head + Other === the full list, `Unclassified` never folded — pinned by `tests/m167ExposureFold.test.ts`). The server route stays authoritative; under strict mode the served payload is the contract — a masked or absent value must stay masked/absent through the fold, never re-materialized from a stale local figure. Reads plaintext today — lock-vacuous until strict mode exists; disposition: extend-as-lens, flag at strict-mode enablement. The per-session expansion state (`setExpandedDimension` / `useExpandedDimension`) is UI memory, not a compute. |
| `lib/impact.ts` | `dayDelta` `liveImpact` `livePortfolio` | **Presentation folds over server payloads, not report math (t_2d75948c; clause 3's receive-only limb — pinned by name).** M129/M130: re-evaluate the server's OWN primitives (quantity, the SSE-patched price, the server's pinned prior close, `fx_rate_to_cad`) at the price on screen — provisional by construction (half-up vs the server's HALF_EVEN, ≤0.5¢/row), labelled "live", and replaced wholesale by the next server payload. The positions TABLE never uses this module; its valuation stays server-owned end to end. The server route stays authoritative; under strict mode the served payload is the contract — a masked or absent value must stay masked/absent through the fold, never re-materialized from a stale local figure. Reads plaintext today — lock-vacuous until strict mode exists; disposition: extend-as-lens, flag at strict-mode enablement. Its `fmtCadSigned` / `fmtSigned` / `fmtSignedPct` / `fmtSignedPctBookCost` / `deltaArrow` / `toCents` exports are display glue (C1's written boundary), not computes. |
| `lib/spendingFold.ts` | `foldCategoryDonut` `diffCategories` | **Presentation folds over server payloads, not report math (t_2d75948c; clause 3's receive-only limb — pinned by name).** M185 (docs/47 §9.1/§10): the donut fold (top 6, exact folded remainder in `Other`, `category_id: null` never folded) and the category-delta chips (two reads of the same endpoint, absolute $50 floor, ≤3 chips) over `GET /reports/spending/categories` shapes — pure functions over payload Decimal strings, quantizing to cents only, no clock and no network. Consumed today by `tests/ledger-ui-build/ledger-folds.test.ts` alone — no production caller yet, so the row ships BEFORE the wiring can land it row-less. The server route stays authoritative; under strict mode the served payload is the contract — a masked or absent value must stay masked/absent through the fold, never re-materialized from a stale local figure. Reads plaintext today — lock-vacuous until strict mode exists; disposition: extend-as-lens, flag at strict-mode enablement. |
| `lib/canadaTaxEngine.ts` | `CanadaTaxInputError` | Error type, not a compute — carries the house `{detail, code}` spirit as `code` (`negative_amount`, `tax_year_not_published`, …) so a client-raised tax failure names its reason exactly. Lock-vacuous. docs/58 (t_52ed8d01). |
| `lib/canadaTaxEngine.ts` | `incomeTax` `grossWithdrawalFor` `caTaxEngine` | **Canadian tax engine v1 income-tax core (docs/58, t_52ed8d01).** Client-only: no server twin exists yet (the server's `app/services/tax_projection.py` is untouched by v1 — parity is future work, stated in docs/58 §Relationship to the server, not claimed here). Reads NO rows and NO `../api/` payloads — integer-cent math over the static tables in `lib/canadaTaxTables.ts` (federal + 13 jurisdictions, per-year, CRA-cited) — so an enrolled+locked and an unlocked vault produce byte-identical output. `grossWithdrawalFor` is exact integer bisection, `caTaxEngine` the `docs/57` simulator seam. Pinned by `tests/canadaTaxEngine.test.ts` (23 tests). Lock-vacuous. |
| `lib/canadaTaxEngine.ts` | `cppRetirementMonthly` `oasMonthly` `oasRecoveryTax` | CPP/OAS timing estimates (docs/58): published age-65/65+ maxima × the Service Canada start-age factor rule, and the 15% recovery-tax estimator capped at the OAS received. Client-only, no server twin, no row reads — pure functions over the year's tables; lock-vacuous. Pinned by `tests/canadaTaxCppOas.test.ts`. |
| `lib/canadaTaxEngine.ts` | `rrspRoom` `rrspOverContribution` `tfsaRoom` `fhsaRoom` | Registered-account room calculators (docs/58): CRA deduction-limit formula, the $2,000/1% over-contribution rule, cumulative TFSA limits since 2009, FHSA annual/lifetime caps. Client-only, no server twin, no row reads — static published limits only; lock-vacuous. Pinned by `tests/canadaTaxRegistered.test.ts`. |
| `lib/canadaTaxEngine.ts` | `respCesg` `respClb` `rrifMinimum` `lifMaximumWithdrawal` `liraSmallBalanceUnlock` `spousalRrspAttribution` | RESP grant/CLB estimators, RRIF s.7308 factors, the FSRA LIF maximum (annuity-due at 6%, verified against the published table), Ontario LIRA small-balance unlocking, spousal 3-year attribution (docs/58). Client-only, no server twin, no row reads — statutory formulas over static tables; lock-vacuous. Pinned by `tests/canadaTaxRegistered.test.ts`. |

### §5.1 — the G1–G3 write-path guards (M136 P0-3, "a locked vault destroys work")

Rule 6's one bug class, fixed: a client-initiated write that cannot produce its
ciphers while the vault is locked. All three now gate PRE-EMPTIVELY — the
control that cannot run is replaced by a notice BEFORE anything is typed — and
the 422 stays as defense-in-depth. Which notice depends on WHO writes the row:
G1/G2 (the browser can produce those ciphers once the vault is open) use the
shared `VaultLocked` `blocked` notice carrying the canonical destination
string; G3's commit is a SERVER-side upload, so since the F4 follow-up
(2026-11-07, PM option A) it uses `ServerSideBlocked
capability="import:ynab"` — keyed on ENROLMENT ALONE with no unlock remedy,
because unlocking cannot enable it (rule 7d). Pinned by
`frontend/tests/lockedVaultWriteGates.test.tsx` (14 cases: enrolled → gated;
NOT enrolled → the gate is silent, because a plaintext tenant's write cannot
422; G1/G2 additionally assert enrolled+unlocked → the control returns on
`vaultchange`, while G3 asserts it does NOT).

| G | Surface | The failure | The gate (M136) | Server defense-in-depth |
| --- | --- | --- | --- | --- |
| G1 | Split editor (`SplitEditor.tsx`) | `submit()` dual-wrote `amount_cipher` only `if (isUnlocked())` → locked + enrolled ⇒ a GUARANTEED 422 on the legs the user had typed | the Save control is REPLACED by `VaultLocked blocked` (`capability="write:split"`); `canSubmit` is false while `cipherWriteBlocked` | `POST /transactions/{id}/split` refuses a part without `amount_cipher` (422 `encryption_required`) |
| G2 | Bulk matched-rename (`BulkActionsToolbar.tsx`) | the matched path sends payee-only and the page path drops to payee-only when locked ⇒ every matched-rename 422s for an enrolled tenant | "Set payee" is disabled WITH the reason (`aria-describedby` → the notice); the rename mode cannot be entered | `PATCH` needs `payee_cipher` + `amount_cipher` under the flag (422 `encryption_required`) |
| G3 | YNAB commit (`YnabMigrationCard.tsx`) | the server refuses an enrolled tenant outright; the UI only discovered this AFTER the commit, through a generic ErrorPanel | the Import control is REPLACED by `ServerSideBlocked` (`capability="import:ynab"`, enrolment alone — F4 follow-up, 2026-11-07); it was `VaultLocked blocked` (`capability="write:ynab"`, `enrolled && !unlocked`) until that gate was found to offer "unlock the vault" for a write unlocking cannot enable | `POST /imports/ynab/commit` refuses an enrolled tenant (422 `encryption_required`, capability phrase `Importing a YNAB budget file`); an `encryption_required` commit error renders the same capability notice, any other error keeps the raw envelope |

Assert rule: all three gate pre-emptively; the frontend test asserts the
PRE-EMPTIVE branch, never the catch.

### §Locked-fixture tenant (M136 §0-a)

The fixture every wave-13a gate stands on — a tenant locked BY CONSTRUCTION:

- `backend/app/services/locked_fixture.py::ensure_locked_fixture_tenant` seeds
  `locked-fixture@fintracker.local` with `encryption_required=TRUE`, 2 accounts
  (both with `name_cipher`), and 8 transactions: 7 carrying a full cipher
  bundle (`payee_cipher`, `raw_desc_cipher`, `amount_cipher`,
  `cad_amount_cipher`, `payee_token`) and 1 deliberately PLAINTEXT legacy row
  (docs/33 A16's mixed-mode contract, so the honest partial state has a real
  row to show).
- **No `vault_envelopes` row exists for it** — that is the property that makes
  it usable: there is no wrapped bundle anywhere to unlock with, so every
  browser starts LOCKED with zero per-device setup. `useVaultEnrollment()`
  still answers "enrolled" (the write guard alone proves a vault story), so
  the shell chip and the write gates are armed.
- Seeded on demand, NEVER from the lifespan (a fixture tenant has no business
  in production): `backend/scripts/seed_locked_fixture.py`, the wave's own
  `backend/tests/test_locked_fixture_tenant.py`, or an operator on a dev DB.
- Gated by the SAME `SEED_DEMO_ACCOUNT` kill switch the demo uses; with it off
  the seed returns `None` and writes nothing (pinned by
  `test_kill_switch_honored`).
- The test suite pins the whole contract: enrollment + lock-by-construction,
  idempotence, `/users/me` carrying the write guard, the G1 split-save and G3
  YNAB-commit 422 envelopes, the signed-amount sum (3428.08 across the 8 rows),
  and the kill switch (7 tests, real Postgres).

## Hardening audit — S1…S5 locked-vault degradation

Reading rule: “replaced” = the server route is gone and the client computes;
“kept” = the route still serves CSV/`html`/API consumers and must stay
numerically identical. **Locked** always means `isUnlocked() === false`.

| # | Route (S-slice) | Status | Locked-vault behavior — the designed claim | Proof |
| --- | --- | --- | --- | --- |
| A1 | `GET /search` (S1) | kept (blind index only) | No `payee_token` is computed or sent while locked (`blindIndexTokenForQuery` returns `undefined`), and cipher rows contribute `""` to client fuzzy matching — encrypted payees neither hit nor render plaintext | `frontend/tests/lockedVaultDegradation.test.ts` → “A1: no blind-index token while locked” + “A1: cipher rows never match and never render plaintext” + `backend/tests/test_search.py::test_search_groups_are_indifferent_to_encryption_required[False/True]` (P1-5 pattern) |
| A2 | `GET /transactions/similar` (S1, M12) | **server-first route (M137 P0-1)** | The server ranks on `normalized_payee` (mandatory plaintext), so it matches cipher rows too — a LOCKED client asks with `hide_encrypted=true` and gets only what it could display plus `withheld`, the honest count. M119's copy survives: an empty result while `withheld > 0` still says unlock, never “No similar past entries”. A non-E2EE tenant has no cipher rows → `withheld: 0` → no lock copy at all (uxbot's rule: gate on excluded data, not lock state) | `backend/tests/test_search.py::test_similar_route_ranks_the_payee_family` + `test_similar_hides_encrypted_rows_only_when_asked` + `m119SimilarPopover.test.tsx` → “5: a locked vault with withheld siblings says unlock, never 'no similar'” + `backend/tests/test_search.py::test_similar_route_is_indifferent_to_encryption_required[False/True]` (P1-5 pattern) |
| A3 | `GET /transactions?q=` (S5) | **server-first route (M137 P0-1)** | The server ranks and thresholds; a locked client sends `hide_encrypted=true` and the page reports `withheld`, so a cipher row never renders its payee while locked and the empty state is a real count (the A18 substrate) rather than silence. `lib/searchMatch` stays as the E2EE/offline fallback | `backend/tests/test_search.py::test_list_q_ranks_the_search_view_server_side` + `test_list_q_without_hide_encrypted_reports_every_match` + `test_search_goldens.py` (the 10 live s5 goldens) + `lockedVaultDegradation.test.ts` → “A3: the search page excludes cipher rows and shows no plaintext” + `backend/tests/test_search.py::test_list_q_ranking_is_indifferent_to_encryption_required[False/True]` (P1-5 pattern) |
| A4 | `GET /reports/spending` (S2a) | **server-first route (M137 P0-2)** | No decryption dependency: the route reads the mandatory plaintext columns, so lock state does not change a single number. The M155-era browser mirror retired with M160 (P2-5); the capture it was parity-gated against is now a server-route regression pin | `backend/tests/test_m160_replay_s2a_matrix.py` → the 29 `s2a-vectors.json` cases replayed against the mounted route (seed → replay → byte-diff vs the frozen golden) + `backend/tests/test_reports_spending_cashflow.py` → “test_spending_monthly_series_exact”, “test_category_breakdown_rolls_up_to_roots_descending”, “test_cashflow_transactions_only” + `backend/tests/test_m160_reports_lock_invariance.py` → “test_spending_reports_are_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A5 | `GET /reports/spending/categories` (S2a) | **server-first route (M137 P0-2)** | same as A4 | `backend/tests/test_m160_replay_s2a_matrix.py` (the same s2a replay — the `breakdown.*` cases) + `backend/tests/test_reports_spending_cashflow.py` → “test_category_breakdown_rolls_up_to_roots_descending” + `backend/tests/test_m160_reports_lock_invariance.py` → “test_spending_reports_are_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A6 | `GET /reports/cashflow` (S2a) | **server-first route (M137 P0-2)** | same as A4 | `backend/tests/test_m160_replay_s2a_matrix.py` (the same s2a replay — the `cashflow.*` cases) + `backend/tests/test_reports_spending_cashflow.py` → “test_cashflow_trade_actions_and_principal_exclusion” + `backend/tests/test_m160_reports_lock_invariance.py` → “test_spending_reports_are_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A7 | `GET /reports/net-worth` (S2b) | **server-first route (M137 P0-2; the S2b browser port retired with M160)** | same as A4 — the route reads committed rows plus cache-only fx/prices, never a cipher column | `backend/tests/test_m160_replay_s2b.py` → “test_s2b_net_worth_golden_replays” (the 7 frozen S2b ranges replayed against the route) + `backend/tests/test_reports_networth.py` → “test_net_worth_monthly_buckets_exact”, “test_net_worth_assets_liabilities_split” + `backend/tests/test_m160_reports_lock_invariance.py` → “test_net_worth_is_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A8 | `GET /reports/investment-performance` (S2b) | **server-first route (M137 P0-2; the S2b browser port retired with M160)** | same as A4 (the ACB replay is pure math over fetched inputs) | `backend/tests/test_m160_replay_s2b.py` → “test_s2b_investment_performance_golden_replays” (the 4 frozen S2b ranges replayed against the route at the captured clock) + `backend/tests/test_reports_performance.py` → “test_performance_splits_realized_and_unrealized”, “test_realized_matches_capital_gains_endpoint” + `backend/tests/test_m160_reports_lock_invariance.py` → “test_investment_performance_is_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A9 | `GET /reports/dividends` (S2c) | **server-first route (M160-R2, t_85311c34 — the panel's client compute is deleted with the shim)** | same as A4 — income trades and trade-date rates are mandatory plaintext / cache-only, so lock state cannot move a number; the route is tenancy-scoped server-side, which is the only half that matters now | `backend/tests/test_dividends.py` → “test_tenancy_other_users_dividends_absent”, “test_conversion_uses_trade_date_rate”, “test_unresolved_fx_counted_and_excluded” + `backend/tests/test_m160_replay_s2c.py` → the four `s2c:dividends.*` cases (the frozen `s2c-vectors.json` golden replayed at the capture clock). The client-side A9 pin retired with the compute it pinned |
| A10 | `POST /rules/preview` (S3) | **server-first route (M137 P1-2)** | The verdict is the SERVER's answer from the same engine `apply` writes with, over the mandatory plaintext `normalized_payee` — lock state cannot change it, and now the preview and the apply it precedes read one predicate instead of two | `backend/tests/test_rules_preview_apply.py::test_preview_is_mounted_and_needs_auth` + `test_preview_dry_runs_server_side` + `test_preview_is_indifferent_to_encryption_required[False/True]` (P1-5 pattern) + `backend/tests/test_m160_replay_s3.py` (the 14 `rules.preview.*` cases replayed against the route) |
| A11 | `POST /rules/{id}/apply` (S3) | kept (server write) | Unchanged by lock state (apply writes server-side); the *preview* that gates it is A10 | `backend/tests/test_rules_preview_apply.py` apply suite (contract unchanged) |
| A12 | `GET /subscriptions` (S3) | **server-first route (M137 P1-2)** | Detection runs server-side over `normalized_payee` (mandatory plaintext) and the engine never reads the vault — lock state changes nothing, and the answer no longer depends on the browser having downloaded the ledger first | `backend/tests/test_subscriptions.py::test_get_subscriptions_answers_with_the_report_shape` + `test_get_subscriptions_requires_auth` + `test_subscriptions_is_indifferent_to_encryption_required[False/True]` (P1-5 pattern) + `backend/tests/test_m160_replay_s3.py` → the `subscriptions.detect` case (the frozen `s3-vectors.json` golden replayed at the capture clock) |
| A13 | `GET /budgets` (S3) | **server-first read (M137 P0-2)** | Budget *definitions* AND their progress are plaintext rows the API serves — the browser renders `spent`/`carried_in`/`remaining`/`pct_used` instead of recomputing them from a full-ledger download, so lock state changes neither, and the page stops downloading the ledger entirely | `backend/tests/test_budgets.py` (progress + the new `/budgets/recurring-summary` suite) + `test_budget_progress_is_indifferent_to_encryption_required[False/True]` (P1-5 pattern) + `lockedVaultDegradation.test.ts` → “A13: budget definitions/progress are lock-invariant and match the golden” (unchanged node; the compute now comes from `lib/budgets.ts` — the Row-8 client-only class) + `backend/tests/test_m160_replay_s3.py` → the `budgets.progress` case |
| A14 | `GET /reports/capital-gains` (S4) | **server-first route for every format (the S4 browser port retired with M160)** | same as A4 — gains math reads committed trades, prices and cache-only fx | `backend/tests/test_tax_reports.py` + `backend/tests/test_m160_replay_s4.py` (the 13 `capital-gains.<year>` cases replayed against the mounted route) + `backend/tests/test_m160_reports_lock_invariance.py` → “test_capital_gains_is_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A15 | `GET /acb` (S4) | **the route is the pool read; the S4 client pool math retired with M160** | same as A4 — `acb_pools` is written only through `rebuild_user_pools`, and the read is vault-independent | `backend/tests/test_m160_replay_s4.py` → the `acb.pools` case (pools rebuilt through the sanctioned write path, then byte-diffed against the frozen golden with the two DB-assigned columns dropped) + `backend/tests/test_tax_acb.py` + `backend/tests/test_m160_reports_lock_invariance.py` → “test_acb_pools_are_indifferent_to_encryption_required” — P1-5: the same JSON with the write guard on and off, the lock-invariance proof M160 moved here from the retired client engines |
| A16 | Transaction list + account names (Phase 2 surface) | n/a (display) | Cipher fields resolve to `🔒` while locked; legacy `v0` rows still show plaintext (mixed-mode by design) | `frontend/tests/e2ee.test.ts` → “shows the lock marker when ciphertext exists but vault is locked” + “falls back to plaintext legacy rows” |
| A17 | Client backfill (Phase 2 surface) | n/a (write) | Fail-closed: `getDEK()` throws `Vault is locked.` and no ciphertext is produced | `frontend/tests/backfill.test.ts` → “fails closed when the vault is locked” |
| A20 | `GET /reports/category-tag-matrix` (M124 R3, no S-slice) | **server-first route (the UI FETCHES it; the M124 client mirror retired with M160)** | Same class as A4: the cross-tab reads the mandatory plaintext columns, so lock state cannot move a cell, a row total or `total_cad`. | `backend/tests/test_m160_replay_s2a_matrix.py` → the 6 `matrix-vectors.json` cases replayed against the mounted route (demo + a 12-tag throwaway tenant exercising the top-8 cut, the `Other tags` spill and the raw-category rows) + `backend/tests/test_reports_tags.py` → “test_matrix_cells_rows_and_columns”, “test_matrix_columns_truncate_at_eight_with_an_other_bucket” + `frontend/tests/m124TagReports.test.tsx` (the live panel) |
| A21 | `POST /projections/run` (M15, no S-slice) | **server-first route (M157, M135 §2 K-12)** | The calculator the Reports tab renders is the SERVER's answer for every tenant. The client-only posture it replaced was never an E2EE one — the run reads no row and no cipher column, so lock state cannot move a number — it was the demo write guard rejecting a POST (M61 #2 moved the math into the browser to escape it). The route is now a documented demo-write exemption (docs/20 §6), and `lib/projectionEngine.ts` survives untouched as the offline fallback this rule requires: golden-parity tested, NOT the shipped path, retired only if M160 rules it | `test_projections.py` — the server-route pin `test_run_matches_the_frozen_m65_golden` (7 frozen goldens, byte-identical, 0 diffs) + `test_auth_security_hardening.py` — the demo exemption `test_projections_run_is_the_only_new_exemption` (the demo runs one, then `POST /accounts` still 403s), + `projectionEngine.test.ts` — the client mirror over the same frozen file, both directions of one golden, + `scripts/parity/parity.ts --slice projection` — the same requests against the deployed stack **as the demo tenant**, 7 of 7, 0 diffs |
| A22 | `GET /reports/spending?scope=household` + `GET /reports/spending/categories?scope=household` (M159, docs/12 §Owner dimension) | **server-first route (M159)** — the Owner lens renders it; the M159-era client engines retired with M160 (P2-5) | Same class as A4: the widened population reads the mandatory plaintext money columns (docs/33 envelope table), so lock state cannot move a household number. `encryption_required` is a WRITE guard and is parametrized both ways over the answer. The one lock-adjacent seam is the owner's `privacy_mode` (docs/43 §9 D4), which is a POPULATION mask here: a privacy-mode owner projects nothing, so the partner's household is their own rows | `test_reports_household.py` → “test_household_scope_unions_own_and_projection_rows”, “test_household_answers_are_indifferent_to_encryption_required”, “test_a_privacy_mode_owner_projects_nothing_into_the_household”, “test_a_non_joined_caller_gets_zero_partner_rows”, “test_household_csv_carries_the_same_rows_as_the_json” + `backend/tests/test_m160_replay_household.py` (the 12 `household-vectors.json` cases replayed against the mounted route — both seats, the narrow-scope contrasts and the privacy mask — carrying M159's hand-computed anchors forward from the householdParity client test this wave deleted) + `m108Reports.test.tsx` → “renders the SERVER-computed household population and labels it honestly” |
| A23 | `GET /reports/category-tag-matrix?scope=household` (M179, docs/12 §Owner dimension / docs/43 §6) | **server-first route (M124 R3, widened M179)** — the Categories tab's matrix panel and the category drill both render the scope the tab's donut renders; there has never been a client mirror for the household reading | Same class as A4/A22: the widened grid reads the mandatory plaintext money columns, so lock state cannot move a cell or `total_cad`; the top-8 tag cut and the row ranking are computed over the household's own spend on the server. The lock-adjacent seam is again the owner's `privacy_mode` (docs/43 §9 D4), a POPULATION mask: a privacy-mode owner projects nothing, so the partner's household matrix is their own rows while the owner's own matrix is untouched. Both axes' NAMES resolve from both tenants' trees, which is metadata, never ciphertext | `test_reports_household.py` → “test_matrix_household_unions_own_and_projection_rows”, “test_matrix_household_names_resolve_from_both_trees”, “test_matrix_household_tag_columns_resolve_from_both_tenants”, “test_matrix_household_csv_carries_the_same_rows_as_the_json”, “test_matrix_privacy_mode_owner_projects_nothing”, “test_matrix_non_joined_caller_gets_zero_partner_rows”, “test_matrix_owner_seat_household_is_the_owner_own_rows” + `m108Reports.test.tsx` → “the Categories tab carries the same dimension — donut AND its matrix” and “the Owner lens DOES drill — into the household population, not the caller's own ledger” |
| A24 | `GET /reports/insights?scope=household` + `GET /reports/spending-comparison?scope=household` (Owner-lens expansion, docs/12 §Owner dimension / docs/43 §6) | **server-first route, widened by the Owner-lens expansion — no UI lens control reads either, so no browser request carries `scope` today** | Same class as A4/A22: both routes read the mandatory plaintext money columns (`cad_amount` + the cached `fx_rates`), never a cipher column, so lock state cannot move a headline, a window total or a delta chip, and `encryption_required` stays a WRITE guard as A4 states. The scope widens only WHO is summed — one `household_ownership()` predicate, shared staging/LEDGER rules, the counterpart's tree naming foreign category ids — and the lock-adjacent seam is again the owner's `privacy_mode` (docs/43 §9 D4), a POPULATION mask: a privacy-mode owner projects nothing, so the partner's household is their own rows | `backend/tests/test_reports_household.py` → “test_insights_household_unions_own_and_projection_rows”, “test_spending_comparison_household_unions_own_and_projection_rows”, “test_the_expanded_routes_obey_the_privacy_mask_and_the_deny_default”, “test_the_expanded_routes_keep_bare_and_own_byte_identical_for_every_seat”, “test_unknown_and_empty_scope_still_refuse_on_the_expanded_routes” |

**Honest gaps this audit found (not fixed silently — each is a row in the
inventory below):**

- **A18 — the S5 search view has no unlock hint.** `docs/33` requires
  “surface *unlock to search encrypted X* instead of silently showing fewer
  hits”, and the global search does (`AppShell.tsx` `lockedEncrypted`), but
  `/transactions?q=` renders a bare empty list while locked. Finding **F4**.
  **FIXED (M136)**: the shared `VaultLocked` `partial` variant replaces the
  empty state with a MEASURED withheld count (“Showing X of Y rows — unlock to
  search the other Z”) and an inline unlock that genuinely changes the outcome
  (the old “Clear all” preserved `q`, a dead end by construction). Pinned by
  `lockedVaultKnownGaps.test.tsx` → “A18/F4 — FIXED” (both cases: the notice
  when cipher rows are withheld, the ordinary empty state when nothing is).
- **A19 — report engines read plaintext, not resolved rows.** `docs/08` used
  to say “over resolved rows”; the loaders fed `GET /transactions` rows
  straight into the engines, which read the mandatory plaintext columns. Lock
  state is therefore *vacuous* for reports (rows A4–A9, A14, A15) — the
  claim in those rows is “no decryption dependency”, not “decryption
  degrades”. Finding **F6** — **doc wording FIXED in M156**: docs/08
  §Reports' five stale rows now state the M137 P0-2 server-first truth (the
  UI reads the route) and name this row as why the mirror read plaintext
  rather than “resolved rows”.
  **[M160 update]** the engines those loaders fed are deleted, so A19 is now
  a statement about the SERVER routes: they read the same mandatory plaintext
  columns, which is why lock state still cannot move a report number and why
  every row above keeps its “no decryption dependency” claim without a client
  half to stand on. **[M160-R2 update]** `lib/reportClient.ts` is deleted too
  (its three panels read their routes), so the remaining client computes are
  `lib/budgets.ts` and `lib/sankeyEngine.ts` — both read the same plaintext
  columns, and the classification is unchanged for them. The other half of
  F6 — a future per-user strict mode (stop SERVING plaintext) would break
  every report until the server resolves cipher columns — is untouched and
  stays open; it is now the only remaining shape of that finding, since there
  is no browser loader left to break.

## Hardening audit — key-handling risk inventory

| # | Risk | Current state | Disposition |
| --- | --- | --- | --- |
| F1 | **Unlock produced a non-extractable index key.** `unwrap()` imported both session keys with `extractable: false`, so after any lock→unlock cycle `indexToken()` threw `Index key is not exportable.` — silently killing blind-index exact search (swallowed by AppShell's `try/catch`) and making `encryptTransaction()` (and therefore encrypted row saves) throw. Only a *freshly created* vault worked. | **FIXED in this change**: `unwrap()` imports with `extractable: true`, documented in the key-holding model. | Fixed + pinned by `frontend/tests/vaultSync.test.ts` → “unlock-from-bundle keeps the index key usable” |
| F2 | **Logout does not lock the vault.** `lockVault()` is only called from the Settings Lock button (`frontend/src/pages/Settings.tsx`); `AuthContext.logout()` clears tokens but leaves `sessionDEK`/`sessionIndexKey` and the decrypted-value cache (`useResolved.ts` `txCache`) alive in memory for the rest of the page's life. | Not touched (outside this card's file ownership: `auth` context). | **Tracked** — follow-up card |
| F3 | **Decrypted plaintext lives in a module-level cache.** `useResolved.ts` `txCache` holds decrypted payees/amounts for the session; it is keyed by row id + a 32-bit cipher digest (so a re-encrypted row cannot serve stale plaintext) and cleared on every `vaultchange` (including lock). Bounded by row count, never by TTL. | Accepted: the same bytes are in the React render tree anyway. | **Accepted**, documented here |
| F4 | **No unlock hint on the S5 search view** (see A18) — the locked state is silent instead of prompting. | **FIXED in M136**: `Transactions.tsx` renders the shared `VaultLocked` `partial` variant with a measured withheld count and an inline unlock (docs/33 §Server-first computing rule 7c/7f). | **Fixed** — pinned by `lockedVaultKnownGaps.test.tsx` → “A18/F4 — FIXED”; the `locked_vault_gate.py` audit row moved from KNOWN_RED to WORKS |
| F5 | **No DEK rotation.** Passphrase rotation re-wraps but the DEK never changes, so a blob copied before the change still decrypts everything under the old passphrase. Closing it needs a client-side re-encryption of every `*_cipher` column and every `payee_token` — a non-atomic data migration that can strand rows if a session dies mid-write. | Not implemented. | **Tracked** — follow-up card |
| F6 | **`docs/08` said reports compute “over resolved rows”; they compute over plaintext columns** (see A19). Also means the per-user strict mode (stop serving plaintext) would break every report until the loaders resolve instead. | **Doc half FIXED in M156**: docs/08 §Reports' five stale rows (`/reports/spending`, `/spending/categories`, `/cashflow`, `/net-worth`, `/investment-performance`) now say the UI reads the route since M137 P0-2 and that `lib/reportEngine` is the rule-5 E2EE/offline fallback over plaintext columns, citing A19 — “over resolved rows” is gone. | **Fixed (doc wording)** — pinned by docs/08 §Reports. The strict-mode half stays **Tracked**, and M160 narrowed it: the browser loaders that fed rows to the mirrors are deleted, so the only code that would have to resolve cipher columns is the server itself. |
| F7 | **`dedup_hash` is still unsalted SHA-256** over `account\|date\|amount\|raw_description` (`importers/base.py:52-60`), dictionary-reversible to descriptions and amounts. The PLANNED table above has promised a keyed HMAC “in Phase 3/4” since before Phase 3. | Not implemented (it changes dedup semantics server-side; needs its own spec). | **Tracked** — follow-up card; the Phase-4 ledger line no longer implies it ships with this card |
| F8 | **Blind index is not built for `rules.pattern` / `learned_payees.normalized_payee`** — still plaintext columns (PLANNED table, “blocked on Phase 3”). | Not implemented. | **Tracked** — follow-up card (same one as F7 is fine) |
| F9 | **Wrapped blobs never expire** (IndexedDB + server row) and there is no server-side TTL. Expiry would force a passphrase re-entry on a schedule without denying an attacker anything — the blob is worthless without the passphrase — so a TTL buys UX pain, not security. | Accepted after analysis. | **Accepted** |
| K1 | **Timing**: `indexToken()` is a client-side HMAC over data the client already holds; the server compares `payee_token` with an indexed equality predicate, which is exactly the comparison the index performs anyway. Neither is a new oracle. | Accepted. | **Accepted** |
| K2 | **Memory residency**: raw DEK/index bytes exist only inside `wrap`/`unwrap`/`indexToken` calls and as non-persisted `CryptoKey` handles; nothing is written to disk except the *wrapped* bundle. | Accepted. | **Accepted** |
| K3 | **Blob size/shape**: server caps at 8000 chars (`envelope_too_large`), validates `device_id` shape, and enforces ≤ 10 active envelopes — the endpoint cannot become unbounded storage. | Implemented here. | Covered by `backend/tests/test_vault_envelopes.py` |
| K4 | **Rollback of an envelope** (hostile server serves an old blob): no generation counter, so the client cannot *detect* it; it fails closed on the first decrypt instead of leaking. | Accepted; a `generation` column is the obvious future hardening. | **Accepted**, noted for F5's follow-up |
| K5 | **Revocation is bound to `localStorage`.** A revoked profile that clears site data becomes a new device. | By design — see the revoke runbook. | **Accepted** |
| K6 | **`label` is plaintext device metadata** on the server (browser + OS family). | By design; it is what makes “which device do I revoke?” answerable. | **Accepted** |

## Verification (Phase 4)

1. **Multi-device round trip** — `frontend/tests/vaultSync.test.ts`
   → “multi-device round trip (docs/33 Phase 4 → Verification #1)”: device A enrolls (wrap → `POST`), device B (fresh
   `device_id`, empty IndexedDB) unlocks with the same passphrase and its DEK
   decrypts a field A encrypted (`data resolves`); A revokes B / B revokes A →
   the revoked device's next unlock returns `device_revoked` and its local
   mirror is deleted, while the surviving device still unlocks.
2. **Revoke server contract** — `backend/tests/test_vault_envelopes.py`
   (create/list/replace/revoke, every error code above, tenancy, demo guard,
   partial-unique re-creation, blob dropped on revoke).
3. **Locked-vault degradation** — the A1–A17 table above; every row cites a
   test that runs in `NODE_ENV=test npx vitest run` or `uv run pytest -q`.
   The citations are **executable**: `uv run python scripts/locked_vault_gate.py`
   (needs the example.internal `DATABASE_URL` for the backend half; `--fe-only` runs
   without it) resolves A1–A19 plus the three global negatives N1–N3 against a
   fresh vitest + pytest run and prints one table. It reads verdicts from the
   reports, not from prose here, so a renamed test or a moved file shows up as
   `BROKEN` instead of rotting silently.

   Two rows are red BY DESIGN until their findings ship, and the runner keeps
   them red even on a green run:

   - **A18 (F4)** — the S5 search view has no unlock hint. Pinned by a
     characterization test in `frontend/tests/lockedVaultKnownGaps.test.tsx`
     ("the S5 search view stays SILENT while locked") which asserts today's
     silent behaviour; when F4 ships that assertion fails and the line flips.
   - **F2** — logout does not lock the vault. Same file, same mechanism
     ("logout clears the session but keeps the DEK").

   The characterization tests PASS while the defects are present — their
   passing is what reports the row as red. A silently-failing `it.fails` would
   also swallow a broken harness, so these files render real pages and the
   failure must be the assertion.

   **Global negatives** (`backend/tests/test_locked_vault_exposures.py`) pin
   the plaintext exposures a ciphertext-leak scan cannot see because they are
   by design: `transaction_events.payload` (N1, verbatim payee/amount in
   `/export/full`), `dedup_hash` (N2, unsalted SHA-256), and
   `rules.pattern` / `learned_payees.normalized_payee` (N3, plaintext columns).
   Each is a tripwire — it fails the day the column is encrypted or the hash
   is keyed, which is the signal to move this inventory and the gate fixture
   in the same change.
4. **Gates** — AGENTS.md §4 in order: doc reconciliation (this section +
   `docs/08`) → ruff → `tsc`/`eslint`/`vitest` → `pytest` on an isolated
   `<db>_test_<session-id>` → `npm run build` → docker rebuild → smoke → single commit.



## Open tradeoffs (accepted, not hidden)

- **Bank sync sits outside the envelope** (M79): consent metadata lives
  server-side and future provider tokens would be **server-side state the
  passphrase never protects** (`docs/39 § 9`) — the opposite of the
  passphrase-locked `vault_envelopes` blobs above. Since M181 the *write* half
  of that exposure is closed for opted-in tenants (every pull is refused 422
  `encryption_required`; a pull for anyone else still lands in the plaintext
  columns). See "OUTSIDE THE ENVELOPE (M79)" above — the disclosure is part of
  the feature, not an afterthought.

- Dates/IDs stay plaintext by design: once Phase 4 lands the server sees
  *when* you transact and *how many* rows, never *who* or *how much*. **Today
  it sees all three** (see Goal) — payee, description and amount are
  plaintext columns and are served by the API.
- No passphrase recovery, by construction. Lost passphrase = restore from
  export/backup taken before the loss.
- Demo account stays plaintext (it's shared sample data, not PII).
