// Query-key shape contract (docs/09 "Query keys").
//
// React Query caches by KEY ONLY: whichever observer fetched first decides the
// payload shape that every other observer of that key receives. Two components
// declaring the same literal key with different queryFns therefore hand each
// other the wrong shape. This shipped as a real defect — Rules shared
// ["categories","all"] (page shape, {items,total,limit}) with Settings'
// fetchAllCategories (a bare Category[]), so a visit to /rules followed by
// /settings gave CategoryManager a non-iterable page object and the route error
// panel replaced the whole page with "TypeError: t is not iterable".
//
// Invariant: within src/, one exact queryKey literal maps to exactly one
// queryFn callee. Keys that are not literals, or hooks without an inline
// queryFn, cannot be resolved statically and are reported, not asserted.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

interface Decl {
  site: string;
  callee: string;
}

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else if (/\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Index of the bracket closing the one at `start` (same char), or -1. */
function closeBracket(text: string, start: string): number {
  const pairs: Record<string, string> = { "{": "}", "(": ")", "[": "]" };
  const open = text[start];
  const close = pairs[open];
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    if (text[i] === open) depth += 1;
    else if (text[i] === close) {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** Value of `field:` at depth 0 inside a query-hook options object. */
function fieldValue(block: string, field: string): string | null {
  const match = new RegExp(`\\b${field}\\s*:`).exec(block);
  if (!match) return null;
  const start = match.index + match[0].length;
  let depth = 0;
  for (let i = start; i < block.length; i += 1) {
    const ch = block[i];
    if ("([{".includes(ch)) depth += 1;
    else if (")]}".includes(ch)) {
      if (depth === 0) return block.slice(start, i).trim();
      depth -= 1;
    } else if (ch === "," && depth === 0) return block.slice(start, i).trim();
  }
  return block.slice(start).trim();
}

/** The function a queryFn expression calls: `() => listTags(200)` -> `listTags`. */
function calleeOf(expr: string): string {
  let e = expr.trim().replace(/^(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*/, "");
  e = e.replace(/^\{\s*return\s+/, "");
  const m = /^([A-Za-z_$][\w$.]*)/.exec(e);
  return m ? m[1] : e.split("(")[0].trim();
}

describe("query keys map to exactly one queryFn", () => {
  const files = walk(join(process.cwd(), "src"));
  const perKey = new Map<string, Decl[]>();
  const unresolved: string[] = [];

  for (const file of files) {
    const source = readFileSync(file, "utf8");
    const rel = file.replace(`${process.cwd()}/`, "");
    const hooks = /use(?:Query|InfiniteQuery|Queries)\s*\(\s*\{/g;
    let hit: RegExpExecArray | null;
    while ((hit = hooks.exec(source)) !== null) {
      const brace = hit.index + hit[0].length - 1;
      const end = closeBracket(source, brace);
      if (end < 0) continue;
      const block = source.slice(brace, end + 1);
      const line = source.slice(0, hit.index).split("\n").length;
      const key = fieldValue(block, "queryKey");
      const fn = fieldValue(block, "queryFn");
      if (key === null || !key.startsWith("[")) continue;
      const norm = key.replace(/\s+/g, "");
      if (fn === null) {
        unresolved.push(`${rel}:${line} ${norm} (no inline queryFn)`);
        continue;
      }
      const decls = perKey.get(norm) ?? [];
      decls.push({ site: `${rel}:${line}`, callee: calleeOf(fn) });
      perKey.set(norm, decls);
    }
  }

  const collisions = [...perKey.entries()]
    .filter(([, decls]) => new Set(decls.map((d) => d.callee)).size > 1)
    .map(
      ([key, decls]) =>
        `${key} <- ${[...new Set(decls.map((d) => d.callee))].join(", ")}: ` +
        decls.map((d) => `${d.site} (${d.callee})`).join(" | "),
    );

  it("scans a meaningful slice of the app", () => {
    expect(perKey.size).toBeGreaterThanOrEqual(40);
  });

  it("has no key shared by two queryFns", () => {
    expect(collisions, `${collisions.length} colliding key(s) — see docs/09 "Query keys"`).toEqual([]);
  });

  it("reports every key it could not resolve", () => {
    // Not a failure: an options object whose queryFn lives elsewhere cannot be
    // checked here. Surfaced so the list stays short and deliberate.
    expect(unresolved).toEqual([]);
  });
});
