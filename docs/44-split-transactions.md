# 44 — Split transactions (M107): parent/child ledger rows, two population rules, merge-back

> **Spec-first (Mark's ruling: "yes, spec-first into this wave").** This document is the
> whole contract. **No implementation code until Mark approves it on the card.** Named
> tests are written down here (M90/M93 pattern) so implementation cannot skip them.
>
> **Vocabulary (one word per thing):** a **split parent** (the statement row that was
> split) and its **children** (the rows it was split into). User-facing copy says
> *"Split · N parts"*; code, API and this doc say `child`.

## Context

A single statement line often belongs in several budget categories —
`COSTCO #123  -187.42` is groceries, kids' clothes and a pharmacy refill. Today the
ledger has exactly one `category_id` per row, so the user either miscategorizes or
picks the dominant bucket and distorts every spending report and budget.

M107 adds splits as a **parent/child model inside the existing `transactions` table**,
and ripples through five contracts that must be pinned before any code:

- **reports/aggregation** (every tab, plus budgets and dashboards),
- **register + dedup** (imported files, re-uploads, near-duplicate preview),
- **the M93 partner serializer** (docs/43),
- **E2EE export snapshots and their restore path** (docs/11, docs/33),
- **the M106 event log** (`transaction_events`, docs/02 — lands first in this wave).

**Sequence:** the budget-rollover card (board **M105**, landed in docs/10 as
**M110** — M105 was already taken by the TWR/MWR display) → M106 (review state +
activity log) → **M107 (this spec)** → M108/M109. M107's implementation writes
`split`/`merge` events through M106's writer, so M106 must be on `main` first.

**What splits are:** a personal-surface feature (retail/groceries/goods with several
buckets).

**What splits are not:** an investment feature. See §13 — trades, `acb_pools`, and all
of docs/06 are untouched, by construction.

## Decisions (the contract in short)

Mark can amend any line; everything below is implemented exactly as written here.

| # | Decision |
|---|---|
| D1 | Children are **rows in `transactions`** (`parent_split_id` FK), not a side table. One table keeps every existing reader one predicate away from correctness. |
| D2 | **Two population rules**, derived once and reused everywhere: **LEDGER** = money/category is counted (split parents out, children in); **IDENTITY** = a statement line is identified/matched (children out, split parents in). §4.2 names every consumer. |
| D3 | Splitting **changes no money total anywhere**: children sum exactly to the parent, in native *and* CAD. Enforced by a `split_checksum` column on the parent and refused with a clean error envelope. |
| D4 | Children inherit the parent's identity (account, date, posting date, description, payee, currency, ciphers, clearing state, import batch) and own `amount_native`, `category_id`, tags, `note`, and their own `cad_amount` (allocated from the parent's, never re-derived from FX). |
| D5 | The parent keeps **all** of its pre-split fields untouched (category, tags, note, amount, payee). They are inert while the split exists — never counted, never matched, never rendered. Merge is therefore an exact inverse. |
| D6 | **Dedup stays parent-level.** Children carry a salted `dedup_hash` and are excluded from exact and near-duplicate candidate sets. Imports never auto-split. |
| D7 | Four new endpoints, all under `/api/v1/transactions/{id}/split` (POST create, PUT replace, GET drilldown, DELETE merge). |
| D8 | While a split exists the parent admits **exactly one edit: merge**. Ledger-field `PATCH`/`DELETE` on the parent → `409 transaction_is_split`; cipher-only `PATCH` stays allowed (E2EE backfill). |
| D9 | Children admit their own `category_id` / `note` / cipher / review edits; identity and money fields are frozen → `409 split_leg_immutable`; a child can never be deleted on its own. |
| D10 | **Investment accounts cannot be split**: `422 split_account_not_allowed`, and the UI hides the affordance there. No child row, no `trades` row, no `acb_pools` row is ever written for an investment account. |
| D11 | `GET /transactions` (owner **and** partner) returns **LEDGER rows in `items`** plus a new **`split_parents`** array carrying the real parent row + `child_count`, so a page can render group headers without ever being double-countable. |
| D12 | Reports, budgets, dashboards, balances, CSV exports and the browser report engines all count children — **and the browser engines need zero code change** because they already read `GET /transactions` (test T14). |
| D13 | Partner: a child is visible **iff** its parent is (they share `account_id`, so scope is inherited by construction, asserted by test). Privacy mode masks `split_parents` amounts exactly like every other money field. |
| D14 | Export/restore: children and parents are ordinary `transactions` rows, so the registry-driven snapshot carries them automatically; the new FK is **`DEFERRABLE INITIALLY DEFERRED`** so a snapshot whose children precede their parents still restores. |
| D15 | M106 events: **one** `split` event on the parent for create, **one** for replace, **one** `merge` event on merge; children write no `created`/`deleted` events (their lifecycle *is* the parent's events), and later child edits write ordinary events with `tx_id = child`. |
| D16 | Out of scope: auto-splitting heuristics (receipt-capture card), splits on investment accounts, multi-currency children (parent currency only), nested splits, YNAB `subtransactions` import. |

## Approach

### 1. Schema (docs/02)

Four columns on the existing `transactions` table — one migration, no new table.

| column | type | null | meaning |
|---|---|---|---|
| `is_split` | `BOOLEAN NOT NULL DEFAULT false` | no | true **iff** this row has ≥1 child. Denormalized so every LEDGER predicate is one indexed boolean test. |
| `parent_split_id` | `INT FK → transactions.id` | yes | set on children only. `ON DELETE CASCADE` (batch discard/revert and account deletion take children with them) **and `DEFERRABLE INITIALLY DEFERRED`** (§10 restore ordering). |
| `split_checksum` | `Numeric(18,2)` | yes | **on parents only**: the exact amount children must sum to (set to `amount_native` at split time, cleared at merge). §3. |
| `note` | `VARCHAR(500)` | yes | free-text on a row. v1 UI exposes it on children (and in the split editor) only; the API accepts it on any row. Plaintext, like `raw_description` — listed in docs/33 §Server-readable plaintext. |

Constraints (named, so migrations and tests can reference them):

- `ck_transactions_split_flag`: `is_split = false OR (parent_split_id IS NULL AND split_checksum IS NOT NULL)`
  — a row is never both parent and child; a parent always carries its checksum.
- `ck_transactions_split_checksum`: `is_split = false OR split_checksum = amount_native`
  — while split, the parent's amount cannot drift away from the checksum.
- `ix_transactions_parent_split_id` on `(parent_split_id)` — group fetch, cascade, and
  the "does this row have children" probe.
- **No new index for `is_split`**: it is near-constant-false, so it adds nothing to
  `(user_id, date)` / `(user_id, account_id, date)`; queries keep using those.
- The child→parent FK is a **self-reference**, exactly like the existing
  `transfer_pair_id` (`0008`/`0010`), so the restore dependency graph already tolerates
  it (docs/11 step 3 fails a *table* cycle, not a self-FK).

Child row content (written in one DB transaction by the split service):

| field | value |
|---|---|
| `user_id`, `account_id`, `date`, `posting_date`, `currency` | copied from the parent, never editable afterwards |
| `raw_description`, `normalized_payee`, `payee_cipher`, `raw_desc_cipher`, `payee_token` | copied verbatim (ciphertext bytes included — same plaintext, same key) |
| `import_batch_id`, `cleared`, `force_include=false`, `is_duplicate=false` | copied / default — children inherit the parent's live scope and are never duplicates |
| `amount_native` | from the request (own amount, parent's sign) |
| `category_id`, `note`, `tags` | own; **the parent's category/tags are NOT copied** (D5) |
| `cad_amount` | allocated from the parent's `cad_amount` (§3.3) |
| `dedup_hash` | `f"{build_dedup_hash(account_id, raw)}:{uuid4().hex}"` — the salted shape staged duplicates already use, so no matcher can ever collide with it (§7) |
| `is_recurring`, `recurring_cadence`, `recurring_next_date` | `false` / `NULL` — the recurring badge is an identity-row property and stays on the parent |
| `transfer_pair_id` | `NULL` — children are never paired (§5) |
| `split_checksum`, `is_split` | `NULL` / `false` — a child can never itself be split (`split_nested`) |

Alembic: `00NN_split_transactions`. **Renumber to the next free head at implementation
time** — M105 and M106 are in flight in this wave and pick from the same range; run
`alembic heads` after the rebase and before pushing (AGENTS §6).

### 2. The two population rules (D2)

One module, `backend/app/services/txn_filters.py`, grows two predicates. **`committed_tx_conditions`
is left exactly as it is** — it is the staging rule (docs/03) and the export snapshot's
visibility filter must keep seeing *every* row or a restore would lose children.

```python
def countable_tx_conditions(user_id) -> list:   # LEDGER
    # committed_tx_conditions(user_id) + [Transaction.is_split == False]

def identity_tx_conditions(user_id) -> list:    # IDENTITY
    # committed_tx_conditions(user_id) + [Transaction.parent_split_id.is_(None)]
```

The rule a fresh agent applies when unsure: **money or category is counted → LEDGER;
a statement line is identified, matched or re-imported → IDENTITY.**

Verified consumers (every call site of `committed_tx_conditions` / `_live_scope` in
`backend/app`, classified):

| Consumer | Rule | Why |
|---|---|---|
| `services/budgets.py::budget_spent` (M105 extends it), `services/insights.py` ×2, `services/spending_comparison.py`, `services/networth_snapshot.py`, `api/goals.py`, `api/reports.py`, `api/spending_alerts.py` | **LEDGER** | spending/velocity/net-worth count children under their own categories |
| `api/accounts.py::_build_public` cash-balance sum **and** the `/accounts/ledger` running-balance rows (both inline predicates today — replaced by the shared helper) | **LEDGER** | totals are unchanged (D3) and the chart's rows match its sums |
| `api/transactions.py::list_transactions` (owner) and `api/partner.py` (partner, via the same `_transaction_page`) | **LEDGER** + `split_parents` | §6 |
| Browser `frontend/src/api/reports.ts::loadTxns` → `lib/reportEngine` / `sankeyEngine` / `totalReturnEngine` | **LEDGER** | it reads `GET /transactions`, which is LEDGER by construction — **no engine change** |
| `services/categorization.py` / `api/suggestions.py` candidate scan | **LEDGER** | a split parent's category is inert while split; a child may receive a suggestion |
| `api/rules.py` preview/apply | **LEDGER** | rules act on what is counted; split parents are skipped |
| `api/export.py` CSV transaction export | **LEDGER** | a CSV carries the categories that are actually counted (money totals are identical either way) |
| `api/transactions.py::_recurring_inputs`, `services/subscriptions.py`, `api/search.py` (payee_token), `api/imports.py` + `services/import_staging.py` (exact dedup), `find_near_duplicates`, `api/export.py` OFX | **IDENTITY** | one statement line = one row: no duplicate hits, no double-counted subscriptions, OFX re-import still dedups to the parent |
| transfer suggestions (`/transactions/transfer-suggestions`), `detect/link/unlink-transfer`, `accept-transfer-suggestion`, `reconciliation.py` link target | **IDENTITY** (and refuse a child as a target, §5) | pairs and statement matches are row-level, never part-level |
| `api/export.py` `GET /export/full` + ZIP/backup snapshot (`_visibility_filter`) | **unchanged (`committed_tx_conditions`)** | the round-trip needs parents *and* children |

**Child counts are row counts.** `TransactionPage.total` counts matching LEDGER rows, so
a three-child split contributes 3 to an unfiltered total and to a `limit`/`offset` page.
Pinned deliberately: the number that feeds reports must be the number the register shows.

### 3. Money math (D3)

#### 3.1 The sum invariant

`Σ(children.amount_native) == parent.split_checksum == parent.amount_native`, exact to
the cent in `Decimal` (AGENTS §2 — no float ever touches this).

Enforced at every write (create, replace), before anything is inserted, and refused with
**`422 {"detail": "…", "code": "split_sum_mismatch"}`** where `detail` states the
expected sum and the residual, e.g.
`Children must sum to -187.42; they sum to -177.42 (residual -10.00).`

`GET /transactions/{id}/split` returns `sum_ok`, recomputed on read, so a test (and QA)
can assert integrity without writing.

#### 3.2 Leg rules

- **2 … 20 children** (`split_too_few_legs` / `split_too_many_legs` — validated in the
  handler, *not* with Pydantic `min_length`, so the envelope carries the specific code
  instead of a generic `validation_error`).
- every child's sign equals the parent's (`split_leg_sign`), no zero child
  (`split_leg_zero`), zero-amount parent can't be split (`split_zero_parent`).
- child order is request order (create) / payload order (replace); `GET` returns children
  ordered by `id ASC` (entry order).

#### 3.3 CAD allocation — a split never moves a CAD total

`cad_amount` is **allocated from the parent's cached value, never re-derived from FX**:
re-deriving would round each child independently (±1¢ drift) and could use a different
rate than the parent was priced at.

```
parent.cad_amount is None  →  every child.cad_amount = None   # unresolved stays unresolved
otherwise:
    residual = parent.cad_amount − Σ quantize(HALF_UP, parent.cad_amount * child.amount / parent.amount, 0.01)
    child_i.cad_amount = quantize(HALF_UP, parent.cad_amount * child_i.amount / parent.amount, 0.01)
    residual (≤ a few cents, may be negative) is added to the child with the LARGEST |amount|;
    ties → lowest index.
```

Consequence, asserted by test T8: **every CAD total in the product — reports, budgets,
net worth, CSV, partner figures — is byte-identical before and after a split.** Only the
*category* the money lands in changes. That is the whole point of the feature.

### 4. Endpoints

All JWT, `/api/v1`, tenant-scoped, `extra=forbid` bodies, Decimal strings on the wire.
**No new demo-tenant rule** — the existing global, method-scoped guard inside `get_current_user` already answers `403 demo_account_restricted` for these three writes on the demo tenant, exactly as it does for `POST`/`PATCH`/`DELETE /transactions` (and no route-specific rate limit either).

#### 4.1 Shapes

```jsonc
// POST /api/v1/transactions/{transaction_id}/split          → 201 TransactionSplit
// PUT  /api/v1/transactions/{transaction_id}/split          → 200 TransactionSplit  (replaces all children)
{
  "children": [
    { "amount_native": "-120.00",          // Decimal string, required
      "category_id": 4,                     // int | null
      "note": "kids clothes",               // str ≤ 500 | null
      "tag_ids": [2, 7],                    // int[] (default []), every id ownership-checked
      "amount_cipher": "v1.…",              // required when users.encryption_required (§10)
      "cad_amount_cipher": null }           // same rule as POST /transactions today
  ]
}
// → 201/200 TransactionSplit
{ "parent": SplitParentPublic,              // real parent row (below)
  "children": [TransactionPublic, …],       // id ASC
  "split_checksum": "-187.42",
  "sum_ok": true }
```

```jsonc
// GET /api/v1/transactions/{transaction_id}/split           → 200 TransactionSplit
//     the drilldown the register opens; always fresh, always recomputes sum_ok.
// DELETE /api/v1/transactions/{transaction_id}/split        → 200 TransactionPublic
//     merge-back: children deleted, parent restored (§5).
```

**Additive shape changes** (every existing consumer keeps working — fields are added,
never moved):

| shape | added field(s) |
|---|---|
| `TransactionPublic` | `parent_split_id: int \| null`, `is_split: bool` (default `false`), `note: str \| null` |
| `SplitParentPublic(TransactionPublic)` | `child_count: int` — children this parent has, independent of the current filter |
| `TransactionPage` | `split_parents: list[SplitParentPublic] = []` — **the parents of the children in `items`**, `date desc, id desc`, one statement (§6.4), `[]` when the page has no children |
| `PartnerTransactionPage` | `split_parents: list[PartnerSplitParentPublic]` (same two money fields widen to `Decimal \| null`) |
| `TransactionUpdate` | `note: str \| null` |

`TransactionCreate` does **not** gain `note` (creating a split is a separate, explicit
action — an imported or manually created row never arrives pre-split).

#### 4.2 Validation order (tests assert *which* code wins)

POST and PUT run all 13 steps; GET and DELETE `/split` stop after step 5.

1. `owned_or_404` → `404 not_found`
2. `_ensure_live` → `409 transaction_in_pending_batch`
3. row is a child (`parent_split_id IS NOT NULL`) → `422 split_nested`
4. account type ∈ `INVESTMENT_ACCOUNT_TYPES` → `422 split_account_not_allowed` (§13)
5. POST: parent already `is_split` → `409 already_split` · PUT/GET/DELETE: not split → `409 not_split`
6. Pydantic body (extra key, wrong type, `note` > 500, blank) → `422 validation_error`
7. `users.encryption_required` and any child missing `amount_cipher` → `422 encryption_required`
8. child count < 2 → `422 split_too_few_legs` · > 20 → `422 split_too_many_legs`
9. parent `amount_native == 0` → `422 split_zero_parent`
10. per child: zero amount → `split_leg_zero`; sign ≠ parent → `split_leg_sign`
11. `category_id` / every `tag_id` ownership → `404 not_found`
12. sum ≠ `split_checksum` → `422 split_sum_mismatch`
13. insert everything in one transaction, then one `split` event (§11); a unique race →
    `409 duplicate_transaction` (same path as create — unreachable in practice because
    child hashes are salted)

#### 4.3 Error envelope (new codes, added to docs/08's code list)

| code | HTTP | raised when |
|---|---|---|
| `split_account_not_allowed` | 422 | split requested on an investment account (detail names the account type) |
| `split_nested` | 422 | splitting a child (no nesting, ever) |
| `already_split` | 409 | POST on a parent that is already split |
| `not_split` | 409 | PUT/GET/DELETE `/split` on a row that is not split |
| `split_too_few_legs` / `split_too_many_legs` | 422 | < 2 or > 20 children |
| `split_zero_parent` | 422 | parent amount is `0.00` |
| `split_leg_zero` / `split_leg_sign` | 422 | a child amount is `0.00` / has the wrong sign |
| `split_sum_mismatch` | 422 | children don't sum to `split_checksum` |
| `transaction_is_split` | 409 | ledger-field `PATCH`, `DELETE`, or a tag/transfer write aimed at a split parent (reconciliation-linking a parent is deliberately ALLOWED — §5) |
| `split_leg_immutable` | 409 | identity/money field change, child `DELETE`, a transfer write aimed at a child, or reconciliation-linking a child (§5) |
| `encryption_required` | 422 | existing code, new trigger: a child without `amount_cipher` under `users.encryption_required` |

Unchanged and still reachable on these routes: `validation_error` (422),
`not_found` (404), `transaction_in_pending_batch` (409), `duplicate_transaction` (409).

### 5. Mutation matrix (D8/D9) — merge-back rules

| Target / operation | Allowed? | Answer |
|---|---|---|
| parent: `DELETE /transactions/{id}` | **no** | `409 transaction_is_split` — "This transaction is split. Merge its parts first." (never a cascade surprise) |
| parent: `PATCH` with any ledger field (`account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency`, `category_id`, `note`) | **no** | `409 transaction_is_split` |
| parent: `PATCH` **only** cipher/index fields (`payee_cipher`, `raw_desc_cipher`, `amount_cipher`, `cad_amount_cipher`, `payee_token`) | **yes** | `200` — the E2EE backfill (docs/33 A17) must keep working |
| parent: `POST /transactions/{id}/split` | no | `409 already_split` (replace is `PUT`) |
| parent: `DELETE /transactions/{id}/split` (**merge**) | **yes** | `200 TransactionPublic` — deletes children, `is_split=false`, `split_checksum=NULL`, everything else byte-identical to pre-split (T9) |
| parent: tag attach/detach (`POST/PUT/DELETE …/tags`) | no | `409 transaction_is_split` (tags live on children while split) |
| parent: transfer detect/link/unlink | no | `409 transaction_is_split` |
| parent: reconciliation `transaction_id` link target | **yes** | allowed — a statement line matches a statement row; the link writes `reconciliation_items`, never the transaction |
| parent: recurring accept/suppress | **yes** | unchanged — the badge is an identity-row property and stays on the parent |
| child: `PATCH` `category_id`, `note`, ciphers, review state (M106) | **yes** | `200 TransactionPublic` |
| child: `PATCH` `account_id`, `date`, `posting_date`, `raw_description`, `normalized_payee`, `amount_native`, `currency` | no | `409 split_leg_immutable` — change amounts only by replacing the whole split (`PUT`) |
| child: `DELETE /transactions/{id}` | no | `409 split_leg_immutable` — detail points at *Re-split* / *Merge* |
| child: transfer detect/link/unlink | no | `409 split_leg_immutable` |
| child: reconciliation `transaction_id` link target | no | `409 split_leg_immutable` — a statement line never matches part of one |
| child: tag attach/detach, review flip, category inline edit | **yes** | normal row behaviour (this is why children are rows) |

Merge mechanics, pinned so it cannot be improvised: children are removed **through the
same internal helper as a normal transaction delete** (so `llm_suggestions`,
`transaction_tags`, `reconciliation_items` and the children's own
`transaction_events` are cleaned exactly as a normal delete cleans them), while the
parent keeps its own event history. Nothing else about the parent is touched.

**Rejected alternative (reported, not hidden):** clearing the parent's
`category_id`/tags at split time and stashing them for merge. Rejected because it makes
merge lossy (any missed field silently disappears) and needs a second copy of the data.
Keeping them untouched gives an exact inverse for free; their inertness is guaranteed by
D2, not by deletion.

### 6. Register, list and grouping (D11)

#### 6.1 Population

`GET /api/v1/transactions` (owner **and** `/partner/transactions`) answers **LEDGER
rows in `items`**: children and ordinary rows, **never a split parent**. Plus
`split_parents` for the headers.

Consequences, all deliberate:

- a naive consumer that sums `items` **cannot** double-count (the parent is absent);
- the browser report engines are correct with zero changes (D12);
- MCP `transactions_list` is safe to sum, and it can still see structure because every
  row now carries `parent_split_id` / `is_split`.

#### 6.2 Filters

| filter | evaluated against |
|---|---|
| `from`, `to`, `payee`, `payee_token`, `account_id` | the child's own copied identity — every child of a group matches together (no partial groups) |
| `amount_min`, `amount_max`, `direction` | **the parent's `amount_native`** (self-outer-join on `parent_split_id`, `COALESCE(parent.amount_native, own.amount_native)`) — an amount filter finds the *statement*: the bound is compared against the parent's own SIGNED amount, so a **`+187.42`** statement split into `+120.00 / +67.42` returns **both** children for `amount_min=100`, and a **`-187.42`** statement split into `-120.00 / -40.00 / -27.42` returns **all three** for `amount_max=-100` (a negative statement cannot satisfy a `min` bound — `-187.42 >= 100` is false, which is the correct answer) |
| `category_id`, `tag_id`, `tag_ids` | the child's own category/tags — the filter that splitting exists for |

A content filter therefore returns *some* children of a group; `split_parents` still
carries the parent (with the true total and `child_count`), so the header is never
wrong, and the drilldown (§6.4) always returns the complete group.

`total` counts matching LEDGER rows (§2).

#### 6.3 Rendering rule (docs/09)

Every surface that renders transaction rows uses **one shared grouping component**
(`frontend/src/components/transactions/SplitGroupRow.tsx`) over `items` + `split_parents`:

- group by `parent_split_id`, header at first occurrence, children ordered `id ASC`;
- the header renders the parent's date / payee / account / **`amount_native` total** and
  the literal badge **`Split · N parts`** (`N` = `child_count`), in place of the category
  chip;
- the header's checkbox toggles **every child of that group present in the page** (bulk
  recategorize then works per child; bulk delete refuses per child and reports the
  conflicts through M96's existing `BulkOutcome.conflicts` channel);
- the header carries **no** inline category combo-box, **no** quick-tag popover, **no**
  Edit and **no** Delete — the affordance is hidden wherever the action is refused (the
  same rule the card applies to investment accounts). Its action cluster is
  **Expand** and **Merge back**;
- expansion is a button with `aria-expanded` + `aria-controls` over a labelled region
  (M102's scroll-region rule applies to the region), target ≥ 44 px, and **always fetches**
  `GET /transactions/{id}/split` so the panel is complete and checksum-verified even when
  a filter hid some children;
- surfaces in scope: `/transactions`, `/accounts/:id`, Dashboard's recent list,
  the fuzzy search view, and the partner `/shared` feed — all read `TransactionPage`, so
  they all get `split_parents` from the same payload;
- if a header is somehow missing for a child (server bug), render the child as an
  ordinary row — fail open, never crash.

The parent's pre-split category/tags/note are **not rendered anywhere while split**
(they exist only to come back on merge); the expanded panel shows the children, each with
its own amount, category, tags and note.

#### 6.4 Cost

`split_parents` is **one conditional statement**, keyed by the page's distinct
`parent_split_id`s and returning `child_count` in the same aggregate
(`LEFT JOIN … GROUP BY`) — the same pattern as the existing pair-account lookup in
`_transaction_page`, taken only when the page actually contains children.
`GET /transactions` therefore stays inside its docs/34 budget — **5** statements for
a page with no split children and **6** with them (5 + the one conditional
`split_parents` fetch; docs/34 states both) — and
`test_transactions_query_count_is_page_size_independent` keeps passing; if a future perf
dataset ever contains splits, the ceiling and docs/34 move together in that change.

### 7. Dedup / import interplay (D6)

Pinned choice, stated in the doc because it is a real fork in the road:

- **Dedup keys stay parent-level only.** The canonical hash (`build_dedup_hash`,
  docs/19) is computed over the statement's identity, which a split never changes —
  the parent's `dedup_hash` is frozen while split (its identity fields cannot be
  edited, §5), so it is a stable anchor.
- **A re-imported statement line matches the PARENT, never a child.** Exact-dedup
  (`import_staging.py`, `api/imports.py`) looks up `account_id + dedup_hash`; children's
  hashes are salted (`canonical:uuid`) so no computed canonical can ever equal one.
- **Near-duplicate preview excludes children** (`find_near_duplicates` candidate query
  gains `parent_split_id IS NULL`) — a child's amount must never make a re-upload look
  like a *new* row.
- **OFX export uses IDENTITY** (parents only): the statement keeps its line count and
  round-trips to `0 net-new` exactly as `tests/test_export_ofx.py` pins today.
- **Imports never auto-split.** v1 has no detection heuristic, no shape sniffing, no
  receipts-triggered split: a split is always an explicit user action. A staged row
  arrives as one row and stays one row.
- If a batch containing a split parent is reverted/discarded, `ON DELETE CASCADE` takes
  the children with it — batch semantics are unchanged, no partial split can survive.

YNAB (docs/38): `subtransactions` keep importing as the parent total with the existing
`split_detail_dropped` warning — the wording "fin_tracker has no split rows" becomes
"fin_tracker does not import YNAB subtransactions (split rows import as the parent total;
you can split them by hand afterwards)". No auto-split, per D16.

### 8. Reports, budgets and the honest numbers (D12)

- Every aggregation counts **children**; split parents are excluded by the LEDGER
  predicate. Nothing else changes: bucketing, zero-fill, sign conventions, root
  roll-up, unresolved-fx exclusion, and the Sankey's conservation rules are untouched.
- **Totals are preserved exactly** (§3): a split cannot move a dollar between buckets of
  a *report total*, only between *categories*. Honest-pct denominators (M65/M97) and the
  8-decimal wire strings therefore read identically before and after a split (T14).
- **Budgets are the point of the feature**: `budget_spent` counts each child under its
  own category, so `COSTCO` groceries + apparel + pharmacy hit three budget rows. M110's
  `carried_in` math and M108's budget roll-up read the same helper, so no budget code
  changes beyond switching the predicate.
- **F-10 encryption inventory (docs/33):** one new row —
  `POST/PUT/DELETE /transactions/{id}/split` requires a per-child `amount_cipher` under
  `users.encryption_required`; `payee_cipher`/`raw_desc_cipher`/`payee_token` are copied
  server-side from the parent (same plaintext), `cad_amount_cipher` follows today's
  create rule (optional; the A17 backfill fills it later).
- Perf budgets (docs/34): the added work is predicates and one conditional statement,
  never a per-row query — `test_perf_budgets.py` runs unchanged.

### 9. Partner projection (D13)

- `GET /partner/transactions` is built by the same `_transaction_page`, so the partner
  gets LEDGER `items` + `split_parents` — parents with the split badge and children,
  exactly as the card asks, without a second serializer to drift.
- **Scope invariant:** a child is visible iff its parent is. Children copy
  `account_id`, and scope is account-level (`sharing_links.account_ids`), so this holds
  *by construction*; test T16 asserts it both ways (shared account → parent + children;
  unshared account → neither).
- No per-child allowlist, no child-level scope, no new partner route (D13 keeps M90's
  "one projection, one mask" rule: `app/schemas/sharing.py` gains
  `_project_split_parent` beside `_project_transaction`).
- **Privacy mode:** `split_parents[].amount_native` / `cad_amount` are masked to `null`
  exactly like every other money field — the group header must show `🔒`, not a leak.
- M108's budget roll-up sees children under their own categories (the POINT of
  splitting); M106 events stay owner+partner-actor scoped as that card pins them.

