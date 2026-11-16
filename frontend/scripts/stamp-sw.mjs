#!/usr/bin/env node
// M114 — stamp the service worker's BUILD_ID at build time.
//
// `frontend/public/*` is copied into `dist/` VERBATIM: it never passes through
// Vite's transform, so `define`/`import.meta.env` cannot reach `public/sw.js`.
// CACHE_NAME was therefore the literal `fintrack-shell-v1` forever — the
// activate handler only ever purged caches whose name differs, so no deploy
// ever rotated it and dead chunks accumulated. This script rewrites the one
// placeholder line in `dist/sw.js` with a 12-hex id derived from the built
// `index.html` (the shell itself: it names the hashed entry script + css, so
// every deploy that changes a chunk changes the id, and rebuilding identical
// sources reproduces the identical id — no timestamps, no cache churn).
//
// Wired into `npm run build` (and therefore into the Dockerfile build stage),
// so an un-stamped worker cannot ship.

import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** The exact line `public/sw.js` declares; the whole stamping contract. */
export const BUILD_ID_TOKEN = 'const BUILD_ID = "__BUILD_ID__";';

/** Shell identity: sha256 of the built index.html, first 12 hex chars. */
export function buildIdFromIndexHtml(html) {
  return createHash("sha256").update(html).digest("hex").slice(0, 12);
}

export function stampSw({ swPath, indexPath } = {}) {
  const swFile = swPath ?? path.join(FRONTEND, "dist", "sw.js");
  const indexFile = indexPath ?? path.join(FRONTEND, "dist", "index.html");
  for (const file of [swFile, indexFile]) {
    if (!existsSync(file)) {
      throw new Error(`stamp-sw: ${file} is missing — run \`vite build\` first`);
    }
  }
  const id = buildIdFromIndexHtml(readFileSync(indexFile, "utf8"));
  const source = readFileSync(swFile, "utf8");
  if (!source.includes(BUILD_ID_TOKEN)) {
    throw new Error(
      `stamp-sw: ${BUILD_ID_TOKEN} not found in ${swFile} — the worker's CACHE_NAME can no longer be stamped`,
    );
  }
  writeFileSync(swFile, source.replace(BUILD_ID_TOKEN, `const BUILD_ID = "${id}";`));
  return id;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const id = stampSw();
  console.log(`stamp-sw: dist/sw.js CACHE_NAME = fintrack-shell-v1-${id}`);
}
