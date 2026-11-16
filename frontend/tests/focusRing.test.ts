// M66 — focus indicator + banned alpha text (docs/09 contrast pairs 13/14;
// WCAG 2.1 1.4.11 non-text contrast and 2.4.7 focus visibility).
//
// `contrastPairs.test.ts` asserts the TOKEN pairs. This file asserts the class
// strings that paint them, because a token can be perfectly compliant while a
// utility alpha silently drags the painted result under the bar: every custom
// focus ring used to read `focus-visible:ring-accent/40`, which composites to
// 2.58:1 (dark) / 1.87:1 (light). Channel A passed it — axe only checks that
// *an* indicator painted — and channel B passed it because it reads token
// blocks, not class strings. Neither could see this.
//
// So: walk `src/`, pull every focus indicator, hover boundary and text alpha
// out of the class strings, composite it over each backdrop it can sit on, and
// require 3:1 in BOTH themes. An alpha'd ring reintroduced tomorrow fails here
// with the file:line and the measured ratio.
//
// Thresholds and pair numbers come from docs/09 "Contrast audit"; keep in sync.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = "src";
const css = readFileSync("src/index.css", "utf8");

/* ------------------------------------------------------------------ tokens */

const darkStart = css.indexOf("  :root {");
const lightStart = css.indexOf("  :root:not(.dark) {");
const lightEnd = css.indexOf("}", css.indexOf("--ambient-c", lightStart));

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

/** `--glass-bg: rgba(r, g, b, a)` — the card surface both themes float on. */
function glassOver(surface: [number, number, number]): [number, number, number] {
  const m = css.match(/--glass-bg:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)/);
  expect(m, "--glass-bg missing from index.css").toBeTruthy();
  const [r, g, b, a] = m!.slice(1).map(Number) as [number, number, number, number];
  return [0, 1, 2].map((i) => Math.round([r, g, b][i] * a + surface[i] * (1 - a))) as [
    number,
    number,
    number,
  ];
}

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
function luminance([r, g, b]: [number, number, number]): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}
function ratio(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = luminance(a) > luminance(b) ? [luminance(a), luminance(b)] : [luminance(b), luminance(a)];
  return (hi + 0.05) / (lo + 0.05);
}
/** Source-over composite of an alpha'd token on an opaque backdrop. */
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

/* --------------------------------------------------------------- src walk */

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.tsx?$/.test(entry)) out.push(path);
  }
  return out;
}

const FILES = sourceFiles(SRC);

interface Site {
  file: string;
  line: number;
  /** The verbatim class token, e.g. `focus-visible:ring-accent/40`. */
  token: string;
  color: string;
  alpha: number;
}

/** Every `…:ring-<token>(/<alpha>)` focus indicator in the tree. */
const FOCUS_SITES: Site[] = [];
for (const file of FILES) {
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((text, i) => {
      for (const m of text.matchAll(/focus-visible:ring-([a-z-]+)(?:\/(\d+))?/g)) {
        // `focus-visible:ring-inset` is a placement modifier (Reports tabs),
        // not a colour — the colour arrives as its own `ring-<token>` class.
        if (m[1] === "inset") continue;
        FOCUS_SITES.push({
          file,
          line: i + 1,
          token: m[0],
          color: m[1],
          alpha: m[2] ? Number(m[2]) / 100 : 1,
        });
      }
    });
}

/** Every `hover:border-<token>/<alpha>` — pair 13's control hover boundary. */
const HOVER_SITES: Site[] = [];
for (const file of FILES) {
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((text, i) => {
      for (const m of text.matchAll(/hover:border-([a-z-]+)(?:\/(\d+))?/g)) {
        HOVER_SITES.push({
          file,
          line: i + 1,
          token: m[0],
          color: m[1],
          alpha: m[2] ? Number(m[2]) / 100 : 1,
        });
      }
    });
}

/**
 * Sites known to still paint below 3:1, each pinned to the card that owns the
 * file. A site leaves this map only when its class changes — at which point the
 * "every exception is still live" assertion below fails and the entry must be
 * deleted. New sub-3:1 sites never enter it silently: they fail the sweep.
 */
const PENDING = new Map<string, string>([]);

/**
 * Every `field-ring` control must pair with the pair-11 boundary token. The
 * decorative `--color-border` measures 1.46:1 (dark) / 1.20:1 (light) on
 * surface, `--color-border-control` measures 3.49:1 / 3.28:1 — docs/09: "Form
 * controls use the separate --color-border-control token (pairs 11/12)
 * precisely because there the border IS the component boundary."
 */
