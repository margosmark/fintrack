// Channel B of the docs/09 accessibility audit (M58): token contrast, both
// themes, straight from the source of truth — the `:root` / `:root:not(.dark)`
// blocks in `src/index.css`.
//
// This covers exactly what channel A (axe-core over a headless browser) cannot
// see: axe reports `incomplete` for any text sitting on glass/gradient, and it
// cannot read `::placeholder` at all. Here every pair is computed from the raw
// token triples, so a token edit that breaks a documented pair fails in CI with
// the measured ratio in the message.
//
// Thresholds (WCAG 2.1): 4.5:1 for text (1.4.3), 3:1 for non-text boundaries
// and the focus indicator (1.4.11 / 2.4.7). The pair table lives in docs/09
// "Contrast audit"; keep the two in sync.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("src/index.css", "utf8");

const darkStart = css.indexOf("  :root {");
const lightStart = css.indexOf("  :root:not(.dark) {");
const lightEnd = css.indexOf("}", css.indexOf("--ambient-c", lightStart));

/** `--color-name: R G B;` triples out of one token block. */
function parseTokens(block: string): Record<string, [number, number, number]> {
  const tokens: Record<string, [number, number, number]> = {};
  for (const match of block.matchAll(/(--color-[a-z-]+):\s*(\d+) (\d+) (\d+);/g)) {
    tokens[match[1]] = [Number(match[2]), Number(match[3]), Number(match[4])];
  }
  return tokens;
}

const THEMES: Array<[string, Record<string, [number, number, number]>]> = [
  ["dark", parseTokens(css.slice(darkStart, lightStart))],
  ["light", parseTokens(css.slice(lightStart, lightEnd))],
];

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function luminance([r, g, b]: [number, number, number]): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function ratio(a: [number, number, number], b: [number, number, number]): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** Source-over composite of an alpha'd token fill on an opaque backdrop. */
function over(
  fg: [number, number, number],
  alpha: number,
  bg: [number, number, number],
): [number, number, number] {
  return [0, 1, 2].map((i) => Math.round(fg[i] * alpha + bg[i] * (1 - alpha))) as [
    number,
    number,
    number,
  ];
}

interface Pair {
  /** docs/09 contrast table row number. */
  row: number;
  fg: string;
  bg: string;
  min: number;
}

/** The docs/09 "Contrast audit" table, rows 1–12 (opaque token pairs). */
const PAIRS: Pair[] = [
  { row: 1, fg: "text", bg: "surface", min: 4.5 },
  { row: 2, fg: "text", bg: "surface-raised", min: 4.5 },
  { row: 3, fg: "muted", bg: "surface", min: 4.5 },
  { row: 4, fg: "muted", bg: "surface-raised", min: 4.5 },
  { row: 5, fg: "accent", bg: "surface", min: 4.5 },
  { row: 6, fg: "accent", bg: "surface-raised", min: 4.5 },
  { row: 7, fg: "accent-ink", bg: "accent", min: 4.5 },
  { row: 8, fg: "danger", bg: "surface", min: 4.5 },
  { row: 9, fg: "warn", bg: "surface", min: 4.5 },
  { row: 10, fg: "gain", bg: "surface", min: 4.5 },
  { row: 11, fg: "border-control", bg: "surface", min: 3.0 },
  { row: 12, fg: "border-control", bg: "surface-raised", min: 3.0 },
];

describe.each(THEMES)("docs/09 contrast audit — %s theme", (theme, tokens) => {
  const token = (name: string): [number, number, number] => {
    const value = tokens[`--color-${name}`];
    expect(value, `--color-${name} missing from the ${theme} block`).toBeDefined();
    return value as [number, number, number];
  };

  it("parses both token blocks (the audit would be vacuous otherwise)", () => {
    expect(Object.keys(tokens).length).toBeGreaterThanOrEqual(14);
    expect(theme === "dark" || theme === "light").toBe(true);
  });

  it.each(PAIRS)("row $row — $fg on $bg ≥ $min:1", ({ row, fg, bg, min }) => {
    const measured = ratio(token(fg), token(bg));
    expect(
      measured,
      `docs/09 pair ${row} (${theme}): --color-${fg} on --color-${bg} = ${measured.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(min);
  });

  // Row 13 — `hover:border-accent/70` composited over the surface: the hover
  // state of every form control. The border is the component boundary, so the
  // COMPOSITED colour is what must clear 3:1.
  it("row 13 — hover border accent/70 composited on surface ≥ 3:1", () => {
    const composited = over(token("accent"), 0.7, token("surface"));
    const measured = ratio(composited, token("surface"));
    expect(measured, `docs/09 pair 13 (${theme}) = ${measured.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      3.0,
    );
  });

  // Row 14 — the focus indicator itself (2.4.7 visibility needs 3:1 against
  // the adjacent surface in 1.4.11 terms).
  it("row 14 — focus indicator (accent) on surface ≥ 3:1", () => {
    const measured = ratio(token("accent"), token("surface"));
    expect(measured, `docs/09 pair 14 (${theme}) = ${measured.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      3.0,
    );
  });

  // Row 9 also ships as a tinted chip: `bg-warn/10` behind warn text (the
  // warning chip in Bills/Transactions). Text sits on the COMPOSITE, not the
  // raw surface — this is the case axe reports `incomplete` for (glass).
  it("row 9 (chip) — warn text on bg-warn/10 ≥ 4.5:1", () => {
    const chip = over(token("warn"), 0.1, token("surface"));
    const measured = ratio(token("warn"), chip);
    expect(measured, `docs/09 warn chip (${theme}) = ${measured.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      4.5,
    );
  });

  // M185 (docs/47 §5): `--color-rule` is the ONE token the Ledger wave adds —
  // the 1px hairline inside components (state rows, truth bar, table rules).
  // It is decorative (WCAG 1.4.11 exempt, docs/09 §Contrast audit), so it is
  // pinned for EXISTENCE in both themes, never for a ratio: a hairline that
  // clears 3:1 would read as a form-control boundary, which is what
  // `--color-border-control` (rows 11/12) is for.
  it("M185 — --color-rule exists in this theme (decorative hairline)", () => {
    const rule = tokens["--color-rule"];
    expect(rule, `--color-rule missing from the ${theme} block`).toBeDefined();
    expect(rule).toHaveLength(3);
    for (const channelValue of rule as [number, number, number]) {
      expect(Number.isInteger(channelValue)).toBe(true);
      expect(channelValue).toBeGreaterThanOrEqual(0);
      expect(channelValue).toBeLessThanOrEqual(255);
    }
  });
});
