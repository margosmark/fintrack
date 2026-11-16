// M89 L2/G2 — the /help learning path.
//
//   1. the three quickstart tracks are present and every step is a working link
//      (the page is executable, not descriptive);
//   2. the "Take the tour" resurface control is there and actually asks the
//      mounted tour to open;
//   3. the three walkthroughs exist in the hub copy, and every screenshot they
//      reference is a real file (a missing one is a broken image in Help);
//   4. each walkthrough has a static-site mirror carrying the same step
//      headings (docs/32's copy-drift rule, extended to the new pages).
import { cleanup, fireEvent, render } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { TOUR_START_EVENT } from "../src/components/tour/tourState";
import { Help } from "../src/pages/Help";
import { GUIDES } from "../src/pages/helpGuides";

const ROOT = path.resolve(__dirname, "..", "..");

afterEach(() => {
  cleanup();
});

function renderHelp() {
  return render(
    <MemoryRouter>
      <Help />
    </MemoryRouter>,
  );
}

describe("three ways in", () => {
  it("offers all three tracks with working step links", () => {
    const { container } = renderHelp();
    for (const title of ["Try it in 2 minutes", "Own your data", "Drive it from your agent"]) {
      expect(container.textContent, title).toContain(title);
    }
    for (const href of [
      "/dashboard",
      "/imports",
      "/transactions",
      "/register",
      "/settings",
      "/help/agent-integration",
      "/help/configuration",
      "/help/agent-tour",
    ]) {
      expect(container.querySelector(`a[href="${href}"]`), `track link ${href}`).toBeTruthy();
    }
    // track 1 hands you a ready-made statement to import
    expect(
      container.querySelector('a[href="/help/import-samples/sample.ofx"]'),
      "sample file download",
    ).toBeTruthy();
  });

  // M92 F-M91-1: the demo tenant answers `403 demo_account_restricted` to
  // POST /imports/upload (docs/20), so Track 1 may not tell a demo user to go
  // upload — it routes to the screenshot walkthrough instead, and says so.
  it("keeps Track 1 inside the demo — no live /imports step (M92 F-M91-1)", () => {
    const { container } = renderHelp();
    const tracks = container.querySelectorAll(
      'section[aria-labelledby="quickstart-heading"] > ol > li',
    );
    expect(tracks).toHaveLength(3);

    // Track 1: the walkthrough, not /imports, and the restriction in copy.
    expect(tracks[0].querySelector('a[href="/imports"]')).toBeNull();
    expect(tracks[0].querySelector('a[href="/help/first-import"]')).toBeTruthy();
    expect(tracks[0].textContent).toMatch(/read-only/);
    expect(tracks[0].textContent).not.toMatch(/nothing is written until you commit/);

    // Track 2 still owns the real (signed-in) import path.
    expect(tracks[1].querySelector('a[href="/imports"]')).toBeTruthy();
  });

  it("exposes Take the tour and fires the tour event when pressed", () => {
    const { container } = renderHelp();
    const button = Array.from(container.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Take the tour"),
    );
    expect(button, "Take the tour button").toBeTruthy();

    let fired = 0;
    const listener = () => {
      fired += 1;
    };
    window.addEventListener(TOUR_START_EVENT, listener);
    fireEvent.click(button as HTMLElement);
    window.removeEventListener(TOUR_START_EVENT, listener);
    expect(fired).toBe(1);
  });
});

describe("the three walkthroughs", () => {
  const SLUGS = ["first-import", "first-budget", "agent-tour"];

  it("exists with at least one step each", () => {
    for (const slug of SLUGS) {
      const guide = GUIDES.find((g) => g.slug === slug);
      expect(guide, `guide ${slug}`).toBeTruthy();
      expect(guide!.steps.length).toBeGreaterThan(0);
      expect(guide!.title.length).toBeGreaterThan(0);
    }
  });

  it("only references screenshots that exist", () => {
    for (const slug of SLUGS) {
      const guide = GUIDES.find((g) => g.slug === slug)!;
      for (const step of guide.steps) {
        if (!step.image) continue;
        const file = path.join(ROOT, "frontend", "public", step.image.replace(/^\//, ""));
        expect(existsSync(file), `${slug}: ${step.image}`).toBe(true);
        expect(step.imageAlt, `${slug}: alt for ${step.image}`).toBeTruthy();
      }
    }
  });

  it("has a static-site mirror carrying the same step headings", () => {
    for (const slug of SLUGS) {
      const guide = GUIDES.find((g) => g.slug === slug)!;
      const file = path.join(ROOT, "site", "guides", `${slug}.html`);
      expect(existsSync(file), `site mirror ${slug}.html`).toBe(true);
      const html = readFileSync(file, "utf8");
      expect(html, `${slug} title`).toContain(guide.title);
      for (const step of guide.steps) {
        expect(html, `${slug} step heading: ${step.heading}`).toContain(step.heading);
      }
      // landmarks + favicon: a new page must load clean (M89 static contract)
      expect(html, `${slug} has a main landmark`).toContain("<main");
      expect(html, `${slug} has a favicon`).toContain('rel="icon"');
    }
  });
});
