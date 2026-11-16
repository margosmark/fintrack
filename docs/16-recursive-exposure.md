# 16 — Recursive Look-Through Exposure & Company Dimension (M19)

Look-through exposure that descends into the holdings of funds the user holds,
so a position in an ETF is attributed to the ETF's underlying companies /
sectors / geographies / asset classes rather than stopping at the fund. Adds a
4th breakdown dimension (`company`) and a background enrichment pass that pulls
composition + fundamentals for inner sub-funds so the recursion has rows to walk.

This is the authoritative spec for the recursive breakdown + enrichment
behavior. Canonical endpoint rows live in `docs/08` (`GET /portfolio/breakdown`,
`GET /portfolio/exposure`, `GET /securities/{id}/holdings`); this doc pins the
exact shapes, resolution order, recursion invariants, and enrichment service.

## Context

`GET /portfolio/breakdown` (M18) distributed a fund position across its direct
`etf_holdings` rows but never descended into *nested* funds — an ETF-of-ETFs
showed up as one ETF bucket. M19 makes the look-through recursive (depth-bounded
and cycle-safe) and adds a `company` dimension so exposure can be read by
underlying issuer. Because the recursion needs each inner fund's own
`etf_holdings` rows, a holdings import/PUT queues a background `deepen` task that
eagerly loads composition + fundamentals for the sub-funds it just wrote (capped,
privacy-safe).

Decisions (locked):
- **Recursion is bounded at 3 levels** (`MAX_DEPTH = 3`). A chain deeper than
  that is flattened: the deepest recursable fund contributes its own weight as a
  leaf bucket instead of descending further. The `visited` set prevents infinite
  loops on cyclic holdings (a fund that lists itself, or A↔B cross-holdings).
- **Full attribution, always.** At every level the denominator is
  `max(sum_of_weights, 100)` and any unallocated residual lands in `Unclassified`,
  so a position's entire market value is always distributed — nothing is dropped.
- **Resolution order (per row):** the row's own column → the matched security's
  `fundamentals` row → `Unclassified`. The `company` dimension never degrades to
  `Unclassified` (a holding always has a name); it uses `holding_symbol` or
  `holding_name`.
- **Only fund-kind securities recurse.** A holding descends only if its resolved
  security `kind ∈ {etf, mutual_fund}` AND that security has `etf_holdings` rows.
  A stock leaf is attributed via its `fundamentals` row (sector/geography/company).
- **No new migration.** The recursion link `etf_holdings.holding_security_id`
  already shipped in migration `0007_add_holding_security_id` (nullable FK →
  `securities.id`).
- **Sector is the one dimension with a wider chain (M166).** No provider
  publishes a sector for every sub-holding — the iShares issuer CSVs and the
  Vanguard CANADIAN gateway carry a GICS column per row, the Vanguard US
  profile JSON does not — so between the row's own column and `Unclassified`
  the `sector` bucket consults two more cache-only sources (the tenant's own
  cross-fund index and a static ticker map), and whatever an equity row still
  leaves unattributed inside a fund that PUBLISHES its own GICS mix is split
  across that mix and reported `estimated`. Geography / asset class / company
  are unchanged. See `## Sector chain (M166)`.

## Approach

### Backend — `app/services/exposure_breakdown.py`

`Dimension = Literal["sector", "geography", "asset_class", "company"]`
`MAX_DEPTH = 3`
`UNCLASSIFIED = "Unclassified"`
`_FUND_KINDS = frozenset({SecurityKind.etf, SecurityKind.mutual_fund})`

`breakdown(session, user_id, dimension, account_id=None) -> dict`:
1. Builds open positions via `valuation.compute_positions(session, user_id,
   account_id=account_id)`. Positions are grouped by `security_id` first, so the
   *same* security held across multiple accounts is looked through exactly once.
   A position with `market_value_cad is None` (no usable price) is skipped —
   nothing to attribute.
2. Preloads the whole tenancy (all `Security`, `EtfHolding`, `Fundamentals` rows
   for `user_id`) into indexes: `symbol_index` (upper symbol → securities),
   `holdings_by_fund` (security_id → rows), `fundamentals_by_security` — plus,
   for `dimension == "sector"` only, the tenant's own **cross-fund sector
   index** built from those same `EtfHolding` rows
   (`sector_map.build_sector_index`, see `## Sector chain (M166)`).
3. For each position security:
   - If `kind ∈ _FUND_KINDS` and it has holdings → `_distribute(...)` at `depth=1`,
     `visited={security_id}`, `fund_symbol=security.symbol`.
   - Elif `kind == stock` and it has a `fundamentals` row → bucket via
     `_fundamentals_bucket(fundamentals_row, dimension, security)`.
   - Else → `_fallback_bucket(security, dimension)` (stocks/other with no
     fundamentals → `Unclassified`, except `asset_class` degrades to the security
     `kind` and `company` degrades to the security `symbol`).
