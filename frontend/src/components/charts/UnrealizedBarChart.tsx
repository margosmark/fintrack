import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PerformanceItem } from "../../api/reports";
import { useChartPalette } from "./palette";
import { useChartChrome } from "./chartDefaults";
import { toNumber } from "./format";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { ChartFrame } from "./ChartFrame";
import { chartAriaLabel, formatCad } from "./aria";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Chart mapping — Performance companion: horizontal bar of
 * unrealized CAD by symbol (registered holdings have unrealized=null and are
 * omitted from the bar, they still appear in the table). Bars color by sign:
 * semantic green gains / red losses only.
 */
export function UnrealizedBarChart({ items }: { items: PerformanceItem[] }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const chrome = useChartChrome();
  const data = items
    .filter((item) => item.unrealized_cad !== null)
    .map((item) => ({ symbol: item.symbol, unrealized: toNumber(item.unrealized_cad) }))
    .sort((a, b) => b.unrealized - a.unrealized);

  const totalUnrealized = data.reduce((sum, row) => sum + row.unrealized, 0);
  const top = data[0];
  const label = chartAriaLabel({
    title: "Unrealized gain/loss by symbol",
    kind: "horizontal bars",
    points: data.length,
    facts: data.length
      ? [`largest ${top.symbol} ${formatCad(top.unrealized)}`, `total ${formatCad(totalUnrealized)}`]
      : [],
  });

  return (
    <ChartFrame label={label}>
      <ResponsiveContainer width="100%" height={Math.max(200, data.length * 36 + 60)}>
        <BarChart data={data} layout="vertical" margin={{ left: 16 }}>
          <CartesianGrid {...chrome.grid} horizontal={false} />
          <XAxis
            type="number"
            {...chrome.axis}
            tickFormatter={chrome.moneyAxis}
          />
          <YAxis type="category" dataKey="symbol" {...chrome.axis} width={70} />
          <Tooltip content={<GlassTooltip />} cursor={chrome.cursorFill} />
          <Bar dataKey="unrealized" name="Unrealized (CAD)" radius={[0, 4, 4, 0]} maxBarSize={18} {...chrome.anim} animationDuration={800}>
            {data.map((entry) => (
              <Cell key={entry.symbol} fill={entry.unrealized >= 0 ? palette.GAIN : palette.LOSS} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
