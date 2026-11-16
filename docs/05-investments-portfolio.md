# 05 — Investments & Portfolio

## Securities

CRUD over the `securities` table (docs/02) with symbol lookup via **yfinance**: name, kind, and listing currency autofill on symbol entry, always with manual override. Lookup is a convenience — a security can be created fully manually when yfinance has no data. Since M142 the lookup ALSO answers a typed **company name**: when both ticker candidates 404, one Yahoo search call resolves it, a single listing is offered as the add form's suggestion chip and several listings come back as a pick-one chooser — the canonical ticker, not the typed name, is what gets stored (docs/07 §Symbol search fallback, docs/09 §New security — the symbol suggestion chip). A row that was already stored under a name is corrected in place by `backend/scripts/repair_symbol.py`, which keeps the row id (and therefore its watchlist attachment, trades and history) intact.

M143 (docs/07 §Candidate resolution) closes the two ways a row could still get the wrong instrument: a ticker that resolves in BOTH markets (`VGRO` on NYSEArca and `VGRO.TO` on the TSX) is never picked silently — the lookup answers a pick-one chooser showing each side's market and currency — and a lookup that finds nothing creates an **unresolved** row instead of one carrying a guessed currency (docs/07 §Unresolved symbols). The CSV import seam (`trades._resolve_security`) is the one caller allowed to choose without asking: its own currency cell picks the matching side of a collision, and an existing row is matched through the bare↔`.TO` **twin** so a repaired `VGRO.TO` still binds the next file's `VGRO` cell (only when the file's currency agrees — a USD cell never latches onto a CAD row).

### Security overview

`GET /securities/{id}/overview` serves the per-security dashboard page in one payload: security identity, latest cached close with source/age/staleness, 365-day stats (52w high/low, window change %, cached-day count), ascending ≤365-point price history, positions **reused verbatim from `valuation.compute_positions`** (same rows as `GET /portfolio/positions`, filtered to this security) with summed totals, watchlist membership, and the last 10 trades. Unlike most read endpoints, `security_overview()` performs **live yfinance fetches on every call**: it calls `market_data.ensure_recent_price` AND `market_data.ensure_fundamentals` for the security (each degrading silently on failure) — so the overview is never served entirely from the local cache; unknown or foreign ids answer 404.

### Fund composition (M17, providers M147)

ETFs and mutual funds can carry their holdings with asset class / region / sector weights (`etf_holdings`, docs/02). Manual entry is the primary path — `PUT /securities/{id}/holdings` bulk-replaces manual rows and rejects weight sums over 100% (under 100 is legal: the residual is uncategorized) — and a manual row links `holding_security_id` through the SAME symbol index the provider path uses (M154, docs/07 §Symbol matching).

**Provider chain (M147).** `POST /securities/{id}/holdings/import` no longer means "ask yfinance": it runs the chain in `market_data.fetch_fund_composition` — the fund's ISSUER full-holdings CSV when `provider_fund_registry` family `ishares_us` has a `verified` row for its ticker (docs/07 §Fund composition providers; full list with per-holding Location/Sector/Asset Class), else VANGUARD's own full holdings when the registry knows it as a `vanguard_us` / `vanguard_ca` product (per-holding country from the ISIN prefix / `bloombergIsoCountry`, GICS sector on the Canadian side), else yfinance `funds_data.top_holdings` (a TOP SLICE only: typically 10–25 rows covering 12–65% of NAV). Either way the write goes through the single path `market_data.import_fund_composition`: the fund's provider-sourced rows (`source != manual`) are replaced together, manual rows are permanent, `holding_security_id` is linked where a ticker resolves to the user's securities **through the one shared index `market_data.security_symbol_index` (M154, docs/07 §Symbol matching: the exact spelling first, then an unambiguous bare stem — a provider's `005930` links to the user's `005930.KS`, while two listings sharing a stem such as `005930.KS` / `005930.KQ` stay unlinked until a resolver names one)**, and a `kind=stock` row the provider just proved to be a fund is repaired to `kind=etf` (import-created rows often carry the stock default — `trades._resolve_security`). None from every provider → 503 `market_data_unavailable`; privacy mode's 403 passes through uncaught (docs/11).

**Auto-enrichment (M147).** Both commit paths (`POST /trades/import/{id}/commit`, `POST /imports/{id}/commit`) queue background enrichment for every fund-kind security of the batch with ZERO composition rows — the same `import_fund_composition`, then the M19 sub-fund deepening (which now runs sub-funds through the SAME chain) and a fundamentals refresh. The task opens its own session, degrades silently on every failure (privacy 403 included), and never touches a fund that already has ANY row. A committed import can no longer leave a held fund decomposing to Unclassified just because nobody clicked Import.

**One-time seed.** `backend/scripts/backfill_etf_holdings.py` runs the same chain over every traded security with zero rows (all users; `--user-id` narrows; dry run by default, `--apply` writes), logging per-fund row counts — the M147 backfill that emptied the ~85%-Unclassified production pies. A run lasts minutes, so it holds NO session across the provider loop: the candidate query (`--refresh --user-id` is the `ORDER BY securities.user_id, securities.id` shape) and the per-fund row count each get a session that closes before the next fetch (M152, docs/01 § "Sessions and the event loop"; `tests/test_m152_seed_session.py`).

**Coverage expectations (M147 / M147-followup / M166 / M168).** iShares US products and Vanguard products import their FULL list — the registry rows are DATA now, so "registered" means "a `verified` row in `provider_fund_registry`", and a fund nobody registered discovers itself on first encounter from the issuer's OWN catalogues (§Fund composition providers; 50 US + 42 Canadian products were seeded from the literals this replaced: VXUS/VTI/VT/VOO/VEA/VGT/VWO/BND…, VCN/VIU/VAB/VUN/VUS/VGRO/VEQT/VFV/VEE/VBG/VBU…) → geography/asset-class/company nearly fully classified. A Vanguard payload does not attribute all of NAV (measured: VXUS 94.0%, VT 93.8%, VTI 98.4% of NAV) and its offshore-domiciled issuers stay country-less by design (docs/07), so even a full import leaves a single-digit `Unclassified` residual. yfinance-only funds carry their top slice; the residual lands in `Unclassified` by design (renormalization rule, docs/16). Each dimension gets its own deterministic step before giving up: geography the static company→country map (`app/services/company_country_map.py`, top mega-caps + TSX majors), and — M166 — sector the cross-fund index + static ticker map + published fund mix (`app/services/sector_map.py`, docs/16 §Sector chain). A full import therefore no longer implies a classified SECTOR pie: the US Vanguard products publish no per-holding sector at all, which is why that dimension carries the `estimated` flag. Both maps are shipped lookups, never LLM calls. Canadian iShares products (XIC/XEF/XEC/XBB/XSH…) still publish their CSVs behind a JS-only app with no stable URL, so those funds ride the yfinance slice until iShares publishes a lookup their screener covers; Canadian **Vanguard** products are covered by the CA graphql provider. Nothing above needs a code change to grow: `fintrackerctl provider-funds` lists, edits and re-verifies the rows (docs/07 §Provider registry).

The security dashboard renders the composition with the unclassified remainder for `kind in (etf, mutual_fund)`; the overview payload carries it cache-only.

## Trades

Trades are recorded per account against a security using the `trades` action enum (`buy | sell | dividend | distribution | return_of_capital | drip | fee | contribution | withdrawal | transfer_in | transfer_out | corporate_action_adjustment`), with `quantity`, `price_native`, `commission`, and `fx_rate_used`.

Income and cash-flow actions follow the input conventions defined on the `trades` table (docs/02): `quantity=0`, `price_native` = gross cash amount in native currency.

Registered accounts (RRSP/TFSA/FHSA/RESP, `taxable=false`) track cost **for reference only** — no `acb_pools` rows, no capital-gains computation (docs/06).
**Investment-account gating:** trades may only be recorded against investment-capable accounts — `brokerage_non_registered`, `rrsp`, `tfsa`, `fhsa`, `resp`, `lira`, `rdsp` (the `INVESTMENT_ACCOUNT_TYPES` frozenset in `backend/app/models/accounts.py`). Any other account type (`chequing` / `savings` / `credit_card` / `line_of_credit`) is rejected with `422 account_not_investment` (`backend/app/api/trades.py:_investment_account_or_422`). Cash-only and corporate-action rows follow the same action enum but still require an investment account.

### Trade CSV import

`POST /trades/import` stages a whole activity file into `import_batches` instead of writing trades one at a time. The route itself, its validation chain, its rate limit and its dedup/force-include flow live in docs/03; its request/response shapes and error codes live in docs/08.

**Per-institution brokerage profiles (M82, docs/41).** A profile is pure configuration that turns one broker's export into `RawTrade` rows and hands them to the same staging loop — there is no second write path. Both routes take an optional `?profile=<slug>` query parameter (Questrade, Wealthsimple, Interactive Brokers, Fidelity, TD Direct Investing, RBC Direct Investing, National Bank Direct Brokerage); without it the header is auto-detected, and a Wealthsimple-shaped file deliberately runs the legacy parser so existing imports stay byte-identical. A zero-write dry run (`POST /trades/import/preview`) reports the exact verdicts staging would produce. Multi-currency handling reuses the docs/07 rules unchanged: the row's own currency decides its listing currency, `fx_rates` (BoC wins, manual fills gaps only) converts it, and the account's currency never leaks into a row. Corporate actions in an IBKR or TD export are refused with `422 unknown_corporate_action` rather than guessed (docs/06).

## Portfolio valuation

- **Positions** = signed sum of trade quantities per (account, security). **Book cost (CAD)** = the ACB pool for non-registered accounts; for registered plans a pure average-cost replay of the account's trades (docs/06 formulas, no superficial loss, never persisted) — surfaced per position in `GET /portfolio/positions` (docs/08).
- Market value shown in native currency and CAD; CAD conversion uses cached `fx_rates` for historical math and latest rate for current valuation.
- Latest price from yfinance with a **manual-price fallback** and a staleness indicator in the UI (see docs/07).
- **Net-worth trend** = cash balance (sum of committed transactions per account) + positions valued at the newest `prices` close ≤ trend date (`manual` rows win over `yfinance`, docs/02); latest spot prices drive the "now" endpoint (historical series served by `GET /reports/net-worth`, docs/12).

- **Watchlist** (W11): securities tracked but not held via `POST /watchlist` — same price caches, staleness badges, and the `POST /portfolio/refresh` action re-quotes held ∪ watched through `queue_backfill` (the 365-day `backfill_range`, contract-identical to `POST /securities/{id}/refresh`; see docs/18 §Market-hours gate for why it is no longer `ensure_recent_price`). Its add-picker searches the STORED securities first and, only when nothing matches (query ≥ 2 chars, 600 ms settle), falls back to `GET /securities/lookup` — a pick creates the row (`POST /securities`) and then watches it (docs/09 §Watchlist ticker search), which is how a symbol you have never held gets followed.

## Look-through exposure breakdown (M18 / M19)

`GET /portfolio/breakdown?dimension=sector|geography|asset_class|company` distributes every open position's market value into buckets. **Recursion (M19):** when a composition row's ticker resolves to another fund of the same user with its own `etf_holdings` rows, the distribution descends into those rows in turn (depth ≤ 3, cycle-safe via visited set); at every level, weights use denominator `max(sum_of_weights, 100)` — over-known funds renormalize, partially-known funds keep face weights and the residual lands in `Unclassified`. Leaf rows fall back to their own column, then the matched security's `fundamentals` row, then `Unclassified`. Stocks fall back to `fundamentals`; anything else is wholly `Unclassified` except that `asset_class` degrades to the security kind and `company` to the security symbol. **Company dimension:** leaf key = holding ticker (or name when symbol is null); direct stock positions key by their own symbol. **Enrichment (M19):** importing or manually saving a fund's holdings queues a background task that auto-creates sub-fund securities (via yfinance lookup) and fetches their composition + fundamentals (depth-1, bounded 12 enrichments AND 60 symbol probes per fund — `SUB_FUND_PROBE_CAP`, because a full-holdings provider hands the deepening thousands of tickers and every non-fund ticker it resolves creates a linking security without counting toward the 12; privacy-degrading, never touches the edited fund). Pure aggregation over cached rows — never a network call at breakdown time.

**Fallback chain + honesty (M147).** Geography leaf resolution is: the row's own `region`/`country` → the matched sub-security's `fundamentals.country` → the static company→country map keyed by the row's ticker (`country_for_symbol`, exact match then known exchange-suffix strip) → `Unclassified`. The response carries `unclassified_funds: [{security_id, symbol, name, market_value_cad, reason, imported_weight_pct}]` — the evidence for the honesty copy (docs/09 §Exposure honesty): `missing` = the fund carries NO composition rows, `partial` = its own rows sum to <99 (the provider published only the top slice; sub-1% residuals name nothing). The auto-enrichment trigger (M147, above) is what empties that list after a new import; the breakdown itself never fetches.

**Sector estimation contract (M166, docs/16 §Sector chain).** Sector is the one dimension no provider fills for every sub-holding — the iShares issuer CSVs and the Vanguard CANADIAN gateway carry a GICS column per row, the Vanguard US profile JSON does not — so its resolution order is wider than the other three: the row's own `sector` → the tenant's own cross-fund sector index (a ticker one of their funds already labelled answers for every other; a bare ticker whose rows DISAGREE is refused, never guessed) → the static ticker map (`services/sector_map.py`) → the matched security's `fundamentals.sector` → `Unclassified`. Every step canonicalises to ONE vocabulary (yfinance's GICS names), so `Information Technology` and `Technology` are one slice and a cash leg (`Cash and/or Derivatives`) is not a sector at all.

