// @vitest-environment node
// M72 — installable PWA contract (docs/09 §Installable PWA).
//
// The acceptance line for this milestone is "manifest valid (link + icons
// resolve 200)", so this test does NOT stop at parsing: it starts a real HTTP
// server over `public/` (the directory vite copies verbatim into `dist/`) and
// fetches the exact URLs the document links to — link tag → manifest → every
// icon — asserting status, content type, PNG magic bytes and the declared
// `sizes` against the IHDR. It also pins the service-worker file and the
// registration wiring, because a manifest with no worker is not installable.
import { createServer } from "node:http";
import type { Server } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ROOT = process.cwd();
const PUBLIC = path.join(ROOT, "public");

const MIME: Record<string, string> = {
  ".json": "application/json",
  ".png": "image/png",
  ".js": "text/javascript",
  ".html": "text/html",
  ".webmanifest": "application/json",
};

function attr(tag: string, name: string): string | null {
  const match = new RegExp(`${name}="([^"]+)"`).exec(tag);
  return match ? match[1] : null;
}

function pngSize(buffer: Buffer): { w: number; h: number } {
  return { w: buffer.readUInt32BE(16), h: buffer.readUInt32BE(20) };
}

const html = readFileSync(path.join(ROOT, "index.html"), "utf8");
const manifestTag =
  html
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.startsWith("<link") && line.includes('rel="manifest"')) ?? "";
const manifestHref = attr(manifestTag, "href") ?? "";
const appleTag =
  html
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.startsWith("<link") && line.includes('rel="apple-touch-icon"')) ??
  "";

const manifest = JSON.parse(
  readFileSync(path.join(PUBLIC, "manifest.json"), "utf8"),
) as {
  id: string;
  name: string;
  short_name: string;
  description: string;
  start_url: string;
  scope: string;
  display: string;
  theme_color: string;
  background_color: string;
  icons: { src: string; sizes: string; type: string; purpose?: string }[];
};

let server: Server;
let base = "";

beforeAll(async () => {
  server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    const file = path.join(PUBLIC, decodeURIComponent(url.pathname));
    if (!file.startsWith(PUBLIC) || !existsSync(file)) {
      res.writeHead(404, { "Content-Type": "text/plain" }).end("not found");
      return;
    }
    res
      .writeHead(200, {
        "Content-Type": MIME[path.extname(file)] ?? "application/octet-stream",
      })
      .end(readFileSync(file));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  base = `http://127.0.0.1:${typeof address === "object" && address ? address.port : 0}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

describe("M72 — the document links a resolvable manifest", () => {
  it("declares the install + safe-area metadata in index.html", () => {
    expect(manifestTag).not.toBe("");
    expect(manifestHref).toBe("/manifest.json");
    expect(html).toContain('name="viewport"');
    expect(html).toContain("viewport-fit=cover");
    expect(html).toContain('name="theme-color"');
    expect(html).toContain('name="apple-mobile-web-app-capable"');
    expect(html).toContain('name="apple-mobile-web-app-status-bar-style"');
    expect(appleTag).not.toBe("");
    expect(attr(appleTag, "href")).toBe("/icons/apple-touch-icon.png");
  });

  it("serves the manifest at the linked URL as JSON", async () => {
    expect(manifestHref).toMatch(/^\//);
    const response = await fetch(base + manifestHref);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("application/json");
    const body = (await response.json()) as typeof manifest;
    expect(body.name).toBe(manifest.name);
  });

  it("carries a standalone, in-scope, themable manifest", () => {
    expect(manifest.id).toBe("/");
    expect(manifest.name.length).toBeGreaterThan(0);
    expect(manifest.short_name.length).toBeGreaterThan(0);
    expect(manifest.description.length).toBeGreaterThan(0);
    expect(manifest.start_url).toBe("/dashboard");
    expect(manifest.scope).toBe("/");
    expect(manifest.display).toBe("standalone");
    expect(manifest.theme_color).toMatch(/^#[0-9a-f]{6}$/i);
    expect(manifest.background_color).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("declares 192 + 512 + maskable + iOS icons", () => {
    const sizes = manifest.icons.map((icon) => icon.sizes);
    expect(sizes).toContain("192x192");
    expect(sizes).toContain("512x512");
    expect(
      manifest.icons.some((icon) => (icon.purpose ?? "").includes("maskable")),
    ).toBe(true);
    expect(
      manifest.icons.some((icon) => icon.sizes === "180x180"),
    ).toBe(true);
    for (const icon of manifest.icons) {
      expect(icon.src).toMatch(/^\//);
      expect(icon.type).toBe("image/png");
    }
  });

  it("serves every icon with 200, image/png, and the declared dimensions", async () => {
    for (const icon of manifest.icons) {
      const response = await fetch(base + icon.src);
      expect(`${icon.src} -> ${response.status}`).toBe(`${icon.src} -> 200`);
      expect(response.headers.get("content-type")).toContain("image/png");
      const bytes = Buffer.from(await response.arrayBuffer());
      expect(bytes.subarray(0, 8)).toEqual(
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      );
      expect(bytes.byteLength).toBeGreaterThan(1_000);
      const { w, h } = pngSize(bytes);
      expect(`${icon.sizes}`).toBe(`${w}x${h}`);
    }
  });
});

describe("M72 — app-shell service worker", () => {
  const sw = readFileSync(path.join(PUBLIC, "sw.js"), "utf8");
  const pwa = readFileSync(path.join(ROOT, "src", "pwa.ts"), "utf8");
  const main = readFileSync(path.join(ROOT, "src", "main.tsx"), "utf8");

  it("ships sw.js and serves it over HTTP", async () => {
    expect(sw).toContain('addEventListener("fetch"');
    const response = await fetch(`${base}/sw.js`);
    expect(response.status).toBe(200);
  });

  it("never caches API responses — data stays server-backed", () => {
    // The fetch handler returns early for /api, so no `caches.*` call can ever
    // see a data request; the app therefore renders its own error state
    // offline instead of stale numbers (docs/09).
    expect(sw).toContain('url.pathname.startsWith("/api")');
    expect(sw).toMatch(/\/\/ Data is server-backed/);
    const apiBranch = sw.slice(
      sw.indexOf('url.pathname.startsWith("/api")'),
      sw.indexOf("if (request.mode"),
    );
    expect(apiBranch).toContain("return;");
    expect(apiBranch).not.toContain("cache.put");
  });

  it("falls back to the cached shell for offline navigations", () => {
    expect(sw).toContain('request.mode === "navigate"');
    expect(sw).toContain('"/index.html"');
    expect(sw).toContain("network first");
  });

  it("registers /sw.js in production builds only", () => {
    expect(pwa).toContain('const SW_URL = "/sw.js"');
    expect(pwa).toContain("scope: SW_SCOPE");
    expect(pwa).toContain("import.meta.env.PROD");
    expect(pwa).toContain("window.isSecureContext");
    expect(main).toContain("startPwa()");
  });
});
