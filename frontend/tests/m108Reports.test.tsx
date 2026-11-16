// M108 — the Reports page's two new affordances (docs/09 §Saved report views,
// docs/12 §Chart mapping):
//
//   P1  the Spending tab's chart-view toggle: Bars (today's stacked chart) vs
//       Treemap (one block per category), same rows, same palette, drill-in
//       through the SAME deep link the app already uses for a category.
//   P2  "Save this view" / "Saved views": a preset stores the tab plus the RAW
//       filter state and is restored onto the page by a click.
//
// The assertions that matter are the behavioural ones: the treemap's share is
// an 8-decimal raw value TRIMMED to 2 on screen, the saved state does NOT
// carry derived UI (the preset chips), and restoring really moves the page.

import type { ReactElement } from "react";
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

// --- fixtures ---------------------------------------------------------------

/**
 * "Today" in the RUNTIME's zone.
 *
 * The app's YTD range end is a LOCAL date (`rangeFor("YTD")`), so a test that
 * hardcodes the calendar dies at UTC midnight — CI runs in UTC, and the first
 * run of a day then disagrees with a fixture written the day before. Derive it
 * the same way the app does; `toISOString()` is UTC-based and would break the
 * local (EDT) run instead.
 */
const _now = new Date();
const TODAY = `${_now.getFullYear()}-${String(_now.getMonth() + 1).padStart(2, "0")}-${String(_now.getDate()).padStart(2, "0")}`;
const YEAR = _now.getFullYear();
/** YTD's start: Jan 1 of the runtime's year. */
const YTD_FROM = `${YEAR}-01-01`;

const SPENDING = {
  from: "2026-01-01",
  to: "2026-10-06",
  granularity: "month" as const,
  group_by: "category" as const,
  items: [
    { bucket: "2026-01-01", category_id: 1, category_name: "Groceries", total_cad: "600.00" },
    { bucket: "2026-01-01", category_id: 2, category_name: "Transit", total_cad: "300.00" },
    { bucket: "2026-02-01", category_id: 1, category_name: "Groceries", total_cad: "400.00" },
  ],
  total_cad: "1300.00",
  // With one tag per transaction the two totals agree; the honesty caption
  // is pinned by its own test in m124TagReports.test.tsx.
  spend_total_cad: "1300.00",
};

const auth = vi.hoisted(() => ({
  user: null as Record<string, unknown> | null,
  setUser: vi.fn(),
}));

// M159: what the Owner lens' server call answers — the HOUSEHOLD population
// (`scope=household`), i.e. the same two report definitions over own +
// shared rows. Deliberately NOT the narrow SPENDING stub above, so a test
// that renders the lens can only pass if the page asked for the wider scope.
const HOUSEHOLD_SPENDING = {
  ...SPENDING,
  items: [{ bucket: "2026-01-01", category_id: 1, category_name: "Groceries", total_cad: "700.00" }],
  total_cad: "700.00",
  spend_total_cad: "700.00",
};

const HOUSEHOLD_BREAKDOWN = {
  group_by: "category" as const,
  items: [{ category_id: 1, name: "Groceries", cad_total: "700.00" }],
  total_cad: "700.00",
  spend_total_cad: "700.00",
};

const usersApi = vi.hoisted(() => ({ updateMe: vi.fn() }));

/** M124 R3 — the narrow (own-rows) Category × Tag matrix. */
const MATRIX = {
  from: "2026-01-01",
  to: "2026-10-06",
  columns: [{ tag_id: null, tag_name: "Untagged" }],
  rows: [{ category_id: 1, category_name: "Groceries" }],
  items: [
    {
      category_id: 1,
      category_name: "Groceries",
      tag_id: null,
      tag_name: "Untagged",
      cad_total: "1000.00",
    },
  ],
  total_cad: "1300.00",
};

/** M179 — the SAME grid over the household population: a different total
 *  (700.00), so a test can only pass if the lens carried the scope. */
const HOUSEHOLD_MATRIX = {
  ...MATRIX,
  items: [
    {
      category_id: 1,
      category_name: "Groceries",
      tag_id: null,
      tag_name: "Untagged",
      cad_total: "700.00",
    },
  ],
  total_cad: "700.00",
};

