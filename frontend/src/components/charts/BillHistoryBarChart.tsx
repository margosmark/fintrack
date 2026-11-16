// BillHistoryBarChart — one bar per period of a bill's matched payments
// (M116 B2.4, docs/25 §M116).
//
// Built from the SAME primitives every other chart here uses: `ChartFrame`
// (role="img" + one sentence — docs/12 "Chart accessibility"), `useChartChrome`
// (axis/grid/tooltip/animation props) and `fmtBucket` (the reports bucket-key
// label, which is why the server sends a bucket KEY and not a display string).
// No new charting dependency — the M77/M108 family, as the milestone pins.
//
// A period with no payment is a real data point, not an absent one: it renders
// as a zero-height bar carrying a `missed` marker, so the gap between two paid
// months is visible instead of being silently collapsed away.
//
// Accessibility: the frame is ONE leaf node with a text equivalent, and the
// `<details>` table underneath is the data-table fallback — keyboard-reachable
// through the native `summary` and walkable by a screen reader, so the
// per-period numbers are available without parsing an SVG.
//
// M122 T3: a dashed 6-period rolling-average LINE rides over the bars — its
// values come from the SERVER (`point.rolling_six`, the trailing ≤6-period
// mean ending at each point), so the line's last point IS the card's "Last 6
// periods" stat. It uses the same accent token as that stat, so the visual
// link is obvious; the fallback table carries the column too.
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { BillChartPoint } from "../../api/bills";
import { formatCad } from "../../lib/format";
import { chartAriaLabel, dateRange } from "./aria";
import { useChartChrome } from "./chartDefaults";
import { ChartFrame } from "./ChartFrame";
import { axisTickInterval, useViewportWidth } from "./thinning";
import { fmtBucket, toNumber } from "./format";
import { useChartPalette } from "./palette";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { useMasked } from "../../lib/mask";

/** Every granularity `fmtBucket` knows — M124 widened this from
 *  month|quarter|year so the category drill-down can reuse the chart at the
 *  reports page's own grain (which also offers day and week). */
type ChartGranularity = "day" | "week" | "month" | "quarter" | "year";

const KIND_LABELS: Record<ChartGranularity, string> = {
  day: "daily buckets",
  week: "weekly buckets",
  month: "monthly buckets",
  quarter: "quarterly buckets",
  year: "yearly buckets",
};

