# 58 — File importers: YNAB · Monarch · Quicken · ProjectionLab · CSV / net-worth spreadsheets

## Context

Kilter adoption: existing budgeting and net-worth data comes in **by file** — there is no
bank sync and there will not be one. fin_tracker already carries most of that story:

- `docs/03` §QIF/OFX — QFX/OFX (banks) and QIF (Quicken, YNAB, Tiller, spreadsheets) parse
  into staged rows through `POST /imports/preview → /upload → /commit`.
- `docs/21` — the column-mapping contract: `POST /imports/suggest-profile` **suggests**
  (LLM → heuristic) and `POST /imports/preview` validates; nothing is ever written before
  `POST /imports/commit`.
- `docs/38` — YNAB migration: one export file → full plan (`/imports/ynab/preview`) →
  `/imports/ynab/commit`.

Two gaps remain for this adoption:

1. **Format awareness in the mapping wizard.** `suggest-profile` answers with a generic
   mapping for every file. A Monarch export (`date, merchant, amount, category, account,
   notes, tags`, expenses negative) deserves a named, deterministic suggestion the user
   confirms — not a guess.
2. **No net-worth file import at all.** ProjectionLab exports a JSON plan
   (`savingsAccounts / investmentAccounts / debts / assets / progress`), and every budgeting
   tool lets the user export a balance spreadsheet (`Account, Balance`). fin_tracker has no
   route that turns either into accounts, so a ProjectionLab user's net worth cannot be
   brought over.

This doc pins both additions. Everything below reuses the existing gates: zero writes
before a confirmed commit, `{detail, code}` envelopes, manual-data-wins, tenancy, E2EE
fail-closed.

## Approach

### A. `format` on `POST /imports/suggest-profile` — format-aware mapping suggestions

`SuggestProfileResponse` gains one field:

```
format: str | None   # slug of the known export format that resolved this mapping, else null
```

Precedence inside `suggest_column_mapping` (docs/21 §6 order is otherwise unchanged):

```
1. app.importers.format_presets.detect(headers)   → preset (deterministic, confidence 0.95)
2. LLM backend reachable                          → heuristic (confidence 0.6)
3. heuristic                                      → heuristic (confidence 0.5)
```

A preset **never** bypasses the confirmation gate: the wizard still shows the mapping and
`POST /imports/upload` still refuses a profile that was never confirmed (docs/21 §4).

**`app/importers/format_presets.py`** — a preset is:

```python
@dataclass(frozen=True)
class FormatPreset:
    slug: str                       # "monarch"
    label: str                      # "Monarch Money export"
    columns: dict[str, tuple[str, ...]]   # logical field -> acceptable header names, ordered
    date_format_from_samples: bool = True # infer %d/%m/%Y order from the file's dates
    credits_positive: bool | None = None  # None = infer from the amounts
    confidence: float = 0.95
```

`detect(headers)` resolves each logical field against the file's header row case- insensitively (header normalization: lowercase, strip a trailing `(…)` suffix, trim — the
same normalization YNAB's parser uses), and returns the preset only if every **required**
field resolves.

**Monarch preset (the only pinned transaction format this card claims):**

| logical field | accepted headers (first hit wins)   | required |
|---------------|-------------------------------------|----------|
| date          | `date`                              | yes |
| amount        | `amount`                            | yes |
| description   | `merchant`, `original statement`, `description` | yes |
| account       | `account`, `account name`           | no  |
| category      | `category`, `category name`         | no  |
| payee         | `merchant` (also description)       | no  |
| memo          | `notes`, `memo`, `original statement` | no |
| tags          | `tags`                              | no  |

Detection additionally requires `date` + `amount` **and** at least one of
`merchant` / `original statement` — so an ordinary bank CSV (`date, description, amount`)
is not claimed as Monarch. `credits_positive=True` (Monarch exports expenses as negative
numbers); `date_format` is inferred from the file's samples because Monarch export date
order is locale-dependent.

