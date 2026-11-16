# 03 — Ingestion

## Importer interface

Every importer implements:

```python
class Importer(Protocol):
    @staticmethod
    def detect(raw: bytes) -> bool: ...
    @staticmethod
    def parse(raw: bytes, account: Account) -> list[RawTransaction]: ...
```

`detect` sniffs the byte stream (OFX/QFX header vs. a QIF section header vs. parseable CSV with a known profile). `parse` returns `RawTransaction` rows that the batch pipeline converts into `transactions`. A registry (`backend/app/importers/registry.py`) maps format → importer; a future SnapTrade/Plaid integration implements the same interface but is explicitly out of scope for v1.

Bank sync (M79, docs/39) does **not** implement this byte-oriented `Importer` interface — a provider
pushes `NormalizedTx` rows instead of a file, and the YNAB migration (docs/38) carries its own plan
bindings. Both enter the pipeline one step later, at the shared staging function below, which is what
makes every source behave identically.

The concrete adapters are `OfxImporter` (in `app/importers/ofx.py`, backed by `ofxparse`), `QifImporter` (`app/importers/qif.py`, a hand-rolled line parser) and `CsvImporter` (`app/importers/csv_profile.py` — there is no `app/importers/csv.py`); the registry maps `"ofx"→OfxImporter`, `"qif"→QifImporter` and `"csv"→CsvImporter`, in that detect order.

### Detect order (why no sniffer steals another format's file)

`ofx` → `qif` → `csv`, fixed in `registry._FORMATS`:

1. **OFX/QFX** — `OFXHEADER` or `<OFX>` in the first 2 KiB. QFX is Quicken's branded OFX, so it reports `format="ofx"`: one parser, one detect path.
2. **QIF** — a `!Type:` / `!Option:` / `!Account` / `!Mem` section-header line in the first 8 KiB. A QIF's first line never carries a comma, so the CSV sniffer below cannot claim it.
3. **CSV** — the first line contains a comma, and it explicitly rejects OFX bytes.

`RawTransaction` is the parser-neutral output contract (`backend/app/importers/base.py`); the batch pipeline consumes it:

```python
@dataclass(frozen=True)
class RawTransaction:
    date: dt.date
    amount_native: Decimal          # signed, account currency; debits negative
    raw_description: str            # verbatim from the file
    currency: str | None = None     # None -> account.currency
    posting_date: dt.date | None = None
    external_ref: str | None = None # e.g. OFX FITID — debugging aid, not persisted
```

## Format support

| Format | Mechanism | Detected by |
| --- | --- | --- |
| OFX / QFX | `ofxparse` library; handles bank statements and credit cards | `OFXHEADER` / `<OFX>` in the first 2 KiB |
| QIF | Hand-rolled line parser (`app/importers/qif.py`) | `!Type:` section header in the first 8 KiB |
| Generic CSV | Per-institution column-mapping profile | comma in the first line (OFX bytes rejected) |

### QIF (M71)

QIF is line-oriented: a `!Type:` section header, then one record per transaction made of single-character field lines, terminated by `^` or a blank line. Consumed fields: `D` date, `T` signed amount, `P` payee, `M` memo, `L` category (payee fallback), `N` cheque/transaction number (kept as `external_ref` only). `A`/`C`/`S`/`E` are read and ignored — split records are accepted because Quicken still writes the signed total in `T`.

