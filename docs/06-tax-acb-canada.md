# 06 — Canadian Tax: ACB & Capital Gains

The tax engine spec; most safety-critical doc in this set. Rules encoded exactly as below in `backend/app/tax/`. Applies to non-registered accounts only (`taxable=true`). ACB is maintained **in CAD** per (non-registered account, security) as a running pool.

**Rounding.** Every cent in this engine goes through `app/tax/acb.py::cents(value)` — the app's single canonical money-rounding helper (docs/02 Conventions, AGENTS §2). It quantizes to `Decimal("0.01")` with `ROUND_HALF_UP`, which is what makes stored pools match `Numeric(18,2)`; no module may re-implement it. The helper takes an optional `rounding=` for the few surfaces whose behaviour is documented as `ROUND_HALF_EVEN` (positions, the exposure breakdown, budget spend) — nothing in the ACB pool math passes it, so the pool numbers here are exactly what they always were.

## ACB pool mechanics

State per pool (table `acb_pools`, docs/02): `quantity`, `total_acb_cad`. Always rebuilt from trades.

**Buy:**

```
pool_acb += qty × price × fx_rate(trade_date) + commission_cad
pool_qty += qty
```

**Sell:**

```
gain = proceeds_cad − (pool_acb ÷ pool_qty) × qty_sold
```

Reduce the pool proportionally: `pool_acb -= (pool_acb ÷ pool_qty) × qty_sold`; `pool_qty -= qty_sold`. A negative gain is a capital loss carried into the annual summary.
**Transfer in / Transfer out (non-registered pools):**

```
transfer_in  → treated like a buy:  pool_acb += |qty| × price × fx_rate(trade_date) + commission_cad; pool_qty += |qty|
transfer_out → treated like a sell: reduces the pool at average cost (pool_acb -= pool_acb ÷ pool_qty × |qty_out|; pool_qty -= |qty_out|) but realizes **NO gain** — no `Disposal` row is emitted (a transfer is a share movement, not a taxable disposition).
```

Both require sufficient **holding** quantity; an over-large `transfer_out` (or `sell`) triggers `422 oversell`.

**Cash transfer rows (M113, docs/03 §Wealthsimple activity vocabulary):** a `transfer_in` / `transfer_out` with `quantity = 0` is a CASH row under the docs/02 input convention — the amount rides in `price_native` and no shares move. Nothing is allocated and nothing is divided: `transfer_out` skips the average-cost step outright (the expression above would evaluate `0/0` for a row with no holding behind it and abort the commit with a 500), `transfer_in` adds `|0| × price + commission(0)` = nothing, and `oversell` cannot fire because `0 > held` is never true. `tests/test_m113_ws_cash_mappings.py` pins both the unit rule and a full taxable-account commit.

**Registered accounts (TFSA/RRSP/FHSA) hold shares but never a taxable ACB pool.** Holding quantity is tracked separately from pool quantity, so a registered account can be validated and drained without one:

- `buy` / `drip` / `transfer_in` — increments holding quantity only; no `acb_pools` row is created.
- `sell` / `transfer_out` — validated against the **holding** quantity, then emit **no `Disposal`**: no gain or loss, no ACB allocation, no division. A registered oversell is still `422 oversell`.
- `corporate_action_adjustment` — sets the holding share count for both account classes, so a split in a registered account is followed by a correctly-validated sell rather than a false oversell.
- Registered holdings still register as acquisitions/holdings for the superficial-loss rule (below), and the repurchase case is handled by "no add-back" — there is no pool to receive it.

For a **non-registered** account the holding quantity and the pool quantity are the same number, so pool allocation is unchanged.

## FX

Bank of Canada Valet API daily USD/CAD rate **on the TRADE date** (`https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json`), cached into `fx_rates` (docs/07). Offline fallback = a manually entered rate stored with source `manual`.

Precedence: pool math derives every rate exclusively from the `fx_rates` row — base = the security's `listing_currency` (docs/02), quote = `CAD`, date = trade date. `trades.fx_rate_used` is a display copy captured at entry and is NEVER authoritative (docs/02 input conventions).

