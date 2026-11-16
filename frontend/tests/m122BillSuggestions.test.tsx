// M122 — bill-rule UX: live term suggestions, the dry-run "Test" panel, the
// chart's rolling line and the Spending stats card (docs/25 §M122).
//
// What is pinned here is the part that can look right and be wrong:
//   * suggestions only fire for an EMPTY draft (top groups) or a draft of
//     ≥3 chars — a 1–2 char draft asks for nothing;
//   * the suggestion list is a real listbox (combobox + aria-activedescendant,
//     the M104 TagPicker contract): ArrowDown highlights, Enter adds the
//     HIGHLIGHTED suggestion — with no highlight Enter still commits the typed
//     text, exactly as before;
//   * the Test panel says "Draft preview — not saved" out loud, renders the
//     SERVER's summary, and is cleared the moment the draft changes (a stale
//     preview is a lie about the current inputs);
//   * every figure on the page comes from the server payload — the page never
//     re-averages what it happens to be showing (the M91 rule).
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getBillDetail: vi.fn(),
  updateBillRule: vi.fn(),
  findPastPayments: vi.fn(),
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
    findPastPayments: mocks.findPastPayments,
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
import type { BillDetail as BillDetailBody, BillFindPast } from "../src/api/bills";

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

const ROGERS = {
  normalized_payee: "ROGERS COMMUNICATIONS",
  count: 14,
  total: "1119.86",
  last_date: "2026-09-03",
};
const NETFLIX = { normalized_payee: "NETFLIX", count: 3, total: "47.97", last_date: "2026-09-05" };

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
      match_terms: ["NETFLIX"],
      match_logic: "any",
      amount_tolerance: null,
      payment_count: 3,
      rolling_average: "15.99",
      ytd_total: "31.98",
    },
    matched_count: 3,
    transactions: {
      items: [],
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
        mean: "10.66",
        median: "15.99",
      },
      twelve: {
        periods: 12,
        window_periods: 3,
        payments: 3,
        total: "31.98",
        mean: "10.66",
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
        // Server-computed trailing means over [15.99, 0.00 (missed), 15.99].
        { period: "2026-01-01", label: "Jan 2026", total: "15.99", count: 1, missed: false, rolling_six: "15.99" },
        { period: "2026-02-01", label: "Feb 2026", total: "0.00", count: 0, missed: true, rolling_six: "8.00" },
        { period: "2026-03-01", label: "Mar 2026", total: "15.99", count: 1, missed: false, rolling_six: "10.66" },
      ],
    },
    stats: {
      all_time: {
        total: "47.97",
        payments: 3,
        average: "15.99",
        years: 1,
        yearly_average: "47.97",
      },
      ytd: { year: 2026, total: "31.98", payments: 2 },
    },
  };
}

