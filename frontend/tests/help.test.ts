import { createElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { Help } from "../src/pages/Help";
import { HelpGuide } from "../src/pages/HelpGuide";
import { GUIDES } from "../src/pages/helpGuides";

afterEach(() => cleanup());

it("hub lists every guide with a link", () => {
  const { container } = render(
    createElement(MemoryRouter, null, createElement(Help)),
  );
  for (const g of GUIDES) {
    // The quickstart tracks also deep-link to guides (M89), so match on the
    // card that carries the guide's own title, not merely the first href hit.
    const links = container.querySelectorAll(`a[href="/help/${g.slug}"]`);
    expect(links.length, `link to /help/${g.slug}`).toBeGreaterThan(0);
    let titled = false;
    links.forEach((link) => {
      if ((link.textContent ?? "").includes(g.title)) titled = true;
    });
    expect(titled, `guide card for ${g.slug} titled ${g.title}`).toBe(true);
  }
});

it("guide page renders steps and screenshots for a slug", () => {
  render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/help/rules"] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: "/help/:slug",
          element: createElement(HelpGuide),
        }),
      ),
    ),
  );
  expect(screen.getByText("Rules & automation")).toBeTruthy();
  expect(screen.getByText("Stop vs continue")).toBeTruthy();
  const img = screen.getByAltText(
    "Categorization rules editor with pattern, priority, and stop toggle",
  );
  expect(img.getAttribute("src")).toBe("/help/rules.png");
});

it("unknown slug shows the not-found state", () => {
  render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/help/nope"] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: "/help/:slug",
          element: createElement(HelpGuide),
        }),
      ),
    ),
  );
  expect(screen.getByText("Guide not found")).toBeTruthy();
});

it("migrate-from-ynab guide carries export steps and both gap tables", () => {
  render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/help/migrate-from-ynab"] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: "/help/:slug",
          element: createElement(HelpGuide),
        }),
      ),
    ),
  );
  expect(screen.getByText("Migrate from YNAB")).toBeTruthy();
  expect(screen.getByText("Export from YNAB")).toBeTruthy();
  expect(screen.getByText("Preview the whole plan first")).toBeTruthy();

  const tables = screen.getAllByRole("table");
  expect(tables).toHaveLength(2);
  expect(screen.getByText("What maps cleanly")).toBeTruthy();
  expect(screen.getByText("What does not come across")).toBeTruthy();

  // The gaps table is the honest one: every row names a YNAB thing AND its
  // outcome, and it is long enough to cover splits/schedules/targets/E2EE.
  const gaps = Array.from(tables[1].querySelectorAll("tbody tr"));
  expect(gaps.length).toBeGreaterThanOrEqual(8);
  for (const row of gaps) {
    const cells = row.querySelectorAll("td");
    expect(cells).toHaveLength(2);
    expect(cells[0].textContent?.trim().length).toBeGreaterThan(0);
    expect(cells[1].textContent?.trim().length).toBeGreaterThan(0);
  }
  expect(tables[1].textContent).toContain("Scheduled transactions");
  expect(tables[1].textContent).toContain("encryption required");

  // The hub links it too.
  const hub = render(createElement(MemoryRouter, null, createElement(Help)));
  expect(
    hub.container.querySelector('a[href="/help/migrate-from-ynab"]'),
  ).toBeTruthy();
});
