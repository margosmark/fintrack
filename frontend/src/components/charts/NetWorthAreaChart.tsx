import { useId, useRef } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { NetWorthBucket } from "../../api/reports";
import { useChartPalette } from "./palette";
import { useChartChrome } from "./chartDefaults";
import { fmtBucket, toNumber } from "./format";
import { ChartGradient, GlassTooltip, safeId } from "../ui/charts/ChartTooltip";
import { ChartFrame } from "./ChartFrame";
import { axisTickInterval, useChartWidth } from "./thinning";
import { chartAriaLabel, dateRange, formatCad, granularityLabel } from "./aria";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Chart mapping — Net worth: stacked Areas (cash, positions) valued
 * at each bucket END + net Line with a pulsing active dot. Gradient fills via
 * SVG defs; quiet buckets carry forward (zero-fill).
 */
export function NetWorthAreaChart({
  buckets,
  granularity,
}: {
  buckets: NetWorthBucket[];
  granularity?: string;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  // The subscription also defeats Recharts' axisPropsAreEqual memo, which is
  // what redraws the money ticks (tests/m140ChartAxis.test.tsx).
  useMasked();

  const cashGrad = safeId(useId());
  const posGrad = safeId(useId());
  const palette = useChartPalette();
  const chrome = useChartChrome();
  // M126: bucket counts now follow the range (docs/12), so the tick density
  // has to follow the width — one formula, every range (thinning.ts).
  // M138 D-7: that width is the CHART's, not the viewport's — the card is
  // ~40% of a 1440 viewport, and a viewport-derived budget asked for 16
  // labels in 576px, which is where M133's 5 overlapping axis-label pairs
  // came from.
  const boxRef = useRef<HTMLDivElement>(null);
  const width = useChartWidth(boxRef);

  const data = buckets.map((bucket) => ({
    bucket: bucket.bucket_end,
    cash: toNumber(bucket.cash_cad),
    positions: toNumber(bucket.positions_cad),
    net: toNumber(bucket.net_cad),
  }));

  const label = chartAriaLabel({
    title: "Net worth trend",
    kind: granularityLabel(granularity),
    range: dateRange(buckets[0]?.bucket_end, buckets[buckets.length - 1]?.bucket_end),
    points: buckets.length,
    facts: data.length
      ? [
          `from ${formatCad(data[0].net)} to ${formatCad(data[data.length - 1].net)}`,
          `latest ${formatCad(data[data.length - 1].net)}`,
        ]
      : [],
  });

  return (
    <ChartFrame label={label}>
      <div ref={boxRef}>
      <ResponsiveContainer width="100%" height={320}>
        <ComposedChart data={data} margin={{ top: 8 }}>
          <ChartGradient id={`cash-${cashGrad}`} from={palette.ACCENT} />
          <ChartGradient id={`pos-${posGrad}`} from={palette.GAIN} />
          <CartesianGrid {...chrome.grid} vertical={false} />
          <XAxis
            dataKey="bucket"
            {...chrome.axis}
            // M138 D-7: `tickMargin` drops the label row clear of the
            // bottom-most Y tick — `-$3,500` and the first X label were
            // printing into each other by a pixel at the axis origin.
            // (Recharts anchors the tick text to the axis line, so the
            // axis' `height` alone does not move it — measured.)
            height={40}
            tickMargin={8}
            interval={axisTickInterval(data.length, width)}
            tickFormatter={(bucket: string) => fmtBucket(bucket, granularity)}
          />
          <YAxis
            {...chrome.axis}
            width={80}
            tickFormatter={chrome.moneyAxis}
          />
          <Tooltip content={<GlassTooltip />} cursor={chrome.cursorStroke} />
          <Legend {...chrome.legend} />
          <Area
            dataKey="cash"
            name="Cash"
            stackId="networth"
            fill={`url(#cash-${cashGrad})`}
            stroke={palette.ACCENT}
            strokeWidth={2}
            {...chrome.anim}
            animationDuration={900}
          />
          <Area
            dataKey="positions"
            name="Positions"
            stackId="networth"
            fill={`url(#pos-${posGrad})`}
            stroke={palette.GAIN}
            strokeWidth={2}
            {...chrome.anim}
            animationDuration={900}
          />
          {/* Radar-blip active dot: CSS pulse class scales it in place. */}
          <Line
            type="monotone"
            dataKey="net"
            name="Net"
            stroke={palette.WARN}
            strokeWidth={2}
            dot={false}
            activeDot={{
              r: 4.5,
              fill: palette.WARN,
              stroke: palette.WARN,
              strokeWidth: 5,
              strokeOpacity: 0.35,
              className: "chart-pulse-dot",
            }}
            {...chrome.anim}
            animationDuration={900}
          />
        </ComposedChart>
      </ResponsiveContainer>
      </div>
    </ChartFrame>
  );
}