4. Emits `{items, total_cad, unclassified_cad, estimated_cad, unclassified_funds}`
   where each item is `{key, market_value_cad, weight_pct, estimated_cad,
   estimated}`:
   - `market_value_cad`, `total_cad`, `unclassified_cad`, `estimated_cad` are
     cents-quantized (`ROUND_HALF_EVEN`) `Decimal` values serialized as
     **strings** on the wire (e.g. `"1234.56"`), matching the frontend
     `BreakdownRow` / `BreakdownResponse` types. The mode is stated explicitly
     as `cents(value, rounding=ROUND_HALF_EVEN)` through the ONE canonical
     helper app-wide (docs/02 Conventions), never a private copy.
   - `weight_pct` is a `float` (2 dp, `ROUND_HALF_UP`) = `value / total_cad * 100`;
     `0.0` when `total_cad == 0`. The opposite mode from the money fields above
     is deliberate; both are pinned by
     `tests/test_money_rounding_single_source.py`, and reading `weight_pct` off
     the money helper (or the reverse) is exactly what that test catches.
   - **M166**: top-level `estimated_cad` is the total attributed by fund-mix
     inheritance; each item's `estimated_cad` is that bucket's own share of it
     and `estimated` is `estimated_cad > 0`. Both are `"0.00"` / `false` for
     every dimension other than `sector` and for any portfolio whose funds all
     publish their holdings per-row. A bucket can be part sourced and part
     estimated — the number, not the boolean, is what the UI's copy quotes.
   - `items` sorted by `market_value_cad` descending.

`_distribute(value, holdings, depth, visited, buckets, dimension, *, symbol_index,
holdings_by_fund, fundamentals_by_security, sector_index=None, estimated=None,
fund_symbol=None)`:
- `total_weight = sum(h.weight_pct)`. If `total_weight <= 0` → whole `value` to
  `_sink(...)`, return.
- `denominator = max(total_weight, 100)`; `residual = value − allocated` after the
  loop, and any `residual` goes to `_sink(...)`.
