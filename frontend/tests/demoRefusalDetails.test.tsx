// demoRefusalDetails — docs/47 §4 for AccountDetail and BillDetail
// (F-2 residual, card t_a51c9b96, item 5).
//
// The absolute pin: a `demo_account_restricted` response NEVER renders
// `role="alert"` or an `error-panel`. Toast renders an error tone AS
// `role="alert"`, so an unguarded `toast.error(...)` for that one code is
// already a violation.
//
// Pinned here:
//   * the controls whose ACTION is the blocked write wear `demo-lock` with
//     `aria-describedby` → a VISIBLE `DemoReason` (never `sr-only`) and stay
//     ENABLED on the demo tenant;
//   * a refused write renders exactly ONE `demo-refusal` (`role="status"`) +
//     ONE `toast.info`, zero `role="alert"`, zero `error-panel`;
//   * a LIVE tenant renders zero padlocks and zero reasons (§13 ex.1).
//
// The harnesses are m116BillDetail's (bill route + inline rule editor) and
// m118MarkerJump's (account route + activity table) — except Toast stays REAL,
// because which TONE reaches the DOM is the whole point.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

/** The session BOTH scopes hand out (they must never disagree). */
const session = vi.hoisted(() => ({
  user: null as { id: string; email: string } | null,
}));

const h = vi.hoisted(() => ({
  // --- bills ---------------------------------------------------------------
  getBillDetail: vi.fn(),
  updateBillRule: vi.fn(),
  suggestTerms: vi.fn(),
  // --- accounts ------------------------------------------------------------
  getAccount: vi.fn(),
  getAccountLedger: vi.fn(),
  getAccountValueHistory: vi.fn(),
  updateAccount: vi.fn(),
  listAccounts: vi.fn(),
  // --- transactions --------------------------------------------------------
  listTransactions: vi.fn(),
  deleteTransaction: vi.fn(),
  updateTransaction: vi.fn(),
  getTransactionTags: vi.fn(),
  setTransactionTags: vi.fn(),
  removeTransactionTag: vi.fn(),
  detectTransferPair: vi.fn(),
  unlinkTransfer: vi.fn(),
  getTransferSuggestions: vi.fn(),
  // --- categories / tags ---------------------------------------------------
  listCategories: vi.fn(),
  listTags: vi.fn(),
  createTag: vi.fn(),
}));

// --- module seams ----------------------------------------------------------
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
vi.mock("../src/api/bills", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/bills")>()),
  getBillDetail: h.getBillDetail,
  updateBillRule: h.updateBillRule,
  suggestTerms: h.suggestTerms,
}));
vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  getAccount: h.getAccount,
  getAccountLedger: h.getAccountLedger,
  getAccountValueHistory: h.getAccountValueHistory,
  updateAccount: h.updateAccount,
  listAccounts: h.listAccounts,
}));
vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: h.listTransactions,
  deleteTransaction: h.deleteTransaction,
  updateTransaction: h.updateTransaction,
  getTransactionTags: h.getTransactionTags,
  setTransactionTags: h.setTransactionTags,
  removeTransactionTag: h.removeTransactionTag,
  detectTransferPair: h.detectTransferPair,
  unlinkTransfer: h.unlinkTransfer,
  getTransferSuggestions: h.getTransferSuggestions,
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: h.listCategories,
}));
vi.mock("../src/api/tags", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/tags")>()),
  listTags: h.listTags,
  createTag: h.createTag,
}));
// The holdings tree pulls prices + the SSE stream — not the subject here.
vi.mock("../src/pages/Investments", () => ({ Investments: () => null }));

import { AccountDetail } from "../src/pages/AccountDetail";
import { BillDetail } from "../src/pages/BillDetail";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { ToastProvider } from "../src/components/ui/Toast";
import type { BillDetail as BillDetailBody } from "../src/api/bills";

const DEMO_USER = { id: "demo", email: "demo@fintracker.local" };
const LIVE_USER = { id: "9", email: "mark@example.com" };

/** The envelope the server really answers a refused demo write with. */
const DEMO_403 = {
  code: "demo_account_restricted",
  detail: "The demo account is read-only; changes are not allowed",
};

// jsdom has no matchMedia; ThemeProvider and framer-motion's reduced-motion
// probe both call it.
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

// A BANK account: `AccountDetail` picks `BankAccountView` for it (the
// investment view has no starting-balance form), and both views share the
// Activity table the row pins below.
const ACCOUNT = {
  id: 731,
  institution_id: null,
  name: "Chequing",
  type: "chequing",
  currency: "CAD",
  taxable: false,
  llm_autopilot: false,
  cash_balance: "0.00",
  investment_value: "0.00",
  total_value: "0.00",
  starting_balance: "0.00",
  name_cipher: null,
  last_sync_at: null,
  provider_status: null,
  pending_import_count: 0,
};

