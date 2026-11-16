// Dashboard section rhythm + comparison-chip separation (t_6263190b).
//
// Two visual contracts the user's screenshot broke, pinned structurally
// (jsdom has no layout engine, so the CONTRACT is the flex/gap classes that
// produce the spacing — the same class-contract style m82/m100 use):
//
//   1. a ComparisonChip with an accessible sentence keeps its amount and its
//      basis in ONE flex row with a gap — when the visible group was a plain
//      inline span, the chip's `gap-1.5` no longer applied to its children
//      and the text rendered glued ("$2Kvs Nov 1");
//   2. every dashboard card slot carries the same top margin, so KPI →
//      spending insights → budgets never butt together in ANY preset, while
//      the KPI grid keeps its own internal `gap-4` (cohesive strip).
//
// Fixtures are the synthetic read-only payloads from the M97 harness — no
// private data, no network, no writes.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CARD_ORDER, Dashboard, type CardKey } from "../src/pages/Dashboard";
import { ComparisonChip } from "../src/components/ui/ComparisonChip";
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

// --- module mocks (same scaffold as m97DashboardPresets) -------------------
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

import { getNetWorth, getCashflow, getSpendingCategories } from "../src/api/reports";
import { getRecurringCandidates, listTransactions } from "../src/api/transactions";
import { listBudgets } from "../src/api/budgets";
import { getSpendingInsights } from "../src/api/insights";
import { updateMe } from "../src/api/users";

/** One marker per card — the same strings the M97 preset file resolves. */
const MARKERS: Array<[CardKey, string]> = [
  ["kpi", "Positions value"],
  ["insights", "Spending insights"],
  ["budgets", "No budgets set"],
  ["subscriptions", "No bill suggestions right now"],
  ["charts", "Spending by category (CAD)"],
  ["recent", "Recent transactions"],
];

