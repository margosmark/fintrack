// Anti-invention source + bundle scan (docs/47 §13 example 16): the artifact's
// mockup-only copy must never reach the product, and the built bundle a browser
// actually loads must not carry it either.
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, "../../../");
const SRC = path.join(FRONTEND, "src");
const DIST = path.join(FRONTEND, "dist");

function walk(dir, exts) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full, exts));
    else if (exts.some((e) => entry.endsWith(e))) out.push(full);
  }
  return out;
}

const FORBIDDEN = [
  "local mockup interaction",
  "illustrative figures",
  "placeholder figure",
];

export default {
  name: "static-scan",
  async run(ctx) {
    const srcFiles = walk(SRC, [".ts", ".tsx", ".css", ".html"]);
    const hits = [];
    for (const file of srcFiles) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const needle of FORBIDDEN) {
        if (text.includes(needle)) hits.push(`${path.relative(FRONTEND, file)}: ${needle}`);
      }
    }
    ctx.check(
      "scan.16a",
      "frontend/src carries no mockup-only copy (example 16)",
      hits.length === 0,
      hits.join("; ") || `0 matches across ${srcFiles.length} files`,
    );

    const distFiles = walk(DIST, [".js", ".html", ".css"]);
    const distHits = [];
    for (const file of distFiles) {
      const text = readFileSync(file, "utf8").toLowerCase();
      for (const needle of FORBIDDEN) {
        if (text.includes(needle)) distHits.push(`${path.relative(FRONTEND, file)}: ${needle}`);
      }
    }
    ctx.check(
      "scan.16b",
      "built bundle served to the browser carries no mockup-only copy",
      distHits.length === 0,
      distHits.join("; ") || `0 matches across ${distFiles.length} dist files`,
    );

    // Vendor-token leak (docs/09 §Naming): internal enum/vendor names must not
    // be user-facing copy. Scoped to the components/pages that RENDER copy —
    // `src/api/*` legitimately carries wire values (`"yfinance"`, `"boc_valet"`)
    // that are request parameters, not text anyone reads.
    const renderFiles = [...srcFiles].filter((f) => /\/(components|pages)\//.test(f));
    const copyHits = [];
    const yahooCopy = [];
    for (const file of renderFiles) {
      // Strip comments first: the spec files are comment-heavy and name the
      // forbidden tokens by design ("never `yfinance`"). Then only flag
      // MULTI-WORD literals — single tokens are wire values / source enum
      // comparisons (`source === "yfinance"`), not copy anyone reads.
      const text = readFileSync(file, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1");
      // docs/09 §M68 + tests/jargonLeaks.test.tsx pin `yfinance` (and the
      // BoC/vendor tokens) out of rendered copy. The single word "Yahoo" is
      // named deliberately in the M141 dead-symbol copy and is NOT in that
      // pin — it is collected below as an observation for QA, not a failure.
      for (const needle of ["yfinance", "BoC"]) {
        const re = new RegExp(`["'\`]([^"'\`\\n]*${needle}[^"'\`\\n]*)["'\`]`, "gi");
        for (const m of text.matchAll(re)) {
          if (m[1].length < 90 && /\s/.test(m[1])) {
            copyHits.push(`${path.relative(FRONTEND, file)}: ${m[1]}`);
          }
        }
      }
      for (const m of text.matchAll(/["'\`]([^"'\`\\n]*\byahoo\b[^"'\`\\n]*)["'\`]/gi)) {
        if (m[1].length < 120 && /\s/.test(m[1])) {
          yahooCopy.push(`${path.relative(FRONTEND, file)}: ${m[1]}`);
        }
      }
    }
    if (yahooCopy.length > 0) {
      ctx.note(
        `observation (not a pinned violation): vendor name "Yahoo" appears in ${yahooCopy.length} rendered literals — ${yahooCopy.slice(0, 3).join(" | ")}`,
      );
    }
    ctx.check(
      "scan.vendor-copy",
      "no vendor/enum tokens inside user-facing string literals",
      copyHits.length === 0,
      copyHits.slice(0, 6).join(" | ") ||
        `0 matches in ${renderFiles.length} component/page files (${yahooCopy.length} "Yahoo" literals observed)`,
    );
  },
};
