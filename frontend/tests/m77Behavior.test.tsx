// M77 behaviour tests (docs/12 §Sankey cash-flow, docs/05 §Total return).
//
// Acceptance claims the golden-parity file does NOT cover:
//
//  1. The Sankey actually RENDERS with at least two income categories and
//     three expense categories — labels on screen, not just nodes in a
//     report object. Both the bare chart and the Reports-page panel are
//     exercised (the panel owns its own query/loading/error states).
//  2. Total return renders the server's 8-decimal strings verbatim (M105's
//     display trim never invents a digit) and shows the M65 no-data state.
//
// M160-R2 (t_85311c34) note: the two PANEL loaders below are now thin reads
// of `GET /reports/cashflow-sankey` / `GET /reports/total-return` in
// `api/reports.ts`, and the `totalReturn` / `dividendReport` computes are
// deleted with the shim — so the hand-computed-ledger pin this file used to
// carry (TWR 10.9 % / MWR 12.46915167 % over the docs/05 ledger) lives where
// the math now runs: `backend/tests/test_total_return.py` asserts the SAME
// ledger against the service, and `test_reports_sankey.py` /
// `test_dividends.py` pin their routes. What stays here is the CHART and the
// PANEL behaviour, which only the browser can prove.

import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../src/theme/ThemeProvider";
import { CashflowSankey } from "../src/components/charts/CashflowSankey";
import { SankeyPanel } from "../src/components/reports/SankeyPanel";
import { TotalReturnMetrics } from "../src/components/investments/TotalReturnMetrics";
// M160-R2: the panel loaders live in `api/reports.ts` again (server reads).
import { getCashflowSankey, getTotalReturn } from "../src/api/reports";
import { cashflowSankey, type SankeyReport } from "../src/lib/sankeyEngine";
import type { Category } from "../src/api/categories";
import type { Transaction } from "../src/api/transactions";

// The two PANEL LOADERS are mocked (the panels own their query state). The
// `cashflowSankey` engine stays REAL for the chart tests below: it is the
// reference implementation the server route was written against (M160-R1
// ruling 3) and the only way to build a multi-category graph fixture here.
vi.mock("../src/api/reports", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/reports")>()),
  getCashflowSankey: vi.fn(),
  getTotalReturn: vi.fn(),
}));

// jsdom has no matchMedia; ThemeProvider/useReducedMotion call it.
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

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider>{ui}</ThemeProvider>);
}

/** Panels reach the same provider — `useChartPalette` is called on their path. */
function withProviders(client: QueryClient, ui: ReactNode) {
  const q = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return createElement(ThemeProvider, null, createElement(q, null, ui));
}

// --- fixture 1: a Sankey with >= 2 income and >= 3 expense categories -------

const categories: Category[] = [
  { id: 1, user_id: 1, name: "Salary", parent_id: null, is_system: false },
  { id: 2, user_id: 1, name: "Interest", parent_id: null, is_system: false },
  { id: 3, user_id: 1, name: "Food", parent_id: null, is_system: false },
  { id: 4, user_id: 1, name: "Transport", parent_id: null, is_system: false },
  { id: 5, user_id: 1, name: "Utilities", parent_id: null, is_system: false },
  { id: 6, user_id: 1, name: "Uncat sibling", parent_id: null, is_system: false },
];

function txn(id: number, day: string, amount: string, categoryId: number | null): Transaction {
  return {
    id,
    account_id: 1,
    date: day,
    posting_date: null,
    raw_description: `raw ${id}`,
    normalized_payee: "Payee",
    amount_native: amount,
    currency: "CAD",
    cad_amount: amount,
    category_id: categoryId,
    transfer_pair_id: null,
    pair_account_name: null,
    tags: [],
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
  };
}

const sankeyTxns: Transaction[] = [
  txn(1, "2025-01-05", "3000.00", 1), // Salary      (income)
  txn(2, "2025-02-05", "25.00", 2), // Interest    (income)
  txn(3, "2025-01-08", "-120.50", 3), // Food        (expense)
  txn(4, "2025-02-03", "-60.00", 4), // Transport   (expense)
  txn(5, "2025-02-14", "-40.25", 5), // Utilities   (expense)
];

function buildSankeyReport(): SankeyReport {
  return cashflowSankey("2025-01-01", "2025-12-31", {
    txns: sankeyTxns,
    trades: [],
    accounts: new Map([[1, { currency: "CAD" }]]),
    categories: new Map(categories.map((c) => [c.id, c])),
    rates: new Map(),
  });
}

