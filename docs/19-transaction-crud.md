# 19 — Manual Transaction CRUD (M22)

Manual create / edit / delete of ledger transactions, plus the read scope that
hides staged/pending import rows. This is the contract a fresh agent could
implement from alone (paired with the canonical endpoint index in docs/08).

## Context

Before M22, transactions only entered the ledger through file imports
(docs/03). Users need to correct mistakes and add cash entries directly: a
missed coffee, a manual transfer, a typo in an imported row. The backend exposes
restful CRUD on `/api/v1/transactions`, the dedup/cad machinery already powering
imports is reused unchanged, and the frontend presents one shared modal for
add + edit (with a `window.confirm` delete gate in the two host pages).

Decisions (locked with user):
- **Manual rows share the `transactions` table.** `import_batch_id` is `NULL`
  for any row created via CRUD; imports set it. The same `dedup_hash` /
  `cad_amount` computation runs for both paths (docs/03, docs/02).
- **CAD is derived, never stored as input.** `cad_amount` is `cached_cad_amount`
  (docs/07): exact-date FX cache row only, or `NULL` when no CAD rate exists for
  that date. The write path never triggers an FX fetch.
- **Pending-review import rows are immutable from CRUD.** Editing or deleting a
  row that still belongs to a `pending_review` batch is rejected with
  `transaction_in_pending_batch` (409). Only committed/manual rows are editable.
- **List scope = manual or committed.** `GET /transactions` returns rows whose
  `import_batch_id` is `NULL` or belongs to a `committed` batch; staged/pending
  rows are invisible to ledger consumers (`_live_scope`).

## Approach

### Backend

Router: `app/api/transactions.py` — `APIRouter(prefix="/transactions")`, mounted
under `/api/v1` (docs/08). All routes require `get_current_user` (tenant-scoped)
and `get_db`.

#### `POST /api/v1/transactions` — create

Request body: `TransactionCreate` (docs/02 fields; `extra="forbid"`).

| field | type | constraint | notes |
|---|---|---|---|
| `account_id` | int | required | must belong to user (404 `not_found` otherwise) |
| `date` | date | required, ISO `YYYY-MM-DD` | used in dedup hash + CAD lookup |
| `posting_date` | date \| null | optional | |
| `raw_description` | str | required, `min_length=1`, non-blank | |
| `normalized_payee` | str \| null | optional, `min_length=1` | if null/omitted → auto `normalized_payee(raw_description)` (collapsed whitespace) |
| `amount_native` | Decimal | required, signed | account currency; convention mirrors docs/03 (debits negative). Persisted verbatim. |
| `currency` | str \| null | optional, `^[A-Z]{3}$` | null → `account.currency` |
| `category_id` | int \| null | optional | if non-null, must belong to user (404 `not_found` otherwise) |

Behavior:
1. Resolve `currency = payload.currency or account.currency`.
2. Build `RawTransaction` and `dedup_hash = build_dedup_hash(account.id, raw)`
   (canonical string below). If a row with the same `account_id` + `dedup_hash`
   exists → **409 `duplicate_transaction`**.
3. `cad_amount = cached_cad_amount(db, currency, date, amount_native)` → exact-date
   CAD or `NULL`.
4. Insert; `IntegrityError` (unique race) → rollback + **409 `duplicate_transaction`**.
5. Return **201 `TransactionPublic`**.

`build_dedup_hash` canonical string (`app/importers/base.py`):
```
f"{account_id}|{date.isoformat()}|{amount_native.quantize('0.01')}|{' '.join(raw_description.split())}"
```
SHA256 hex digest.

#### `PATCH /api/v1/transactions/{transaction_id}` — update

Request body: `TransactionUpdate` (all fields optional, `extra="forbid"`;
`model_dump(exclude_unset=True)` is applied).

| field | type | constraint |
|---|---|---|
| `account_id` | int \| null | null → **422 `validation_error`** |
| `date` | date \| null | |
| `posting_date` | date \| null | |
| `raw_description` | str \| null | `min_length=1`, non-blank; null → **422 `validation_error`** |
| `normalized_payee` | str \| null | `min_length=1`; null → recomputed from raw_description |
| `amount_native` | Decimal \| null | |
| `currency` | str \| null | `^[A-Z]{3}$`; null/omitted → `account.currency`; empty string → **422 `validation_error`** |
| `category_id` | int \| null | non-null must belong to user (404) |

Behavior:
1. `owned_or_404(Transaction, id, user, db)`; then `_ensure_live` (pending batch ->
   **409 `transaction_in_pending_batch`**).
