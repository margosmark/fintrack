// demoRefusalLedger — docs/47 §4 / §13 for the LEDGER surfaces, pinned for
// security finding F-2's residual (card t_a51c9b96).
//
// The absolute pin: a `demo_account_restricted` response NEVER renders
// `role="alert"` or an `error-panel`. `components/ui/Toast.tsx` renders an
// error tone AS `role="alert"`, so an unguarded `toast.error(...)` for that
// one code is already a violation — every surface below is pinned for:
//
//   * exactly ONE `demo-refusal` (`role="status"`) + ONE `toast.info`;
//   * zero `role="alert"`, zero `error-panel`;
//   * the control whose ACTION is the blocked write wears `demo-lock` with
//     `aria-describedby` → a VISIBLE `DemoReason` (never `sr-only`);
//   * the write really goes out (the boundary stays server-authoritative);
//   * a LIVE tenant renders zero padlocks and zero reasons (§13 ex.1).
//
// The harness is m106ReviewQueue's (the real page, real ToastProvider, real
// React Query, only the API seam mocked) plus the two session scopes a
// component may read (`useAuth` and the M136 optional user scope).

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

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

/** The session BOTH scopes hand out (they must never disagree). */
const session = vi.hoisted(() => ({
  user: null as { id: string; email: string } | null,
}));

const m = vi.hoisted(() => ({
  listTransactions: vi.fn(),
  fetchAllTransactions: vi.fn(),
  deleteTransaction: vi.fn(),
  updateTransaction: vi.fn(),
  createTransaction: vi.fn(),
  createTransactionSplit: vi.fn(),
  updateTransactionSplit: vi.fn(),
  mergeTransactionSplit: vi.fn(),
  detectTransferPair: vi.fn(),
  linkTransfer: vi.fn(),
  unlinkTransfer: vi.fn(),
  setTransactionTags: vi.fn(),
  getTransactionTags: vi.fn(),
  getUnreviewedCount: vi.fn(),
  setTransactionReviewed: vi.fn(),
  listTransactionEvents: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  createTag: vi.fn(),
  listSuggestions: vi.fn(),
}));

// docs/47 §4: the guard reads the session through `useAuth`; the editor and
// the row verdicts read the M136 optional scope. Stub ONLY those two exports
// so every other binding (AuthProvider, AuthProvider's own exports) stays real.
vi.mock("../src/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({ user: session.user, setUser: () => undefined }),
  };
});
vi.mock("../src/auth/authUser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/auth/authUser")>();
  return { ...actual, useOptionalUser: () => session.user };
});

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: m.listTransactions,
  fetchAllTransactions: m.fetchAllTransactions,
  deleteTransaction: m.deleteTransaction,
  updateTransaction: m.updateTransaction,
  createTransaction: m.createTransaction,
  createTransactionSplit: m.createTransactionSplit,
  updateTransactionSplit: m.updateTransactionSplit,
  mergeTransactionSplit: m.mergeTransactionSplit,
  detectTransferPair: m.detectTransferPair,
  linkTransfer: m.linkTransfer,
  unlinkTransfer: m.unlinkTransfer,
  setTransactionTags: m.setTransactionTags,
  getTransactionTags: m.getTransactionTags,
  getUnreviewedCount: m.getUnreviewedCount,
  setTransactionReviewed: m.setTransactionReviewed,
  listTransactionEvents: m.listTransactionEvents,
}));
vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  listAccounts: m.listAccounts,
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: m.listCategories,
}));
vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: m.listTags,
  createTag: m.createTag,
}));
vi.mock("../src/api/suggestions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/suggestions")>()),
  listSuggestions: m.listSuggestions,
}));

import type { Account } from "../src/api/accounts";
import type { Transaction } from "../src/api/transactions";
import { ToastProvider } from "../src/components/ui/Toast";
import { Transactions } from "../src/pages/Transactions";

const DEMO_USER = { id: "1", email: "demo@fintracker.local" };
const LIVE_USER = { id: "9", email: "mark@example.com" };

/** The envelope the server really answers a refused demo write with. */
const DEMO_403 = {
  code: "demo_account_restricted",
  detail: "The demo account is read-only; changes are not allowed",
};

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

