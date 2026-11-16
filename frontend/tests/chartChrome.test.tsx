// M67 (M62 audit G3 + G7) — chart chrome behaviour, both themes.
//
// G3: Recharts' DefaultLegendContent paints every legend label in the SERIES
// colour (`finalLabelStyle.color || entry.color`), which fails 4.5:1 on light —
// the audited "housing" label `#d97706` measures 2.92:1 on the card. The
// shared chrome therefore wraps each label in a span carrying LEGEND_COLOR, so
// all 13 chart consumers inherit readable legend text.
// G7: money-axis ticks were raw (`$165000` / `$-55000`); every other money
// surface is compact, so the shared formatter now reads `$165K` / `-$55K`.
//
// These are behaviour assertions: the colour is read back out of a rendered
// <Legend> and the ticks out of the hook the charts actually spread — not from
// the config object alone.

import { readFileSync } from "node:fs";
import React from "react";
import { render, renderHook } from "@testing-library/react";
import { Bar, BarChart, Legend } from "recharts";
import { beforeEach, describe, expect, it } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import type { Theme } from "../src/theme/ThemeProvider";
import {
  formatAxisMoney,
  useChartChrome,
} from "../src/components/charts/chartDefaults";
import { CHART_PALETTES } from "../src/components/charts/palette";

// jsdom has no matchMedia; ThemeProvider and useReducedMotion both call it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

const THEMES: Theme[] = ["dark", "light"];

function wrapper({ children }: { children: React.ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

function rgbOf(hex: string): string {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return `rgb(${r}, ${g}, ${b})`;
}

/** Renders a chart whose legend carries the shared chrome, per theme. */
function renderLegendFor(theme: Theme) {
  localStorage.setItem("theme", theme);
  const { result } = renderHook(() => useChartChrome(), { wrapper });
  const { container } = render(
    <BarChart width={400} height={300} data={[{ label: "Housing", value: 42 }]}>
      <Bar dataKey="value" name="Housing" fill="#d97706" />
      <Legend {...result.current.legend} />
    </BarChart>,
  );
  return { container, chrome: result.current, palette: CHART_PALETTES[theme] };
}

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The docs/09 token blocks, as `contrastPairs.test.ts` reads them. */
const css = readFileSync("src/index.css", "utf8");
function token(theme: Theme, name: string): string {
  const start = theme === "dark" ? css.indexOf("  :root {") : css.indexOf("  :root:not(.dark) {");
  const end =
    theme === "dark" ? css.indexOf("  :root:not(.dark) {") : css.indexOf("}", css.indexOf("--ambient-c", start));
  const block = css.slice(start, end);
  const match = block.match(new RegExp(`--color-${name}:\\s*(\\d+) (\\d+) (\\d+);`));
  if (!match) throw new Error(`--color-${name} missing from the ${theme} block`);
  const hex = match
    .slice(1, 4)
    .map((c) => Number(c).toString(16).padStart(2, "0"))
    .join("");
  return `#${hex}`;
}

beforeEach(() => {
  localStorage.clear();
});

describe("G3 — legend labels render in LEGEND_COLOR, not the series colour", () => {
  it.each(THEMES)("%s theme: the label text carries LEGEND_COLOR inline", (theme) => {
    const { container, palette } = renderLegendFor(theme);

    const label = container.querySelector<HTMLElement>(".recharts-legend-item-text");
    expect(label, "legend item text missing").not.toBeNull();
    expect(label?.textContent).toBe("Housing");

    // The series colour Recharts puts on the wrapper span is the audited
    // failure; the formatter's span overrides it for the text itself.
    expect(label?.style.color).toBe(rgbOf("#d97706"));
    const inner = label?.querySelector<HTMLElement>("span");
    expect(inner, "legendFormatter span missing").not.toBeNull();
    expect(inner?.textContent).toBe("Housing");
    expect(inner?.style.color).toBe(rgbOf(palette.LEGEND_COLOR));
  });

  it.each(THEMES)("%s theme: LEGEND_COLOR clears 4.5:1 on both card surfaces", (theme) => {
    const { palette } = renderLegendFor(theme);
    const surface = token(theme, "surface");
    const raised = token(theme, "surface-raised");
    expect(
      contrast(palette.LEGEND_COLOR, surface),
      `LEGEND_COLOR ${palette.LEGEND_COLOR} on --color-surface (${theme})`,
    ).toBeGreaterThanOrEqual(4.5);
    expect(
      contrast(palette.LEGEND_COLOR, raised),
      `LEGEND_COLOR ${palette.LEGEND_COLOR} on --color-surface-raised (${theme})`,
    ).toBeGreaterThanOrEqual(4.5);
  });

  it("light: the series colour the legend used to inherit fails AA (why the override exists)", () => {
    // M62 G3's cited figure — "housing" is a light-theme series label. The
    // exact ratio tracks --color-surface, which the M185 Ledger re-skin moved
    // to warm paper #f6f4ef (2.90; 2.92 on the old cool #f3f5f8). The verdict
    // the override exists for is unchanged: sub-4.5 either way.
    expect(contrast("#d97706", token("light", "surface"))).toBeCloseTo(2.9, 2);
    expect(contrast("#d97706", token("light", "surface"))).toBeLessThan(4.5);
  });
});

describe("G7 — money axis is compact and signed", () => {
  it.each(THEMES)("useChartChrome().moneyAxis formats ticks compactly (%s)", (theme) => {
    localStorage.setItem("theme", theme);
    const { result } = renderHook(() => useChartChrome(), { wrapper });
    const money = result.current.moneyAxis;

    expect(money(165000)).toBe("$165K");
    expect(money(-55000)).toBe("-$55K");
    expect(money(1234567)).toBe("$1.2M");
    expect(money(-1234567)).toBe("-$1.2M");
    // Below the compact threshold the tick stays exact, without forced cents.
    expect(money(5000)).toBe("$5,000");
    expect(money(-55)).toBe("-$55");
    expect(money(12.5)).toBe("$12.5");
    expect(money(0)).toBe("$0");
    expect(money(-0)).toBe("$0");
  });

  it("is the exported formatter (one money-axis implementation)", () => {
    expect(formatAxisMoney(999999)).toBe("$1M");
    expect(formatAxisMoney(999)).toBe("$999");
  });
});
