// M167 — the choropleth's 6-step sequential scale (docs/09 §Exposure visuals).
//
// What this pins, and why each check is here:
//   * the bucket thresholds — a legend names them, so a silently moved
//     breakpoint would make the legend lie;
//   * the ramp itself, hex for hex, in BOTH themes — the scale is derived
//     from the donut's own accent token, so this is also the pin that says
//     "same house palette" rather than a stray colour in a component;
//   * the measured contrast floors — the top stop must clear 3:1 against the
//     card (it is the map's loudest graphic), and the lightest stop must stay
//     a PERCEPTIBLE tint of the neutral land, not the same colour as it (the
//     "shaded vs not shaded" line the honesty copy depends on);
//   * palette.SURFACE / SURFACE_RAISED against index.css — the ramp mixes
//     against the design system's own tokens, so a token edit that moves them
//     has to move palette.ts in the same commit.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  WEIGHT_BREAKPOINTS,
  WEIGHT_BUCKET_LABELS,
  choroplethColors,
  weightBucket,
} from "../src/components/charts/choropleth";
import { CHART_PALETTES } from "../src/components/charts/palette";

const css = readFileSync("src/index.css", "utf8");

/** `--color-name: R G B;` triples out of one `:root` token block. */
function parseTokens(block: string): Record<string, [number, number, number]> {
  const tokens: Record<string, [number, number, number]> = {};
  for (const match of block.matchAll(/(--color-[a-z-]+):\s*(\d+) (\d+) (\d+);/g)) {
    tokens[match[1]] = [Number(match[2]), Number(match[3]), Number(match[4])];
  }
  return tokens;
}

const darkStart = css.indexOf("  :root {");
const lightStart = css.indexOf("  :root:not(.dark) {");
const lightEnd = css.indexOf("}", css.indexOf("--ambient-c", lightStart));

const THEMES: Record<"dark" | "light", Record<string, [number, number, number]>> = {
  dark: parseTokens(css.slice(darkStart, lightStart)),
  light: parseTokens(css.slice(lightStart, lightEnd)),
};

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function luminance(rgb: [number, number, number]): number {
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
}

function ratio(a: string, b: string): number {
  const la = luminance(hexToRgb(a));
  const lb = luminance(hexToRgb(b));
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** CIE76 ΔE in Lab (D65) — "can a normal-vision eye tell these apart?". */
function deltaE(a: string, b: string): number {
  const toLab = (hex: string) => {
    const [r, g, bl] = hexToRgb(hex).map((c) => channel(c) * 255) as [
      number,
      number,
      number,
    ];
    const [x, y, z] = [
      0.4124 * r + 0.3576 * g + 0.1805 * bl,
      0.2126 * r + 0.7152 * g + 0.0722 * bl,
      0.0193 * r + 0.1192 * g + 0.9505 * bl,
    ].map((v) => v / 100);
    const f = (v: number) => (v > 0.008856 ? Math.cbrt(v) : 7.787 * v + 16 / 116);
    const fx = f(x / 0.95047);
    const fy = f(y);
    const fz = f(z / 1.08883);
    return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)] as const;
  };
  const [l1, a1, b1] = toLab(a);
  const [l2, a2, b2] = toLab(b);
  return Math.sqrt((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2);
}

describe("M167 — choropleth buckets", () => {
  it("names six buckets and maps a weight into one of them", () => {
    expect(WEIGHT_BUCKET_LABELS).toHaveLength(6);
    expect(WEIGHT_BREAKPOINTS).toHaveLength(5);
    expect(WEIGHT_BUCKET_LABELS[0]).toBe("Under 1%");
    expect(WEIGHT_BUCKET_LABELS[5]).toBe("25% or more");
  });

  it("treats 'no exposure' and 'no data' as the SAME absence — never a zero", () => {
    expect(weightBucket(0)).toBeNull();
    expect(weightBucket(-1)).toBeNull();
    expect(weightBucket(null)).toBeNull();
    expect(weightBucket(undefined)).toBeNull();
    expect(weightBucket(Number.NaN)).toBeNull();
  });

  it("puts each boundary weight in the bucket its legend names", () => {
    expect(weightBucket(0.4)).toBe(0); // Under 1%
    expect(weightBucket(0.9999)).toBe(0);
    expect(weightBucket(1)).toBe(1); // 1–2.5% is inclusive at the bottom
    expect(weightBucket(2.5)).toBe(2); // 2.5–5%
    expect(weightBucket(5)).toBe(3); // 5–10%
    expect(weightBucket(10)).toBe(4); // 10–25%
    expect(weightBucket(25)).toBe(5); // 25% or more
    expect(weightBucket(44.7)).toBe(5); // Mark's United States
  });
});

