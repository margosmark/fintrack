// benchmarks — compare portfolio vs market indexes (docs/28, M173).
import { api } from "./client";

export interface BenchmarkComparison {
  portfolio_start: string;
  portfolio_end: string;
  portfolio_return_pct: string;
  benchmark_symbol: string;
  benchmark_name: string;
  /**
   * The benchmark's price return over the window, EXPRESSED IN
   * `benchmark_currency` and FX-INCLUSIVE (SPY is quoted in USD and converted
   * through the cached rate book, so a CAD investor sees the currency effect
   * they actually experienced). `null` — never a numeric 0 — when the series
   * cannot produce one: fewer than two closes in the window, or a missing
   * cached rate for a foreign-listing benchmark (docs/28 decision 5, M173).
   */
  benchmark_return_pct: string | null;
  /**
   * Number of benchmark price points in [start, end] (M65/m61 #7: <2 means
   * there is no comparison to show). The EXCESS cell is gated on this AND on
   * `benchmark_return_pct` being non-null.
   */
  benchmark_points: number;
  /** The currency the return above is expressed in (the reporting currency),
   *  and the venue's own listing currency (SPY → USD, the TSX funds → CAD). */
  benchmark_currency: string;
  benchmark_listing_currency: string | null;
  /** Cached coverage for the selected benchmark, and whether the requested
   *  window starts BEFORE it — the comparison then covers a shorter period,
   *  and the panel says so instead of silently shortening it. */
  benchmark_series_start: string | null;
  benchmark_series_end: string | null;
  benchmark_clipped: boolean;
  /** `portfolio − benchmark`, computed server-side; `null` whenever the
   *  benchmark return is (so the two can never disagree). */
  excess_return_pct: string | null;
  start_date: string;
  end_date: string;
}

export function getBenchmarkComparison(
  startDate: string,
  endDate: string,
  symbol: string = "SPY"
): Promise<BenchmarkComparison> {
  const params = new URLSearchParams({
    start_date: startDate,
    end_date: endDate,
    symbol,
  });
  return api(`/portfolio/benchmarks?${params.toString()}`);
}
