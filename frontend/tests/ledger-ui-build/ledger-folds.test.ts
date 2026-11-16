// M185 ledger-ui-build — the two documented pure client folds (docs/47 §10
// G2/G5) and the client mirror of the demo write boundary (§4).
//
// These are the wave's only new COMPUTATION and its only new GUARD, so they
// get direct unit coverage: the fold rules are what keep the donut honest
// (uncategorized never folds, `Other (n)` carries the exact remainder, the
// $50 floor keeps the chip set stable), and the guard is what keeps a padlock
// off every live tenant and every read (PM binding W1).

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  CATEGORY_DIFF_FLOOR_CAD,
  CATEGORY_DIFF_MAX,
  DONUT_KEEP,
  diffCategories,
  foldCategoryDonut,
} from "../../src/lib/spendingFold";
import {
  DEMO_MUTATION_EXEMPT_PATHS,
  DEMO_MUTATION_EXEMPT_PREFIXES,
  demoWouldRefuse,
  isDemoExemptPath,
} from "../../src/lib/demoWrite";

const slice = (id: number | null, cad_total: string, name = `cat-${id}`) => ({
  id,
  name,
  cad_total,
});

/**
 * Read the SERVER's exemption lists straight out of `backend/app/api/deps.py`.
 *
 * This pin used to assert a frozen copy of them, so a backend boundary edit
 * drifted silently under a green test (QA finding, 2026-10-16: "the comment
 * promises a backend change breaks loudly here — it won't: the test asserts a
 * frozen literal"). Reading the source is what `src/lib/demoWrite.ts` always
 * promised, so a change to deps.py now breaks THIS file.
 */
function backendExemptions(): { paths: string[]; prefixes: string[] } {
  // cwd-relative, as the rest of this suite reads fixtures: vitest runs with
  // cwd = webapp/, so the backend source is one level up. (`process` is not
  // typed in this tsconfig, so no process.cwd() — and no @types/node needed.)
  const src = readFileSync("../backend/app/api/deps.py", "utf8");

  /** Every double-quoted string inside the literal assigned to `name`. */
  const stringsIn = (name: string): string[] => {
    const decl = src.indexOf(name);
    if (decl < 0) {
      throw new Error(`${name} not found in deps.py — the pin cannot read the boundary`);
    }
    // Handles both `NAME = frozenset({…})` and the annotated
    // `NAME: tuple[str, ...] = (…)`, so a reformatting of the declaration is
    // fine — but a rename or a moved constant fails loudly.
    const open = src.indexOf("(", src.indexOf("=", decl));
    if (open < 0) {
      throw new Error(`${name} has no literal in deps.py — the pin cannot read the boundary`);
    }
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
      const ch = src[i];
      if (ch === "(" || ch === "{") depth += 1;
      else if (ch === ")" || ch === "}") {
        depth -= 1;
        if (depth === 0) {
          return [...src.slice(open, i + 1).matchAll(/"([^"]+)"/g)].map((m) => m[1] as string);
        }
      }
    }
    throw new Error(`${name}'s literal is unterminated in deps.py`);
  };

  const paths = stringsIn("DEMO_MUTATION_EXEMPT_PATHS");
  const prefixes = stringsIn("DEMO_MUTATION_EXEMPT_PREFIXES");
  // An empty extraction would compare nothing and pass — the exact false-green
  // this reader replaces. If deps.py changes shape, fail instead of passing.
  if (paths.length === 0 || prefixes.length === 0) {
    throw new Error(
      `deps.py exemptions came back empty (paths=${JSON.stringify(paths)}, prefixes=${JSON.stringify(prefixes)}) — nothing to compare, so the pin cannot pass`,
    );
  }
  return { paths, prefixes };
}

