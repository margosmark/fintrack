#!/usr/bin/env node
// M140 sweep — the authoritative audit of every money-formatter CALL in
// `frontend/src` (docs/46 §D6). TypeScript's own AST, so a call inside a
// module-level helper, a JSX expression, a ternary or a template literal is
// found the same way — no regex guessing about which component owns a line.
//
//   node scripts/sweep_money.mjs            # table + verdict
//   node scripts/sweep_money.mjs --json     # machine-readable
//   node scripts/sweep_money.mjs --check    # exit 1 if any owner does not subscribe
//
// A component "owns" a call when the call sits inside that component's body,
// or inside a same-file helper the component calls (the `formatAmount` /
// `money()` shape). Formatter LIBRARIES are excluded — they implement the
// mask and cannot subscribe (they are not components).
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ROOT = join(import.meta.dirname, "..");
const SRC = join(ROOT, "frontend", "src");
const ts = require(join(ROOT, "frontend", "node_modules", "typescript"));

/** The mask-aware formatters (docs/46 §Touch points). `fmtSignedPct` is NOT
 *  here on purpose: percentages are ruled visible (D2). `fmtResearch` is the
 *  F-12 addition — a page-local research-number formatter that used to
 *  interpolate without asking the mask, so the sweep could not see the card it
 *  rendered (the F-9/F-10/F-11 structural class). */
const MONEY = new Set([
  "fmtCad",
  "fmtCadCompact",
  "formatWithCad",
  "formatCad",
  "formatCadCompact",
  "formatMoney",
  "fmtCadSigned",
  "fmtSigned",
  "fmtNative",
  "fmtSignedPctBookCost",
  "formatAxisMoney",
  "fmtResearch",
]);

/** Formatter libraries, relative to src/ — they implement the mask. */
const FORMATTER_LIBS = new Set([
  "lib/format.ts",
  "lib/impact.ts",
  "lib/mask.ts",
  "components/charts/aria.ts",
  "components/charts/format.ts",
  "components/charts/chartDefaults.ts",
  "components/transactions/format.ts",
]);

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      walk(full, out);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

const files = walk(SRC)
  .map((f) => [f, relative(SRC, f).split(sep).join("/")])
  .filter(([, rel]) => !FORMATTER_LIBS.has(rel));

function calleeName(node) {
  const e = node.expression;
  if (ts.isIdentifier(e)) return e.text;
  if (ts.isPropertyAccessExpression(e)) return e.name.text;
  return null;
}

function declName(fn) {
  if (fn.name && ts.isIdentifier(fn.name)) return fn.name.text;
  const p = fn.parent;
  if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name)) return p.name.text;
  if (p && ts.isPropertyAssignment(p) && ts.isIdentifier(p.name)) return p.name.text;
  return null;
}

/** Stale-by-construction blocks: a re-render does NOT re-run these, so a money
 *  string built inside one would survive a mask flip. docs/46 §D3 forbids them
 *  around money formatting; the sweep pins that. */
const STALE_BLOCKS = new Set(["useMemo", "useCallback", "useEffect"]);

/** A currency formatter the shared helpers do not route through. Two shapes:
 *  an `Intl.NumberFormat(..., { style: "currency" ... })` construction, and a
 *  `` `$${n}` `` template literal (source text: backtick, dollar, dollar-brace —
 *  the first `$` is literal, the second opens the interpolation). Both are
 *  M139 leaks unless the enclosing function consults `isMasked()` itself —
 *  that is exactly how Dashboard's `fmtDelta` kept printing `$5.9K` beside a
 *  masked net worth (docs/46 §D6). */