export function BillHistoryBarChart({
  points,
  granularity,
  title = "Payments by period",
  emptyMessage = "No payments match this rule yet.",
}: {
  points: BillChartPoint[];
  /** `fmtBucket` granularity: day | week | month | quarter | year. */
  granularity: ChartGranularity;
  title?: string;
  /** Shown when there is nothing to draw (M124: the drill-down reuses this
   *  chart and its empty state is "no spend", not "no payments"). */
  emptyMessage?: string;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const chrome = useChartChrome();
  // M126: the range control can narrow this to a handful of periods or widen
  // it to years of them — thin the axis from point count + viewport.
  const width = useViewportWidth();
  const first = points[0];
  const last = points[points.length - 1];
  const paid = points.filter((point) => !point.missed);
  const missed = points.length - paid.length;
  // M124: a drilled category has no payment COUNT, so the column would print
  // a meaningless 0 per row — hide it instead of inventing a number.
  const showCount = points.some((point) => point.count !== undefined);
  // M124: same rule for M122's rolling line — the drill-down's rows carry no
  // trailing-payment average, so a row of em-dashes would be a column of
  // invented nothings. Bills always send it, so their chart is unchanged.
  const showRolling = points.some((point) => point.rolling_six != null);
  const grandTotal = paid.reduce((sum, point) => sum + toNumber(point.total), 0);
  const peak = paid.reduce<BillChartPoint | null>(
    (best, point) =>
      best === null || toNumber(point.total) > toNumber(best.total) ? point : best,
    null,
  );

  const rows = points.map((point) => ({
    ...point,
    value: toNumber(point.total),
    // M122 T3: the SERVER-computed trailing 6-period mean; the last point's
    // value is the card's "Last 6 periods" stat by construction.
    rollingSix: point.rolling_six == null ? null : toNumber(point.rolling_six),
  }));
  const rollingLast = rows[rows.length - 1]?.rollingSix ?? null;

  const label = chartAriaLabel({
    title,
    kind: KIND_LABELS[granularity],
    range: dateRange(first?.period, last?.period),
    points: points.length,
    facts: [
      paid.length > 0 ? `total ${formatCad(grandTotal)}` : "",
      peak ? `highest ${peak.label} ${formatCad(peak.total)}` : "",
      rollingLast !== null ? `6-period average ends at ${formatCad(rollingLast)}` : "",
      missed > 0 ? `${missed} missed period${missed === 1 ? "" : "s"}` : "",
    ].filter(Boolean),
  });

  if (points.length === 0) {
    return (
      <ChartFrame label={`${title} — ${emptyMessage}`}>
        <p className="grid h-40 place-items-center text-sm text-muted">{emptyMessage}</p>
      </ChartFrame>
    );
  }

  return (
    <figure className="m-0">
      <ChartFrame label={label}>
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={rows} margin={{ top: 8 }}>
            <CartesianGrid {...chrome.grid} vertical={false} />
            <XAxis
              dataKey="period"
              {...chrome.axis}
              interval={axisTickInterval(rows.length, width)}
              tickFormatter={(period: string) => fmtBucket(period, granularity)}
            />
            <YAxis {...chrome.axis} width={70} tickFormatter={chrome.moneyAxis} />
            <Tooltip
              content={
                <GlassTooltip
                  formatter={(value: number) => formatCad(value)}
                  labelFormatter={(period: string) => fmtBucket(period, granularity)}
                />
              }
              cursor={chrome.cursorFill}
            />
            {/* A missed period has no money to draw, so its bar is
                zero-height BY DEFINITION — this dashed marker through the
                category is what keeps the gap visible on the time axis
                instead of looking like a bar that never rendered. */}
            {points
              .filter((point) => point.missed)
              .map((point) => (
                <ReferenceLine
                  key={point.period}
                  x={point.period}
                  stroke={palette.WARN}
                  strokeDasharray="4 4"
                />
              ))}
            <Bar
              dataKey="value"
              name="Total"
              fill={palette.SERIES_COLORS[0]}
              radius={[3, 3, 0, 0]}
              maxBarSize={42}
              {...chrome.anim}
              animationDuration={600}
            />
            {/* M122 T3: the 6-period rolling average, SERVER-computed per
                point — its last value IS the card's "Last 6 periods" stat.
                Dashed so it reads as a trend over the solid bars in both
                themes, on the same accent token the stats card uses. Hidden
                when the rows carry no such series (M124's drill-down). */}
            {showRolling && (
              <Line
                type="monotone"
                dataKey="rollingSix"
                name="6-period average"
                stroke={palette.ACCENT}
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={false}
                activeDot={{ r: 3 }}
                {...chrome.anim}
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      {/* Data-table fallback (M116 B2 testing): keyboard-reachable through the
          native `summary`, and a screen reader can walk the whole table. A
          missed period says "missed" rather than an empty cell. */}
      <details className="mt-1 rounded-lg border border-control">
        <summary className="cursor-pointer px-3 py-2 text-xs text-muted outline-none focus-visible:ring-2 focus-visible:ring-accent">
          Show the numbers behind this chart
        </summary>
        <div
          className="overflow-x-auto px-3 pb-3"
          tabIndex={0}
          aria-label="Payments by period, as a table"
        >
          <table className="w-full text-left text-xs">
            <caption className="sr-only">{label}</caption>
            <thead>
              <tr className="border-b border-control text-muted">
                <th scope="col" className="py-1 pr-3 font-medium">
                  Period
                </th>
                {showCount && (
                  <th scope="col" className="py-1 pr-3 text-right font-medium">
                    Payments
                  </th>
                )}
                <th scope="col" className="py-1 pr-3 text-right font-medium">
                  Total
                </th>
                {/* M122 T3: the line's numbers are in the fallback too — the
                    SVG is never the only way to read them. */}
                {showRolling && (
                  <th scope="col" className="py-1 text-right font-medium">
                    6-period avg
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.period} className="border-b border-veil/[0.05] last:border-b-0">
                  <th scope="row" className="py-1 pr-3 font-normal text-text">
                    {point.label}
                    {point.missed && <span className="ml-1 text-warn">missed</span>}
                  </th>
                  {showCount && (
                    <td className="py-1 pr-3 text-right tabular text-muted">{point.count ?? 0}</td>
                  )}
                  <td className="py-1 pr-3 text-right tabular text-text">{formatCad(point.total)}</td>
                  {showRolling && (
                    <td className="py-1 text-right tabular text-muted">
                      {point.rolling_six == null ? "—" : formatCad(point.rolling_six)}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