Whatever an **equity** row still leaves unattributed inside a fund that PUBLISHES its own GICS sector mix is split across that mix and reported as an **estimate**: the payload carries top-level `estimated_cad` plus per-item `estimated_cad` and `estimated`, and the UI labels the rows and quotes the share (docs/09 §Exposure honesty). Only a fund's own published mix is used (`provider_fund_registry` family `fund_sector_mix` since M168, captured from the Vanguard fund profile; VXUS/VT/VEA/VWO publish none, so they stay partly `Unclassified` rather than being estimated from a number nobody published), the split only ever redistributes value that was going to `Unclassified` — so the M19 denominator rule and full attribution are untouched — and a cash, bond or commodity leg never inherits an equity mix. Geography, asset class and company are unaffected and report `estimated_cad: "0.00"`.
Both `GET /portfolio/breakdown` and `GET /portfolio/exposure` accept an optional `account_id` filter (scope the distribution to a single account). `GET /securities/{id}/holdings?recursive=true` returns the recursive `asset_class_breakdown` for a fund (depth ≤ 3, cycle-safe via visited set); `recursive=false` (default) returns one level. Invalid `dimension` → 422 envelope (docs/08).

## Currency-exposure view

Aggregate holdings grouped by `listing_currency` across all accounts — the FX exposure picture: how much of the portfolio is USD-listed vs CAD-listed regardless of account currency.

