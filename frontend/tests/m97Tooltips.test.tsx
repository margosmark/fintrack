// M97 E1 — the honest "why is this number what it is" tooltips on the real
// aggregates (docs/09 §Tooltips & in-app help).
//
// The primitive's own contract lives in `helpHint.test.ts`; this file proves
// that each SURFACE actually carries its derivation and that the copy says
// what the code does:
//
//   - Net worth  → assets − liabilities with both numbers inline (docs/12 §4
//     split, `assets_cad`/`liabilities_cad`), as-of = min(bucket end, range
//     end) so a mid-month tooltip never claims a future date;
//   - pct_used   → the M65 true ratio ("201.94% prints as 202%, only the bar
//     clamps") on the dashboard cards, the Budgets page and the partner view;
//   - TWR / MWR  → one sentence each, which is shown where, and why the other
//     exists (docs/05 §Total return);
//   - the spending-insight aggregates whose derivation is NOT printed on the
//     card (burn rate, projection, part-month vs full-month comparison).
//
// Deliberately NOT hinted (docs/09 sweep rule: no tooltip where the
// derivation is literally on the same screen): the net-worth trend chart
// draws cash + positions stacked to net, the spending donut prints its own
// total, and every "% used" card prints spent / limit above the bar — the
// hint there exists only for the CLAMP the bar hides.
import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "../src/pages/Dashboard";
import { Budgets } from "../src/pages/Budgets";
import { TotalReturnMetrics } from "../src/components/investments/TotalReturnMetrics";
import type { BudgetWithSpent } from "../src/api/budgets";

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
// M160-R2 (t_85311c34): `getTotalReturn` is back in `api/reports.ts` as a
// thin read of `GET /reports/total-return`, so one mock module covers the
// whole reports surface the dashboard touches.
vi.mock("../src/api/reports", () => ({
  getNetWorth: vi.fn(),
  getCashflow: vi.fn(),
  getSpendingCategories: vi.fn(),
  getTotalReturn: vi.fn(),
}));
vi.mock("../src/api/transactions", () => ({
  listTransactions: vi.fn(),
  getRecurringCandidates: vi.fn(() =>
    Promise.resolve({
      groups: [],
      combined_monthly_total: "0.00",
      suppressed_count: 0,
      window_start: "2025-09-01",
    }),
  ),
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
  // M137 P0-2: the M96 recurring label now reads its own tiny route instead
  // of downloading the ledger, so the Budgets page renders this too.
  getRecurringSummary: vi.fn().mockResolvedValue({ count: 0, monthly: 0 }),
}));
vi.mock("../src/api/categories", () => ({ listCategories: vi.fn() }));
vi.mock("../src/api/insights", () => ({ getSpendingInsights: vi.fn() }));
// The Dashboard reads its M97 layout preset from the auth context (docs/09
// §Dashboard layout presets); this file is about tooltips, not auth.
vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({
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
    },
    setUser: vi.fn(),
  }),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));
// These three are not under test — they carry no M97 hint and their own
// suites cover them; stubbing them keeps this file about the KPI strip.
vi.mock("../src/components/reports/SubscriptionsPanel", () => ({
  SubscriptionsPanel: () => null,
}));

import {
  getCashflow,
  getNetWorth,
  getSpendingCategories,
  getTotalReturn,
} from "../src/api/reports";
import { listTransactions } from "../src/api/transactions";
import { listBudgets } from "../src/api/budgets";
import { listCategories } from "../src/api/categories";
import { getSpendingInsights } from "../src/api/insights";

const mocked = {
  getNetWorth: vi.mocked(getNetWorth),
  getCashflow: vi.mocked(getCashflow),
  getSpendingCategories: vi.mocked(getSpendingCategories),
  getTotalReturn: vi.mocked(getTotalReturn),
  listTransactions: vi.mocked(listTransactions),
  listBudgets: vi.mocked(listBudgets),
  listCategories: vi.mocked(listCategories),
  getSpendingInsights: vi.mocked(getSpendingInsights),
};

function renderPage(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // ThemeProvider: the chart palette reads the theme (NetWorthAreaChart does),
  // and matchMedia is stubbed above for reduced-motion/scheme reads.
  return render(
    createElement(
      ThemeProvider,
      null,
      createElement(
        MemoryRouter,
        null,
        createElement(QueryClientProvider, { client }, node),
      ),
    ),
  );
}

