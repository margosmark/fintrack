// M116 — the bill drill-down `/bills/:billId` (docs/25 §M116 B2).
//
// What is pinned here is the part that can look right and be wrong:
//   * the header states the CURRENT RULE (mode + term + amount band), because
//     the matched set is that rule re-applied over the ledger (D3) — nothing
//     else is keeping it honest;
//   * the rolling readout comes from the SERVER (the page never re-averages
//     the rows it happens to be showing — a paginated list would disagree with
//     the chart);
//   * the period chart ships a DATA TABLE fallback: keyboard-reachable through
//     the native `summary`, walkable by a screen reader, and a missed period
//     says "missed" instead of leaving a blank cell;
//   * the rule editor is inline and says out loud that saving re-matches.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getBillDetail: vi.fn(),
  updateBillRule: vi.fn(),
  // M122: the rule editor's suggestion hook — resolved empty here; the M122
  // module owns the suggestion behavior itself.
  suggestTerms: vi.fn(),
  updateTransaction: vi.fn(),
  getTransactionTags: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
}));

vi.mock("../src/api/bills", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/bills")>();
  return {
    ...actual,
    getBillDetail: mocks.getBillDetail,
    updateBillRule: mocks.updateBillRule,
    suggestTerms: mocks.suggestTerms,
  };
});
vi.mock("../src/api/accounts", () => ({ listAccounts: mocks.listAccounts }));
vi.mock("../src/api/categories", () => ({ listCategories: mocks.listCategories }));
vi.mock("../src/api/transactions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...actual,
    updateTransaction: mocks.updateTransaction,
    getTransactionTags: mocks.getTransactionTags,
    setTransactionTags: vi.fn(),
    removeTransactionTag: vi.fn(),
  };
});
vi.mock("../src/api/tags", () => ({ listTags: mocks.listTags, createTag: vi.fn() }));
vi.mock("../src/api/receipts", () => ({
  fetchReceiptBlob: vi.fn(),
  uploadReceipt: vi.fn(),
  deleteReceipt: vi.fn(),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { BillDetail } from "../src/pages/BillDetail";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import type { BillDetail as BillDetailBody } from "../src/api/bills";

// jsdom has no matchMedia; ThemeProvider and useReducedMotion both call it.
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

function detail(): BillDetailBody {
  return {
    bill: {
      id: 7,
      user_id: 1,
      name: "Netflix",
      amount: "15.99",
      category_id: null,
      frequency: "monthly",
      next_due_date: "2026-11-01",
      remind_days_before: 3,
      enabled: true,
      match_mode: "contains",
      match_term: "NETFLIX",
      amount_tolerance: null,
      payment_count: 3,
      rolling_average: "15.99",
    },
    matched_count: 3,
    transactions: {
      items: [
        {
          id: 101,
          account_id: 5,
          date: "2026-03-05",
          posting_date: null,
          raw_description: "NETFLIX.COM",
          normalized_payee: "NETFLIX.COM",
          amount_native: "-15.99",
          currency: "CAD",
          cad_amount: "-15.99",
          category_id: null,
          is_recurring: false,
          recurring_cadence: null,
          recurring_next_date: null,
          reviewed_at: null,
          reviewed_by: null,
          transfer_pair_id: null,
          pair_account_name: null,
          has_receipt: false,
          parent_split_id: null,
          is_split: false,
          note: null,
          tags: [],
          payee_cipher: null,
          raw_desc_cipher: null,
          amount_cipher: null,
          cad_amount_cipher: null,
          payee_token: null,
        },
      ],
      total: 3,
      limit: 50,
      offset: 0,
      split_parents: [],
    },
    rolling: {
      frequency: "monthly",
      six: {
        periods: 6,
        window_periods: 3,
        payments: 3,
        total: "31.98",
        mean: "15.99",
        median: "15.99",
      },
      twelve: {
        periods: 12,
        window_periods: 3,
        payments: 3,
        total: "31.98",
        mean: "15.99",
        median: "15.99",
      },
    },
    chart: {
      frequency: "monthly",
      unit: "month",
      granularity: "month",
      first_period: "2026-01-01",
      last_period: "2026-03-01",
      points: [
        // `rolling_six` is the M122 T3 server line: trailing means over
        // [15.99, 0.00 (missed), 15.99] — the last point IS rolling.six.mean.
        { period: "2026-01-01", label: "Jan 2026", total: "15.99", count: 1, missed: false, rolling_six: "15.99" },
        // The gap: Feb was not paid, and the chart must SAY so (B2.4).
        { period: "2026-02-01", label: "Feb 2026", total: "0.00", count: 0, missed: true, rolling_six: "8.00" },
        { period: "2026-03-01", label: "Mar 2026", total: "15.99", count: 1, missed: false, rolling_six: "10.66" },
      ],
    },
    stats: {
      all_time: { total: "31.98", payments: 2, average: "15.99", years: 1, yearly_average: "31.98" },
      ytd: { year: 2026, total: "31.98", payments: 2 },
    },
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/bills/7"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <Routes>
            <Route path="/bills/:billId" element={<BillDetail />} />
          </Routes>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.getBillDetail.mockResolvedValue(detail());
  mocks.updateBillRule.mockResolvedValue({});
  mocks.suggestTerms.mockResolvedValue([]);
  mocks.updateTransaction.mockResolvedValue({});
  mocks.listAccounts.mockResolvedValue({ items: [{ id: 5, name: "Chequing" }] });
  mocks.listCategories.mockResolvedValue({ items: [] });
  mocks.listTags.mockResolvedValue({ items: [] });
  mocks.getTransactionTags.mockResolvedValue({ items: [] });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("bill detail (M116 B2)", () => {
  it("states the current rule, the payment count and both rolling windows", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Netflix" })).toBeTruthy();

    // The header explains HOW a transaction belongs to this bill.
    const ruleLine = screen.getByText(/Matches transactions whose payee/);
    expect(ruleLine.textContent).toContain("contains “NETFLIX”");
    expect(ruleLine.textContent).toContain("at $15.99 exactly");

    // "3 payments" appears on the badge AND in both stat hints — a unique
    // matcher would fail on a correct page, so count them.
    expect(screen.getAllByText("3 payments").length).toBeGreaterThanOrEqual(1);
    // `Stat` renders label → value → hint; both windows come from the SERVER,
    // so the page can never re-average the rows it happens to be showing.
    expect(screen.getByText("Last 6 periods").nextElementSibling?.textContent).toBe("$15.99");
    expect(screen.getByText("Last 12 periods").nextElementSibling?.textContent).toBe("$15.99");
    expect(screen.getAllByText(/median \$15\.99/)).toHaveLength(2);
  });

  it("renders the period chart with its data-table fallback, missed marker included", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });

    // One leaf node with a text equivalent (docs/12), NOT a walk of <rect>s.
    const frame = document.querySelector('[role="img"]');
    expect(frame).toBeTruthy();
    expect(frame?.getAttribute("aria-label")).toContain("3 points");
    expect(frame?.getAttribute("aria-label")).toContain("1 missed period");

    // The table is the fallback: a real <table>, a caption, and the gap is
    // spelled out rather than left as an empty cell. Scoped to the disclosure,
    // because the matched-transactions table is on the same page.
    const disclosure = screen.getByText(/show the numbers behind this chart/i);
    fireEvent.click(disclosure);
    const panel = disclosure.closest("details") as HTMLElement;
    const table = within(panel).getByRole("table");
    // Scoped to the table: the chart's own axis tick renders the SAME label,
    // and a query that could match either would prove nothing.
    const scoped = within(table);
    expect(scoped.getByText("missed")).toBeTruthy();
    expect(scoped.getByText("Feb 2026")).toBeTruthy();
    expect(scoped.queryByText("Jan 2026")).toBeTruthy();
    expect(within(table).getAllByRole("row")).toHaveLength(4); // header + 3 periods
  });

  it("lists the matched transactions with their account and opens the editor", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });
    expect(screen.getByText("NETFLIX.COM")).toBeTruthy();
    expect(screen.getByText("Chequing")).toBeTruthy();

    fireEvent.click(screen.getByText("NETFLIX.COM"));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
  });

  it("edits the rule inline and says that saving re-matches the ledger", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });

    fireEvent.click(screen.getByRole("button", { name: "Edit rule for Netflix" }));
    // M121: the rule IS the chip list — editing means removing the old term
    // and typing the new one (the input is never a copy of the rule).
    fireEvent.click(screen.getByRole("button", { name: "Remove term — NETFLIX" }));
    const term = screen.getByRole("combobox", { name: "Match terms" });
    fireEvent.change(term, { target: { value: "NETFLIX COM" } });
    // Exact name: "Match terms" is a DIFFERENT combobox since M122.
    fireEvent.change(screen.getByRole("combobox", { name: "Match" }), {
      target: { value: "exact" },
    });
    // The editor says out loud what D3 means, while it is open.
    expect(screen.getByText(/re-matches every committed transaction/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(mocks.updateBillRule).toHaveBeenCalledTimes(1));
    expect(mocks.updateBillRule).toHaveBeenCalledWith(7, {
      match_mode: "exact",
      match_terms: ["NETFLIX COM"],
      match_logic: "any",
      amount_tolerance: null,
    });
  });

  /** A two-term body — `match_logic` is the only thing each case flips. */
  async function renderTwoTermRule(logic: "any" | "all"): Promise<HTMLElement> {
    const body = detail();
    body.bill.match_terms = ["NETFLIX", "COM"];
    body.bill.match_logic = logic;
    mocks.getBillDetail.mockResolvedValue(body);
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });
    return screen.getByText(/Matches transactions whose payee/);
  }

  it("states `or` for a multi-term ANY rule (M121 D9)", async () => {
    const ruleLine = await renderTwoTermRule("any");
    // ANY reads as "or": the sentence must widen exactly where the counts do.
    expect(ruleLine.textContent).toContain("contains “NETFLIX” or “COM”");
  });

  it("states `and` for a multi-term ALL rule (M121 D9)", async () => {
    const ruleLine = await renderTwoTermRule("all");
    expect(ruleLine.textContent).toContain("contains “NETFLIX” and “COM”");
  });
});