## Dividends / distributions

Recorded as trades (`dividend`, `distribution`, `return_of_capital` actions); feed the cash-flow report (docs/12), income reports and, for ROC on non-registered holdings, the ACB engine (docs/06). The Reports page adds a yearly dividend overview (docs/12 §6): per-security gross CAD income split by action type at trade-date fx rates, 12-month cadence, and a trailing-365-day total.

## Total return — TWR / MWR (M77)

`GET /reports/total-return?from=&to=` returns two **period** returns (never
annualized, so they sit beside the benchmark comparison's own period return for
the same window) plus the value and flow context they were computed from:

```json
{"from": "2025-01-01", "to": "2025-03-31", "status": "ok",
 "start_value_cad": "0.00", "end_value_cad": "909.00",
 "net_flow_cad": "800.00", "income_cad": "40.00",
 "twr_pct": "10.90000000", "mwr_pct": "12.46915167",
 "valuation_days": 90, "active_days": 89, "flow_days": 2,
 "account_count": 1}
```

**Scope.** Accounts whose type is in `INVESTMENT_ACCOUNT_TYPES` (docs/02);
`account_count` reports how many. Reads are cache-only — no path here ever
fetches (docs/12). Committed rows only (docs/03 staging).

**Portfolio value** `V(d) = positions(d) + cash(d)` (the account page's value-history chart, docs/08 §Account value history / M118, renders this same chain for ONE account):

- positions valued at the newest cached price ≤ `d` (manual wins, earliest-price
  fallback), converted at the cache-only rate; an unresolved price/fx row is
  **excluded, never fetched** (docs/12);
- cash = the `transactions` ledger + trade-generated cash + each account's
  `starting_balance`. The schema keeps account balances on `transactions` only,
  so trade cash is derived per action:

  | action | cash effect (the ROW's currency, M132) |
  | --- | --- |
  | `dividend \| distribution \| return_of_capital \| contribution` | `+price_native` |
  | `withdrawal \| fee` | `−abs(price_native)` — ALWAYS money out (M131). The row stores the broker's native sign (negative), so a plain `−price_native` would credit a correctly-signed row; `-abs` is direction-safe for both the repaired negative rows and any legacy row stored positive. |
  | `buy \| drip` | `−(abs(quantity) × price_native) − commission` |
  | `sell` | `+abs(quantity) × price_native − commission` |
  | `transfer_in \| transfer_out` | `±abs(price_native)` when `quantity == 0` (cash row, docs/02); `0` when it moves shares (docs/06) |
  | `corporate_action_…` | `0` |

  That makes a cash dividend neutral (value falls with the price, cash rises)
  and a DRIP neutral (the reinvested shares give back what the price gave up) —
  which is exactly what "total return including reinvested dividends" must mean.
  `income_cad` still reports the gross dividend/distribution/ROC income
  separately.

**Multi-currency cash (M132).** Every effect above is stated in the ROW's own
currency — `trades.currency`, the broker's per-row code (docs/02); a row written
before M132 has none and reads in its ACCOUNT's currency, which is exactly what
every consumer did before the column existed. Two different conversions then
apply, because a balance and a flow are different questions:

- trade cash is a **balance**: it accumulates **per currency** and converts at
  the day it is *valued* (`d`, or a bucket end) — the same day positions and
  `starting_balance` convert. Converting each row at its own trade date instead
  prices one balance at a basket of historical rates: real account 731's
  +6,124.38 and −6,124.18 (both USD) net to +0.20 USD, and per-row-date
  conversion turned that near-zero balance into −147 CAD of pure rate drift;
- external flows and income are **events**: they convert at the **trade date**,
  and at the ROW's currency (a USD dividend in a CAD account is USD income).

Both are cache-only reads (docs/12): an unresolved rate for a row currency
excludes the figure — `investment_cash_cad` returns `None`, a bucket's
`cash_cad`/`value_cad` go `null` — never a CAD-only half-truth, never a fetch.

Why this was worth a migration: a USD buy inside a CAD account used to land
FACE-VALUE in a single `cash_native`, so account 731 read **−582.73** where
Wealthsimple shows **$0.27**. Re-read row by row against the source export, the
whole gap is two `AdministrativePayment / TRANSFER_FEE_REIMBURSEMENT` credits
("CAD credited", +145.75 each) that M131's `fee → −abs` rule counted as money
LEAVING — 583.00 of it — plus the +0.20 USD leg that was never converted.
`backend/scripts/backfill_trade_currency.py` re-reads the source file (the
authority for both the currency and those two signs); with its repair,
`0.0410 CAD + 0.1610 USD × 1.4226 = 0.2700 → 0.27` — Wealthsimple's number.

**External flows** (money crossing the portfolio boundary) are
`contribution | withdrawal | transfer_in | transfer_out` plus every committed
transaction on an investment account. Everything else — buys, sells, drips,
dividends, fees, commissions — is **internal** and stays inside the return.

**TWR** chain-links daily: `r_d = (V_d − CF_d) / V_{d−1}`, flows treated as
occurring at the END of their date so external money cannot inflate the result.
The product is carried as an exact integer ratio (cents numerator / cents
denominator) and rounded ONCE, half-up, to 8 decimal places. A day only counts
(`active_days`) when the previous value was `> 0`; if no day qualifies,
`status = "no_data"` and both percentages are `null`.

**MWR** is Modified Dietz over the same window:
`r = (V_end − V_start − CF) × window_days / (V_start × window_days + Σ w_i·CF_i)`
with `w_i = days remaining after flow i`. Same single rounding to 8 dp;
`null` when the denominator is 0 (never a confident `0.00000000`, M65 honesty).

**Serialization.** All money fields are 2-decimal strings (`"0.00"` even when
empty); both percentages are plain fixed 8-decimal strings — never scientific
notation (`0.00000001`, not `1E-8`) — via the shared integer `ratio_pct`
divisor.

**Display (M105).** That 8-decimal string is the wire contract and is never
re-parsed on the way in; what the `Period return` card *shows* is a separate,
display-only concern. `formatPct` SLICES the string to a 2-decimal reading —
a trim of the engine's exact output, never a `toFixed` re-round, so no digit
on screen is invented — while the sign prefix and the muted-zero tone still
come from the **RAW** string (a `0.00000123%` gain renders `+0.00%` in gain
green, not muted; only an exact `0.00000000` is muted). The hidden digits are
recoverable, not lost: the value's `HelpHint` reads `21.38091492% precise` and
the span's `aria-label` carries the full string for assistive tech. Any shape
that is not exactly `[-]digits.8dp` renders as it always did (fail-open rather
than mis-parse).

**Benchmark comparison.** The Investments page renders this for the SAME window
the benchmark comparison uses (one pair of constants in `Investments.tsx`, so
the two cards can never drift apart), and the benchmark's own honesty gate is
unchanged in shape and wider in scope since M173: no comparison is shown when
`benchmark_points < 2` **or** the benchmark return is `null` (a window before
the cached series, or a foreign-listing benchmark with no cached rate) —
"No benchmark data for this window" instead of fabricated alpha (M65, docs/28).

**One implementation (the browser port retired in M160, its shim in M160-R2).**
`backend/app/services/total_return.py::total_return_report` is the only
implementation of these numbers. The `frontend/src/lib/totalReturnEngine.ts`
port and its `m77Parity` diff were deleted in **M160** (M134 P2-5); the
`lib/reportClient.ts` shim that temporarily carried the port verbatim was
deleted by **M160-R2 (t_85311c34)**, which re-pointed `TotalReturnMetrics` at
`GET /reports/total-return` — the panel is a thin read of the route
(`api/reports.ts::getTotalReturn`), so there is no browser fallback and no
independent authority. The frozen `m77-vectors.json` golden now replays against
the ROUTE in `backend/tests/test_m160_replay_m77.py`.

The math is pinned where it lives:
`backend/tests/test_total_return.py` asserts the route against hand-derived
fixtures inside 1e-8 (chain-linked TWR 10.9%, Modified Dietz 12.46915167%, a
DRIP-neutral window where TWR == MWR, and a pure transfer showing exactly
`0.00000000%`). The captured golden
`frontend/tests/fixtures/m77-vectors.json` (3 ranges, captured by
`backend/scripts/capture_m77.py`) is still on disk; its client-comparison
harness retired with the port, and the hand-derived backend pins above are
the authority.