/** The trigger whose own label/hint host starts with `prefix`. */
function hintFor(prefix: string): HTMLButtonElement {
  const all = [...document.querySelectorAll<HTMLButtonElement>(
    'button[aria-label="What does this mean?"]',
  )];
  const hit = all.find((b) => (b.parentElement?.textContent ?? "").trim().startsWith(prefix));
  if (!hit) {
    const owners = all.map((b) => JSON.stringify((b.parentElement?.textContent ?? "").trim()));
    throw new Error(`no hint near ${JSON.stringify(prefix)}; hosts: ${owners.join(", ")}`);
  }
  return hit;
}

function tipOf(btn: HTMLButtonElement): HTMLElement {
  const id = btn.getAttribute("aria-describedby");
  const tip = id === null ? null : document.getElementById(id);
  if (!tip) throw new Error("aria-describedby did not resolve to an element");
  return tip;
}

/** Focus opens it, Escape dismisses it — the same contract for every surface. */
function expectHintRenders(prefix: string, needle: string): void {
  const btn = hintFor(prefix);
  const tip = tipOf(btn);
  expect(tip.classList.contains("sr-only"), `${prefix}: closed before focus`).toBe(true);
  expect(tip.textContent).toContain(needle);
  act(() => {
    btn.focus();
    fireEvent.focus(btn);
  });
  expect(tip.classList.contains("sr-only"), `${prefix}: open after focus`).toBe(false);
  fireEvent.keyDown(btn, { key: "Escape" });
  expect(tip.classList.contains("sr-only"), `${prefix}: closed after Escape`).toBe(true);
}

// --- fixtures --------------------------------------------------------------
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

function budget(id: number, pct_used: number, category_name: string): BudgetWithSpent {
  return {
    id,
    user_id: 1,
    category_id: id,
    amount: "250.00",
    period: "monthly",
    category_name,
    spent: ((250 * pct_used) / 100).toFixed(2),
    pct_used,
    remaining: (250 - (250 * pct_used) / 100).toFixed(2),
  } as BudgetWithSpent;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("M97 tooltips — Dashboard KPI strip", () => {
  it("net worth shows assets − liabilities with BOTH numbers and the exclusions", async () => {
    mocked.getNetWorth.mockReturnValue(
      Promise.resolve({ from: "2026-01-01", to: "2026-03-31", granularity: "month", buckets: BUCKETS }) as never,
    );
    mocked.getCashflow.mockReturnValue(
      Promise.resolve({ from: "2026-01-01", to: "2026-03-31", granularity: "month", buckets: [] }) as never,
    );
    mocked.getSpendingCategories.mockReturnValue(
      Promise.resolve({ items: [], total_cad: "0.00" }) as never,
    );
    mocked.listTransactions.mockReturnValue(
      Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 }) as never,
    );
    mocked.listBudgets.mockReturnValue(Promise.resolve([]) as never);
    mocked.getSpendingInsights.mockReturnValue(
      Promise.resolve({
        current_month_spending: "120.00",
        previous_month_spending: "300.00",
        month_over_month_pct: -60,
        daily_burn_rate: "6.00",
        projected_month_end: "180.00",
        days_elapsed: 20,
        days_in_month: 30,
        category_deltas: [],
      }) as never,
    );

    renderPage(createElement(Dashboard));
    // The hint only exists once the net-worth query lands (no data, no number,
    // no derivation to explain).
    await waitFor(() => expect(screen.queryByText(/Assets − Liabilities/)).not.toBeNull());

    // as-of = the bucket end (2026-02-28 <= today), never the future month end.
    expectHintRenders(
      "Net worth",
      "Assets − Liabilities, as of 2026-02-28: assets $1,700.00 − liabilities $100.00.",
    );
    const netWorthBtn = hintFor("Net worth");
    expect(tipOf(netWorthBtn).textContent).toContain("never fetched");
  });

  it("the other three KPIs explain their own derivation", () => {
    mocked.getNetWorth.mockReturnValue(
      Promise.resolve({ from: "2026-01-01", to: "2026-03-31", granularity: "month", buckets: BUCKETS }) as never,
    );
    mocked.getCashflow.mockReturnValue(
      Promise.resolve({
        from: "2026-01-01",
        to: "2026-03-31",
        granularity: "month",
        buckets: [
          { bucket_end: "2026-02-28", net_cad: "250.00" },
          { bucket_end: "2026-03-31", net_cad: "-40.00" },
        ],
      }) as never,
    );
    mocked.getSpendingCategories.mockReturnValue(Promise.resolve({ items: [], total_cad: "0.00" }) as never);
    mocked.listTransactions.mockReturnValue(Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 }) as never);
    mocked.listBudgets.mockReturnValue(Promise.resolve([]) as never);
    mocked.getSpendingInsights.mockReturnValue(
      Promise.resolve({
        current_month_spending: "120.00",
        previous_month_spending: "300.00",
        month_over_month_pct: -60,
        daily_burn_rate: "6.00",
        projected_month_end: "180.00",
        days_elapsed: 20,
        days_in_month: 30,
        category_deltas: [],
      }) as never,
    );

    renderPage(createElement(Dashboard));

    expectHintRenders("Range net", "Income minus spending summed over EVERY bucket");
    expectHintRenders("Positions value", "newest cached price on or before that date");
    expectHintRenders("Cash", "starting balance plus committed transactions");
  });

  it("the spending-insight aggregates carry their formulas too", async () => {
    mocked.getNetWorth.mockReturnValue(
      Promise.resolve({ from: "2026-01-01", to: "2026-03-31", granularity: "month", buckets: BUCKETS }) as never,
    );
    mocked.getCashflow.mockReturnValue(
      Promise.resolve({ from: "2026-01-01", to: "2026-03-31", granularity: "month", buckets: [] }) as never,
    );
    mocked.getSpendingCategories.mockReturnValue(Promise.resolve({ items: [], total_cad: "0.00" }) as never);
    mocked.listTransactions.mockReturnValue(Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 }) as never);
    mocked.listBudgets.mockReturnValue(
      Promise.resolve([budget(1, 201.94, "Groceries")]) as never,
    );
    mocked.getSpendingInsights.mockReturnValue(
      Promise.resolve({
        current_month_spending: "120.00",
        previous_month_spending: "300.00",
        month_over_month_pct: -60,
        daily_burn_rate: "6.00",
        projected_month_end: "180.00",
        days_elapsed: 20,
        days_in_month: 30,
        category_deltas: [],
      }) as never,
    );

    renderPage(createElement(Dashboard));
    await waitFor(() => expect(screen.queryByText(/refunds subtract/)).not.toBeNull());
    await waitFor(() => expect(screen.queryByText(/spent ÷ limit/)).not.toBeNull());

    expectHintRenders("This month", "refunds subtract, income never counts");
    expectHintRenders("vs last month", "against ALL of last month");
    expectHintRenders("Daily burn rate", "÷ the days elapsed");
    expectHintRenders("Projected month-end", "straight-line projection");
    // Dashboard budget cards: the TRUE ratio, and only the bar clamps.
    expectHintRenders("202% used", "spent ÷ limit — the true ratio, never clamped");
    expect(tipOf(hintFor("202% used")).textContent).toContain("201.94% prints as 202%");
  });
});

