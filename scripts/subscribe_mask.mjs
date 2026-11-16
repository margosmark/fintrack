#!/usr/bin/env node
// M140 codemod — subscribe every money-rendering component to the mask store.
//
// For each component `scripts/sweep_money.mjs --json` reports as NEEDS, this
// inserts `useMasked();` as the FIRST statement of the component body (hooks
// run unconditionally, before any early return) and adds/extends the
// `lib/mask` import. Expression-bodied arrows are reported, never rewritten.
//
//   node scripts/subscribe_mask.mjs --apply
import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname, relative, sep } from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";

const require = createRequire(import.meta.url);
const ROOT = join(import.meta.dirname, "..");
const SRC = join(ROOT, "frontend", "src");
const ts = require(join(ROOT, "frontend", "node_modules", "typescript"));

const REASON =
  "// M140: re-render on a mask flip — the formatters below read the ambient\n" +
  "// mask at call time, so this component only has to re-render (docs/46 §D6).";

const sweep = JSON.parse(execFileSync("node", [join(ROOT, "scripts", "sweep_money.mjs"), "--json"], { encoding: "utf8" }));
const targets = sweep.rows.filter((r) => !r.subscribes).map((r) => [r.file, r.component]);

function relImport(fromFile, toModule) {
  let rel = relative(dirname(join(SRC, fromFile)), join(SRC, toModule)).split(sep).join("/");
  if (!rel.startsWith(".")) rel = `./${rel}`;
  return rel.replace(/\.ts$/, "");
}

const byFile = new Map();
for (const [file, component] of targets) {
  byFile.set(file, [...(byFile.get(file) ?? []), component]);
}

const manual = [];
const done = [];

for (const [file, components] of byFile) {
  const path = join(SRC, file);
  const text = readFileSync(path, "utf8");
  const sf = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

  // Locate each component's function body.
  const edits = []; // {pos, end, replacement}
  const find = (node) => {
    if (ts.isFunctionDeclaration(node) && node.name && components.includes(node.name.text)) {
      consider(node.name.text, node);
    } else if (ts.isVariableStatement(node)) {
      for (const d of node.declarationList.declarations) {
        if (
          ts.isIdentifier(d.name) &&
          components.includes(d.name.text) &&
          d.initializer &&
          (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))
        ) {
          consider(d.name.text, d.initializer);
        }
      }
    }
    ts.forEachChild(node, find);
  };

  function consider(name, fn) {
    const body = fn.body;
    if (!body || !ts.isBlock(body)) {
      manual.push(`${file} :: ${name} — expression-bodied arrow, needs a manual block body`);
      return;
    }
    const open = body.getStart(sf);
    const indent = " ".repeat(
      sf.getLineAndCharacterOfPosition(fn.getStart(sf)).character + 2,
    );
    edits.push({ pos: open + 1, end: open + 1, replacement: `\n${indent}${REASON.split("\n").join(`\n${indent}`)}\n${indent}useMasked();\n` });
  }

  find(sf);
  if (edits.length === 0) {
    manual.push(`${file} — no component bodies found for: ${components.join(", ")}`);
    continue;
  }

  // Apply body edits back-to-front so offsets stay valid.
  edits.sort((a, b) => b.pos - a.pos);
  let out = text;
  for (const e of edits) out = out.slice(0, e.pos) + e.replacement + out.slice(e.end);

  // Import: extend an existing lib/mask import, else add one.
  const spec = relImport(file, "lib/mask");
  const importRe = new RegExp(
    `import\\s*\\{([^}]*)\\}\\s*from\\s*"${spec.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}";?`,
  );
  const m = out.match(importRe);
  if (m) {
    const names = m[1]
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!names.includes("useMasked")) names.push("useMasked");
    names.sort();
    out = out.replace(importRe, `import { ${names.join(", ")} } from "${spec}";`);
  } else {
    // Insert after the LAST import line so relative order stays sane.
    const lines = out.split("\n");
    let lastImport = -1;
    for (let i = 0; i < lines.length; i += 1) {
      if (/^import\s/.test(lines[i])) lastImport = i;
      // A multi-line import ends at the line containing `from "..."`.
      if (lastImport >= 0 && /^}\s*from\s+"/.test(lines[i])) lastImport = i;
    }
    if (lastImport < 0) {
      manual.push(`${file} — could not find an import block`);
      continue;
    }
    lines.splice(lastImport + 1, 0, `import { useMasked } from "${spec}";`);
    out = lines.join("\n");
  }

  writeFileSync(path, out);
  done.push(`${file} (${components.length}: ${components.join(", ")})`);
}

console.log(`subscribed ${done.length} files:`);
for (const d of done) console.log(`  ${d}`);
if (manual.length > 0) {
  console.log(`\n${manual.length} needing manual attention:`);
  for (const s of manual) console.log(`  ${s}`);
}
