# 17 — Account Detail, Sidebar & Transfer Suggestions

Per-account sidebar navigation grouped by type, a dedicated account detail page
(Bank vs Investment view), a running-balance ledger, editable starting balance,
and transfer-pair detection/suggestion/accept across accounts. Companion to
docs/14 (accounts & institutions) and docs/19 (transaction CRUD). Covers the
implemented M20 contract; canonical endpoint rows also appear in docs/08.

## Context

Users need to drill from the sidebar into a single account and see its activity
in context: a running cash balance (not just the latest total), the ability to
set a starting balance that predates imported history, and a way to mark
movements of money between two of their own accounts as *transfers* so they
net to zero and stop double-counting income/spending.

Decisions (locked with code):
- **Starting balance is first-class.** `accounts.starting_balance` seeds the
  running ledger and is part of `AccountPublic`; it is editable via
  `PATCH /accounts/{id}`. It represents the cash position before the earliest
  recorded transaction.
- **Ledger is derived, not stored.** `GET /accounts/ledger` walks committed /
  manual transactions in `(date, id)` order, accumulating `amount_native` from
  `starting_balance`. No new table.
- **Transfers are pairs.** Two transactions in *different* accounts with
  opposite `amount_native` are linked via `transactions.transfer_pair_id` and
  assigned the system `transfers` category (`is_system == True`). Linked pairs
  are excluded from income/expense totals.
- **Suggestions are heuristic, acceptance is explicit.** The backend proposes
  candidate pairs (opposite amount, ±10 days); the client calls
  `accept-transfer-suggestion` to actually pair them.

## Approach

### Data model

`backend/app/models/accounts.py` — `Account`
- `starting_balance: Decimal | None = Field(default=None, sa_type=Numeric(18, 2))`
  — nullable; migrations `0009_add_starting_balance`. Server never auto-fills it.
- `cash_balance` is **not** a column. It is derived:
  `cash_balance = starting_balance (+0 if null) + Σ amount_native` over the
  committed-or-manual transactions for the account (`_committed_cash_subquery`,
  `_build_public` in `app/api/accounts.py`).
- `taxable` is server-derived: `True` iff `type == brokerage_non_registered`
  (`create_account` / `update_account`); clients cannot set it directly.

