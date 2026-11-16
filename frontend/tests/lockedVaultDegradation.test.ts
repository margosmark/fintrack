// @vitest-environment node
// Hardening audit rows A1–A15 — docs/33 Phase 4 § "Hardening audit — S1…S5
// locked-vault degradation".
//
// Two halves:
//   (1) LOCKED BEHAVIOUR — the routes that moved from server to client must
//       treat a locked vault as "no plaintext", never as "show what you have".
//   (2) LOCK-INVARIANCE — the client computes that REMAIN must be provably
//       vault-independent: computed twice (locked, then unlocked) and asserted
//       byte-identical, plus (where a golden exists) that the output still
//       equals the captured live-server JSON — so the comparison can never
//       pass on empty/degenerate input.
//
// M160 (P2-5, docs/33 §Server-first rule 5) retired the report-engine mirrors
// with `lib/reportEngine.ts` / `lib/s3Engine.ts` / `lib/totalReturnEngine.ts`,
// so the per-engine locked/unlocked golden-diff halves for spending, cashflow,
// net-worth, performance, capital-gains, ACB, rule-preview and subscriptions
// retired WITH their engines (M160-R5): a server replay cannot reproduce a
// client lock state, and their guarantee is absorbed by the server routes,
// whose tenancy/consent scoping replaces lock behavior server-side. What
// survives here is every client-only compute that remains after the
// amendment:
//
//   - A13 `budgetsProgress` (lib/budgets.ts — the audited client-only Row-8
//         class: the partner view's progress math has NO server twin by D3
//         design, docs/43 §9, so its lock behavior stays client-tested),
//   - A1–A3 the blind-index / searchMatch locked behaviour (unchanged).
//
// A9 (`dividendReport`) retired here with M160-R2 (t_85311c34): its panel is
// re-pointed at `GET /reports/dividends`, the shim that carried the compute
// is deleted, and the guarantee moved to the server route — lock/tenancy
// scoping is the server's job now (the client compute could only ever read
// the mandatory plaintext columns, audit finding F6).
//
// The invariance half is a tripwire as much as a proof: docs/08 used to say
// the reports compute "over resolved rows" and they never did — they read the
// mandatory plaintext columns (audit finding F6, FIXED in M156). If a future
// change makes a surviving loader vault-aware, this file catches it.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Budget } from "../src/api/budgets";
import type { Category } from "../src/api/categories";
import type { Transaction } from "../src/api/transactions";
import { blindIndexTokenForQuery } from "../src/lib/e2ee";
import { budgetsProgress } from "../src/lib/budgets";
import { matchRows, searchPage, searchPayee } from "../src/lib/searchMatch";
import { createVault, isUnlocked, lockVault } from "../src/lib/vault";

// ── fixtures ─────────────────────────────────────────────────────────────────

// cwd is the frontend root under vitest (config root).
function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(`tests/fixtures/${name}-vectors.json`, "utf8")) as T;
}

const s3 = fixture<{
  meta: { captured_at_et: string; reporting_currency: string };
  datasets: Record<
    string,
    {
      transactions: Transaction[];
      categories: Category[];
      budgets?: Budget[];
      fx?: Record<string, Array<{ date: string; rate: string }>> | null;
    }
  >;
  cases: Array<{
    id: string;
    dataset: string;
    request: { method: string; path: string; body?: unknown };
    expected: { status: number; body: unknown };
  }>;
}>("s3");

const s3Demo = s3.datasets.demo!;
const s3Rates = new Map<string, Array<{ date: string; rate: string }>>(
  Object.entries(s3Demo.fx ?? {}),
);
const budgetsCase = s3.cases.find((c) => c.id === "budgets.progress")!;

// ── every surviving client-only compute, called the same way in both lock
// states ─────────────────────────────────────────────────────────────────────

