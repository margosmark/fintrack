#!/usr/bin/env node
// M140 guard — a component that calls `useMasked()` is a HOOK consumer, so it
// may only ever be rendered as JSX. Calling it as a plain function anywhere
// ("Invalid hook call") is a crash the compiler will not catch. This walks the
// AST of every source file and reports any `Name(...)` call of a subscribed
// component that is not `<Name />`.
//
//   node scripts/hook_call_guard.mjs          # report
//   node scripts/hook_call_guard.mjs --check  # exit 1 on any offender
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ROOT = join(import.meta.dirname, "..");
const SRC = join(ROOT, "frontend", "src");
const ts = require(join(ROOT, "frontend", "node_modules", "typescript"));

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      walk(full, out);
    } else if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const files = walk(SRC);

/** Components whose body calls useMasked(), across all source files. */
const subscribed = new Map(); // name -> declaring file
for (const file of files) {
  if (/\.test\.tsx?$/.test(file)) continue;
  const sf = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const scan = (node) => {
    let name = null;
    if (ts.isFunctionDeclaration(node) && node.name) name = node.name.text;
    else if (ts.isVariableStatement(node)) {
      for (const d of node.declarationList.declarations) {
        if (
          ts.isIdentifier(d.name) &&
          d.initializer &&
          (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))
        ) {
          name = d.name.text;
        }
      }
    }
    if (name && /^[A-Z]/.test(name)) {
      let has = false;
      const inner = (n) => {
        if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === "useMasked") has = true;
        ts.forEachChild(n, inner);
      };
      inner(node);
      if (has) subscribed.set(name, relative(SRC, file).split(sep).join("/"));
    }
    ts.forEachChild(node, scan);
  };
  scan(sf);
}

const offenders = [];
for (const file of files) {
  const sf = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const rel = relative(SRC, file).split(sep).join("/");
  const scan = (node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      const name = node.expression.text;
      if (subscribed.has(name)) {
        // JSX is a JsxSelfClosingElement / JsxOpeningElement parent, not a call.
        const parent = node.parent;
        const isJsx =
          parent &&
          (ts.isJsxSelfClosingElement(parent) || ts.isJsxOpeningElement(parent)) &&
          parent.tagName === node.expression;
        const isDecl =
          parent &&
          (ts.isFunctionDeclaration(parent) ||
            (ts.isVariableDeclaration(parent) && parent.name === node.expression));
        if (!isJsx && !isDecl) {
          offenders.push({
            file: rel,
            line: sf.getLineAndCharacterOfPosition(node.getStart()).line + 1,
            name,
            declaredIn: subscribed.get(name),
          });
        }
      }
    }
    ts.forEachChild(node, scan);
  };
  scan(sf);
}

const check = process.argv.includes("--check");
if (offenders.length === 0) {
  console.log(`OK: ${subscribed.size} subscribed components are only ever rendered as JSX.`);
} else {
  for (const o of offenders) {
    console.log(`OFFENDER ${o.file}:${o.line} calls ${o.name}() as a function (hook component declared in ${o.declaredIn})`);
  }
  if (check) process.exit(1);
}
