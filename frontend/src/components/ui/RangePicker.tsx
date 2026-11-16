/**
 * Shared date-range picker (docs/14 follow-up): preset chips — every range
 * surface offers YTD — plus custom From/To inputs. Used by Dashboard,
 * Reports and the Transactions filter bar so range semantics stay identical
 * everywhere (YTD = Jan 1 → today, docs/12).
 */
import { useId } from "react";

import { SegmentedControl } from "./SegmentedControl";

export const RANGE_PRESETS = ["1W", "1M", "3M", "6M", "YTD", "1Y"] as const;
export type RangePreset = (typeof RANGE_PRESETS)[number];

export interface DateRange {
  from: string;
  to: string;
}

export function rangeFor(pick: RangePreset): DateRange {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  const to = `${y}-${m}-${d}`;
  if (pick === "YTD") return { from: `${y}-01-01`, to };
  if (pick === "1W") {
    const past = new Date(now);
    past.setDate(now.getDate() - 7);
    const py = past.getFullYear();
    const pm = String(past.getMonth() + 1).padStart(2, "0");
    const pd = String(past.getDate()).padStart(2, "0");
    return { from: `${py}-${pm}-${pd}`, to };
  }
  const months = pick === "1M" ? 1 : pick === "3M" ? 3 : pick === "6M" ? 6 : 12;
  const past = new Date(now);
  past.setMonth(past.getMonth() - months);
  const py = past.getFullYear();
  const pm = String(past.getMonth() + 1).padStart(2, "0");
  const pd = String(past.getDate()).padStart(2, "0");
  return { from: `${py}-${pm}-${pd}`, to };
}

export function isPresetRange(range: DateRange): RangePreset | null {
  return RANGE_PRESETS.find((p) => JSON.stringify(rangeFor(p)) === JSON.stringify(range)) ?? null;
}

interface RangePickerProps {
  from: string;
  to: string;
  onChange: (range: DateRange) => void;
  /** Extra chip that clears the custom range (Transactions filter bar). */
  allowAll?: boolean;
  size?: "sm" | "md";
}

export function RangePicker({ from, to, onChange, allowAll = false, size = "sm" }: RangePickerProps) {
  const layoutId = useId();
  const active = isPresetRange({ from, to });
  return (
    <div className="flex flex-wrap items-center gap-3">
      <SegmentedControl<string>
        size={size}
        ariaLabel="Date range presets"
        layoutId={layoutId}
        options={[
          ...(allowAll ? [{ value: "all", label: "All", title: "No date filter" }] : []),
          ...RANGE_PRESETS.map((pick) => {
            const r = rangeFor(pick);
            return { value: pick, label: pick, title: `${r.from} → ${r.to}` };
          }),
        ]}
        value={allowAll && from === "" && to === "" ? "all" : (active ?? "")}
        onChange={(value) => {
          if (value === "all") {
            onChange({ from: "", to: "" });
          } else {
            onChange(rangeFor(value as RangePreset));
          }
        }}
      />
      <label className="flex items-center gap-1 text-xs">
        <span className="text-muted">From</span>
        <input
          type="date"
          value={from}
          onChange={(e) => onChange({ from: e.target.value, to })}
          className="field-ring rounded-lg border border-control bg-surface-raised px-2 py-1 outline-none transition-colors hover:border-accent/70"
        />
      </label>
      <label className="flex items-center gap-1 text-xs">
        <span className="text-muted">To</span>
        <input
          type="date"
          value={to}
          onChange={(e) => onChange({ from, to: e.target.value })}
          className="field-ring rounded-lg border border-control bg-surface-raised px-2 py-1 outline-none transition-colors hover:border-accent/70"
        />
      </label>
    </div>
  );
}
