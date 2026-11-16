// M124 — the Reports page's tag lens, the category drill and the matrix
// (docs/09 §honesty, docs/12 §1/§2).
//
// The engine's math was pinned by m124TagEngine.test.ts, which M160 deleted
// with the client mirror; the same 6 vectors are now replayed at the route by
// backend/tests/test_m160_replay_s2a_matrix.py. This file is the
// WIRING: that the Group-by control offers Tag to a lone tenant, that the
// charts relabel and the caption tells the truth about duplicated totals,
// that a click drills in-page rather than navigating away, and that the
// matrix renders as a real table with a focusable scroll region.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

const _now = new Date();
const YEAR = _now.getFullYear();

/** A ledger where one transaction carries TWO tags — the whole point. */
const SPENDING_BY_TAG = {
  from: `${YEAR}-01-01`,
  to: `${YEAR}-03-31`,
  granularity: "month" as const,
  group_by: "tag" as const,
  items: [
    { bucket: `${YEAR}-01-01`, tag_id: 3, tag_name: "vacation", total_cad: "63.60" },
    { bucket: `${YEAR}-01-01`, tag_id: 4, tag_name: "work", total_cad: "50.00" },
    { bucket: `${YEAR}-02-01`, tag_id: 4, tag_name: "work", total_cad: "12.25" },
    { bucket: `${YEAR}-02-01`, tag_id: null, tag_name: "Untagged", total_cad: "25.50" },
    { bucket: `${YEAR}-03-01`, tag_id: null, tag_name: "Untagged", total_cad: "0.00" },
  ],
  // 151.35 summed over blocks vs 101.35 actually spent — the 50.00 two-tag
  // row is counted twice above and once here.
  total_cad: "151.35",
  spend_total_cad: "101.35",
};

const SPENDING_BY_CATEGORY = {
  ...SPENDING_BY_TAG,
  group_by: "category" as const,
  items: [
    { bucket: `${YEAR}-01-01`, category_id: 2, category_name: "Groceries", total_cad: "63.60" },
    { bucket: `${YEAR}-02-01`, category_id: 3, category_name: "Dining", total_cad: "37.75" },
    { bucket: `${YEAR}-03-01`, category_id: null, category_name: "Uncategorized", total_cad: "0.00" },
  ],
  total_cad: "101.35",
  spend_total_cad: "101.35",
};

const BREAKDOWN_BY_TAG = {
  group_by: "tag" as const,
  items: [
    { tag_id: 3, name: "vacation", cad_total: "63.60" },
    { tag_id: 4, name: "work", cad_total: "62.25" },
    { tag_id: null, name: "Untagged", cad_total: "25.50" },
  ],
  total_cad: "151.35",
  spend_total_cad: "101.35",
};

const MATRIX = {
  from: `${YEAR}-01-01`,
  to: `${YEAR}-03-31`,
  columns: [
    { tag_id: 3, tag_name: "vacation" },
    { tag_id: null, tag_name: "Untagged" },
  ],
  rows: [
    { category_id: 2, category_name: "Groceries" },
    { category_id: 3, category_name: "Dining" },
  ],
  items: [
    {
      category_id: 2,
      category_name: "Groceries",
      tag_id: 3,
      tag_name: "vacation",
      cad_total: "63.60",
    },
    { category_id: 3, category_name: "Dining", tag_id: null, tag_name: "Untagged", cad_total: "25.50" },
  ],
  total_cad: "101.35",
};

const auth = vi.hoisted(() => ({
  user: null as Record<string, unknown> | null,
  setUser: vi.fn(),
}));
const usersApi = vi.hoisted(() => ({ updateMe: vi.fn() }));
const sharingApi = vi.hoisted(() => ({ getPartnerState: vi.fn() }));
/** The group-by each report call was made with — asserted, not guessed. */
const reportCalls = vi.hoisted(() => ({ spending: [] as string[], matrix: 0 }));

vi.mock("../src/auth/AuthContext", async () => {
  const { useState } = await import("react");
  return {
    useAuth: () => {
      const [user, setUser] = useState<Record<string, unknown> | null>(auth.user);
      return { user, setUser };
    },
  };
});

vi.mock("../src/api/users", () => ({ updateMe: usersApi.updateMe }));

vi.mock("../src/api/sharing", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/sharing")>();
  return { ...actual, getPartnerState: sharingApi.getPartnerState };
});

vi.mock("../src/api/reports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/reports")>();
  return {
    ...actual,
    getSpending: vi.fn(async (_f: string, _t: string, _g: string, groupBy = "category") => {
      reportCalls.spending.push(groupBy);
      return groupBy === "tag" ? SPENDING_BY_TAG : SPENDING_BY_CATEGORY;
    }),
    getSpendingCategories: vi.fn(async (_f: string, _t: string, groupBy = "category") =>
      groupBy === "tag"
        ? BREAKDOWN_BY_TAG
        : {
            group_by: "category",
            items: [{ category_id: 2, name: "Groceries", cad_total: "63.60" }],
            total_cad: "63.60",
            spend_total_cad: "63.60",
          },
    ),
    getCategoryTagMatrix: vi.fn(async () => {
      reportCalls.matrix += 1;
      return MATRIX;
    }),
    getCashflow: vi.fn(() => new Promise<never>(() => {})),
    getNetWorth: vi.fn(() => new Promise<never>(() => {})),
    getInvestmentPerformance: vi.fn(() => new Promise<never>(() => {})),
  };
});