### 10. E2EE, export and restore (D14)

- **Snapshot:** `GET /export/full`, the ZIP and the browser backup are registry-driven
  (docs/11), so `transactions` children ride along with **`parent_split_id` preserved
  verbatim** — no field is invented for the export, and `_visibility_filter` keeps
  using `committed_tx_conditions` so neither side of the relationship is dropped.
- **Restore:** rows are inserted in array order inside one transaction; the new FK is
  `DEFERRABLE INITIALLY DEFERRED`, so a snapshot whose children appear *before* their
  parent still commits (the existing `transfer_pair_id` self-FK is the precedent that
  self-references don't trip the dependency-order cycle check). Required test: reorder
  the `transactions` array children-first and restore → success, relationships identical.
- **Cipher columns:** children carry their own `amount_cipher` (required when
  `encryption_required`), the parent's `payee_cipher`/`raw_desc_cipher`/`payee_token`
  bytes, and `cad_amount_cipher` per today's rule. Locked-vault rendering needs no new
  branch: a child is a normal row with a normal cipher set.
- `note` is **plaintext** in v1 (like `raw_description`) and must be listed in docs/33's
  server-readable-plaintext inventory — the wrong omission would overstate the posture.

### 11. Event log integration (M106, D15)

`transaction_events` (M106) gains one enum value and three write points. `event_kind`
values: **`split`** and **`merge`** (`merge` is new; StrEnum widening needs no migration
— if M106 ships the enum first, M107 adds it in the same style).

| action | events written |
|---|---|
| `POST …/split` | **one** `split` event, `tx_id = parent.id` |
| `PUT …/split` | **one** `split` event, `tx_id = parent.id` (payload carries before/after) |
| `DELETE …/split` (merge) | **one** `merge` event, `tx_id = parent.id` (payload carries the deleted child ids, so the history survives the deletion) |
| child created / deleted as part of the above | **none** — the parent's event is the record (one mutation path → one event, M106's rule) |
| child `PATCH` category / tags / note / review | ordinary `categorized` / `tagged` / `edited` / `reviewed` events with `tx_id = child.id` |