Quicken's CSV register layout is **not** published in a stable, citable form, so this card
claims **no** Quicken CSV preset: Quicken is served by QFX/OFX and QIF (docs/03 §QIF/OFX,
fixtures `test_sample.qif`, `test_import.qif`, `sample.qfx`, `sample.ofx`) and any Quicken
CSV reaches staging through the generic wizard, which already detects its split
(`Inflow`/`Outflow`) columns heuristically. Guessing headers for an undocumented layout
would be a silent-mapping risk, which docs/21 §7 forbids.

**Parentheses in generic amounts.** `_parse_magnitude` (docs/21 §1) accepts `US$`, thousands
separators and decimal commas but not accounting parentheses, which hand-edited and some
bank exports use: `(58.12)` → `-58.12` before the `credits_positive` convention is applied.
The existing convention logic is untouched; only the magnitude parser grows the `(…)` form.

### B. Net-worth file import — `POST /imports/networth/preview` → `/imports/networth/commit`

Two routes on the existing `imports` router (registered **before** the `/{batch_id}` routes,
the ordering lesson of docs/38 §2):

```
POST /api/v1/imports/networth/preview   multipart: file[, currency][, account_name]
POST /api/v1/imports/networth/commit    identical form
```

`preview` maps and returns the full plan; it writes nothing and may be run repeatedly.
`commit` maps **and applies in one transaction**. Both enforce, in order (docs/03 §L6):
rate limit (`IMPORT_UPLOAD_LIMIT` for preview, `IMPORT_MUTATE_LIMIT` for commit) →
`Content-Length` 413 → byte cap 5 MB → parse → row cap (`MAX_NETWORTH_ROWS = 1000`,
413 `networth_row_cap`) → detection.

**File kinds and detection** (first non-whitespace byte decides):

1. **ProjectionLab JSON** — the object shape of ProjectionLab's data export (as consumed by
   community tooling; fields below are the ones read, everything else is ignored):
   `today.savingsAccounts[]`, `today.investmentAccounts[]`, `today.debts[]`,
   `today.assets[]`, `progress[]`, `meta`, `plans[]`. Detection: valid JSON **and** at
   least one of `today.savingsAccounts` / `today.investmentAccounts` / `today.debts`
   present. Otherwise → `422 unsupported_networth_format`.
2. **Balance CSV** — header sniff: needs an account column
   (`account`, `account name`, `name`) **or** the form field `account_name`, and a balance
   column (`balance`, `value`, `amount`, `net worth`, `total`). Optional `date`
   (history: latest row per account wins, older rows collapsed into a note) and optional
   `type`/`currency` columns. Missing either half → `422 networth_missing_columns`,
   naming the columns it looked for.

**Form fields:** `file` (required), `currency` (3-letter, default `CAD`), `account_name`
(label for a single-account file whose CSV has no account column).

**Row currency (balance CSV):** a non-empty `currency` cell follows the **same rule as the
form field** — stripped, upper-cased, and exactly 3 letters (`^[A-Za-z]{3}$`, the contract
`POST /accounts` enforces on `AccountCreate.currency`). An empty cell means "no opinion":
the row falls back to the form's validated `currency`. Any other value (e.g.
`CANADIAN DOLLARS`, unbounded length) → `422 networth_row_invalid` with **nothing written**,
on **preview and commit alike** (both parse the file; `build_plan` re-checks every row as
defense in depth). A junk code can therefore never reach `accounts.currency`, where it
would 422 every fx lookup and the validated PATCH path could not repair it (security S-1).

**Plan shape** (Decimal as string — docs/01):

```json
{
  "source_format": "projectionlab_json | balance_csv",
  "file_name": "projectionlab_export.json",
  "currency": "CAD",
  "counts": {
    "accounts": 7, "accounts_create": 4, "accounts_reuse": 2, "accounts_unmapped": 1,
    "balance_rows_collapsed": 3, "progress_snapshots_skipped": 12, "assets_skipped": 2
  },
  "accounts": [
    {"name": "Everyday", "source_type": "savings", "mapped_type": "savings",
     "action": "create | reuse | unmapped", "existing_id": null,
     "file_balance": "2411.27", "current_balance": null, "currency": "CAD",
     "reason": null, "notes": ["balance at 2026-02-03; 3 older balances collapsed"]}
  ],
  "skipped":  [{"kind": "asset | plan_data | progress_history", "name": "House",
                "reason": "…"}],
  "warnings": [{"code": "conflicting_balance", "detail": "…"}],
  "can_commit": true
}
```

