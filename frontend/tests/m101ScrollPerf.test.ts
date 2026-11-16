// M101 — Android Firefox scroll jank: `backdrop-filter` is the cost, so it is
// off below md (docs/09 "Scroll performance", docs/34 scroll-frame budget).
//
// The A/B that produced this rule is in the M101 card: at 412×915 / DPR 2.6 a
// scripted sweep of /transactions lost 26 of ~400 frames to >50 ms with
// `.glass` blurred, and 0 when the blur alone was removed while every alpha
// background stayed byte-identical. This test pins the four properties that
// make the fix actually hold in the built CSS:
//
//   1. the kill rule exists at all, for both spellings of the property;
//   2. it is UNLAYERED — a rule inside `@layer utilities` would lose to
//      Tailwind's own `backdrop-blur-*` classes and silently do nothing;
//   3. it is scoped to `< 768px` only — desktop keeps the signature glass;
//   4. `.glass` keeps its stacking context (`isolation: isolate`), so removing
//      the filter cannot repaint in-card z-* children into the root context.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const appShell = readFileSync("src/components/layout/AppShell.tsx", "utf8");

/** Comments are stripped before any structural scan: prose in this file names
 *  at-rules verbatim (`@layer …`), and a scanner that trusts it reports the
 *  kill rule as layered when it is not. */
const css = readFileSync("src/index.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** Slice the block that starts at `open` (`{`) through its matching `}`. */
function braceBlock(source: string, open: number): string {
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error("unbalanced braces in index.css");
}

interface Region {
  text: string;
  start: number;
  end: number;
}

/** Every `@media (max-width: 767.98px)` block in the sheet, in source order. */
const mobileBlocks: Region[] = [
  ...css.matchAll(/@media \(max-width: 767\.98px\) \{/g),
].map((m) => {
  const start = m.index + m[0].length - 1;
  const text = braceBlock(css, start);
  return { text, start, end: start + text.length };
});

/** True when `index` sits inside any `@layer … {` block. */
function insideLayer(index: number): boolean {
  for (const m of css.matchAll(/@layer [^{]*\{/g)) {
    const block = braceBlock(css, m.index + m[0].length - 1);
    if (m.index <= index && index < m.index + block.length) return true;
  }
  return false;
}

describe("M101 scroll perf — backdrop-filter off below md", () => {
  it("kills both backdrop-filter spellings inside a mobile media block", () => {
    const killers = mobileBlocks.filter(
      (b) =>
        /backdrop-filter:\s*none\s*!important/.test(b.text) &&
        /-webkit-backdrop-filter:\s*none\s*!important/.test(b.text),
    );
    expect(killers.length).toBeGreaterThan(0);
    // The `*` selector matters: a narrower rule would let the next
    // backdrop-blur-* utility added to the codebase back in.
    expect(
      killers.some((b) => /\*\s*\{[^}]*backdrop-filter:\s*none/.test(b.text)),
    ).toBe(true);
  });

  it("is unlayered — a layered rule would lose to Tailwind's utilities", () => {
    for (const m of css.matchAll(/backdrop-filter:\s*none\s*!important/g)) {
      expect(insideLayer(m.index)).toBe(false);
    }
  });

  it("only disables blur below 768px — desktop glass keeps its frost", () => {
    // Every `backdrop-filter: none` in the sheet lives in a mobile block …
    for (const m of css.matchAll(/backdrop-filter:\s*none/g)) {
      const inMobile = mobileBlocks.some(
        (b) => b.start <= m.index && m.index < b.end,
      );
      expect(inMobile).toBe(true);
    }
    // … and the signature surfaces still declare their blur for wider views.
    expect(css).toMatch(/\.glass\s*\{[^}]*backdrop-filter:\s*blur\(14px\)/);
    expect(css).toMatch(/\.glass-strong\s*\{[^}]*backdrop-filter:\s*blur\(18px\)/);
    // Token backgrounds are untouched: only blur may change (M101 card).
    expect(css).toMatch(/\.glass\s*\{[^}]*background:\s*var\(--glass-bg\)/);
  });

  it("keeps .glass a stacking context without its filter", () => {
    const mobile = mobileBlocks.map((b) => b.text).join("\n");
    expect(mobile).toMatch(/\.glass,[\s\S]*?isolation:\s*isolate/);
  });

  it("the fixed mobile header is opaque now that it cannot blur", () => {
    const header = appShell.match(
      /<header className="[^"]*inset-x-0[^"]*">/,
    )?.[0];
    expect(header).toBeTruthy();
    // md:hidden ⇒ this element only ever renders below md, so it must not
    // rely on an alpha that the removed blur used to hide behind.
    expect(header).toContain("md:hidden");
    expect(header).not.toContain("backdrop-blur");
    expect(header).not.toMatch(/bg-surface-raised\/\d+/);
    expect(header).toContain("bg-surface-raised");
  });
});
