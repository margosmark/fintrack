# 28 — Investment Benchmarks

Compare portfolio performance against market benchmarks (S&P 500, TSX, etc.).

## Context

Users want to know if their investment portfolio is beating the market. The existing performance report shows absolute returns but doesn't compare against benchmarks. This feature adds benchmark comparison to the Investments page.

**Status (M173, 2026-10-12).** The feature shipped in M43 with the benchmark
side as an HONEST STUB: `benchmark_return_pct = 0`, `benchmark_points = 0`,
gated client-side so a placeholder zero could never read as alpha (M65). Mark's
report — "Benchmarks all zero / No benchmark data for this window" — was that
stub working as designed, and the comparison it stood in for had never been
built. M173 implements it: a real cached price series per benchmark, an
FX-inclusive return in the reporting currency, and the excess computed
server-side. Decisions 5 and 6 below are the ones this changes.

## Decisions

1. **Predefined benchmarks.** Fixed set of four: S&P 500 (`SPY`), TSX
   Composite (`XIU`), Canadian Bond (`XBB`), Global ex-Canada (`XAW`). No
   custom benchmarks in v1.

2. **Same-period comparison.** Compare portfolio returns over the same time
   period as the benchmark.

3. **Time-weighted returns.** The PORTFOLIO side is a value ratio over the
   window (the M29 shape this endpoint has always served). The benchmark side
   is a price return, which is the market's own TWR for a buy-and-hold index —
   no flows to weight.

5. **Benchmark data — IMPLEMENTED (M173).** A real series, cached like any
   other price series. The four picks are listed on the venue where the index
   is CA-relevant — `SPY` on NYSE Arca (USD); `XIU.TO`, `XBB.TO` and `XAW.TO`
   on the TSX (CAD) — and stored in `benchmark_prices`, keyed to the BENCHMARK
   symbol and **never** to a `securities` row: a benchmark is not held, and a
   security for `SPY` would put a fund in every tenant's picker that no trade
   explains. The table has no `user_id` (a close for `SPY` on a date is
   universal — the `fx_rates` operator level, AGENTS §2). Fetching is
   `market_data.fetch_benchmark_history` (the one yfinance seam): the pinned
   venue ticker only, no `.TO` fan-out and no search heal, because resolving a
   bare ticker can answer as a DIFFERENT instrument (the M144 `VUS` case).
   `services/benchmark_refresh.py` (scheduled, `benchmark_refresh_interval_seconds`)
   keeps the series current and `scripts/backfill_benchmarks.py` does the
   one-time deep walk; both are the ONLY writers. The benchmark reads are
   cache-only, so the comparison itself can never become a yfinance call.

6. **Decimal discipline (M47, docs/29 F6).** Money and percentage fields are
   computed and serialized as Decimal-as-strings (no float touches market-value
   math). Since M173 `benchmark_return_pct` and `excess_return_pct` are
   `string | null` — `null` when there is no comparison to show, never a
   numeric `0`, because a placeholder 0 beside a real portfolio return IS
   fabricated alpha.

7. **Chart overlay.** Line chart showing portfolio value vs benchmark value
   over time, normalized to 100 at start date. *(Still future work — the
   endpoint serves the numbers, not a series. `GET /portfolio/benchmark-history`
   does not exist; see §API.)*

> Numbering note: decisions 5 and 6 are referenced BY NUMBER from docs/08 and
> docs/29, so the numbers stay put. The chart-overlay decision used to carry a
> second "5."; it is renumbered to 7 here so nothing cites an ambiguous number.

## Schema

```sql
-- created by alembic 0052_benchmark_prices
CREATE TABLE benchmark_prices (
    id serial PRIMARY KEY,
    symbol    varchar(32)  NOT NULL,   -- the BENCHMARK key: SPY | XIU | XBB | XAW
    date      date         NOT NULL,
    close     numeric(20,8) NOT NULL,  -- the provider's SPLIT-ADJUSTED close
    currency  varchar(3)   NOT NULL,   -- the venue's quote currency (USD | CAD)
    source    varchar(32)  NOT NULL,   -- yfinance | manual (manual wins, insert-only)
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    CONSTRAINT uq_benchmark_prices_symbol_date UNIQUE (symbol, date)
);
```