describe("Sankey cash-flow chart (M77)", () => {
  afterEach(() => vi.clearAllMocks());

  it("renders >= 2 income and >= 3 expense categories with their labels", () => {
    const report = buildSankeyReport();
    const income = report.nodes.filter((n) => n.column === "source" && n.group === "income");
    const spent = report.nodes.filter((n) => n.column === "destination" && n.group === "spent");
    expect(income.length).toBeGreaterThanOrEqual(2);
    expect(spent.length).toBeGreaterThanOrEqual(3);

    renderWithTheme(<CashflowSankey report={report} />);

    // income side
    expect(screen.getByText(/Salary/)).toBeDefined();
    expect(screen.getByText(/Interest/)).toBeDefined();
    // expense side
    expect(screen.getByText(/Food/)).toBeDefined();
    expect(screen.getByText(/Transport/)).toBeDefined();
    expect(screen.getByText(/Utilities/)).toBeDefined();
    // the balancing destination the surplus creates
    expect(screen.getByText(/Savings/)).toBeDefined();

    // one accessible sentence over the whole tree (docs/12 accessibility)
    const frame = screen.getByRole("img");
    expect(frame.getAttribute("aria-label")).toContain("Cash flow Sankey");
  });

  it("conserves to the cent: money in == saved + invested + spent", () => {
    const { totals } = buildSankeyReport();
    const spent = Number(totals.expenses_cad);
    expect(Number(totals.money_in_cad)).toBeCloseTo(
      Number(totals.saved_cad) + Number(totals.invested_cad) + spent,
      8,
    );
    // and income alone equals money in when there is no deficit
    expect(totals.income_cad).toBe(totals.money_in_cad);
    expect(totals.drawn_cad).toBe("0.00");
  });

  it("SankeyPanel renders the totals line from the query", async () => {
    vi.mocked(getCashflowSankey).mockResolvedValue(buildSankeyReport());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      withProviders(
        client,
        createElement(SankeyPanel, { from: "2025-01-01", to: "2025-12-31" }),
      ),
    );

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Where the money went" }),
      ).toBeDefined();
    });
    // one <p> carries the whole roll-up: "Money in $x · Spent $y · …"
    // (the hub <text> in the SVG says "Money in" too, hence getAllByText)
    const rollup = screen
      .getAllByText(/Money in/)
      .map((el) => el.textContent ?? "")
      .find((text) => text.includes("·"));
    expect(rollup).toBeDefined();
    expect(rollup).toMatch(/Spent/);
    expect(rollup).toMatch(/Invested/);
    expect(rollup).toMatch(/Saved/);
    expect(vi.mocked(getCashflowSankey)).toHaveBeenCalledWith("2025-01-01", "2025-12-31");
  });

  it("SankeyPanel shows the no-data panel for an empty graph (M65 honesty)", async () => {
    vi.mocked(getCashflowSankey).mockResolvedValue(
      cashflowSankey("2024-01-01", "2024-12-31", {
        txns: [],
        trades: [],
        accounts: new Map([[1, { currency: "CAD" }]]),
        categories: new Map(categories.map((c) => [c.id, c])),
        rates: new Map(),
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      withProviders(
        client,
        createElement(SankeyPanel, { from: "2024-01-01", to: "2024-12-31" }),
      ),
    );

    await waitFor(() => {
      expect(screen.getByText("No data in range")).toBeDefined();
    });
    expect(screen.queryByText("Where the money went")).toBeNull();
  });
});

// --- Total return metrics card (M77/M105, docs/05) --------------------------
//
// The numbers this card renders come from the docs/05 hand-computed ledger —
// contribution 1000, buy 10 @ 50, sell 5 @ 62, withdraw 200 across Jan–Mar
// 2025 ⇒ TWR 10.9 % and MWR 12.46915167 %. M160-R2 (t_85311c34) retired the
// client compute, so the MATH is pinned where it runs now:
// `backend/tests/test_total_return.py::test_fixture_one_hand_computed_twr_and_mwr`
// asserts that SAME ledger against the service (plus reinvested-dividend
// neutrality, flow-vs-return, no-data and the half-up ratio rounding). What
// only the browser can prove stays here: the server's 8-decimal strings
// reaching the screen verbatim, and the M65 no-data state.

describe("Total return metrics — server strings on screen (M77/M105)", () => {
  it("TotalReturnMetrics renders both metrics and the M65 no-data state", async () => {
    vi.mocked(getTotalReturn).mockResolvedValue({
      from: "2025-01-01",
      to: "2025-03-31",
      status: "ok",
      start_value_cad: "0.00",
      end_value_cad: "909.00",
      net_flow_cad: "800.00",
      income_cad: "0.00",
      twr_pct: "10.90000000",
      mwr_pct: "12.46915167",
      valuation_days: 90,
      active_days: 89,
      flow_days: 2,
      account_count: 1,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      withProviders(
        client,
        createElement(TotalReturnMetrics, { from: "2025-01-01", to: "2025-03-31" }),
      ),
    );

    await waitFor(() => {
      // Display-only trim (M105): the card reads as a person's number…
      expect(screen.getByText("+10.90%")).toBeDefined();
      expect(screen.getByText("+12.46%")).toBeDefined();
    });
    // …and the 8-decimal wire string is NOT lost — it is the span's aria-label.
    expect(screen.getByText("+10.90%").getAttribute("aria-label")).toBe("10.90000000%");
    expect(screen.getByText("+12.46%").getAttribute("aria-label")).toBe("12.46915167%");
    expect(screen.getByText("Time-weighted (TWR)")).toBeDefined();
    expect(screen.getByText("Money-weighted (MWR)")).toBeDefined();
    expect(screen.getByText("Period return")).toBeDefined();
  });

  it("a null return renders 'No data', never a confident 0.00000000% (M65)", async () => {
    vi.mocked(getTotalReturn).mockResolvedValue({
      from: "2025-01-01",
      to: "2025-03-31",
      status: "no_data",
      start_value_cad: "0.00",
      end_value_cad: "0.00",
      net_flow_cad: "0.00",
      income_cad: "0.00",
      twr_pct: null,
      mwr_pct: null,
      valuation_days: 90,
      active_days: 0,
      flow_days: 0,
      account_count: 1,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      withProviders(
        client,
        createElement(TotalReturnMetrics, { from: "2025-01-01", to: "2025-03-31" }),
      ),
    );

    await waitFor(() => {
      expect(screen.getAllByText("No data").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/0\.00000000%/)).toBeNull();
  });
});