function computeAll(): Record<string, unknown> {
  return {
    // S3 client-only class (lib/budgets.ts — the Row-8 disposition)
    budgets: budgetsProgress({
      budgets: s3Demo.budgets ?? [],
      categories: s3Demo.categories,
      transactions: s3Demo.transactions,
      rates: s3Rates,
      today: s3.meta.captured_at_et,
      reporting: s3.meta.reporting_currency,
    }),
  };
}

// ── (1) locked behaviour ─────────────────────────────────────────────────────

const cipherRow = {
  id: 1,
  payee: "🔒",
  category_id: 1,
  payee_cipher: "v1:opaque-ciphertext",
  // The server still sends plaintext `normalized_payee` for mixed-mode rows
  // (docs/33 open tradeoffs) — the client must simply never SHOW it for a
  // cipher row while locked.
  normalized_payee: "GROCERY STORE",
} as const;

const plainRow = {
  id: 2,
  payee: "Coffee Shop",
  category_id: 1,
  payee_cipher: null,
  normalized_payee: "Coffee Shop",
} as const;

describe("locked-vault degradation — behaviour (audit rows A1–A3)", () => {
  it("A1: no blind-index token while locked", async () => {
    lockVault();
    expect(isUnlocked()).toBe(false);
    // The server must receive no payee_token at all, so it answers
    // `transactions: []` instead of silently returning fewer hits.
    expect(await blindIndexTokenForQuery("grocery store")).toBeUndefined();
  });

  it("A1: cipher rows never match and never render plaintext", () => {
    lockVault();
    // searchPayee(undefined, …) is the locked path: no resolution available.
    expect(searchPayee(undefined, cipherRow)).toBe("");
    expect(searchPayee(undefined, plainRow)).toBe("Coffee Shop");

    const resolved = new Map<number, string>([[cipherRow.id, "🔒"]]);
    const hits = matchRows(
      [{ ...cipherRow, payee: resolved.get(cipherRow.id)! }],
      "grocery store",
      { limit: 10 },
    );
    expect(hits.map((h) => h.row.id)).not.toContain(cipherRow.id);
    // The plaintext the server still ships for mixed-mode rows never surfaces.
    expect(hits.map((h) => h.row.payee)).not.toContain("GROCERY STORE");
  });

  it("A2: locked cipher rows never qualify as Similar examples", () => {
    lockVault();
    const rows = [
      { ...cipherRow, payee: "🔒" },
      { ...plainRow, payee: "Coffee Shop" },
    ];
    // The Similar population is `matchRows(…, { categorizedOnly: true })` —
    // the client replacement for the removed GET /transactions/similar (405).
    const hits = matchRows(rows, "coffee shop", { categorizedOnly: true });
    expect(hits.map((h) => h.row.id)).toEqual([plainRow.id]);
    // The cipher row's plaintext is not present anywhere in the result.
    expect(hits.map((h) => h.row.payee)).not.toContain("GROCERY STORE");
  });

  it("A3: the search page excludes cipher rows and shows no plaintext", () => {
    lockVault();
    const rows = [
      { ...cipherRow, payee: "🔒" },
      { ...plainRow, payee: "Coffee Shop" },
    ];
    const page = searchPage(rows, "coffee", { limit: 50, offset: 0 });
    expect(page.items.map((r) => r.id)).toEqual([plainRow.id]);
    expect(page.total).toBe(1);
    expect(page.items.map((r) => r.payee)).not.toContain("GROCERY STORE");

    // Querying the cipher row's OWN plaintext returns an empty envelope: the
    // row's payee is the lock marker while locked, so it cannot match.
    const noHits = searchPage(rows, "grocery store", { limit: 50, offset: 0 });
    expect(noHits.items).toEqual([]);
    expect(noHits.total).toBe(0);
    expect(JSON.stringify(noHits)).not.toContain("GROCERY STORE");
  });
});

// ── (2) lock invariance of the surviving client-only computes ────────────────