- Per holding: `amount = value * weight_pct / denominator`.
  - `_resolve_sub_security(holding, symbol_index, holdings_by_fund)` matches a
    user security by the row's `holding_symbol` (exact, case-insensitive; prefers
    a fund that itself has holdings → any fund → lowest id; never guesses exchange).
  - If the resolved security is a fund with rows AND `depth < MAX_DEPTH` AND not in
    `visited` → recurse at `depth + 1` with `visited | {sub.id}` and
    `fund_symbol = sub.symbol` (the estimate the walk descends into belongs to
    the INNER fund: `VUN`'s unattributed share inherits `VTI`'s mix, not `VUN`'s).
  - Else → bucket via `_row_bucket(holding, dimension, sub_fundamentals,
    sector_index)` where `sub_fundamentals` is the resolved security's
    `fundamentals` row (or `None`). A `sector` bucket that comes back
    `Unclassified` for an `asset_class == stock` row goes to `_sink(...)`;
    a cash / bond / commodity leg has no GICS sector and goes straight to
    `Unclassified`.

`_sink(value, dimension, fund_symbol, buckets, estimated)` — the M166 sink.
For `sector` only, and only when `fund_symbol` has a published mix in
`services.sector_map`, the amount is split across that mix renormalized
against the mix's own total, and every share it moves is recorded in
`estimated`. Because the sink only ever redistributes value that was going to
`Unclassified`, the M19 full-attribution rule is untouched: no bucket can grow
beyond what its parent received and the position's total is unchanged.
Everything else — non-sector dimensions, non-equity rows, funds with no
published mix — is `buckets[UNCLASSIFIED] += value` exactly as before.

Bucket resolution (per dimension):
- `company` → `holding.holding_symbol or holding.holding_name` (never `Unclassified`).
- `asset_class` → `holding.asset_class.value`.
- `sector` → the M166 chain (`_sector_bucket`): the row's own column, the
  cross-fund index, the static ticker map, `sub_fundamentals.sector`, then
  `Unclassified` — every step canonicalised to one vocabulary.
- `geography` → `holding.region or holding.country`, else
  `sub_fundamentals.country`, else the static company→country map (M147), else
  `Unclassified`.
- `_fundamentals_bucket`: `company`→`security.symbol`,
  `sector`→`canonical_sector(row.sector) or static_sector_for_symbol(security.symbol)
  or Unclassified`, `geography`→`row.country or country_for_symbol(security.symbol) or
  Unclassified`, `asset_class`→`"stock"`.

## Sector chain (M166)

`services/sector_map.py` holds the three cache-only sources the `sector`
bucket consults. None of them fetches — the breakdown stays a pure
aggregation over cached rows (AGENTS §2, docs/07 cache-only convention).

### One vocabulary

`canonical_sector(label) -> str | None` maps every provider spelling onto the
yfinance names (`Technology`, `Financial Services`, `Healthcare`, `Consumer
Cyclical`, `Consumer Defensive`, `Basic Materials`, `Communication Services`,
`Industrials`, `Energy`, `Real Estate`, `Utilities`) — the same discipline the
country map uses for `fundamentals.country`. iShares' `Information Technology`
and yfinance's `Technology` are ONE pie slice, not two. Idempotent, and it is
applied to composition rows, to `fundamentals.sector`, to the static tables
and to the payload keys, so no code path can reintroduce the split.

- `SECTOR_ALIASES: dict[str, str]` — lowercased provider label → canonical
  name. Deliberately only labels seen live in the three providers.
- `SECTOR_NON_SECTORS: frozenset[str]` — labels in a SECTOR column that are not
  sectors (`"Cash and/or Derivatives"`, `"Not Applicable"`, …). They resolve to
  `None`, so a cash leg falls through the chain instead of growing a pie slice
  nothing else can be compared against.
- An unrecognised label passes through **unchanged**: the provider published
  it and dropping it would silently shrink the pie. A new provider spelling
  needs a mapping entry, which is what the table's comment asks for.

### The cross-fund sector index

`build_sector_index(rows) -> SectorIndex` harvests, from the tenant's OWN
`etf_holdings` rows, every (symbol, country) → sector pair the providers
already published. The same listed company appears across many of a user's
funds, so a label that arrived with one of them answers for every other: the
S&P 500 rows imported from IVV's issuer CSV label exactly the tickers VTI's
Vanguard rows leave blank.

- Resolution order: `(symbol, country)` exactly, then the bare symbol **only
  when every labelled row carrying it agrees**.
- A bare ticker that names two different companies on two exchanges (`SU`,
  `IAG`, `KEY`, …) is dropped from the bare table entirely and survives only
  as an exact pair — the M143 lesson (a bare-ticker collision silently picked
  the wrong VGRO). Refusing beats guessing.
- Built per request from the same `all_holdings` list `breakdown` already
  loads, and only for `dimension == "sector"`, so tenancy and the other three
  dimensions are untouched.

### The static tables (M147 country-map pattern) — rows since M168

Both are **data** now: `provider_fund_registry` families `static_sector`
(28 rows) and `fund_sector_mix` (11 rows), seeded verbatim by migration
`0050_provider_fund_registry` from the literals they replace (docs/07
§Provider registry). `breakdown` loads the pair **once** per call —
`SectorMaps.load(session)`, one query each — and every sector lookup in the
walk is then a plain dict read; the other three dimensions never load them at
all. The lookups take the snapshot as an argument instead of reading a module
global, so a 25,000-row exposure walk never touches the database again.

- `SectorMaps.static_sector(symbol)` — the hand-curated ticker → sector table
  for the handful of names no provider in the cache has ever labelled (the
  mega-caps a total-market row spells differently, plus the exchange's
  bare-numeric listings). Matching is unchanged: exact, then a known exchange
  suffix stripped, then a slash share-class separator read as a dot
  (`BRK/B` → `BRK.B`). A miss is `None`.
- `SectorMaps.published_mix(symbol)` — a fund's OWN published GICS sector mix,
  for the US-domiciled equity funds whose profile publishes one and whose
  per-holding rows do not. It also resolves a venue-suffixed listing and
  returns a **copy** with `Decimal` values rebuilt from the row's decimal
  TEXT, so the split is arithmetic-identical to the old literals and a caller
  cannot mutate the table. Captured LIVE 2026-10-11 from
  `GET https://investor.vanguard.com/irr/funds/profile/{TICKER}` →
  `.portfolioComposition.weightedExposures.sectorExposure.long`; the published
  figures drift with the fund's rebalance, so a stale entry is a slightly
  wrong ESTIMATE, never a wrong fact — which is exactly what `estimated` is
  for. Only the funds answering `isavailable: true` with the 11-sector
  taxonomy belong here: Vanguard reports `isavailable: false` for every
  non-US-index product (VXUS, VT, VEA, VWO, BNDX), and the sector-specific
  funds (VGT, VHT, VNQ, VCR, VDC, VDE, VIS, VPU, VAW) publish SUB-INDUSTRY
  names, which would invent pie rows nothing else can be compared against.

Both families are deliberately incomplete: an unknown ticker or fund stays
`Unclassified`. Coverage grows by adding verified rows — `fintrackerctl
provider-funds set static_sector <TICKER> --sector <Sector>`, never a code
change — never by inferring a
sector from ticker text.

### Backend — `app/api/portfolio.py`

`GET /api/v1/portfolio/breakdown` (router prefix `/portfolio`, `docs/08` row):
- Query: `dimension: Literal["sector","geography","asset_class","company"]`
  (required), `account_id: int | None = None` (optional filter).
- Calls `exposure_breakdown.breakdown(db, user.id, dimension, account_id=...)`.
- Response: `{"dimension": dimension, "items": [...], "total_cad": ...,
  "unclassified_cad": ..., "estimated_cad": ..., "unclassified_funds": [...]}`.
- **Invalid `dimension` → 422 with code `validation_error`** (the standard
  `RequestValidationError` handler, `core/errors.py:validation_error_handler` →
  `{detail, code: "validation_error"}`). No other error codes; it is a
  tenant-scoped read.
- `account_id` is also accepted by `GET /api/v1/portfolio/exposure` (currency
  exposure, `docs/05`) and `GET /api/v1/portfolio/positions`.

### Backend — `app/api/securities.py`

`GET /api/v1/securities/{id}/holdings` (router prefix `/securities`):
- Query: `recursive: bool = False`.
- `security = owned_or_404(...)` → foreign id → 404 (standard envelope).
- Builds `EtfHoldingsBlock` via `_holdings_block(db, security, recursive=...)`.
  - Rows sorted by `weight_pct` desc then `id` asc.
  - `source` = majority provenance: `"manual"` when `manual_count * 2 >= len(rows)`,
    else `"yfinance"`; `null` when empty.
  - When `recursive=True`, for each fund holding whose `holding_security_id`
    resolves to a `kind ∈ {etf, mutual_fund}` security that has rows, the row is
    enriched with `asset_class_breakdown: dict[str, str]` (asset class →
    weight-% string) computed by the recursive `_compute_asset_class_breakdown`
    (same depth-3 / `visited`-set discipline, denominator `max(sum,100)`).
- Response `EtfHoldingsBlock` (`schemas/securities.py`):
  ```python
  class EtfHoldingRead(BaseModel):
      holding_symbol: str | None = None
      holding_security_id: int | None = None
      holding_name: str
      asset_class: str
      region: str | None = None
      country: str | None = None
      sector: str | None = None
      weight_pct: Decimal
      asset_class_breakdown: dict[str, str] | None = None   # recursive=true only

  class EtfHoldingsBlock(BaseModel):
      items: list[EtfHoldingRead] = []
      total_weight_pct: str
      source: str | None = None
  ```
- `POST /{id}/holdings/import` and `PUT /{id}/holdings` (M17) both call
  `queue_deepen(background_tasks, fund, symbols)` after the write — see Enrichment.

### Backend — `app/services/market_data.py` (enrichment)

`SUB_FUND_ENRICHMENT_CAP = 12`
`ensure_sub_fund_composition(session, fund, symbols, max_sub_funds=12) -> int`:
- Eagerly loads composition + fundamentals for up to `SUB_FUND_ENRICHMENT_CAP`
  distinct sub-fund tickers written by the triggering holdings import/PUT.
- For each distinct ticker (excluding the fund's own symbol): resolve the user's
  security by symbol; if absent, `lookup_symbol` (guarded) and create a `Security`
  of the reported fund kind; individual stocks are created for linking but have no
  sub-holdings to fetch.
- For a sub-fund with **no** existing `etf_holdings` rows: `fetch_fund_holdings`
  (guarded) and insert `source="yfinance"` rows (existing rows never touched);
  ensures its `fundamentals` row; backfills 1y price history for newly created
  securities. All network/fundamental failures degrade silently (logged warning).
- After the loop, backfills `holding_security_id` on touched funds where the
  symbol now resolves to a security.
- **Privacy mode:** the guard in `_guard_external_fetch` raises on any yfinance
  call. This function runs inside a BackgroundTask, so the caller wraps it and the
  whole enrichment degrades silently (no rows enriched, no 5xx) — consistent with
  the `docs/07` privacy discipline.

`queue_deepen` / `_run_deepen` (`app/api/securities.py`): dispatch
`ensure_sub_fund_composition` as a `BackgroundTasks` task with the loaded `fund`
and the tickers just written. The background task opens its own Session (request
sessions close before the task runs), which is why the caller passes both the
fund and the symbols rather than relying on the request's uncommitted rows.

### Models — `app/models/etf_holdings.py`

`etf_holdings` (created `0006_etf_holdings`; recursion link added `0007`):
| column | type | invariant |
|---|---|---|
| `id` | int PK | |
| `user_id` | int FK→users.id | tenant scoping |
| `security_id` | int FK→securities.id | the fund this row belongs to |
| `holding_security_id` | int \| None FK→securities.id | **recursion link** to the underlying security (nullable — not every holding is a tracked security); index `ix_etf_holdings_holding_security_id` |
| `holding_symbol` | str \| None, max_length=20 | ticker used to resolve `holding_security_id` / recursion |
| `holding_name` | str, max_length=160 | always present (company fallback) |
| `asset_class` | `EtfAssetClass` enum (native_enum=False, len 16), NOT NULL, default `other` | `stock\|bond\|cash\|commodity\|crypto\|other` |
| `region` | str \| None, max_length=80 | |
| `country` | str \| None, max_length=80 | |
| `sector` | str \| None, max_length=80 | |
| `weight_pct` | `Numeric(7,4)`, NOT NULL | percent weight within the fund |
| `source` | `EtfHoldingSource` enum (len 16), NOT NULL | `yfinance` \| `manual` \| `issuer` \| `vanguard` (manual-first: a provider re-import never destroys manual rows; the three providers never coexist on one fund because the chain returns exactly one) |

`fundamentals.sector` / `fundamentals.country` are the leaf fallback for the
`sector` / `geography` dimensions when a composition row lacks the column but the
resolved security has a `fundamentals` row.

### Frontend

- `frontend/src/pages/Investments.tsx` — `ExposureSection` renders a dimension
  tab set including **Companies** (`value="company"`, label "Companies"); since
  M167 every dimension's list (and its donut) folds to the top 8 — top 10 for
  Companies — plus an `Other` bucket, via
  `frontend/src/lib/exposureFold.ts` (docs/09 §Exposure visuals).
- `frontend/src/api/securities.ts` — `getHoldings(id, recursive=false)` calls
  `/securities/{id}/holdings?recursive=${recursive}`; the security-detail overview
  requests `recursive=true` so nested funds show their `asset_class_breakdown`.
- `frontend/src/pages/SecurityDetail.tsx` — holdings table has a "Recursive: On/Off"
  toggle; when on, each fund row renders its `asset_class_breakdown` chips.
- `frontend/src/api/portfolio.ts` — `BreakdownResponse` /
  `BreakdownRow` types pin the wire contract
  (`key: string`, `market_value_cad: string`, `weight_pct: number`,
  `total_cad: string`, `unclassified_cad: string`).

## Exposure drill (M169)

Mark's follow-on to M167's geography view: "neat would be to drill down what
security or fund is from that place and in the case of fund what holding(s) if
possible." The engine already attributed every dollar through the M19 walk — it
just threw the trail away after bucketing. The drill keeps it.

### It is the SAME walk, not a second one

`breakdown(..., drill=<bucket key>)` threads one extra collector through
`_distribute` / `_sink` / the two top-level branches. Every attribution the
walk already performs — the leaf it placed, the bucket it chose, the chain of
funds it descended, the amount — is also appended to a `_Trail` list, but only
when the cent lands in the drilled bucket and only when a drill was asked for.

- `_Trail` (`dataclass(slots=True)`) records `bucket`, `amount`, `chain`
  (the `(security_id, symbol)` pairs of every fund the money entered,
  outermost first), `position_security_id`, `position_accounts`, `holding`
  (the `EtfHolding` row, or `None`), `sub` (the resolved `Security`), and
  `kind` (`"named"` | `"estimated"` | `"unattributed"`).
- `trail=None` (the default) and `drill_key=None` cost nothing: the aggregate
  walk is byte-identical to the pre-M169 one, which
  `test_the_drill_never_changes_the_aggregate_it_drills` pins.
- **No attribution rule is re-implemented.** The denominator, the renormalization,
  the cycle guard, the depth cap and the M166 sink are all the SAME code paths.
  That is what makes the drill reconcile to the bucket by construction.

### `chain` is the walk's own path

When `breakdown` calls `_distribute` for a fund position it passes
`chain=((security.id, security.symbol),)`; each recursion into a sub-fund
appends `(sub.id, sub.symbol)`. A leaf therefore carries exactly the funds the
money walked through, and `_drill_leaf` renders that as `via` — so a Microsoft
reached as VEQT → VXUS reports `via: ["VEQT", "VXUS"]` (docs/08 §Breakdown
drill pins the wire shape and the honesty rule).

`depth` is `len(chain)`, and the walk's own `depth < MAX_DEPTH` guard is what
caps it at 3. A deeper chain is not truncated in the payload — it is never
generated, and `drill.depth_cap` (= `MAX_DEPTH`) is the disclosure.

### The three kinds of trail

| `kind` | What produced it | What the drill shows |
|---|---|---|
| `named` | A composition row, or a stock's `fundamentals` row | A holding row with the provider's `provenance_source` and `native_weight_share` |
| `estimated` | M166's fund-mix split in `_sink` | One row naming the FUND, `estimated: true`, `provenance_source: null` |
| `unattributed` | The denominator residual, or a fund with no rows at all | One row naming the FUND, `unattributed: true` |

The last two exist because those dollars are REAL and in the bucket, but no
holding accounts for them. Dropping them would make the drill fail to
reconcile; attributing them to a named holding would fabricate a fact. Naming
the fund that produced them is the only honest option, and it is the same story
M147 and M166 already tell at bucket level — one level down.

### Folding and reconciliation

`_merge_synthetic` runs first: the M166 sink fires once per sector a fund's
residual is split across and once per residual event, so one fund can produce
several no-row rows with nothing to tell them apart but their amount. Rows that
share a symbol, a kind and a chain are one fact reported several times and are
collapsed into one. Real leaves are never merged — two holdings with the same
ticker are two holdings.

`_fold_drill_rows` then sorts by value desc, keeps `DRILL_N` = 25, and returns
the tail as one `{count, market_value_cad, weight_pct}` block — M167's rule, on
the wire. It runs at both levels: the bucket's contributions, and every fund's
own holdings.

Reconciliation holds at every level because the rows and the bucket come from
the same `_Trail` list: `Σ contributions + other == bucket_market_value_cad`,
and for each fund `Σ holdings + holdings_other == its contribution`. Rows round
to cents independently, so `contributions_cad` is the SERVER's total and the
client renders it rather than re-adding rows (M91's anti-fabrication pin).

## Touch points

- `backend/app/services/exposure_breakdown.py` — `breakdown`, `_distribute`,
  `_row_bucket`, `_sector_bucket`, `_sink`, `_fundamentals_bucket`,
  `_fallback_bucket`, `_resolve_sub_security`, `MAX_DEPTH`, `UNCLASSIFIED`,
  `_FUND_KINDS`. M169 adds `DRILL_N`, `_Trail`, `_pct`, `_drill_leaf`,
  `_drill_synthetic`, `_fold_drill_rows`, `_drill_block`.
- `backend/app/services/sector_map.py` (M166, M168) — `canonical_sector`,
  `SECTOR_ALIASES`, `SECTOR_NON_SECTORS`, `SectorIndex`, `build_sector_index`,
  `SectorMaps` (`load`, `static_sector`, `published_mix`, `static_rows`,
  `mix_rows`), `static_sector_for_symbol(symbol, maps)`,
  `fund_sector_mix(symbol, maps)`.
- `backend/app/models/provider_registry.py` + `services/provider_registry.py`
  (M168) — `ProviderFundRegistry`, `ProviderFundFamily`,
  `ProviderFundStatus`, and the session rules every registry read/write goes
  through (docs/07 §Provider registry). Migration
  `0050_provider_fund_registry` seeds it.
- `backend/app/api/portfolio.py` — `GET /portfolio/breakdown` (account_id
  filter; M169 adds `drill` and the 404 `drill_bucket_not_found` envelope).
- `backend/app/api/securities.py` — `GET /{id}/holdings` (`recursive`),
  `_holdings_block`, `_compute_asset_class_breakdown`, `queue_deepen` / `_run_deepen`.
- `backend/app/services/market_data.py` — `ensure_sub_fund_composition`,
  `SUB_FUND_ENRICHMENT_CAP`, `fetch_fund_holdings`, `_guard_external_fetch`.
- `backend/app/models/etf_holdings.py` — `EtfHolding.holding_security_id`,
  `EtfHoldingSource`, `EtfAssetClass`; migration `0007_add_holding_security_id`.
- `backend/app/schemas/securities.py` — `EtfHoldingRead` (`asset_class_breakdown`),
  `EtfHoldingsBlock`.
- `backend/app/core/errors.py` — `validation_error_handler` (422 `validation_error`).
- `frontend/src/pages/Investments.tsx`, `frontend/src/pages/SecurityDetail.tsx`,
  `frontend/src/api/securities.ts`, `frontend/src/api/portfolio.ts`.
- `frontend/src/components/ExposureDrillPanel.tsx` (M169) — the inline drill
  panel, its provenance badges and its `via` chain string.

## Verification

Hand case (dimension=`company`):
- Position `F` = 1000 CAD, a fund with holdings `S` (60%) and `A` (30%).
  `denominator = max(90,100) = 100`.
- `S` (600 CAD) recurses: `S` holds `B` (50%) and `C` (30%); `denominator = 100`.
  `B` = 600·50/100 = **300**, `C` = 600·30/100 = **180**; `S` residual
  (600 − 480) = 120 → `Unclassified`.
- `A` = 1000·30/100 = **300** (leaf stock).
- `F` residual (1000 − 600 − 300) = 100 → `Unclassified`.
- **Expected:** `A=300`, `B=300`, `C=180`, `Unclassified=220`; `total_cad="1000.00"`;
  weights 30 / 30 / 18 / 22; all four keys sum to 1000 (full attribution).

Concrete inputs → expected outputs (running stack, demo user):
- `GET /api/v1/portfolio/breakdown?dimension=company` → 200, body has
  `dimension:"company"`, `items` each with `market_value_cad` (string) +
  `weight_pct` (number), `total_cad` + `unclassified_cad` (strings); sum of
  `market_value_cad` == `total_cad`; sum of `weight_pct` ≈ 100 (residual in
  `unclassified_cad` inclusive).
- `GET /api/v1/portfolio/breakdown?dimension=sector|geography|asset_class` → 200
  with the respective buckets; `dimension=asset_class` degrades unknown positions
  to the security `kind`, `company` degrades to the security `symbol`.
- `GET /api/v1/portfolio/breakdown?dimension=bogus` → 422
  `{"detail": [...], "code": "validation_error"}`.
- **M169** `GET /api/v1/portfolio/breakdown?dimension=geography&drill=Canada`
  → 200 with a `drill` block: `contributions` are the positions that fed the
  bucket, a `fund` contribution's `holdings` are its leaves, and every leaf's
  `via` names the funds the money passed through (a two-level chain reports
  `["VEQT","VXUS"]`). `Σ contributions + drill.other == the bucket's
  `market_value_cad`; for every fund `Σ holdings + holdings_other == that
  contribution's `market_value_cad`. Omitting `drill` returns a body with no
  `drill` key at all, and the `items`/`total_cad` are identical either way.
  A `drill` naming a key that is not a bucket in that dimension → 404
  `{"detail": ..., "code": "drill_bucket_not_found"}`.
- `GET /api/v1/portfolio/breakdown?account_id=<id>` → only that account's positions
  are looked through; total reconciles to `GET /api/v1/portfolio/positions` filtered
  by the same `account_id`.
- `GET /api/v1/securities/{fundId}/holdings?recursive=true` → 200
  `EtfHoldingsBlock`; each fund row whose `holding_security_id` resolves to an
  ETF/mutual-fund with rows carries `asset_class_breakdown` (asset class → weight
  string); `recursive=false` → `asset_class_breakdown` absent on every row.
- Depth cap: a 4-fund chain (F1→F2→F3→F4, each 100% in the next) returns F4's
  weight as a single leaf bucket at F3's level; no descent into F4. A cyclic
  holding (A lists B, B lists A) terminates via `visited` with no infinite loop /
  no 5xx.
- Enrichment: `POST /api/v1/securities/{id}/holdings/import` (or `PUT .../holdings`)
  with sub-fund tickers queues `ensure_sub_fund_composition`; within the cap of 12,
  within the cap of 12, the sub-funds gain `etf_holdings` + `fundamentals`
  rows, and — M147-followup — the loop itself is bounded to
  `SUB_FUND_PROBE_CAP` (60) distinct tickers per fund, because a
  full-holdings provider passes thousands of tickers here and each
  RESOLVED non-fund ticker creates a linking security without counting
  toward the 12 (docs/07), and
  `holding_security_id` is backfilled — visible on a subsequent `recursive=true`
  holdings read. Under privacy mode the deepen task degrades silently (no external
  fetch, no error) and the triggering fund's own rows are unchanged.
- Regression: `backend/tests/test_exposure_breakdown.py` exercises the hand case
  above and the depth/cycle invariants; existing `breakdown` / holdings tests still
  pass (additive to M18). M166 changed ONE assertion there on purpose: a manually
  entered `"Financials"` row now reports as `"Financial Services"`, because the
  sector dimension has one vocabulary (see `## Sector chain (M166)`).

M169 drill (`backend/tests/test_m169_exposure_drill.py`, 18 tests;
`frontend/tests/m169ExposureDrill.test.tsx`, 15 tests):

- **The drill never changes the walk.** `items`, `total_cad`,
  `unclassified_cad` and `estimated_cad` are identical with and without
  `drill`, across all four dimensions — and a payload that never asked for a
  drill has no `drill` key at all.
- **Depth honesty.** A Microsoft reached as VEQT → VXUS reports
  `via: ["VEQT","VXUS"]`, `depth: 2`; a 4-fund chain stops at `MAX_DEPTH` and
  names the fund it stopped at as `sub_fund`; a cycle back-edge reports the
  full path including the hop that closed the loop.
- **Reconciliation at every level.** For each of five (dimension, key) pairs,
  `Σ contributions + other == the bucket`, and for every fund
  `Σ holdings + holdings_other == its contribution`. Every `weight_pct` on the
  wire equals `row value ÷ bucket × 100` at the SAME rounding the items use.
- **Honest slices.** The M166 fund-mix estimate and the denominator residual
  each become a row naming the fund that produced them — `estimated` /
  `unattributed`, with `provenance_source: null` — so drilling `Unclassified`
  or a partly-estimated sector still reconciles to the cent.
- **Bounds.** 30 holdings keep 25 and report `other: {count: 5,
  market_value_cad: "500.00"}`; a drill that fits reports `other: null`.

M166 sector chain (`backend/tests/test_m166_sector_classification.py`, 59 tests):

- **One vocabulary.** Every provider spelling maps to the yfinance name and
  canonicalisation is idempotent; a non-sector label (`"Cash and/or Derivatives"`)
  resolves `None`; an unrecognised label passes through unchanged rather than
  being dropped. Two rows for one company land in ONE slice (`"Information
  Technology"` + `"Technology"` → `Technology` = the whole position).
- **The cross-fund index.** A ticker labelled by one of the tenant's funds
  answers for another that left it blank (`RY.TO` labelled `Financials` by an
  IVV import → VTI's blank row reports `Financial Services`, `estimated_cad`
  `"0.00"`). A bare ticker whose rows DISAGREE (`SU` = Energy in Canada,
  Industrials in France) is refused outright and the row stays `Unclassified`
  with `estimated_cad` `"0.00"` — refused, never estimated. Rows with no sector
  or no symbol contribute nothing; an index built from another tenant's rows
  answers nothing.
- **The static ticker map.** Exact, slash-as-dot and exchange-suffixed spellings
  all resolve (`2330` = `2330.TW` = `Technology`); an unknown ticker is `None`;
  every stored value is already canonical.
- **The fund mix.** Returns a copy (mutating it cannot corrupt the table),
  resolves a venue-suffixed listing (`VOO.TO` = `VOO`), and answers `None` for
  a fund that publishes no mix — including VXUS/VT/VEA/VWO, which Vanguard
  reports `isavailable: false` for. Every stored mix is positive and canonical.
- **Inheritance is equity-only and labelled.** A `VOO` position of 1000 with a
  60% equity row and a 40% cash row splits only the 600 across VOO's mix
  (Technology = 600 × mix.total⁻¹ × mix.Technology, to the cent), leaves the 400
  of cash `Unclassified`, sums to exactly 1000, and reports
  `estimated_cad` = `"600.00"`; the Technology item carries `estimated: true` +
  its own `estimated_cad`, the Unclassified item `estimated: false` +
  `"0.00"`.
- **M19 holds under inheritance.** `VTI` → `VOO` (rows summing 94, so 6% is the
  M19 residual) distributes to exactly the position's value, every bucket is
  ≤ that value, every key is one of VOO's published sectors — never a second
  helping of VTI's weight — and `estimated_cad` equals the whole position.
- **No silent guessing.** A fund with no published mix stays wholly
  `Unclassified`; a bond row inside a fund that DOES publish a mix stays
  `Unclassified`; an unmapped ticker stays `Unclassified`.
- **The flag is sector-scoped.** `estimated_cad` is `"0.00"` on geography,
  asset_class and company.
- **The issuer CSV column reaches the pie.** `_parse_issuer_csv` maps the
  `Sector` column and the resulting rows report as `Technology`, not a second
  `Information Technology` slice.

Frontend (`frontend/tests/m166EstimatedCopy.test.tsx`, 5 tests): the estimate
note quotes the share, the dollar amount and what stays exact; only estimated
rows carry the `est` marker and each states its own estimate on hover; a
payload reporting zero, or omitting the fields entirely (older API), renders
neither the marker nor the note.

Live, on the portfolio that motivated M166 (21 positions, CAD 1,444,820.89):

| dimension | `unclassified_cad` | Unclassified | `estimated_cad` |
|---|---|---|---|
| sector | 174,868.34 | **12.10%** (was 62.77%) | 49,757.41 (3.44%) |
| geography | 108,851.26 | 7.53% (unchanged) | 0.00 |
| asset_class | 64,107.51 | 4.44% (unchanged) | 0.00 |
| company | 64,107.51 | 4.44% (unchanged) | 0.00 |
