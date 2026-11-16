// @vitest-environment node
// Deploy-identity stamp (QA finding 2026-10-10) — the contract that keeps
// "which commit is live on web" answerable from outside the box.
//
// Three properties pin the whole design:
//   1. the TOKEN lives in the source index.html — if a future edit drops it,
//      stamp-build throws and the image build fails instead of silently
//      shipping an unidentifiable shell;
//   2. a valid sha is stamped verbatim, so the SERVED bytes name their commit;
//   3. REQUIRE_BUILD_COMMIT=1 (the Dockerfile) turns a forgotten build arg
//      into a FAILED build, never a wrong-but-plausible image.
import { mkdtempSync, readFileSync, rmSync, writeFileSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

import {
  BUILD_COMMIT_TOKEN,
  resolveCommit,
  stampBuildCommit,
} from "../scripts/stamp-build.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE_INDEX = path.join(FRONTEND, "index.html");

const dir = mkdtempSync(path.join(tmpdir(), "stamp-build-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

/** A dist/index.html carrying the real source bytes (token included). */
function freshIndex(): string {
  const file = path.join(dir, `index-${Math.random().toString(36).slice(2)}.html`);
  copyFileSync(SOURCE_INDEX, file);
  return file;
}

describe("stamp-build: the token is a contract on the source file", () => {
  it("index.html declares exactly one build-commit meta", () => {
    const html = readFileSync(SOURCE_INDEX, "utf8");
    expect(html.split(BUILD_COMMIT_TOKEN).length - 1).toBe(1);
  });

  it("throws when the built html no longer carries the token", () => {
    const file = path.join(dir, "stripped.html");
    writeFileSync(file, "<html><head></head></html>");
    expect(() => stampBuildCommit({ indexPath: file, env: {} })).toThrow(/not found/);
  });

  it("throws when dist/index.html is absent (vite build not run)", () => {
    expect(() =>
      stampBuildCommit({ indexPath: path.join(dir, "nope.html"), env: {} }),
    ).toThrow(/missing/);
  });
});

describe("stamp-build: sha resolution", () => {
  it("accepts a full and an abbreviated sha, normalized to lowercase", () => {
    expect(resolveCommit({ GIT_SHA: "e5ee587c0ffee1234567890abcdef0123456789a" })).toBe(
      "e5ee587c0ffee1234567890abcdef0123456789a",
    );
    expect(resolveCommit({ GIT_SHA: "E5EE587" })).toBe("e5ee587");
    expect(resolveCommit({ GIT_SHA: "  0804871  " })).toBe("0804871");
  });

  it("rejects anything that is not a commit id", () => {
    expect(resolveCommit({})).toBeNull();
    expect(resolveCommit({ GIT_SHA: "" })).toBeNull();
    expect(resolveCommit({ GIT_SHA: "__BUILD_COMMIT__" })).toBeNull();
    expect(resolveCommit({ GIT_SHA: "not-a-sha" })).toBeNull();
    expect(resolveCommit({ GIT_SHA: "e5ee58" })).toBeNull(); // 6 chars: too short
    expect(resolveCommit({ GIT_SHA: "z" .repeat(40) })).toBeNull(); // not hex
    expect(resolveCommit({ GIT_SHA: "a".repeat(41) })).toBeNull(); // too long
  });

  it("accepts the -dirty marker a checkout with uncommitted edits must carry", () => {
    // A stamp that claims commit X while shipping different bytes is the
    // misidentification this mechanism exists to prevent (QA rebuilt a clean
    // commit and got a hash that matched no build) — so a dirty checkout is
    // labelled, never laundered into a clean sha.
    expect(resolveCommit({ GIT_SHA: "b0f1491-dirty" })).toBe("b0f1491-dirty");
    expect(resolveCommit({ GIT_SHA: "B0F1491-DIRTY" })).toBe("b0f1491-dirty");
    expect(resolveCommit({ GIT_SHA: "dirty" })).toBeNull();
    expect(resolveCommit({ GIT_SHA: "b0f1491-dirt" })).toBeNull();
  });

  it("stamps the dirty marker into the served meta tag", () => {
    const file = freshIndex();
    stampBuildCommit({ indexPath: file, env: { GIT_SHA: "b0f1491-dirty" } });
    expect(readFileSync(file, "utf8")).toContain('content="b0f1491-dirty"');
  });
});

describe("stamp-build: rewriting dist/index.html", () => {
  it("stamps the sha into the meta tag", () => {
    const file = freshIndex();
    const value = stampBuildCommit({ indexPath: file, env: { GIT_SHA: "15e8de0" } });
    expect(value).toBe("15e8de0");
    const html = readFileSync(file, "utf8");
    expect(html).toContain('name="build-commit" content="15e8de0"');
    expect(html).not.toContain("__BUILD_COMMIT__");
    // The stamped file still parses as the shell it was: the two CSP-pinned
    // inline scripts must survive byte-identical, or the image's pinned
    // hashes go stale and the browser blocks the theme bootstrap.
    expect(html).toContain("localStorage.getItem(\"theme\")");
    expect(html).toContain("meta.content = document.documentElement");
  });

  it("stamps 'unknown' for a local build that never received a sha", () => {
    const file = freshIndex();
    const value = stampBuildCommit({ indexPath: file, env: {} });
    expect(value).toBe("unknown");
    expect(readFileSync(file, "utf8")).toContain('content="unknown"');
  });

  it("FAILS the build when REQUIRE_BUILD_COMMIT=1 and no sha arrived", () => {
    const file = freshIndex();
    expect(() => stampBuildCommit({ indexPath: file, env: { REQUIRE_BUILD_COMMIT: "1" } })).toThrow(
      /GIT_SHA/,
    );
    // and it did not half-write: the token is intact for the next attempt
    expect(readFileSync(file, "utf8")).toContain("__BUILD_COMMIT__");
  });

  it("stamps a mistyped build arg as 'unknown' rather than trusting it", () => {
    const file = freshIndex();
    const value = stampBuildCommit({ indexPath: file, env: { GIT_SHA: "$(git rev-parse HEAD)" } });
    expect(value).toBe("unknown");
    expect(readFileSync(file, "utf8")).toContain('content="unknown"');
  });
});
