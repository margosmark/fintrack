// M97 E2 — dashboard layout presets (docs/09 §Dashboard layout presets).
//
// Three FIXED orders over the SAME six cards. This file pins the promises the
// card makes, none of which are "there is an editor":
//
//   1. a preset only REORDERS — all six cards are present and reachable in
//      every one of them (nothing created, removed or hidden);
//   2. Overview is the first-visit default and is today's order byte-for-byte,
//      because the G1 tour teaches on it;
//   3. the choice is server-owned: it round-trips through PATCH /users/me, so
//      a fresh mount — i.e. a reload — comes back in the chosen order;
//   4. at 390 the DOM order IS the visual order (no CSS `order`, single
//      column), so a screen reader's reading order and the drawn order cannot
//      drift apart between presets;
//   5. axe-core stays at zero with a non-default preset applied.
//
// The rejected scope (docs/10 §M97) is deliberate: drag-and-drop editor,
// widget hiding and custom card sets are NOT built, so no test pretends they
// exist.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CARD_ORDER, Dashboard, type CardKey } from "../src/pages/Dashboard";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import type { DashboardPreset } from "../src/types";

// --- jsdom shims -----------------------------------------------------------
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

// --- module mocks ----------------------------------------------------------
const auth = vi.hoisted(() => ({
  user: {
    id: "user-1",
    email: "demo@fintracker.local",
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "ollama",
    llm_base_url: null,
    llm_model: null,
    encryption_required: false,
    dashboard_preset: "overview",
  } as Record<string, unknown>,
  setUser: vi.fn(),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: auth.user, setUser: auth.setUser }),
}));
vi.mock("../src/api/users", () => ({ updateMe: vi.fn() }));
vi.mock("../src/api/reports", () => ({
  getNetWorth: vi.fn(),
  getCashflow: vi.fn(),
  getSpendingCategories: vi.fn(),
}));
vi.mock("../src/api/transactions", () => ({
  listTransactions: vi.fn(),
  // M138: the subscriptions card reads the strict detector, not the M27 shim.
  getRecurringCandidates: vi.fn(),
  suppressRecurringGroup: vi.fn(),
}));
vi.mock("../src/api/notifications", () => ({
  INBOX_QUERY_KEY: ["notifications", "inbox"],
  criticalBudgetItems: vi.fn(() => []),
  getNotificationInbox: vi.fn(() => Promise.resolve({ items: [] })),
  markNotificationRead: vi.fn(),
}));
vi.mock("../src/api/budgets", () => ({
  listBudgets: vi.fn(),
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
}));
vi.mock("../src/api/insights", () => ({ getSpendingInsights: vi.fn() }));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import {
  getNetWorth,
  getCashflow,
  getSpendingCategories,
} from "../src/api/reports";
import { getRecurringCandidates, listTransactions } from "../src/api/transactions";
import { listBudgets } from "../src/api/budgets";
import { getSpendingInsights } from "../src/api/insights";
import { updateMe } from "../src/api/users";

/** One marker per card: text that exists in EVERY preset (including the
 *  empty-state copy, which is itself a card being reachable). */
const MARKERS: Array<[CardKey, string]> = [
  ["kpi", "Positions value"],
  ["insights", "Spending insights"],
  ["budgets", "No budgets set"],
  ["subscriptions", "No bill suggestions right now"],
  ["charts", "Spending by category (CAD)"],
  ["recent", "Recent transactions"],
];

const BUCKETS = [
  {
    bucket_end: "2026-01-31",
    cash_cad: "1000.00",
    positions_cad: "400.00",
    net_cad: "1400.00",
    assets_cad: "1500.00",
    liabilities_cad: "100.00",
  },
  {
    bucket_end: "2026-02-28",
    cash_cad: "1200.00",
    positions_cad: "400.00",
    net_cad: "1600.00",
    assets_cad: "1700.00",
    liabilities_cad: "100.00",
  },
];

function mockData(): void {
  // The PATCH answers with the updated user, exactly as /users/me does — the
  // component then pushes it into auth state (setUser) like the real context.
  vi.mocked(updateMe).mockImplementation(async (patch) => {
    auth.user = { ...auth.user, ...patch };
    return auth.user as never;
  });
  vi.mocked(getNetWorth).mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: BUCKETS,
  } as never);
  vi.mocked(getCashflow).mockResolvedValue({
    from: "2026-01-01",
    to: "2026-03-31",
    granularity: "month",
    buckets: [{ bucket_end: "2026-02-28", net_cad: "250.00" }],
  } as never);
  vi.mocked(getSpendingCategories).mockResolvedValue({
    items: [],
    total_cad: "0.00",
  } as never);
  vi.mocked(listTransactions).mockResolvedValue({
    items: [],
    total: 0,
    limit: 10,
    offset: 0,
  } as never);
  vi.mocked(listBudgets).mockResolvedValue([] as never);
  vi.mocked(getRecurringCandidates).mockResolvedValue(
    { groups: [], combined_monthly_total: "0.00", suppressed_count: 0, window_start: "2025-09-01" } as never,
  );
  vi.mocked(getSpendingInsights).mockResolvedValue({
    current_month_spending: "120.00",
    previous_month_spending: "300.00",
    month_over_month_pct: -60,
    daily_burn_rate: "6.00",
    projected_month_end: "180.00",
    days_elapsed: 20,
    days_in_month: 30,
    category_deltas: [],
  } as never);
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <Dashboard />
        </QueryClientProvider>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