`backend/app/models/transactions.py` — `Transaction`
- `transfer_pair_id: int | None = Field(default=None, foreign_key="transactions.id")`
  — self-referential FK. Added in migration **`0008_add_transfer_pair_id`**; the
  `ON DELETE SET NULL` behavior was added separately in migration
  **`0010_transfer_pair_on_delete_set_null`** (deleting one side of a pair nulls
  the other's reference rather than failing the FK). Indexed
  (`ix_transactions_transfer_pair_id`).
- `dedup_hash` UNIQUE per `(account_id, dedup_hash)` — manual edits that change
  identity recompute it and reject collisions with `duplicate_transaction`.

`backend/app/models/categories.py` — `Category`
- The `transfers` category is a **system** category (`is_system == True`,
  `name == "transfers"`). Transfer pairing requires it to exist; if a user
  deleted it, pairing endpoints raise `transfers_category_not_found`.

### Backend

All routes are under `GET /api/v1/...` (router prefixes `/accounts`,
`/transactions`). Auth: `get_current_user`; every account/transaction is
tenant-scoped via `owned_or_404`.

#### Accounts (read + starting balance)

`app/api/accounts.py`
- `GET /accounts` → `AccountPage`
  `{ items: AccountPublic[], total: int, limit: int, offset: int }`.
  `AccountPublic` (below) is returned for every account; `starting_balance`,
  `cash_balance`, `investment_value`, `total_value` all included.
- `GET /accounts/{account_id}` → `AccountPublic`.
- `PATCH /accounts/{account_id}` → `AccountPublic`.
  Body `AccountUpdate` (all optional, `extra="forbid"`):
  `name: str|None (min_length=1)`, `type: AccountType|None`,
  `currency: str|None` (exactly 3 uppercase chars `^[A-Z]{3}$`, `to_upper=True`),
  `institution_id: int|None`, `llm_autopilot: bool|None`,
  `starting_balance: Decimal|None`. On every update `taxable` is re-derived from
  `type`. `account_in_use` (409) is raised by `DELETE /accounts/{id}` when the
  account still has transactions (unrelated to PATCH).
- `GET /accounts/ledger?account_id=<int>` → `LedgerPage`
  `{ items: LedgerEntry[], starting_balance: str, current_balance: str, total: int }`.
  - Scope: only `manual` transactions and `committed`-batch transactions
    (`_live_scope`); pending-review rows are excluded. `account_id` is required.
  - Items ordered by `(date, id)` ascending.
  - `running_balance` for the first row = `starting_balance + amount_native`;
    each subsequent row accumulates `amount_native` in order. So
    `current_balance` = `starting_balance + Σ amount_native` and equals the
    account's derived `cash_balance`.
  - `LedgerEntry`: `transaction_id: int`, `date: date`,
    `description: str` (= `normalized_payee or raw_description`), `amount: str`
    (2-dp), `running_balance: str` (2-dp). All monetary fields are strings
    quantized to 2 decimals (`_format_cash`).

`AccountPublic` (`app/schemas/accounts.py`):
`id: int`, `institution_id: int|None`, `name: str`, `type: AccountType`,
`currency: str`, `taxable: bool`, `llm_autopilot: bool`,
`cash_balance: str | None` (M145: KIND-dependent — cash accounts the committed
ledger + starting_balance; investment accounts the docs/05 trade-chain cash the
value chart draws; `null` = no honest figure, rendered as a dash),
`investment_value: str` (market value of holdings, native currency; `"0.00"` if
none), `total_value: str | None` (`cash_balance + investment_value`, native
currency; `null` exactly when `cash_balance` is), `starting_balance: str`.

#### Transfer suggestions & pairing

`app/api/transactions.py`
- `GET /transactions/transfer-suggestions?account_id=<int|None>` →
  `TransferSuggestionPage { items: TransferSuggestion[] }`.
  - Finds unpaired (`transfer_pair_id IS NULL`), live (manual/committed)
    transactions whose `amount_native` has an opposite-amount counterpart in a
    **different** account within **±10 days**.
  - `account_id` (optional) filters the *source* side to that account.
  - Each pair is emitted once (deduped by `(min_id, max_id)`).
  - `TransferSuggestion`: `source_id: int`, `source_account_id: int`,
    `source_account_name: str|None`, `source_amount: str`, `source_date: str`,
    `source_payee: str`, `candidate_id: int`, `candidate_account_id: int`,
    `candidate_account_name: str|None`, `candidate_amount: str`,
    `candidate_date: str`, `candidate_payee: str`. All amounts/dates are strings.
- `POST /transactions/accept-transfer-suggestion` → `TransactionPublic` (the
  source, refreshed).
  Body `AcceptTransferSuggestionPayload` (`extra="forbid"`):
  `{ source_id: int, candidate_id: int }`.
  - Sets both rows' `category_id` to the system `transfers` category and sets
    `source.transfer_pair_id = candidate.id`, `candidate.transfer_pair_id =
    source.id`. Both must be live (`_ensure_live`).
  - Errors: `already_paired` (409) if either side already has a
    `transfer_pair_id`; `transfers_category_not_found` (404) if the system
    `transfers` category is missing; `not_found` (404) for unknown ids;
    `transaction_in_pending_batch` (409) if either row is in a pending batch.
- `POST /transactions/{transaction_id}/detect-transfer` → `TransactionPublic`.
  - Auto-pairs the transaction with another live transaction in a **different**
    account whose `amount_native == -this.amount_native`, within **±3 days**,
    already categorized as the system `transfers` category, and not yet paired.
  - Errors: `transfers_category_not_found` (404) if the system `transfers`
    category is missing; `no_matching_transfer` (404) when no candidate matches;
    `transaction_in_pending_batch` (409) if the source is in a pending batch;
    `not_found` (404) for an unknown id.
- `POST /transactions/{transaction_id}/link-transfer` → `TransactionPublic`.
  Body `LinkTransferPayload` (`extra="forbid"`): `{ pair_id: int }`.
  - Manually links the two transactions as a pair (both get each other's id in
    `transfer_pair_id`). Any pre-existing pair on either side is unlinked first
    (its `transfer_pair_id` set to `NULL`). Both rows must be live.
  - Errors: `transaction_in_pending_batch` (409) if either side is pending;
    `not_found` (404) for unknown ids.
- `POST /transactions/{transaction_id}/unlink-transfer` → `TransactionPublic`.
  - Nulls `transfer_pair_id` on both this transaction and its counterpart (if
    any). No body. Must be live (`transaction_in_pending_batch` 409 if pending).
- `PATCH/DELETE /transactions/{id}` (full contract in docs/19) raise
  `transaction_in_pending_batch` (409) when the row belongs to a pending import
  batch (`_ensure_live`); `DELETE` also deletes `LlmSuggestion` rows and sets a
  counterpart's `transfer_pair_id` to `NULL` (FK `SET NULL`).

`TransactionPublic` includes `transfer_pair_id: int|None` and
`pair_account_name: str|None` (name of the paired account, useful for the UI).

#### Error codes (this doc's surface)

| code | HTTP | raised by |
|------|------|-----------|
| `already_paired` | 409 | accept-transfer-suggestion (either side already paired) |
| `transfers_category_not_found` | 404 | accept / detect-transfer (system `transfers` category missing) |
| `no_matching_transfer` | 404 | detect-transfer (no candidate within ±3d) |
| `transaction_in_pending_batch` | 409 | accept / detect / link / unlink / PATCH / DELETE when row is pending-review |
| `duplicate_transaction` | 409 | PATCH changing identity to a colliding dedup_hash |
| `not_found` | 404 | unknown account/transaction id (generic `owned_or_404`) |
| `validation_error` | 422 | PATCH null `account_id`/`raw_description`; bad `category_id` filter |

### Frontend

`frontend/src/components/layout/AppShell.tsx`
- `TYPE_GROUPS` partitions accounts into four sidebar sections:
  - **Cash** — `chequing`, `savings`
  - **Credit** — `credit_card`, `line_of_credit`
  - **Registered** — `rrsp`, `tfsa`, `fhsa`, `resp`, `lira`, `rdsp`
  - **Non-registered** — `brokerage_non_registered`
- Each account renders a `NavLink` to `/accounts/${account.id}`; the active pill
  uses `layoutId` morph. Accounts are loaded via `listAccounts()` and filtered
  into groups by `type`.

`frontend/src/pages/AccountDetail.tsx`
- `AccountDetail()` reads `:accountId`, fetches `getAccount(id)`, then dispatches:
  - `type ∈ INVESTMENT_ACCOUNT_TYPES` → `<InvestmentAccountView>` (reuses the
    `Investments` page components: holdings, allocation, trades, watchlist, cash
    activity).
  - otherwise → `<BankAccountView>`.
- `BankAccountView({ account })`:
  - `getAccountLedger(id)` → renders a running-balance **AreaChart**
    (`recharts` `AreaChart`/`Area`) of `running_balance` over time.
  - Editable starting balance: toggles an input, calls
    `updateAccount(id, { starting_balance })`; on success invalidates the
    `accounts`, `accounts/{id}`, and `ledger/{id}` queries.
  - `getTransferSuggestions(id)` → renders `<TransferSuggestions>` (accept
    pairs inline; see docs/19 TransactionEditor for the shared editor used to
    add/edit/delete transactions from this view).
  - `AccountActivity` lists filterable transactions (date range / payee / category).
- `InvestmentAccountView` reuses the investment surfaces; no separate ledger
  chart (positions valued via docs/05).

`frontend/src/components/transactions/TransferSuggestions.tsx` — the panel
itself (**M112**, the panel-layout fix; see the label note below)
- **One grid, one shared track.** Both sides of a pair render inside ONE
  `div.grid.min-w-0.flex-1.grid-cols-1`, so every amount / date / account /
  payee line of BOTH rows starts at the same x. The two self-contained inline
  `flex` rows this replaced staggered into a staircase, wrapped mid-label,
  and pinned the card at 374 px wide at every viewport from 320 to 420 (14 px
  past a 360 px screen).
- **Fixed line order, tagged cells.** Each side renders, in order: `amount`
  (`formatMoney` + `tabular-nums`), `date`, `account` (parenthesized; no cell
  when `null`), `payee`. Every line carries `data-side="source|candidate"`
  and `data-cell="amount|date|account|payee"` — the hooks
  `tests/transferSuggestionsLayout.test.tsx` and
  `scripts/a11y/transfer_suggestions_probe.py` key on (the probe reads those
  roles, never className position).
- **Every line is `truncate`.** `overflow:hidden`, so a pathological value
  clips instead of breaking mid-label ("(CIBC" / "Chequing)"), painting the
  date on 2 lines and the account on up to 7, or shoving the card wider than
  its container.
- **A `"-"` or blank payee renders NO cell.** `payee.trim()` equal to `""` or
  `"-"` omits the payee span entirely — `"-"` is a real value from the data
  meaning "no description", and printing it was the stray truncated dash line
  above the amount.
- **Action buttons (check / X) are a `shrink-0` flex SIBLING of the grid**,
  never children of it, so no amount of content can put them on top of text.
- **Deviation, measured (AGENTS §3):** side-by-side `amount | date | account`
  columns were measured OUT, not assumed — the probe put the content box at
  **92 / 128 / 158 / 188 px** (320 / 360 / 390 / 420; **160 px** in the
  desktop sidebar) against **294 px** needed (77 px amount + 82 px date +
  119 px a realistic account label + gaps): columns fit at **no** supported
  width. The same measurement sits in the component comment.

**Label note (M-number).** The fix's two commits — `8330033`
(`fix(M110) …`) and `f75828c` (`test(M110) …`) — carry the WRONG milestone
number: docs/10's **M110 is the budget rollover**, and **M111** was taken by
`5dfbbd4` (import-wizard profile columns). This work is **M112** (next free
number), recorded here and in docs/09; do not propagate `M110` for
transfer-suggestion layout work.

`frontend/src/api/accounts.ts`
- `INVESTMENT_ACCOUNT_TYPES` (mirror of backend frozenset), `getAccountLedger`,
  `updateAccount`, `listAccounts`, `AccountPublic`/`AccountUpdateInput` with
  `starting_balance`, `LedgerPage`/`LedgerEntry`.

## Touch points

- `backend/app/models/accounts.py` — `Account.starting_balance`.
- `backend/app/models/transactions.py` — `Transaction.transfer_pair_id`.
- `backend/app/models/categories.py` — system `transfers` category.
- `backend/app/api/accounts.py` — `GET /accounts`, `GET /accounts/{id}`,
  `PATCH /accounts/{id}`, `GET /accounts/ledger`, `_build_public`,
  `_committed_cash_subquery`.
- `backend/app/api/transactions.py` — `GET /transactions/transfer-suggestions`,
  `POST /transactions/accept-transfer-suggestion`,
  `POST /transactions/{id}/detect-transfer`,
  `POST /transactions/{id}/link-transfer`,
  `POST /transactions/{id}/unlink-transfer`, `_ensure_live`, `_live_scope`.
- `backend/app/schemas/accounts.py` — `AccountPublic`, `AccountUpdate`,
  `LedgerEntry`, `LedgerPage`.
- `backend/app/schemas/transactions.py` — `TransferSuggestion`,
  `TransferSuggestionPage`, `AcceptTransferSuggestionPayload`,
  `LinkTransferPayload`, `TransactionPublic`.
- `backend/alembic/versions/0008_add_transfer_pair_id.py`,
  `0010_transfer_pair_on_delete_set_null.py`, `0009_add_starting_balance.py`.
- `frontend/src/components/layout/AppShell.tsx` — `TYPE_GROUPS`, `/accounts/:id`.
- `frontend/src/pages/AccountDetail.tsx` — dispatch + `BankAccountView`.
- `frontend/src/api/accounts.ts` — `getAccountLedger`, `updateAccount`,
  `INVESTMENT_ACCOUNT_TYPES`.
- `frontend/src/components/transactions/TransferSuggestions.tsx` — the panel's
  single shared grid track, `data-side`/`data-cell` tagging, `"-"`/blank payee
  fallback, `shrink-0` button sibling.
- `frontend/tests/transferSuggestionsLayout.test.tsx` (9 tests),
  `frontend/scripts/a11y/transfer_suggestions_probe.py` +
  `transfer-suggestions-probe-{before,after}.json` (evidence).

## Verification

- **Starting balance seeds ledger.** Create account `A` (chequing, CAD) with
  `starting_balance = 1000`. `GET /accounts/ledger?account_id=A` →
  `starting_balance == "1000.00"`, `items == []`, `current_balance == "1000.00"`.
  `GET /accounts/A` → `cash_balance == "1000.00"`, `total_value == "1000.00"`.
- **Running balance accumulates.** Add manual txns: `+250` on 2026-01-02,
  `-100` on 2026-01-05 (docs/19). Re-fetch ledger → items in
  `(date, id)` order; running balances `1250.00`, `1150.00`;
  `current_balance == "1150.00"` == `cash_balance`.
- **Sidebar grouping.** Seed one chequing + one rrsp + one credit_card account;
  `AppShell` renders three `TYPE_GROUPS` sections (Cash / Registered / Credit)
  with `NavLink`s to `/accounts/<id>`; clicking opens `AccountDetail`.
- **Investment dispatch.** Open a `brokerage_non_registered` account →
  `InvestmentAccountView` (no bank ledger chart). Open a chequing account →
  `BankAccountView` with AreaChart.
- **Transfer suggestion + accept.** Create txn `+500` in account A on 2026-02-01
  and txn `-500` in account B on 2026-02-03 (different accounts, ±10d).
  `GET /transactions/transfer-suggestions` → one `TransferSuggestion` with
  `source_id`/`candidate_id` cross-referencing A and B.
  `POST /transactions/accept-transfer-suggestion {source_id, candidate_id}` →
  200; both rows now have `transfer_pair_id` set and `category_id` ==
  system `transfers`. Re-query suggestions → pair no longer suggested
  (`already_paired` if retried: 409).
- **Detect-transfer (±3d).** With a txn already categorized `transfers` in
  account B (`-500`, 2026-02-03), `POST /transactions/A/detect-transfer` for the
  `+500` txn in A → 200, pairs them. A candidate >3 days away →
  `no_matching_transfer` (404).
- **Link / unlink.** `POST /transactions/A/link-transfer {pair_id: B}` → 200,
  both `transfer_pair_id` set; `POST /transactions/A/unlink-transfer` → 200,
  both `transfer_pair_id` null.
- **Pending-batch guard.** A transaction inside a `pending_review` import batch
  cannot be linked/unlinked/patched/deleted → `transaction_in_pending_batch`
  (409).
- **Delete cascade.** Deleting one side of a paired (committed) transaction
  leaves the other's `transfer_pair_id == null` (FK `SET NULL`, migration 0010),
  no FK violation.
- **Editable starting balance round-trips.** `PATCH /accounts/A
  {starting_balance: 2000}` → `starting_balance == "2000.00"`; ledger
  `starting_balance` and `current_balance` shift by +1000.
- **Rejected shapes.** `PATCH /accounts/A {currency: "usd"}` → 422
  (must be 3 uppercase); `PATCH /transactions/X {account_id: null}` → 422
  `validation_error`.
- **Transfer-suggestion pair layout (M112).** Unit —
  `frontend/tests/transferSuggestionsLayout.test.tsx` **9/9**: one grid holds
  both rows, a missing account keeps the track, the candidate row keeps its
  own gap, a `"-"`/blank payee renders no cell on either side, a real payee is
  kept, a long account ellipsizes instead of wrapping, every line stays on
  the clipped track (nothing overflows the card), the action buttons stay a
  `shrink-0` sibling of the grid, and the empty state renders.
  Browser — `frontend/scripts/a11y/transfer_suggestions_probe.py` against the
  deployed origin at **320 / 360 / 390 / 420 / 1440**: **78/153 → 154/154**
  (committed evidence
  `frontend/scripts/a11y/transfer-suggestions-probe-{before,after}.json`,
  each carrying its full `checks` array, plus before/after screenshots per
  width). The 75 before-failures: `flex` shape
  not `grid` ×10, date column staggered (5.0–5.1 px) ×10, account column
  staggered (1.8–5.2 px) ×10, date painted on 2 lines ×20, account painted on
  2–7 lines ×20, stray `-` payee line ×5. Both runs: M82 text-node overlap
  sweep **0** at every width, axe **0 critical/serious** at 390, console
  errors **0**.

## Notes / discrepancies vs plan

- The plan text referenced `transactions.transfer_pair_id` `ON DELETE SET NULL`
  as "(0008)". In code the FK column + index ship in **`0008_add_transfer_pair_id`**,
  but the `ON DELETE SET NULL` clause is a **separate migration
  `0010_transfer_pair_on_delete_set_null`**. Documented here as the actual split.
- `detect-transfer` matches candidates that are **already** in the system
  `transfers` category (code: `Transaction.category_id == transfers_category.id`),
  not any unpaired opposite-amount row — this is the implemented behavior and is
  documented faithfully above.