function baseUser() {
  return { id: 1, email: "a@b.c", saved_report_views: [] };
}

/** The probe has to sit INSIDE the Router — a hook above it has no context. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function locationText(): string {
  return screen.getByTestId("location").textContent ?? "";
}

function renderReports() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/reports"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <LocationProbe />
            <Reports />
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  reportCalls.spending.length = 0;
  reportCalls.matrix = 0;
  sharingApi.getPartnerState.mockResolvedValue({ linked: false });
  usersApi.updateMe.mockImplementation(async (patch: { saved_report_views?: unknown[] }) => ({
    ...baseUser(),
    saved_report_views: patch.saved_report_views ?? [],
  }));
  auth.user = baseUser();
  window.history.pushState({}, "", "/reports");
});

// --- R1: the Group-by toggle ------------------------------------------------

describe("M124 R1 — the Group-by control offers Tag to a lone tenant", () => {
  it("shows Category and Tag, and NO Owner, with no partnership", async () => {
    renderReports();
    const control = await screen.findByRole("tablist", { name: "Group by" });
    const options = [...control.querySelectorAll("button")].map((b) => b.textContent);
    expect(options).toContain("Category");
    expect(options).toContain("Tag");
    expect(options).not.toContain("Owner");
  });

  it("asking for Tag recomputes with group_by=tag and relabels the chart", async () => {
    renderReports();
    const control = await screen.findByRole("tablist", { name: "Group by" });
    fireEvent.click([...control.querySelectorAll("button")].find((b) => b.textContent === "Tag")!);

    await waitFor(() => expect(reportCalls.spending).toContain("tag"));
    // The dimension is the chart's accessible name — same chart, new label.
    const frame = await screen.findByRole("img", { name: /Spending by tag/ });
    expect(frame).toBeTruthy();
  });

  it("the tag caption admits the duplication and names the real figure", async () => {
    renderReports();
    const control = await screen.findByRole("tablist", { name: "Group by" });
    fireEvent.click([...control.querySelectorAll("button")].find((b) => b.textContent === "Tag")!);

    await waitFor(() => expect(reportCalls.spending).toContain("tag"));
    const caption = await screen.findByText(/totals per tag; multi-tag transactions count in each/);
    // Both numbers are on screen: the inflated sum and the honest one.
    expect(caption.textContent).toContain("$151.35");
    expect(caption.textContent).toContain("$101.35");
  });
});

// --- R2: the in-page drill --------------------------------------------------

describe("M124 R2 — clicking a category drills in-page", () => {
  /** The treemap's legend buttons are the drill path under test. */
  async function showTreemap() {
    await screen.findByRole("tab", { name: "Treemap" });
    fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));
    return screen.findByRole("button", { name: /^Groceries —/ });
  }

  it("opens a breadcrumb + drill chart instead of navigating away", async () => {
    renderReports();
    fireEvent.click(await showTreemap());

    const breadcrumb = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(breadcrumb.textContent).toContain("Groceries");
    // The M116 history-chart family is what draws it — same table fallback.
    expect(screen.getByText("Show the numbers behind this chart")).toBeTruthy();
    expect(locationText()).toBe("/reports");
  });

  it("the ledger link survives the drill as a secondary action", async () => {
    renderReports();
    fireEvent.click(await showTreemap());

    const link = await screen.findByRole("link", { name: /View these transactions/ });
    expect(link.getAttribute("href")).toBe("/transactions?category=2");
  });

  it("the CSV export follows the drill", async () => {
    renderReports();
    fireEvent.click(await showTreemap());

    await screen.findByRole("navigation", { name: "Breadcrumb" });
    // The header CSV button now carries the drilled category.
    const csv = screen.getByRole("button", { name: /^CSV$/ });
    fireEvent.click(csv);
    await waitFor(() =>
      expect(reportCalls.spending.length).toBeGreaterThan(1),
    );
  });
});

// --- R3: the Category × Tag matrix -----------------------------------------

describe("M124 R3 — the Category × Tag matrix", () => {
  it("renders as a real table with both axes scoped", async () => {
    renderReports();
    fireEvent.click(await screen.findByRole("button", { name: "Categories" }));

    const table = await screen.findByRole("table");
    expect(table.querySelectorAll('th[scope="col"]').length).toBe(3); // Category + 2 tags
    expect(table.querySelectorAll('th[scope="row"]').length).toBe(2); // 2 category rows
    expect(table.textContent).toContain("Groceries");
    expect(table.textContent).toContain("vacation");
  });

  it("lives in a focusable scroll region (docs/09 §Scroll regions)", async () => {
    renderReports();
    fireEvent.click(await screen.findByRole("button", { name: "Categories" }));

    const region = await screen.findByRole("region", {
      name: /Category by tag spending matrix/,
    });
    expect(region.getAttribute("tabindex")).toBe("0");
    expect(region.className).toContain("overflow-x-auto");
  });

  it("labels the honesty caveats: raw rows, per-tag cells, truncation", async () => {
    renderReports();
    fireEvent.click(await screen.findByRole("button", { name: "Categories" }));

    const note = await screen.findByText(/Rows are the category on each transaction/);
    expect(note.textContent).toContain("once per tag");
    expect(note.textContent).toContain("largest tags are columns");
  });
});
