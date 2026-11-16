# 41 — Brokerage CSV import profiles (M82)

## Context

M71 gave the app a generic CSV importer driven by per-institution column
profiles (`institutions.importer_profile`, docs/03) and a single investment-CSV
path for Wealthsimple-shaped activity files (`POST /trades/import` — staging
and validation in docs/03, route shape in docs/08, trade semantics in
docs/02/docs/05).
Brokers other than Wealthsimple still have no profile, so their exports fall
through to `422 missing_importer_profile` (generic CSV) or parse as the wrong
shape (investment CSV).

The competitive evidence for this wave is that no mainstream personal-finance
app ingests brokerage data reliably: *"Not one can interface with brokerage
firms like IBKR and Fidelity without issues."* This doc pins **one profile per
brokerage** so an export either imports with correct semantics or is refused
with a per-institution reason — never partially and never silently.

Out of scope (unchanged by this doc): screen scraping, bank auto-sync / open
banking (docs/39), direct API connectivity to any broker, aggregator
integrations, new migrations, and any UI beyond the import wizard's
per-institution picker.

## Approach

### What a profile is

A brokerage profile is **pure configuration plus a fixture**. It never writes to
the database, never resolves a security, and never invents a trade: it turns a
CSV into `RawTrade` rows (docs/03 `importers/base.py`) using exactly the same
row semantics the Wealthsimple path already uses, and hands them to the one
existing trade-staging loop.

```
file ─▶ decode ─▶ reader (flat table | IBKR sectioned statement)
                 ─▶ canonical activity dict (WS column vocabulary)
                 ─▶ investment_profile.build_trades(...)          ← SHARED semantics
                 ─▶ (list[RawTrade], list[UnmappedRow], list[str] notices)
```

The canonical dict uses the Wealthsimple vocabulary that
`investment_profile._read_investment_rows` already consumes:

| canonical key | meaning |
| --- | --- |
| `activity_type` | the broker's action word, verbatim from the file |
| `activity_sub_type` | qualifier (`DRIP`, `EXPRESS`, `EFT`, …) or empty |
| `symbol` | ticker |
| `name` | security name / description |
| `currency` | ISO-4217 token or empty |
| `description` | free-text description |
| `effective_date` | trade date, profile's date format |
| `quantity` | shares/contracts, as written |
| `unit_price` | per-share price |
| `commission` | commission charged |
| `net_cash_amount` | net cash amount |
| `account_id` | account label from the file (matched by the user, docs/05) |
| `__line__` | physical 1-based line number in the file (integer) |

Because the canonical dict is the Wealthsimple vocabulary, **every trade
semantics rule docs/02/docs/05 already pins is inherited, not re-implemented**:
signed quantities (buy/drip positive, sell negative), cash-only actions
(`quantity=0`, `price_native` = gross cash amount), transfers requiring a
symbol and carrying a per-share cost basis, commission only on
`buy|sell|drip`, and unmapped rows instead of whole-file rejection for a bad
row. **M113 carve-out:** a `MoneyMovement` transfer is a CASH row, not a share
movement, so the symbol rule and `require_quantity` are skipped for those two
keys and the direction is read from `net_cash_amount`'s sign (docs/03 §
Wealthsimple activity vocabulary); `SecurityTransfer` keeps every rule above.
**M131 carve-out:** a `MoneyMovement/EFT` row's contribution-vs-withdrawal
direction lives in the DESCRIPTION (`Deposit` / `Withdrawal` / `Money transfer
into|out of`), resolved per row by `build_trades.cash_row_direction` with the
amount's sign as the fallback; a cash OUTFLOW (`fee`/`withdrawal`) is stored
at its NEGATIVE native sign (docs/02 §Signed-amount convention) while a cash
INFLOW keeps the file's sign — the cash effect is `-abs` / `+price_native`, so
the LEDGER is unchanged. That is the "reuses `investment_profile.py` machinery" requirement of M82
made literal: `build_trades` is the only place those rules live.

One behaviour-preserving refactor was made to `investment_profile.py`. The old
private `_read_investment_rows(reader, lookup)` body became the public
`build_trades(entries, lookup, **opts)`, and `_read_investment_rows` is now a
two-line wrapper that calls it with `enumerate(reader, start=2)` — byte-for-byte
the same output the legacy path always produced
(`tests/test_investment_import.py` pins the reason wording).

`build_trades` takes these keyword options; every one defaults to the legacy
behaviour, and a brokerage profile opts in:

| option | when set | legacy default |
| --- | --- | --- |
| `institution` | prefixes every reason `"<label>: "` and fills `UnmappedRow.institution` | `None` — wording and field exactly as before |
| `tolerant` | adds the second, normalised action-token lookup stage | off — exact match only |
| `require_quantity` | a zero-quantity share row becomes an unmapped row | off |
| `date_formats` | replaces the best-effort date parser with the profile's pinned, self-delimiting formats (a mismatch names them) | `None` — best effort |
| `require_currency` | an empty currency cell becomes an unmapped row instead of defaulting to `USD` | off |
| `abs_magnitudes` | a TRADE's commission is stored positive (a broker that writes a negative IBCommission still means a cost). Since M131 it no longer rewrites a cash-only row's amount: `fee`/`withdrawal` keep the file's negative sign and a cash transfer keeps the magnitude M113 already signed from `net_cash_amount` — the cash effect (`-abs`, docs/05) is direction-safe either way | off |

### Action vocabulary and the tolerant lookup