/** M108 P3: the partnership probe (M92). */
const sharingApi = vi.hoisted(() => ({
  getPartnerState: vi.fn(),
}));

/** M159: the Owner lens' two server calls, hoisted so a test can count the
 *  ones that actually asked for `scope=household`. */
const reportsApi = vi.hoisted(() => ({
  getSpending: vi.fn(),
  getSpendingCategories: vi.fn(),
}));

/** M179: the Category × Tag matrix's own query. The matrix is the Categories
 *  tab's second panel over the SAME population as the donut above it, so the
 *  Owner lens has to drive it too — hoisted for the same reason. */
const matrixApi = vi.hoisted(() => ({ getCategoryTagMatrix: vi.fn() }));

/** Every `getSpending`/`getSpendingCategories`/`getCategoryTagMatrix` call
 *  issued with the household scope — the ONLY calls the Owner lens is allowed
 *  to make. */
function householdCalls(): unknown[][] {
  return [
    ...reportsApi.getSpending.mock.calls.filter((call) => call[5] === "household"),
    ...reportsApi.getSpendingCategories.mock.calls.filter((call) => call[3] === "household"),
    ...matrixApi.getCategoryTagMatrix.mock.calls.filter((call) => call[2] === "household"),
  ];
}

const pending = vi.hoisted(() => <T,>(): Promise<T> => new Promise<never>(() => {}));

// STATEFUL, not a constant stub: after a rename/delete Reports calls
// `setUser(updated)` and re-renders the list from the new object (the real
// AuthProvider does exactly that). A bare `() => ({user: auth.user})` never
// re-renders, so the test would assert against a list that production has
// already refreshed — and would look for the OLD title after a rename.
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

// Real `GET /partner/state` would run in every test; the default is the
// lone-tenant answer, and the P3 cases override it with `{linked:true}`.
vi.mock("../src/api/sharing", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/sharing")>();
  return {
    ...actual,
    getPartnerState: sharingApi.getPartnerState,
  };
});

vi.mock("../src/api/reports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/reports")>();
  return {
    ...actual,
    // M159: the Owner lens asks for the HOUSEHOLD scope on the SAME two
    // routes (docs/12 §Owner dimension) — one call, server-computed. Each
    // stub ANSWERS TO ITS `scope` argument (see `beforeEach`), so the lens
    // gets the household population and everything else keeps the narrow one.
    // The client engines (`getSpendingOver` / `getSpendingCategoriesOver`)
    // stay in the real module, un-called — M160 owns their retirement.
    getSpending: reportsApi.getSpending,
    getSpendingCategories: reportsApi.getSpendingCategories,
    // M124 R2: the drill is its OWN query over the same rows, so it resolves
    // a tick after the click — the tests await it rather than asserting on the
    // skeleton in between. M179: the matrix is hoisted too, because the Owner
    // lens drives it and a test has to see WHICH scope it asked for.
    getCategoryTagMatrix: matrixApi.getCategoryTagMatrix,
    getCashflow: vi.fn(pending),
    getNetWorth: vi.fn(pending),
    getInvestmentPerformance: vi.fn(pending),
    getDividendReport: vi.fn(pending),
  };
});

beforeEach(() => {
  // The suite asserts CALL COUNTS on `updateMe`; a shared spy would carry the
  // previous test's calls into the next one.
  usersApi.updateMe.mockReset();
  sharingApi.getPartnerState.mockReset();
  sharingApi.getPartnerState.mockResolvedValue({ linked: false });
  // M159: scope-aware report stubs — `scope=household` answers with the
  // household population, every other call with the narrow one.
  reportsApi.getSpending.mockReset();
  reportsApi.getSpending.mockImplementation(
    async (
      _from: string,
      _to: string,
      _granularity: string,
      _groupBy?: string,
      _drill?: unknown,
      scope?: string,
    ) => (scope === "household" ? HOUSEHOLD_SPENDING : SPENDING),
  );
  reportsApi.getSpendingCategories.mockReset();
  reportsApi.getSpendingCategories.mockImplementation(
    async (_from: string, _to: string, _groupBy?: string, scope?: string) =>
      scope === "household" ? HOUSEHOLD_BREAKDOWN : { items: [], total_cad: "0.00" },
  );
  // M179: the matrix answers to its `scope` argument the same way, so the
  // Owner lens can only render the household grid if it ASKED for it.
  matrixApi.getCategoryTagMatrix.mockReset();
  matrixApi.getCategoryTagMatrix.mockImplementation(
    async (_from: string, _to: string, scope?: string) =>
      scope === "household" ? HOUSEHOLD_MATRIX : MATRIX,
  );
  // The server echoes back what it STORED (docs/08: PATCH → 200 UserPublic),
  // and Reports re-renders from that object — so the stub has to too, or the
  // list under test would still show the pre-rename titles.
  usersApi.updateMe.mockImplementation(async (patch: { saved_report_views?: unknown[] }) =>
    baseUser((patch.saved_report_views ?? []) as never[]),
  );
  auth.user = null;
});