**Non-publication days (M115).** BoC publishes business days only, so a weekend or holiday trade date (2025-12-26 is Boxing Day) has no row of its own, and an exact-date lookup alone would refuse it. Resolution therefore runs in order: the exact trade-date row, else **carry-forward** to the newest published rate on or before the trade date, bounded to `CARRY_FORWARD_MAX_DAYS = 7` days (a wider gap is genuinely missing data, not a closed market). A date neither step answers triggers ONE bulk `get_rates_between` backfill per currency for the whole sweep — issued before the replay starts, fail-soft per symbol (a failure logs and continues), and committed into the *caller's* transaction because `rebuild_user_pools` never commits — then both steps run again. The table stays the only source: the backfill fills `fx_rates` first, nothing reads around it. `FxRateBook` owns that fetch; the single-date seam `fx_rate_cad` (positions / registered book cost) stays cache-only and only carries forward. Under privacy mode no fetch is attempted at all and the same `422 fx_rate_missing` envelope says the backfill was blocked.

**Hard errors (all `422`):** `fx_rate_missing` — no `fx_rates` row (base → CAD, trade date) resolves, no carry-forward row within 7 days does either, and the backfill could not supply one (it failed, or privacy mode blocked it); `oversell` — a `sell`/`transfer_out` exceeds the account's **holding** quantity (pool quantity for a non-registered account, holding quantity for a registered one); `invalid_corporate_action` — a `corporate_action_adjustment` with `quantity < 0` (must be `> 0` for the resulting share count, or `0` for a signed ACB delta). All raised in `backend/app/tax/acb.py:_replay`.

## Superficial loss rule

Deny a loss when ALL of:

1. The sale realizes a loss, AND
2. The same security was acquired within 30 days before or after the sale in ANY account of the user, AND
3. A positive balance is still held 30 days after the sale.

When denied, the outcome depends on where the repurchase landed:

- **Repurchase in another NON-registered account**: the denied loss amount is added to THAT ACB pool's ACB — a deferral that resurfaces when those shares are later disposed of.
- **Repurchase in a REGISTERED account**: the loss is denied outright with NO add-back anywhere (no pool exists to receive it; registered cost tracking stays reference-only per docs/05).

Either way the annual report flags the denial as its own row (see Output below).

Evaluation semantics: the engine re-evaluates over the complete trade history at every rebuild/report run (docs/02 invariant). A sale whose 30-day window is still open is recomputed on each subsequent run — provisional until the window closes.

## Return of capital

T3 box 42 / ETF distributions recorded as `return_of_capital` trades:

- Reduce the ACB pool by the ROC amount.
- Floor at zero.
- Any excess above zero is treated as a **capital gain in the year realized**.

## Corporate actions / reorgs

v1 = manual `corporate_action_adjustment` trade entries (e.g. split, merger, conversion entered by hand). No automated parsing of corporate-action notices. **Import-time gate (M82, docs/41):** an IBKR or TD activity export that contains a split/merger/spinoff/reorganization is refused with `422 unknown_corporate_action` — naming the line, the institution and the raw action word — before any row is staged, because no published export states either accepted docs/06 value. `CORPORATE_ACTION_FORMS` is the pinned extension point: an export that *does* carry the resulting share count or the signed ACB delta earns one entry there and starts importing with no parser change. Mechanics (matching the extended fixture, docs/03):

- `quantity > 0` = the resulting absolute share count for that security after the action (a 2:1 split doubles the position). Engine sets `pool_qty := quantity` while preserving `total_acb_cad` — splits and consolidations leave ACB unchanged.
- `quantity = 0` = `price_native` carries a SIGNED CAD ACB delta applied directly to the pool (mergers/conversions that move cost basis without changing share count).