Each profile owns an `actions` map of `(activity_type, activity_sub_type) →
TradeAction`. Lookup is two-stage, in this order:

1. **exact** — the raw strings must match a key verbatim;
2. **normalised** — both sides are uppercased with every non-alphanumeric run
   collapsed to a single space (`"Buy - Express"` → `"BUY EXPRESS"`,
   `"TRANSFER_FEE_REIMBURSEMENT"` → `"TRANSFER FEE REIMBURSEMENT"`);
3. otherwise the row is **unmapped**.

Stage 2 is what makes casing and label decoration non-fatal without ever
guessing at a word that is not in the map: a Wealthsimple express-order row
written `Buy - Express` resolves because `("BUY EXPRESS", None)` is a pinned
key, while `Reorganization` resolves to nothing and is reported. Keys are
pinned per profile below; adding a word is a config change with a fixture row.

### The registry

`backend/app/importers/brokerage_profiles.py` exports:

```python
BROKERAGE_PROFILES: dict[str, BrokerageProfile]   # slug -> profile
PROFILE_SLUGS: tuple[str, ...]                    # stable order, picker + docs
get_profile(slug: str) -> BrokerageProfile | None
detect_profile(raw: bytes | str) -> BrokerageProfile | None
parse_brokerage_csv(raw: bytes, slug: str | None, *,
                    account_currency: str) -> BrokerageFile | None
```

`parse_brokerage_csv` returns `None` only when `slug` is empty **and** nothing
detects; an explicit slug the registry does not hold raises
`UnknownProfileError`. `account_currency` is the target account's currency —
the value the `row_column` fallback and `target_account` rules resolve to.

`BrokerageFile` is `(profile, rows: list[RawTrade], unmapped: list[UnmappedRow], notices: list[str])`.

`BrokerageProfile` fields (frozen dataclass): `slug`, `label`, `reader`
(`"table"` | `"ibkr"`), `columns: Mapping[str, str | tuple[str, ...]]`
(canonical key → source header; a tuple is an ordered candidate list for
string fields and a SUM for numeric ones), `required_headers: tuple[str,
...]`, `actions: Mapping[tuple[str, str | None], TradeAction]`, `date_formats:
tuple[str, ...]`, `action_column: str`, `currency_rule` (`"row_column"` |
`"file_constant"`), `currency_column`, `currency_constant`,
`commission_sum: tuple[str, ...]`, `require_currency: bool`,
`require_quantity: bool`, `corporate_action_tokens: frozenset[str]`,
`sections: Mapping[str, SectionSpec]`, `ignored_sections: frozenset[str]`,
`strict_sections: frozenset[str]`, `section_messages: Mapping[str, str]`,
`notices: tuple[str, ...]`.

The institution written to `import_batches.institution` is `profile.label` —
there is no separate field, so the two can never disagree. `SectionSpec`
describes one IBKR statement section: its column map, a fixed `action` or an
`action_column`, `sign_from_quantity` (IBKR `Trades` has no Buy/Sell column),
an optional `symbol_pattern` for dividends whose symbol lives only in the
description, and an optional `code_column` that marks corporate actions.

The module also exports `ProfileError` and its three subclasses —
`UnknownProfileError` (`unknown_import_profile`), `ProfileHeaderMismatchError`
(`profile_header_mismatch`), `UnknownCorporateActionError`
(`unknown_corporate_action`) — each carrying the `code` `api/trades.py`
envelopes, plus the empty `CORPORATE_ACTION_FORMS` extension point.

#### Profiles in scope

| slug | `label` | export recipe (pinned in `/help/brokerage-imports`) | required headers | date formats |
| --- | --- | --- | --- | --- |
| `questrade` | Questrade | Reports → Account Activity → all activity types → Download (CSV) | `Transaction Date`, `Action`, `Symbol`, `Quantity`, `Price` | `%Y-%m-%d`, `%Y/%m/%d`, `%d-%b-%Y`, `%b %d, %Y` |
| `wealthsimple` | Wealthsimple | Activity → Download activities → CSV | `activity_type`, `effective_date`, `net_cash_amount` | `%Y-%m-%d`, `%Y/%m/%d` |
| `ibkr` | Interactive Brokers | Performance & Reports → Flex Queries → Activity Flex Query, **Format = CSV** | flat: `Symbol`, `DateTime`, `Quantity`, `TradePrice`, `CurrencyPrimary`; sectioned: first field `Statement` + second field `Header` | `%Y%m%d`, `%Y%m%d;%H%M%S`, `%Y-%m-%d`, `%Y-%m-%d %H:%M:%S` |
| `fidelity` | Fidelity (US) | Accounts & Trade → Activity & Orders → Download CSV | `Run Date`, `Action`, `Symbol`, `Quantity` | `%m/%d/%Y`, `%Y-%m-%d` |
| `td-di` | TD Direct Investing | WebBroker → Accounts → Activity → Export (CSV) | `Trade Date`, `Action`, `Symbol`, `Net Amount` | `%Y-%m-%d`, `%d-%b-%Y`, `%b %d, %Y` |
| `rbc-di` | RBC Direct Investing | My Portfolio → Account Activity → Download (CSV) | `Date`, `Activity Type`, `Symbol`, `Quantity` | `%Y-%m-%d`, `%d-%b-%Y` |
| `nbdb` | National Bank Direct Brokerage | Account → Transaction History → Download (CSV) | `Date`, `Type`, `Symbol`, `Quantity` | `%Y-%m-%d`, `%d-%m-%Y`, `%d-%b-%Y` |