const FIELD_SITES: Site[] = [];
for (const file of FILES) {
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((text, i) => {
      if (text.includes("field-ring")) {
        FIELD_SITES.push({ file, line: i + 1, token: text.trim(), color: "", alpha: 1 });
      }
    });
}

/**
 * `outline-none` in the utilities layer beats the `*:focus-visible` baseline in
 * @layer base (proved at runtime: the outline computes to `rgba(0,0,0,0)` and
 * boxShadow is `none`), so a control that drops the outline without painting a
 * replacement focus indicator paints NOTHING — docs/09's keyboard contract
 * says "Components must not remove an indicator without replacing it".
 */
const OUTLINE_ONLY: Site[] = [];
for (const file of FILES) {
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((text, i) => {
      if (/outline-none/.test(text) && !/focus-visible:ring|field-ring/.test(text)) {
        OUTLINE_ONLY.push({ file, line: i + 1, token: text.trim(), color: "", alpha: 1 });
      }
    });
}

/**
 * Modal surfaces that legitimately carry no indicator: `useDialogFocus` moves
 * focus onto them programmatically (they are `tabIndex=-1`, never a Tab stop)
 * and the dialog itself is already the visible feedback. Keyed by file plus a
 * stable class fragment so a NEW indicator-less line in the same file still
 * fails.
 */
const NO_INDICATOR_EXEMPT = new Map<string, string>([
  ["src/pages/Transactions.tsx::bg-scrim", "modal scrim wrapper — not a Tab stop"],
  [
    "src/components/transactions/SimilarExamples.tsx::shadow-glow-sm",
    "dialog surface (role=dialog, tabIndex=-1) — useDialogFocus target",
  ],
  [
    "src/components/settings/AccountsSection.tsx::max-w-md",
    "dialog surface (role=dialog, tabIndex=-1) — useDialogFocus target",
  ],
  [
    "src/components/transactions/QuickTagPopover.tsx::z-40",
    "popover surface (tabIndex=-1) — useDialogFocus target, focus lands in the picker input (M104)",
  ],
  [
    "src/components/transactions/TagManagerModal.tsx::max-w-2xl",
    "dialog surface (role=dialog, tabIndex=-1) — useDialogFocus target (M104)",
  ],
  [
    "src/components/transactions/HistoryDrawer.tsx::max-w-md",
    "dialog surface (role=dialog, tabIndex=-1) — useDialogFocus target (M106)",
  ],
]);

const pos = (site: Site): string => `${site.file}:${site.line}`;

/* ------------------------------------------------------------------- tests */

