// choropleth — the 6-step sequential scale behind the Exposure card's world
// map (M167, docs/09 §Exposure visuals).
//
// The map answers exactly one question per country: how much of the portfolio
// sits there. That is a QUANTITY, so the scale is sequential (light →
// saturated with weight), not the categorical SERIES palette the donut uses —
// but it is built from the SAME house token the donut gives its first slice
// (`palette.ACCENT`), mixed against the same card surface, in both themes.
//
// Honesty rules encoded here (M147's family):
//   * a weight of 0 and a country the payload never mentioned both return
//     `null` — the map cannot tell "nothing there" from "not in the data", so
//     it draws the same neutral land for both and never claims a zero;
//   * the buckets are FIXED (Under 1 / 1–2.5 / 2.5–5 / 5–10 / 10–25 / 25+),
//     not derived from the biggest row, so two accounts' maps are comparable
//     and a legend can name them once;
//   * the numbers themselves live in the table and the per-country readout —
//     shading is a picture of the list, never a replacement for it.

import type { ChartPalette } from "./palette";
import type { Theme } from "../../theme/ThemeProvider";

/** Upper bound (exclusive, weight %) of each bucket; six steps, last open. */
export const WEIGHT_BREAKPOINTS = [1, 2.5, 5, 10, 25] as const;

/** Legend copy — one label per step, in scale order. */
export const WEIGHT_BUCKET_LABELS = [
  "Under 1%",
  "1–2.5%",
  "2.5–5%",
  "5–10%",
  "10–25%",
  "25% or more",
] as const;

/**
 * Bucket index 0–5 for a weight, or `null` for "no exposure to draw".
 * 0 and negative weights are `null`: the map shades what the portfolio holds,
 * and a zero-weight row is not a shaded country at 0%.
 */
export function weightBucket(weightPct: number | null | undefined): number | null {
  if (weightPct === null || weightPct === undefined) return null;
  if (!Number.isFinite(weightPct) || weightPct <= 0) return null;
  for (let i = 0; i < WEIGHT_BREAKPOINTS.length; i += 1) {
    if (weightPct < WEIGHT_BREAKPOINTS[i]) return i;
  }
  return WEIGHT_BREAKPOINTS.length;
}

/** sRGB hex → [r, g, b]. */
function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

/** Lighten/darken perceptionally: mix in LINEAR light, re-encode to sRGB. */
function mixHex(from: string, to: string, t: number): string {
  const a = hexToRgb(from);
  const b = hexToRgb(to);
  const channels = [0, 1, 2].map((i) => {
    const la = a[i] / 255 <= 0.04045 ? a[i] / 255 / 12.92 : ((a[i] / 255 + 0.055) / 1.055) ** 2.4;
    const lb = b[i] / 255 <= 0.04045 ? b[i] / 255 / 12.92 : ((b[i] / 255 + 0.055) / 1.055) ** 2.4;
    const l = la * (1 - t) + lb * t;
    const s = l <= 0.0031308 ? 12.92 * l : 1.055 * l ** (1 / 2.4) - 0.055;
    return Math.round(Math.max(0, Math.min(1, s)) * 255);
  });
  return `#${channels.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * Where the ramp stops. The LIGHT theme's accent (`#0b4fb8`, M185 Ledger
 * signal blue) is the only mid-dark token in a near-white theme, so mixing it
 * straight toward the surface gives five lavender tints; deepening the top
 * stop 35% toward black buys the ramp headroom it needs to span the frame.
 * The dark theme's accent is already the bright end, so it is used as-is.
 */
const RAMP_T = [0.35, 0.5, 0.65, 0.8, 0.92, 1] as const;
const LIGHT_THEME_DEEPEN = 0.35;

export interface ChoroplethColors {
  /** Six fills, index = `weightBucket(...)`, light → saturated. */
  fills: string[];
  /** Neutral land: no exposure / never mentioned. */
  noData: string;
  /** Hairline between countries — the card's own colour. */
  stroke: string;
}

/** The scale for the current theme, built from the same tokens as the donut. */
export function choroplethColors(palette: ChartPalette, theme: Theme): ChoroplethColors {
  const lightTheme = theme === "light";
  const top = lightTheme ? mixHex(palette.ACCENT, "#000000", LIGHT_THEME_DEEPEN) : palette.ACCENT;
  return {
    fills: RAMP_T.map((t) => mixHex(palette.SURFACE, top, t)),
    noData: palette.SURFACE,
    stroke: palette.SURFACE_RAISED,
  };
}