function preview(count: number): BillFindPast {
  return {
    match_mode: "contains",
    match_term: "NETFLIX",
    match_terms: ["NETFLIX"],
    match_logic: "any",
    frequency: "monthly",
    total_count: count,
    truncated: false,
    groups: [
      {
        period: "2026-09-01",
        label: "Sep 2026",
        count,
        total: (count * 15.99).toFixed(2),
        items: Array.from({ length: count }, (_unused, index) => ({
          id: 900 + index,
          date: `2026-09-0${index + 1}`,
          amount_native: "-15.99",
          currency: "CAD",
          cad_amount: "-15.99",
          normalized_payee: "NETFLIX",
          account_id: 5,
          account_name: "Chequing",
        })),
      },
    ],
    stats: {
      first_date: "2026-09-01",
      last_date: "2026-09-03",
      last_12_total: (count * 15.99).toFixed(2),
      account_counts: [{ account_id: 5, account_name: "Chequing", count }],
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

async function openEditor(): Promise<HTMLElement> {
  renderPage();
  await screen.findByRole("heading", { name: "Netflix" });
  fireEvent.click(screen.getByRole("button", { name: "Edit rule for Netflix" }));
  return screen.getByRole("combobox", { name: "Match terms" });
}

beforeEach(() => {
  mocks.getBillDetail.mockResolvedValue(detail());
  mocks.updateBillRule.mockResolvedValue({});
  mocks.findPastPayments.mockResolvedValue(preview(2));
  // Empty draft → the top groups; any draft → the draft's matches.
  mocks.suggestTerms.mockImplementation(({ q }: { q: string }) =>
    Promise.resolve(q === "" ? [NETFLIX, ROGERS] : [ROGERS]),
  );
  mocks.updateTransaction.mockResolvedValue({});
  mocks.listAccounts.mockResolvedValue({ items: [{ id: 5, name: "Chequing" }] });
  mocks.listCategories.mockResolvedValue({ items: [] });
  mocks.listTags.mockResolvedValue({ items: [] });
  mocks.getTransactionTags.mockResolvedValue({ items: [] });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("term suggestions from the own ledger (M122 T1)", () => {
  it("asks for the top groups on open, but nothing until the draft is 3 chars", async () => {
    const term = await openEditor();

    // Opening the editor asks for the empty-draft candidates ONCE…
    await waitFor(() => expect(mocks.suggestTerms).toHaveBeenCalledTimes(1));
    expect(mocks.suggestTerms).toHaveBeenCalledWith({ q: "", billId: 7, matchMode: "contains" });

    // …a 1–2 char draft asks for NOTHING (the gate is the contract).
    fireEvent.change(term, { target: { value: "ro" } });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(mocks.suggestTerms).toHaveBeenCalledTimes(1);

    // ≥3 chars asks for the draft's matches.
    fireEvent.change(term, { target: { value: "rog" } });
    await waitFor(() =>
      expect(mocks.suggestTerms).toHaveBeenCalledWith({
        q: "rog",
        billId: 7,
        matchMode: "contains",
      }),
    );
  });

  it("offers the empty-draft candidates as quick-add chips with their counts", async () => {
    await openEditor();
    const chip = await screen.findByRole("button", {
      name: "Add suggested term — ROGERS COMMUNICATIONS ×14",
    });
    expect(chip.textContent).toContain("×14");

    fireEvent.click(chip);
    // The suggestion becomes a real chip of the rule.
    expect(
      screen.getByRole("button", { name: "Remove term — ROGERS COMMUNICATIONS" }),
    ).toBeTruthy();
  });

  it("is a keyboard-complete listbox: ArrowDown highlights, Enter adds the highlight", async () => {
    const term = await openEditor();
    fireEvent.change(term, { target: { value: "rog" } });

    const listbox = await screen.findByRole("listbox", { name: /match terms suggestions/i });
    const option = within(listbox).getByRole("option", { name: /ROGERS COMMUNICATIONS/ });
    expect(option.getAttribute("aria-selected")).toBe("false");
    // The combobox tracks the highlight without taking focus away.
    expect(term.getAttribute("role")).toBe("combobox");
    expect(term.getAttribute("aria-expanded")).toBe("true");
    expect(term.getAttribute("aria-controls")).toBe(listbox.id);

    fireEvent.keyDown(term, { key: "ArrowDown" });
    const active = term.getAttribute("aria-activedescendant");
    expect(active).toBe(option.id);
    expect(document.getElementById(active ?? "")?.getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(term, { key: "Enter" });
    // The SUGGESTION was added — the partial draft did not survive as a chip.
    expect(
      screen.getByRole("button", { name: "Remove term — ROGERS COMMUNICATIONS" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Remove term — rog" })).toBeNull();
  });

  it("Enter with no highlight still commits the typed text (M121 behavior kept)", async () => {
    const term = await openEditor();
    fireEvent.change(term, { target: { value: "rogers fibre" } });
    await screen.findByRole("listbox", { name: /match terms suggestions/i });

    fireEvent.keyDown(term, { key: "Enter" });
    expect(screen.getByRole("button", { name: "Remove term — rogers fibre" })).toBeTruthy();
  });
});

describe("the draft Test panel (M122 T2)", () => {
  it("runs the DRAFT rule and renders the server's summary, labelled as unsaved", async () => {
    const term = await openEditor();
    fireEvent.click(screen.getByRole("button", { name: /Test/ }));

    await waitFor(() => expect(mocks.findPastPayments).toHaveBeenCalledTimes(1));
    expect(mocks.findPastPayments).toHaveBeenCalledWith(7, {
      match_mode: "contains",
      match_terms: ["NETFLIX"],
      match_logic: "any",
      amount_tolerance: null,
    });

    // The panel is loud about being a preview of the DRAFT. Its outer div
    // carries the aria-label, so the scope query is stable.
    const panel = await screen.findByText("Draft preview — not saved");
    expect(panel).toBeTruthy();
    expect(screen.getByText("2 matching transactions")).toBeTruthy();
    expect(screen.getByText("2026-09-01 → 2026-09-03")).toBeTruthy();
    expect(screen.getByText("Chequing ×2")).toBeTruthy();
    // The sample rows and the period table both render — scoped to the panel,
    // because the term CHIP on the same page says "NETFLIX" too.
    const scoped = within(screen.getByLabelText("Draft preview — not saved"));
    expect(scoped.getAllByText("NETFLIX").length).toBeGreaterThanOrEqual(2);
    expect(scoped.getByText("Sep 2026")).toBeTruthy();
    // The editor is still open underneath — nothing was saved.
    expect(term).toBeTruthy();
  });

  it("re-tests a changed draft and clears a stale preview the moment it changes", async () => {
    await openEditor();
    const term = screen.getByRole("combobox", { name: "Match terms" });

    // First run: 2 matches. Add a second term → the DRAFT changed, so the old
    // preview must not survive as if it described the current inputs.
    mocks.findPastPayments.mockResolvedValueOnce(preview(2));
    fireEvent.click(screen.getByRole("button", { name: /Test/ }));
    await screen.findByText("2 matching transactions");

    fireEvent.change(term, { target: { value: "com" } });
    fireEvent.keyDown(term, { key: "Enter" });
    await waitFor(() => expect(screen.queryByText("2 matching transactions")).toBeNull());

    // Second run: the narrowed draft finds 1.
    mocks.findPastPayments.mockResolvedValueOnce(preview(1));
    fireEvent.click(screen.getByRole("button", { name: /Test/ }));
    await screen.findByText("1 matching transaction");
    expect(mocks.findPastPayments).toHaveBeenLastCalledWith(7, {
      match_mode: "contains",
      match_terms: ["NETFLIX", "com"],
      match_logic: "any",
      amount_tolerance: null,
    });
  });
});

describe("the chart line and spending stats (M122 T3/T4)", () => {
  it("describes the rolling line in the chart's text equivalent AND its fallback table", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });

    const frame = document.querySelector('[role="img"]');
    expect(frame?.getAttribute("aria-label")).toContain("6-period average ends at $10.66");

    const disclosure = screen.getByText(/show the numbers behind this chart/i);
    fireEvent.click(disclosure);
    const table = within(disclosure.closest("details") as HTMLElement).getByRole("table");
    expect(within(table).getByText("6-period avg")).toBeTruthy();
    // The line's values are walkable without parsing the SVG.
    expect(within(table).getByText("$8.00")).toBeTruthy();
    expect(within(table).getByText("$10.66")).toBeTruthy();
  });

  it("renders the Spending stats card entirely from the server payload", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Netflix" });

    expect(screen.getByText("Spending stats")).toBeTruthy();
    expect(screen.getByText("All-time total").nextElementSibling?.textContent).toBe("$47.97");
    expect(screen.getByText("Per payment (all-time)").nextElementSibling?.textContent).toBe(
      "$15.99",
    );
    expect(screen.getByText("This year (2026)").nextElementSibling?.textContent).toBe("$31.98");
    expect(screen.getByText("Yearly average").nextElementSibling?.textContent).toBe("$47.97");
  });
});