2. **M107 split guard, before any other validation** (docs/44 §5):
   `_refuse_split_row_write(transaction, "patch", list(data))` — the payload's own
   field names decide. Any LEDGER field (`account_id`, `date`, `posting_date`,
   `raw_description`, `normalized_payee`, `amount_native`, `currency`,
   `category_id`, `note`) on a split **parent** -> **409 `transaction_is_split`**;
   on a **child**, the frozen identity/money fields -> **409 `split_leg_immutable`**,
   while `category_id` / `note` / ciphers / review state stay ordinary 200 edits.
   A cipher-ONLY patch on a split parent (`payee_cipher`, `raw_desc_cipher`,
   `amount_cipher`, `cad_amount_cipher`, `payee_token`) is allowed — the docs/33 A17
   E2EE backfill must keep working.
3. Reject explicit null `account_id` / `raw_description` → **422 `validation_error`**.
4. If `account_id`/`category_id` present and non-null, ownership-checked.
5. If any identity field (`account_id`, `date`, `amount_native`, `raw_description`, `currency`) changed: rebuild `RawTransaction`, recompute `dedup_hash` + `cad_amount`;
   collision with another row → **409 `duplicate_transaction`**.
6. Apply remaining fields; commit (race → **409 `duplicate_transaction`**).
7. Return **200 `TransactionPublic`**.

#### `DELETE /api/v1/transactions/{transaction_id}` — delete

