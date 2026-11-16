// M95 P3 — the 5-second undo on destructive deletes (docs/09 §Destructive
// actions). Per surface: nothing is sent while the window is open, Undo during
// it restores the row exactly (order + count), expiry commits once and the
// refetch reflects server truth, a 409 surfaces the server's detail verbatim,
// and the Undo control is a focusable button inside a polite live region.
//
// Timer mechanics: DTL's waitFor only advances REAL timers (its fake-timer
// detector keys on `globalThis.jest`), so each expiry test renders under real
// timers, switches to fake timers ONLY for the click + `advanceTimersByTimeAsync`,
// then switches back before any waitFor.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

// jsdom has no matchMedia; framer-motion asks for it (reduced motion).
beforeAll(() => {
  if (window.matchMedia === undefined) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    });
  }
});

const m = vi.hoisted(() => ({
  listTransactions: vi.fn(),
  fetchAllTransactions: vi.fn(),
  deleteTransaction: vi.fn(),
  listAccounts: vi.fn(),
  deleteAccount: vi.fn(),
  listInstitutions: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  listSuggestions: vi.fn(),
  listTrades: vi.fn(),
  listAcbPools: vi.fn(),
  listImports: vi.fn(),
  revertBatch: vi.fn(),
  listDebts: vi.fn(),
  listGoals: vi.fn(),
  getReconciliation: vi.fn(),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, setUser: () => {} }),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: m.listTransactions,
  fetchAllTransactions: m.fetchAllTransactions,
  deleteTransaction: m.deleteTransaction,
}));
vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  listAccounts: m.listAccounts,
  deleteAccount: m.deleteAccount,
}));
vi.mock("../src/api/institutions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/institutions")>()),
  listInstitutions: m.listInstitutions,
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: m.listCategories,
}));
vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: m.listTags,
}));
vi.mock("../src/api/suggestions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/suggestions")>()),
  listSuggestions: m.listSuggestions,
}));
vi.mock("../src/api/trades", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/trades")>()),
  listTrades: m.listTrades,
}));
vi.mock("../src/api/acb", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/acb")>()),
  listAcbPools: m.listAcbPools,
}));
vi.mock("../src/api/imports", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/imports")>()),
  listImports: m.listImports,
  revertBatch: m.revertBatch,
}));
vi.mock("../src/api/debts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/debts")>()),
  listDebts: m.listDebts,
}));
vi.mock("../src/api/goals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/goals")>()),
  listGoals: m.listGoals,
}));
vi.mock("../src/api/reconciliation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/reconciliation")>()),
  getReconciliation: m.getReconciliation,
}));

import { ApiError } from "../src/api/client";
import type { Account } from "../src/api/accounts";
import { UNDO_WINDOW_MS, ToastProvider } from "../src/components/ui/Toast";
import { Transactions } from "../src/pages/Transactions";
import { AccountsSection } from "../src/components/settings/AccountsSection";
import { BatchHistory } from "../src/pages/Imports";

const TXNS = [
  {
    id: 1,
    date: "2026-01-05",
    normalized_payee: "Alpha Groceries",
    raw_description: "ALPHA GROCERIES TORONTO",
    amount_native: "-42.10",
    currency: "CAD",
    cad_amount: null,
    category_id: null,
    account_id: 7,
    transfer_pair_id: null,
  },
  {
    id: 2,
    date: "2026-01-06",
    normalized_payee: "Beta Gas",
    raw_description: "BETA GAS",
    amount_native: "-30.00",
    currency: "CAD",
    cad_amount: null,
    category_id: null,
    account_id: 7,
    transfer_pair_id: null,
  },
];

const ACCOUNT = {
  id: 7,
  institution_id: null,
  name: "RBC Main Chequing",
  type: "chequing",
  currency: "CAD",
  taxable: true,
  llm_autopilot: false,
  cash_balance: "1500.00",
  investment_value: "0.00",
  total_value: "1500.00",
  starting_balance: "0.00",
  name_cipher: null,
} as Account;

