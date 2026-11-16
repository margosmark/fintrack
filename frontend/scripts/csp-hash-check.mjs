// CSP inline-script hash guard (docs/09 §Mobile & PWA, QA F2).
//
// The nginx CSP this repo ships pins `script-src` to `'self'` + two sha256
// hashes, one per inline <script> in index.html (the pre-hydration theme
// bootstrap and the status-bar theme-color snippet). The hashes are computed
// over the script's EXACT text content in the BUILT file — vite rewrites
// index.html, so the source file's bytes are not what the browser sees.
//
// Any edit to either snippet silently invalidates its hash and the browser
// then refuses to run the script (2 console errors per page load, and a light
// theme user getting a dark first paint — the exact defect this guard exists
// to prevent). This check runs in the Dockerfile build stage, so the image
// cannot be built with stale hashes; run it locally with `npm run check:csp`
// after `npm run build`.
//
// Passes only when EVERY `script-src` directive in the Dockerfile (there are
// one per add_header — a location that declares its own header does not
// inherit the server-level one) pins EVERY hash of every inline script in
// dist/index.html, and the Dockerfile pins no hash the build does not produce.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(here, "..");
const distHtml = path.join(frontendDir, "dist", "index.html");
const dockerfile = path.join(frontendDir, "Dockerfile");

function sha256Base64(text) {
  return createHash("sha256").update(text, "utf8").digest("base64");
}

let html;
try {
  html = readFileSync(distHtml, "utf8");
} catch {
  console.error(
    `csp-hash-check: ${distHtml} not found — run \`npm run build\` first.`,
  );
  process.exit(1);
}

const built = [];
const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
for (const match of html.matchAll(re)) {
  built.push(sha256Base64(match[1]));
}

const dockerfileText = readFileSync(dockerfile, "utf8");
// One entry per `add_header Content-Security-Policy` line (a location that
// declares its own header does not inherit the server-level one, so EVERY copy
// has to carry the hashes), tagged with its line number so a failure names the
// exact line to edit. Comment prose is skipped by keying on `add_header`.
const directives = dockerfileText
  .split("\n")
  .map((text, index) => ({ text, line: index + 1 }))
  .filter(({ text }) => text.includes("add_header Content-Security-Policy"))
  .map(({ text, line }) => ({
    line,
    text,
    body: /script-src ([^;]*)/.exec(text)?.[1] ?? "",
  }));

const problems = [];

// Every directive must pin every built hash.
for (const { line, body } of directives) {
  for (const hash of built) {
    if (!body.includes(`'sha256-${hash}'`)) {
      problems.push(
        `  line ${line}: script-src is missing 'sha256-${hash}'` +
          ` (blocks an inline script in dist/index.html)`,
      );
    }
  }
}

// The copies must carry the SAME POLICY. nginx's `add_header` does not
// inherit: a `location` that declares its own `add_header` silently DROPS every
// header it did not redeclare, so a drifted copy means that location serves a
// WEAKER policy — the failure is invisible in review (one line looks right) and
// invisible at runtime (the page still renders, just less locked down).
//
// Comparison is on the NORMALIZED policy, not the raw line: these entries are
// lines of a shell heredoc that writes the nginx config, so their leading
// whitespace becomes BLOCK NESTING — the server-level copy is indented 2 spaces
// and each `location` copy 4. That indentation is required and correct, so
// comparing raw text flags every properly-indented copy. Collapse whitespace
// and drop the heredoc quoting, then compare the directive itself.
const policyOf = (text) => {
  const after = text.slice(text.indexOf("add_header Content-Security-Policy"));
  return after
    .replace(/\\\s*$/, "") // trailing heredoc line-continuation
    .replace(/\\+"/g, '"') // escaped quotes around the policy
    .replace(/^add_header\s+Content-Security-Policy\s*/, "")
    .replace(/\balways;?\s*$/, "")
    .replace(/\s+/g, " ")
    .trim();
};

for (let i = 1; i < directives.length; i += 1) {
  if (policyOf(directives[i].text) !== policyOf(directives[0].text)) {
    problems.push(
      `  line ${directives[i].line}: Content-Security-Policy differs from line ` +
        `${directives[0].line} — nginx add_header does not inherit, so this` +
        ` location serves its OWN (possibly weaker) policy. All copies must be` +
        ` identical. (Indentation inside the heredoc is block nesting and is` +
        ` ignored; only the policy itself is compared.)`,
    );
  }
}

// `default-src` is NOT the fallback for these two, so each must be spelled out
// (a missing one is a hole the header as written does not close).
for (const { line, text } of directives) {
  for (const directive of ["base-uri", "form-action"]) {
    if (!new RegExp(`(?:^|;\\s*)${directive}\\s`).test(text)) {
      problems.push(
        `  line ${line}: no \`${directive}\` directive — default-src does not` +
          ` cover it, so a ${directive === "base-uri" ? "<base href>" : "form"}` +
          ` off-origin is allowed by this policy.`,
      );
    }
  }
}

// No directive may pin a hash the build no longer produces.
const allPinned = new Set(
  [...dockerfileText.matchAll(/sha256-[A-Za-z0-9+/=]+/g)].map((m) => m[0]),
);
for (const pinned of allPinned) {
  if (!built.includes(pinned.replace(/^sha256-/, ""))) {
    problems.push(
      `  ${pinned}: pinned in the Dockerfile but no inline script hashes to it` +
        ` (dead entry — remove it)`,
    );
  }
}

if (directives.length === 0) {
  problems.push("  no `script-src` directive found in the Dockerfile at all");
}

if (problems.length === 0) {
  console.log(
    `csp-hash-check: OK — ${built.length} inline script(s) pinned in all ` +
      `${directives.length} script-src directive(s).`,
  );
  process.exit(0);
}

console.error("csp-hash-check: FAILED — the CSP and dist/index.html disagree.\n");
for (const problem of problems) console.error(problem);
console.error(
  "\nInline scripts in dist/index.html and their hashes:\n" +
    built.map((h) => `  'sha256-${h}'`).join("\n"),
);
process.exit(1);
