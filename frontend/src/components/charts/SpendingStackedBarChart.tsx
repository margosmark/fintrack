import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SpendSlice } from "../../api/reports";
import { useChartPalette } from "./palette";
import { useChartChrome } from "./chartDefaults";
import { fmtBucket, toNumber } from "./format";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { ChartFrame } from "./ChartFrame";
import { chartAriaLabel, dateRange, formatCad, granularityLabel } from "./aria";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Chart mapping — Spending: stacked BarChart, one bar per bucket,
 * stacked by the active dimension's series. Uncategorized (or Untagged, or
 * the Owner split) is its own series, never dropped. Bars get rounded caps +
 * animated draw-in (bounded duration).
 *
 * M124 (docs/12 §1): takes `SpendSlice[]`, so the dimension is already
 * flattened — the chart renders categories, tags or owners without knowing
 * which. M124 R2 adds the per-series click: each `<Bar>` is one series, so a
 * click handler on it fires for that series' rectangles only, which is how
 * the drill knows WHICH segment was hit without any hit-testing of its own.
 */
export function SpendingStackedBarChart({
  items,
  granularity,
  title = "Spending by category",
  onDrill,
}: {
  /** Slices with the dimension already flattened (M124, docs/12 §1). */
  items: SpendSlice[];
  granularity?: string;
  /** Accessible summary title (docs/12) — "Spending by owner" under M108 P3,
   *  "Spending by tag" under M124. */
  title?: string;
  /**
   * M124 R2: a bar segment was clicked. OMITTED where the dimension has no
   * drill target (Owner — "Partner" is not a category) or where the page
   * chose not to offer one; without it the bars are inert.
   */
  onDrill?: (id: number | null) => void;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const chrome = useChartChrome();
  const bucketKeys = [...new Set(items.map((item) => item.bucket))].sort();
  const series = [
    ...new Map(items.map((item) => [item.name, item.id] as const)).entries(),
  ].sort((a, b) => (a[1] ?? -1) - (b[1] ?? -1));

  const data = bucketKeys.map((bucket) => {
    const row: Record<string, string | number> = { bucket };
    for (const [name] of series) row[name] = 0;
    return row;
  });
  const rowIndex = new Map(bucketKeys.map((bucket, i) => [bucket, i]));
  for (const item of items) {
    const i = rowIndex.get(item.bucket);
    if (i !== undefined) {
      data[i][item.name] = toNumber(item.total_cad);
    }
  }

  const bucketTotals = bucketKeys.map((bucket) => data[rowIndex.get(bucket) ?? 0]);
  const totals = bucketTotals.map((row) =>
    series.reduce((sum, [name]) => sum + Number(row[name] ?? 0), 0),
  );
  const grandTotal = totals.reduce((sum, v) => sum + v, 0);
  const peakIndex = totals.indexOf(Math.max(...totals));
  const label = chartAriaLabel({
    title,
    kind: granularityLabel(granularity),
    range: dateRange(bucketKeys[0], bucketKeys[bucketKeys.length - 1]),
    points: bucketKeys.length,
    facts:
      bucketKeys.length && grandTotal > 0
        ? [
            `total ${formatCad(grandTotal)}`,
            `peak ${bucketKeys[peakIndex]} ${formatCad(totals[peakIndex])}`,
          ]
        : [],
  });

  return (
    <ChartFrame label={label}>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} margin={{ top: 8 }}>
          <CartesianGrid {...chrome.grid} vertical={false} />
          <XAxis
            dataKey="bucket"
            {...chrome.axis}
            tickFormatter={(bucket: string) => fmtBucket(bucket, granularity)}
          />
          <YAxis
            {...chrome.axis}
            width={70}
            tickFormatter={chrome.moneyAxis}
          />
          <Tooltip
            content={<GlassTooltip />}
            cursor={chrome.cursorFill}
          />
          <Legend {...chrome.legend} />
          {series.map(([name, id], i) => (
            <Bar
              key={name}
              dataKey={name}
              stackId="spending"
              fill={palette.SERIES_COLORS[i % palette.SERIES_COLORS.length]}
              radius={[3, 3, 0, 0]}
              maxBarSize={42}
              /* M124 R2: ONE handler per <Bar> element, and each <Bar> owns
                 exactly the rectangles of its own series — so the id is known
                 from the closure and no hit-testing is needed to tell which
                 segment of the stack was clicked. */
              onClick={onDrill ? () => onDrill(id ?? null) : undefined}
              style={onDrill ? { cursor: "pointer" } : undefined}
              {...chrome.anim}
              animationDuration={800}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
