// Sparkline (ui-contract move #3): tiny inline SVG trend for table rows
// (positions, cashflow) where a series exists. Pure SVG — no Recharts weight.
// Stroke defaults to the mode-aware accent (indigo in daybreak).

import { useId } from "react";
import { safeId } from "./ChartTooltip";
import { useChartPalette } from "../../charts/palette";

export function Sparkline({
  values,
  width = 96,
  height = 26,
  stroke,
  filled = true,
  className = "",
}: {
  values: number[];
  width?: number;
  height?: number;
  stroke?: string;
  filled?: boolean;
  className?: string;
}) {
  const gradId = safeId(useId());
  const lineColor = stroke ?? useChartPalette().ACCENT;

  if (values.length < 2) {
    // Placeholder-ready slot (positions table): quiet dash until a series exists.
    return (
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={className} aria-hidden>
        <line
          x1={4}
          x2={width - 4}
          y1={height / 2}
          y2={height / 2}
          stroke="currentColor"
          strokeOpacity={0.25}
          strokeDasharray="2 4"
          strokeLinecap="round"
          className="text-muted"
        />
      </svg>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const padY = 3;
  const stepX = (width - 2) / (values.length - 1);
  const points = values.map((v, i) => {
    const x = 1 + i * stepX;
    const y = padY + (1 - (v - min) / span) * (height - padY * 2);
    return [x, y] as const;
  });
  const line = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      aria-hidden
    >
      {filled && (
        <>
          <defs>
            <linearGradient id={`spark-${gradId}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity={0.32} />
              <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          <polygon
            points={`${line} ${width - 1},${height} 1,${height}`}
            fill={`url(#spark-${gradId})`}
            stroke="none"
          />
        </>
      )}
      <polyline
        points={line}
        fill="none"
        stroke={lineColor}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* End dot emphasizes the latest value. */}
      <circle cx={points[points.length - 1][0]} cy={points[points.length - 1][1]} r={2} fill={lineColor} />
    </svg>
  );
}