describe("locked-vault degradation — client-only compute invariance (audit row A13)", () => {
  let LOCKED: Record<string, unknown>;
  let UNLOCKED: Record<string, unknown>;

  beforeAll(async () => {
    lockVault();
    LOCKED = computeAll();
    expect(isUnlocked()).toBe(false);
    await createVault("locked-degradation-audit");
    expect(isUnlocked()).toBe(true);
    UNLOCKED = computeAll();
  });

  afterAll(() => lockVault());

  function same(key: string): unknown {
    expect(UNLOCKED[key]).toEqual(LOCKED[key]);
    return UNLOCKED[key];
  }

  it("A13: budget definitions/progress are lock-invariant and match the golden", () => {
    expect(same("budgets")).toEqual(budgetsCase.expected.body);
    expect(budgetsCase.expected.status).toBe(200);
  });
});


// ── rule 8 standing gate (M156, docs/33 §"Rule-8 inventory") ─────────────
//
// docs/33 §Server-first computing rule 8: "every NEW client compute ships
// with its docs/33 inventory row + disposition … A client compute without a
// disposition is a failed gate." Until M156 that rule was prose only
// (`grep -rn disposition frontend/tests/` returned 0 hits), so the audit
// table could rot the moment a new engine landed — M135 §3.4-2's tripwire,
// qabot's G6.
//
// This block reads the machine-readable table back and fails when the tree
// and the doc disagree in EITHER direction: a compute with no row, or a row
// naming something the tree no longer has. The five enumeration rules are
// stated in docs/33 itself, so a reviewer checks them against this code line
// by line rather than against a second opinion.
//
// Proof the gate can actually go red: M156 landed a synthetic
// `src/lib/syntheticGateProbeEngine.ts` with no inventory row, recorded the
// failure output on the kanban card, and removed the file again. Rule 3
// keeps its own red-proof permanently in-tree: the boundary test below
// drives the clause-3 predicate with a synthetic new offender and shows the
// table has no row for it (red) while a synthetic row satisfies it (green).

const DOC33 = "../docs/33-end-to-end-encryption.md";
const INVENTORY_HEADING = "### §Rule-8 inventory";
const SRC = "src";

interface InventoryRow {
  module: string;
  computes: string[];
  disposition: string;
  raw: string;
}

function readInventoryFrom(text: string): InventoryRow[] {
  const lines = text.split("\n");
  const start = lines.findIndex((line) => line.startsWith(INVENTORY_HEADING));
  if (start < 0) {
    throw new Error(
      `docs/33 lost its "${INVENTORY_HEADING}" section — rule 8 has nothing to read back`,
    );
  }
  const rows: InventoryRow[] = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^#{2,4} /.test(line)) break; // the next heading ends the table
    if (!line.startsWith("| `")) continue; // header, separator, blank
    const cells = line.split("|");
    if (cells.length !== 5) throw new Error(`rule-8 row is not 3 cells: ${line}`);
    rows.push({
      module: cells[1].trim().replace(/`/g, ""),
      computes: [...cells[2].matchAll(/`([^`]+)`/g)].map((m) => m[1]),
      disposition: cells[3].trim(),
      raw: line,
    });
  }
  if (rows.length === 0) throw new Error("the rule-8 inventory table parsed to 0 rows");
  return rows;
}

function readInventory(): InventoryRow[] {
  return readInventoryFrom(readFileSync(DOC33, "utf8"));
}