**M120 did not widen this table.** Receipts for the four surfaces that used to
be silent (suggestions-accept, rules-apply, recurring-accept, the pipeline's
three layers — docs/02 §transaction_events, docs/10 §M120) were explicitly
checked against merge-back and replace-split and **neither gained a receipt**:
the parent's fields are byte-identical after both (D5 / T9), so there is no
unreceipted field change to narrate, and a split parent is LEDGER-invisible to
both new receipting surfaces (`countable_tx_conditions` and the pipeline's own
`LEDGER_EXCLUDE_SPLIT`), so neither can reach one. Pinned by
`tests/test_m106_review_events.py::test_m120_merge_and_replace_split_gain_no_new_receipt`.

`payload_hash` is the sha256 hex of the canonical body — byte-identical formula to
`sharing.py::_args_hash` / `mcp_audit.args_hash`:
`json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()` —
over:

```jsonc
// split (create): {"op": "create", "parent_id": 51, "child_ids": [812, 813], "amounts": ["-120.00", "-67.42"]}
// split (replace): {"op": "replace", "parent_id": 51, "before": [812, 813], "after": [820, 821, 822]}
// merge:           {"op": "merge",  "parent_id": 51, "child_ids": [812, 813]}
```

Ids and amounts only — no payee, no note, nothing a DB reader could mine; the hash can't
lie about what happened (M106's receipt property).

### 12. UI contract (docs/09)

- **Entry point:** a **Split** item in the row action cluster beside Edit/Delete on
  `/transactions` and `/accounts/:id`, keyboard-reachable, ≥ 44 px. **Hidden entirely**
  when the row's account type ∈ `INVESTMENT_ACCOUNT_TYPES` (client hides, server refuses
  — defence in depth, D10).