describe("M167 — the scale, both themes", () => {
  it("is the donut's accent, mixed against the card — pinned hex for hex", () => {
    // M185 Ledger re-skin: the ramp is palette.ACCENT mixed against
    // palette.SURFACE, so a token move re-derives these hexes (recomputed
    // with the same linear-light mix in choropleth.ts).
    expect(choroplethColors(CHART_PALETTES.dark, "dark").fills).toEqual([
      "#4b6aa1",
      "#587cbc",
      "#638bd3",
      "#6d99e7",
      "#75a3f6",
      "#79a9ff",
    ]);
    expect(choroplethColors(CHART_PALETTES.light, "light").fills).toEqual([
      "#cbccd6",
      "#b5b8c9",
      "#9aa0bc",
      "#7782ad",
      "#4e61a0",
      "#074097",
    ]);
    expect(choroplethColors(CHART_PALETTES.dark, "dark").fills).toHaveLength(6);
  });

  it("is monotone in luminance with no two stops the same", () => {
    for (const theme of ["dark", "light"] as const) {
      const { fills, noData } = choroplethColors(CHART_PALETTES[theme], theme);
      const lums = fills.map((fill) => luminance(hexToRgb(fill)));
      // Dark theme brightens with weight; light theme darkens with it.
      const increasing = lums.every((l, i) => i === 0 || l > lums[i - 1]);
      const decreasing = lums.every((l, i) => i === 0 || l < lums[i - 1]);
      expect({ theme, ordered: increasing || decreasing }).toEqual({ theme, ordered: true });
      expect(new Set(fills).size).toBe(6);
      // The lightest stop is not the land itself.
      expect(fills[0]).not.toBe(noData);
    }
  });

  it("clears 3:1 at the loud end and stays a perceptible tint at the quiet end", () => {
    for (const theme of ["dark", "light"] as const) {
      const { fills, noData, stroke } = choroplethColors(CHART_PALETTES[theme], theme);
      // Loudest stop: a meaningful graphic on the card (WCAG 1.4.11).
      expect(ratio(fills[5], stroke)).toBeGreaterThanOrEqual(3);
      // Quietest stop: a TINT of the neutral land, not the same colour — the
      // "has exposure / none listed" line the legend and the honesty copy rest
      // on. Measured floors, not an aspiration (docs/09 §Exposure visuals).
      expect(deltaE(fills[0], noData)).toBeGreaterThanOrEqual(8);
      expect(ratio(fills[0], noData)).toBeGreaterThanOrEqual(1.2);
      // Every stop is distinguishable from the one before it.
      for (let i = 1; i < fills.length; i += 1) {
        expect(deltaE(fills[i], fills[i - 1])).toBeGreaterThan(1.5);
      }
    }
  });

  it("draws neutral land on the card's own surface tokens", () => {
    for (const theme of ["dark", "light"] as const) {
      const colors = choroplethColors(CHART_PALETTES[theme], theme);
      expect(colors.noData).toBe(CHART_PALETTES[theme].SURFACE);
      expect(colors.stroke).toBe(CHART_PALETTES[theme].SURFACE_RAISED);
    }
  });

  it("keeps the JS-side surface mirror in step with index.css", () => {
    const toHex = (rgb: [number, number, number]) =>
      `#${rgb.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
    expect(CHART_PALETTES.dark.SURFACE).toBe(toHex(THEMES.dark["--color-surface"]));
    expect(CHART_PALETTES.dark.SURFACE_RAISED).toBe(
      toHex(THEMES.dark["--color-surface-raised"]),
    );
    expect(CHART_PALETTES.light.SURFACE).toBe(toHex(THEMES.light["--color-surface"]));
    expect(CHART_PALETTES.light.SURFACE_RAISED).toBe(
      toHex(THEMES.light["--color-surface-raised"]),
    );
  });
});