describe("M66 focus indicator (docs/09 pair 14 / WCAG 1.4.11)", () => {
  it("scans the whole tree — the sweep below is vacuous otherwise", () => {
    expect(FILES.length).toBeGreaterThan(100);
    // The M66 baseline: 38 accent sites + the danger sites swept with them.
    expect(FOCUS_SITES.length).toBeGreaterThanOrEqual(38);
    expect(HOVER_SITES.length).toBeGreaterThanOrEqual(20);
  });

  it("no `ring-accent/40` survives anywhere in src", () => {
    const leaked = FOCUS_SITES.filter((s) => s.token.includes("ring-accent/40")).map(pos);
    expect(leaked, `ring-accent/40 still present: ${leaked.join(", ")}`).toEqual([]);
  });

  it("every documented exception is still a real site (no stale entries)", () => {
    const live = new Set(FOCUS_SITES.map((s) => `${s.file}::${s.token}`));
    for (const key of PENDING.keys()) {
      expect(live.has(key), `stale exception ${key} — the site is fixed, delete the entry`).toBe(
        true,
      );
    }
  });

  it("`outline-none` is never a dead end — remove an indicator, replace it", () => {
    const used = new Set<string>();
    const violations: string[] = [];
    for (const site of OUTLINE_ONLY) {
      const hit = [...NO_INDICATOR_EXEMPT.keys()].find(
        (key) => site.file === key.split("::")[0] && site.token.includes(key.split("::")[1]),
      );
      if (hit) {
        used.add(hit);
        continue;
      }
      violations.push(
        `${pos(site)} removes the global outline and paints no replacement: ${site.token.slice(0, 140)}`,
      );
    }
    expect(violations, `docs/09 keyboard contract:\n${violations.join("\n")}`).toEqual([]);
    for (const key of NO_INDICATOR_EXEMPT.keys()) {
      expect(used.has(key), `stale exemption ${key} — no longer matches a site, delete it`).toBe(
        true,
      );
    }
  });

  it("every `field-ring` control carries the pair-11 `border-control` token", () => {
    expect(FIELD_SITES.length).toBeGreaterThanOrEqual(30);
    const bad = FIELD_SITES.filter((s) => !s.token.includes("border-control")).map(pos);
    expect(
      bad,
      `field-ring without border-control (docs/09 pair 11 needs 3:1):\n${bad.join("\n")}`,
    ).toEqual([]);
  });

  it.each(THEMES)("%s theme — every focus ring composite ≥ 3:1", (theme, tokens) => {
    const surface = tokens["--color-surface"] as [number, number, number];
    const backdrops: Array<[string, [number, number, number]]> = [
      ["surface", surface],
      ["surface-raised", tokens["--color-surface-raised"] as [number, number, number]],
      ["glass", glassOver(surface)],
    ];
    const failures: string[] = [];
    for (const site of FOCUS_SITES) {
      if (PENDING.has(`${site.file}::${site.token}`)) continue;
      const color = tokens[`--color-${site.color}`];
      expect(color, `--color-${site.color} missing from the ${theme} block`).toBeDefined();
      for (const [bgName, bg] of backdrops) {
        const measured = ratio(over(color as [number, number, number], site.alpha, bg), bg);
        if (measured < 3.0) {
          failures.push(
            `${pos(site)} \`${site.token}\` vs ${bgName} = ${measured.toFixed(2)}:1 (needs 3.0)`,
          );
        }
      }
    }
    expect(failures, `${theme}:\n${failures.join("\n")}`).toEqual([]);
  });

  it.each(THEMES)("%s theme — every hover control boundary composite ≥ 3:1", (theme, tokens) => {
    const surface = tokens["--color-surface"] as [number, number, number];
    const backdrops: Array<[string, [number, number, number]]> = [
      ["surface", surface],
      ["surface-raised", tokens["--color-surface-raised"] as [number, number, number]],
      ["glass", glassOver(surface)],
    ];
    const failures: string[] = [];
    for (const site of HOVER_SITES) {
      const color = tokens[`--color-${site.color}`] as [number, number, number];
      for (const [bgName, bg] of backdrops) {
        const measured = ratio(over(color, site.alpha, bg), bg);
        if (measured < 3.0) {
          failures.push(
            `${pos(site)} \`${site.token}\` vs ${bgName} = ${measured.toFixed(2)}:1 (needs 3.0)`,
          );
        }
      }
    }
    expect(failures, `${theme}:\n${failures.join("\n")}`).toEqual([]);
  });

  // `.field-ring` opts out of the global outline for its own border + halo:
  // the BORDER is the indicator (docs/09 line ~183), the 3px halo is decoration
  // and deliberately sits below 3:1, so only the border is held to the bar.
  it.each(THEMES)("%s theme — .field-ring focus border ≥ 3:1", (theme, tokens) => {
    const rule = css.slice(css.indexOf(".field-ring:focus-visible"));
    const block = rule.slice(0, rule.indexOf("}"));
    const m = block.match(/border-color:\s*rgb\(var\(--color-accent\)\s*\/\s*([\d.]+)\)/);
    expect(m, ".field-ring:focus-visible border-color rule not found").toBeTruthy();
    const alpha = Number(m![1]);
    const surface = tokens["--color-surface"] as [number, number, number];
    const accent = tokens["--color-accent"] as [number, number, number];
    const measured = ratio(over(accent, alpha, surface), surface);
    expect(
      measured,
      `docs/09 field-ring focus border (${theme}) = ${measured.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(3.0);
  });
});

describe("M66 banned alpha text (docs/09: alpha-composited text is banned)", () => {
  const TEXT_ALPHA: RegExp = /(?:^|[\s"'`])(?:placeholder:)?text-[a-z-]+\/(?:\d+|\[[^\]]+\])/;

  it("no alpha-modified text token survives in src", () => {
    const hits: string[] = [];
    for (const file of FILES) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((text, i) => {
          if (TEXT_ALPHA.test(text)) hits.push(`${file}:${i + 1} — ${text.trim().slice(0, 120)}`);
        });
    }
    expect(hits, `alpha text is banned by docs/09:\n${hits.join("\n")}`).toEqual([]);
  });

  it.each(THEMES)("%s theme — full-strength muted is AA as placeholder text", (theme, tokens) => {
    const measured = ratio(
      tokens["--color-muted"] as [number, number, number],
      tokens["--color-surface"] as [number, number, number],
    );
    expect(measured, `muted on surface (${theme}) = ${measured.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      4.5,
    );
  });
});
