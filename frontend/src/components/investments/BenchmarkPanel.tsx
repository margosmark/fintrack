import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { getBenchmarkComparison } from "../../api/benchmarks";
import { Card, CardTitle } from "../ui/Card";
import { SkeletonRows } from "../ui/Skeleton";
import { EmptyState } from "../ui/EmptyState";

const BENCHMARKS = [
  { symbol: "SPY", name: "S&P 500" },
  { symbol: "XIU", name: "TSX Composite" },
  { symbol: "XBB", name: "Canadian Bond" },
  { symbol: "XAW", name: "Global ex-Canada" },
];

function formatPct(v: number | string) {
  const numeric = Number(v);
  const sign = numeric >= 0 ? "+" : "";
  return `${sign}${numeric.toFixed(2)}%`;
}

/**
 * Format an ISO `YYYY-MM-DD` wire date.
 *
 * The date is built from its PARTS and formatted in UTC on purpose: `new
 * Date("2026-01-01")` is parsed as UTC midnight and then rendered in the
 * BROWSER's zone, so the card printed "Dec 31, 2025" west of Greenwich — one
 * day earlier than the window the numbers were actually computed for (and
 * than the period-return card right above it, which prints the API strings).
 */
function formatDate(d: string) {
  const [year, month, day] = d.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-CA", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function BenchmarkPanel({ startDate, endDate }: { startDate: string; endDate: string }) {
  const [selected, setSelected] = useState("SPY");

  const { data, isLoading, error } = useQuery({
    queryKey: ["benchmark", selected, startDate, endDate],
    queryFn: () => getBenchmarkComparison(startDate, endDate, selected),
    enabled: !!startDate && !!endDate,
  });
  const portfolioReturn = Number(data?.portfolio_return_pct);
  // A null return is NOT 0: `Number(null)` is 0, which would print a
  // fabricated "+0.00%" for a benchmark we could not compute (M173).
  const benchmarkReturn =
    data?.benchmark_return_pct == null ? null : Number(data.benchmark_return_pct);
  const excessReturn = data?.excess_return_pct == null ? null : Number(data.excess_return_pct);
  // M65 / m61 #7, extended by M173: EXCESS is shown only against a REAL
  // benchmark return. Fewer than two cached closes in the window, or a
  // benchmark whose series cannot be expressed in the reporting currency
  // (SPY with no cached USD/CAD), leaves `benchmark_return_pct` null — gate
  // on that too, or the excess would just echo the portfolio return as alpha.
  const noData =
    data !== undefined && (data.benchmark_points < 2 || data.benchmark_return_pct == null);
  const converted =
    data?.benchmark_listing_currency != null &&
    data.benchmark_listing_currency !== data.benchmark_currency;

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-3">
        <CardTitle>vs Benchmark</CardTitle>
        <div className="flex gap-1">
          {BENCHMARKS.map(b => (
            <button
              key={b.symbol}
              onClick={() => setSelected(b.symbol)}
              disabled={noData && selected === b.symbol}
              title={noData && selected === b.symbol ? "No benchmark data for this window" : undefined}
              className={`px-2 py-1 text-xs rounded ${
                selected === b.symbol
                  ? noData
                    ? "bg-accent text-accent-ink opacity-40 cursor-not-allowed"
                    : "bg-accent text-accent-ink"
                  : "bg-surface-raised text-muted hover:text-text"
              }`}
            >
              {b.symbol}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <SkeletonRows rows={2} />
      ) : error || !data ? (
        <EmptyState
          icon={<TrendingUp />}
          title="No data"
          hint="The comparison runs over the fixed benchmark window — import or record trades to fill it."
          preview={{ src: "/help/investments.png", alt: "The Investments page with holdings and totals" }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <div className="text-xs text-muted uppercase">Portfolio</div>
              <div className={`text-lg font-semibold ${portfolioReturn >= 0 ? "text-gain" : "text-danger"}`}>
                {formatPct(portfolioReturn)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted uppercase">{data.benchmark_name}</div>
              {benchmarkReturn === null ? (
                <div className="text-sm text-muted">—</div>
              ) : (
                <div className={`text-lg font-semibold ${benchmarkReturn >= 0 ? "text-gain" : "text-danger"}`}>
                  {formatPct(benchmarkReturn)}
                </div>
              )}
            </div>
            <div>
              <div className="text-xs text-muted uppercase">Excess</div>
              {noData || excessReturn === null ? (
                <div className="text-sm text-muted">No benchmark data for this window</div>
              ) : (
                <div className={`text-lg font-semibold flex items-center justify-center gap-1 ${
                  excessReturn > 0 ? "text-gain" : excessReturn < 0 ? "text-danger" : "text-muted"
                }`}>
                  {excessReturn > 0 ? <TrendingUp className="size-4" /> :
                   excessReturn < 0 ? <TrendingDown className="size-4" /> :
                   <Minus className="size-4" />}
                  {formatPct(excessReturn)}
                </div>
              )}
            </div>
          </div>
          <div className="text-xs text-muted text-center">
            {formatDate(data.start_date)} — {formatDate(data.end_date)}
          </div>
          {!noData && converted && (
            <div className="text-xs text-muted text-center">
              {data.benchmark_name} is quoted in {data.benchmark_listing_currency}; the return above
              is FX-inclusive, converted to {data.benchmark_currency}.
            </div>
          )}
          {!noData && data.benchmark_clipped && data.benchmark_series_start && (
            <div className="text-xs text-muted text-center">
              Benchmark data starts {formatDate(data.benchmark_series_start)} — this window is clipped.
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