`skipped` is never truncated (AGENTS §3: flag, never skip silently).

**Account creation semantics (commit):**

- `starting_balance` = the account's **latest** balance in the file; `created_at` =
  `now()` (docs/14: the app stamps it; never a row's own timestamp).
- `taxable` is server-derived from `type` (docs/14 C1) — never read from the file.
- **Reuse, never overwrite:** an existing account with the same name (case-insensitive,
  trimmed — the YNAB rule) is reused: `action=reuse`, its balance, type and currency are
  untouched. `current_balance` in the plan is that account's native balance
  (starting balance + committed transactions, per the committed predicate in
  `services/reports.py::net_worth_report`) purely so the reviewer can compare it against
  `file_balance`. A mismatch is a note, never a write — manual data wins (docs/01 §4).
- ProjectionLab type mapping (unmapped → `422`-free `unmapped` row with a reason):

  | source | fin_tracker |
  |--------|-------------|
  | `savingsAccounts.type` matches an `AccountType` token | that type |
  | `savingsAccounts.type` otherwise | `savings` |
  | `investmentAccounts.type`: `taxable` | `brokerage_non_registered` |
  | `401k`, `traditional-ira` | `rrsp` |
  | `roth-ira` | `tfsa` |
  | `529` | `resp` |
  | `hsa` (no Canadian equivalent) | `brokerage_non_registered` + note |
  | any other investment type | `brokerage_non_registered` + note (a taxable import never hides a gain) |
  | `debts.subtype`: `credit-card` | `credit_card` |
  | `debts.subtype` otherwise (mortgage, auto, student, …) | `line_of_credit` |
- **Skipped, with reasons:** ProjectionLab `assets` (real estate / vehicles — fin_tracker
  has no asset account type, and an asset's `balance` is its *loan* balance when financed,
  so writing one would invent money), `progress[]` (dated net-worth history — fin_tracker
  has no snapshot table; one balance per account is imported), and `plans[]`
  income/expense/milestone planning data. Each appears in `skipped` with its own reason.
- Duplicate/conflicting balances for one account: the **latest dated** row wins; same-date
  conflicts keep the first row and add a `conflicting_balance` warning (never silent).

**Guards:** `file` byte cap 5 MB (413 `file_too_large`), row cap (413 `networth_row_cap`),
E2EE tenants refused on **commit** with the byte-identical shared server-side phrase
(docs/38 §8; preview is read-only and runs), demo tenant blocked by the standard auth
dependency. A commit with nothing to create still succeeds (`created=0, reused=N`) so
re-importing the same file is idempotent — the merge-review proof.

### C. What this card does NOT do

- No bank sync, no Plaid/E2EE connectivity — file only (Kilter scope).
- No `Debt` rows, no `NetWorthSnapshot` history, no `ProgressSnapshot` table: gaps B
  documents as `skipped` items, candidates for their own cards.
- No Quicken CSV preset (see A).
- `suggest-profile`'s LLM path is untouched; the preset only short-circuits it when the
  file is a format we claim to know.

## Touch points

| file | change |
|------|--------|
| `backend/app/importers/format_presets.py` | **new** — `FormatPreset`, `MONARCH`, `detect()` |
| `backend/app/importers/csv_profile.py` | `_parse_magnitude`: accounting parentheses |
| `backend/app/services/column_mapping.py` | preset short-circuit in `suggest_column_mapping` |
| `backend/app/schemas/imports.py` | `SuggestProfileResponse.format`; net-worth plan schemas |
| `backend/app/services/networth_import.py` | **new** — detect → parse → plan → apply |
| `backend/app/api/imports.py` | two routes above `/{batch_id}` |
| `backend/tests/fixtures/monarch_transactions.csv` | **new** (synthetic merchants) |
| `backend/tests/fixtures/projectionlab_export.json` | **new** (synthetic accounts) |
| `backend/tests/fixtures/networth_spreadsheet.csv` | **new** (synthetic balances) |
| `backend/tests/test_file_import_formats.py` | **new** — one round-trip per format; per-row currency pins (security S-1) |
| `backend/tests/test_mapping_confirmation_gate.py` | **new** — no write before confirm |
| `frontend/src/api/networth.ts` | **new** — `previewNetWorth` / `commitNetWorth` clients |
| `frontend/src/components/imports/NetWorthImportCard.tsx` | **new** — preview-first card: pick file → plan → Import (the only write); enrolment gate per docs/33 rule 7d |
| `frontend/src/pages/Imports.tsx` | mounts the card after `YnabMigrationCard` |
| `frontend/src/components/e2ee/ServerSideBlocked.tsx` | `import:networth` capability copy (byte-identical to the server phrase) |
| `frontend/src/api/imports.ts` | `SuggestProfile.format` type |
| `frontend/tests/networthImport.test.tsx` | **new** — preview writes nothing, plan surfaces, commit required, enrolled tenant sees no commit control |
| `docs/03` §QIF/OFX + fixtures table | supported-formats row + `format` field |
| `docs/08` §Imports | suggest-profile `format` row; networth preview/commit rows |
| `docs/10` | milestone `M190` |
| `docs/21` §5–§7 | `format` on suggest, preset precedence, parentheses rule |

## Verification

Gates (AGENTS §4): `ruff format --check && ruff check`, `pytest -q` against real Postgres,
`npx tsc --noEmit && npx eslint . --max-warnings 0`, `npm run build`, docker rebuild,
running-stack smoke on `http://localhost:8080`, then commit.

Behavioural assertions (each is a test in the files above):

1. **Round-trip per format** — fixture → import → committed rows/accounts for the test's
   own tenant → re-upload → `net_new == 0` (or `created == 0`): YNAB (`ynab_budget.json`),
   Quicken (`sample.qif`), Monarch (`monarch_transactions.csv`), ProjectionLab
   (`projectionlab_export.json`), generic CSV (`csv_decimal_comma.csv`), net-worth
   spreadsheet (`networth_spreadsheet.csv`). QFX/OFX round trips stay covered by the
   pre-existing `tests/test_import_preview.py` / `tests/test_imports.py` fixtures
   (`sample.qfx`, `sample.ofx`) and run in the same full-suite gate.
2. **Mapping confirmation gate** — `suggest-profile` on the Monarch fixture returns
   `format == "monarch"` and writes nothing (row counts identical before/after);
   `POST /imports/upload` **without** `profile_json` is 422 `missing_importer_profile`
   with row counts unchanged; with the confirmed profile it stages; live `GET /transactions`
   shows nothing until `commit`.
3. **Net-worth merge review** — preview writes nothing; commit creates the accounts;
   re-commit of the same file creates 0 and does not touch any balance; an existing account
   matched by name keeps its own balance (`current_balance` ≠ `file_balance` is reported,
   not applied).
4. **Never silent** — ProjectionLab assets/progress/plans and collapsed balance history all
   appear in `skipped` with reasons; an unsupported JSON shape is
   `422 unsupported_networth_format`, not an empty plan.
5. **Per-row currency** — a balance CSV whose `currency` cell is not a 3-letter code
   (e.g. `CANADIAN DOLLARS`) is `422 networth_row_invalid` on **preview and commit**, with
   zero accounts written — not even the rows whose own currency was valid; a valid cell is
   stripped/upper-cased (` usd ` → `USD`) and an empty cell falls back to the form's
   `currency` (`test_networth_junk_row_currency_is_a_named_422_and_writes_nothing`,
   `test_networth_row_currency_is_normalized_and_falls_back_to_the_form_value` in
   `tests/test_file_import_formats.py`).
