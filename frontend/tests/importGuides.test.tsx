// M71 — /help/import-guides: the per-institution hub must render for every
// institution the milestone names, with no missing page and no internal
// jargon leaking into the copy (the M68 DOM-assert pattern).
//
// Second channel: the static marketing site duplicates guide copy
// (docs/32 "copy drift rule"), so the same page is asserted to exist there
// and to be linked from the site index — a missing file is a real 404 on the
// static host, unlike the app's in-place not-found state.
import { createElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

import { Help } from "../src/pages/Help";
import { HelpGuide } from "../src/pages/HelpGuide";
import { GUIDES } from "../src/pages/helpGuides";

afterEach(() => cleanup());

/** The institutions M71's guide hub promises, by their visible names. */
const INSTITUTIONS = [
  "RBC Royal Bank",
  "TD Canada Trust (EasyWeb)",
  "BMO (Bank of Montreal)",
  "Scotiabank",
  "Tangerine",
  "Wealthsimple",
  "EQ Bank",
  "Rogers Bank",
  "MBNA",
  "Credit unions",
];

const FORBIDDEN: RegExp[] = [/docs\/\d+/, /pending_review/, /\byfinance\b/i, /bucket\/category/];

function renderGuide(slug: string) {
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: [`/help/${slug}`] },
      createElement(
        Routes,
        null,
        createElement(Route, { path: "/help/:slug", element: createElement(HelpGuide) }),
      ),
    ),
  );
}

it("the help hub links to the import guides hub", () => {
  const { container } = render(createElement(MemoryRouter, null, createElement(Help)));
  const link = container.querySelector('a[href="/help/import-guides"]');
  expect(link, "link to /help/import-guides").toBeTruthy();
  expect(link?.textContent).toContain("Import guides by bank");
});

it("import guides renders every institution with no not-found state", () => {
  const { container } = renderGuide("import-guides");
  const text = container.textContent ?? "";

  expect(text).not.toContain("Guide not found");
  for (const name of INSTITUTIONS) {
    expect(text, `institution ${name}`).toContain(name);
  }

  for (const pattern of FORBIDDEN) {
    expect(pattern.test(text), `rendered jargon ${pattern}`).toBe(false);
  }
});

it("every guide slug resolves — no page 404s", () => {
  for (const guide of GUIDES) {
    const { container } = renderGuide(guide.slug);
    const text = container.textContent ?? "";
    expect(text, `/help/${guide.slug}`).not.toContain("Guide not found");
    expect(text).toContain(guide.title);
    cleanup();
  }
});

it("offers one synthetic sample per format, all downloadable", () => {
  renderGuide("import-guides");
  for (const name of ["Sample CSV", "Sample OFX", "Sample QFX", "Sample QIF"]) {
    const link = screen.getByText(name);
    expect(link.getAttribute("download")).not.toBeNull();
    expect(link.getAttribute("href")).toMatch(/^\/help\/import-samples\/sample\.(csv|ofx|qfx|qif)$/);
  }
});

it("the sample files exist on disk", () => {
  for (const name of ["sample.csv", "sample.ofx", "sample.qfx", "sample.qif"]) {
    const raw = readFileSync(path.resolve("public", "help", "import-samples", name), "utf8");
    expect(raw.length, name).toBeGreaterThan(50);
  }
});

it("static site mirrors the guide (docs/32 copy-drift rule)", () => {
  const page = readFileSync(path.resolve("..", "site", "guides", "import-guides.html"), "utf8");
  const index = readFileSync(path.resolve("..", "site", "index.html"), "utf8");

  expect(index).toContain('href="guides/import-guides.html"');
  for (const name of INSTITUTIONS) {
    expect(page, `site page institution ${name}`).toContain(name);
  }
  for (const sample of ["sample.csv", "sample.ofx", "sample.qfx", "sample.qif"]) {
    expect(page).toContain(`samples/${sample}`);
  }
  for (const pattern of FORBIDDEN) {
    expect(pattern.test(page), `site page jargon ${pattern}`).toBe(false);
  }
});