- `owned_or_404` + `_ensure_live` (→ **409 `transaction_in_pending_batch`** for
  pending-batch rows), then the M107 guard (docs/44 §5): a split **parent** →
  **409 `transaction_is_split`** ("This transaction is split. Merge its parts
  first.") and a **child** → **409 `split_leg_immutable`** — a child is never
  deleted on its own; `DELETE …/split` (merge) or `PUT …/split` (re-split) is the
  way out. Merge is the ONLY edit a split parent admits.
- `db.delete(transaction)`; commit → **204**.
- Counterpart `transfer_pair_id` is set to `NULL` by the FK `ON DELETE SET NULL`
  (migration `0010_transfer_pair_on_delete_set_null`). Manual rows have
  `import_batch_id = NULL`, so no import batch history is touched.

#### `GET /api/v1/transactions` — list (scope only)

`_live_scope(user_id)` = `import_batch_id IS NULL OR import_batch_id IN (committed
batches)`. Returns manual + committed rows only; staged/pending rows omitted.

**M107 population (docs/44 §2, §6):** on top of `_live_scope` the scope applies
the LEDGER rule (`LEDGER_EXCLUDE_SPLIT` = `is_split = false`), so `items` returns
manual + committed rows **and split children, never a split parent**. The page
carries `split_parents: list[SplitParentPublic]` — the parents of the children
IN the page, ordered `date desc, id desc`, `[]` when the page has none — and
`total` counts matching **LEDGER** rows (a three-child split contributes 3).

**Amount filters read the STATEMENT.** `amount_min`, `amount_max` and
`direction` compare `COALESCE(parent.amount_native, own.amount_native)` over a
correlated self-subquery (an ordinary row gets its own amount, a child gets its
parent's), so an amount filter finds the statement, not the part: a **`+187.42`**
statement split into `+120.00 / +67.42` returns **both** children for
`?amount_min=100`, and a **`-187.42`** statement split into
`-120.00 / -40.00 / -27.42` returns **all three** for `?amount_max=-100`. The
bound is the parent's own SIGNED amount, so the sign decides: `amount_min=100`
against `-187.42` matches nothing (`-187.42 >= 100` is false) — the correct
answer, not a bug. Content filters (`from`, `to`, `payee`, `payee_token`,
`account_id`, `category_id`, `tag_id`, `tag_ids`, `reviewed`) still match each
child on its own copied identity and its own category/tags.
Filter params are documented in docs/08 (`account_id`, `category_id`, `from`, `to`,
`payee`, `amount_min`, `amount_max`, `direction`, `payee_token`, `tag_id`, and
the M104 multi-tag `tag_ids` — repeated, AND-composed: a row must carry every
id).
The fuzzy `q` param is **removed** (docs/33 S5): the search view scores in the
browser, and a request carrying `q` is rejected **422 `q_removed`** before any
scope is built (no relevance path remains — the list is always date-ordered).

#### Mutation matrix (M107, docs/44 §5)

One function — `_refuse_split_row_write(transaction, operation, fields)` —
guards every route that writes a ledger row. `operation` is one of `patch` /
`delete` / `tag` / `transfer` / `reconcile`; only `patch` consults `fields`,
because its answer depends on WHICH columns moved.

| Target / operation | Allowed? | Answer |
|---|---|---|
| parent: `DELETE /transactions/{id}` | **no** | **409 `transaction_is_split`** — "This transaction is split. Merge its parts first." (never a cascade surprise) |
| parent: `PATCH` with any LEDGER field (`account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency`, `category_id`, `note`) | **no** | **409 `transaction_is_split`** |
| parent: `PATCH` **only** cipher/index fields (`payee_cipher`, `raw_desc_cipher`, `amount_cipher`, `cad_amount_cipher`, `payee_token`) | **yes** | **200** — the E2EE backfill (docs/33 A17) keeps working |
| parent: `POST /transactions/{id}/split` | no | **409 `already_split`** (replace is `PUT …/split`) |
| parent: `DELETE /transactions/{id}/split` (merge) | **yes** | **200 `TransactionPublic`** — children deleted, `is_split=false`, `split_checksum=NULL`, every other field byte-identical to pre-split |
| parent: tag attach / replace / detach (`POST`/`PUT`/`DELETE …/tags`) | no | **409 `transaction_is_split`** (tags live on the children while the split exists) |
| parent: transfer `detect-transfer` / `link-transfer` / `unlink-transfer` / `accept-transfer-suggestion` | no | **409 `transaction_is_split`** |
| parent: reconciliation `transaction_id` link target | **yes** | allowed — a statement line matches a statement row; the link writes `reconciliation_items`, never the transaction |
| parent: recurring accept / suppress | **yes** | unchanged — the badge is an identity-row property and stays on the parent |
| parent: `POST /transactions/{id}/review` | **yes** | unchanged — review state is an identity-row property, deliberately NOT guarded |
| parent: `POST /suggestions/{id}/accept` | no | **409 `transaction_is_split`** — accept writes `category_id`, a LEDGER field; the candidate scan is LEDGER so a fresh suggestion can never offer a parent, this closes a stale row |
| child: `PATCH` `category_id`, `note`, ciphers, review state | **yes** | **200 `TransactionPublic`** |
| child: `PATCH` `account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency` | no | **409 `split_leg_immutable`** — change amounts only by replacing the whole split (`PUT …/split`) |
| child: `DELETE /transactions/{id}` | no | **409 `split_leg_immutable`** — the detail points at *Re-split* / *Merge* |
| child: tag attach / replace / detach | **yes** | normal row behaviour (this is why children are rows) |
| child: transfer `detect` / `link` / `unlink` / accept-suggestion | no | **409 `split_leg_immutable`** |
| child: reconciliation `transaction_id` link target | no | **409 `split_leg_immutable`** — a statement line never matches part of one |
| child: `POST /transactions/{id}/review` | **yes** | normal row behaviour (children carry the parent's review state forward, docs/02) |

Transfer suggestion **lists** stay IDENTITY: a split parent may appear (it IS a
statement line) while a child never does — and accepting a listed parent is
refused by the row above, which is the intended outcome, not a broken link.


### Response shape — `TransactionPublic`

(`from_attributes=True`; Decimal fields serialize as JSON strings.)

| field | type |
|---|---|
| `id` | int |
| `account_id` | int |
| `date` | date |
| `posting_date` | date \| null |
| `raw_description` | str |
| `normalized_payee` | str |
| `amount_native` | Decimal (str) |
| `currency` | str |
| `cad_amount` | Decimal \| null (str) |
| `category_id` | int \| null |
| `is_recurring` | bool (default false) — M96, the badge the prompt card sets |
| `recurring_cadence` | `weekly \| biweekly \| four_weekly \| monthly` \| null (M96) |
| `recurring_next_date` | date \| null — the group's next expected occurrence (M96) |
| `transfer_pair_id` | int \| null |
| `pair_account_name` | str \| null |
| `has_receipt` | bool (default false) — M108 P4, one batched query per page |
| `reviewed_at` | datetime \| null — M106, NULL **is** "unreviewed" |
| `reviewed_by` | int \| null — M106, no FK |
| `parent_split_id` | int \| null — **M107**, set on split children only; null on ordinary rows and on split PARENTS |
| `is_split` | bool (default false) — **M107**, true iff this row still has children |
| `note` | str \| null — **M107**, plaintext ≤ 500 (docs/33 §Server-readable plaintext) |

### Error codes

| code | HTTP | raised when |
|---|---|---|
| `duplicate_transaction` | 409 | create/update collides on `account_id`+`dedup_hash` (explicit check or `IntegrityError`) |
| `transaction_in_pending_batch` | 409 | PATCH/DELETE/link/detect on a row owned by a `pending_review` batch (`_ensure_live`) |
| `validation_error` | 422 | Pydantic request validation (min_length, `^[A-Z]{3}$`, types, `extra` fields) **and** explicit null `account_id`/`raw_description` on PATCH |
| `not_found` | 404 | transaction/batch missing or not owned by user |
| `already_paired` | 409 | transfer link/accept when either side already paired (docs/14 touch point; not CRUD itself) |
| `transaction_is_split` | 409 | **M107**: a ledger-field `PATCH`, a `DELETE`, or a tag/transfer write aimed at a split **parent** (merge is the only edit it admits); also `POST /suggestions/{id}/accept` on a parent |
| `split_leg_immutable` | 409 | **M107**: an identity/money `PATCH` or a `DELETE` on a split **child**, a transfer write on a child, or reconciliation-linking a child |
| `encryption_required` | 422 | E2EE tenant sends a create/update without `payee_cipher` + `amount_cipher` (docs/33) |

### Frontend

`frontend/src/components/transactions/TransactionEditor.tsx` — one shared modal
for **add + edit** (distinguished by `target: Transaction | null`):
- Fields: Account (required select), Transaction date (required), Posting date,
  Payee (auto from description), Amount (required, signed `step="0.01"`),
  Currency (`maxLength=3`, `pattern="[A-Z]{3}"`, uppercased), Category (combo-box:
  type to filter existing categories, pick one, or create new on the fly via
  `POST /categories` — new category is auto-selected and categories query
  invalidated), Raw description (required).
- Local required-field check → inline `"Account, date, description, and amount are
  required."`; server error falls back to `apiErrorMessage(error)`.
- Closes on **Escape** (keydown, unless saving) and **backdrop mousedown**
  (`currentTarget === target`, unless saving).
- `onSave` sends `TransactionCreate | TransactionPatch` (snake_case, `null` for
  empties); reused by `Transactions.tsx` and `AccountDetail.tsx`.

Delete confirm lives in the host pages (not the editor): both
`frontend/src/pages/Transactions.tsx` and `frontend/src/pages/AccountDetail.tsx`
call `window.confirm("Delete this transaction permanently?")` before
`deleteTransaction(id)` (TanStack `useMutation`).

#### Bulk operations (M96 B1) — the same routes, sequenced

`frontend/src/components/transactions/BulkActionsToolbar.tsx` (the
`/transactions` multi-select, docs/09 §Bulk selection toolbar) adds **no route
and no payload shape** for a PAGE selection: `bulkActions.ts::runSequential`
issues the existing `PATCH /transactions/{id}` (recategorize / payee-set) or
`DELETE /transactions/{id}` once per selected id, in order, with a `n / total`
readout between rows. Every row keeps this document's contract exactly — the
delete path above runs per row, so each deleted row cleans its own
`LlmSuggestion` / `TransactionTag` / `ReconciliationItem` rows and unlinks its
counterpart through the FK.

#### Bulk over a matched set (M125) — the same helper, one request

When the selection is "every row the filter matches" rather than an explicit
id list, the toolbar posts `POST /transactions/bulk/{action}`
(docs/08 §Transactions, docs/09 §Select-all-matched) with the page's filter and
the ids the user unchecked. Per-row behaviour is deliberately identical:
`_bulk_delete` is the ONE internal delete path and calls the SAME
`_purge_transaction()` this section's single `DELETE /transactions/{id}` calls
(`docs/19` blast radius, byte for byte — dependents cleaned, one `deleted`
receipt, row gone), so a 345-row run is 345 single deletes in one transaction
rather than a different, wider operation. Delete additionally refuses with
`409 bulk_count_mismatch` unless the server's current count equals the
`expected_count` the confirmation printed.

A refusal never fails the batch: it lands in `BulkOutcome.conflicts`
(`{id, status, code, detail}`) and the UI reports partial success —
`Deleted 7 of 9 — 2 skipped` plus one line per skipped row carrying the
server's `{detail, code}`. Conflict kinds that exist today are
`409 transaction_in_pending_batch` (this document's pending-batch guard),
`404 not_found` (foreign or already deleted) and any `422` the row itself
raises.

**Deviation, reported rather than hidden.** The bulk-delete spec anticipated
rows whose *dependent trade lots* drop out of the batch with a per-row
conflict. This schema has **no trade → transaction foreign key at all** — the
only FKs into `transactions` are `llm_suggestions` (migration `0001`),
`transaction_tags` (`0016`), `reconciliation_items` (`0019`) and the
transfer-pair self-FK (`0008`) — so no trade-backed row can be refused today.
The per-row channel is generic, so when such a FK lands it is reported through
this same shape with no contract change; the deletion blast radius a row
actually carries is the one documented above.

`frontend/src/api/transactions.ts` — `createTransaction`, `updateTransaction`,
`deleteTransaction` hitting `/transactions` (POST/PATCH/DELETE); `Transaction`,
`TransactionCreate`, `TransactionPatch` interfaces mirror `TransactionPublic` /
the request bodies (Decimal as string).

## Models

`backend/app/models/transactions.py` — `Transaction`:
- `amount_native: Decimal` (`Numeric(18,2)`), `cad_amount: Decimal | None`
  (`Numeric(18,2)`).
- `transfer_pair_id: int | None` self-FK `transactions.id`; DB-level `ON DELETE SET
  NULL` (migration `0010`).
- `dedup_hash: str` + `UniqueConstraint("account_id", "dedup_hash")` (per-account
  uniqueness; drives `duplicate_transaction`).
- `import_batch_id: int | None` — `NULL` for CRUD rows.

Migrations: `0008_add_transfer_pair_id` (nullable self-FK), `0010_transfer_pair_on_delete_set_null`
(ON DELETE SET NULL). Manual CRUD adds no new migration. M107 added
`0042_split_transactions` (the four split columns + two CHECKs + one index,
docs/02) — it changes no CRUD contract, only adds the guards above.
(ON DELETE SET NULL). Manual CRUD adds no new migration.

## Services / touch points

- `app/api/transactions.py` — `create_transaction`, `update_transaction`,
  `delete_transaction`, `list_transactions`; helpers `_live_scope`,
  `_ensure_live`, `_parse_category_filter`.
- `app/importers/base.py` — `build_dedup_hash`, `normalized_payee`,
  `RawTransaction` (shared contract, no DB access).
- `app/services/fx.py` — `cached_cad_amount` (exact-date cache only; `None` on
  miss; never fetches).
- `backend/app/models/transactions.py` — `Transaction`.
- `frontend/src/api/transactions.ts` — types + `createTransaction` /
  `updateTransaction` / `deleteTransaction`.
- `frontend/src/components/transactions/TransactionEditor.tsx` — shared modal.
- `frontend/src/pages/Transactions.tsx`, `frontend/src/pages/AccountDetail.tsx` —
  host pages (editor open + delete confirm).

## Verification

- **Create (manual):** `POST /api/v1/transactions`
  `{account_id:1, date:"2026-05-01", raw_description:"Coffee", amount_native:"-4.50"}`
  → 201; `currency` defaults to account currency, `normalized_payee="Coffee"`,
  `cad_amount` = exact-date CAD or `null`, `import_batch_id=null`,
  `dedup_hash` stable per canonical string.
- **Dedup:** repeat identical payload → **409 `duplicate_transaction`**. Change
  `date` by one day → 201 (distinct hash).
- **Update:** `PATCH /api/v1/transactions/{id}` `{"raw_description":null}` →
  **422 `validation_error`**; `{"raw_description":"","amount_native":"-5.00"}` →
  **422 `validation_error`** (blank). Valid `{"amount_native":"-5.00"}` → 200 with
  recomputed `cad_amount`; changing `date`/`amount`/`raw_description` to match
  another row → **409 `duplicate_transaction`**.
- **Delete:** `DELETE /api/v1/transactions/{id}` → 204; its `LlmSuggestion` rows
  gone; a counterpart that referenced it via `transfer_pair_id` now reads `null`.
- **Pending batch guard:** PATCH/DELETE a `pending_review`-batch row →
  **409 `transaction_in_pending_batch`**; same row after commit → succeeds.
- **Scope:** `GET /api/v1/transactions` returns manual + committed rows only;
  staged/pending rows absent.
- **Frontend:** open Transactions → "New transaction" opens `TransactionEditor`;
  save with empty amount → inline required-field error; Escape/backdrop closes;
  delete → `window.confirm` then 204 and list refreshes. AccountDetail edit/delete
  path mirrors it for the account's rows.
- **Regression:** existing list/filter/similar/transfer endpoints (docs/08, docs/14)
  unaffected; `TransactionPublic` is **additive** for importers — M107 added
  `parent_split_id` / `is_split` / `note`, no existing field moved.
- **Split guards (M107):** ledger `PATCH` on a split parent → 409
  `transaction_is_split`, cipher-only `PATCH` on the same row → 200, `DELETE` on
  it → 409; `PATCH` `amount_native` on a child → 409 `split_leg_immutable`,
  `PATCH` `category_id` on that child → 200, `DELETE` on it → 409.
  unaffected; `TransactionPublic` shape unchanged for importers.
