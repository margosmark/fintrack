import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Coins } from "lucide-react";
import { ApiError } from "../../api/client";
import { getDividendReport, type DividendReport } from "../../api/reports";
import { useChartPalette } from "../charts/palette";
import { fmtBucket, fmtCad } from "../charts/format";
import { Card, CardTitle } from "../ui/Card";
import { DividendProvenanceStrip } from "./DividendProvenanceStrip";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { StateRow } from "../ui/StateRow";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { useChartChrome } from "../charts/chartDefaults";
import { chartAriaLabel } from "../charts/aria";
import { inputClass } from "../ui/formStyles";
import { useMasked } from "../../lib/mask";

/**
 * Reports ▸ Dividends tab body (M14, docs/12 §Dividends): yearly income
 * overview per security with trade-date CAD conversion done server-side.
 * Self-contained — the page renders <DividendsPanel /> inside its tab frame.
 */

const STATS: readonly { label: string; value: (d: DividendReport) => string }[] = [
  { label: "Dividends", value: (d) => d.totals.dividend_cad },
  { label: "Distributions", value: (d) => d.totals.distribution_cad },
  { label: "Return of capital", value: (d) => d.totals.roc_cad },
  { label: "Trailing 12 months", value: (d) => d.ttm_total_cad },
];

// Compact year selector: shared base, inset surface (no forced width).
const FIELD = `${inputClass} bg-surface-raised`;