- **Editor** `frontend/src/components/transactions/SplitEditor.tsx`: a modal over the
  row's amount, starting with two children. The first child is prefilled with the
  parent's category and the rest are empty — a **client-side convenience only**: the
  server copies **identity fields only** (D4) and never `category_id` or tags (D5).
  Per child: amount (signed `step="0.01"`), category
  combo-box (M104's `CategoryPicker`), tag picker (`TagPicker`), note. Live line
  **`Remaining: -10.00`** driven by exact Decimal-string arithmetic; submit is disabled
  until it reads `Remaining: 0.00`. Add/remove child rows (2–20). Escape/backdrop rules
  identical to `TransactionEditor`. A server envelope renders `detail` verbatim plus the
  code — never a generic "something went wrong".
- **Re-split / merge back** live in the expanded panel: *Re-split* reopens the editor
  prefilled with the current children (`PUT`), *Merge back* is a two-step confirm
  (M95's destructive-action pattern) that calls `DELETE …/split`.
- **Honesty (docs/09):** the UI never claims a feature it doesn't have — no
  "auto-split", no "AI split"; the copy says *Split this transaction into parts*.
- **A11y:** group header = one focusable button with `aria-expanded`/`aria-controls`;
  children live in a labelled `role="region"` (M102 rule); the badge text is part of the
  header's accessible name; axe clean with a group open, both themes, 390/412 and 1440.
- **No new ⌘K palette entry in v1** (out of scope; note it if wanted later).

### 13. Investment accounts and Canadian tax — explicit no-op

Splits are a **personal-surface** feature. Investment accounts
(`INVESTMENT_ACCOUNT_TYPES` = `brokerage_non_registered | rrsp | tfsa | fhsa | resp |
lira | rdsp`, docs/02) are excluded at both ends:

1. `POST/PUT …/split` on a row in one of those accounts → `422 split_account_not_allowed`
   (detail names the account type), **before** any write;
2. the UI hides the affordance there;
3. **there is no path — not even a bug-level one — that puts a child row on a trade.**
   The ACB engine reads `trades` only (docs/02 invariant: `acb_pools` is rebuildable from
   `trades` alone); `trades` has no FK to `transactions` at all (docs/19), so a child
   cannot attach to one. **Canadian ACB/capital-gains math therefore sees exactly one
   transaction per trade, byte-identical to today: no child rows on trade records, no ACB
   change, no docs/06 edit.** Test T4 asserts the refused call writes no child, no trade
   and no `acb_pools` row.

## Touch points

Implementation (files this feature owns or edits):

| Area | Files |
|---|---|
| schema | `backend/app/models/transactions.py`, `backend/alembic/versions/00NN_split_transactions.py` |
| predicates | `backend/app/services/txn_filters.py` (`countable_tx_conditions`, `identity_tx_conditions`) |
| endpoints | `backend/app/api/transactions.py` (4 routes, list population + `split_parents`, mutation guards), `backend/app/api/partner.py` (projection passes `split_parents`) |
| shapes | `backend/app/schemas/transactions.py`, `backend/app/schemas/sharing.py` |
| consumers switching predicate | `services/budgets.py`, `services/insights.py`, `services/spending_comparison.py`, `services/networth_snapshot.py`, `services/subscriptions.py`, `services/categorization.py`, `api/accounts.py` (2 inline sites), `api/goals.py`, `api/rules.py`, `api/search.py`, `api/imports.py`, `services/import_staging.py` (+ `find_near_duplicates`), `api/export.py` (CSV → LEDGER, OFX → IDENTITY), `api/reconciliation.py` |
| events | M106's writer + `event_kind` widening (`merge`) |
| frontend | `api/transactions.ts` (types + 4 calls), `components/transactions/SplitGroupRow.tsx`, `SplitEditor.tsx`, `pages/Transactions.tsx`, `pages/AccountDetail.tsx`, `pages/Dashboard.tsx`, `pages/SharedHousehold.tsx`, search view |

Doc reconciliation carried by the **implementation** commit (this spec commit adds only
this doc + the roadmap entry, per AGENTS §7 / the M90 precedent `02e5c67`):

| doc | what changes |
|---|---|
| `docs/02` | four columns, two CHECKs, one index, ERD note |
| `docs/08` | four endpoints, additive shape fields, `total` semantics, the 11 new error codes |
| `docs/09` | `SplitGroupRow` rendering rule, `SplitEditor`, hidden affordances, a11y |
| `docs/03` | dedup stays parent-level; children salted + excluded from near-dup; no auto-split |
| `docs/12` | population rule (children counted, parents excluded), totals preserved |
| `docs/19` | mutation matrix (§5) and the additive `TransactionPublic` fields |
| `docs/11` | deferrable FK + the children-first restore test |
| `docs/33` | F-10 wire-inventory row; `note` in server-readable plaintext |
| `docs/43` | `split_parents` on the partner page, scope invariant, privacy-mode masking |
| `docs/34` | the conditional-statement note on `GET /transactions` |
| `docs/38` | YNAB split wording |
| `docs/10` | the M107 milestone entry (already wired by this commit) |

## Named tests (M90/M93 pattern — these names are the contract)

Backend, `backend/tests/test_split_transactions.py`:

| test | asserts |
|---|---|
| `test_split_children_sum_exactly_to_the_parent` | 3 children on `-187.42` → 201; `Σ amount_native` exact; `parent.is_split`; `split_checksum == amount_native`; `GET …/split` `sum_ok: true` |
| `test_split_sum_mismatch_is_refused_and_writes_nothing` | children summing `-177.42` → 422 `split_sum_mismatch` with the residual in `detail`; **zero rows written**, parent untouched |
| `test_split_leg_sign_zero_and_count_rules` | wrong-sign child → `split_leg_sign`; `0.00` → `split_leg_zero`; 1 child → `split_too_few_legs`; 21 → `split_too_many_legs`; zero parent → `split_zero_parent` |
| `test_split_validation_order_is_deterministic` | one request tripping several rules answers exactly the code §4.2 puts first |
| `test_investment_accounts_are_excluded_from_splitting` | brokerage/tfsa/rrsp parents → 422 `split_account_not_allowed`, detail names the type; chequing/credit_card → 201; **no child, no trade, no `acb_pools` row** written by the refused call |
| `test_nested_split_and_split_state_codes` | POST on a child → `split_nested`; second POST → `already_split`; DELETE on an unsplit row → `not_split` |
| `test_parent_mutation_is_refused_while_split` | ledger `PATCH` → `transaction_is_split`; `DELETE` → `transaction_is_split`; cipher-only `PATCH` → 200 (backfill still works); tag attach → `transaction_is_split` |
| `test_child_edit_matrix` | `category_id`/`note` → 200; `amount_native`/`date`/`account_id` → `split_leg_immutable`; `DELETE` child → `split_leg_immutable` |
| `test_merge_is_an_exact_inverse` | snapshot the parent row → split → merge → every field byte-identical (`amount_native`, `cad_amount`, `category_id`, `dedup_hash`, tags, `note`, ciphers), children gone, `is_split=false`, `split_checksum=NULL` |
| `test_cad_allocation_never_moves_a_cad_total` | 3 children, parent `cad_amount` set → `Σ child cad_amount` equals the parent's to the cent (residual absorbed by the largest child); parent `NULL` → all children `NULL` |
| `test_ledger_population_counts_children_and_never_parents` | unsplit vs split fixtures: `budget_spent`, spending report, cashflow, net-worth and `accounts` cash_balance **byte-identical totals**; category breakdown moves the money to the children's categories |
| `test_list_returns_ledger_rows_plus_split_parents` | `items` has children + ordinary rows and **no split parent**; `split_parents` carries the parent with the right `child_count`; `total` counts children |
| `test_list_filters_use_parent_identity_and_child_content` | `category_id=<child cat>` → only matching children (+ parent in `split_parents`); `amount_min` above a child's amount → the **whole group**; `payee_token` → every child |
| `test_import_dedup_matches_the_parent_and_never_a_child` | re-upload the same CSV → `duplicates_skipped` counts the parent (matched on its unchanged hash); a child's salted hash never matches; `find_near_duplicates` sees no child |
| `test_import_never_auto_splits` | a staged row lands as one row with `is_split=false`, `parent_split_id=NULL` |
| `test_ofx_export_is_identity_and_round_trips` | after a split, the OFX holds the parent line (no children) and re-imports to `0 net-new` |
| `test_search_and_recurring_are_identity` | `GET /search?payee_token=` returns **one** hit (the parent), not N children; recurring candidates contain no child |
| `test_export_restore_roundtrip_reconstructs_relationships` | export → wipe → restore → `GET /export/full` semantically equal **and** `parent_split_id` identical; repeat with the `transactions` array reordered children-first → restore still succeeds |
| `test_partner_sees_children_with_their_parent_and_masks_them` | shared account → partner gets parent + children; unshared account → neither; privacy mode → `split_parents[].amount_native` is `null` and the byte-scan finds no seeded amount |
| `test_split_and_merge_write_exactly_one_event_each` | M106: one `split` event on create, one on replace, one `merge` on merge; **no** `created`/`deleted` events for children; `payload_hash` hand-recomputed (sorted-keys canonical JSON) matches; child category edits write `categorized` with `tx_id = child` |
| `test_transfer_and_reconciliation_refuse_split_rows` | detect/link/unlink on a parent → `transaction_is_split`; on a child → `split_leg_immutable`; suggestion lists and reconciliation link targets never contain a child |
| `test_perf_list_query_count_unchanged_with_splits` | `GET /transactions` on a page containing children stays at budget + 1 = 6 statements — 5 for a page without splits, 6 with them (one conditional `split_parents` fetch, no N+1) |

Frontend, `frontend/tests/splitTransactions.test.tsx` (+ fixture):

| test | asserts |
|---|---|
| `splitGrouping` | 3 children render **one** header (parent payee/date/total + `Split · 3 parts`), children in `id ASC` on expand; bulk-select on the header selects the children |
| `splitEditor` | `Remaining:` line in exact Decimal arithmetic, submit disabled until `0.00`, server `split_sum_mismatch` rendered verbatim |
| ~~`splitReportParity`~~ | **[RETIRED 2026-10-11, M160 / P2-5]** — it diffed the client report engines against each other, and those engines are deleted. The invariance it proved is still asserted server-side by `backend/tests/test_split_transactions.py::test_ledger_population_counts_children_and_never_parents` |
| `splitInvestmentHide` | no Split affordance on a row in an investment account; the row's accessible-name sweep stays clean |
| `splitA11y` | header button `aria-expanded`/`aria-controls` wiring, labelled region, axe-core 0 violations with the group open, both themes |
| `splitPartnerFeed` | `/shared` groups the same way; privacy mode shows `🔒` in the header amount |

## Verification

**Unit/integration:** the **28 named tests** above (22 backend + 6 frontend),
`cd backend && uv run pytest -q` and
`cd frontend && npx vitest run`, all green (AGENTS §4 gates 1–3 unchanged).

**Doc reconciliation:** every row of the touch-point table lands in the same commit as
the code it describes (AGENTS §9).

**Running-stack smoke (AGENTS §4 step 5, through `http://localhost:8080`):**

1. `docker compose up -d --build` → open `/transactions`, split a `-187.42` committed
   row into `-120.00` (Groceries) / `-40.00` (Apparel) / `-27.42` (Health) →
   the row becomes one header with `Split · 3 parts`; expanding shows three rows with
   their own categories.
2. Money check by hand: Spending → Categories for the range shows the three categories
   carrying the split amounts and **the same grand total as before the split** (hand
   arithmetic, as standard evidence); Budgets shows the three categories' `spent`
   move by exactly those amounts.
3. `GET /transactions?category_id=<Apparel>` returns only that child plus
   `split_parents` containing the parent; `amount_min=100` returns the whole group.
4. Re-upload the same statement file → `duplicates_skipped` reports the parent as a
   duplicate; nothing new is staged; no auto-split appears.
5. Merge back → the row is exactly what it was (payee, amount, category, tags), the
   badge is gone, and the report totals are unchanged again.
6. On an investment account: no Split item in the row's action cluster, and
   `POST /api/v1/transactions/{id}/split` → `422 split_account_not_allowed`.
7. Export → wipe (demo reset or a throwaway account) → restore → the split relationships
   are identical; `GET /export/full` is semantically equal.
8. With a partner linked: `/shared` shows the group with a badge; with the owner's
   privacy mode on, the header amount is `🔒`.
9. `GET /transactions?limit=50` measured ≤ 6 SQL statements with a split on the account (5 without one).

**Evidence to pin in the card:** pytest/vitest counts, CI run id, the deployed bundle
marker, and the smoke screenshots (light + dark) — same standard as M104/M105.

## Out of scope (stated so implementation cannot drift into them)

- **Auto-splitting heuristics** — receipts-triggered or LLM-suggested splits are the
  receipt-capture card's territory; v1 is a user action only.
- **Splits on investment accounts** (§13) and anything touching `trades`, `acb_pools`,
  docs/06.
- **Multi-currency children** — a child always carries the parent's `currency`;
  `currency` is a frozen identity field (§5).
- **Nested splits** (`split_nested`) and child-level deletion.
- **YNAB `subtransactions` import** — stays dropped with `split_detail_dropped`.
- Per-child dates/posting dates, split-specific reports, a ⌘K entry, drag-to-reorder.