- **No `user_id`** — a shared market-data cache, the `fx_rates` operator level
  (docs/02 §benchmark_prices). One fetch writes one row and every tenant's
  panel reads it.
- **The frame is the provider's split-adjusted close**, not `prices`'
  as-traded one: `prices` un-adjusts to match the ledger's own quantities
  (docs/07 §Price frame), while a benchmark has no ledger and un-adjusting
  would read a 2:1 split as a −50% return. A benchmark series is a price index.
- **Insert-only.** A backfill re-run adds missing dates and rewrites nothing;
  a `manual` row (no write path ships yet) would never be overwritten.
- **Not exported, not restorable.** `app/api/export.py::EXCLUDED_TABLES` and
  `app/api/backup.py::RESTORE_FORBIDDEN_TABLES` both carry it: a crafted
  snapshot must not be able to write closes that every tenant's panel then
  reports as the market's own answer (the M168 registry precedent).
- The venue table (benchmark → ticker/currency/venue) stays a code constant in
  `services/benchmarks.py::BENCHMARKS`, so re-pointing a benchmark at another
  listing is a code change, not a re-key of cached rows.

## API

- `GET /portfolio/benchmarks?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&symbol=SPY`

  → 200 `{portfolio_start, portfolio_end, portfolio_return_pct,
  benchmark_symbol, benchmark_name, benchmark_return_pct, benchmark_points,
  benchmark_currency, benchmark_listing_currency, benchmark_series_start,
  benchmark_series_end, benchmark_clipped, excess_return_pct, start_date,
  end_date}`

  Money and percentage fields are Decimal-as-strings (decision 6). Since M173:

  | field | meaning |
  | --- | --- |
  | `benchmark_return_pct` | `string \| null`. The benchmark's PRICE return over the window, **FX-INCLUSIVE and expressed in `benchmark_currency`** (see §Benchmark calculation). `null` when fewer than two cached closes fall in the window, or when an endpoint's rate is missing from the cache. |
  | `benchmark_points` | count of cached benchmark closes in `[start_date, end_date]` — the truth, whatever else is missing (the M65 gate reads it). |
  | `benchmark_currency` | the reporting currency the return is expressed in (`CAD`). |
  | `benchmark_listing_currency` | the venue's own quote currency (`USD` for SPY, `CAD` for the TSX funds); `null` for an unknown symbol. |
  | `benchmark_series_start` / `_end` | the cached coverage window, ISO dates or `null` when nothing is cached. |
  | `benchmark_clipped` | `true` when `start_date` precedes the cached series, i.e. the comparison covers a SHORTER period than requested. A window that ends after the newest cached close is NOT clipped — that is the ordinary "today's bar is not published yet" case, which `benchmark_series_end` reports instead. |
  | `excess_return_pct` | `portfolio_return_pct − benchmark_return_pct`, computed server-side and `null` exactly when the benchmark return is, so the two can never disagree. |

  Unknown `symbol` → `benchmark_name` echoes the symbol, the listing currency
  is `null`, and there is no series: points 0, both returns `null`. Missing or
  malformed dates → `422 validation_error` (the route raises no domain code of
  its own).

  Pinned by `backend/tests/test_m173_benchmarks.py` (venue table, backfill
  insert-only, the hand-computed FX-inclusive return, the CAD-only path, the
  missing-rate and clipped-window honesty cases, the four chips' point counts,
  the cache-only served path, privacy, and the split frame) plus the
  pre-existing `backend/tests/test_m29_stream.py` (Decimal math + wire shape).

- `GET /portfolio/benchmark-history?symbol=SPY&start_date=...&end_date=...` —
  **not implemented.** No such route and no caller in backend or frontend;
  chart data would come from a future overlay (decision 7).

## Benchmark calculation

```
Portfolio return  = (end value − start value) / start value            [CAD, M29]
Benchmark return  = (close_end × fx_end) / (close_start × fx_start) − 1
Excess return     = Portfolio return − Benchmark return                [server-side]
```

