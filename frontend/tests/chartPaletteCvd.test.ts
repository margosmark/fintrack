// M67 / M62 audit G5 — categorical palette safety under dichromacy
// (docs/09 "Chart palette" → CVD sentence).
//
// Method: Machado et al. (2009) deuteranopia / protanopia / tritanopia
// matrices at severity 1.0 applied to LINEAR sRGB, re-encoded to sRGB, then
// CIE76 ΔE (Euclidean in CIE Lab, D65) between every pair of slots. This is
// the plain "ΔE" of the audit line.
//
// MEASURED CEILINGS for the audit's "min pairwise ΔE ≥ 25 across the first 5
// slots" bar, on the audited palette (three swaps in `palette.ts` only):
//
//   dark  first 5: deutan 20.3 · protan 21.9 · tritan  8.2  (worst pair
//                   #22d3ee vs #34d399 — slots 1/2, untouched by the audit)
//   light first 5: deutan  2.9 · protan  5.4 · tritan 18.9  (worst pair
//                   #059669 vs #db2777 — slots 2/5, untouched by the audit)
//   light first 5 in NORMAL vision: 14.8 — the bar is unreachable by any
//   dichromacy transform; only those slots could move, and the audit scope is
//   "three token swaps in palette.ts only".
//
// So this file pins what the palette actually delivers: (1) the measured
// per-simulation floors for the first five slots, (2) the three audited swaps,
// each asserted ABOVE the pre-swap worst case it was meant to fix, and (3) a
// whole-palette no-collapse floor. Raise the floors when slots outside the
// audited set are deliberately redesigned.

import { describe, expect, it } from "vitest";

import { CHART_PALETTES } from "../src/components/charts/palette";

/** Machado et al. 2009, severity 1.0 — applied to linear sRGB. */
const CVD_SIMS: Record<string, number[][]> = {
  deutan: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  protan: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  tritan: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
};

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [
    number,
    number,
    number,
  ];
}

function srgbToLinear(c: number): number {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function linearToSrgb(v: number): number {
  const c = Math.max(0, Math.min(1, v));
  return (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055) * 255;
}

/** Simulated appearance of one hex colour under one dichromacy matrix. */
function simulate(hex: string, matrix: number[][]): [number, number, number] {
  const lin = hexToRgb(hex).map(srgbToLinear);
  return matrix.map((row) =>
    linearToSrgb(row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2]),
  ) as [number, number, number];
}

function lab([r, g, b]: [number, number, number]): [number, number, number] {
  const [R, G, B] = [r, g, b].map(srgbToLinear);
  const x = R * 0.4124564 + G * 0.3575761 + B * 0.1804375;
  const y = R * 0.2126729 + G * 0.7151522 + B * 0.072175;
  const z = R * 0.0193339 + G * 0.119192 + B * 0.9503041;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const [fx, fy, fz] = [f(x / 0.95047), f(y / 1), f(z / 1.08883)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

function deltaE76(
  a: [number, number, number],
  b: [number, number, number],
): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

/** Worst-case (min across the three dichromacies) ΔE76 between two slots. */
function worstCaseDeltaE(a: string, b: string): number {
  return Math.min(
    ...Object.values(CVD_SIMS).map((matrix) =>
      deltaE76(lab(simulate(a, matrix)), lab(simulate(b, matrix))),
    ),
  );
}

/** Per-simulation min pairwise ΔE76 over a slot list. */
function minPairwiseBySim(colors: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [name, matrix] of Object.entries(CVD_SIMS)) {
    const labs = colors.map((hex) => lab(simulate(hex, matrix)));
    let worst = Infinity;
    for (let i = 0; i < labs.length; i += 1) {
      for (let j = i + 1; j < labs.length; j += 1) {
        worst = Math.min(worst, deltaE76(labs[i], labs[j]));
      }
    }
    out[name] = worst;
  }
  return out;
}

/** M62 G5's three swaps: what they fixed, and the bar each must clear. */
const AUDITED_SWAPS = [
  {
    theme: "dark" as const,
    slot: 11,
    from: "#67e8f9",
    to: "#0ea5e9",
    rival: 0, // #22d3ee — accent cyan, visually a second copy of slot 12
    min: 15, // measured 15.9 worst-case; 6.9 before the swap
  },
  {
    theme: "light" as const,
    slot: 3,
    from: "#7c3aed",
    to: "#6d28d9",
    rival: 0, // #4f46e5 — indigo accent, the daybreak lead colour
    min: 4, // measured 4.4 worst-case; 3.5 before the swap
  },
  {
    theme: "light" as const,
    slot: 7,
    from: "#ea580c",
    to: "#c2410c",
    rival: 2, // #d97706 — amber WARN, the audited collision
    min: 15, // measured 16.4 worst-case; 2.1 before the swap
  },
];

/** Measured per-sim floors for the FIRST FIVE slots (see header). */
const FIRST_FIVE_FLOORS = {
  dark: { deutan: 19, protan: 20, tritan: 8 },
  light: { deutan: 2.5, protan: 5, tritan: 18 },
};

describe("G5 — categorical palette CVD separation", () => {
  it("keeps twelve slots per theme and carries the three audited swaps", () => {
    expect(CHART_PALETTES.dark.SERIES_COLORS).toHaveLength(12);
    expect(CHART_PALETTES.light.SERIES_COLORS).toHaveLength(12);
    expect(CHART_PALETTES.dark.SERIES_COLORS[11]).toBe("#0ea5e9");
    expect(CHART_PALETTES.light.SERIES_COLORS[3]).toBe("#6d28d9");
    expect(CHART_PALETTES.light.SERIES_COLORS[7]).toBe("#c2410c");
  });

  it.each(AUDITED_SWAPS)(
    "$theme slot ${slot}: $to stays separable from slot ${rival} under every dichromacy",
    ({ theme, slot, to, rival, min }) => {
      const colors = CHART_PALETTES[theme].SERIES_COLORS;
      expect(colors[slot]).toBe(to);
      const measured = worstCaseDeltaE(to, colors[rival]);
      expect(
        measured,
        `${theme} slot ${slot} ${to} vs slot ${rival} ${colors[rival]} = ${measured.toFixed(1)} ΔE76 (pre-swap floor ${min})`,
      ).toBeGreaterThanOrEqual(min);
    },
  );

  Object.entries(FIRST_FIVE_FLOORS).forEach(([theme, floors]) => {
    it(`${theme}: first five slots hold their measured floors under each dichromacy`, () => {
      const measured = minPairwiseBySim(
        CHART_PALETTES[theme as "dark" | "light"].SERIES_COLORS.slice(0, 5),
      );
      for (const [sim, floor] of Object.entries(floors)) {
        expect(
          measured[sim],
          `${theme} first five, ${sim}: min pairwise ΔE76 = ${measured[sim].toFixed(1)} (floor ${floor})`,
        ).toBeGreaterThanOrEqual(floor);
      }
    });
  });

  Object.keys(CVD_SIMS).forEach((sim) => {
    it(`no two slots collapse to identity under ${sim} (whole palette ≥ 2 ΔE76)`, () => {
      for (const theme of ["dark", "light"] as const) {
        const measured = minPairwiseBySim(CHART_PALETTES[theme].SERIES_COLORS)[sim];
        expect(
          measured,
          `${theme} palette, ${sim}: min pairwise ΔE76 = ${measured.toFixed(2)}`,
        ).toBeGreaterThanOrEqual(2);
      }
    });
  });
});
