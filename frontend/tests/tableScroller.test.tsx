// M64 (docs/09 "Horizontal table scroll"): the transactions tables overflow
// their ~650px box at a 1280px viewport and the columns that overflow are the
// page's core controls, so the SCROLL AFFORDANCE is contract, not decoration.
// These tests pin the parts no browser-level check can see cheaply: the
// wrapper is a labelled + focusable region (WCAG 2.1.1), the custom scrollbar
// is >=8px and ARIA-complete, the "Scroll for actions" hint appears exactly
// when scrollWidth > clientWidth (and flips at the end), the hint/track live
// in a STICKY bar so they are on screen while the table is, and both pages
// actually use the wrapper instead of a bare `overflow-x-auto` div.

import { fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { TableScroller } from "../src/components/ui/TableScroller";

/** jsdom lays nothing out: stub the two metrics the wrapper measures. */
function stubMetrics(el: HTMLElement, scrollWidth: number, clientWidth: number, scrollLeft = 0) {
  Object.defineProperty(el, "scrollWidth", { value: scrollWidth, configurable: true });
  Object.defineProperty(el, "clientWidth", { value: clientWidth, configurable: true });
  Object.defineProperty(el, "scrollLeft", { value: scrollLeft, writable: true, configurable: true });
}

function measure(el: HTMLElement) {
  fireEvent.scroll(el);
}

function renderTable(scrollWidth = 1113, clientWidth = 646) {
  const view = render(
    <TableScroller label="Transactions table" className="mt-4 min-w-0">
      <table>
        <tbody>
          <tr>
            <td>row</td>
          </tr>
        </tbody>
      </table>
    </TableScroller>,
  );
  const scroller = screen.getByRole("region", { name: "Transactions table" });
  stubMetrics(scroller, scrollWidth, clientWidth);
  measure(scroller);
  return { view, scroller };
}

describe("TableScroller (M64 — docs/09 \"Horizontal table scroll\")", () => {
  it("exposes the scroll box as a labelled, focusable region", () => {
    const { scroller } = renderTable();
    expect(scroller.tagName).toBe("DIV");
    expect(scroller.tabIndex).toBe(0);
    // The native scrollbar is suppressed by `.table-scroll` (index.css) so the
    // custom track below is the only scrollbar on the box.
    expect(scroller.className).toContain("table-scroll");
    expect(scroller.className).toContain("overflow-x-auto");
  });

  it("renders no affordance when the table fits its box", () => {
    renderTable(600, 646);
    expect(screen.queryByText(/Scroll for actions/)).toBeNull();
    expect(screen.queryByRole("scrollbar")).toBeNull();
  });

  it("renders the >=8px scrollbar and the 'Scroll for actions' hint while overflowing", () => {
    renderTable();
    const hint = screen.getByRole("button", { name: /Scroll for actions/ });
    expect(hint.textContent).toBe("Scroll for actions →");

    const track = screen.getByRole("scrollbar");
    // h-2.5 = 10px — the audit's floor is 8px, and it must be drawn in-DOM so
    // it renders in environments that suppress native scrollbars entirely.
    expect(track.className).toContain("h-2.5");
    expect(track.getAttribute("aria-orientation")).toBe("horizontal");
    expect(track.getAttribute("aria-valuemin")).toBe("0");
    expect(track.getAttribute("aria-valuemax")).toBe("100");
    expect(track.getAttribute("aria-valuenow")).toBe("0");
    expect(track.getAttribute("aria-controls")).toBe(
      screen.getByRole("region", { name: "Transactions table" }).id,
    );
    expect(track.tabIndex).toBe(0);

    // ...and both live in the sticky bar (a 50-row table is ~3000px tall, so a
    // static bar under the card would sit screenfuls below the fold).
    expect(track.parentElement?.className).toContain("sticky");
    // Below md the fixed nav.mobile-bottom-nav owns the viewport bottom, so a
    // bare `bottom-4` parked the bar INSIDE the nav band and the nav painted
    // over the hint (M82 F5): the mobile offset must clear the nav's 57px
    // (h-14 + 1px border) + its safe-area padding + 1rem of air, and the
    // desktop offset returns to bottom-4 at the nav's own `md:hidden` width.
    expect(track.parentElement?.className).toContain(
      "bottom-[calc(57px_+_env(safe-area-inset-bottom)_+_1rem)]",
    );
    expect(track.parentElement?.className).toContain("md:bottom-4");
  });

  it("does not paint an affordance for sub-pixel layout noise", () => {
    renderTable(646.4, 646);
    expect(screen.queryByRole("scrollbar")).toBeNull();
  });

  it("the hint scrolls to the end, flips its label and updates aria-valuenow", () => {
    const { scroller } = renderTable();
    const hint = screen.getByRole("button", { name: /Scroll for actions/ });

    fireEvent.click(hint);
    expect(scroller.scrollLeft).toBe(1113 - 646);

    expect(screen.getByRole("scrollbar").getAttribute("aria-valuenow")).toBe("100");
    expect(screen.getByRole("button", { name: /Scroll back/ }).textContent).toBe("← Scroll back");

    // At the end it returns to the start instead of being a dead control.
    fireEvent.click(screen.getByRole("button", { name: /Scroll back/ }));
    expect(scroller.scrollLeft).toBe(0);
    expect(screen.getByRole("button", { name: /Scroll for actions/ })).toBeTruthy();
  });

  it("supports the ARIA scrollbar keyboard model (End / Home / arrows)", () => {
    const { scroller } = renderTable();
    const track = screen.getByRole("scrollbar");

    fireEvent.keyDown(track, { key: "End" });
    expect(scroller.scrollLeft).toBe(1113 - 646);

    fireEvent.keyDown(track, { key: "ArrowLeft" });
    expect(scroller.scrollLeft).toBe(1113 - 646 - 48);

    fireEvent.keyDown(track, { key: "Home" });
    expect(scroller.scrollLeft).toBe(0);
  });

  it("re-measures when the box or its content resizes", () => {
    const { scroller } = renderTable(1113, 646);
    expect(screen.getByRole("scrollbar")).toBeTruthy();

    // Narrower box / wider content -> the thumb and valuenow follow.
    stubMetrics(scroller, 1600, 646, 1600 - 646);
    measure(scroller);
    expect(screen.getByRole("scrollbar").getAttribute("aria-valuenow")).toBe("100");
    expect(screen.getByRole("button", { name: /Scroll back/ })).toBeTruthy();
  });
});

describe("M64 page contract — both mirrored tables use the wrapper", () => {
  // vitest runs from the package root (same convention as contrastPairs.test.ts).
  const pages = ["src/pages/Transactions.tsx", "src/pages/AccountDetail.tsx"].map((path) => ({
    file: path.split("/").pop(),
    src: readFileSync(path, "utf8"),
  }));

  it.each(pages)("$file wraps its table in TableScroller", ({ src }) => {
    expect(src).toContain("<TableScroller");
    expect(src).toContain("</TableScroller>");
  });

  it.each(pages)("$file leaves no bare overflow-x-auto table wrapper behind", ({ src }) => {
    expect(src).not.toMatch(/overflow-x-auto/);
  });

  it.each(pages)("$file leads with the scan + categorize columns (M64 order)", ({ src }) => {
    const headers = [...src.matchAll(/<th[^>]*>([^<]+)<\/th>/g)].map((m) => m[1].trim());
    expect(headers.slice(0, 4)).toEqual(["Date", "Payee", "Amount", "Category"]);
    expect(headers[headers.length - 1]).toBe("Actions");
  });
});