function txn(id: number, payee: string) {
  return {
    id,
    account_id: 731,
    date: "2026-01-05",
    posting_date: null,
    raw_description: payee.toUpperCase(),
    normalized_payee: payee,
    amount_native: "-42.10",
    currency: "CAD",
    cad_amount: "-42.10",
    category_id: null,
    transfer_pair_id: null,
    pair_account_name: null,
    has_receipt: false,
    tags: [],
    is_recurring: false,
    recurring_cadence: null,
    recurring_next_date: null,
    reviewed_at: null,
    reviewed_by: null,
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
    parent_split_id: null,
    is_split: false,
    note: null,
  };
}

function billDetail(): BillDetailBody {
  const item = {
    ...txn(101, "Netflix"),
    account_id: 5,
    raw_description: "NETFLIX.COM",
    normalized_payee: "NETFLIX.COM",
    amount_native: "-15.99",
    cad_amount: "-15.99",
  };
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
    transactions: { items: [item], total: 3, limit: 50, offset: 0, split_parents: [] },
    rolling: {
      frequency: "monthly",
      six: { periods: 6, window_periods: 3, payments: 3, total: "31.98", mean: "15.99", median: "15.99" },
      twelve: { periods: 12, window_periods: 3, payments: 3, total: "31.98", mean: "15.99", median: "15.99" },
    },
    chart: {
      frequency: "monthly",
      unit: "month",
      granularity: "month",
      first_period: "2026-01-01",
      last_period: "2026-03-01",
      points: [{ period: "2026-01-01", label: "Jan 2026", total: "15.99", count: 1, missed: false, rolling_six: "15.99" }],
    },
    stats: {
      all_time: { total: "31.98", payments: 2, average: "15.99", years: 1, yearly_average: "31.98" },
      ytd: { year: 2026, total: "31.98", payments: 2 },
    },
  } as unknown as BillDetailBody;
}

function providers(ui: ReactElement, initialEntries: string[], path: string): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={initialEntries}>
            <Routes>
              <Route path={path} element={ui} />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

function renderBill(): void {
  render(providers(<BillDetail />, ["/bills/7"], "/bills/:billId"));
}

function renderAccount(): void {
  render(providers(<AccountDetail />, ["/accounts/731"], "/accounts/:accountId"));
}

function errorAlerts(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="alert"]'));
}

/** ONE refusal element (`role="status"`) + the ONE toast.info, nothing else. */
async function expectRefusal(action: string, apiMock: { mock: { calls: unknown[][] } }) {
  const refusals = await screen.findAllByTestId("demo-refusal");
  expect(refusals).toHaveLength(1);
  expect(refusals[0].getAttribute("role")).toBe("status");
  expect(refusals[0].textContent).toContain("read-only");
  expect(screen.getByText(`${action} — the demo account is read-only`)).toBeTruthy();
  expect(errorAlerts()).toHaveLength(0);
  expect(screen.queryByTestId("error-panel")).toBeNull();
  await waitFor(() => expect(apiMock.mock.calls.length).toBeGreaterThan(0));
}

/** §4: padlock on the control, bound to a VISIBLE reason in the same panel. */
function expectLocked(control: HTMLElement, reasonId: string): void {
  const holder = control.querySelector('[data-testid="demo-lock"]')
    ? control
    : control.parentElement;
  expect(holder?.querySelector('[data-testid="demo-lock"]') ?? null).not.toBeNull();
  expect(control.getAttribute("aria-describedby")).toBe(reasonId);
  const reason = document.getElementById(reasonId);
  expect(reason).not.toBeNull();
  expect(reason?.getAttribute("data-testid")).toBe("demo-reason");
  expect(reason?.className ?? "").not.toContain("sr-only");
  expect(reason?.textContent ?? "").toContain("read-only");
}

function expectNoLocks(): void {
  expect(document.querySelectorAll('[data-testid="demo-lock"]')).toHaveLength(0);
  expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(0);
}

beforeEach(() => {
  vi.clearAllMocks();
  session.user = DEMO_USER;
  localStorage.clear();
  sessionStorage.clear();

  h.getBillDetail.mockResolvedValue(billDetail());
  h.updateBillRule.mockResolvedValue({});
  h.suggestTerms.mockResolvedValue([]);
  h.getAccount.mockResolvedValue(ACCOUNT);
  h.getAccountLedger.mockResolvedValue({ items: [txn(456, "Wealthsimple")], starting_balance: "0.00", current_balance: "0.00", total: 1 });
  h.getAccountValueHistory.mockResolvedValue({
    account_id: 731,
    from: "2026-01-01",
    to: "2026-06-15",
    granularity: "month",
    requested_granularity: "month",
    currency: "CAD",
    native_currency: "CAD",
    min_event_value: "0.00",
    has_data: false,
    buckets: [],
    events: [],
  });
  h.updateAccount.mockResolvedValue(ACCOUNT);
  h.listAccounts.mockResolvedValue({ items: [ACCOUNT], total: 1, limit: 200, offset: 0 });
  h.listTransactions.mockResolvedValue({ items: [txn(456, "Wealthsimple")], total: 1, limit: 50, offset: 0, split_parents: [] });
  h.deleteTransaction.mockResolvedValue(undefined);
  h.updateTransaction.mockResolvedValue(txn(456, "Wealthsimple"));
  h.getTransactionTags.mockResolvedValue({ items: [] });
  h.setTransactionTags.mockResolvedValue({ items: [] });
  h.removeTransactionTag.mockResolvedValue(undefined);
  h.detectTransferPair.mockResolvedValue(txn(456, "Wealthsimple"));
  h.unlinkTransfer.mockResolvedValue(txn(456, "Wealthsimple"));
  // A PAGE shape, not a bare array — BankAccountView reads `.items.length`.
  h.getTransferSuggestions.mockResolvedValue({ items: [], total: 0 });
  h.listCategories.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.listTags.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.createTag.mockResolvedValue({ id: 9, user_id: 1, name: "new" });
});

