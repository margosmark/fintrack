import { useId } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CashflowBucket } from "../../api/reports";
import { useChartPalette } from "./palette";
import { useChartChrome } from "./chartDefaults";
import { fmtBucket, toNumber } from "./format";
import { ChartGradient, GlassTooltip, safeId } from "../ui/charts/ChartTooltip";
import { ChartFrame } from "./ChartFrame";
import { chartAriaLabel, dateRange, formatCad, granularityLabel } from "./aria";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Chart mapping — Cash flow: ComposedChart with ± bars for
 * income/expenses (income above the axis, expenses below) and a net line.
 * Gradient fills on both bars; net line in amber (momentum).
 */
export function CashflowComposedChart({
  buckets,
  granularity,
}: {
  buckets: CashflowBucket[];
  granularity?: string;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const incomeGrad = safeId(useId());
  const expenseGrad = safeId(useId());
  const palette = useChartPalette();
  const chrome = useChartChrome();

  const data = buckets.map((bucket) => ({
    bucket: bucket.bucket,
    income: toNumber(bucket.income_cad),
    expenses: -toNumber(bucket.expenses_cad),
    net: toNumber(bucket.net_cad),
  }));

  const sumIncome = data.reduce((sum, row) => sum + row.income, 0);
  const sumExpenses = data.reduce((sum, row) => sum - row.expenses, 0);
  const sumNet = sumIncome - sumExpenses;
  const label = chartAriaLabel({
    title: "Cash flow",
    kind: granularityLabel(granularity),
    range: dateRange(buckets[0]?.bucket, buckets[buckets.length - 1]?.bucket),
    points: buckets.length,
    facts: buckets.length
      ? [
          `income ${formatCad(sumIncome)}`,
          `expenses ${formatCad(sumExpenses)}`,
          `net ${formatCad(sumNet)}`,
        ]
      : [],
  });

  return (
    <ChartFrame label={label}>
      <ResponsiveContainer width="100%" height={320}>
        <ComposedChart data={data} margin={{ top: 8 }}>
          <ChartGradient id={`in-${incomeGrad}`} from={palette.GAIN} />
          <ChartGradient id={`out-${expenseGrad}`} from={palette.LOSS} />
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
          <Tooltip content={<GlassTooltip />} cursor={chrome.cursorFill} />
          <Legend {...chrome.legend} />
          <Bar
            dataKey="income"
            name="Income"
            fill={`url(#in-${incomeGrad})`}
            stroke={palette.GAIN}
            strokeWidth={1}
            radius={[3, 3, 0, 0]}
            maxBarSize={42}
            {...chrome.anim}
            animationDuration={800}
          />
          <Bar
            dataKey="expenses"
            name="Expenses"
            fill={`url(#out-${expenseGrad})`}
            stroke={palette.LOSS}
            strokeWidth={1}
            radius={[0, 0, 3, 3]}
            maxBarSize={42}
            {...chrome.anim}
            animationDuration={800}
          />
          <Line
            type="monotone"
            dataKey="net"
            name="Net"
            stroke={palette.WARN}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
            {...chrome.anim}
            animationDuration={800}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