- **The window** is the first cached close **at or after** `start_date` and
  the last cached close **at or before** `end_date`. `benchmark_points` is how
  many closes fall between them; fewer than two means there is no return to
  report.
- **FX-inclusive, in the reporting currency.** `SPY` is quoted in USD, so a CAD
  investor's benchmark return includes the currency effect they actually
  experienced: both endpoint closes are converted at the cached `fx_rates`
  value for their OWN date (`fx.latest_rate(..., allow_fetch=False)`, the same
  cache-only book `services/valuation.py` converts holdings with — docs/07).
  A same-currency benchmark (the three TSX funds) reduces to the plain price
  ratio. The payload carries both currencies so the panel can label which one
  it is showing; the client renders
  `"S&P 500 is quoted in USD; the return above is FX-inclusive, converted to CAD."`
  whenever they differ.
- **A missing rate is an honest gap.** If either endpoint has no cached rate on
  or before its date, `benchmark_return_pct` is `null` — never a face-value USD
  number presented in a CAD cell, and never a fetch from a served path.
- **It is a PRICE return** (distributions excluded). The portfolio side of this
  endpoint is likewise a value ratio, not the M77 total return; the panel says
  so rather than implying a dividend-inclusive comparison.
- **Rounding:** both returns are quantized HALF-EVEN to 2 decimal places (the
  module's existing convention), and the excess is the difference of the two
  rounded values.

## Frontend

- **Investments page** — "vs Benchmark" card in the Exposure section
  (`components/investments/BenchmarkPanel.tsx`), driven by the SAME window the
  total-return card uses.
- **Summary card** — portfolio return, benchmark return, excess return.
- **No-data gate (M65, m61 #7; extended M173)** — when `benchmark_points < 2`
  **or `benchmark_return_pct` is `null`**, the EXCESS cell renders "No benchmark
  data for this window" (never the portfolio return echoed as alpha), the
  benchmark cell renders `—` (never `Number(null) → 0` as "+0.00%"), and the
  selected benchmark chip renders visually disabled (`disabled` + `opacity-40` +
  a `title` carrying the same sentence); the other chips stay clickable since
  each fetches its own comparison. With a real return, EXCESS formats normally
  (signed, coloured, icon). `frontend/tests/benchmarkPanel.test.ts` pins both
  sides plus the FX and clipped labels.
- **Honesty labels** — the FX-inclusive sentence above when the listing
  currency differs from the reporting one, and
  `Benchmark data starts <date> — this window is clipped.` when
  `benchmark_clipped` is true, so a shortened comparison is never silently
  shortened.
- **Window label** — the card prints the API's own ISO dates, parsed from
  their PARTS in UTC. `new Date("2026-01-01")` is UTC midnight, so rendering it
  in the browser's zone printed the day BEFORE the window (and disagreed with
  the period-return card above it, which prints the API strings) west of
  Greenwich. `frontend/tests/benchmarkPanel.test.ts` pins the fixed reading.
- **Benchmark selector** — the four chips (SPY / XIU / XBB / XAW), each
  fetching its own comparison.

## Acceptance criteria

- [x] Benchmark prices fetched via yfinance (cached) — M173:
      `benchmark_prices` + `services/benchmark_refresh.py` +
      `scripts/backfill_benchmarks.py`
- [x] Return calculated correctly for both portfolio and benchmark — hand-
      computed pins in `tests/test_m173_benchmarks.py`
- [ ] Chart shows normalized comparison (start = 100) — decision 7, still future
      work; the endpoint serves numbers, not a series
- [x] Excess return displayed with color coding (green if positive)
- [x] EXCESS gated on real data: no return / `<2` points → "No benchmark data
      for this window" + disabled selected chip (M65, m61 #7; M173 adds the
      null-return half)
- [x] Privacy mode shows "no data" gracefully — the shared series is never
      fetched while any tenant has `privacy_mode` on (docs/11), and the panel
      renders its honest no-data state
- [x] Time period matches dashboard date range — one pair of constants in
      `Investments.tsx`

## Priority: low
## Effort: 3-4 days
## Dependencies: M4 (investments), M7 (market data), M18 (exposure)