afterEach(() => {
  vi.clearAllMocks();
  session.user = null;
});

describe("docs/47 §4 — BillDetail rule editor (F-2 item 5)", () => {
  it("demo: Save rule and Test both wear the padlock bound to ONE visible reason, and stay enabled", async () => {
    renderBill();
    fireEvent.click(await screen.findByRole("button", { name: "Edit rule for Netflix" }));

    const save = await screen.findByRole("button", { name: /Save rule/ });
    const test = await screen.findByRole("button", { name: /Test/ });
    expect(save.hasAttribute("disabled")).toBe(false);
    expect(test.hasAttribute("disabled")).toBe(false);
    expectLocked(save, "bill-rule-write-demo-reason");
    expectLocked(test, "bill-rule-write-demo-reason");
    expect(document.querySelectorAll('[data-testid="demo-reason"]')).toHaveLength(1);
  });

  it("demo: a refused save is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    h.updateBillRule.mockRejectedValue({ ...DEMO_403 });
    renderBill();
    fireEvent.click(await screen.findByRole("button", { name: "Edit rule for Netflix" }));
    const save = await screen.findByRole("button", { name: /Save rule/ });
    fireEvent.click(save);

    await expectRefusal("Save rule", h.updateBillRule);
  });

  it("live tenant: ZERO padlocks and ZERO reasons (§13 ex.1)", async () => {
    session.user = LIVE_USER;
    renderBill();
    fireEvent.click(await screen.findByRole("button", { name: "Edit rule for Netflix" }));
    await screen.findByRole("button", { name: /Save rule/ });
    expectNoLocks();
  });
});

describe("docs/47 §4 — AccountDetail (F-2 item 5)", () => {
  /** The card's edit opener — the money label is formatted (and masked), so
   *  the hint line is the stable handle. */
  async function openStartingBalance(): Promise<void> {
    const hint = await screen.findByText("Balance before the earliest imported transaction");
    const row = hint.closest("div.flex") as HTMLElement;
    fireEvent.click(within(row).getByRole("button"));
  }

  it("demo: the starting-balance Save wears the padlock bound to a VISIBLE reason", async () => {
    renderAccount();
    await openStartingBalance();
    const save = (await screen.findByRole("button", { name: /^Save$/ })) as HTMLElement;
    expect(save.hasAttribute("disabled")).toBe(false);
    expectLocked(save, "account-starting-balance-demo-reason");
  });

  it("demo: a refused starting-balance save is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    h.updateAccount.mockRejectedValue({ ...DEMO_403 });
    renderAccount();
    await openStartingBalance();
    const save = await screen.findByRole("button", { name: /^Save$/ });
    fireEvent.click(save);

    await expectRefusal("Update starting balance", h.updateAccount);
  });

  it("demo: the row Delete control wears the padlock bound to the row reason", async () => {
    renderAccount();
    const del = await screen.findByRole("button", { name: "Delete transaction 456" });
    expectLocked(del, "account-activity-row-write-demo-reason");
    // An IDREF may be shared by every row control: ONE visible reason serves
    // them (the card may carry its own for a different control, so pin THIS
    // id's cardinality rather than the page total).
    const reasons = [...document.querySelectorAll('[data-testid="demo-reason"]')];
    expect(reasons.filter((r) => r.id === "account-activity-row-write-demo-reason")).toHaveLength(1);
    expect(del.hasAttribute("disabled")).toBe(false);
  });

  it("demo: the deferred DELETE's 403 is ONE demo-refusal + ONE toast.info, never role=alert", async () => {
    h.deleteTransaction.mockRejectedValue({ ...DEMO_403 });
    renderAccount();
    const del = await screen.findByRole("button", { name: "Delete transaction 456" });
    fireEvent.click(del);
    // Stage 2 fires on Dismiss (immediately) rather than after the 5s window.
    fireEvent.click(await screen.findByTestId("undo-snackbar-dismiss"));

    await expectRefusal("Delete transaction", h.deleteTransaction);
  });

  it("live tenant: ZERO padlocks and ZERO reasons (§13 ex.1)", async () => {
    session.user = LIVE_USER;
    renderAccount();
    await screen.findByRole("button", { name: "Delete transaction 456" });
    expectNoLocks();
  });
});