- **Signs**: `T` is taken verbatim and already signed (debits negative), matching the `RawTransaction` contract. QIF has no `credits_positive` toggle.
- **Currency**: QIF carries none, so `RawTransaction.currency` is `None` and the account's currency applies.
- **Date order**: no locale marker exists, so an ambiguous pair such as `05/06/2026` is read **month-first** (Quicken's convention). A component `> 12` settles the order unambiguously; ISO (`2026-06-05`) and `15-Mar-2026` are both accepted; two-digit years pivot at 70 (00–69 → 2000s, 70–99 → 1900s).
- **Sections**: `!Account` / `!Option` / `!Mem` hold no transactions and are skipped. `!Type:Invst` (and any other non-bank section) is **rejected**, not silently ignored — a file containing only those raises `422 qif_parse_error` naming the section and pointing at the trade-CSV path (docs/05).
- **Strictness** (AGENTS §3): a record with no date, no amount, an unparseable amount, or an unrecognised date raises `QifParseError` → `422 qif_parse_error` naming the record ordinal. No record is ever dropped.


### CSV mapping profiles

Stored as `institutions.importer_profile` JSON, mappable in the UI (Settings → import profiles):

- column name → field (`date`, `amount_native`, `raw_description`, ...)
- date format string
- decimal comma toggle
- amount sign convention (e.g. "credits positive" vs "debits negative")

Profile JSON (validated by `CsvProfile` on save; unknown keys rejected). `columns` MUST map at least `date` and `raw_description`. `amount_native` is required ONLY in **signed mode** (below). In **split mode** `columns` MUST map `debit` AND `credit` and MUST NOT include `amount_native` — including `amount_native` alongside `debit`/`credit` is auto-resolved by `_resolve_mode_conflict` (removes `amount_native`, preferring split mode); `_validate_mode` then passes. This handles LLM or stored profiles that return both modes.
- **Headerless CSVs** (`headerless: true`): when the uploaded CSV has no header
  row, set `headerless: true` and map `columns` by **1-based column position**
  (strings: `"1"`, `"2"`, …) instead of by header name. The parser then consumes
  every row as data via `csv.reader` (no header line is skipped). `CsvProfile`
  validates that every `columns` value is a positive integer when `headerless` is
  true, and a non-empty header name when it is false (`_validate_headerless`). A
  ragged/short row raises `CsvProfileError` → `422 parse_error`, exactly like the
  headerful path. Detection lives in `column_mapping._is_headerless`; the wizard
  renders positional `Column N` selectors and a manual "no header row" override.

```json
{
  "columns": {"date": "Trans. Date", "amount_native": "Amount", "raw_description": "Description"},
  "date_format": "%d/%m/%Y",
  "decimal_comma": true,
  "credits_positive": false
}
```

### Split-column profiles (debit/credit CSVs)

Bank CSVs often carry two amount columns instead of one signed column. The profile supports both modes — `columns.amount_native` present → **signed mode** (semantics above, incl. `credits_positive`); otherwise `columns.debit` AND `columns.credit` BOTH present → **split mode**: `amount_native = credit×credit_sign − debit×debit_sign` with configurable per-column signs via the OPTIONAL `signs` object (`{"debit": -1, "credit": 1}` — the default; values must be exactly ±1, invalid → 422 naming the key; ignored in signed mode). A row with both amount cells empty is SKIPPED entirely (not imported, not counted). Neither mapping → 422 naming the missing keys. `date_format`/`decimal_comma` apply to both modes.

Optional column mappings (BOTH modes): `posting_date` (parsed with `date_format`; blank → NULL) and `currency` (per-row token, uppercased; must match `[A-Z]{3}` else the row falls back to the account's currency — never dropped). Omit the key entirely when the file has no such column. A blank value (`""` or whitespace) on an OPTIONAL key is normalised to an absent key at validation time (`CsvProfile._drop_empty_optionals`) — the mapping wizard's "None" option serialises as `""`, and that must not poison an already-saved profile. REQUIRED and mode keys are never normalised: a blank there is a real gap and still 422s.

### CSV column-mapping wizard (LLM suggestions)

`POST /imports/suggest-profile` analyzes an uploaded CSV (multipart form field `file`) and returns a `SuggestProfileResponse` mapping the file's columns onto the profile fields:

- `columns`: `{date, raw_description, amount_native, debit, credit, posting_date, currency}`
- `date_format`, `decimal_comma`, `credits_positive`, `headerless`, `confidence` (0.0–1.0), `reasoning` (free text)
- `format`: slug of a recognised export format (`"monarch"` today) or `null` — set when `app/importers/format_presets.py` answered (docs/58 §A)

Resolution order: headerless file → positional mapping (offline); known export format (deterministic preset, `confidence=0.95`, **no LLM call**); LLM when one is configured; heuristic (header-keyword / date-format / decimal-comma detection) otherwise. Whatever answered, the mapping is still only a suggestion: the wizard shows it and `POST /imports/upload` refuses a profile that was never confirmed (`422 missing_importer_profile`). Errors: `422 not_csv` (filename not `.csv`), `413 file_too_large` (> 5 MB), plus the shared upload caps `422 csv_line_too_long` / `422 csv_too_complex` / `422 csv_parse_error` (see **Upload validation order** below) and `429 rate_limited` (see **Rate limits**). The confirmed profile drives the `CsvProfile` used by `POST /imports/upload` (docs/21).

**Session discipline (docs/07 §Provider-wait ledger).** `suggest_profile` releases its
request session (`services/session_release.py::release_before_provider_wait`, called
inside the route's `core()` right after `build_provider`) BEFORE the 60 s `chat()` in
`services/column_mapping.py::_llm_suggest`. The route writes nothing, so nothing of the
caller's is pending at that release; the response is exactly the mapping the LLM (or the
heuristic fallback) produced. Without it the auth read sits `idle in transaction` for the
whole wait — one pinned connection-pool slot per concurrent upload (M148's shape).
Verification: `backend/tests/test_llm_session_release.py` drives this route against a
one-slot pool with a `chat()` blocked on an event and issues a second authenticated read
while it waits — **RED before the fix with `QueuePool limit of size 1 overflow 0 reached,
connection timed out`**, GREEN after.

## Test fixtures

Checked-in under `backend/tests/fixtures/` — filenames are canonical (mirrored in the docs/01 layout) and referenced by the M2/M5/M8 acceptance criteria (docs/10):

| `csv_sign_inverted.csv` | Generic CSV with the opposite sign convention (`credits_positive=true`) | M2 profile-importer tests |
| `csv_credit_debit.csv` | Split-column CSV: Date, Description, Withdrawal, Deposit (`%d/%m/%Y`, decimal comma); one both-empty row asserted skipped | M2 split-mode tests |
| `sample.qif` | Synthetic QIF: signed `T` amounts, `P`/`M`/`L` payee fallbacks, a `1,200.00` comma amount, a trailing `!Account` section | M71 QIF parse + dry-run tests |
| `near_dup_seed.csv` | Three plain signed-amount CSV rows committed first | M71 near-duplicate baseline |
| `near_dup_shift.csv` | The same money one day off, a padded payee, a byte-identical row, and one genuinely new row | M71 verdict precedence (3 near / 1 exact / 1 new) |
| `trades_fixture.json` | Buys in CAD and USD, a partial sell, a superficial-loss scenario, a ROC distribution, and a 2:1 split entered as `corporate_action_adjustment` (mechanics docs/06) | M5 tax-engine acceptance; M8 investment-performance report check |

New fixtures follow `<what>_<variant>` snake_case naming. These are committed in M1/M2 alongside the code they test.

**Downloadable samples (M71).** Four more synthetic statements live in `frontend/public/help/import-samples/` (`sample.csv`, `sample.ofx`, `sample.qfx`, `sample.qif`) and are linked from the `/help/import-guides` hub; they carry invented merchants and amounts and exist for humans to try, not for tests. Duplicated byte-for-byte under `site/guides/samples/` so the static site's copy of the guide does not 404 (docs/32).

## Dry-run preview (cancel-safe, M71)

`POST /imports/preview` runs the **entire** ingest pipeline and writes nothing.

- Multipart `file` + `account_id` + optional `profile_json` — the same three fields as `POST /imports/upload`, so a wizard-confirmed profile previews exactly what a Confirm would upload.
- The same cheapest-first validation chain (Content-Length → byte cap → sniff → CSV line caps → parse → row cap) with the same error codes; a rejection still leaves zero rows behind.
- Response (`DryRunResponse`, `app/schemas/imports.py`):

```json
{
  "format": "csv",
  "mapping": {
    "kind": "csv_profile",
    "fields": [{"field": "date", "source": "Trans. Date"}],
    "mode": "signed",
    "date_format": "%d/%m/%Y",
    "decimal_comma": true,
    "credits_positive": false,
    "signs": null,
    "headerless": false
  },
  "rows": [
    {
      "index": 0,
      "date": "2026-03-15",
      "amount_native": "-45.67",
      "currency": "CAD",
      "raw_description": "METRO STORES #123",
      "cad_amount": "-45.67",
      "verdict": "near_duplicate",
      "near_duplicate_match": {
        "source": "committed",
        "transaction_id": 118,
        "date": "2026-03-14",
        "amount_native": "-45.67",
        "currency": "CAD",
        "raw_description": "METRO STORES #123",
        "delta_days": 1,
        "similarity": 1.0
      }
    }
  ],
  "summary": {"total": 3, "new": 1, "duplicates": 1, "near_duplicates": 1, "unresolved": 0}
}
```

- **`mapping`** is the column-role table. `kind="structured"` for OFX/QIF, whose source tokens are fixed (`date → D`, `amount_native → T` for QIF; `date → DTPOSTED`, `amount_native → TRNAMT` for OFX/QFX). `kind="csv_profile"` for CSV, where `fields` is the field → header-name map and `mode`/`date_format`/`decimal_comma`/`credits_positive`/`signs`/`headerless` echo the profile that drove the parse.
- **`verdict`** is how each row is categorised before commit: `new` | `duplicate` | `near_duplicate`.
- **`cad_amount`** is the cached-FX application (`services/fx.cached_cad_amount` — cache reads only, never a fetch); `summary.unresolved` counts rows with no cached rate for that date, the same rule as `import_batches.unresolved`.
- Rows carry `index` (position in the parsed file), not a database `id`, because nothing was inserted.

**Zero-write guarantee.** The handler issues only `SELECT`s: no `add`, no `flush`, no `commit`. `tests/test_import_preview.py::test_cancel_after_a_full_preview_persists_nothing` previews OFX, QIF and CSV in sequence against one account and asserts `transactions` and `import_batches` row counts are byte-identical before and after (both zero).

**Confirm** is a separate `POST /imports/upload` carrying the same file and profile; **Cancel** is never making that call, so an abandoned preview leaves no batch, no staged row and no dedup row behind. The two calls parse the file independently, so preview and Confirm agree unless the committed data changed in between.
## YNAB migration (docs/38)

A whole-budget import sits beside the statement upload and reuses its review
loop: `POST /imports/ynab/preview` maps the export (accounts, the group →
category tree, group tags, targets, every row) and writes nothing;
`POST /imports/ynab/commit` applies that same plan in one transaction into
`pending_review` batches, one per target account, committed/discarded through
the routes above. Same upload validation order (rate limit → Content-Length →
5 MB → parse → 50 000-row cap) and the same dedup contract, so a second run
reports `net_new = 0`. Rows it cannot import come back with a code, a reason
and their row number instead of being skipped. Spec: `docs/38`.

## Net-worth file import (docs/58 §B)

Balance data — not transactions — comes in through two routes on the same
`imports` router, registered **above** the `/{batch_id}` routes so
`/imports/networth/commit` is never swallowed as a batch id (the docs/38 §2
ordering lesson):

- `POST /imports/networth/preview` — multipart `file` + optional `currency`
  (3-letter, default `CAD`) + optional `account_name` (label for a
  single-account spreadsheet). Maps and returns the full plan; **writes
  nothing**, safe to run repeatedly.
- `POST /imports/networth/commit` — identical form; maps **and applies in one
  transaction**: `created` new `accounts` rows, `reused` matched by name.

**File kinds** (first non-whitespace byte decides): a ProjectionLab JSON
export (`today.savingsAccounts` / `today.investmentAccounts` / `today.debts`)
→ `source_format="projectionlab_json"`; otherwise a balance CSV needing an
account column (`account`, `account name`, `name` — or the `account_name` form
field) **and** a balance column (`balance`, `value`, `amount`, `net worth`,
`total`, `current balance`), with optional `date` (latest row per account
wins, older rows collapsed into a note), `type` and `currency` columns →
`source_format="balance_csv"`.

**Plan** (Decimal as string): `counts{accounts, accounts_create,
accounts_reuse, accounts_unmapped, balance_rows_collapsed,
progress_snapshots_skipped, assets_skipped}`, `accounts[]{name, source_type,
mapped_type, action(create|reuse|unmapped), existing_id, file_balance,
current_balance, currency, reason, notes[]}`, `skipped[]{kind, name, reason}`
(never truncated), `warnings[]{code, detail}`, `can_commit`.

**Apply semantics:** `starting_balance` = the file's latest balance;
`taxable` server-derived from `type` (docs/14 C1); an existing account with
the same name (case-insensitive) is **reused, never overwritten** — its
balance is reported as `current_balance` beside `file_balance` and kept
(merge review; manual data wins). ProjectionLab `assets` (real estate /
vehicles), `progress[]` history and `plans[]` planning data are listed in
`skipped` with reasons; account-type mapping is the docs/58 §B table.

**Errors** (all `{detail, code}`): `422 unsupported_networth_format` (JSON
that is not a ProjectionLab export), `422 networth_missing_columns`,
`422 networth_row_invalid`, `422 networth_no_accounts`,
`413 networth_row_cap` (>1000 rows), `413 file_too_large` (>5 MB),
`422 invalid_currency`, plus the shared E2EE refusal on **commit**
(`encryption_required`; preview is read-only and runs). Rate limits: preview
`6/minute`, commit `10/minute`. Spec: `docs/58`.

## Dedup

SHA256 over `(account_id, date, amount_native, whitespace-normalized raw_description)`.
The amount is quantized to cents first; a sub-cent amount quantizing to `-0.00` is
normalized to `0.00` so equal money is one row (property-pinned in
`tests/test_property_dedup_hash.py`).

Security note (audit H5, docs/33): the live-row hash is **unsalted** SHA-256 — dictionary-reversible to descriptions/amounts by anyone with DB read access; only staged duplicates and split children salt (uuid appended, below). Planned replacement: keyed HMAC under the docs/33 Phase 3/4 index key.

Enforced as a UNIQUE constraint on `transactions.dedup_hash` per account (docs/02). A detected duplicate is **staged, not dropped**: it is INSERTed with a salted `dedup_hash` (canonical hash + uuid) and `is_duplicate=true`, and reported in the batch summary. At commit the user may force-include specific duplicates via an `include_ids` allow-list; un-forced duplicates are deleted and only forced ones become live. Duplicates are never silently merged or overwritten.

### Near-duplicates (M71)

The canonical hash only catches a byte-identical re-upload. `app/services/import_dedup.py::find_near_duplicates` catches the near ones — a re-exported purchase with a padded payee, or a posting date a day off — and **surfaces them for a decision instead of skipping them** (AGENTS §3 arbitration principle).

A row is `near_duplicate` against a candidate of the same account when all of:

- `amount_native` is equal to the cent **and** `currency` is equal;
- `|Δdate| <= 3` days (`NEAR_DUP_DATE_WINDOW_DAYS`);
- payee similarity `>= 0.75` (`NEAR_DUP_MIN_SIMILARITY`), scored as `max(difflib.SequenceMatcher ratio, token Jaccard)` over a lowercased, punctuation-free, whitespace-collapsed payee (the Jaccard term is what still matches `METRO STORES #123` against `METRO STORES #123 TORONTO`);
- and it is not already an exact canonical-hash hit.

**Verdict precedence is strict:** `duplicate` > `near_duplicate` > `new`. An identical (date, amount, payee) triple *is* the canonical hash's input, so it is reported as `duplicate` exactly once and never double-labelled.

Candidates are **committed rows and staged rows of any pending batch**, plus the other rows of the file being previewed. Each match names which (`source: committed | staged | batch`), the matched row (`transaction_id` when it exists in the database), `delta_days` and `similarity`. Cost is bounded: one range query over `[min_date - 3, max_date + 3]` for the account (joining `import_batches.status` — `import_batch_id` survives commit, so the *status* is what distinguishes live from awaiting-review) plus an in-memory bucket keyed by `(currency, amount)`, so the pairwise scan only ever sees amount-identical rows.

Verdicts are reported in three places: the dry run, `POST /imports/upload`'s `preview[]`, and `GET /imports/{batch_id}/rows` (re-derived on read, so a batch reopened later keeps its flags).

**A near-duplicate is staged as a normal row** and imports on commit — it is never dropped for the user. The available decisions are cancel at preview, discard the whole batch, or correct the row through transaction CRUD after commit. Nothing here touches rules or categorisation: docs/04's standing rule still holds (nothing fires on import commit).

Accepted tradeoff: two genuinely identical same-day purchases (same account, date, amount, payee) collapse into one live row plus one **staged** duplicate — the duplicate row is surfaced for review and is force-includable; the batch summary counts every skip.
Manual transaction CRUD uses this same canonical hash and cache-only CAD calculation, with `import_batch_id=NULL`. Manual rows are live immediately; pending-review rows remain owned by the staged batch flow and cannot be edited or deleted through transaction CRUD. Committed imported rows may be corrected through transaction CRUD.

### Split transactions (M107) — dedup stays parent-level

Splitting a row (docs/44) changes **no** dedup key, so re-uploading the same
statement after a split behaves exactly as it did before:

- **The parent is the anchor.** The canonical hash covers the statement's
  identity — `account_id`, `date`, `amount_native`, whitespace-normalized
  `raw_description` — every field a split leaves untouched, and the parent's
  `dedup_hash` cannot drift while it is split (its identity columns are
  frozen, docs/44 §5).
- **A re-imported statement line matches the PARENT, never a child.** Exact
  dedup (`services/import_staging.py::stage_rows`, `api/imports.py`) looks up
  `account_id + dedup_hash`; each child stores
  `f"{canonical}:{uuid4().hex}"` — the same salted shape staged duplicates
  already use — so no hash the importer can compute ever equals a child's.
- **Near-duplicate preview excludes children.**
  `services/import_dedup.py::find_near_duplicates` adds the shared
  `IDENTITY_EXCLUDE_CHILDREN` predicate (`parent_split_id IS NULL`) to its
  candidate query, so a child's amount can never make a re-upload look like a
  *new* row. The parent stays in the candidate set: it IS the statement.
- **Imports never auto-split.** No detection heuristic, no shape sniffing, no
  receipt-triggered split (docs/44 D16): a staged row arrives as one row and
  stays one row — `is_split=false`, `parent_split_id=NULL`.
- **OFX export is IDENTITY** (`GET /export/ofx` → `identity_tx_conditions`):
  a split statement keeps its single line and the children never reach the
  file, so the round trip still re-imports to `0 net-new`.
- **A batch takes its split with it.** `parent_split_id` is
  `ON DELETE CASCADE`, so discarding or reverting a batch deletes a parent's
  children alongside it — no partial split can survive (docs/02).

**Exports apply NO split population rule — deliberately** (docs/44 §10,
docs/11): `GET /export/full` collects every row with no visibility filter at
all (staged included — the payload the encrypted browser backup carries
verbatim), and the ZIP's `collect_rows(committed_only=True)` runs
`api/export.py::_visibility_filter`, which for `transactions` is the
**unchanged** `committed_tx_conditions` (the staging rule above). Parents
*and* children ship in both, because a restore that dropped either side would
lose the relationship.

That settles a line in docs/44's touch-point table: **this codebase has no
separate transaction-CSV route.** Every CSV ships inside `GET /export/zip` as
`transactions.csv`, rendered from that same snapshot — a faithful table dump
(carrying `is_split` and `parent_split_id`), not a report. The LEDGER rule
that docs/44 D12 names for "CSV exports" is the one the **report** CSVs
(`/reports/…&format=csv`, docs/12) follow through `countable_tx_conditions`;
nothing infers LEDGER from the ZIP.


## Staged-review flow

```
preview ──▶ parse ──▶ mapping + row verdicts + cached-FX     ZERO WRITES
   │  Cancel = do nothing; no batch, no staged row, no dedup row
   │  Confirm
   ▼
upload ──▶ parse ──▶ import_batches status=pending_review
                         │  user resolves unmapped payees / categories in UI
                         │  (near-duplicate flags ride along on the staged rows)
                         ▼
                    commit flips batch to committed (rows become live transactions)
```

Any batch may instead be discarded. A **committed** batch can later be **reverted** via `POST /imports/{batch_id}/revert` (status → `reverted`): the batch's **transactions AND trades** are deleted — both are the user's data, and `import_batches` rows are never deleted, so nothing cascades — together with the transaction children no FK cascades either (tag links, LLM suggestions, reconciliation matches; the docs/29 F3 child set) and with `acb_pools` rebuilt in the same transaction (docs/02), so a revert returns the account to its pre-import state on both the positions and the cost-basis surface. Any paired `transfer_pair_id` is unlinked (ON DELETE SET NULL, migration 0010); only committed batches may be reverted, otherwise → `409 batch_not_committed`. Apart from revert, a committed batch's rows are stable — corrections happen on the live `transactions`/`trades` rows.

Before M112 the revert/discard paths deleted only the `transactions` leg, so a batch that had staged into `trades` reached `reverted`/`discarded` with its trades still linked (prod: batch 176 `reverted` kept 71, 177 `discarded` kept 70 — invisible to every staging-filtered reader, still listed by the trades surfaces). `backend/scripts/sweep_terminal_batch_trades.py` repairs such leftovers: report-only by default, `--apply` to delete, and it only ever touches `discarded`/`reverted` batches — a `committed` batch's rows are LIVE data and a `pending_review` batch is an undecided review. Its tests pin that the sweep moves neither positions nor `acb_pools`.

Staging model — no side table: parsed rows INSERT immediately into `transactions` with `import_batch_id` set while the batch sits in `pending_review`. Batch-scoped reads (lists, dashboards, reports, categorization) filter on `batch.status = 'committed'`. `commit` flips the status (deleting un-forced `is_duplicate` rows first); `discard` deletes the batch's rows — the same two legs revert runs, `transactions` and `trades`. The dedup UNIQUE `(account_id, dedup_hash)` spans committed AND pending rows, so re-uploading a file whose copy is still pending also lands in `duplicates_skipped`. Staged duplicates carry a salted `dedup_hash` (canonical + uuid) so the UNIQUE constraint is not violated; the same `is_duplicate` / `force_include` mechanism applies to investment CSV imports via the `trades` table (docs/02). Upload caps (security-hardening): files over 5 MB → 413 `file_too_large`; parsed rows over 2000 → 413 `too_many_rows` — both rejected before any row is staged. The full cheapest-first validation chain (rate limit → Content-Length → byte cap → CSV line caps → parse → row cap) is pinned in **Upload validation order** below.
The batch summary's `unresolved` count = rows whose `cad_amount` stayed NULL because no `fx_rates` row resolved for the transaction date (docs/02). They commit normally and remain flagged until a rate exists.

**The same staging rule governs `trades`.** The investment-CSV import stages through the identical `import_batches` lifecycle (`trades.import_batch_id`, migration 0011), so every trade-derived LIVE calculation — positions, ACB pools, realized gains, dividends, dividend calendar, tax-loss alerts, benchmark comparison, the security-detail page's `recent_trades`, and which securities get price-refreshed — filters on `batch.status = 'committed'` (or `import_batch_id IS NULL`) via the shared predicate `services/trade_filters.committed_trade_conditions`, the trades-side twin of `services/txn_filters.committed_tx_conditions`. No reader inlines its own copy. The import REVIEW surface is the deliberate exception: `POST /trades/import` (preview), `POST /trades/import/{id}/commit`, `POST /trades/import/{id}/discard`, `GET /trades`, and the batch-row lookups all still see staged rows, because they are the only window into a pending batch (docs/29 F1e).

**Wealthsimple activity vocabulary (M113).** Three keys of the WS `ACTIVITY_MAP` carry rules the static action alone cannot express. Evidence: Mark's real TFSA export left **20 rows** unmapped (every `Tax/NRT` withholding line plus every TF-to-TF cash transfer), his RESP export **1** (`MoneyMovement/TRANSFER`, the money out) — both now parse to **zero** unmapped rows in BOTH parsers (the legacy one the wizard falls back to, and the opted-in `wealthsimple` profile):

| key | lands as | the rule |
| --- | --- | --- |
| `(Tax, NRT)` | `fee` | Non-resident withholding on US dividends — an **income-side cost**. The dividend row keeps its **gross** amount and the withholding becomes its own visible fee row: gross + fee is the auditable representation, and no net dividend is ever fabricated (the file states no such row). Since M131 `price_native` stores the file's NEGATIVE native sign (money out) and `fee`'s cash effect is `-abs(price_native)` (docs/05), which is money out however the row is stored. The opted-in profile surfaces this as a per-file notice (docs/41 §Notices). |
| `(MoneyMovement, TRANSFER_TF)` | `transfer_in` / `transfer_out` | TF-to-TF transfer **into/out of** the account. |
| `(MoneyMovement, TRANSFER)` | `transfer_in` / `transfer_out` | The generic WS money transfer (the RESP row says `Money transfer out of the account`). |
| `(MoneyMovement, EFT)` | `contribution` / `withdrawal` | **M131:** a contribution AND a withdrawal both arrive as `EFT` — the direction lives in the DESCRIPTION (`Deposit` / `Withdrawal` / `Money transfer into\\|out of the account`), not the sub-type. With no description match the `net_cash_amount` sign decides (negative = money out). A `contribution` whose amount is NEGATIVE is refused **422 `negative_contribution`**, never stored. |

The two `MoneyMovement` TRANSFER keys are **cash rows wearing transfer vocabulary** (docs/02 input convention): `quantity = 0`, symbol NOT required, staged against the `CASH` sentinel security — and their direction exists nowhere except `net_cash_amount`'s **sign**: negative = money out, positive = money in (both real exports agree; the WS `direction` column is `LONG` on trade rows and EMPTY on every transfer row in both files, so it cannot disambiguate). The sign is read in `investment_profile.build_trades` **before** any `abs()`, so `abs_magnitudes` can never flip it; `CASH_TRANSFER_KEYS` pins both keys. The `(MoneyMovement, EFT)` key is instead pinned by `CASH_DIRECTION_KEYS` and resolved per row by `build_trades.cash_row_direction` (M131, docs/03 table above). `SecurityTransfer/*` is deliberately in neither set: an in-kind transfer always carries a symbol and keeps the symbol + per-share cost-basis rules, and a symbol-less one still comes back unmapped.

**Cash-row signs (M131, docs/02 §Signed-amount convention).** `build_trades` stores a cash OUTFLOW (`fee`/`withdrawal`) at its NEGATIVE native sign and a cash INFLOW (`dividend`/`roc`/`contribution`) at the file's sign; a cash TRANSFER stores a MAGNITUDE because M113 already read its direction. `abs_magnitudes` (every profile file, M82) still normalises a TRADE's commission to a positive magnitude but no longer rewrites a cash row's amount — the cash effect (`-abs` for outflows, docs/05) is direction-safe either way, so the LEDGER is unchanged while the stored number stays honest on the transactions surface, exports and the event log.

Because a cash `transfer_out` now reaches the ACB replay with `quantity = 0` and no holding behind it, `tax/acb.py` allocates nothing for it — the old `pool.total_acb * quantity_out / held_quantity` would have divided `0/0` and 500'd the commit in a taxable account (docs/06).

**Live A/B:** the pre-M113 parser (git `3b17b95`) on Mark's real bytes reports 20 unmapped of 91 (TFSA) and 1 of 20 (RESP); the deployed stack answers 0 for both — measured by `frontend/scripts/a11y/m113_pending_import_probe.py`, evidence in its `m113-pending-import-probe.json`.

**Touch points / verification:** `app/importers/investment_profile.py` (`ACTIVITY_MAP`, `CASH_TRANSFER_KEYS`, `CASH_DIRECTION_KEYS`, `cash_row_direction`, `build_trades`), `app/importers/brokerage_profiles.py` (the notices), `app/tax/acb.py` (qty-0 guard); fixtures `tests/fixtures/m113_{tfsa,resp}_activity.csv` — clipped from Mark's real exports with the account id anonymized to `ws-anon`; `tests/test_m113_ws_cash_mappings.py` and `tests/test_m131_ws_cash_signs.py`.

**Per-broker CSV profiles (M82, docs/41)** sit *in front of* that one staging loop, never beside it. `app/importers/brokerage_profiles.py` holds seven pure-config profiles (`questrade`, `wealthsimple`, `ibkr`, `fidelity`, `td-di`, `rbc-di`, `nbdb`) that decode a broker's activity file into the same canonical Wealthsimple-vocabulary rows `investment_profile.build_trades` already consumes, so signed quantities, cash-only handling, transfer cost basis and commission rules are inherited rather than re-implemented. Selection is `?profile=<slug>` on `POST /trades/import{,/preview}`; without it `detect_profile` matches the header, and a detected Wealthsimple file falls back to the legacy parser so existing imports stay byte-identical. `import_batches.institution` becomes the profile label instead of `investment_csv`. No profile touches the database or the network.

Encoding: UTF-8 with a latin-1 fallback on `UnicodeDecodeError` (bank exports vary); OFX/QFX SGML quirks are `ofxparse`'s problem, not ours — but every `ofxparse` failure (`OfxParserException`, malformed-header `ValueError`/`LookupError`) is wrapped as `OfxParseError` in `app/importers/ofx.py` and surfaced as `422 ofx_parse_error` (audit L5), never a bare 500.

Stray quotes: some bank CSV exports (e.g. TOYS "R" US) include unescaped double-quotes inside quoted fields without doubling them, which breaks `csv.reader`. A `_fix_stray_quotes` preprocessor runs before `csv.reader` on both headed and headerless paths: it detects `"` mid-field (not at field boundaries) and replaces them with single-quotes. This is transparent to downstream parsing. The sanitizer is **single-pass O(n)**: each line is walked once and the "does this quote close the field?" look-ahead visits each character of the line at most once in total. The pre-H2 implementation re-joined the remainder of the line for every quote — O(quotes × line), measured at ≈61 s for one 160k-quote line — which was a whole-API CPU DoS through `POST /imports/upload`. Outputs are byte-for-byte unchanged, pinned by characterization cases plus a differential test against a verbatim copy of the old implementation in `backend/tests/test_import_dos.py` (200k-quote line now sanitizes in ≈0.02 s).

## One staging function for every source (M79)

`backend/app/services/import_staging.py::stage_rows` owns batch creation, the
cache-only CAD resolution, the dedup hash + duplicate salt, the counters and
the 2000-row cap. Every ingestion source calls it:

- `POST /imports/upload` (OFX/QFX, generic CSV) — parses, then delegates.
- `POST /sync/agreements/{id}/pull` (docs/39) — maps provider rows onto
  `RawTransaction`, then delegates.
- `POST /imports/ynab/commit` (docs/38) — maps the plan's row bindings onto
  `RawTransaction` and delegates, passing the optional **`decorate` hook**:
  `decorate(txn, raw, index)` is called once per row, in order, before the row
  is added to the session, and is the only sanctioned way for a source to set
  columns the pipeline does not model (YNAB's `category_id` and `cleared`).
  It must not touch `dedup_hash`, `is_duplicate`, `import_batch_id` or the
  counters — those belong to `stage_rows`. Anything that needs ids (YNAB's
  `transaction_tags` links and its `ynab_id` → staged-row map for transfer
  pairing) runs after `stage_rows` returns, from the `StagedRow.txn` objects it
  hands back.

**M106 additions to the same function** (docs/02 §transaction_events,
docs/09 §Review queue):

- every staged row is written with **NEITHER `reviewed_at` NOR `reviewed_by`**
  — that is what makes a review queue form by itself after every import, and
  it is set explicitly so a future column default cannot quietly pre-review a
  stranger's statement;
- after the flush that assigns ids, `stage_rows` appends **one `created`
  receipt per staged row** in the same transaction: `{"source": "import",
  "batch_id", "file_name", "source_label"}` for a file or a YNAB plan, and
  `{"source": "sync", …}` with `actor = NULL` for a bank pull
  (`system_actor=True` — no person was at the keyboard, which is exactly what
  keeps those receipts out of a partner's history view, docs/43 §6). A batch
  rejected before it exists (`413 too_many_rows`) therefore writes neither
  rows nor receipts.

Consequences, all pinned by `tests/test_bank_sync.py` and
`tests/test_ynab_migration.py`:

- **Duplicate detection bridges file, sync and YNAB automatically**, because
  the canonical hash is looked up across *all* of the account's rows (committed
  and pending) regardless of which source produced them. CSV-then-sync (or
  YNAB-then-CSV) of the same transaction = one live row plus a staged
  `is_duplicate` match decision, force-includable or deleted at commit exactly
  as in the file path.
- A multi-account source creates **one batch per account** that received rows
  (`ImportBatch.account_id` is per-account); a pull with no new rows stages
  nothing.
- The row cap fires *before* the batch exists, so a rejected source leaves no
  rows (`413 too_many_rows`, message prefixed `Bank sync` on the pull path,
  `YNAB import` on the YNAB path and `File` on the upload path — same code; the
  YNAB migration rolls the WHOLE migration back, because that path is one
  transaction).
- A pulled batch records `file_name = "bank-sync:<provider slug>"` and the
  account's institution name, so the Imports history reads the same way for
  both.

**Batch creation is therefore confined to this module.** The two documented
exceptions, both outside the transaction-ingestion path:
`app/services/demo_seed.py` (the demo dataset seeds `committed` batches
directly — it does not ingest a file) and `app/api/trades.py` (investment CSV
stages `trades` rows, not `transactions` — docs/05 keeps that loop separate).
`grep -c 'ImportBatch(' backend/app/services/*.py backend/app/api/*.py` must
return exactly those three sites.

## Upload validation order (audit H2/L5/L6)

Every ingest upload is validated cheapest-check-first. Each failure is the `{detail, code}` envelope (docs/08) — no path returns a bare 500:

1. **Rate limit** — see **Rate limits** below → `429 rate_limited`.
2. **Content-Length** — request body declaring more than `MAX_BODY_BYTES`
   (5,500,000 = 5 MB file cap + multipart overhead) → `413 file_too_large`,
   read from the header before the file bytes are touched
   (`_reject_oversize_body` in `backend/app/api/imports.py`).
   *RESIDUAL (accepted):* FastAPI parses the multipart body before the route
   runs, so an over-cap body is still buffered once by the ASGI layer; a true
   streaming abort needs custom middleware (out of scope for this fix) and
   nginx's `client_max_body_size` (10 MB) remains the network backstop.
3. **Byte cap** — file content over 5,000,000 bytes → `413 file_too_large`.
4. **Format sniff** — OFX/QFX, then QIF, then CSV (registry order) → `422 unsupported_format`.
5. **CSV line caps (pre-parse, O(n))** — CSV only; a line over
   `MAX_CSV_LINE_CHARS` (1,000,000 chars) → `422 csv_line_too_long`; a line
   with over `MAX_CSV_LINE_QUOTES` (1,000 `"`) → `422 csv_too_complex`.
   Implemented by `check_csv_complexity` in `backend/app/importers/csv_profile.py`.
6. **Parse** — stable codes, never 500: `422 csv_parse_error` (`csv.Error`,
   e.g. a field over csv's 128 KiB field limit), `422 ofx_parse_error` (every
   `ofxparse` failure), `422 qif_parse_error` (every `QifParseError`: missing
   date/amount, unparseable amount, unrecognised date, no transaction records,
   a file whose only sections are non-bank such as `!Type:Invst`), `422 parse_error` (profile/semantic row errors —
 including malformed investment CSV on `POST /trades/import`, which maps
 `csv.Error` to `InvestmentCsvProfileError` → `422 parse_error`, and M82's
 **stuck-row refusal**: a row whose date or number cannot be read, or a ragged
 row, makes `POST /trades/import` answer `422 parse_error` naming the line
 instead of staging its neighbours — the dry run lists the same row with
 `unmapped[].code = "stuck"` so nothing is dropped silently, docs/41),
 `422 missing_importer_profile`.
7. **Row cap** — parsed rows over 2000 → `413 too_many_rows` (only knowable
   after parse). Enforced inside `stage_rows` (`MAX_STAGED_ROWS`), so the bank-
   sync pull hits the identical cap with no batch created (docs/39), and so
   does a YNAB commit whose single target account carries more than 2000 rows
   (docs/38 §6 — that path is one transaction, so nothing at all is written).

The same chain applies to `POST /imports/suggest-profile` (steps 1–3 and 5,
then the CSV sample read at step 6).

Touch points: `backend/app/api/imports.py`, `backend/app/importers/{csv_profile,ofx,qif,investment_profile,brokerage_profiles}.py`.

Verification (covered by `backend/tests/test_import_dos.py`): a CSV with one 1,100,000-char line → `422 csv_line_too_long`; 1,001 quotes on one line → `422 csv_too_complex`; a 200 KB quoted field (under both caps) → `422 csv_parse_error`; an OFX body whose `<OFX>` transaction is missing `TRNAMT` (or `OFXHEADER:100` + garbage header) → `422 ofx_parse_error`; a `!Type:Bank` record with a date but no `T` amount → `422 qif_parse_error` naming the record; a 6 MB body → `413 file_too_large` from the Content-Length check.

## Rate limits

Human-paced per-IP limits on the mutating ingestion endpoints, via the shared slowapi limiter in `backend/app/core/ratelimit.py` (`human_paced_limit` decorator factory; `IMPORT_UPLOAD_LIMIT = "6/minute"`, `IMPORT_MUTATE_LIMIT = "10/minute"`). Buckets are keyed (client key, route function) — the limiter uses `key_style="endpoint"` — so path parameters such as `/imports/{batch_id}/commit` cannot mint a fresh bucket per id:

| Route | Limit |
| --- | --- |
| `POST /imports/preview` | `6/minute` |
| `POST /imports/upload` | `6/minute` |
| `POST /imports/suggest-profile` | `6/minute` |
| `POST /trades/import` | `6/minute` |
| `POST /trades/import/preview` | `6/minute` (docs/41) |
| `POST /imports/{batch_id}/commit` | `10/minute` |
| `POST /imports/networth/preview` | `6/minute` (docs/58) |
| `POST /imports/networth/commit` | `10/minute` (docs/58) |
| `POST /imports/{batch_id}/discard` | `10/minute` |
| `POST /imports/{batch_id}/revert` | `10/minute` |
| `POST /sync/agreements` · `/{id}/pull` · `/{id}/revoke` | `10/minute` (`SYNC_MUTATE_LIMIT`, docs/39) |

Breach → `429 {"detail": "Too many attempts", "code": "rate_limited"}`. Enforced only while `settings.rate_limit_enabled` (config default true; `tests/conftest.py` sets `RATE_LIMIT_ENABLED=false` — tests opt in by flipping `limiter.enabled`, as `tests/test_import_dos.py` does). The client key is the rightmost `X-Forwarded-For` entry with socket-peer fallback; see the SECURITY NOTE in `core/ratelimit.py` for the current proxy-topology caveat (audit M3: the in-repo nginx appends rather than strips XFF — front-proxy remediation is tracked separately).

Verification: with `limiter.enabled = True`, the first 6 `POST /imports/upload` requests in a minute pass through and the 7th returns `429 rate_limited` (`test_import_upload_rate_limit_engages_when_enabled`).

## Test fixtures

Checked-in under `backend/tests/fixtures/` — filenames are canonical (mirrored in the docs/01 layout) and referenced by the M2/M5/M8 acceptance criteria (docs/10):

| File | Contents | Consumed by |
| --- | --- | --- |
| `sample.qfx` | OFX/QFX bank statement sample | M2 importer tests; M71 QFX dry run |
| `sample.qif` | Synthetic QIF bank export (see the first fixtures table) | M71 QIF parse tests |
| `near_dup_seed.csv` / `near_dup_shift.csv` | Near-duplicate pair (see the first fixtures table) | M71 near-duplicate verdicts |
| `csv_decimal_comma.csv` | Generic CSV: `%d/%m/%Y` dates, decimal comma, `credits_positive=false` (matches the profile JSON example above) | M2 profile-importer tests |
| `csv_sign_inverted.csv` | Generic CSV with the opposite sign convention (`credits_positive=true`) | M2 profile-importer tests |
| `monarch_transactions.csv` | Synthetic Monarch Money export: `Date,Merchant,Account,Category,Notes,Amount,Tags`, expenses negative, one accounting-parentheses row | docs/58 mapping-confirmation gate + Monarch round trip |
| `projectionlab_export.json` | Synthetic ProjectionLab export: 3 savings + 5 investment + 2 debt accounts, 2 assets, 2 progress snapshots, 1 plan | docs/58 net-worth round trip |
| `networth_spreadsheet.csv` | Balance spreadsheet: `Account,Type,Balance,Date,Currency`, one account with two dated balances (collapse) and one parenthesized balance | docs/58 net-worth round trip + merge review |
| `trades_fixture.json` | Buys in CAD and USD, a partial sell, a superficial-loss scenario, a ROC distribution, and a 2:1 split entered as `corporate_action_adjustment` (mechanics docs/06) | M5 tax-engine acceptance; M8 investment-performance report check |

New fixtures follow `<what>_<variant>` snake_case naming. These are committed in M1/M2 alongside the code they test.
