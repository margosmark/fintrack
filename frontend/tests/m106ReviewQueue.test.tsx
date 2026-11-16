// M106 (docs/09 §Review queue & activity log) — the two surfaces on
// `/transactions`:
//
//  1. the row-level review toggle: `aria-pressed` + a per-row label, one click
//     → one `setTransactionReviewed` call, no modal;
//  2. the queue chip: a FILTER shortcut that applies immediately and carries
//     the same count the header hint shows;
//  3. the "N unreviewed" header hint: visible while there is work, dismissible
//     for the SESSION (sessionStorage), silent at zero;
//  4. the History drawer's two entry points (row Actions cell + edit modal).

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
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
  updateTransaction: vi.fn(),
  createTransaction: vi.fn(),
  detectTransferPair: vi.fn(),
  linkTransfer: vi.fn(),
  unlinkTransfer: vi.fn(),
  setTransactionTags: vi.fn(),
  getTransactionTags: vi.fn(),
  getUnreviewedCount: vi.fn(),
  setTransactionReviewed: vi.fn(),
  listTransactionEvents: vi.fn(),
  listAccounts: vi.fn(),
  listInstitutions: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  createTag: vi.fn(),
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
  updateTransaction: m.updateTransaction,
  createTransaction: m.createTransaction,
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

const ROWS = [txn(1, "Alpha Groceries"), txn(2, "Beta Gas"), txn(3, "Gamma Hydro", "2026-01-06T10:00:00Z")];

function page<T>(items: T[], total = items.length) {
  return { items, total, limit: 50, offset: 0 };
}

function renderPage() {
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
  return queryClient;
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  m.listTransactions.mockResolvedValue(page(ROWS));
  m.fetchAllTransactions.mockResolvedValue(ROWS);
  m.deleteTransaction.mockResolvedValue(undefined);
  m.updateTransaction.mockImplementation((id: number, patch: Transaction) =>
    Promise.resolve({ ...ROWS.find((row) => row.id === id), ...patch }),
  );
  m.createTransaction.mockResolvedValue(txn(9, "New"));
  m.detectTransferPair.mockRejectedValue(new Error("no match"));
  m.linkTransfer.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.unlinkTransfer.mockResolvedValue(txn(1, "Alpha Groceries"));
  m.setTransactionTags.mockResolvedValue({ items: [] });
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.getUnreviewedCount.mockResolvedValue(2);
  m.setTransactionReviewed.mockImplementation((id: number, reviewed: boolean) =>
    Promise.resolve(
      txn(id, "x", reviewed ? "2026-01-07T10:00:00Z" : null),
    ),
  );
  m.listTransactionEvents.mockResolvedValue({
    items: [
      {
        id: 11,
        tx_id: 1,
        event_kind: "categorized",
        actor: 1,
        actor_label: "you",
        at: "2026-01-07T10:00:00Z",
        payload_hash: "a".repeat(64),
        summary: "you set category Groceries",
        payload: { category_id: 3, category_name: "Groceries" },
      },
      {
        id: 10,
        tx_id: 1,
        event_kind: "created",
        actor: 1,
        actor_label: "you",
        at: "2026-01-05T10:00:00Z",
        payload_hash: "b".repeat(64),
        summary: "you created this from import Batch #412",
        payload: { source: "import", batch_id: 412 },
      },
    ],
    total: 2,
  });
  m.listAccounts.mockResolvedValue(page([ACCOUNT], 1));
  m.listInstitutions.mockResolvedValue(page([]));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page([], 0));
  m.createTag.mockResolvedValue({ id: 99, user_id: 1, name: "new" });
  m.listSuggestions.mockResolvedValue([]);
  m.listTrades.mockResolvedValue(page([], 0));
  m.listAcbPools.mockResolvedValue(page([], 0));
  m.listImports.mockResolvedValue(page([], 0));
  m.revertBatch.mockResolvedValue(undefined);
  m.listDebts.mockResolvedValue([]);
  m.listGoals.mockResolvedValue([]);
  m.getReconciliation.mockResolvedValue({
    items: [],
    summary: { total: 0, matched: 0, unmatched: 0, matched_sum: "0.00", unmatched_sum: "0.00" },
  });
});

afterEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
});