/** Rule 1's pattern: what counts as an export of an engine module. */
const ENGINE_EXPORT =
  /^export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)|^export\s+class\s+([A-Za-z0-9_$]+)|^export\s+const\s+([A-Za-z0-9_$]+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\(|function|[A-Za-z0-9_$]+\s*=>)/gm;

/** Rule 4 needs EVERY export, types included, to validate a row's Compute cell. */
const ANY_EXPORT =
  /^export\s+(?:declare\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+([A-Za-z0-9_$]+)/gm;

function matchAll(source: string, re: RegExp): string[] {
  const out: string[] = [];
  for (const m of source.matchAll(re)) if (m[1]) out.push(m[1]);
  return out;
}

/**
 * The body of `function <name>`: from the first `{` that opens at paren
 * depth 0 (so a `{` inside a parameter's type literal is not mistaken for the
 * body) to its matching close. `null` means "no body found" — the gate
 * reports that rather than silently exempting the symbol.
 */
function functionBody(source: string, name: string): string | null {
  const sig = new RegExp(`^(?:export\\s+)?(?:async\\s+)?function\\s+${name}\\b`, "m");
  const m = sig.exec(source);
  if (!m) return null;
  let i = m.index + m[0].length;
  let parens = 0;
  for (; i < source.length; i += 1) {
    const c = source[i];
    if (c === "(") parens += 1;
    else if (c === ")") parens -= 1;
    else if (c === "{" && parens === 0) break;
    else if (c === ";" && parens === 0) return null;
  }
  if (i >= source.length) return null;
  const bodyStart = i;
  let braces = 0;
  for (; i < source.length; i += 1) {
    const c = source[i];
    if (c === "{") braces += 1;
    else if (c === "}") {
      braces -= 1;
      if (braces === 0) return source.slice(bodyStart, i + 1);
    }
  }
  return null;
}

/** Rule 2's second half: the body reshapes a value instead of only fetching. */
const TRANSFORM = /\.(map|filter|reduce|sort)\s*\(/;

/** Rule 3's first half: the payload reaches the lib module through `../api/…`
 *  imports — value OR type, because a pure fold type-imports its wire shapes
 *  and never calls the api layer itself. */
const API_IMPORT = /from\s*["']\.\.\/api\//;

/** Rule 3 enumerates per exported function, exactly like rule 2. */
const EXPORT_FUNCTION = /^export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/gm;

/**
 * docs/33 C1 clause 3 applied to one source: a `../api/` import AND an
 * exported body that transforms. Returns `module::export` keys. Module-
 * private helpers are covered by the exported compute that calls them (the
 * per-export discipline), which is why `billCoverage.ts`'s `terms.map` —
 * inside the non-exported `termsCover` — is not a clause-3 key.
 */
function clause3FoldsIn(module: string, source: string): string[] {
  if (!API_IMPORT.test(source)) return [];
  const folds: string[] = [];
  for (const name of matchAll(source, EXPORT_FUNCTION)) {
    const body = functionBody(source, name);
    if (body !== null && TRANSFORM.test(body)) folds.push(`${module}::${name}`);
  }
  return folds;
}

/** Rule 3's receive-only limb (docs/33 C1 clause 3): modules that take
 *  payload rows as plain parameters and declare their mirror interfaces
 *  locally, so no `../api/` import text exists to match. Pinned by name —
 *  a rename without a doc edit fails here, and a NEW receive-only fold is
 *  added to this list (and to docs/33) in the same change that adds it. */
const LEGACY_PAYLOAD_FOLDS = [
  "lib/impact.ts::dayDelta",
  "lib/impact.ts::liveImpact",
  "lib/impact.ts::livePortfolio",
  "lib/spendingFold.ts::foldCategoryDonut",
  "lib/spendingFold.ts::diffCategories",
];

/** Symbols an `api/*.ts` module imports from `../lib/…` (value imports only). */
function libImports(source: string): Set<string> {
  const symbols = new Set<string>();
  for (const m of source.matchAll(
    /import\s+(?!type\b)([^;]*?)from\s*["']\.\.\/lib\/[^"']+["'];/gs,
  )) {
    const clause = m[1];
    const braced = clause.match(/\{([^}]*)\}/);
    if (braced) {
      for (const raw of braced[1].split(",")) {
        const part = raw.trim();
        if (!part || part.startsWith("type ")) continue;
        const pieces = part.split(/\s+as\s+/);
        symbols.add((pieces[1] ?? pieces[0]).trim());
      }
    }
    const bare = clause
      .replace(/\{[^}]*\}/, "")
      .replace(/,/g, " ")
      .trim();
    if (bare && !bare.startsWith("*")) {
      for (const p of bare.split(/\s+/)) if (p) symbols.add(p);
    }
  }
  return symbols;
}

interface Enumeration {
  /** rule 1: `module::export` for every engine + searchMatch export */
  engineExports: string[];
  /** rule 2: `module::export` for every api/*.ts compute */
  apiComputes: string[];
  /** rule 3: `module::export` for every non-Engine lib fold over ../api/ payloads */
  libFolds: string[];
  /** rules 1+2 surfaces — where a `module`-level row does NOT count */
  enumeratedModules: Set<string>;
  /** rule 4: every export name per module, to validate a Compute cell */
  exportsOf: Map<string, Set<string>>;
  /** rule 4: api exports whose body could not be located */
  bodyless: string[];
}

function enumerate(tree: string): Enumeration {
  const engineFiles = readdirSync(`${tree}/lib`)
    .filter((n) => /Engine.*\.ts$/.test(n))
    .sort()
    .map((n) => `lib/${n}`);

  const engineExports: string[] = [];
  const enumeratedModules = new Set<string>(engineFiles);
  const exportsOf = new Map<string, Set<string>>();
  const bodyless: string[] = [];

  for (const module of engineFiles) {
    const source = readFileSync(`${tree}/${module}`, "utf8");
    exportsOf.set(module, new Set(matchAll(source, ANY_EXPORT)));
    for (const name of matchAll(source, ENGINE_EXPORT)) {
      engineExports.push(`${module}::${name}`);
    }
  }

  const apiComputes: string[] = [];
  for (const file of readdirSync(`${tree}/api`).filter((n) => n.endsWith(".ts")).sort()) {
    const module = `api/${file}`;
    const source = readFileSync(`${tree}/${module}`, "utf8");
    exportsOf.set(module, new Set(matchAll(source, ANY_EXPORT)));
    const libSymbols = libImports(source);
    for (const name of matchAll(source, /^export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/gm)) {
      const body = functionBody(source, name);
      if (body === null) {
        bodyless.push(`${module}::${name}`);
        continue;
      }
      const viaLib = [...libSymbols].some((s) => new RegExp(`\\b${s}\\b`).test(body));
      if (viaLib || TRANSFORM.test(body)) {
        enumeratedModules.add(module);
        apiComputes.push(`${module}::${name}`);
      }
    }
  }

  // rule 3: the same per-export transform test, pointed at lib/ modules that
  // are NOT engines (rule 1 owns those). The receive-only limb has no import
  // text to match, so it is pinned by name in LEGACY_PAYLOAD_FOLDS above.
  const libFolds: string[] = [];
  for (const file of readdirSync(`${tree}/lib`)
    .filter((n: string) => n.endsWith(".ts") && !/Engine.*\.ts$/.test(n))
    .sort()) {
    const module = `lib/${file}`;
    const source = readFileSync(`${tree}/${module}`, "utf8");
    const folds = clause3FoldsIn(module, source);
    if (folds.length === 0) continue;
    exportsOf.set(module, new Set(matchAll(source, ANY_EXPORT)));
    enumeratedModules.add(module);
    libFolds.push(...folds);
  }

  return { engineExports, apiComputes, libFolds, enumeratedModules, exportsOf, bodyless };
}

describe("rule 8 standing gate — every client compute carries a docs/33 row + disposition", () => {
  const doc33 = readFileSync(DOC33, "utf8");
  const inventory = readInventory();
  const tree = enumerate(SRC);

  const rowFor = new Map<string, InventoryRow>();
  const duplicates: string[] = [];
  for (const row of inventory) {
    for (const compute of row.computes) {
      const key = `${row.module}::${compute}`;
      if (rowFor.has(key)) duplicates.push(key);
      rowFor.set(key, row);
    }
  }

  it("rule 1: every export of an engine module has a row", () => {
    const missing = tree.engineExports.filter((key) => !rowFor.has(key));
    expect({ missing, count: missing.length }).toEqual({ missing: [], count: 0 });
  });

  it("rule 2: every client compute in api/*.ts has a row", () => {
    const missing = tree.apiComputes.filter((key) => !rowFor.has(key));
    expect({ missing, count: missing.length }).toEqual({ missing: [], count: 0 });
  });

  it("rule 2 parser: every exported api function yielded a locatable body", () => {
    expect(tree.bodyless).toEqual([]);
  });

  it("rule 3: every non-Engine lib fold over ../api/ payloads has a row", () => {
    // docs/33 C1 clause 3 (t_2d75948c): the M156 hole was that clause 1 only
    // named `*Engine*.ts`, so `positionGroups`, `exposureFold` and `impact`
    // shipped as live client computes with no row. This is clause 2's
    // transform test pointed at lib/, plus the named receive-only limb.
    const missing = tree.libFolds.filter((key) => !rowFor.has(key));
    expect({ missing, count: missing.length }).toEqual({ missing: [], count: 0 });
  });

  it("rule 3: the receive-only legacy folds stay pinned by name", () => {
    const missing = LEGACY_PAYLOAD_FOLDS.filter((key) => !rowFor.has(key));
    expect({ missing, count: missing.length }).toEqual({ missing: [], count: 0 });
  });

  it("rule 3: the three QA-named escapees carry inventory rows today", () => {
    const named = ["lib/positionGroups.ts", "lib/exposureFold.ts", "lib/impact.ts"];
    const missing = named.filter((module) => !inventory.some((row) => row.module === module));
    expect({ missing, count: missing.length }).toEqual({ missing: [], count: 0 });
  });

  it("rule 3 boundary: a synthetic new offender is caught, formatting glue is not, and the doc row is what satisfies it", () => {
    // The permanent red-proof (M156 landed and removed a probe file; this
    // keeps the same proof in-tree, driven through the very predicate rule
    // 3 uses). The offender is a NEW lib fold over ../api/ payloads with no
    // row — the gate must flag it — and a pure formatting helper over the
    // same-shaped string must NOT be flagged, or the clause over-captures
    // the glue docs/33 names in its written boundary.
    const offender = [
      'import type { Position } from "../api/portfolio";',
      "export function syntheticFoldProbe(rows: Position[]): number[] {",
      "  return rows.filter((row) => row.quantity != null).map((row) => Number(row.quantity));",
      "}",
    ].join("\n");
    const glue = [
      "export function syntheticFmtProbe(value: string | null): string {",
      '  return String(value ?? "").split("|").map((part) => part.trim()).join(" ");',
      "}",
    ].join("\n");

    const caught = clause3FoldsIn("lib/syntheticFoldProbe.ts", offender);
    expect(caught).toEqual(["lib/syntheticFoldProbe.ts::syntheticFoldProbe"]);
    expect(clause3FoldsIn("lib/syntheticFmtProbe.ts", glue)).toEqual([]);

    // RED: the real table has no row for the offender…
    expect(rowFor.has(caught[0])).toBe(false);
    // …and GREEN is exactly one table row away: the gate reads the doc, so
    // adding the row (what the offender's ship must do) is what satisfies it.
    const syntheticDoc = [
      INVENTORY_HEADING,
      "| Module (`frontend/src/`) | Compute | Disposition |",
      "| --- | --- | --- |",
      "| `lib/syntheticFoldProbe.ts` | `syntheticFoldProbe` | synthetic fixture — the inventory row is what satisfies rule 3 |",
    ].join("\n");
    const covered = readInventoryFrom(syntheticDoc).some(
      (row) => `${row.module}::${row.computes[0]}` === caught[0] && row.disposition.length > 0,
    );
    expect(covered).toBe(true);
  });

  it("C2: every prose compute bullet cites an A-row, an F-finding, or why none applies", () => {
    // docs/33 §"Compute moves client-side" is the PROSE inventory of what runs
    // in the browser. If a bullet there names a client module without saying
    // which audit row covers it, this table becomes a second, independent
    // inventory that can silently disagree with the audit — which is the rot
    // rule 8 exists to stop. "no A-row" is the one sanctioned escape, and it
    // has to be said out loud in the bullet.
    const lines = doc33.split("\n");
    const start = lines.findIndex((line) => line.startsWith("## Compute moves client-side"));
    expect(start, "docs/33 §Compute moves client-side moved").toBeGreaterThan(-1);
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i += 1) {
      if (/^#{2,3} /.test(lines[i])) {
        end = i;
        break;
      }
    }
    // A bullet is a top-level `- ` line plus its continuation lines.
    const bullets: Array<{ line: number; text: string }> = [];
    for (let i = start; i < end; i += 1) {
      const line = lines[i];
      if (line.startsWith("- ")) bullets.push({ line: i + 1, text: line });
      else if (bullets.length > 0 && /^\s/.test(line) && line.trim() !== "") {
        bullets[bullets.length - 1].text += ` ${line.trim()}`;
      }
    }
    const problems: string[] = [];
    let checked = 0;
    for (const bullet of bullets) {
      if (!/`(lib\/|components\/)/.test(bullet.text)) continue;
      checked += 1;
      const covered =
        /\bA\d+[A-Z]?\b/.test(bullet.text) ||
        /\bF\d+\b/.test(bullet.text) ||
        /no A-row/i.test(bullet.text);
      if (!covered) {
        problems.push(`docs/33:${bullet.line} — ${bullet.text.slice(0, 120)}…`);
      }
    }
    expect(checked, "the bullet parser found no compute bullets").toBeGreaterThanOrEqual(3);
    expect(problems).toEqual([]);
  });

  it("rule 4: every row points at a real module and a real export", () => {
    const problems: string[] = [];
    for (const row of inventory) {
      // Modules outside rules 1/2 (the doc-named `module` rows) are read on
      // demand rather than pre-indexed, so a row can never point at a file
      // the gate has not actually opened.
      let exports = tree.exportsOf.get(row.module);
      if (!exports) {
        const file = `${SRC}/${row.module}`;
        if (!existsSync(file)) {
          problems.push(`${row.module}: no such module under ${SRC}/ (${row.raw})`);
          continue;
        }
        exports = new Set(matchAll(readFileSync(file, "utf8"), ANY_EXPORT));
      }
      if (row.computes.length === 0) problems.push(`${row.module}: empty Compute cell`);
      for (const compute of row.computes) {
        if (compute === "module") {
          if (tree.enumeratedModules.has(row.module)) {
            problems.push(
              `${row.module}: a \`module\` row cannot stand in for rule 1/2's per-export rows`,
            );
          }
          continue;
        }
        if (!exports.has(compute)) {
          problems.push(`${row.module}: \`${compute}\` is not an export of that module`);
        }
      }
    }
    expect(problems).toEqual([]);
  });

  it("rule 4: no blank, placeholder, or duplicated disposition", () => {
    const problems: string[] = [];
    for (const row of inventory) {
      if (row.disposition.length < 10) {
        problems.push(`${row.module}::${row.computes[0]} — blank/short disposition`);
      }
      if (/^(todo|tbd|n\/a|-|\.\.\.)$/i.test(row.disposition)) {
        problems.push(`${row.module}::${row.computes[0]} — placeholder disposition`);
      }
    }
    expect({ problems, duplicates }).toEqual({ problems: [], duplicates: [] });
  });
});
