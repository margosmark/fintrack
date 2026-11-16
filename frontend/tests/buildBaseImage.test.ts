// @vitest-environment node
// Build-stage base image pinning (CVE-2026-111497 hygiene).
//
// The contract, in one line: the BUILD stage of frontend/Dockerfile names an
// EXPLICIT node PATCH (`node:<major>.<minor>.<patch>-alpine`), never the
// floating `node:22` / `node:22-alpine` tag. A floating tag resolves to
// whatever 22.x Docker Hub serves that day, so two builds a week apart can
// compile the frontend on different Node runtimes — a build is not
// reproducible, and the runtime it lands on is whatever the tag happened to
// be.
//
// Scope, stated so nobody re-derives it: this is BUILD-SUPPLY-CHAIN hygiene,
// NOT a runtime exposure. The shipped image is the serve stage
// (`nginxinc/nginx-unprivileged:alpine`), which carries no node binary at
// all — production therefore never executes the pinned runtime. The pin
// closes the audit; it does not remediate a live exploit.
//
// The other two properties pinned here exist so a FUTURE edit cannot widen
// the blast radius by accident:
//   - the serve stage stays on the nginx-unprivileged image (it is what
//     actually ships, and it is what the non-root/port-8080 contract rests
//     on);
//   - backend/Dockerfile and site/Dockerfile never gain a node base (the
//     backend image is `python:3.12-slim`, the site image is static nginx).
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const FRONTEND_DOCKERFILE = path.join(REPO, "frontend", "Dockerfile");
const BACKEND_DOCKERFILE = path.join(REPO, "backend", "Dockerfile");
const SITE_DOCKERFILE = path.join(REPO, "site", "Dockerfile");

function read(file: string): string {
  return readFileSync(file, "utf8");
}

/** Every `FROM <image>` line, in order, as the bare image reference. */
function fromLines(dockerfile: string): string[] {
  return dockerfile
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("FROM "))
    .map((line) => line.split(/\s+/)[1]);
}

describe("frontend/Dockerfile: the build stage pins an explicit node patch", () => {
  const dockerfile = read(FRONTEND_DOCKERFILE);
  const froms = fromLines(dockerfile);

  it("declares exactly two stages: the node build stage and the nginx serve stage", () => {
    expect(froms).toHaveLength(2);
  });

  it("builds on node:<major>.<minor>.<patch>-alpine — an explicit patch, not a floating tag", () => {
    const buildStage = froms[0];
    expect(buildStage).toMatch(/^node:\d+\.\d+\.\d+-alpine$/);
    // The floating tags this replaces: `node:22` and `node:22-alpine`. Both
    // are banned — the first is even less specific than the second.
    expect(buildStage).not.toBe("node:22");
    expect(buildStage).not.toBe("node:22-alpine");
  });

  it("serves from the nginx-unprivileged image, which is what actually ships", () => {
    expect(froms[1]).toBe("nginxinc/nginx-unprivileged:alpine");
  });

  it("documents the pin in the file, so the reason survives the next edit", () => {
    expect(dockerfile).toMatch(/CVE-2026-111497/);
  });
});

describe("no other image in the repo gains a node base", () => {
  it("backend/Dockerfile is python, not node", () => {
    expect(read(BACKEND_DOCKERFILE)).not.toMatch(/^FROM node/m);
  });

  it("site/Dockerfile is nginx, not node", () => {
    expect(read(SITE_DOCKERFILE)).not.toMatch(/^FROM node/m);
  });
});
