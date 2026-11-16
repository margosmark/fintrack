// M82-F1 — the /reports family strip must never squeeze a label out of its
// button (docs/09 "Underline tab strips wrap, never squeeze").
//
// The strip is ~740px of labels in a viewport of 320–412. Two ways to be
// wrong: a nowrap flex row SHRINKS the buttons (text spills into the
// neighbour — 30.4px of glyph-on-glyph overlap at 412 — and the last tab
// lands past the viewport edge with document.scrollWidth == innerWidth, so it
// is unreachable), or a scroller parks the far tabs off-screen. The shipped
// contract is `flex-wrap` + per-button `shrink-0 whitespace-nowrap`, which
// packs the row onto as many lines as the viewport needs.
//
// jsdom has no layout, so the geometry itself is measured live (QA harness
// m82/round13 glyph rects + m82/round15 kind=flow text-node sweep; evidence on
// t_ec231361 — fitsInBtn must be computed with br.right/br.left, DOMRect has
// no `.r`). What this file pins is the DOM contract that produces that
// geometry, so a className "tidy-up" cannot silently reintroduce the squeeze:
//   - the strip flexes AND wraps
//   - every tab button is individually unshrinkable and single-line
//   - all 7 families render, in docs/09 order
//   - the morphing underline lives under the ACTIVE tab only, and follows a
//     click that lands on the other wrapped row (row-spanning morph).

import type { ReactElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Reports } from "../src/pages/Reports";

/** jsdom has no matchMedia (ThemeProvider / reduced-motion read it). */
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width:\s*(\d+)px/.test(query) ? window.innerWidth >= 768 : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// M108 P2: Reports reads `user.saved_report_views` for its "Saved views"
// control. The strip is the unit under test, so the auth context is stubbed
// to a logged-out shape (the same stub `reportMoneyAxes` / `m97Tooltips`
// already use) — no session, no fetch, no AuthProvider in the wrapper.
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, setUser: () => {} }),
}));

// M108 P3: Reports probes `GET /partner/state` on mount. The strip is the unit
// under test, so answer the lone-tenant shape instead of letting a real fetch
// reject inside jsdom.
vi.mock("../src/api/sharing", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/sharing")>();
  return {
    ...actual,
    getPartnerState: vi.fn(async () => ({ linked: false })),
    fetchAllPartnerTransactions: vi.fn(async () => []),
  };
});

// Every report fetcher stays PENDING: the tab strip renders regardless of
// data, and a never-resolving query keeps the panels on skeletons — no
// fixture shapes, no recharts. The strip is the unit under test.
// vi.mock factories are hoisted above module scope — keep the helper hoisted too.
const pending = vi.hoisted(() => <T,>(): Promise<T> => new Promise<never>(() => {}));
vi.mock("../src/api/reports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/reports")>();
  return {
    ...actual,
    getSpending: vi.fn(pending),
    getSpendingCategories: vi.fn(pending),
    getCashflow: vi.fn(pending),
    getNetWorth: vi.fn(pending),
    getInvestmentPerformance: vi.fn(pending),
    getDividendReport: vi.fn(pending),
  };
});
vi.mock("../src/api/projections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/projections")>();
  return { ...actual, getProjectionDefaults: vi.fn(pending) };
});

const TABS = [
  "Spending",
  "Categories",
  "Cash flow",
  "Net worth",
  "Performance",
  "Dividends",
  "Projections",
];

function renderReports(initialPath = "/reports"): HTMLElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <MemoryRouter initialEntries={[initialPath]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <LocationProbe />
            <Reports />
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
  const { container } = render(ui);
  return container;
}

/** Current location, so the `?tab=` deep link (M95 P1) is assertable. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function tabButtons(container: HTMLElement): HTMLButtonElement[] {
  const all = Array.from(container.querySelectorAll("button"));
  return all.filter((b) => TABS.includes((b.textContent ?? "").trim()));
}

describe("M82-F1 /reports tab strip (docs/09 'Underline tab strips wrap, never squeeze')", () => {
  it("renders all 7 families in spec order", () => {
    const container = renderReports();
    expect(tabButtons(container).map((b) => (b.textContent ?? "").trim())).toEqual(TABS);
  });

  it("the strip flexes AND wraps — a nowrap row is what squeezed the labels", () => {
    const container = renderReports();
    const [first] = tabButtons(container);
    expect(first).toBeDefined();
    const strip = first.parentElement as HTMLElement;
    expect(strip.classList.contains("flex")).toBe(true);
    expect(strip.classList.contains("flex-wrap")).toBe(true);
    expect(strip.classList.contains("gap-1")).toBe(true);
    expect(strip.classList.contains("border-b")).toBe(true);
  });

  it("every tab button is unshrinkable and single-line (shrink-0 + whitespace-nowrap)", () => {
    const container = renderReports();
    const buttons = tabButtons(container);
    expect(buttons).toHaveLength(7);
    for (const b of buttons) {
      const label = (b.textContent ?? "").trim();
      expect(b.classList.contains("shrink-0"), `${label} must be shrink-0`).toBe(true);
      expect(b.classList.contains("whitespace-nowrap"), `${label} must be nowrap`).toBe(true);
    }
    // Padding tightens only below sm; the desktop rhythm is unchanged.
    expect(buttons[0].className).toContain("px-2");
    expect(buttons[0].className).toContain("sm:px-3.5");
  });

  it("the morphing underline sits under the active tab only and follows a cross-row click", () => {
    const container = renderReports();
    const byLabel = (label: string) =>
      tabButtons(container).find((b) => (b.textContent ?? "").trim() === label);
    const spending = byLabel("Spending");
    const performance_ = byLabel("Performance");
    expect(spending).toBeDefined();
    expect(performance_).toBeDefined();

    const underline = (b: HTMLButtonElement | undefined) =>
      b?.querySelector("span[aria-hidden]") ?? null;

    // Default tab: Spending owns the underline, nobody else has one.
    expect(underline(spending)).not.toBeNull();
    expect(underline(performance_)).toBeNull();

    // Performance sits on the other wrapped row at 412 — the morph must land there.
    fireEvent.click(performance_!);
    expect(underline(performance_)).not.toBeNull();
    expect(underline(spending)).toBeNull();
  });
});

// M95 P1 — the command palette deep-links a report tab (`/reports?tab=<id>`),
// so the param is part of the strip's contract: honoured on mount, ignored when
// it names nothing real, and written back on every tab click.
describe("M95 P1 /reports?tab= deep link", () => {
  const underlineOf = (container: HTMLElement, label: string) => {
    const button = tabButtons(container).find(
      (b) => (b.textContent ?? "").trim() === label,
    );
    return button?.querySelector("span[aria-hidden]") ?? null;
  };

  it("lands on the tab the param names", () => {
    const container = renderReports("/reports?tab=dividends");
    expect(underlineOf(container, "Dividends")).not.toBeNull();
    expect(underlineOf(container, "Spending")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe("/reports?tab=dividends");
  });

  it("falls back to Spending for an unknown tab", () => {
    const container = renderReports("/reports?tab=not-a-report");
    expect(underlineOf(container, "Spending")).not.toBeNull();
  });

  it("writes the param back when a tab is clicked", () => {
    const container = renderReports("/reports");
    const performance_ = tabButtons(container).find(
      (b) => (b.textContent ?? "").trim() === "Performance",
    );
    fireEvent.click(performance_!);
    expect(screen.getByTestId("location").textContent).toBe("/reports?tab=performance");
  });
});