function baseUser(saved: unknown[] = []): Record<string, unknown> {
  return {
    id: "u1",
    email: "qa@example.com",
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "ollama",
    llm_base_url: null,
    llm_model: "",
    encryption_required: false,
    dashboard_preset: "overview",
    saved_report_views: saved,
  };
}

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

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function locationText(): string {
  return screen.getByTestId("location").textContent ?? "";
}

// --- P1: treemap ------------------------------------------------------------

/** Renders the page and waits for the Spending chart to finish computing. */
async function renderSpending(): Promise<void> {
  renderReports();
  // The report is computed in the browser (docs/33 S2a), so the first paint
  // is a skeleton — every chart assertion has to wait for the real frame.
  await screen.findByRole("img", { name: /Spending by category/ });
}

/** Switch to the treemap variant and wait for its legend. */
async function showTreemap(): Promise<void> {
  await renderSpending();
  fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));
  await screen.findAllByRole("button", { name: /Show these transactions\.$/ });
}

describe("M108 P1 — Spending tab chart view (docs/12 §Chart mapping)", () => {
  it("defaults to the stacked bars and offers Treemap beside them", async () => {
    auth.user = baseUser();
    await renderSpending();
    expect(screen.queryByRole("img", { name: /treemap/ })).toBeNull();
    expect(screen.getByRole("tab", { name: "Bars" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Treemap" })).toBeTruthy();
  });

  it("renders the treemap as ONE text equivalent over aria-hidden geometry", async () => {
    auth.user = baseUser();
    await showTreemap();

    const frame = screen.getByRole("img", { name: /Spending by category — treemap/ });
    // docs/12 §Chart accessibility: the wrapper is the leaf, the geometry is
    // hidden — a screen reader hears the sentence, not a hundred <rect>s.
    const svg = frame.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
    expect(frame.querySelectorAll("rect").length).toBe(2);
  });

  it("shows the share TRIMMED to 2dp while the raw share stays on the control", async () => {
    auth.user = baseUser();
    await showTreemap();

    // 1000 / 1300 = 76.92307692% — the eye gets 76.92, the aria-label keeps
    // every digit (M105's rule applied to a share).
    const groceries = screen.getByRole("button", { name: /^Groceries —/ });
    expect(groceries.getAttribute("aria-label")).toContain("76.92307692%");
    expect(groceries.getAttribute("aria-label")).toContain("(shown as 76.92%)");
    // The visible text is the trimmed reading, not the raw one.
    expect(groceries.textContent).toContain("76.92%");
    expect(groceries.textContent).not.toContain("76.92307692");
  });

  it("a block/legend press drills into that category's PERIOD breakdown (M124 R2)", async () => {
    auth.user = baseUser();
    await showTreemap();

    // M124 R2 replaced the ledger navigation with an in-page drill: the same
    // click now shows that one category's periods, with a way back.
    fireEvent.click(screen.getByRole("button", { name: /^Transit —/ }));

    const breadcrumb = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(breadcrumb.textContent).toContain("Transit");
    expect(screen.getByRole("button", { name: /All categories/ })).toBeTruthy();
    // The chart is the M116 history-chart family with a table fallback.
    expect(screen.getByText("Show the numbers behind this chart")).toBeTruthy();
    // Still on this page — no navigation happened.
    expect(locationText()).toBe("/reports");
  });

  it("keeps the ledger link reachable from inside the drill (M124 R2)", async () => {
    auth.user = baseUser();
    await showTreemap();

    fireEvent.click(screen.getByRole("button", { name: /^Transit —/ }));

    // The treemap used to navigate here directly; the drill must not be a
    // dead end for someone who wanted the raw transactions.
    const link = await screen.findByRole("link", { name: /View these transactions/ });
    expect(link.getAttribute("href")).toBe("/transactions?category=2");
  });

  it("the drill back control returns to the all-categories view", async () => {
    auth.user = baseUser();
    await showTreemap();

    fireEvent.click(screen.getByRole("button", { name: /^Transit —/ }));
    const back = await screen.findByRole("button", { name: /All categories/ });
    fireEvent.click(back);

    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Groceries —/ })).toBeTruthy();
  });

  it("offers exactly the categories that spent — none invented, none dropped", async () => {
    auth.user = baseUser();
    await showTreemap();
    // The fixture has two categories (Groceries appears in two buckets and
    // re-aggregates to one block). A third button would mean a zero-fill row
    // (docs/12) became a block, or a real category was silently dropped.
    expect(screen.getAllByRole("button", { name: /Show these transactions\.$/ })).toHaveLength(2);
  });

  it("the legend is keyboard reachable (≥44px targets, real buttons)", async () => {
    auth.user = baseUser();
    await showTreemap();
    const buttons = screen.getAllByRole("button", { name: /Show these transactions\.$/ });
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) {
      // jsdom has no layout, so pin the class contract that produces the
      // ≥44px target docs/09 requires (same method as the m82 strip test).
      expect(button.className).toContain("min-h-11");
      expect(button.className).toContain("focus-visible:ring-2");
    }
  });
});

