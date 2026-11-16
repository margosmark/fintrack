// ChartRangeControl — the chart-header range selector (M126, docs/12
// §Chart ranges, docs/09 §Range control).
//
// Presets only, deliberately: it lives in a card header next to the title, so
// it must be ONE short wrapping row (never an overflow-scrolled strip — at
// 390/412 the seven labels wrap onto two lines and every option stays visible
// and hit-testable). The page-level `RangePicker` keeps its free-form From/To
// inputs; this control is for "show me a window", not "type a window".
//
// a11y: a labelled GROUP of toggle buttons. `aria-pressed` is the state a
// screen reader announces for "which range am I on" — not `aria-selected`,
// which would claim tab semantics this control does not have (no tabpanel
// follows it). Buttons are native, so Tab reaches every option and the
// focus-visible ring is the same `focus-visible:ring-accent` the rest of the
// app uses.

import { CHART_RANGES, rangeWindow, type ChartRange } from "../../lib/chartRange";

export function ChartRangeControl({
  value,
  onChange,
  ariaLabel = "Chart time range",
  className = "",
  /** Rendered under a chart title in a flex header row. */
  size = "sm",
}: {
  value: ChartRange;
  onChange: (next: ChartRange) => void;
  ariaLabel?: string;
  className?: string;
  size?: "sm" | "md";
}) {
  const pad = size === "sm" ? "px-2 py-1 text-xs" : "px-2.5 py-1.5 text-sm";
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={`flex flex-wrap items-center gap-1 ${className}`}
    >
      {CHART_RANGES.map((range) => {
        const active = range === value;
        const window = rangeWindow(range);
        return (
          <button
            key={range}
            type="button"
            aria-pressed={active}
            title={
              range === "All"
                ? "Full cached history"
                : `From ${window.from} to ${window.to}`
            }
            onClick={() => onChange(range)}
            className={`shrink-0 cursor-pointer rounded-md outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${pad} ${
              active
                ? "bg-veil/[0.08] font-medium text-accent ring-1 ring-inset ring-veil/[0.06]"
                : "text-muted hover:text-text"
            }`}
          >
            {range}
          </button>
        );
      })}
    </div>
  );
}