`NBDB` is the sixth M82 scope item on its own (its French/English vocabulary
justifies a profile); `Interactive Brokers Canada` folds into `ibkr` — IBKR's
Canadian accounts export the identical layout, only `CurrencyPrimary` reads
`CAD`.

**Date formats are never guessed.** Every format a profile accepts is
self-delimiting (ISO, `15-Mar-2026`, `Mar 15, 2026`, `yyyyMMdd`, US
`MM/DD/YYYY` for the US broker only). A `dd/mm/yyyy` vs `mm/dd/yyyy` ambiguity
is **not** resolvable from data alone, so no Canadian profile accepts a slash
date at all: such a row becomes an unmapped row naming the accepted formats
rather than a silently swapped day and month. Formats are tried in the listed
order; first `strptime` match wins.

#### Column maps

Canonical key → source header. Headers are matched after trimming and
case-folding, so `Price ($)` and `price ($)` are the same column. Extra
columns in the file are ignored.

| canonical | questrade | wealthsimple | ibkr (flat flex) | fidelity | td-di | rbc-di | nbdb |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `date` | `Transaction Date` | `effective_date` | `DateTime` | `Run Date` | `Trade Date` | `Date` | `Date` |
| `action` | `Action` | `activity_type` | `Buy/Sell` | `Action` | `Action` | `Activity Type` | `Type` |
| `sub` | `—` | `activity_sub_type` | `—` | `—` | `—` | `—` | `—` |
| `symbol` | `Symbol` | `symbol` | `Symbol` | `Symbol` | `Symbol` | `Symbol` | `Symbol` |
| `name` | `Description` | `name` | `Symbol` | `Security Description`† | `Description` | `Description` | `—` |
| `currency` | `Currency` | `currency` | `CurrencyPrimary` | *none* | `Currency` | `Currency` | `Currency` |
| `description` | `Description` | `description` | `Code` | `Description`† | `Description` | `Description` | `Description` |
| `quantity` | `Quantity` | `quantity` | `Quantity` | `Quantity` | `Quantity` | `Quantity` | `Quantity` |
| `unit_price` | `Price` | `unit_price` | `TradePrice` | `Price ($)` | `Price` | `Price` | `Price` |
| `commission` | `Commission` | `commission` | `IBCommission` | `Commission ($)` | `Commission` | *none* | `Commission` |
| `commission_sum` | `—` | `—` | `—` | `Fees ($)` | `—` | `—` | `—` |
| `amount` | `Net Amount` | `net_cash_amount` | `—` | `Amount ($)` | `Net Amount` | `Settlement Amount` | `Total` |
| `account` | `Account` | `account_id` | `Account` | `Account` | *none* | *none* | *none* |

† Fidelity ships either `Security Description` or `Description`; the profile
takes whichever header the file carries (declared as an ordered candidate list
— the first present wins). `commission_sum` is added to `commission` after
parsing (Fidelity reports them in two columns).

Missing optional columns are simply absent. A missing **required** header on a
file the user asked for by slug → `422 profile_header_mismatch` naming the
absent headers (see Errors).

#### Detection and the legacy Wealthsimple fallback

`detect_profile` matches the header against every profile in `PROFILE_SLUGS`
order and returns the first whose `required_headers` all resolve; a header
whose second field is literally `Header` is IBKR's sectioned statement and
short-circuits to `ibkr`. An empty or unparseable file returns `None`. The
endpoints then apply one deliberate rule:

- a **detected `wealthsimple` file with no `?profile=`** runs the *legacy*
  `parse_investment_csv`, not the profile. Existing Wealthsimple imports stay
  byte-identical — same rows, same `unmapped` reason strings, `profile: null`,
  `institution: null` — which `tests/test_investment_import.py` pins.
- **any other detected slug, or any explicit `?profile=`**, runs that profile.
- an undetectable file also falls back to the legacy parser.

The consequence is stated rather than hidden: the legacy parser is exact-match
only, so a Wealthsimple express order (`activity_type=Buy`,
`activity_sub_type=Express`) is *reported* as `Unknown activity: 'Buy' /
'Express'`, while the opted-in `wealthsimple` profile resolves it to `buy`
through `_WS_EXTRAS`. That is M82's evidence note made concrete — the fix is
opt-in so nobody's existing import changes shape underneath them, and the
Imports wizard's **Broker export** picker sends the slug every time.

### Multi-currency rules (explicit, per profile, never per row)

`RawTrade.currency` feeds `securities.listing_currency` on auto-create and is
the currency the ACB/fx math treats the row's native amounts as (docs/06,
docs/07). A row never gets to vote on its own currency implicitly — each
profile declares one of exactly three rules:

| rule | meaning | used by |
| --- | --- | --- |
| `row_column:<header>` | Read the row's own ISO-4217 cell. Blank or malformed → **the target account's currency** (the row is kept, `require_currency=false`) or an **unmapped row** (`require_currency=true`). | `questrade` (`Currency`), `wealthsimple` (`currency`), `ibkr` (`CurrencyPrimary`), `td-di` (`Currency`), `nbdb` (`Currency` — frequently blank → account), `rbc-di` (`Currency`) |
| `file_constant:<ISO>` | The file never states a currency and every row is in that currency **regardless of which account it is imported into**. | `fidelity` → `USD` |
| `target_account` | The file never states a currency and the row's currency is the selected account's — chosen **once, at import**, from `accounts.currency`. | `rbc-di` when its `Currency` column is absent |