// --- P2: saved views --------------------------------------------------------

describe("M108 P2 — saved report views (docs/09 §Saved report views)", () => {
  it("saves the tab's RAW filter state and nothing derived", async () => {
    auth.user = baseUser();
    renderReports();

    fireEvent.click(screen.getByRole("button", { name: "Save this view" }));
    const title = screen.getByLabelText("Name this view");
    fireEvent.change(title, { target: { value: "YTD groceries" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(usersApi.updateMe).toHaveBeenCalledTimes(1));
    const payload = usersApi.updateMe.mock.calls[0][0] as {
      saved_report_views: Array<{ title: string; tab: string; state: Record<string, unknown> }>;
    };
    const [saved] = payload.saved_report_views;
    expect(saved.title).toBe("YTD groceries");
    expect(saved.tab).toBe("spending");
    // The RANGE is stored verbatim; the preset CHIPS (YTD/1M/…) are derived
    // from it and must NOT be persisted, or the two could drift.
    expect(saved.state.from).toBe(YTD_FROM);
    expect(saved.state.to).toBe(TODAY);
    expect(saved.state.granularity).toBe("month");
    expect(saved.state.view).toBe("bars");
    // M124: group_by is stored for a lone tenant too, because Tag is now a
    // real option on this tab (it needs no partnership). Before M124 the
    // Owner option was gated on a link, so a lone tenant's preset carried no
    // group-by at all — there was no dimension left to record.
    expect(saved.state.group_by).toBe("category");
    expect(Object.keys(saved.state).sort()).toEqual([
      "from",
      "granularity",
      "group_by",
      "to",
      "view",
    ]);
  });

  it("saving the treemap variant stores THAT variant", async () => {
    auth.user = baseUser();
    renderReports();
    fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));

    fireEvent.click(screen.getByRole("button", { name: "Save this view" }));
    fireEvent.change(screen.getByLabelText("Name this view"), {
      target: { value: "Treemap YTD" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(usersApi.updateMe).toHaveBeenCalledTimes(1));
    const payload = usersApi.updateMe.mock.calls[0][0] as {
      saved_report_views: Array<{ state: Record<string, unknown> }>;
    };
    expect(payload.saved_report_views[0].state.view).toBe("treemap");
  });

  it("lists only THIS tab's presets and restores one onto the page", () => {
    auth.user = baseUser([
      {
        id: "v-11111111",
        title: "Weekly cash flow",
        tab: "cashflow",
        state: { from: "2026-02-01", to: "2026-03-01", granularity: "week" },
      },
      {
        id: "v-22222222",
        title: "Spending YTD",
        tab: "spending",
        state: { from: "2026-01-01", to: "2026-10-06", granularity: "month", view: "treemap" },
      },
    ]);
    renderReports();

    fireEvent.click(screen.getByRole("button", { name: "Saved views (1)" }));
    // Only the spending preset is offered on the Spending tab.
    // Anchored: the Rename/Delete controls name the same preset and must not
    // collide with the restore button (the accessible name is title + summary).
    expect(screen.getByRole("button", { name: /^Spending YTD/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Weekly cash flow/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Spending YTD/ }));
    expect(locationText()).toBe("/reports?tab=spending");
    // Restoring the preset also restored its chart variant.
    expect(screen.getByRole("tab", { name: "Treemap" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("From").getAttribute("value")).toBe("2026-01-01");
    expect(screen.getByLabelText("To").getAttribute("value")).toBe("2026-10-06");
  });

  it("restoring a preset on ANOTHER tab moves the page to it", () => {
    auth.user = baseUser([
      {
        id: "v-33333333",
        title: "Weekly cash flow",
        tab: "cashflow",
        state: { from: "2026-02-01", to: "2026-03-01", granularity: "week" },
      },
    ]);
    renderReports();

    // The Spending tab has none of its own, so the list says so honestly
    // instead of showing another tab's presets as if they applied here.
    fireEvent.click(screen.getByRole("button", { name: "Saved views" }));
    expect(screen.getByText(/No saved views on this tab yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Saved views" }));

    fireEvent.click(screen.getByRole("button", { name: "Cash flow" }));
    fireEvent.click(screen.getByRole("button", { name: "Saved views (1)" }));
    fireEvent.click(screen.getByRole("button", { name: /^Weekly cash flow/ }));

    expect(locationText()).toBe("/reports?tab=cashflow");
    expect(screen.getByLabelText("From").getAttribute("value")).toBe("2026-02-01");
    expect(screen.getByRole("tab", { name: "week" }).getAttribute("aria-selected")).toBe("true");
  });

  it("rename and delete are whole-list writes", async () => {
    auth.user = baseUser([
      {
        id: "v-44444444",
        title: "Old name",
        tab: "spending",
        state: { from: "2026-01-01", to: "2026-10-06", granularity: "month", view: "bars" },
      },
    ]);
    renderReports();

    fireEvent.click(screen.getByRole("button", { name: "Saved views (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Rename saved view Old name" }));
    const field = screen.getByLabelText("New name for Old name");
    fireEvent.change(field, { target: { value: "New name" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(usersApi.updateMe).toHaveBeenCalledTimes(1));
    const payload = usersApi.updateMe.mock.calls[0][0] as {
      saved_report_views: Array<{ id: string; title: string }>;
    };
    expect(payload.saved_report_views).toHaveLength(1);
    expect(payload.saved_report_views[0]).toMatchObject({ id: "v-44444444", title: "New name" });

    fireEvent.click(screen.getByRole("button", { name: "Delete saved view New name" }));
    await waitFor(() => expect(usersApi.updateMe).toHaveBeenCalledTimes(2));
    const second = usersApi.updateMe.mock.calls[1][0] as { saved_report_views: unknown[] };
    expect(second.saved_report_views).toEqual([]);
  });

  it("the control is inert — not merely hidden — while the range is invalid", () => {
    auth.user = baseUser();
    renderReports("/reports?tab=spending");
    const save = screen.getByRole("button", { name: "Save this view" }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);

    const from = screen.getByLabelText("From") as HTMLInputElement;
    // Year-relative and NOT today-anchored: `from > to` is the only property
    // this test needs, and Jan 2 > Jan 1 holds on every date of every year.
    fireEvent.change(from, { target: { value: `${YEAR}-01-02` } });
    const to = screen.getByLabelText("To") as HTMLInputElement;
    fireEvent.change(to, { target: { value: `${YEAR}-01-01` } });
    expect((screen.getByRole("button", { name: "Save this view" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});

// --- P3: the Owner dimension -------------------------------------------------

describe("M108 P3 — Reports Owner dimension (docs/12 §Owner dimension)", () => {
  const linkedState = {
    linked: true as const,
    owner: { user_id: 9, name: "Princess", email: "owner@example.com" },
    account_ids: [7],
    privacy_mode: false,
    link_id: 3,
    accepted_at: "2026-10-01T00:00:00Z",
  };

  it("a lone tenant sees NO Owner option and never asks for the household scope", async () => {
    auth.user = baseUser();
    await renderSpending();

    expect(screen.queryByRole("tab", { name: "Owner" })).toBeNull();
    // Zero UI and zero queries: the dimension cannot exist without a partner,
    // so no `scope=household` request is ever issued (M159 — the scope is the
    // only thing that can widen a population, and it is never sent blind).
    expect(householdCalls()).toHaveLength(0);
  });

  it("a linked partner gets the option on the transaction-backed tabs only", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();

    expect(await screen.findByRole("tab", { name: "Owner" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Category" }).getAttribute("aria-selected")).toBe("true");

    // Cash flow is NOT one of them: its population mixes trades the M93
    // projection does not deliver (docs/43 §6 has no /partner/trades), so an
    // owner split there would print a Partner row with zero dividend income.
    fireEvent.click(screen.getByRole("button", { name: "Cash flow" }));
    expect(screen.queryByRole("tab", { name: "Owner" })).toBeNull();
  });

  it("does not ask for the household scope until the Owner lens is chosen", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();
    await screen.findByRole("tab", { name: "Owner" });

    expect(householdCalls()).toHaveLength(0);

    fireEvent.click(screen.getByRole("tab", { name: "Owner" }));
    await waitFor(() => expect(householdCalls()).toHaveLength(1));
  });

  it("renders the SERVER-computed household population and labels it honestly", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();
    await screen.findByRole("tab", { name: "Owner" });

    fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));
    fireEvent.click(screen.getByRole("tab", { name: "Owner" }));

    // The lens' chart is the household report the SERVER computed — the page
    // asked for `scope=household` and rendered what came back (M159), and
    // the fixture's 700.00 can only appear here if that scope was requested.
    expect(await screen.findByRole("img", { name: /own \+ shared household/ })).toBeTruthy();
    expect(householdCalls()).toHaveLength(1);
    // The number says which rows it is made of (docs/12 §Owner dimension).
    expect(screen.getByText(/Range total \(own \+ shared household\)/)).toBeTruthy();
    expect(screen.getByText(/never a third account/)).toBeTruthy();
    // $700.00 renders in BOTH the block title and the caption — pin the pair
    // with an exact length rather than a one-match query that races.
    expect(screen.getAllByText(/\$700\.00/).length).toBeGreaterThanOrEqual(2);
  });

  it("the Owner lens DOES drill — into the household population, not the caller's own ledger", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();
    await screen.findByRole("tab", { name: "Owner" });

    fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));
    fireEvent.click(screen.getByRole("tab", { name: "Owner" }));
    await screen.findByRole("img", { name: /own \+ shared household/ });
    expect(householdCalls()).toHaveLength(1);

    // M179: pre-M179 the household chart offered no drill at all, because the
    // only target the page had (`/transactions?category=`) describes the
    // caller's OWN rows. The drill now asks the SERVER for the same household
    // scope, so the block is a real control again — it opens the household's
    // periods for that category.
    const block = screen.getAllByRole("button", { name: /Show these transactions\.$/ })[0];
    fireEvent.click(block);

    await waitFor(() => expect(householdCalls()).toHaveLength(2));
    const drillCall = reportsApi.getSpending.mock.calls.find((call) => call[4] !== undefined);
    expect(drillCall?.[5]).toBe("household");
    expect((drillCall?.[4] as { categoryId: number | null }).categoryId).toBe(1);
    // …and the view says which population it is, instead of offering the
    // own-rows ledger link (which would open a different population).
    expect(await screen.findByText(/All categories/)).toBeTruthy();
    expect(screen.getByText(/across the household/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /View these transactions/ })).toBeNull();
  });

  it("keeps the own-rows ledger link on the narrow lens (the withholding is household-only)", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();
    // The treemap is the variant with per-slice controls (the stacked bars
    // drill through a Bar onClick, which has no accessible name of its own).
    fireEvent.click(screen.getByRole("tab", { name: "Treemap" }));
    const slices = await screen.findAllByRole("button", { name: /Show these transactions\.$/ });
    fireEvent.click(slices[0]);

    // Category grouping drills through `/transactions?category=`, which only
    // describes the caller's OWN rows — correct on the narrow lens, and still
    // offered there.
    expect(await screen.findByRole("link", { name: /View these transactions/ })).toBeTruthy();
    expect(screen.queryByText(/across the household/)).toBeNull();
  });

  it("drops back to Category instead of parking on a skeleton when the link is gone", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    // The partnership died mid-session: the household scope can no longer
    // resolve the owner's rows, so the route answers 401 sharing_not_linked
    // for the HOUSEHOLD call only — the narrow one still answers, which is
    // what the page falls back to.
    reportsApi.getSpending.mockImplementation(
      async (
        _from: string,
        _to: string,
        _granularity: string,
        _groupBy?: string,
        _drill?: unknown,
        scope?: string,
      ) => {
        if (scope === "household") {
          throw Object.assign(new Error("No shared household is linked to this account"), {
            status: 401,
            code: "sharing_not_linked",
          });
        }
        return SPENDING;
      },
    );
    await renderSpending();
    await screen.findByRole("tab", { name: "Owner" });

    fireEvent.click(screen.getByRole("tab", { name: "Owner" }));
    await waitFor(() =>
      expect(screen.getByRole("img", { name: /Spending by category/ })).toBeTruthy(),
    );
    expect(screen.queryByRole("img", { name: /own \+ shared household/ })).toBeNull();
  });

  it("the Categories tab carries the same dimension — donut AND its matrix", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    renderReports();
    fireEvent.click(await screen.findByRole("button", { name: "Categories" }));
    fireEvent.click(await screen.findByRole("tab", { name: "Owner" }));

    // The donut renders through Recharts' ResponsiveContainer, which cannot
    // measure a jsdom DOM — so the LEGEND never mounts here. What does mount
    // is the wrapper's one sentence (docs/12 §Chart accessibility), and it
    // carries the household slice: "… 1 point. Groceries 100%; total $700.00".
    // M179: that wrapper is a `role="group"` now, not `role="img"` — the Donut
    // flips its role by interactivity (a focusable drill control inside
    // `role="img"` is axe's nested-interactive violation), and the household
    // donut IS drillable since M179. The role change is the FEATURE.
    const frame = await screen.findByRole("group", { name: /own \+ shared household/ });
    expect(frame.getAttribute("aria-label")).toContain("Groceries 100%");

    // M179: the Category × Tag matrix is this tab's SECOND panel, drawn from
    // the same population — it used to vanish under the lens (the Owner branch
    // returned before it) and, before M159, would have been own-rows only.
    // Both the donut and the matrix now ask for `scope=household`.
    const matrix = screen.getByRole("table");
    expect(matrix).toBeTruthy();
    // BOTH panels name the population they summed — the donut's caption and
    // M179's matrix caption, hence two matches (a one-match query here would
    // race the render order).
    expect(
      screen.getAllByText(/Own rows \+ the owner’s allowlisted accounts/).length,
    ).toBe(2);
    expect(householdCalls()).toHaveLength(2);
    // The household grid's 700.00 — not the narrow fixture's 1000.00/1300.00
    // (scoped to the table: the donut's aria-label carries the total too).
    expect(matrix.textContent).toContain("700.00");
    expect(matrix.textContent).not.toContain("1,000.00");
    expect(screen.queryByText(/1,300\.00/)).toBeNull();
  });

  it("a saved preset carries the group-by with the filter", async () => {
    auth.user = baseUser();
    sharingApi.getPartnerState.mockResolvedValue(linkedState);
    await renderSpending();
    await screen.findByRole("tab", { name: "Owner" });
    fireEvent.click(screen.getByRole("tab", { name: "Owner" }));

    fireEvent.click(screen.getByRole("button", { name: "Save this view" }));
    fireEvent.change(screen.getByLabelText("Name this view"), {
      target: { value: "Household YTD" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(usersApi.updateMe).toHaveBeenCalledTimes(1));
    const payload = usersApi.updateMe.mock.calls[0][0] as {
      saved_report_views: Array<{ tab: string; state: Record<string, unknown> }>;
    };
    expect(payload.saved_report_views[0].tab).toBe("spending");
    expect(payload.saved_report_views[0].state.group_by).toBe("owner");
  });
});
