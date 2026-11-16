// Shared Recharts axis/legend/grid/cursor props (scout #4). The chart wrappers
// below repeated these literal objects verbatim; this hook centralizes them so
// a theme change only touches one place. Re-export-safe: no side effects.

import { createElement } from "react";
import type { ReactElement } from "react";
import { useChartPalette } from "./palette";
import { MASK_TEXT, isMasked, useMasked } from "../../lib/mask";

export interface ChartChrome {
  /** Shared XAxis / YAxis props (tick style, no lines). */
  axis: { tick: { fill: string; fontSize: number }; tickLine: false; axisLine: false };
  /** Shared Legend props (small circle swatches + series-colour override). */
  legend: {
    wrapperStyle: { fontSize: number; color: string };
    iconType: "circle";
    iconSize: number;
    formatter: (value: string) => ReactElement;
  };
  /** Shared CartesianGrid stroke. */
  grid: { stroke: string };
  /** Hover band behind bars/columns. */
  cursorFill: { fill: string };
  /** Hover tracking line for area/line charts. */
  cursorStroke: { stroke: string };
  /** Compact "$165K" money axis tick formatter — REACTIVE (docs/46 §D3): the
   *  identity is rebuilt every render of the subscribed component, so a mask
   *  flip hands recharts a new prop and `axisPropsAreEqual` misses instead of
   *  bailing on a stable reference. Never memoize it, never destructure it
   *  outside a render. */
  moneyAxis: (v: number) => string;
  /** Animation switch for chart primitives — off under reduced-motion. */
  anim: { isAnimationActive: boolean };
}

/** True when the OS asks for reduced motion (hooks into matchMedia). */
export function useReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function")
    return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Legend label colour (docs/09 "Chart palette").
 *
 * Recharts' `DefaultLegendContent` paints each label span in the SERIES colour
 * (`finalLabelStyle.color = finalLabelStyle.color || entry.color`), so a light
 * theme legend fails 4.5:1 — the audited "housing" label (`#d97706`) measures
 * 2.92:1 on the card. Returning a span with its own inline colour overrides
 * that inherited series colour; the swatch icon keeps the series colour, so the
 * colour→series mapping stays readable. Every chart consumer inherits this by
 * spreading `chrome.legend`.
 */
export function legendFormatter(color: string): (value: string) => ReactElement {
  return (value: string) => createElement("span", { style: { color } }, value);
}

// Money axis ticks (docs/09 chart palette / money displays): compact with the
// sign on the outside — `$165K`, `-$55K`. This is NOT `formatCadCompact()`,
// which forces a fraction digit in compact notation ("$165.0K") and two below
// it ("$5,000.00") — both read as noise on a gridline. Below $10K the tick
// stays exact but drops the forced cents ("$5,000", "-$55", "$12.5").
const AXIS_COMPACT = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "CAD",
  notation: "compact",
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
});
const AXIS_EXACT = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "CAD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

/** Compact money axis tick: `$165K` / `-$55K` ≥ $10K, `$5,000` / `-$55` below.
 *  M139: axis ticks are absolute CAD, so they mask with the session. A masked
 *  chart draws every tick as bullets — the SHAPE of the line stays readable
 *  (proportional), the scale does not.
 *
 *  NOTE (why no `masked?` parameter here, unlike the other M139 formatters):
 *  Recharts calls `tickFormatter(value, index)`. A hand-written second
 *  parameter silently receives the tick's INDEX — truthy for every tick except
 *  the first — so `v === 0` would render dollars while its neighbours rendered
 *  bullets. Axis formatting therefore has exactly one input and consults the
 *  ambient mask directly; never thread a positional mask flag through a
 *  Recharts callback. */
export function formatAxisMoney(v: number): string {
  if (isMasked()) return MASK_TEXT;
  const n = v === 0 ? 0 : v; // `-0` must not print "-$0"
  return Math.abs(n) >= 10_000 ? AXIS_COMPACT.format(n) : AXIS_EXACT.format(n);
}

export function useChartChrome(): ChartChrome {
  const palette = useChartPalette();
  // F-7 (docs/46 §D3): the hook SUBSCRIBES to the mask, so every component
  // that builds its chrome re-renders on a flip — including ones with no
  // `useMasked()` of their own (Forecast) — and `moneyAxis` below is a FRESH
  // closure on that pass. That pair IS the reactive axis contract the B1
  // blocker fix introduced for AccountValueChart: a new prop identity per
  // render is what makes recharts' `axisPropsAreEqual` memo miss and the tick
  // layer re-run `formatAxisMoney`, which reads the ambient mask at call time.
  // Stable-reference formatters freeze the axis at mount state (three of the
  // nine F-7 charts reproduced live; the rest repainted only by the accident
  // of a per-render `data` array). Deliberately NOT useMemo/useCallback: the
  // D6 sweep fails on money built inside either, and the closure must be new
  // every pass for the memo to miss.
  const masked = useMasked();
  return {
    axis: { tick: palette.AXIS_TICK, tickLine: false, axisLine: false },
    legend: {
      wrapperStyle: { fontSize: 12, color: palette.LEGEND_COLOR },
      iconType: "circle",
      iconSize: 8,
      formatter: legendFormatter(palette.LEGEND_COLOR),
    },
    grid: { stroke: palette.GRID_STROKE },
    cursorFill: { fill: palette.CURSOR_FILL },
    cursorStroke: { stroke: palette.CURSOR_STROKE },
    // Branch on THIS render's `masked` value (B1's contract), not on a global
    // read at call time — the branch is the part that must be visible to the
    // prop comparison. `formatAxisMoney` stays the one formatter (and stays
    // fail-closed: it consults the ambient mask itself if asked directly).
    moneyAxis: (v: number) => (masked ? MASK_TEXT : formatAxisMoney(v)),
    // No rAF-driven animation under reduced-motion: primitives render
    // their final state immediately (also fixes headless screenshots).
    anim: { isAnimationActive: !useReducedMotion() },
  };
}