Consequences that are asserted by tests:

- A `file_constant` USD row imported into a **CAD** account still creates the
  security with `listing_currency == "USD"` and its CAD conversion goes
  through `fx_rates` (BoC wins, manual fills gaps only — docs/07); the
  account's currency never leaks into the row.
- A `row_column` row with a blank cell on a `require_currency=true` profile
  becomes an unmapped row; on `false` it is kept and falls back to the
  account.
- `target_account` is applied once per import, so one file can never mix two
  currencies on that path.

**One file, one currency side for TD.** TD publishes separate CAD and USD
sub-account Activity exports; the guide says to export both and import them as
two files against the matching account. Nothing in this doc merges them.

### Corporate actions — parse strictly, refuse unknown forms

AGENTS §3 arbitration principle: strict validation beats silent tolerance for
financial data.

`CORPORATE_ACTION_TOKENS` per profile is the pinned set of words that mean
"this row is a corporate action":

- `ibkr`: every row in the `Corporate Actions` section, plus any `Trades`
  row whose `Code` is `SPLIT`, `MERGER`, `SPINOFF`, `SPIN OFF`, `REORG`,
  `REORGANIZATION`, `CONVERSION`.
- `td-di`: `Action` in `SPLIT`, `STOCK SPLIT`, `REVERSE SPLIT`, `MERGER`,
  `MERGE`, `SPINOFF`, `SPIN OFF`, `REORG`, `REORGANIZATION`, `CONVERSION`.

docs/06 §Corporate actions pins **v1 as manual `corporate_action_adjustment`
entries** and defines the only two accepted shapes: `quantity > 0` = the
resulting absolute share count after the action, `quantity = 0` with
`price_native` = a signed CAD ACB delta. **No published IBKR or TD export
layout carries either value** — the share count would have to be reconstructed
from a position the file may not contain, which is exactly the silent-wrong
failure this app refuses to ship.

Therefore M82 imports **no** corporate-action row:

- Any corporate-action row raises `UnknownCorporateActionError` →
  **`422 unknown_corporate_action`**, naming the physical line, the
  institution, the raw form, and the manual alternative. The whole file is
  rejected — a partially imported history is worse than a refused one.
- `CORPORATE_ACTION_FORMS` (a `dict[(slug, token) -> handler]`, empty in M82)
  is the pinned extension point. A future export that does carry a docs/06
  value gets one entry there and starts importing with no parser change;
  until then every form is an unknown form and answers 422.

The export recipes in `/help/brokerage-imports` therefore tell IBKR users to
leave `Corporate Actions` **out of** the Flex Query for the routine file, and
tell TD users to expect a refusal when the window contains a reorganization.

### Endpoints

Both are on `/api/v1/trades`, rate-limited with the existing
`IMPORT_UPLOAD_LIMIT` (`6/minute`), and share the cheapest-first validation
chain docs/03 pins for ingestion (Content-Length/byte cap 5 MB → CSV line caps
→ parse → 2000-row cap).

On **both** routes the body is `multipart/form-data` with exactly two fields,
and `profile` travels as a **query parameter** (`?profile=<slug>`), never as a
form field:

| field | transport | type | required | notes |
| --- | --- | --- | --- | --- |
| `file` | form | file | yes | the broker CSV |
| `account_id` | form | int | yes | an investment-capable account owned by the caller, else `404 not_found` / `422 account_not_investment` |
| `profile` | query | str | no | slug from `PROFILE_SLUGS`; absent/empty → auto-detect from the header, then the legacy Wealthsimple fallback |

#### `POST /trades/import/preview` — dry run, zero writes

Response `201` → `TradeDryRunResponse`:

```json
{
  "profile": "questrade",
  "rows": [
    {"index": 0, "date": "2026-01-15", "symbol": "VEQT", "security_name": "Vanguard All-Equity ETF",
     "action": "buy", "quantity": "10", "price_native": "40.00", "commission": "4.95",
     "currency": "CAD", "verdict": "new", "security_action": "existing"}
  ],
  "unmapped": [
    {"line": 7, "activity_type": "Reorganization", "activity_sub_type": null, "symbol": "ABC",
     "date": "2026-02-01", "reason": "Questrade: Unknown activity: 'Reorganization' / None",
     "institution": "Questrade", "code": null}
  ],
  "notices": ["Questrade: Settlement Date is read and ignored — the trade date drives every calculation."],
  "summary": {"total": 6, "new": 5, "duplicates": 1, "unmapped": 1}
}
```

- `index` is the row's position among parsed trades (0-based), not a database
  id — nothing was inserted.
- `verdict` ∈ `new | duplicate`, derived by `_trade_dedup_hash` over
  `(account_id, security_id, date, action, quantity, price_native)` against
  committed **and** pending rows of that account, exactly as the staging loop
  computes it.
- `security_action` ∈ `existing | create`. Preview never calls
  `market_data.lookup_symbol` and never inserts a `securities` row; a symbol
  with no row yet is reported `create`.
- `notices` are per-FILE caveats (below), not rows.

**Zero-write guarantee.** The handler only selects. If a symbol needs creating
it is reported, not created; no `securities`, `trades` or `import_batches`
row appears.

#### `POST /trades/import` — stage (unchanged route, one new optional field)

Same multipart fields as before; `profile` is the same **query** parameter the
dry run takes. Response `201` →
`TradeImportResponse`, now:

```json
{
  "batch_id": 41, "profile": "questrade", "institution": "Questrade",
  "imported": 5, "duplicates_skipped": 1,
  "preview": [{"id": 91, "date": "2026-01-15", "symbol": "VEQT",
               "security_name": "Vanguard All-Equity ETF", "action": "buy",
               "quantity": "10", "price_native": "40.00", "commission": "4.95",
               "currency": "CAD", "duplicate": false}],
  "unmapped": [ … same shape as the dry run … ],
  "notices": [ … ]
}
```

New/changed fields on `TradeImportResponse`: `profile` (slug or `null` when
the legacy parser ran), `institution` (`profile.label` or `null`),
`notices: list[str]`; `TradePreviewRow` gains `currency: str`.
`batch_id`, `imported`, `duplicates_skipped`, `preview`, `unmapped` keep their
meaning. `institution` is written to `import_batches.institution`, so the
Imports history reads `Questrade` instead of `investment_csv` for a profiled
import (legacy un-profiled imports still write `investment_csv`).

`POST /trades/import/{batch_id}/commit` and `…/discard` are unchanged
(docs/03).

#### `POST /trades/import/preview` verdict parity

The dry run and the staging call share ONE decision function,
`app/api/trades.py::_plan_trade_rows`, so a row reported `new` by the dry run
is staged as `imported` and a row reported `duplicate` is staged with a salted
hash and `is_duplicate=true`. The only difference is that staging may create
the missing `securities` rows first; because a symbol with no `securities` row
can have no prior `trades` row in that account, a creation-bound row can never
already be a duplicate. `tests/test_brokerage_profiles.py::test_dry_run_matches_staged_verdicts`
asserts the parity for every fixture.

### Notices (per file, never a row)

`notices: list[str]` carries caveats that apply to the whole file. Each starts
with `"<label>: "`. They are informational — they never block an import:

| profile | n | notice (pinned string, verbatim) |
| --- | --- | --- |
| `questrade` | 1 | `Questrade: Settlement Date is read and ignored — the trade date drives every calculation.` |
| `wealthsimple` | 2 | `Wealthsimple: non-resident tax (NRT) imports as a FEE against the gross dividend — the dividend row keeps its gross amount and the withholding shows as its own row, so no net dividend is fabricated.` (M113, docs/03 §Wealthsimple activity vocabulary), then `Wealthsimple: a MoneyMovement/EFT row is a contribution or a withdrawal according to its description ("Deposit" / "Withdrawal" / "Money transfer into|out of the account"); with no description the amount's sign decides, and a negative contribution is refused rather than imported.` (M131) |
| `ibkr` | 1 + 1 per excluded section | `Interactive Brokers: run the Flex Query with Format = CSV; the Activity Statement layout is read block by block.` then, for every section present but not imported, `Interactive Brokers: section 'Withholding Tax' is reported on your tax slip and is not imported (1 rows).` Sections `Statement` and `Account Information` are file furniture and never produce a notice |
| `fidelity` | 1 | `Fidelity (US): this export states no currency — every row is USD regardless of the account you import it into.` |
| `td-di` | 1 | `TD Direct Investing: the CAD and USD sides of an account are separate Activity exports — import each against its matching account.` |
| `rbc-di` | 1 | `RBC Direct Investing: this export has no Commission column — trades import with commission 0.00, so check them against your statement.` |
| `nbdb` | 1 | `National Bank Direct Brokerage: French exports are semicolon-separated and are read as-is; rows with no Currency fall back to the account you import into.` |

The counts are asserted per fixture; the `"<label>: "` prefix is asserted on
every one. Notice copy is user-facing (docs/09 "No internal jargon in copy"),
so no notice carries a spec pointer.

### Unmapped rows (per row, never silent)

A row that cannot become a `RawTrade` is returned as `UnmappedRow`
(`line`, `activity_type`, `activity_sub_type`, `symbol`, `date`, `reason`,
`institution`, `code`) — the same shape the Wealthsimple path already returns,
plus `institution` and `code`. With a profile set, `reason` is prefixed
`"<label>: "` so two institutions' rows are distinguishable on sight. Causes:

- unknown action word (not in the profile's `actions` map after both lookups)
- unparseable date (none of the profile's `date_formats` matched)
- unparseable/absent numeric field, or a row whose field count does not match
  its header
- `require_quantity` violated: `buy` / `sell` / `drip` / `transfer_in` /
  `transfer_out` with a zero or missing quantity — **except** the WS
  `MoneyMovement` cash-transfer keys, whose file quantity is cash, not shares
  (M113, docs/03 §Wealthsimple activity vocabulary)
- `require_currency` violated: blank or malformed ISO token
- non-cash or transfer action with an empty symbol — again with the same
  `MoneyMovement` cash-transfer exception: those rows are cash and stage
  against the `CASH` sentinel security

**`code` splits those into two dispositions.** `code: null` is a row-level
skip: the file stages and the row is listed with its reason. `code: "stuck"`
means staging would bake in a guess or leave a hole, so:

- the **dry run** still returns the row — that is how the user sees what is
  wrong — carrying `code: "stuck"`;
- **`POST /trades/import` answers `422 parse_error`** naming the first stuck
  line and stages nothing:
  `line 3: Unparseable date: 'not-a-date' (expected one of %Y-%m-%d, …) — 1 row(s) cannot be imported; fix the export rather than accept a guessed value`.

`_UNIMPORTABLE_ROW` in `app/api/trades.py` is the pinned matcher: an
unparseable date, an invalid `quantity` / `unit_price` / `commission` /
`net_cash_amount`, a ragged row, or an IBKR statement with no `Header` row.
Every other cause stays a row-level skip. A corporate action (§6 below) is
stricter still: the parser raises it before any row exists, so the file never
reaches the staging loop at all.

### Fixture contract and the commit-time balance invariant

Fixtures live in `backend/tests/fixtures/brokerage/`: **13 CSVs, 9 of them
carrying a sidecar**. The 9 are the per-institution smokes (one per profile,
plus the French NBDB twin); the 4 without one are refusal fixtures.

**Smoke fixtures (sidecar present):**

| file | contents |
| --- | --- |
| `questrade_activity.csv` | deposit, buy, dividend, sell, plus one unknown action (`Reorganization`) that must be listed, not imported |
| `wealthsimple_activity.csv` | buy, express-order buy (`Buy,Express`), DRIP dividend, CESG-style contribution, unknown activity (`Foo/WEIRD`) |
| `ibkr_flex.csv` | flat Flex Query — `Symbol,DateTime,Quantity,TradePrice,CurrencyPrimary,Buy/Sell,IBCommission,Code,Account`: a CAD buy, a USD buy, a negative-quantity CAD sell whose `IBCommission` is written `-4.95` |
| `ibkr_statement.csv` | sectioned Activity Statement — `Statement`/`Account Information` furniture, `Trades` (2), `Dividends`, `Deposits & Withdrawals`, `Fees`, and a `Withholding Tax` block excluded **with** a notice |
| `fidelity_activity.csv` | US buy/sell with commission split across `Commission ($)` + `Fees ($)`, a dividend, an unmapped `REINVESTMENT` |
| `td_activity.csv` | `1E+1`-style quantity, a multi-line quoted description, a dividend, a contribution |
| `rbc_activity.csv` | deposit, buy, dividend, sell with **no** commission column |
| `nbdb_activity.csv` | deposit, buy, dividend, sell in English, with one blank `Currency` cell |
| `nbdb_activity_fr.csv` | the same file in French and semicolon-separated (`Achat`/`Vente`/`Dividende`/`Dépôt`, `Quantité`, `Prix`, `Devise`, `,` decimals) |

**Refusal fixtures (no sidecar — the 422 *is* the expected result):**

| file | must answer |
| --- | --- |
| `questrade_bad_date.csv` | dry run lists the row with `code: "stuck"`; `POST /trades/import` → `422 parse_error` naming line 3 |
| `questrade_bad_number.csv` | same, for `Quantity = ten` |
| `ibkr_corporate_actions.csv` | `422 unknown_corporate_action` (section `Corporate Actions`, a `Split`) |
| `td_corporate_action.csv` | `422 unknown_corporate_action` (`Action = SPLIT`) |

Each smoke CSV has a sidecar `<name>.expected.json` — **the fixture's own
stated totals**, which the test recomputes from the database and compares:

```json
{
  "profile": "ibkr",
  "rows": 3, "imported": 3, "duplicates_skipped": 0, "unmapped": 0,
  "notices": 1,
  "positions": {"SHOP": "6", "AAPL": "5"},
  "cash_effect": {"CAD": "-869.90", "USD": "-1001.00"},
  "account_currency": "CAD"
}
```

`account_currency` is the account the smoke imports into (every fixture uses
CAD); `positions` omits symbols that net to zero, and `cash_effect` omits
currencies that net to zero.

**Commit-time balance invariant.** After
`POST /trades/import/{id}/commit`, both maps are recomputed **from the
database**, never from parser output, and must equal the sidecar:

- `positions[symbol]` = `Σ trades.quantity` over the account's committed rows
  for that symbol (signed: sells negative).
- `cash_effect[currency]` = `Σ services/total_return.py::_trade_cash_effect(trade)`
  grouped by `securities.listing_currency` — the single source of the docs/05
  cash-effect table, so the invariant cannot drift from the report engine.

`duplicates_skipped` and `rows` are likewise recomputed from
`import_batches` and the staged rows. A second import of the same fixture
must report `net-new = 0`.

### Fixtures are reconstructed, not captured — stated plainly

M82's original ask was "a golden fixture exported from real documents
(redacted)". No real customer export was available to this work, so **every
fixture is reconstructed from published column layouts** and each profile's
`docs/41` table above cites the layout it was built from. They are faithful to
the published headers, dates and action words but they are not byte-captures
of a broker's file. Replacing one with a redacted real export is a fixture
change only — the profiles are pure config, so no code moves.

## Touch points

| file | change |
| --- | --- |
| `backend/app/importers/brokerage_profiles.py` | **new** — the registry, readers, canonical adapter, currency rules, corporate-action gate |
| `backend/app/importers/investment_profile.py` | `_read_investment_rows` takes `(line, dict)` pairs + `institution` + `require_quantity`; `parse_investment_csv` unchanged for callers |
| `backend/app/api/trades.py` | `_plan_trade_rows` (shared verdict/security decision), `POST /trades/import/preview`, `profile` on `POST /trades/import`, `UnknownCorporateActionError` → 422 |
| `backend/app/schemas/trades.py` | `TradeDryRunResponse`/`Row`/`Summary`, `UnmappedRow.institution`, `TradeImportResponse.{profile,institution,notices}`, `TradePreviewRow.currency` |
| `backend/tests/test_brokerage_profiles.py` | **new** — per-institution dry-run → stage → commit → invariant |
| `backend/tests/fixtures/brokerage/*` | **new** — 13 CSVs: 9 smoke fixtures with `.expected.json` sidecars + 4 refusal fixtures |
| `frontend/src/api/trades.ts` | `profile` field + `importTradesDryRun` |
| `frontend/src/pages/Imports.tsx` | per-institution picker in the investment mode block; render `notices` |
| `frontend/src/pages/helpGuides.ts` | **new** guide `brokerage-imports` — one export recipe per institution |
| `site/guides/brokerage-imports.html` | static twin (docs/32 copy-drift rule) |
| `site/index.html` | the guide's card in the static index (removed by the t_fe44260e landing rebuild; the static twin `site/guides/brokerage-imports.html` remains) |
| `docs/02`, `docs/03`, `docs/05`, `docs/06`, `docs/08`, `docs/10` | cross-references, reconciled in the same commit |

**Unchanged by design:** the export registry
(`tests/test_export_registry.py`), the `import_batches` construction sites
(`grep -c 'ImportBatch('` still returns exactly `demo_seed.py:1`,
`import_staging.py:1`, `api/trades.py:1`), `services/import_staging.py`,
migrations, and the `institutions.importer_profile` CSV column-mapping path.

## Error envelope

Every failure is `{detail, code}` (docs/08). Codes this doc adds or pins:

| status | code | when |
| --- | --- | --- |
| 422 | `unknown_import_profile` | `profile` is not a slug in `PROFILE_SLUGS` |
| 422 | `profile_header_mismatch` | an explicit `profile` was given and the file lacks one of its `required_headers` — detail names the missing headers and the detected header line |
| 422 | `unknown_corporate_action` | a corporate-action row reached `CORPORATE_ACTION_FORMS` with no entry — detail names line, institution, raw form, and the manual docs/06 alternative |
| 422 | `parse_error` | two shapes: (a) unchanged — no header row, `csv.Error`, malformed profile-driven CSV; (b) M82's stuck-row refusal — at least one unmapped row carries `code: "stuck"`, and the detail names the first such line plus how many there are |
| 413 | `file_too_large` / `too_many_rows` | unchanged (5 MB / 2000 rows) |
| 404 | `not_found` | unknown or foreign `account_id` |
| 422 | `account_not_investment` | target account is not in `INVESTMENT_ACCOUNT_TYPES` |
| 429 | `rate_limited` | unchanged, `6/minute` per endpoint |

An `unknown_corporate_action` is raised **before** any row is staged, so the
rejection leaves `securities`, `trades` and `import_batches` untouched.

## Verification

`backend/tests/test_brokerage_profiles.py` — **34 tests, run with
`cd backend && uv run pytest -q tests/test_brokerage_profiles.py`.**
`backend/tests/test_m131_regression_sweep.py` — **3 tests** that sweep the
whole committed corpus as ONE user (see "The M131 regression sweep" below),
`cd backend && uv run pytest -q tests/test_m131_regression_sweep.py`.

### Per-institution smoke (`test_per_institution_smoke`, one per fixture)

For each of the 9 smoke fixtures: dry run (zero writes, asserted by row counts
before/after) → `POST /trades/import` → `POST /trades/import/{id}/commit` →
recompute `positions` and `cash_effect` **from the database** → assert
equality with the sidecar → re-import the same file and assert `net-new = 0`.

| fixture | profile | staged → imported | dup | unmapped | notices | positions | cash effect |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `questrade_activity.csv` | `questrade` | 4 → 4 | 0 | 1 | 1 | SHOP 6 | CAD 10 000.00 · USD −857.40 |
| `wealthsimple_activity.csv` | `wealthsimple` | 4 → 4 | 0 | 1 | 0 | VEQT 10.25 · SHOP 2 | CAD −610.00 |
| `ibkr_flex.csv` | `ibkr` | 3 → 3 | 0 | 0 | 1 | SHOP 6 · AAPL 5 | CAD −869.90 · USD −1 001.00 |
| `ibkr_statement.csv` | `ibkr` | 5 → 5 | 0 | 0 | 2 | SHOP 6 | CAD 9 136.10 |
| `fidelity_activity.csv` | `fidelity` | 3 → 3 | 0 | 1 | 1 | AAPL 6 | USD −845.10 |
| `td_activity.csv` | `td-di` | 4 → 4 | 0 | 0 | 1 | SHOP 6 | CAD 4 132.52 |
| `rbc_activity.csv` | `rbc-di` | 4 → 4 | 0 | 0 | 1 | SHOP 6 | CAD 9 152.50 |
| `nbdb_activity.csv` | `nbdb` | 4 → 4 | 0 | 0 | 1 | SHOP 6 | CAD 9 138.52 |
| `nbdb_activity_fr.csv` | `nbdb` | 4 → 4 | 0 | 0 | 1 | SHOP 6 | CAD 9 138.52 |
| **total** | 7 profiles | **35 → 35** | 0 | **3** | **9** | | |

The account is CAD for every fixture, so the USD lines above are also the
proof that `file_constant` / `row_column` currencies survive a CAD account —
`fx_rates` is seeded for the whole fixture date span (`fx_cache` fixture) so
the ACB rebuild at commit time resolves every non-CAD trade instead of
answering `422 fx_rate_missing`.

### The rest of the suite

| # | test | claim |
| --- | --- | --- |
| 1 | `test_dry_run_matches_staged_verdicts[×9]` | dry-run `verdict` sequence == staged `duplicate` sequence, on a clean account and again after the first import |
| 2 | `test_dry_run_writes_nothing` | `securities` / `trades` / `import_batches` counts byte-identical across a dry run of all 13 fixtures; the 2 stuck files still list their row with `code: "stuck"` |
| 3 | `test_unmapped_rows_carry_the_institution` | every unmapped row's `institution` is the profile label, its `reason` carries the `"<label>: "` prefix, and `total + unmapped == eligible data rows` for every fixture (no silent drops, made arithmetic) |
| 4 | `test_unparseable_rows_are_shown_then_refused` | dry run shows the bad row; `POST /trades/import` answers `422 parse_error` naming `line 3` |
| 5 | `test_fidelity_usd_survives_a_cad_account` | `file_constant` beats `accounts.currency`; `listing_currency == "USD"`; committed cash effect is USD −845.10 |
| 6 | `test_row_column_blank_currency_falls_back_to_account` | NBDB's blank `Currency` cell keeps the row and takes the account's CAD |
| 7 | `test_row_column_required_currency_rejects_the_row` | Questrade's blank `Currency` cell becomes an unmapped row naming the column |
| 8 | `test_unknown_corporate_action_is_422_and_writes_nothing[×2]` | IBKR section and TD `SPLIT` both answer `422 unknown_corporate_action` with line + manual alternative, and the row counts do not move |
| 9 | `test_detect_profile`, `test_detect_profile_stays_in_registry_order` | every fixture auto-detects to its own slug, an unknown header and an empty file detect to `None`, and `PROFILE_SLUGS` order is pinned |
| 10 | `test_explicit_profile_header_mismatch_is_422`, `test_unknown_import_profile_is_422` | `422 profile_header_mismatch` / `422 unknown_import_profile` |
| 11 | `test_auto_detection_without_a_slug` | the legacy path stays byte-identical to `parse_investment_csv` (3 imported, 2 unmapped), while `?profile=wealthsimple` resolves the express order (4 imported, 1 unmapped) |
| 12 | `test_export_registry_unchanged`, `test_importbatch_write_sites_are_pinned`, `test_profile_registry_is_pure_config` | the export registry still covers `SQLModel.metadata`, there are still exactly three `ImportBatch(` construction sites (`services/import_staging.py`, `services/demo_seed.py`, `api/trades.py`), and the profile registry never touches a session or a network client |
| 13 | §4 gates | `ruff format --check && ruff check`, `pytest -q` against real Postgres, `tsc --noEmit && eslint . --max-warnings 0`, `NODE_ENV=test vitest run`, `npm run build`, `docker compose up -d --build`, then the changed paths smoked through `http://localhost:8080` |

### The M131 regression sweep (`test_m131_regression_sweep.py`, 3 tests)

M131 (de14346) changed `investment_profile.build_trades` — the ONE place
trade-mutation semantics live, which every profile funnels through. The
per-institution smokes above prove each profile in ISOLATION (a fresh user
per fixture), which cannot see the state the profiles SHARE when one
household imports them all: the security rows, the `CASH` security, the
dedup hash, and the ACB rebuild that runs over the whole user on every
commit. `tests/test_m131_regression_sweep.py` runs the whole committed
corpus as ONE user, in registry order, and holds every fixture to its
sidecar — then pins the cross-profile state:

| test | claim |
| --- | --- |
| `test_committed_fixture_corpus_is_complete` | the sweep's file lists cover `tests/fixtures/brokerage/` **exactly**: every CSV on disk is classified (smoke + sidecar, refusal, or stuck) and every sidecar pairs a smoke fixture — a fixture committed without a classification fails instead of drifting untested |
| `test_all_committed_profiles_in_one_pass` | all 9 smoke fixtures import sequentially as one user, each against its own sidecar (counts, notices, unmapped, DB-recomputed `positions`/`cash_effect`, re-import net-new 0); the 2 refusal and 2 stuck fixtures answer their 422s in the same pass with row counts proving nothing moved; then the shared book: exactly one `SHOP`/`AAPL`/`VEQT`/`CASH` security for the user, `SHOP.listing_currency` = the FIRST importer's (USD), the same security carrying both `USD` and `CAD` rows (M132 `trades.currency` keeps each row's own), and **every committed `fee`/`withdrawal` row stored NEGATIVE** — the M131 sign contract the sidecar's `-abs` cash effect is deliberately blind to |
| `test_cross_profile_dedup_is_currency_blind` | RBC's four rows — numerically identical to Questrade's, differing only in currency cells — import into Questrade's account as 0 new / 4 duplicates, and the committed book still holds exactly one SHOP position: `_trade_dedup_hash` spans `account\|security\|date\|action\|quantity\|price` and currency is deliberately not in it (docs/03) |

The stored-sign assertion exists because `cash_effect` is deliberately
`-abs` for `fee`/`withdrawal` (docs/05): a fee row stored back at its
file's positive amount produces the SAME sidecar total while the
transactions surface, exports and the event log render an inflationary
`+$6.50 fee` — the exact M131 defect. Verified by flipping the rule: the
sweep names `ibkr_statement.csv` and the stored value.

The EFT-direction rule (`cash_row_direction`) is WS-specific and has no row
in the committed profile corpus; it is pinned by
`tests/test_m131_ws_cash_signs.py` and `tests/test_m113_ws_cash_mappings.py`
(8 tests red when the rule is disabled).

The `fx_cache` fixture seeds USD/CAD daily across 2025-12-01 → 2026-04-30 on
the suite's own isolated database (conftest derives `<db>_test_<session-id>`),
which is the
isolation AGENTS §5 requires of any suite that writes `fx_rates`.