function txn(id: number, payee: string, reviewedAt: string | null = null): Transaction {
  return {
    id,
    date: "2026-01-05",
    normalized_payee: payee,
    raw_description: payee.toUpperCase(),
    amount_native: "-42.10",
    currency: "CAD",
    cad_amount: null,
    category_id: null,
    account_id: 7,
    transfer_pair_id: null,
    pair_account_name: null,
    tags: [],
    is_recurring: false,
    recurring_cadence: null,
    recurring_next_date: null,
    reviewed_at: reviewedAt,
    reviewed_by: reviewedAt === null ? null : 1,
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
  } as unknown as Transaction;
}

const ROWS = [txn(1, "Alpha Groceries"), txn(2, "Beta Gas")];

function page<T>(items: T[], total = items.length) {
  return { items, total, limit: 50, offset: 0 };
}

function renderPage(user: { id: string; email: string }): void {
  session.user = user;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ToastProvider>
          <Transactions />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  render(ui);
}

/** Everything that is error-shaped on screen right now. */
function errorAlerts(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="alert"]'));
}

/** ONE refusal element, `role="status"`, + the ONE toast.info, nothing else. */
async function expectRefusal(action: string, apiMock: { mock: { calls: unknown[][] } }) {
  const refusals = await screen.findAllByTestId("demo-refusal");
  expect(refusals).toHaveLength(1);
  expect(refusals[0].getAttribute("role")).toBe("status");
  expect(refusals[0].textContent).toContain("read-only");
  // The one toast.info — an error toast would be a `role="alert"` element.
  expect(screen.getByText(`${action} — the demo account is read-only`)).toBeTruthy();
  expect(errorAlerts()).toHaveLength(0);
  expect(screen.queryByTestId("error-panel")).toBeNull();
  // The write really went out: the boundary stays server-authoritative.
  await waitFor(() => expect(apiMock.mock.calls.length).toBeGreaterThan(0));
}

/** §4: padlock on the control, bound to a VISIBLE reason in the same panel. */
function expectLocked(control: HTMLElement, reasonId: string): void {
  expect(control.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
  expect(control.getAttribute("aria-describedby")).toBe(reasonId);
  const reason = document.getElementById(reasonId);
  expect(reason).not.toBeNull();
  expect(reason?.getAttribute("data-testid")).toBe("demo-reason");
  // F-4: never the sr-only clip that measured 1×1 px.
  expect(reason?.className ?? "").not.toContain("sr-only");
  expect(reason?.textContent ?? "").toContain("read-only");
}

beforeEach(() => {
  vi.clearAllMocks();
  session.user = DEMO_USER;
  sessionStorage.clear();
  localStorage.clear();
  m.listTransactions.mockResolvedValue(page(ROWS));
  m.fetchAllTransactions.mockResolvedValue(ROWS);
  m.deleteTransaction.mockResolvedValue(undefined);
  m.updateTransaction.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.createTransaction.mockResolvedValue(txn(9, "New"));
  m.createTransactionSplit.mockResolvedValue({});
  m.updateTransactionSplit.mockResolvedValue({});
  m.mergeTransactionSplit.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.detectTransferPair.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.linkTransfer.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.unlinkTransfer.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.setTransactionTags.mockResolvedValue({ items: [] });
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.getUnreviewedCount.mockResolvedValue(1);
  m.setTransactionReviewed.mockResolvedValue(txn(1, "Alpha Groceries", "2026-01-07T10:00:00Z"));
  m.listTransactionEvents.mockResolvedValue({ items: [], total: 0 });
  m.listAccounts.mockResolvedValue(page([ACCOUNT], 1));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page([], 0));
  m.createTag.mockResolvedValue({ id: 99, user_id: 1, name: "new" });
  m.listSuggestions.mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
  session.user = null;
  sessionStorage.clear();
});

