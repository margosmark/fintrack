import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { ReactElement } from "react";
import { useChartPalette } from "./palette";
import { legendFormatter, useReducedMotion } from "./chartDefaults";
import { AnimatedNumber } from "../ui/AnimatedNumber";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { formatCadCompact } from "../../lib/format";
import { chartAriaLabel, shareFact } from "./aria";
import { useMasked } from "../../lib/mask";

export interface DonutDatum {
  name: string;
  value: number;
  [key: string]: unknown;
}

export interface DonutProps {
  data: DonutDatum[];
  /** Numeric value shown in the center label. */
  centerValue: number;
  /** Caption under the center value. */
  centerLabel: string;
  /** Center-value formatter; defaults to compact CAD. */
  centerFormat?: (n: number) => string;
  /**
   * F-9 (docs/46 §D2): the money fact in the chart's TEXT ALTERNATIVE.
   * Defaults to `total <centerFormat(total)>`, which is right when the centre
   * IS the money total (`CategoriesDonut`) — `centerFormat` is the mask-aware
   * default there, so the sentence masks itself.
   *
   * It is WRONG for a donut whose centre is a COUNT and whose slices are
   * native values in DIFFERENT currencies (`ExposureDonut`): `total` would sum
   * across denominations — the very dishonesty that donut's own docstring
   * forbids — and, read through a count formatter, would print that money RAW
   * while masked. Such a donut owns this fact instead. `null` states no total
   * at all (the label then carries only the share fact).
   */
  totalFact?: string | null;
  /** Custom tooltip content (a GlassTooltip element); defaults to plain. */
  tooltip?: ReactElement;
  height?: number;
  /** Accessible summary title, e.g. "Spending by category" (docs/12). */
  title?: string;
  /**
   * M124 R2 (docs/12 §2): make slices drillable. Omit it and the donut is
   * exactly the read-only chart it was before — which is why the Investments
   * page's ExposureDonut is untouched by this change.
   *
   * The recharts `<Legend>` cannot take focus, so passing `onSelect` ALSO
   * swaps it for an HTML legend of buttons: the keyboard path is the same
   * swatch+name+value list, just focusable. That is the treemap's precedent
   * applied to the donut (docs/09 §Keyboard: every chart action reachable
   * without a pointer).
   */
  onSelect?: (index: number) => void;
}

/**
 * Shared donut (docs/12 §Chart mapping): categorical PieChart over the
 * relative palette, with a center caption label. Both CategoriesDonut and
 * ExposureDonut render through this — only their data + center value differ —
 * so the radius/legend/palette boilerplate lives in exactly one place.
 */
export function Donut({
  data,
  centerValue,
  centerLabel,
  centerFormat = formatCadCompact,
  totalFact,
  tooltip = <GlassTooltip />,
  height = 320,
  title = "Breakdown",
  onSelect,
}: DonutProps) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const reducedMotion = useReducedMotion();
  const total = data.reduce((sum, d) => sum + Number(d.value), 0);
  const top = [...data].sort((a, b) => Number(b.value) - Number(a.value))[0];
  // F-9: the caller-owned fact wins; the default is the mask-aware
  // `centerFormat(total)` (compact CAD for a money-centred donut).
  const moneyFact = totalFact ?? (data.length ? `total ${centerFormat(total)}` : null);
  const label = chartAriaLabel({
    title,
    kind: "categorical breakdown",
    points: data.length,
    facts: data.length
      ? [
          top ? shareFact(String(top.name), Number(top.value), total) : "",
          moneyFact ?? "",
        ]
      : [],
  });
  // A focusable control inside `role="img"` is axe's `nested-interactive`
  // violation: `role=img` promises "one node, no descendants with semantics",
  // and a drillable legend breaks that promise. So the role FOLLOWS the
  // interactivity — a read-only donut stays one image, a drillable one becomes
  // a labelled group whose buttons are legitimate children.
  return (
    <div
      role={onSelect ? "group" : "img"}
      aria-label={label}
      className="relative"
    >
      <ResponsiveContainer width="100%" height={height}>
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            innerRadius="62%"
            outerRadius="88%"
            paddingAngle={2}
            cornerRadius={4}
            isAnimationActive={!reducedMotion}
            animationDuration={850}
          >
            {data.map((entry, i) => (
              <Cell
                key={entry.name}
                fill={palette.SERIES_COLORS[i % palette.SERIES_COLORS.length]}
                onClick={onSelect ? () => onSelect(i) : undefined}
                style={onSelect ? { cursor: "pointer" } : undefined}
              />
            ))}
          </Pie>
          <Tooltip content={tooltip} />
          {onSelect ? (
            /* The keyboard path (docs/09 §Keyboard): recharts' Legend renders
               SVG text that cannot take focus, so a drillable donut swaps it
               for real buttons — same swatch, same name, same value. */
            <ul className="mt-1 flex flex-wrap justify-center gap-x-3 gap-y-1 px-2">
              {data.map((entry, i) => (
                <li key={entry.name}>
                  <button
                    type="button"
                    onClick={() => onSelect(i)}
                    className="flex items-center gap-1.5 rounded px-1 py-0.5 text-xs text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
                    aria-label={`${entry.name}, ${formatCadCompact(Number(entry.value))}, show detail`}
                  >
                    <span
                      aria-hidden="true"
                      className="size-2 shrink-0 rounded-full"
                      style={{
                        backgroundColor:
                          palette.SERIES_COLORS[i % palette.SERIES_COLORS.length],
                      }}
                    />
                    <span>{entry.name}</span>
                    <span className="tabular">{formatCadCompact(Number(entry.value))}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Legend
              wrapperStyle={{ fontSize: 12, color: palette.LEGEND_COLOR }}
              iconType="circle"
              iconSize={8}
              formatter={legendFormatter(palette.LEGEND_COLOR)}
            />
          )}
        </PieChart>
      </ResponsiveContainer>
      {/* Center label — range total / count (pointer-events none keeps hover). */}
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center pb-8">
        <AnimatedNumber value={centerValue} format={centerFormat} className="text-xl font-semibold" />
        <span className="mt-0.5 text-[11px] uppercase tracking-widest text-muted">{centerLabel}</span>
      </div>
    </div>
  );
}