/** Two month buckets → the net-worth/positions chips compare against the PREVIOUS
 *  bucket's payload date. The default range is YTD, whose granularity resolves to
 *  day or week — both format bucket_end 2026-01-31 as "Jan 31". */
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
  // The PATCH answers with the updated user (m97 harness), so the preset
  // picker's save path resolves instead of calling .then on undefined.
  vi.mocked(updateMe).mockImplementation(async (patched) => {
    auth.user = { ...auth.user, ...patched };
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
  vi.mocked(getRecurringCandidates).mockResolvedValue({
    groups: [],
    combined_monthly_total: "0.00",
    suppressed_count: 0,
    window_start: "2025-09-01",
  } as never);
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

/** The `mt-4` slot div that wraps one card, i.e. the element whose PARENT is
 *  the page <section> — climb from the card's own marker text. */
function slotOf(section: HTMLElement, text: string): HTMLElement {
  let el: HTMLElement = screen.getByText(text);
  while (el.parentElement !== section) {
    expect(el.parentElement, `marker "${text}" never reaches the section`).not.toBeNull();
    el = el.parentElement as HTMLElement;
  }
  return el;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  auth.user.dashboard_preset = "overview";
});

describe("ComparisonChip separation (amount vs basis)", () => {
  it("keeps the visible amount and basis in ONE flex row with a gap when an aria sentence rides along", () => {
    render(
      <ComparisonChip
        delta="$2.1K"
        basis="vs Nov 1"
        ariaLabel="Net worth up $2.1K versus Nov 1"
      />,
    );
    const basis = screen.getByText("vs Nov 1");
    const amount = screen.getByText("$2.1K");
    // The visible group is the aria-hidden wrapper around both runs…
    const group = basis.parentElement as HTMLElement;
    expect(group).toBe(amount.closest("[aria-hidden]"));
    // …and it must be a flex ROW carrying a gap, otherwise its children fall
    // back to inline flow and print glued together ("$2.1Kvs Nov 1").
    expect(group.classList.contains("inline-flex")).toBe(true);
    expect([...group.classList].some((c) => /^gap-/.test(c))).toBe(true);
    // The direction arrow rides inside the same group (icon + amount gap-1).
    expect(group.querySelector("svg")).not.toBeNull();
    // The accessible sentence is the full spaced sentence, not the glyphs.
    const spoken = screen.getByText("Net worth up $2.1K versus Nov 1");
    expect(spoken.classList.contains("sr-only")).toBe(true);
  });

  it("without an aria sentence the chip's own gap governs the same pair", () => {
    render(<ComparisonChip delta="$2.1K" basis="vs Nov 1" />);
    const basis = screen.getByText("vs Nov 1");
    const chip = basis.parentElement as HTMLElement;
    expect([...chip.classList].some((c) => /^gap-/.test(c))).toBe(true);
    expect(screen.queryByText(/versus/)).toBeNull();
  });

  it("dashboard KPI chips: every rendered basis sits in a gapped aria-hidden group", async () => {
    mockData();
    renderDashboard();
    await settled();
    // Net worth + Positions value both compare month-over-month on the same
    // two payload buckets ("up" both times: 1400→1600, 400→400).
    const bases = screen.getAllByText("vs Jan 31");
    expect(bases.length).toBeGreaterThanOrEqual(2);
    for (const basis of bases) {
      const group = basis.parentElement as HTMLElement;
      expect(group.getAttribute("aria-hidden")).toBe("true");
      expect([...group.classList].some((c) => /^gap-/.test(c))).toBe(true);
    }
    // Signs/currency/labels preserved: the spoken sentence still carries the
    // formatted CAD amount and the payload-derived basis date.
    expect(screen.getByText("Net worth up $200 versus Jan 31")).toBeTruthy();
  });
});

describe("Dashboard section rhythm", () => {
  it("every card slot carries the same top margin, in CARD_ORDER, for every preset", async () => {
    mockData();
    const { container } = renderDashboard();
    await settled();
    const section = container.querySelector("section") as HTMLElement;

    for (const preset of ["overview", "budget_first", "import_first"] as const) {
      pick(preset);
      // The first card of the new order must resolve before slot-walking it.
      await screen.findByText(MARKERS.find(([k]) => k === CARD_ORDER[preset][0])![1]);
      const slots = CARD_ORDER[preset].map((key) => {
        const marker = MARKERS.find(([k]) => k === key)![1];
        return slotOf(section, marker);
      });
      for (const slot of slots) {
        expect(slot.classList.contains("mt-4"), `${preset}: ${slot.outerHTML}`).toBe(true);
      }
      // Visual order = slot order (single column below sm, no CSS `order`).
      for (let i = 1; i < slots.length; i += 1) {
        const follows =
          slots[i - 1].compareDocumentPosition(slots[i]) & Node.DOCUMENT_POSITION_FOLLOWING;
        expect(follows !== 0, `${preset}: slot ${i} out of order`).toBe(true);
      }
      // Exactly ONE spacing source: the slots themselves (no per-card margin
      // left to stack on top of the slot's).
      expect(section.querySelectorAll(":scope > .mt-4").length).toBe(6);
    }
  });

  it("the KPI strip keeps its cohesive internal grid — gap-4, 2-up from sm, 4-up at xl", async () => {
    mockData();
    const { container } = renderDashboard();
    await settled();
    const section = container.querySelector("section") as HTMLElement;
    const kpiGrid = slotOf(section, "Positions value").firstElementChild as HTMLElement;
    for (const cls of ["grid", "gap-4", "sm:grid-cols-2", "xl:grid-cols-4"]) {
      expect(kpiGrid.classList.contains(cls), `KPI grid missing ${cls}`).toBe(true);
    }
    // Its spacing comes from the slot, not a second margin on the grid.
    expect(kpiGrid.classList.contains("mt-4")).toBe(false);
    // The charts grid keeps its own contract too.
    const chartsGrid = slotOf(section, "Spending by category (CAD)")
      .firstElementChild as HTMLElement;
    expect(chartsGrid.classList.contains("grid")).toBe(true);
    expect(chartsGrid.classList.contains("gap-4")).toBe(true);
    expect(chartsGrid.classList.contains("lg:grid-cols-2")).toBe(true);
    expect(chartsGrid.classList.contains("mt-4")).toBe(false);
  });
});