describe("G5 — foldCategoryDonut", () => {
  it("keeps the top 6 and folds the tail into one exact `Other (n)` line", () => {
    const items = [
      slice(1, "1000.00", "housing"),
      slice(2, "500.00", "dining"),
      slice(3, "400.00", "groceries"),
      slice(4, "300.00", "transport"),
      slice(5, "200.00", "fun"),
      slice(6, "100.00", "health"),
      slice(7, "50.00", "gifts"),
      slice(8, "30.00", "pets"),
      slice(9, "20.00", "books"),
    ];
    const folded = foldCategoryDonut(items);

    expect(folded.slices.map((s) => s.id)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(DONUT_KEEP).toBe(6);
    expect(folded.other?.name).toBe("Other (3)");
    // The remainder is EXACT: 50 + 30 + 20, cents-quantised, never a share.
    expect(folded.other?.total_cad).toBe("100.00");
    expect(folded.other?.items.map((s) => s.id)).toEqual([7, 8, 9]);
  });

  it("never folds Uncategorized — the `category_id: null` slice survives the tail", () => {
    const items = [
      slice(1, "900.00", "housing"),
      slice(2, "800.00", "dining"),
      slice(3, "700.00", "groceries"),
      slice(4, "600.00", "transport"),
      slice(5, "500.00", "fun"),
      slice(6, "400.00", "health"),
      // Smallest slice AND uncategoryy — still not foldable (docs/47 G4/G5:
      // it is the action, not a tail).
      slice(null, "1.00", "Uncategorized"),
      slice(7, "2.00", "gifts"),
    ];
    const folded = foldCategoryDonut(items);

    expect(folded.slices.some((s) => s.id === null)).toBe(true);
    expect(folded.other?.items.some((s) => s.id === null)).toBe(false);
    expect(folded.other?.name).toBe("Other (1)");
    expect(folded.other?.total_cad).toBe("2.00");
  });

  it("returns no `other` when nothing folds, and never mutates the payload", () => {
    const items = [slice(1, "10.00"), slice(null, "5.00", "Uncategorized")];
    const snapshot = JSON.parse(JSON.stringify(items));
    const folded = foldCategoryDonut(items);
    expect(folded.other).toBeNull();
    expect(items).toEqual(snapshot);
  });
});

describe("G2 — diffCategories", () => {
  it("floors at exactly $50 and keeps the three biggest movers", () => {
    const current = [
      slice(1, "300.00", "dining"), // +171.90 vs prior
      slice(2, "66.80", "groceries"), // −133.20
      slice(3, "55.35", "transport"), // +50.35
      slice(4, "100.00", "fun"), // +49.99 → under the floor
      slice(5, "200.00", "pets"), // +0 → no delta, renders nothing
    ];
    const prior = [
      slice(1, "128.10", "dining"),
      slice(2, "200.00", "groceries"),
      slice(3, "5.00", "transport"),
      slice(4, "50.01", "fun"),
      slice(5, "200.00", "pets"),
    ];

    const deltas = diffCategories(current, prior);
    expect(CATEGORY_DIFF_FLOOR_CAD).toBe("50.00");
    expect(CATEGORY_DIFF_MAX).toBe(3);
    expect(deltas.map((d) => d.name)).toEqual(["dining", "groceries", "transport"]);
    // Signed, cents-quantised Decimal strings — money is never a float here.
    expect(deltas.map((d) => d.delta_cad)).toEqual(["171.90", "-133.20", "50.35"]);
  });

  it("diffs a vanished category against zero instead of dropping it", () => {
    const deltas = diffCategories([], [slice(9, "250.00", "gone")]);
    expect(deltas).toHaveLength(1);
    expect(deltas[0]).toMatchObject({ id: 9, name: "gone", delta_cad: "-250.00" });
  });

  it("is pure — same inputs, byte-identical output, inputs untouched", () => {
    const current = [slice(1, "300.00")];
    const prior = [slice(1, "100.00")];
    const first = diffCategories(current, prior);
    const second = diffCategories(current, prior);
    expect(first).toEqual(second);
    expect(current[0].cad_total).toBe("300.00");
  });
});

describe("docs/47 §4 — the demo write mirror", () => {
  it("reads never refuse, on any tenant (PM binding W1: downloads included)", () => {
    for (const method of ["GET", "HEAD", "OPTIONS"]) {
      expect(demoWouldRefuse(method, "/export/zip")).toBe(false);
      expect(demoWouldRefuse(method, "/export/full")).toBe(false);
      expect(demoWouldRefuse(method, "/export/schedule")).toBe(false);
      expect(demoWouldRefuse(method, "/reports/spending?format=csv")).toBe(false);
    }
    // The write half of the export pair IS a write (it may carry DemoLock).
    expect(demoWouldRefuse("PUT", "/export/schedule")).toBe(true);
  });

  it("mirrors the backend's exemption sets — READ from deps.py, not a copy", () => {
    const { paths, prefixes } = backendExemptions();
    // Canonical form the mirror carries: drop the /api/v1 the server paths have.
    const canonical = (p: string) => p.replace(/^\/api\/v1/, "");
    expect(paths.map(canonical).sort()).toEqual([...DEMO_MUTATION_EXEMPT_PATHS].sort());
    expect(prefixes.map(canonical)).toEqual([...DEMO_MUTATION_EXEMPT_PREFIXES]);

    // Behavioural equivalence over the routes that exist, so a representation
    // difference (the trailing slash, the /api/v1 prefix) can never hide a real
    // one: the SERVER's own rule — exact set, then prefix — must agree with the
    // mirror on every exempt route and on every write it refuses.
    const serverExempt = (p: string) =>
      paths.includes(p) || prefixes.some((pre) => p.startsWith(pre));
    for (const probe of [
      ...paths,
      "/api/v1/auth/mcp-tokens/rotate",
      "/api/v1/auth/mcp-tokens/42",
      // The divergence the mirror's old slash-less prefix had: it read exempt
      // here and was refused by deps.py. Both sides must refuse it now.
      "/api/v1/auth/mcp-tokensfoo",
      "/api/v1/transactions",
      "/api/v1/users/me",
    ]) {
      expect(demoWouldRefuse("POST", probe), probe).toBe(!serverExempt(probe));
    }

    expect(demoWouldRefuse("POST", "/projections/run")).toBe(false);
    expect(demoWouldRefuse("POST", "/acb/rebuild")).toBe(false);
    expect(demoWouldRefuse("POST", "/auth/mcp-tokens/rotate")).toBe(false);
    // …and a path outside the sets refuses.
    expect(demoWouldRefuse("POST", "/transactions")).toBe(true);
    expect(demoWouldRefuse("DELETE", "/transactions/42")).toBe(true);
    expect(demoWouldRefuse("PATCH", "/users/me")).toBe(true);
  });

  it("canonicalises the API prefix, query strings and trailing slashes", () => {
    expect(isDemoExemptPath("/api/v1/projections/run")).toBe(true);
    expect(isDemoExemptPath("/api/v1/projections/run/")).toBe(true);
    expect(isDemoExemptPath("https://fintrac.example/api/v1/projections/run?x=1")).toBe(true);
    expect(isDemoExemptPath("/transactions?account=1")).toBe(false);
  });
});
