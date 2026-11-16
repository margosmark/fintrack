import { useId } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SecurityOverview } from "../../api/securities";
import { useChartPalette } from "./palette";
import { useChartChrome } from "./chartDefaults";
import { fmtBucket, toNumber } from "./format";
import { ChartGradient, GlassTooltip, safeId } from "../ui/charts/ChartTooltip";
import { ChartFrame } from "./ChartFrame";
import { chartAriaLabel, dateRange, formatMoney } from "./aria";
import { ChartRangeControl } from "../ui/ChartRangeControl";
import { axisTickInterval, useViewportWidth } from "./thinning";
import type { ChartRange, SeriesGranularity } from "../../lib/chartRange";
import { useMasked } from "../../lib/mask";

/**
 * docs/09 — Security dashboard: cached-close price history as a single
 * gradient area. Same glass chrome as NetWorthAreaChart.
 *
 * M126: the chart carries the range selector in ITS OWN header (docs/12
 * §Chart ranges) — the range is a property of the chart, not of the page, so
 * the security page and the positions expansion can offer it independently.
 * The series arrives already bucketed (the server picks day/week/month/year
 * from the window), and the x-axis thins from the point count + viewport
 * (`thinning.ts`).
 */
export function PriceHistoryChart({
  history,
  symbol,
  currency = "CAD",
  range,
  onRangeChange,
  granularity = "day",
}: {
  history: SecurityOverview["history"];
  /** Shown in the summary label ("XEQT price history"). */
  symbol?: string;
  /** Listing currency for the price figures. */
  currency?: string;
  /** Current range — only read when `onRangeChange` is given. */
  range?: ChartRange;
  /** Supplies the header range control (M126). Omit to render chart only. */
  onRangeChange?: (next: ChartRange) => void;
  /** The bucket size the SERVER answered with — drives the tick labels. */
  granularity?: SeriesGranularity;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const grad = safeId(useId());
  const palette = useChartPalette();
  const chrome = useChartChrome();
  const width = useViewportWidth();

  const data = history.map((point) => ({
    date: point.date,
    price: toNumber(point.price),
  }));

  const first = data[0];
  const last = data[data.length - 1];
  const title = symbol ? `${symbol} price history` : "Price history";
  const label = chartAriaLabel({
    title,
    kind: granularity === "day" ? "daily closes" : `${granularity} closes`,
    range: dateRange(first?.date, last?.date),
    points: data.length,
    facts: data.length
      ? [
          `from ${formatMoney(first.price, currency)} to ${formatMoney(last.price, currency)}`,
          `latest ${formatMoney(last.price, currency)}`,
        ]
      : [],
  });

  const chart = (
    <ChartFrame label={label}>
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={data} margin={{ top: 8 }}>
          <ChartGradient id={`price-${grad}`} from={palette.ACCENT} />
          <CartesianGrid {...chrome.grid} vertical={false} />
          <XAxis
            dataKey="date"
            {...chrome.axis}
            interval={axisTickInterval(data.length, width)}
            tickFormatter={(date: string) => fmtBucket(date, granularity)}
          />
          <YAxis
            {...chrome.axis}
            width={70}
            domain={["auto", "auto"]}
            tickFormatter={chrome.moneyAxis}
          />
          <Tooltip content={<GlassTooltip />} cursor={chrome.cursorStroke} />
          <Area
            dataKey="price"
            name="Price"
            type="monotone"
            fill={`url(#price-${grad})`}
            stroke={palette.ACCENT}
            strokeWidth={2}
            {...chrome.anim}
            animationDuration={900}
          />
        </AreaChart>
      </ResponsiveContainer>
    </ChartFrame>
  );

  if (onRangeChange === undefined) return chart;

  return (
    <div className="space-y-2">
      <PriceHistoryHeader title={title} range={range ?? "1M"} onChange={onRangeChange} />
      {chart}
    </div>
  );
}

/**
 * The title + range-selector row (M126). Exported so the loading / error /
 * empty states of `PriceHistoryPanel` render the SAME header as the chart —
 * the control must not disappear while a range is fetching, or a user who
 * changes range during a slow request cannot see that they did.
 *
 * `flex-wrap` (not overflow) so at 390/412 the two stack instead of hiding a
 * control behind a horizontal scroll (docs/09 §Range control).
 */
export function PriceHistoryHeader({
  title,
  range,
  onChange,
}: {
  title: string;
  range: ChartRange;
  onChange: (next: ChartRange) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-[11px] font-medium uppercase tracking-widest text-muted">
        {title}
      </span>
      <ChartRangeControl value={range} onChange={onChange} ariaLabel={`${title} range`} />
    </div>
  );
}