describe("M106 row review toggle", () => {
  it("is a per-row toggle: constant label, aria-pressed carries the state, ≥44px below md", async () => {
    renderPage();
    const unreviewed = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    const reviewed = screen.getByRole("button", {
      name: "Reviewed — Gamma Hydro (transaction 3)",
    });
    expect(unreviewed.getAttribute("aria-pressed")).toBe("false");
    expect(reviewed.getAttribute("aria-pressed")).toBe("true");
    // Touch floor is declared on the BUTTON (the clickable thing), the same
    // shape the quick-tag control uses (docs/09 §Review queue).
    expect(unreviewed.className).toContain("min-h-11");
    expect(unreviewed.className).toContain("min-w-11");
    expect(unreviewed.className).toContain("md:min-h-7");
  });

  it("flips through the ONE single-row route the bulk toolbar also drives", async () => {
    renderPage();
    const toggle = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    fireEvent.click(toggle);
    await waitFor(() => expect(m.setTransactionReviewed).toHaveBeenCalledTimes(1));
    expect(m.setTransactionReviewed).toHaveBeenCalledWith(1, true);
  });

  // jsdom implements no keyboard DEFAULT ACTIONS (Enter on a button does not
  // dispatch click there), so this pins the contract that makes Enter/Space
  // work in a real browser: a native <button>, in the tab order, enabled.
  // The real keystroke is proved on the running stack by
  // `scripts/a11y/m106_review_probe.py` ("keyboard-only flip").
  it("is keyboard-operable — a native button in the tab order, never a roving div", async () => {
    renderPage();
    const toggle = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle.getAttribute("type")).toBe("button");
    expect(toggle.tabIndex).toBe(0);
    expect(toggle.hasAttribute("disabled")).toBe(false);
    expect(toggle.getAttribute("tabindex")).not.toBe("-1");
    toggle.focus();
    expect(document.activeElement).toBe(toggle);
  });
});

