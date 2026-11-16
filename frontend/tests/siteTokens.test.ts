// M89 — static-site contract (docs/32 "Page contract (M89)").
//
// The marketing site has no runtime tests, so the things that silently rot
// are pinned here instead: every colour is a token declared in :root (no
// literal palette classes below the block, docs/09 discipline), the contrast
// pairs stay documented where the tokens live, and every page the site ships
// — the M89 landing/changelog/walkthroughs and, since M93 W3, the twelve
// pre-existing guides — carries landmarks, a favicon and no inline styles.
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "..", "..");
const SITE = path.join(ROOT, "site");

const read = (rel: string) => readFileSync(path.join(SITE, rel), "utf8");

/** Everything after the :root block, with that block's comments removed. */
function cssWithoutTokens(css: string): string {
  const start = css.indexOf(":root");
  expect(start, "site stylesheet declares a :root token block").toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  let depth = 0;
  let end = open;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  return css.slice(0, start) + css.slice(end + 1);
}

describe("static site token discipline", () => {
  const css = read("styles.css");

  it("keeps every colour literal inside :root", () => {
    const literals = cssWithoutTokens(css).match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g) ?? [];
    expect(literals, "literal colours below the token block").toEqual([]);
  });

  it("documents the contrast pairs next to the tokens", () => {
    expect(css).toContain("Contrast pairs (measured");
    // the pairs that carry meaning: body text and control boundaries
    expect(css).toMatch(/text\s+on bg\s+1[0-9]\.\d\d/);
    expect(css).toMatch(/border-control on bg\s+3\.\d\d/);
  });

  it("gives the ghost button a real control boundary (3:1)", () => {
    expect(css).toContain("--border-control:");
    expect(css).toMatch(/\.btn\.ghost[^}]*border: 1px solid var\(--border-control\)/);
  });
});

describe("static site page contract", () => {
  // Every page under site/, not a hand-picked list: the contract is the
  // site's, so a page added later inherits it without anyone editing here.
  const pages = sitePages();

  for (const page of pages) {
    describe(page, () => {
      const html = read(page);

      it("has landmarks", () => {
        expect(html).toContain("<header");
        expect(html).toContain("<main");
        expect(html).toContain("<footer");
      });

      it("carries a favicon (no /favicon.ico request)", () => {
        expect(html).toContain('rel="icon"');
        expect(html).toContain("data:image/svg+xml");
      });

      it("has no inline style attributes", () => {
        expect(html).not.toMatch(/\sstyle="/);
      });

      it("declares a viewport", () => {
        expect(html).toContain('name="viewport"');
      });
    });
  }

  it("links the changelog from the landing page", () => {
    expect(read("index.html")).toContain('href="changelog.html"');
  });

  it("serves /changelog from the image", () => {
    const dockerfile = read("Dockerfile");
    expect(dockerfile).toContain("location = /changelog");
    // the route is only any use if the page is in the image at all
    expect(dockerfile).toContain("changelog.html");
  });
});

/** Every `.html` page under `site/`, recursively, relative to `site/`. */
function sitePages(dir: string = SITE, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) sitePages(full, out);
    else if (entry.endsWith(".html")) out.push(path.relative(SITE, full));
  }
  return out;
}

// M92 F-M91-2 — the "No trackers" bullet on index.html claims the site loads
// nothing from anyone else. A claim like that is only worth making if
// something fails when it stops being true, so it is asserted here over
// EVERY page the site ships.
describe("zero third-party loads (docs/32 honesty claim)", () => {
  const pages = sitePages();

  it("scans a non-vacuous set of pages", () => {
    expect(pages.length).toBeGreaterThanOrEqual(9);
    expect(pages).toContain("index.html");
  });

  for (const page of pages) {
    it(`${page} requests nothing off-origin`, () => {
      const html = read(page);
      // Resource-loading tags only: `<a href>` navigations (the app login CTA)
      // are links, not loads, and are deliberately out of scope.
      const tags =
        html.match(/<(?:script|img|iframe|source|video|audio|object|embed|link)\b[^>]*>/gi) ?? [];
      const external: string[] = [];
      for (const tag of tags) {
        const attr = tag.match(/\b(?:src|href|data)\s*=\s*"([^"]*)"/i);
        if (attr && /^(?:https?:)?\/\//i.test(attr[1])) external.push(tag);
      }
      expect(external, `off-origin load in ${page}`).toEqual([]);
      // No inline <script> either — the site ships no JS at all.
      expect(html, `${page} inline script`).not.toMatch(/<script(?![^>]*\bsrc=)/i);
    });
  }

  it("keeps the stylesheet free of fetched url()/@import", () => {
    const css = read("styles.css");
    expect(css).not.toMatch(/@import/i);
    expect(css).not.toMatch(/url\(/i);
  });

  it("states a shipped-files claim the page actually ships", () => {
    const html = read("index.html");
    expect(html).toContain("one HTML document, one stylesheet and four screenshots");
    // The pre-M92 claim, false since the four PNGs landed (M89).
    expect(html).not.toContain("ships two files");
  });

  it("gives the four proof screenshots dimensions and lazy loading", () => {
    const html = read("index.html");
    const images = html.match(/<img\b[^>]*>/gi) ?? [];
    expect(images).toHaveLength(4);
    for (const img of images) {
      expect(img, img).toContain('width="1600"');
      expect(img, img).toContain('height="1000"');
      expect(img, img).toContain('loading="lazy"');
    }
  });
});