describe("docs/47 §4 — ledger row writes wear the lock and refuse without an error", () => {
  it("demo: the row controls whose ACTION is the write wear the padlock, bound to ONE visible reason", async () => {
    renderPage(DEMO_USER);

    const toggle = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    expectLocked(toggle, "ledger-row-write-demo-reason");

    const del = screen.getByRole("button", { name: "Delete transaction 1" });
    expectLocked(del, "ledger-row-write-demo-reason");

    // The row category input PATCHes the row on selection (it carries no
    // accessible name of its own — the cell is the category column — so the
    // placeholder is its handle). Scope to THE ROW: the filter bar owns a
    // second CategoryPicker that dispatches nothing and must stay unlocked.
    const row = del.closest("tr") as HTMLElement;
    const category = within(row).getByPlaceholderText("Type to search…");
    // An <input> has no descendants, so the glyph sits in the picker's own
    // relative wrapper, level with it: the ARIA binding is on the input.
    expect(category.getAttribute("aria-describedby")).toBe("ledger-row-write-demo-reason");
    const pickerBox = category.parentElement as HTMLElement;
    expect(pickerBox.querySelector('[data-testid="demo-lock"]')).not.toBeNull();
    const rowReason = document.getElementById("ledger-row-write-demo-reason");
    expect(rowReason?.getAttribute("data-testid")).toBe("demo-reason");
    expect(rowReason?.textContent ?? "").toContain("read-only");

    // Exactly ONE visible reason serves them all, and the buttons stay ENABLED
    // (an inert control cannot explain itself).
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(1);
    expect(toggle.hasAttribute("disabled")).toBe(false);
    expect(del.hasAttribute("disabled")).toBe(false);
  });

  it("demo: the real 403 from the review toggle is ONE refusal + ONE toast.info, never role=alert", async () => {
    m.setTransactionReviewed.mockRejectedValue({ ...DEMO_403 });
    renderPage(DEMO_USER);

    const toggle = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    fireEvent.click(toggle);

    await expectRefusal("Review change", m.setTransactionReviewed);
  });

  it("demo: the deferred DELETE's 403 is a refusal too (dismiss commits the stage-2 write)", async () => {
    m.deleteTransaction.mockRejectedValue({ ...DEMO_403 });
    renderPage(DEMO_USER);

    const del = await screen.findByRole("button", { name: "Delete transaction 1" });
    fireEvent.click(del);
    // Stage 2 fires on Dismiss (immediately) rather than after the 5s window.
    const snackbar = await screen.findByTestId("undo-snackbar-dismiss");
    fireEvent.click(snackbar);

    await expectRefusal("Delete transaction", m.deleteTransaction);
  });

  it("demo: a refused quick-tag create is a refusal, never an error", async () => {
    m.createTag.mockRejectedValue({ ...DEMO_403 });
    renderPage(DEMO_USER);

    const addTags = await screen.findByRole("button", { name: /Add tags — Alpha Groceries/ });
    fireEvent.click(addTags);

    const input = await screen.findByRole("combobox", { name: "Add or create tag" });
    fireEvent.change(input, { target: { value: "brand-new" } });
    // The create row is a listbox OPTION and attaches on mousedown (M104).
    const createRow = await screen.findByRole("option", { name: 'Create "brand-new"' });
    fireEvent.mouseDown(createRow);

    await expectRefusal("Create tag", m.createTag);
  });
});

describe("docs/47 §4 — the transaction editor (F-2 item 1)", () => {
  it("demo: Save wears the padlock bound to a VISIBLE reason, and stays enabled", async () => {
    renderPage(DEMO_USER);

    fireEvent.click(await screen.findByRole("button", { name: "Edit transaction 1" }));
    const save = await screen.findByRole("button", { name: /Save changes/ });
    expect(save.hasAttribute("disabled")).toBe(false);
    expectLocked(save, "transaction-editor-save-demo-reason");
  });

  it("demo: a refused save renders the refusal INSIDE the dialog — one element, zero role=alert", async () => {
    m.updateTransaction.mockRejectedValue({ ...DEMO_403 });
    renderPage(DEMO_USER);

    fireEvent.click(await screen.findByRole("button", { name: "Edit transaction 1" }));
    const save = await screen.findByRole("button", { name: /Save changes/ });
    fireEvent.click(save);

    await expectRefusal("Save transaction", m.updateTransaction);
    // The refusal replaces the editor's error line — it lives in the dialog.
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector('[data-testid="demo-refusal"]')).not.toBeNull();
    expect(dialog.querySelector('[role="alert"]')).toBeNull();
  });

  it("live tenant: the editor and the rows render ZERO padlocks (§13 ex.1)", async () => {
    renderPage(LIVE_USER);

    fireEvent.click(await screen.findByRole("button", { name: "Edit transaction 1" }));
    await screen.findByRole("button", { name: /Save changes/ });

    expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-refusal"]')).toHaveLength(0);
  });
});

describe("docs/47 §4 — live tenant never wears a lock (§13 ex.1)", () => {
  it("zero padlocks, zero reasons, zero refusals on /transactions", async () => {
    renderPage(LIVE_USER);

    await screen.findByRole("button", { name: "Reviewed — Alpha Groceries (transaction 1)" });
    expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
  });
});