describe("M106 queue chip + header hint", () => {
  it("shows the count on BOTH surfaces and applies the filter on click", async () => {
    renderPage();
    const hint = await screen.findByText("2 unreviewed");
    expect(hint).toBeTruthy();
    const chip = screen.getByRole("button", { name: /Needs review/ });
    expect(chip.getAttribute("aria-pressed")).toBe("false");
    expect(chip.textContent).toContain("2");

    fireEvent.click(chip);
    await waitFor(() => {
      const last = m.listTransactions.mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(last.reviewed).toBe(false);
    });
    expect(chip.getAttribute("aria-pressed")).toBe("true");
  });

  it("dismisses for the SESSION only — sessionStorage, not localStorage", async () => {
    renderPage();
    const dismiss = await screen.findByRole("button", { name: "Dismiss the unreviewed hint" });
    fireEvent.click(dismiss);
    expect(screen.queryByText("2 unreviewed")).toBeNull();
    // F-6: the key carries the tenant scope — this harness signs nobody in,
    // so `tenantScope(null)` resolves to the "signed-out" bucket and the
    // dismissal can never collide with a signed-in tenant's key.
    expect(
      sessionStorage.getItem("fintrack.m106.unreviewed-hint.dismissed.signed-out"),
    ).toBe("1");
    expect(
      localStorage.getItem("fintrack.m106.unreviewed-hint.dismissed.signed-out"),
    ).toBeNull();
  });

  it("is silent at zero — nothing to review is not a number worth printing", async () => {
    m.getUnreviewedCount.mockResolvedValue(0);
    renderPage();
    await screen.findByRole("button", { name: /Needs review/ });
    expect(screen.queryByText("0 unreviewed")).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss the unreviewed hint" })).toBeNull();
  });

  // docs/09 §Review queue: "one number, two surfaces" is a promise about the
  // moment AFTER a change, not just about the first paint — the hint must
  // re-read the endpoint whenever the rows move, or the pill prints yesterday's
  // number over a table that already disagrees with it.
  it("updates BOTH surfaces live after a single-row flip", async () => {
    m.getUnreviewedCount.mockResolvedValueOnce(2).mockResolvedValue(1);
    renderPage();
    await screen.findByText("2 unreviewed");

    const toggle = await screen.findByRole("button", {
      name: "Reviewed — Alpha Groceries (transaction 1)",
    });
    fireEvent.click(toggle);

    const hint = await screen.findByText("1 unreviewed");
    expect(hint).toBeTruthy();
    expect(screen.getByRole("button", { name: /Needs review/ }).textContent).toContain("1");
    // The number was RE-READ (not merely re-rendered from the old payload).
    expect(m.getUnreviewedCount.mock.calls.length).toBeGreaterThan(1);
  });

  it("bulk 'Mark reviewed' moves the count too — N rows, one number", async () => {
    // Two of the three rows are unreviewed; reviewing all three empties the
    // queue, so BOTH surfaces must go quiet (silent at zero, no stale pill).
    m.getUnreviewedCount.mockResolvedValueOnce(2).mockResolvedValue(0);
    renderPage();
    await screen.findByText("2 unreviewed");

    fireEvent.click(screen.getByLabelText("Select every transaction on this page"));
    const markReviewed = await screen.findByRole("button", {
      name: "Mark 3 selected transactions reviewed",
    });
    fireEvent.click(markReviewed);

    // N rows are N single-row requests — every row gets its own flip (and
    // therefore its own `reviewed` receipt server-side).
    await waitFor(() => expect(m.setTransactionReviewed).toHaveBeenCalledTimes(3));
    expect(m.setTransactionReviewed).toHaveBeenCalledWith(1, true);
    expect(m.setTransactionReviewed).toHaveBeenCalledWith(2, true);
    expect(m.setTransactionReviewed).toHaveBeenCalledWith(3, true);

    // The count surface moved with them: the stale "2" is gone, nothing is
    // printed at zero, and the chip's pill disappeared with it.
    await waitFor(() => expect(screen.queryByText("2 unreviewed")).toBeNull());
    expect(screen.queryByText("0 unreviewed")).toBeNull();
    expect(m.getUnreviewedCount.mock.calls.length).toBeGreaterThan(1);
    const chip = screen.getByRole("button", { name: /Needs review/ });
    expect(chip.textContent).not.toContain("2");
    expect(chip.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("M106 History drawer entry points", () => {
  it("opens from the row's Actions cell and renders the receipts newest first", async () => {
    renderPage();
    const opener = await screen.findByRole("button", {
      name: "History — Alpha Groceries (transaction 1)",
    });
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const list = await within(dialog).findAllByRole("listitem");
    expect(list).toHaveLength(2);
    expect(list[0].textContent).toContain("you set category Groceries");
    expect(list[1].textContent).toContain("you created this from import Batch #412");
    expect(m.listTransactionEvents).toHaveBeenCalledWith(1);
  });

  it("opens from the edit modal's footer — one dialog at a time", async () => {
    renderPage();
    const edit = await screen.findByRole("button", { name: "Edit transaction 1" });
    fireEvent.click(edit);
    const editor = await screen.findByRole("dialog");
    const history = within(editor).getByRole("button", { name: "History for this transaction" });
    fireEvent.click(history);
    // The editor hands the row over and closes first.
    await screen.findByRole("dialog", { name: "History" });
    expect(screen.queryByRole("dialog", { name: /Edit|New transaction/i })).toBeNull();
    expect(m.listTransactionEvents).toHaveBeenCalledWith(1);
  });
});

describe("M106 History drawer body", () => {
  it("renders a unicode receipt sentence verbatim — accents, emoji, CJK", async () => {
    m.listTransactionEvents.mockResolvedValue({
      items: [
        {
          id: 12,
          tx_id: 1,
          event_kind: "categorized",
          actor: 1,
          actor_label: "you",
          at: "2026-01-07T10:00:00Z",
          payload_hash: "c".repeat(64),
          summary: "you set category Café ☕ 食品 😀",
          payload: { category_id: 3, category_name: "Café ☕ 食品 😀" },
        },
      ],
      total: 1,
    });
    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "History — Alpha Groceries (transaction 1)" }),
    );
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.textContent).toContain("you set category Café ☕ 食品 😀"));
    // Escaped or transcoded text would land as `CafÃ©` or ` Café`, never as
    // these exact characters, and the receipt line beside it still reads.
    expect(dialog.textContent).not.toContain("CafÃ©");
    expect(dialog.textContent).toContain(`categorized · ${"c".repeat(12)}`);
    expect(dialog.querySelector("[title]")?.getAttribute("title")).toBe(
      `receipt ${"c".repeat(64)}`,
    );
  });

  it("shows only the row that was opened — a second row fetches its own history", async () => {
    m.listTransactionEvents.mockImplementation((id: number) =>
      Promise.resolve({
        items: [
          {
            id: 100 + id,
            tx_id: id,
            event_kind: "edited",
            actor: 1,
            actor_label: "you",
            at: "2026-01-07T10:00:00Z",
            payload_hash: String(id).repeat(64),
            summary: `receipt for row ${id}`,
            payload: {},
          },
        ],
        total: 1,
      }),
    );
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "History — Alpha Groceries (transaction 1)" }),
    );
    let dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.textContent).toContain("receipt for row 1"));
    expect(dialog.textContent).not.toContain("receipt for row 2");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close history" }));

    fireEvent.click(await screen.findByRole("button", { name: "History — Beta Gas (transaction 2)" }));
    dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.textContent).toContain("receipt for row 2"));
    // A different queryKey per row: row 1's cached lines must not bleed in.
    expect(dialog.textContent).not.toContain("receipt for row 1");
    expect(m.listTransactionEvents).toHaveBeenLastCalledWith(2);
    expect(m.listTransactionEvents).not.toHaveBeenCalledWith(3);
  });
});

describe("M106 History entry-point page contract", () => {
  // The drawer is specified per SURFACE, not per component: every list that
  // edits a row must offer both links. Same source-contract style as the M64
  // table wrapper check (tests/tableScroller.test.tsx).
  const surfaces = ["src/pages/Transactions.tsx", "src/pages/AccountDetail.tsx"].map((path) => ({
    file: path.split("/").pop(),
    src: readFileSync(path, "utf8"),
  }));

  it.each(surfaces)("$file mounts the drawer", ({ src }) => {
    expect(src).toContain("<HistoryDrawer");
  });

  it.each(surfaces)("$file links History from the edit modal (onHistory)", ({ src }) => {
    expect(src).toMatch(/<TransactionEditor[\s\S]*?onHistory=\{/);
  });

  it.each(surfaces)("$file links History from the row's action cell", ({ src }) => {
    expect(src).toMatch(/aria-label=\{`History — /);
  });
});
