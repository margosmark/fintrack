// Shared Recharts chrome (ui-contract move #3): glass custom tooltip used
// by EVERY chart, plus theme-aware gradient defs helper (accent →
// transparent fills).
import { useChartPalette } from "../../charts/palette";
import { fmtCad } from "../../charts/format";
import { useMasked } from "../../../lib/mask";

interface TooltipEntry {
  name?: string | number;
  value?: string | number;
  color?: string;
  fill?: string;
  stroke?: string;
  dataKey?: string | number;
}

export interface GlassTooltipProps {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
  /** (value, name) => display string; defaults to CAD money. */
  formatter?: (value: number, name: string) => string;
  labelFormatter?: (label: string) => string;
}

/** Dark-glass tooltip shared by all Recharts usages. */
export function GlassTooltip({
  active,
  payload,
  label,
  formatter,
  labelFormatter,
}: GlassTooltipProps) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  if (active !== true || payload === undefined || payload.length === 0) return null;
  const fmt = formatter ?? ((v: number) => fmtCad(String(v)));
  return (
    <div className="glass-strong min-w-36 rounded-lg px-3 py-2 text-xs shadow-card">
      {label !== undefined && label !== "" && (
        <p className="mb-1.5 font-medium text-muted">
          {labelFormatter !== undefined && typeof label === "string"
            ? labelFormatter(label)
            : label}
        </p>
      )}
      <div className="space-y-1">
        {payload.map((entry, i) => {
          const numeric = typeof entry.value === "number" ? entry.value : Number(entry.value);
          const color = entry.color ?? entry.stroke ?? entry.fill ?? "rgb(148 156 170)";
          return (
            <div key={`${entry.dataKey ?? i}`} className="flex items-center gap-2">
              <span
                className="size-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: color }}
                aria-hidden
              />
              <span className="text-muted">{String(entry.name ?? entry.dataKey ?? "")}</span>
              <span className="ml-auto pl-4 font-medium tabular">
                {Number.isNaN(numeric)
                  ? String(entry.value)
                  : fmt(numeric, String(entry.name ?? ""))}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Vertical fade gradient for Area/Bar fills: `from` at ~42% opacity fading to
 * transparent. Consumers own the id (sanitized via safeId) and reference it as
 * fill={`url(#${id})`}.
 */
export function ChartGradient({ id, from }: { id: string; from: string }) {
  // Daybreak keeps fills airy: top stop-alpha 0.20 vs dark neon 0.42.
  const { GRAD_TOP } = useChartPalette();
  return (
    <defs>
      <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor={from} stopOpacity={GRAD_TOP} />
        <stop offset="88%" stopColor={from} stopOpacity={GRAD_TOP * 0.07} />
        <stop offset="100%" stopColor={from} stopOpacity={0} />
      </linearGradient>
    </defs>
  );
}

/**
 * useId() output contains colons which are invalid inside url(#…) references;
 * every chart sanitizes through this one helper so SVG defs stay resolvable.
 */
export function safeId(raw: string): string {
  return raw.replace(/[^a-zA-Z0-9]/g, "");
}