describe("M97 tooltips — Budgets page", () => {
  it("pct_used on the Budgets page carries the same honest copy", async () => {
    mocked.listBudgets.mockReturnValue(Promise.resolve([budget(1, 201.94, "Groceries")]) as never);
    mocked.listCategories.mockReturnValue(Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 }) as never);
    renderPage(createElement(Budgets));
    await screen.findByText("202% used");
    expectHintRenders("202% used", "spent ÷ limit — the true ratio, never clamped");
  });
});

describe("M97 tooltips — TWR / MWR (docs/05 §Total return)", () => {
  it("each metric explains itself, its sibling, and why both exist", async () => {
    mocked.getTotalReturn.mockReturnValue(
      Promise.resolve({
        from: "2026-01-01",
        to: "2026-03-31",
        status: "ok",
        start_value_cad: "1000.00",
        end_value_cad: "1100.00",
        net_flow_cad: "0.00",
        income_cad: "40.00",
        twr_pct: "10.00000000",
        mwr_pct: "9.50000000",
        valuation_days: 90,
        active_days: 89,
        flow_days: 0,
        account_count: 1,
      }) as never,
    );
    renderPage(createElement(TotalReturnMetrics, { from: "2026-01-01", to: "2026-03-31" }));
    await screen.findByText("Time-weighted (TWR)");

    expectHintRenders("Time-weighted (TWR)", "chain-linked");
    expect(tipOf(hintFor("Time-weighted (TWR)")).textContent).toContain(
      "TWR is the one you compare against a benchmark",
    );
    expectHintRenders("Money-weighted (MWR)", "Modified Dietz");
    expect(tipOf(hintFor("Money-weighted (MWR)")).textContent).toContain(
      "neither replaces the other",
    );
    expectHintRenders("Net flows", "External money crossing the portfolio boundary");
    expectHintRenders("Investment income", "price moves are not counted here");
  });
});