export function DividendsPanel({
  year: yearProp,
  onYearChange,
}: {
  /** M108 P2: pass `onYearChange` and the page OWNS the selector, so a saved
   *  view can restore this tab's year with its range. Without it the panel
   *  keeps its own state (standalone use, and the existing tests). */
  year?: number;
  onYearChange?: (year: number | undefined) => void;
} = {}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const chrome = useChartChrome();
  const [internalYear, setInternalYear] = useState<number | undefined>(undefined);
  const controlled = onYearChange !== undefined;
  const year = controlled ? yearProp : internalYear;
  const setYear = controlled ? onYearChange : setInternalYear;

  const report = useQuery({
    queryKey: ["dividends", year],
    queryFn: () => getDividendReport(year),
    retry: false,
  });

  if (report.isPending) return <StateRow variant="loading" title="Loading" />;
  if (report.isError) {
    return (
      <QueryErrorPanel
        query={report}
        message={report.error instanceof ApiError ? report.error.message : String(report.error)}
      />
    );
  }

  const data = report.data;
  const empty =
    data.items.length === 0 &&
    data.totals.total_cad === "0.00" &&
    data.ttm_total_cad === "0.00";

  // Year options: everything the server reports plus the current selection
  // ("Latest" = undefined → server picks the most recent year with rows).
  const years = [...data.available_years];
  if (!years.includes(data.year)) years.push(data.year);
  years.sort((a, b) => b - a);

  const chartLabel = chartAriaLabel({
    title: `Dividend income ${data.year}`,
    kind: "monthly buckets",
    range: String(data.year),
    points: data.by_month.length,
    facts: [
      `year total ${fmtCad(data.totals.total_cad)}`,
      `trailing 12 months ${fmtCad(data.ttm_total_cad)}`,
    ],
  });

  return (
    <div className="space-y-4">
      {/* Controls + totals strip */}
      <Card className="p-4">
        <CardTitle
          right={
            <label className="flex items-center gap-1.5 text-xs">
              <span className="text-muted">Year</span>
              <select
                aria-label="Dividend year"
                value={year ?? ""}
                onChange={(e) =>
                  setYear(e.target.value === "" ? undefined : Number(e.target.value))
                }
                className={FIELD}
              >
                <option value="">Latest</option>
                {years.map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </select>
            </label>
          }
        >
          Dividend income {data.year}
        </CardTitle>
        {/* docs/07 §M184 / docs/12 §Dividends: WHO produced these figures and
            HOW old the evidence is — the recorded trades themselves plus the
            trade-date FX cache that converts them, each with its own
            stale-or-missing verdict. Renders above the empty state too: when
            there is nothing to show, the reason lives in the source. */}
        <DividendProvenanceStrip provenance={data.provenance} className="mt-3" />
        {empty ? (
          <EmptyState
            icon={<Coins aria-hidden />}
            title={`No dividend income recorded for ${data.year}`}
            hint="Income actions (dividend, distribution, return of capital) recorded as trades show up here."
            preview={{ src: "/help/reports.png", alt: "The Reports page with charts for the selected range" }}
            action={{ label: "Record a trade", to: "/investments" }}
          />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {STATS.map(({ label, value }) => (
              <div
                key={label}
                className="rounded-lg border border-veil/[0.06] bg-veil/[0.03] px-3 py-2.5"
              >
                <p className="text-[10px] uppercase tracking-wider text-muted">{label}</p>
                <p className="mt-1 text-base font-medium tabular">{fmtCad(value(data))}</p>
              </div>
            ))}
          </div>
        )}
      </Card>

      {!empty && (
        <>
          {/* Monthly cadence */}
          <Card className="p-4">
            <CardTitle>Monthly income</CardTitle>
            <div role="img" aria-label={chartLabel}>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={data.by_month} margin={{ top: 8, right: 8, left: 8 }}>
                  <CartesianGrid stroke={palette.GRID_STROKE} vertical={false} />
                  <XAxis
                    dataKey="month"
                    tickFormatter={(m: string) => fmtBucket(m)}
                    tick={palette.AXIS_TICK}
                    tickLine={false}
                    axisLine={false}
                    minTickGap={12}
                  />
                  <YAxis
                    tick={palette.AXIS_TICK}
                    tickLine={false}
                    axisLine={false}
                    width={56}
                    tickFormatter={chrome.moneyAxis}
                  />
                  <Tooltip
                    content={<GlassTooltip />}
                    cursor={{ fill: palette.CURSOR_FILL }}
                    formatter={(value) => [fmtCad(String(value)), "Income"]}
                  />
                    <Bar
                      dataKey="total_cad"
                      name="Income (CAD)"
                      fill={palette.GAIN}
                      radius={[4, 4, 0, 0]}
                      maxBarSize={28}
                      {...chrome.anim}
                      animationDuration={500}
                    />
                  </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>

          {/* Per-security breakdown */}
          <Card role="region" aria-label="Dividends by security table" tabIndex={0} className="overflow-x-auto p-4">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2.5 font-medium">Symbol</th>
                  <th className="px-3 py-2.5 font-medium">Name</th>
                  <th className="px-3 py-2.5 text-right font-medium">Dividends</th>
                  <th className="px-3 py-2.5 text-right font-medium">Distributions</th>
                  <th className="px-3 py-2.5 text-right font-medium">ROC</th>
                  <th className="px-3 py-2.5 text-right font-medium">Total</th>
                  <th className="px-3 py-2.5 text-right font-medium">Payments</th>
                  <th className="px-3 py-2.5 text-right font-medium">Last payment</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.security_id} className="border-b border-veil/[0.04] last:border-0">
                    <td className="px-3 py-2 font-medium">{item.symbol}</td>
                    <td className="max-w-48 truncate px-3 py-2 text-muted">{item.name}</td>
                    <td className="px-3 py-2 text-right tabular">{fmtCad(item.dividend_cad)}</td>
                    <td className="px-3 py-2 text-right tabular">
                      {fmtCad(item.distribution_cad)}
                    </td>
                    <td className="px-3 py-2 text-right tabular">{fmtCad(item.roc_cad)}</td>
                    <td className="px-3 py-2 text-right font-medium tabular text-gain">
                      {fmtCad(item.total_cad)}
                    </td>
                    <td className="px-3 py-2 text-right tabular">{item.payment_count}</td>
                    <td className="px-3 py-2 text-right text-muted tabular">
                      {item.last_payment_date ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-veil/[0.06] text-sm font-medium">
                  <td className="px-3 py-2" colSpan={2}>
                    Total
                  </td>
                  <td className="px-3 py-2 text-right tabular">
                    {fmtCad(data.totals.dividend_cad)}
                  </td>
                  <td className="px-3 py-2 text-right tabular">
                    {fmtCad(data.totals.distribution_cad)}
                  </td>
                  <td className="px-3 py-2 text-right tabular">{fmtCad(data.totals.roc_cad)}</td>
                  <td className="px-3 py-2 text-right tabular text-gain">
                    {fmtCad(data.totals.total_cad)}
                  </td>
                  <td colSpan={2} className="px-3 py-2 text-right text-xs text-muted">
                    {data.unresolved_fx > 0
                      ? `${data.unresolved_fx} payment${data.unresolved_fx > 1 ? "s" : ""} without an FX rate excluded`
                      : ""}
                  </td>
                </tr>
              </tfoot>
            </table>
          </Card>
        </>
      )}
    </div>
  );
}
