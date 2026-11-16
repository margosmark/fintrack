// Categorical palette for multi-series charts, dual-mode ("midnight
// terminal" dark / "daybreak" light). Consume through useChartPalette() so
// every Recharts usage re-colors instantly on theme toggle. Tooltip styling
// lives in the shared GlassTooltip (components/ui/charts/ChartTooltip.tsx).

import { useTheme } from "../../theme/ThemeProvider";
import type { Theme } from "../../theme/ThemeProvider";

export interface ChartPalette {
  ACCENT: string;
  ACCENT_SOFT: string;
  /** Semantic green — gains/income only. */
  GAIN: string;
  /** Semantic red — losses/expenses only. */
  LOSS: string;
  /** Amber — momentum line / warnings. */
  WARN: string;
  SERIES_COLORS: string[];
  AXIS_TICK: { fill: string; fontSize: number };
  GRID_STROKE: string;
  /** Hover band behind bars/columns. */
  CURSOR_FILL: string;
  /** Hover tracking line for area/line charts. */
  CURSOR_STROKE: string;
  LEGEND_COLOR: string;
  /** Top stop alpha for area/bar gradient fills (fades to transparent). */
  GRAD_TOP: number;
  /**
   * The canvas a bare-SVG chart draws on (M167, docs/09 §Exposure visuals):
   * the page's `--color-surface` and the card's `--color-surface-raised`,
   * mirrored into JS because a choropleth has to MIX colours against them (a
   * CSS variable cannot be interpolated in JS). tests/m167Choropleth.test.ts
   * pins both to the `:root` token blocks in index.css, so a token edit fails
   * that test here instead of drifting away from the design system.
   */
  SURFACE: string;
  SURFACE_RAISED: string;
}

/** Dark series are bright against near-black surfaces. The token-mirroring
   constants (ACCENT/ACCENT_SOFT/GAIN/LOSS/WARN/AXIS/GRID/SURFACE…) track
   `src/index.css` exactly — m167Choropleth pins SURFACE/SURFACE_RAISED to the
   token blocks, so a token edit lands here too (M185 Ledger re-skin). The 12
   categorical SERIES_COLORS slots are the M62/M67-audited set and are NOT
   re-skinned: tests/chartPaletteCvd.test.ts holds their measured CVD floors. */
const DARK_PALETTE: ChartPalette = {
  ACCENT: "#79a9ff",
  ACCENT_SOFT: "#a5c4ff",
  GAIN: "#3fcf97",
  LOSS: "#ff8078",
  WARN: "#efb53a",
  SERIES_COLORS: [
    "#22d3ee",
    "#34d399",
    "#fbbf24",
    "#c084fc",
    "#f472b6",
    "#60a5fa",
    "#2dd4bf",
    "#fb923c",
    "#a3e635",
    "#94a3b8",
    "#f87171",
    "#0ea5e9", // slot 12 (M67/G5): sky-500 — `#67e8f9` read as a second accent cyan
  ],
  AXIS_TICK: { fill: "#a2a8b3", fontSize: 11 },
  GRID_STROKE: "rgba(162, 168, 179, 0.13)",
  CURSOR_FILL: "rgba(162, 168, 179, 0.07)",
  CURSOR_STROKE: "rgba(162, 168, 179, 0.25)",
  LEGEND_COLOR: "#a2a8b3",
  GRAD_TOP: 0.42,
  SURFACE: "#101216", // --color-surface (index.css :root)
  SURFACE_RAISED: "#171a21", // --color-surface-raised (index.css :root)
};

/** Light series darken ≥1 step for ≥3:1 vs white; the Ledger signal blue
   leads to carry the Daylight identity (docs/47 §5). */
const LIGHT_PALETTE: ChartPalette = {
  ACCENT: "#0b4fb8", // Ledger signal blue
  ACCENT_SOFT: "#093f95",
  GAIN: "#0a6b45",
  LOSS: "#b3261e",
  WARN: "#7a4e00",
  SERIES_COLORS: [
    "#4f46e5",
    "#059669",
    "#d97706",
    "#6d28d9", // slot 4 (M67/G5): violet-700 — `#7c3aed` collapses into indigo under protan
    "#db2777",
    "#2563eb",
    "#0d9488",
    "#c2410c", // slot 8 (M67/G5): orange-700 — `#ea580c` collides with amber `#d97706` under deutan
    "#65a30d",
    "#64748b",
    "#dc2626",
    "#0891b2",
  ],
  AXIS_TICK: { fill: "#565d68", fontSize: 11 },
  GRID_STROKE: "#e2ddd3",
  CURSOR_FILL: "rgba(86, 93, 104, 0.1)",
  CURSOR_STROKE: "rgba(86, 93, 104, 0.35)",
  LEGEND_COLOR: "#565d68",
  GRAD_TOP: 0.2,
  SURFACE: "#f6f4ef", // --color-surface (index.css :root:not(.dark))
  SURFACE_RAISED: "#ffffff", // --color-surface-raised (index.css :root:not(.dark))
};

/** Both palettes, exported for the docs/09 CVD tests (charts still consume
   them through `useChartPalette()` so a theme flip re-renders every usage). */
export const CHART_PALETTES: Record<Theme, ChartPalette> = {
  dark: DARK_PALETTE,
  light: LIGHT_PALETTE,
};

/** Mode-aware chart constants — re-renders consumers when the theme flips. */
export function useChartPalette(): ChartPalette {
  const { theme } = useTheme();
  return CHART_PALETTES[theme];
}
