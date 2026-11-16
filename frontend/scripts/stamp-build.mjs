#!/usr/bin/env node
// Deploy-identity stamp (QA finding 2026-10-10) — write the COMMIT into the
// built shell so "which commit is live" is answerable from outside the box.
//
// The problem this kills: the deployed web image carried a content hash
// (index-DVGtM7iv.js) that no local build reproduced, and nothing in the
// SERVED bytes named a commit — so the live web build was unidentifiable
// from outside, and answering it took 13 rebuilds across 9 candidate
// commits. A meta tag in index.html ends that class of investigation:
//   curl -s https://<host>/ | grep -o 'name="build-commit" content="[^"]*"'
//
// Contract (the same shape as stamp-sw.mjs — a token that MUST exist):
//   * `frontend/index.html` declares `<meta name="build-commit"
//     content="__BUILD_COMMIT__" />` in <head>. That line is the contract.
//   * This script rewrites it in dist/index.html with process.env.GIT_SHA.
//   * Missing token -> throw, so the image build fails instead of shipping
//     an unidentifiable shell again.
//
// Determinism: the stamp is the commit sha and NOTHING else — no timestamp,
// no hostname. Rebuilding the same commit reproduces byte-identical
// index.html (and therefore the identical sw.js BUILD_ID, since stamp-sw
// hashes this file — run stamp-build BEFORE stamp-sw so the cache id is a
// function of the exact bytes that ship, commit stamp included).
//
// Honesty when the sha is unavailable:
//   * REQUIRE_BUILD_COMMIT=1 (the Dockerfile sets it) + no valid sha -> the
//     build FAILS. An image can never be built without knowing its commit.
//   * Otherwise (local `npm run build`) -> stamped "unknown": a local dist is
//     never served, and "unknown" beats a wrong sha.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** The exact meta line `index.html` declares; the whole stamping contract. */
export const BUILD_COMMIT_TOKEN = '<meta name="build-commit" content="__BUILD_COMMIT__" />';

/**
 * A git object id, full or abbreviated — anything else is not a commit.
 * The optional `-dirty` suffix is the honesty marker: a deploy from a
 * checkout carrying uncommitted edits stamps `<sha>-dirty`, because a stamp
 * that claims a commit while shipping different bytes is exactly the
 * misidentification this whole mechanism exists to prevent.
 */
export const SHA_RE = /^[0-9a-f]{7,40}(-dirty)?$/i;

/**
 * Resolve the sha to stamp, or null when there is none.
 * GIT_SHA wins; an invalid value is treated as absent (a mistyped build arg
 * must not reach the shell as if it were a commit).
 *
 * @param {Record<string, string | undefined>} [env]
 * @returns {string | null}
 */
export function resolveCommit(env = process.env) {
  const raw = (env.GIT_SHA ?? "").trim().toLowerCase();
  return SHA_RE.test(raw) ? raw : null;
}

/**
 * Rewrite dist/index.html's placeholder. Throws on a missing token.
 *
 * @param {{indexPath?: string, env?: Record<string, string | undefined>}} [options]
 * @returns {string} the commit stamped (or "unknown" for a local build)
 */
export function stampBuildCommit({ indexPath, env = process.env } = {}) {
  const indexFile = indexPath ?? path.join(FRONTEND, "dist", "index.html");
  if (!existsSync(indexFile)) {
    throw new Error(`stamp-build: ${indexFile} is missing — run \`vite build\` first`);
  }
  const source = readFileSync(indexFile, "utf8");
  if (!source.includes(BUILD_COMMIT_TOKEN)) {
    throw new Error(
      `stamp-build: ${BUILD_COMMIT_TOKEN} not found in ${indexFile} — the shell can no longer be stamped with its commit`,
    );
  }
  const commit = resolveCommit(env);
  if (!commit && env.REQUIRE_BUILD_COMMIT === "1") {
    throw new Error(
      "stamp-build: REQUIRE_BUILD_COMMIT=1 but GIT_SHA is unset or not a commit sha — pass the build arg (docker compose passes $(git rev-parse HEAD))",
    );
  }
  const value = commit ?? "unknown";
  writeFileSync(indexFile, source.replaceAll(BUILD_COMMIT_TOKEN, BUILD_COMMIT_TOKEN.replace("__BUILD_COMMIT__", value)));
  return value;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const commit = stampBuildCommit();
  console.log(`stamp-build: dist/index.html build-commit = ${commit}`);
}
