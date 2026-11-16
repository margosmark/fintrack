// ChartFrame — the accessibility wrapper every chart that renders a bare
// <ResponsiveContainer> needs (docs/12 "Chart accessibility"):
//
//   <ChartFrame label={chartAriaLabel({...})}>
//     <ResponsiveContainer …>…</ResponsiveContainer>
//   </ChartFrame>
//
// role="img" + aria-label makes the chart ONE leaf node with a text
// equivalent; without it a screen reader walks hundreds of <path>/<rect>
// nodes and hears nothing useful.

import type { ReactNode } from "react";

export function ChartFrame({
  label,
  className = "",
  children,
}: {
  /** Built with `chartAriaLabel` from ./aria. */
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="img" aria-label={label} className={`w-full ${className}`}>
      {children}
    </div>
  );
}