const CURRENCY_CONSTRUCTION = /style\s*:\s*["']currency["']/;
/** `` `$${n}` `` — a literal dollar sign glued to an interpolation. This is the
 *  exact shape every page-local `$`-shim in the tree uses (`$${n.toLocale…}`),
 *  and the only dollar-then-interpolation shape that renders money. */
const DOLLAR_TEMPLATE = /`\$\$\{/;

const rows = [];
const stale = [];
const rawCurrency = [];
const problems = [];

for (const [file, rel] of files) {
  const text = readFileSync(file, "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

  // Every named function / const-arrow in the module.
  const decls = new Map();
  const collect = (node) => {
    if ((ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node)) && node.name) {
      decls.set(node.name.text, node);
    } else if (ts.isVariableStatement(node)) {
      for (const d of node.declarationList.declarations) {
        if (
          ts.isIdentifier(d.name) &&
          d.initializer &&
          (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))
        ) {
          decls.set(d.name.text, d.initializer);
        }
      }
    }
    ts.forEachChild(node, collect);
  };
  collect(sf);

  // Raw currency builders: a function that formats money itself (an
  // `Intl.NumberFormat(..., {style:"currency"})` or a `` `$${n}` `` template)
  // rather than calling a mask-aware helper. It is a leak unless it consults
  // `isMasked()` — the helper cannot mask what the helper never sees.
  for (const [name, fn] of decls) {
    const src = fn.getText(sf);
    if (!CURRENCY_CONSTRUCTION.test(src) && !DOLLAR_TEMPLATE.test(src)) continue;
    if (/\bisMasked\s*\(/.test(src)) continue; // consulted the mask itself
    const line = sf.getLineAndCharacterOfPosition(fn.getStart(sf)).line + 1;
    rawCurrency.push({
      file: rel,
      line,
      name,
      kind: CURRENCY_CONSTRUCTION.test(src) ? "Intl style:currency" : "`$${…}` template",
    });
  }

  // Money calls with the innermost NAMED function that contains them
  // (null owner = module scope). `exempt` marks docs/46 §D4 sites — the
  // working surfaces that pin `masked: false` (import wizard, reconciliation,
  // receipts, export previews); those never flip, so they need no subscription.
  const calls = [];
  const findCalls = (node, owner, staleBlock = null) => {
    if (ts.isCallExpression(node)) {
      const cn = calleeName(node);
      if (cn && MONEY.has(cn)) {
        const exempt = node.arguments.some((a) => a.kind === ts.SyntaxKind.FalseKeyword);
        calls.push({
          callee: cn,
          line: sf.getLineAndCharacterOfPosition(node.getStart()).line + 1,
          owner,
          exempt,
          staleBlock,
        });
      }
    }
    ts.forEachChild(node, (child) => {
      let nextOwner = owner;
      if (
        (ts.isFunctionDeclaration(child) || ts.isFunctionExpression(child) || ts.isArrowFunction(child)) &&
        declName(child)
      ) {
        nextOwner = declName(child);
      }
      let nextStale = staleBlock;
      if (ts.isCallExpression(child)) {
        const cn = calleeName(child);
        if (cn && STALE_BLOCKS.has(cn)) nextStale = cn;
      }
      findCalls(child, nextOwner, nextStale);
    });
  };
  findCalls(sf, null);
  if (calls.length === 0) continue;

  // A money string built inside useMemo/useCallback/useEffect survives the very
  // re-render the flip depends on (docs/46 §D3 "no useMemo around money").
  for (const c of calls) {
    if (c.staleBlock) {
      stale.push({ file: rel, line: c.line, block: c.staleBlock, callee: c.callee, owner: c.owner ?? "<module>" });
    }
  }

  // Money-touching set to a fixpoint, so `Component -> helper -> formatCad`
  // chains resolve (a helper touching money makes its callers touch money).
  const touches = new Set();
  for (let pass = 0; pass < 8; pass += 1) {
    for (const [name, fn] of decls) {
      if (touches.has(name)) continue;
      let hit = false;
      const scan = (n) => {
        if (ts.isCallExpression(n)) {
          const cn = calleeName(n);
          if ((cn && MONEY.has(cn)) || (cn && touches.has(cn))) hit = true;
        }
        ts.forEachChild(n, scan);
      };
      scan(fn);
      if (hit) touches.add(name);
    }
  }

  const isComponent = (n) => /^[A-Z]/.test(n);
  const callersOf = (helper) => {
    const out = [];
    for (const [name, fn] of decls) {
      if (!isComponent(name) || !touches.has(name)) continue;
      let hit = false;
      const scan = (n) => {
        if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === helper) hit = true;
        ts.forEachChild(n, scan);
      };
      scan(fn);
      if (hit) out.push(name);
    }
    return out;
  };

  // Per-COMPONENT subscription, not per-file: a file where one component
  // subscribes does not make its siblings re-render (docs/46 §D6).
  const componentSubscribes = new Map();
  for (const [name, fn] of decls) {
    let has = false;
    const scan = (n) => {
      if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === "useMasked") has = true;
      ts.forEachChild(n, scan);
    };
    scan(fn);
    componentSubscribes.set(name, has);
  }

  const owned = new Map();

  for (const c of calls) {
    // docs/46 §D4: the working surfaces pin `masked: false`, so the string is
    // the same masked or not — a subscription there would be dead weight.
    if (c.exempt) continue;
    if (c.owner === null) {
      problems.push({
        file: rel,
        line: c.line,
        kind: "module-scope-money",
        detail: `${c.callee}() runs at module scope and can never re-render`,
      });
      continue;
    }
    if (!touches.has(c.owner)) continue;
    if (isComponent(c.owner)) {
      owned.set(c.owner, [...(owned.get(c.owner) ?? []), c]);
      continue;
    }
    const callers = callersOf(c.owner);
    if (callers.length === 0) {
      problems.push({ file: rel, line: c.line, kind: "helper-with-no-component-caller", detail: `${c.owner}()` });
      continue;
    }
    for (const caller of callers) {
      owned.set(caller, [...(owned.get(caller) ?? []), c]);
    }
  }

  for (const [component, list] of owned) {
    rows.push({
      file: rel,
      component,
      subscribes: componentSubscribes.get(component) === true,
      calls: list.map((c) => `${c.callee}@${c.line}`),
    });
  }
}

rows.sort((a, b) => (a.file + a.component).localeCompare(b.file + b.component));
problems.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);
stale.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);
rawCurrency.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);

const json = process.argv.includes("--json");
const check = process.argv.includes("--check");

if (json) {
  console.log(JSON.stringify({ rows, problems, stale, rawCurrency }, null, 2));
} else {
  for (const r of rows) {
    const flag = r.subscribes ? "subscribes" : "NEEDS    ";
    console.log(`${flag}  ${r.file} :: ${r.component}  (${r.calls.length} call${r.calls.length === 1 ? "" : "s"})`);
  }
  for (const p of problems) {
    console.log(`PROBLEM  ${p.file}:${p.line} ${p.kind} — ${p.detail}`);
  }
  for (const s of stale) {
    console.log(`STALE    ${s.file}:${s.line} money inside ${s.block}() — owner ${s.owner} (${s.callee})`);
  }
  for (const c of rawCurrency) {
    console.log(`RAW      ${c.file}:${c.line} ${c.name}() formats currency itself via ${c.kind} and never asks the mask`);
  }
  const needs = rows.filter((r) => !r.subscribes).length;
  console.log(`\n${rows.length} money-rendering components; ${needs} without a useMasked() subscription`);
  console.log(
    `${problems.length} structural problems; ${stale.length} stale (memoized) money strings; ${rawCurrency.length} raw currency builders`,
  );
}

if (check) {
  const needs = rows.filter((r) => !r.subscribes);
  if (needs.length > 0 || problems.length > 0 || stale.length > 0 || rawCurrency.length > 0) {
    console.error("\nM140 sweep FAILED:");
    for (const r of needs) console.error(`  no subscription: ${r.file} :: ${r.component}`);
    for (const p of problems) console.error(`  ${p.kind}: ${p.file}:${p.line} (${p.detail})`);
    for (const s of stale) console.error(`  stale money: ${s.file}:${s.line} inside ${s.block}()`);
    for (const c of rawCurrency) console.error(`  raw currency builder: ${c.file}:${c.line} ${c.name}() (${c.kind})`);
    process.exit(1);
  }
  console.log(
    "\nM140 sweep OK: every money-rendering component subscribes to the mask store, and no money string is memoized past the flip.",
  );
}