const BATCH = {
  id: 3,
  status: "committed",
  file_name: "rbc-2026-01.csv",
  institution: "RBC",
  account_id: 7,
  imported: 3,
  duplicates_skipped: 1,
  unresolved: 0,
};

function page<T>(items: T[], total = items.length): { items: T[]; total: number; limit: number; offset: number } {
  return { items, total, limit: 200, offset: 0 };
}

function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ToastProvider>{ui}</ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

function snackbar() {
  return screen.getByTestId("undo-snackbar");
}

beforeEach(() => {
  // Spies are module-level and survive between tests — call history from the
  // previous test would poison every toHaveBeenCalledTimes assertion.
  vi.clearAllMocks();
  m.listTransactions.mockResolvedValue(page(TXNS, TXNS.length));
  m.fetchAllTransactions.mockResolvedValue(TXNS);
  m.deleteTransaction.mockResolvedValue({});
  m.listAccounts.mockResolvedValue(page([ACCOUNT], 1));
  m.deleteAccount.mockResolvedValue({});
  m.listInstitutions.mockResolvedValue(page([]));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page([]));
  m.listSuggestions.mockResolvedValue([]);
  m.listTrades.mockResolvedValue(page([], 0));
  m.listAcbPools.mockResolvedValue(page([], 0));
  m.listImports.mockResolvedValue(page([BATCH], 1));
  m.revertBatch.mockResolvedValue({});
  m.listDebts.mockResolvedValue([]);
  m.listGoals.mockResolvedValue([]);
  m.getReconciliation.mockResolvedValue({
    items: [],
    summary: { total: 0, matched: 0, unmatched: 0, matched_sum: "0.00", unmatched_sum: "0.00" },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("M95 P3 — undo window constants", () => {
  it("is exactly 5 seconds", () => {
    expect(UNDO_WINDOW_MS).toBe(5000);
  });
});

describe("M95 P3 — transactions row delete", () => {
  async function renderTransactions() {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText("Delete transaction 1");
  }

  it("hides the row instantly, announces Undo politely, sends nothing, and restores exactly on Undo", async () => {
    await renderTransactions();

    fireEvent.click(screen.getByLabelText("Delete transaction 2"));

    // Stage 1: gone from the table, snackbar up, NOTHING dispatched.
    expect(screen.queryByLabelText("Delete transaction 2")).toBeNull();
    expect(screen.getByLabelText("Delete transaction 1")).not.toBeNull();
    const bar = snackbar();
    expect(bar.getAttribute("role")).toBe("status");
    expect(bar.getAttribute("aria-live")).toBe("polite");
    expect(screen.getByText(/Deleted transaction/)).not.toBeNull();
    expect(m.deleteTransaction).not.toHaveBeenCalled();

    // Keyboard-reachable: a native <button> that takes focus.
    const undo = screen.getByTestId("undo-snackbar-undo");
    expect(undo.tagName).toBe("BUTTON");
    undo.focus();
    expect(document.activeElement).toBe(undo);
    expect(undo.textContent).toMatch(/Undo \([0-5]s\)/);

    fireEvent.click(undo);
    await waitFor(() =>
      expect(screen.queryByTestId("undo-snackbar")).toBeNull(),
    );
    // Exactly restored: both rows, original order, still no network.
    const labels = screen
      .queryAllByLabelText(/^Delete transaction/)
      .map((el) => el.getAttribute("aria-label"));
    expect(labels).toEqual(["Delete transaction 1", "Delete transaction 2"]);
    expect(m.deleteTransaction).not.toHaveBeenCalled();
  });

  it("commits once when the window closes and the refetch reflects it", async () => {
    await renderTransactions();
    const before = m.listTransactions.mock.calls.length;
    vi.useFakeTimers();
    fireEvent.click(screen.getByLabelText("Delete transaction 1"));
    expect(m.deleteTransaction).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() => expect(m.deleteTransaction).toHaveBeenCalledTimes(1));
    expect(m.deleteTransaction).toHaveBeenCalledWith(1);
    // invalidateLedgerData() → the page refetches server truth.
    await waitFor(() => expect(m.listTransactions.mock.calls.length).toBeGreaterThan(before));
    expect(screen.queryByLabelText("Delete transaction 1")).toBeNull();
  });

  it("a failed commit restores the row and surfaces the server detail verbatim", async () => {
    await renderTransactions();
    m.deleteTransaction.mockRejectedValueOnce(
      new ApiError(409, "Transaction is referenced by a reconciliation item", "conflict"),
    );

    vi.useFakeTimers();
    fireEvent.click(screen.getByLabelText("Delete transaction 1"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(
        screen.getByText("Transaction is referenced by a reconciliation item"),
      ).not.toBeNull(),
    );
    // Restored locally; the alert carries the envelope detail untouched.
    expect(screen.getByLabelText("Delete transaction 1")).not.toBeNull();
    expect(screen.getByRole("alert")).not.toBeNull();
  });
});

describe("M95 P3 — account delete (blast radius)", () => {
  async function openConfirmDialog(counts?: Partial<Record<string, number>>) {
    const total = (key: string) => counts?.[key] ?? 0;
    m.listTransactions.mockResolvedValue(page([], total("transactions")));
    m.listTrades.mockResolvedValue(page([], total("trades")));
    m.listAcbPools.mockResolvedValue(page([], total("lots")));
    m.listImports.mockResolvedValue(page([], total("batches")));
    const rowsFor = (n: number): { id: number; account_id: number }[] =>
      Array.from({ length: n }, (_, i) => ({ id: i + 1, account_id: 7 }));
    m.listDebts.mockResolvedValue(rowsFor(total("debts")));
    m.listGoals.mockResolvedValue(rowsFor(total("goals")));
    m.getReconciliation.mockResolvedValue({
      items: [],
      summary: {
        total: total("reconciliation"),
        matched: 0,
        unmatched: 0,
        matched_sum: "0.00",
        unmatched_sum: "0.00",
      },
    });

    renderWithProviders(<AccountsSection />);
    await screen.findByRole("button", { name: "Delete RBC Main Chequing" });
    fireEvent.click(screen.getByRole("button", { name: "Delete RBC Main Chequing" }));
    await screen.findByTestId("delete-account-blast");
  }

  it("lists every dependent with live counts and blocks while anything is attached", async () => {
    await openConfirmDialog({
      transactions: 12,
      trades: 3,
      lots: 2,
      batches: 1,
      debts: 1,
      goals: 2,
      reconciliation: 5,
    });

    // Every table the server pre-checks, spelled out (docs/29 F3) — budgets
    // deliberately absent (they hang off categories, not accounts).
    const blast = screen.getByTestId("delete-account-blast");
    const expected: Record<string, string> = {
      transactions: "12",
      trades: "3",
      lots: "2",
      batches: "1",
      debts: "1",
      goals: "2",
      reconciliation: "5",
    };
    for (const [key, value] of Object.entries(expected)) {
      const row = within(blast).getByTestId(`blast-${key}`);
      expect(within(row).getByText(value)).not.toBeNull();
    }
    expect(within(blast).queryByText(/budget/i)).toBeNull();

    // Attached: the refusal is quoted verbatim and the button cannot fire.
    expect(screen.getByTestId("blast-blocked").textContent).toContain(
      "Account is in use; reassign references first",
    );
    expect(
      (screen.getByTestId("confirm-delete-account") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(m.deleteAccount).not.toHaveBeenCalled();
  });

  it("stages a clean account's delete: hide → Undo restores, expiry commits once", async () => {
    await openConfirmDialog();
    expect(screen.queryByTestId("blast-blocked")).toBeNull();
    const confirm = screen.getByTestId("confirm-delete-account");
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(confirm);
    expect(screen.queryByRole("button", { name: "Delete RBC Main Chequing" })).toBeNull();
    expect(screen.getByTestId("undo-snackbar")).not.toBeNull();
    expect(m.deleteAccount).not.toHaveBeenCalled();

    // Undo restores the row — still nothing sent.
    fireEvent.click(screen.getByTestId("undo-snackbar-undo"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete RBC Main Chequing" })).not.toBeNull(),
    );
    expect(m.deleteAccount).not.toHaveBeenCalled();

    // Second pass: window expires → exactly one DELETE + accounts refetch.
    const accountsBefore = m.listAccounts.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Delete RBC Main Chequing" }));
    await screen.findByTestId("delete-account-blast");
    const confirm2 = screen.getByTestId("confirm-delete-account");
    await waitFor(() => expect((confirm2 as HTMLButtonElement).disabled).toBe(false));
    vi.useFakeTimers();
    fireEvent.click(confirm2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() => expect(m.deleteAccount).toHaveBeenCalledTimes(1));
    expect(m.deleteAccount).toHaveBeenCalledWith(7);
    await waitFor(() =>
      expect(m.listAccounts.mock.calls.length).toBeGreaterThan(accountsBefore),
    );
  });

  it("a 409 on commit restores the account and surfaces the envelope detail", async () => {
    await openConfirmDialog();
    const confirm = screen.getByTestId("confirm-delete-account");
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false));
    m.deleteAccount.mockRejectedValueOnce(
      new ApiError(409, "Account is in use; reassign references first", "account_in_use"),
    );

    vi.useFakeTimers();
    fireEvent.click(confirm);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(
        screen.getByText("Account is in use; reassign references first"),
      ).not.toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Delete RBC Main Chequing" })).not.toBeNull();
    expect(m.deleteAccount).toHaveBeenCalledTimes(1);
  });
});

describe("M95 P3 — sibling fix: the account editor dialog actually renders", () => {
  // Found while verifying the confirm dialog: BOTH dialogs in AccountsSection
  // wrapped `createPortal(...)` in `<AnimatePresence>`, and framer drops
  // non-element children (portals fail React.isValidElement) — Settings →
  // Accounts → New account did nothing, live-verified against the running
  // stack. The confirm dialog's tests above prove the same fix works.
  it("New account opens the editor dialog and Escape closes it", async () => {
    renderWithProviders(<AccountsSection />);
    await screen.findByRole("button", { name: "Delete RBC Main Chequing" });
    fireEvent.click(screen.getByRole("button", { name: "New account" }));
    await screen.findByRole("dialog", { name: "New account" });

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "New account" })).toBeNull(),
    );
  });
});