/** Document order of the six cards, as rendered. */
function cardOrder(): CardKey[] {
  const found = MARKERS.map(([key, text]) => {
    const el = screen.getByText(text);
    return { key, el };
  });
  found.sort((a, b) =>
    a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
  );
  return found.map((f) => f.key);
}

/** Every card has resolved (each block owns its own query, so they land in
 *  different ticks). */
async function settled(): Promise<void> {
  for (const [, text] of MARKERS) {
    await screen.findByText(text);
  }
}

function pick(preset: DashboardPreset): void {
  const label = { overview: "Overview", import_first: "Import first", budget_first: "Budget first" }[
    preset
  ];
  fireEvent.click(screen.getByRole("tab", { name: label }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  auth.user.dashboard_preset = "overview";
});

describe("M97 dashboard layout presets", () => {
  it("first visit is Overview: today's order, all six cards", async () => {
    mockData();
    renderDashboard();
    await settled();
    expect(cardOrder()).toEqual(CARD_ORDER.overview);
    const tablist = screen.getByRole("tablist", { name: "Dashboard layout" });
    // The three choices are the whole feature surface — no editor, no drag.
    // (Scoped to THIS tablist: the RangePicker owns one too.)
    expect([...tablist.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual([
      "Overview",
      "Import first",
      "Budget first",
    ]);
  });

  it("an unknown stored value falls back to Overview (a future row cannot blank the page)", async () => {
    auth.user.dashboard_preset = "not-a-preset";
    mockData();
    renderDashboard();
    await settled();
    expect(cardOrder()).toEqual(CARD_ORDER.overview);
  });

  it("switching reorders the SAME six cards — none is created, removed or hidden", async () => {
    mockData();
    renderDashboard();
    await settled();

    for (const preset of ["budget_first", "import_first"] as const) {
      pick(preset);
      expect(cardOrder()).toEqual(CARD_ORDER[preset]);
      // Every marker still resolves: the preset reorders, it never drops.
      for (const [, text] of MARKERS) {
        expect(screen.getByText(text), `${preset}: missing ${text}`).toBeTruthy();
      }
      expect(updateMe).toHaveBeenLastCalledWith({ dashboard_preset: preset });
    }
  });

  it("the choice persists across a reload (it lives on the user row)", async () => {
    mockData();
    // PATCH /users/me answers with the updated user — the same object a fresh
    // page load would read back from GET /users/me.
    vi.mocked(updateMe).mockImplementation(async (patch) => {
      auth.user = { ...auth.user, ...patch };
      return auth.user as never;
    });

    renderDashboard();
    await settled();
    pick("budget_first");
    expect(cardOrder()).toEqual(CARD_ORDER.budget_first);
    await vi.waitFor(() => expect(auth.user.dashboard_preset).toBe("budget_first"));
    expect(auth.setUser).toHaveBeenCalled();

    // "Reload": a NEW mount reads the user object, not this tab's state.
    cleanup();
    renderDashboard();
    await settled();
    expect(cardOrder()).toEqual(CARD_ORDER.budget_first);
  });

  it("a refused write rolls the layout back instead of lying about the save", async () => {
    mockData();
    vi.mocked(updateMe).mockRejectedValue(
      Object.assign(new Error("read only"), { code: "demo_account_restricted" }),
    );
    renderDashboard();
    await settled();
    pick("budget_first");
    // The rollback lands when the rejected PATCH is handled, not synchronously.
    await waitFor(() => expect(cardOrder()).toEqual(CARD_ORDER.overview));
  });

  it("390: DOM order is the visual order — no CSS `order`, cards stack single-column", async () => {
    mockData();
    renderDashboard();
    await settled();

    const section = screen.getByText("Dashboard").closest("section") as HTMLElement;
    // No `order-*` utility anywhere: CSS `order` moves pixels without moving
    // the accessibility tree, which would desync reading order per preset.
    expect(section.outerHTML).not.toMatch(/\border(-\d+)?\b/);
    // Below `sm` (640px) every grid is one column: each `grid-cols-*` class a
    // grid carries is breakpoint-prefixed, so the sequence the DOM lists is
    // the sequence a 390px screen draws.
    for (const grid of section.querySelectorAll(".grid")) {
      const columnClasses = [...grid.classList].filter((c) => /(^|:)grid-cols-\d/.test(c));
      const stackedBelowSm = columnClasses.every((c) => /^(sm|md|lg|xl|2xl):grid-cols-/.test(c));
      expect(stackedBelowSm, `${grid.className}: a fixed column count below 640px`).toBe(true);
    }
  });

  it("axe-core: zero violations with a non-default preset applied", async () => {
    mockData();
    const { container } = renderDashboard();
    await settled();
    pick("budget_first");
    expect(cardOrder()).toEqual(CARD_ORDER.budget_first);

    const results = await axe.run(container, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