**The quantity side and the price side must stay in one frame (M149, docs/07 §Price frame).** A split moves the share count here; the historical trades keep the quantities the broker exported, so the price cache is the side that has to follow — `prices.price` is stored **as-traded**, un-adjusted for splits, and `_fetch_one_history` un-adjusts Yahoo's split-adjusted `Close` before it reaches the cache. The two halves then compose: a 51-share position bought at $732.59 stays worth $37.4k before an 8:1 split, and the 408 shares the `corporate_action_adjustment` records stay worth the same afterwards. A position held across a split whose adjustment has NOT been entered is the one case that still reads wrong — and it fails VISIBLE, as a cliff in the value history, rather than as a flat line that hides the missing row.

A window cached before that seam existed keeps its wrong rows (the sweep inserts only missing dates), so the escape hatch is `POST /securities/{id}/split-repair` (docs/07 §Split repair): it re-fetches one window through the normalizing seam and rewrites the machine rows, leaving every `source=manual` price alone.

## Output

Annual per-security capital-gains summary:

| Column | Source |
| --- | --- |
| Proceeds (CAD) | sells |
| ACB (CAD) | pool |
| Gain/Loss | computed |
| Outlays/commissions | trades |

The JSON envelope (`GET /reports/capital-gains?year=`, `format=json`) has the shape:

- `year` (int), `disclaimer` (str, the informational-only notice), `totals` (per-field sums over the finished item rows),
- `items[]`: one per security — `security`, `security_id`, `symbol`, `proceeds_cad`, `acb_cad`, `gain_loss_cad`, `outlays_commissions_cad`. All money columns are Decimal serialized as strings. `security` carries the **symbol**, not the security's name — it is a deliberate duplicate of `symbol`, kept because the CSV/HTML column header is labelled "Security" (`backend/app/tax/reports.py::_bucket`).
- `denials[]`: one per superficial-loss denial — `security_id`, `symbol`, `sale_date`, `denied_loss_cad`, `outcome` (`deferral` | `registered`), `acquiring_account_id` (null when the repurchase was in a registered account). Ordered by `(sale_date, symbol)`.

`items` are ordered by `symbol` only (a bucket is created by the first disposal or by a non-zero-excess ROC of that year, and the sort is stable), a `return_of_capital` whose excess is zero creates **no** row, and denial add-backs are applied after the ROC pass to buckets that already exist. Empty year → `items: []` and `totals` of scale-0 `"0"` (not `"0.00"`).

`format=csv` and `format=html` embed the same denial rows inline (CSV adds a `superficial_loss_denials` block; HTML adds a denials table) and both carry the `disclaimer`.

Delivered as CSV (`format=csv`), printable HTML (`format=html`), or JSON (`format=json`, default), matching Schedule 3 line items. Explicit `disclaimer`: informational only, not filed automatically.

**Server-side, single implementation (the S4 browser port retired in M160).** Every format of this report — `format=json` (the default the Taxes page renders), `format=csv` and `format=html` — is computed by `backend/app/tax/reports.py::capital_gains_report` over `app.tax.acb`'s `compute_user_tax_state` (the ACB replay and superficial-loss fixpoint). There is no second implementation to keep identical: the `frontend/src/lib/reportEngine.ts::capitalGainsReport` port and its `s4CapitalGains` parity test were deleted in **M160** (M134 P2-5).

The frozen capture survives as a SERVER-ROUTE regression pin instead: `frontend/tests/fixtures/s4-vectors.json` holds the server's JSON for all 13 tax years **and for `GET /acb?limit=200`** (14 cases), and `backend/tests/test_m160_replay_s4.py` re-seeds that captured dataset into a throwaway tenant, replays each request against the mounted route and byte-diffs the answer. Of the ACB response only `id` + `updated_at` are database-assigned, so those two are dropped on both sides of the comparison; the pool set, every other field per row, the order and the pagination envelope still have to match exactly. The pools are rebuilt through the one sanctioned write path (`rebuild_user_pools`), never inserted directly. Rounding is per-row HALF_UP in Decimal, never re-quantized at the total — and because only one implementation now exists, "HALF_UP" is a property of the route, not of an agreement between two ports.

The `year` query bound (`ge=1900`, `le=2200`) remains enforced server-side on the kept formats; the Taxes page only ever offers 2014…current.