describe("M95 P3 — import batch revert", () => {
  async function renderBatches() {
    renderWithProviders(<BatchHistory onOpenBatch={vi.fn()} />);
    await screen.findByRole("button", { name: "Revert" });
  }

  it("stages the revert: row hides, nothing is sent, Undo restores order and count", async () => {
    await renderBatches();
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));

    expect(screen.queryByRole("button", { name: "Revert" })).toBeNull();
    expect(screen.queryByText("rbc-2026-01.csv")).toBeNull();
    expect(screen.getByTestId("undo-snackbar")).not.toBeNull();
    expect(screen.getByText(/Reverted import batch/)).not.toBeNull();
    expect(m.revertBatch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("undo-snackbar-undo"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Revert" })).not.toBeNull(),
    );
    expect(screen.getByText("rbc-2026-01.csv")).not.toBeNull();
    expect(m.revertBatch).not.toHaveBeenCalled();
  });

  it("commits the revert once when the window closes and refetches the batch list", async () => {
    await renderBatches();
    const before = m.listImports.mock.calls.length;
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));
    expect(m.revertBatch).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() => expect(m.revertBatch).toHaveBeenCalledTimes(1));
    expect(m.revertBatch).toHaveBeenCalledWith(3);
    await waitFor(() => expect(m.listImports.mock.calls.length).toBeGreaterThan(before));
    expect(screen.queryByText("rbc-2026-01.csv")).toBeNull();
  });

  it("a failed revert restores the batch and surfaces the server detail verbatim", async () => {
    await renderBatches();
    m.revertBatch.mockRejectedValueOnce(
      new ApiError(409, "Batch has reconciled transactions", "batch_locked"),
    );

    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UNDO_WINDOW_MS + 250);
    });
    vi.useRealTimers();

    await waitFor(() =>
      expect(screen.getByText("Batch has reconciled transactions")).not.toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Revert" })).not.toBeNull();
    expect(screen.getByText("rbc-2026-01.csv")).not.toBeNull();
  });
});
