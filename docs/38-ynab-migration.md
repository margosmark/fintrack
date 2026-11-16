# 38 — YNAB budget migration: preview-first import (M76)

Move a household off YNAB in one upload: the whole budget (accounts,
categories, group tags, targets) plus every transaction, **mapped in full
before a single row is written**. Built for the `t_063fa501` card ("M76 —
YNAB import, next after M75").

Two routes, one plan builder — `app/services/ynab_migration.py`:

- `POST /api/v1/imports/ynab/preview` — parse + map, return the complete
  plan. **Read-only**: the test asserts every table's row count is unchanged
  after a preview.
- `POST /api/v1/imports/ynab/commit` — parse + map + apply in ONE transaction
  (all-or-nothing), returning the plan it applied plus per-account batch
  results. The transaction staging itself is **not** re-implemented here: it
  goes through the shared `app/services/import_staging.py::stage_rows`
  pipeline (docs/03 § One staging function), so batch creation, the dedup
  bridge, the counters and the 2000-row cap are identical to a file upload or
  a bank pull. YNAB only decorates each row with its `category_id`/`cleared`
  and links tags and transfer pairs after the flush.

Both routes are `@human_paced_limit`-wrapped (6/min upload limit, 10/min
mutate limit) and accept the same multipart form.

## 1. Formats accepted

### JSON — YNAB budget/plan export

Verified against the YNAB OpenAPI spec (`api.ynab.com/v1`, which renamed
`budget` → `plan` in v1.78) and Actual Budget's `ynab5.ts` importer.

Accepted shapes, in order:

1. `{"data": {"plan": {...}}}` — current API wrapper
2. `{"data": {"budget": {...}}}` — older exports / third-party tools
3. `{"plan": {...}}` / `{"budget": {...}}` — unwrapped
4. the bare budget object

Arrays consumed: `accounts`, `category_groups`, `categories`, `payees`,
`transactions`, `subtransactions`, `scheduled_transactions`.

The canonical YNAB-side export is `GET https://api.ynab.com/v1/plans/{plan_id}`
(`plan_id` accepts `last-used`) — the spec calls it "effectively a full plan
export" and it answers in shape 1. Per-account register CSVs cover users who
would rather not mint an API token.

Anything else → `422 {detail, code: "ynab_parse_error"}`.

### CSV — per-account register export

Header aliases are matched case-insensitively after stripping a trailing
`(…)` suffix, so `Outflow (CAD)` matches `outflow`.

| Field | Aliases |
|---|---|
| `date` (required) | `date`, `transaction date`, `trans. date` |
| `outflow` / `inflow` (one required) | `outflow`/`debit`/`withdrawal`, `inflow`/`credit`/`deposit` |
| `account` | `account`, `account name` |
| `subcategory` | `sub category`, `subcategory`, `sub-category` |
| `category` | `category` |
| `group` | `master category`, `category group`, `group` |
| `payee` | `payee`, `payee name`, `description` |
| `memo` | `memo`, `notes`, `note` |
| `cleared` | `cleared`, `cleared status`, `status` |

`subcategory` wins over `category` (YNAB 4 ships both columns). A combined
`"Master: Sub"` cell splits into group + category when no Master column is
present.

CSV has no account type, no currency and no targets, so the form carries
optional overrides — each validated up front with its own error code:
`account_type` (`invalid_account_type`), `currency` (`invalid_currency`),
`date_format` (`invalid_date_format`), `account_id` (target account for rows
with no Account column).

## 2. Normalization rules

- **Amounts** are YNAB *milliunits* (1/1000 major unit), signed the same way
  fin_tracker signs (`inflow` > 0, `outflow` < 0) → `Decimal(milli) / 1000`,
  then quantized to cents (`ROUND_HALF_UP`) because
  `transactions.amount_native` is `Numeric(18,2)`. A quantizing run is
  reported as a `amounts_quantized` note. CSV money cells accept `1,234.56`,
  `1.234,56`, `12,34`, `(12.34)`; more than 3 decimals → `invalid_amount`.
- **Dates**: JSON must be `YYYY-MM-DD`. CSV picks its format from the file's
  own evidence (ISO first; then whether any row forces a day-first vs
  month-first order). When every date is ambiguous (both parts ≤ 12) the
  month-first YNAB default is assumed **and a `date_format_assumed` note is
  returned** — never applied silently. `date_format=%d/%m/%Y` pins it, and a
  pattern that parses no sample is rejected rather than mis-dating a row.
- **Memo** rides into the description (`Payee — memo`, memo alone, payee
  alone) so the column stays within `raw_description`'s length limit and the
  dedup hash sees one stable string. A row with neither → `(YNAB import)` plus
  a `missing_payee` warning.

## 3. Entity mapping

### Accounts (`accounts` table)

| YNAB `type` | `AccountType` |
|---|---|
| `checking`, `cash` | `chequing` |
| `savings`, `otherAsset` | `savings` |
| `creditCard` | `credit_card` |
| `lineOfCredit`, `mortgage`, `autoLoan`, `studentLoan`, `personalLoan`, `medicalDebt`, `otherDebt`, `otherLiability` | `line_of_credit` |

Keys are normalized (lowercase, `_`/`-`/spaces stripped), so `creditCard`,
`credit_card` and `CREDIT CARD` agree. A type outside this table →
`action: "unmapped"` with a reason, and **every row referencing that account
is flagged `account_unmappable` instead of being dropped**. Taxability is
server-derived exactly as `POST /accounts` derives it (docs/14 C1).

Matching is by name (case-insensitive) → `reuse` (the existing account keeps
its own currency — reported as an `account_currency_kept` note), otherwise
`create`. `closed` accounts import as open accounts (note
`account_closed_in_ynab`); `deleted` accounts are `unmapped`.

**No `starting_balance` is set**: balances rebuild from the imported history,
which is correct exactly when the export is complete. A trimmed export
therefore shows a different balance than YNAB (§8 gaps).

### Categories → a two-level tree

fin_tracker categories have `parent_id`, so the mapping is:

- YNAB **category group** → a top-level `Category` (its name),
- YNAB **category** → a child `Category` under that group's category,
- YNAB **group name** → a `Tag` attached to that group's transaction rows
  (UNIQUE(user_id, name) makes the re-import a no-op).

Both levels match existing rows by name first (`reuse`), so a second run
creates nothing. A reused category keeps its current parent — manual data
wins over the import.

### Targets (`budgets` / `goals` tables)

| YNAB `goal_type` | Mapping |
|---|---|
| `MF` (monthly funding) | `Budget(user, category, amount, period="monthly")` |
| `TB` / `TBD` (target balance, by date) | `Goal(name = "<category> target", target_amount, target_date)` |
| `NEED`, `DEBT`, anything else | `kind: "unsupported"`, `action: "skipped"` **with a reason** |

Existing budgets/goals are kept, never overwritten (manual amounts win), and
counts show create vs reuse so the preview is honest about what changes.
Targets are never auto-spent: importing a YNAB budget does not move money out
of your chequing account.

### Transfers

`transfer_account_id` + `transfer_transaction_id` are honored: both sides
present **and importable** → one pair, linked after both rows are flushed
(`transactions.transfer_pair_id`, docs/02). A missing/uncountable counterpart
is a `transfer_counterpart_missing` warning. CSV has no transfer markers, so
a payee starting with `transfer:` is display-flagged only.

### `transactions.cleared` (model change)

YNAB's `uncleared` / `cleared` / `reconciled` is preserved in the new
nullable `transactions.cleared` column (migration `0033_tx_cleared`,
`String(16)`). **NULL means "the source carried no clearing state"** — OFX/QFX
and generic CSVs leave it NULL, and it is never read as "uncleared". An
unrecognized value → NULL + a `cleared_not_recognized` warning.

### Splits and schedules

`subtransactions` are counted, not imported: a split row imports as its
parent total with a `split_detail_dropped` warning and the per-part categories
never arrive. There is **no auto-split anywhere** — M107 (docs/44) added
hand-splitting of an already-imported row, never import-time splitting (D16),
so a YNAB split still lands as one row the user can re-split by hand
afterwards.

**Wording (docs/44 §7, pinned for M107).** The warning must read:

> fin_tracker does not import YNAB subtransactions (split rows import as the
> parent total; you can split them by hand afterwards)

replacing the pre-M107 parenthetical *"(fin_tracker has no split rows)"*,
which M107 made false — split rows DO exist in the ledger now (docs/44); they
are simply never *imported*. The `code` (`split_detail_dropped`), the row
index and the behaviour are unchanged and correct either way.

**Known code/doc gap (reported, not hidden — AGENTS §9):**
`backend/app/services/ynab_migration.py` still emits the OLD parenthetical
inside its `reason` string (`"…the total imports but the per-part categories
do not (fin_tracker has no split rows)"`). Only that sentence is stale;
updating it is a code change, deliberately not made in this docs pass.

`scheduled_transactions` are skipped with a `scheduled_skipped` note —
fin_tracker has no scheduled-transaction concept (unchanged by M107).

## 4. Dedup and idempotency

Same contract as `POST /imports/upload` (docs/03): the canonical hash spans
pending **and** committed rows, so re-running the migration on the same
budget stages duplicates rather than net-new rows. Concretely, a second commit
of the fixture returns `net_new: 0`, `duplicates_skipped: 10`,
`counts.accounts_create: 0`, `counts.categories_create: 0`, and the live
transaction count is unchanged after its batches are committed. The commit
side of that contract is not implemented twice: `apply_plan` calls the shared
`stage_rows`, which owns the canonical hash, the committed/pending existence
probe, the salt and the `imported` / `duplicates_skipped` / `unresolved`
counters — one implementation for a file, a bank pull and a budget migration,
so the three cannot drift.

Because the hash includes the target account's id, a row whose account is
being created this run cannot be checked yet: the preview reports those as
`duplicates_pending` and the commit resolves them. New duplicates get a salted
hash so `UNIQUE(dedup_hash)` holds while `is_duplicate = true` keeps them out
of live reads.

## 5. Never silently dropped

Every row that cannot become a transaction comes back in
`transactions.unmappable` with `{code, reason, index, detail}` — **that list
is never truncated** (row previews cap at 500, warnings at 500, both flagged).

| Code | Meaning |
|---|---|
| `invalid_date` | unparseable/missing date, or it disagrees with the file's format |
| `invalid_amount` | missing, non-numeric, both in+outflow, or > 3 decimals |
| `unknown_account` | row has no account and no target account was supplied, or references an account absent from the export |
| `account_unmappable` | the row's account is deleted in YNAB or has no `AccountType` equivalent (reason carries which) |

Warnings (`transactions.warnings`, rows still import): `missing_payee`,
`unknown_payee`, `unknown_category`, `split_detail_dropped`,
`transfer_counterpart_missing`, `cleared_not_recognized`. File-level
`notes`: `deleted_rows_skipped`, `scheduled_skipped`,
`account_closed_in_ynab`, `account_currency_kept`, `date_format_assumed`,
`tag_name_too_long`, `amounts_quantized`, `targets_rounded`.

Staged rows keep the docs/03 review loop: one `import_batches` row per target
account with `status = pending_review`, committed/discarded/reverted through
the existing routes. Staging runs through the shared
`services/import_staging.py::stage_rows` (docs/03), which means the **2000-row
per-batch cap applies here too**: a single target account carrying more than
`MAX_STAGED_ROWS` importable rows returns `413 too_many_rows` before that
batch exists — and because the migration is ONE transaction, every account,
category, tag, target and row it had already written is rolled back. The
file-level guard (50 000 rows) still runs first, at parse time.

## 6. Routes (docs/08)

```
POST /api/v1/imports/ynab/preview   multipart: file, account_id?, account_type?,
                                               currency?, date_format?
  200 YnabMigrationPlan   (accounts, category_groups, categories, targets,
                           transactions{rows, unmappable, warnings}, notes, counts)
  413 file_too_large | too_many_rows (5 MB / 50 000 rows)
  422 ynab_parse_error | invalid_account_type | invalid_currency | invalid_date_format

POST /api/v1/imports/ynab/commit    same form
  201 YnabCommitResult   {plan, batches[], net_new, duplicates_skipped}
  413 too_many_rows      (one target account over 2000 importable rows —
                          MAX_STAGED_ROWS, raised by stage_rows before that
                          batch exists; the whole migration rolls back, docs/03)
  422 encryption_required   (E2EE tenants — see §8)
```

The router is registered **before** `imports.router` in `app/main.py`:
`POST /imports/{batch_id}/commit` would otherwise swallow
`/imports/ynab/commit` as `batch_id = "ynab"`.

## 7. Frontend

`src/components/imports/YnabMigrationCard.tsx` on the Imports page:
file picker → **Preview** (read-only) → the plan (counts, unmapped accounts
with reasons, refused rows with row numbers, caveats on demand, notes) →
**Import N transactions** → toast + batch history refresh. A successful import
keeps the plan on screen with "N new transactions staged for review below" and
disables the Import button, so the action cannot double-fire.

Help: a dedicated `/help/migrate-from-ynab` guide (`src/pages/helpGuides.ts`,
mirror `site/guides/migrate-from-ynab.html`) carries both export recipes, the
preview-first flow, and two comparison tables — "What maps cleanly" (8 rows)
and the honest gaps table "What does not come across" (10 rows: splits,
schedules, unsupported targets, reconciliation progress, underfunded amounts,
payee memories, closed/deleted rows, partial history, E2EE refusal). The
"Imports" guide's "Move over a YNAB budget" step points at it; every string
is duplicated in the static mirror per docs/32's copy-drift rule.
`Guide` grew an optional `tables` field (`caption`, `columns`, `rows`),
rendered as semantic `<table>`s by `HelpGuide.tsx`.

## 8. Known gaps and deviations

- **M71 deviation**: M71's shared dry-run contract had not landed when this
  card shipped, so the preview here is this plan builder rather than a reused
  M71 helper. If M71 later ships a shared dry-run primitive, the preview
  endpoint should adopt it without changing the response shape above.
- **E2EE tenants are refused on commit** (`422 encryption_required`): a
  server-side upload cannot produce `payee_cipher`/`amount_cipher`, so writing
  plaintext into an encryption-required tenant is blocked rather than
  silently degrading it. Manual entry with client-side encryption remains the
  path for those tenants (the transactions API demands both cipher fields).
  `POST /imports/upload` predates this card and carries no such check — the
  statement path still stages plaintext for those tenants; recorded here as a
  pre-existing gap, not a workaround.
- Account balances are rebuilt from imported history (no `starting_balance`
  seed) — a trimmed export shows a different balance than YNAB.
- Split detail, scheduled transactions, reconciliation progress, category
  funding ("underfunded"), `approved` flags, account order, and payee rules
  (YNAB payee→category memories) are not imported; payee→category learning is
  fin_tracker's own (docs/04).
- YNAB supports only one category per transaction, exactly like fin_tracker —
  group tags are the multi-label story (docs/02).
- Goals are imported as metadata; nothing is auto-spent (docs/24's bill
  flow is separate).
- OFX/QFX and the generic CSV importer do not populate `cleared` yet — only
  the YNAB path writes it today.

## 9. Verification

- `backend/tests/test_ynab_migration.py` — 11 tests against fixtures
  `tests/fixtures/ynab_budget.json` (16 source rows: 10 importable, 5
  refused with distinct codes, 1 deleted, 1 scheduled), `ynab_register.csv`
  and `ynab_register_ambiguous.csv`: preview writes nothing; commit creates
  3 accounts / 6 categories / 3 tags / 1 budget / 1 goal / 3 batches / 10 rows
  with `cleared` preserved and the transfer pair linked; re-import is
  idempotent (`net_new 0`, live count unchanged); every refused row carries a
  reason; CSV, fallback account, ambiguous-date note, `date_format` pinning,
  the error envelope (including a 5 MB rejection) and the E2EE refusal.
- Shared-pipeline pin: `test_commit_enforces_the_shared_row_cap` posts a
  2001-row register to one target account and asserts `413 too_many_rows`
  (`MAX_STAGED_ROWS`) with **every table's row count unchanged** — the cap
  comes from `stage_rows`, and the ONE-transaction migration rolls back.
- Shared fix found on the way: `POST /imports/{id}/commit|discard|revert`
  bulk-deleted staged transactions without clearing `transaction_tags`, which
  has no `ON DELETE CASCADE` — any importer that tags rows at stage time hit a
  `ForeignKeyViolation`. `_unlink_tags()` now runs before all three deletes
  (`app/api/imports.py`).
- Gates: `ruff format --check` + `ruff check` clean; pytest on Postgres;
  `tsc --noEmit`, `eslint` on touched files, `vitest`
  (`ynabMigration`, `help`, `jargonLeaks`).
