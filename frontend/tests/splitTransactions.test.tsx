// M107 — split transactions (docs/44). The SIX NAMED tests are the contract
// (docs/44 §Named tests), so this file contains exactly those names:
//
//   splitGrouping        one header per group, children id ASC on expand,
//                        header checkbox selects the page's children
//   splitEditor          `Remaining:` in exact Decimal arithmetic, submit
//                        disabled until 0.00, the server's split_sum_mismatch
//                        envelope rendered verbatim (detail + code)
//   (splitReportParity retired with the client report engines, M160)
//   splitInvestmentHide  no Split affordance on an investment row + a clean
//                        accessible-name sweep of that row
//   splitA11y            aria-expanded / aria-controls wiring, labelled
//                        region, axe-core 0 violations open, both themes
//   splitPartnerFeed     /shared groups the same way; privacy mode shows 🔒
//
// The API mock EMULATES the server (docs/44 §4.2 validation order, §3.1 sum
// rule, §6.1 LEDGER population, le=200 paging): it rejects nothing the real
// one would accept and paginates page-for-page.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";

const m = vi.hoisted(() => ({
  listTransactions: vi.fn(),
  fetchAllTransactions: vi.fn(),
  fetchAllTransactionPages: vi.fn(),
  createTransaction: vi.fn(),
  updateTransaction: vi.fn(),
  deleteTransaction: vi.fn(),
  detectTransferPair: vi.fn(),
  linkTransfer: vi.fn(),
  unlinkTransfer: vi.fn(),
  setTransactionTags: vi.fn(),
  getTransactionTags: vi.fn(),
  getUnreviewedCount: vi.fn(),
  setTransactionReviewed: vi.fn(),
  listTransactionEvents: vi.fn(),
  getRecurringCandidates: vi.fn(),
  createTransactionSplit: vi.fn(),
  updateTransactionSplit: vi.fn(),
  getTransactionSplit: vi.fn(),
  mergeTransactionSplit: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  createTag: vi.fn(),
  listSuggestions: vi.fn(),
  loadSharedView: vi.fn(),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, setUser: () => {} }),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: m.listTransactions,
  fetchAllTransactions: m.fetchAllTransactions,
  fetchAllTransactionPages: m.fetchAllTransactionPages,
  createTransaction: m.createTransaction,
  updateTransaction: m.updateTransaction,
  deleteTransaction: m.deleteTransaction,
  detectTransferPair: m.detectTransferPair,
  linkTransfer: m.linkTransfer,
  unlinkTransfer: m.unlinkTransfer,
  setTransactionTags: m.setTransactionTags,
  getTransactionTags: m.getTransactionTags,
  getUnreviewedCount: m.getUnreviewedCount,
  setTransactionReviewed: m.setTransactionReviewed,
  listTransactionEvents: m.listTransactionEvents,
  getRecurringCandidates: m.getRecurringCandidates,
  createTransactionSplit: m.createTransactionSplit,
  updateTransactionSplit: m.updateTransactionSplit,
  getTransactionSplit: m.getTransactionSplit,
  mergeTransactionSplit: m.mergeTransactionSplit,
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
vi.mock("../src/api/sharing", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/sharing")>()),
  loadSharedView: m.loadSharedView,
}));

import { ApiError } from "../src/api/client";
import type { Account } from "../src/api/accounts";
import type { Category } from "../src/api/categories";
import type {
  SplitChildInput,
  SplitParentPublic,
  Transaction,
  TransactionPage,
  TransactionSplit,
} from "../src/api/transactions";
import type { SharedView } from "../src/api/sharing";
import { Transactions } from "../src/pages/Transactions";
import { SharedHousehold } from "../src/pages/SharedHousehold";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { remainingLabel } from "../src/components/transactions/SplitEditor";

// ── jsdom shims (framer-motion asks for matchMedia) ──────────────────────────
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
});

// ── fixtures ────────────────────────────────────────────────────────────────

const CHEQUING: Account = {
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

const TFSA: Account = {
  ...CHEQUING,
  id: 8,
  name: "Wealthsimple TFSA",
  type: "tfsa",
  taxable: false,
} as Account;

const CATEGORIES: Category[] = [
  { id: 1, user_id: 1, name: "Groceries", parent_id: null, is_system: false },
  { id: 2, user_id: 1, name: "Apparel", parent_id: null, is_system: false },
  { id: 3, user_id: 1, name: "Health", parent_id: null, is_system: false },
] as Category[];

function makeTx(args: {
  id: number;
  date: string;
  amount: string;
  payee: string;
  accountId?: number;
  categoryId?: number | null;
  parentSplitId?: number | null;
  note?: string | null;
}): Transaction {
  return {
    id: args.id,
    account_id: args.accountId ?? CHEQUING.id,
    date: args.date,
    posting_date: null,
    raw_description: args.payee.toUpperCase(),
    normalized_payee: args.payee,
    amount_native: args.amount,
    currency: "CAD",
    cad_amount: args.amount,
    category_id: args.categoryId ?? null,
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
    parent_split_id: args.parentSplitId ?? null,
    is_split: false,
    note: args.note ?? null,
  };
}

function makeParent(): SplitParentPublic {
  return {
    ...makeTx({
      id: 51,
      date: "2026-01-05",
      amount: "-187.42",
      payee: "COSTCO #123",
      categoryId: 1, // inert while split (docs/44 D5) — never rendered
      note: "bulk shop",
    }),
    is_split: true,
    child_count: 3,
  };
}

/**
 * One LEDGER fixture for every test: a three-child split, two ordinary rows
 * the editor drives (`-187.42` and the float-trap `+0.30`), and one row in an
 * investment account. `db.parents` is NEVER part of `items` (LEDGER, D2).
 */
const db = {
  ordinary: [] as Transaction[],
  parents: [] as SplitParentPublic[],
  children: [] as Transaction[],
};

function seed(): void {
  db.ordinary = [
    makeTx({ id: 60, date: "2026-01-06", amount: "-187.42", payee: "Metro", categoryId: 1 }),
    makeTx({ id: 61, date: "2026-01-06", amount: "0.30", payee: "Refund" }),
    makeTx({ id: 70, date: "2026-01-07", amount: "-50.00", payee: "Wealthsimple", accountId: TFSA.id }),
  ];
  db.parents = [makeParent()];
  db.children = [
    makeTx({ id: 814, date: "2026-01-05", amount: "-27.42", payee: "COSTCO #123", categoryId: 3, parentSplitId: 51 }),
    makeTx({ id: 813, date: "2026-01-05", amount: "-40.00", payee: "COSTCO #123", categoryId: 2, parentSplitId: 51 }),
    makeTx({ id: 812, date: "2026-01-05", amount: "-120.00", payee: "COSTCO #123", categoryId: 1, parentSplitId: 51, note: "kids clothes" }),
  ];
}

// ── the fake server ─────────────────────────────────────────────────────────

/** Exact cents — no float ever decides a sum here either. */
function cents(text: string): bigint {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text.trim());
  if (match === null) throw new Error(`not a decimal: ${text}`);
  const fraction = (match[3] ?? "").padEnd(2, "0").slice(0, 2);
  const value = BigInt(match[2]) * 100n + BigInt(fraction);
  return match[1] === "-" ? -value : value;
}

function sumText(values: string[]): string {
  const total = values.reduce((acc, value) => acc + cents(value), 0n);
  const negative = total < 0n;
  const magnitude = negative ? -total : total;
  return `${negative ? "-" : ""}${magnitude / 100n}.${String(magnitude % 100n).padStart(2, "0")}`;
}

function reject(status: number, detail: string, code: string): Promise<never> {
  return Promise.reject(new ApiError(status, detail, code));
}

const INVESTMENT_TYPES = new Set(["tfsa", "rrsp", "fhsa", "resp", "lira", "rdsp", "brokerage_non_registered"]);

function accountTypeOf(accountId: number): string {
  return accountId === TFSA.id ? TFSA.type : CHEQUING.type;
}

/** LEDGER = children + ordinary rows; a split parent is never in `items`. */
function ledgerRows(filters: Record<string, unknown> = {}): Transaction[] {
  let rows = [...db.ordinary, ...db.children];
  const accountId = filters.accountId as number | undefined;
  if (accountId !== undefined) rows = rows.filter((row) => row.account_id === accountId);
  const categoryId = filters.categoryId as number | "uncategorized" | undefined;
  if (categoryId !== undefined) {
    rows = rows.filter((row) =>
      categoryId === "uncategorized" ? row.category_id === null : row.category_id === categoryId,
    );
  }
  const payee = filters.payee as string | undefined;
  if (payee !== undefined && payee.trim() !== "") {
    const needle = payee.trim().toLowerCase();
    rows = rows.filter((row) => row.normalized_payee.toLowerCase().includes(needle));
  }
  // docs/08 page order: `date desc, id desc`.
  rows.sort((a, b) => (a.date === b.date ? b.id - a.id : a.date < b.date ? 1 : -1));
  return rows;
}

function listTransactionsImpl(
  filters: Record<string, unknown> = {},
  paging: { limit?: number; offset?: number } = {},
): Promise<TransactionPage> {
  const limit = paging.limit ?? 50;
  const offset = paging.offset ?? 0;
  if (limit < 1 || limit > 200) {
    return reject(422, "limit must be between 1 and 200", "validation_error");
  }
  const rows = ledgerRows(filters);
  const items = rows.slice(offset, offset + limit);
  const parentIds = new Set(
    items.map((item) => item.parent_split_id).filter((id): id is number => id !== null),
  );
  return Promise.resolve({
    items,
    total: rows.length,
    limit,
    offset,
    split_parents: db.parents.filter((parent) => parentIds.has(parent.id)),
  });
}

async function fetchAllPagesImpl(
  filters: Record<string, unknown> = {},
  pageSize = 200,
): Promise<{ items: Transaction[]; split_parents: SplitParentPublic[] }> {
  const items: Transaction[] = [];
  const parents = new Map<number, SplitParentPublic>();
  let offset = 0;
  for (;;) {
    const page = await listTransactionsImpl(filters, { limit: pageSize, offset });
    items.push(...page.items);
    for (const parent of page.split_parents) parents.set(parent.id, parent);
    offset += page.items.length;
    if (page.items.length === 0 || items.length >= page.total) break;
  }
  return { items, split_parents: [...parents.values()] };
}

function findRow(id: number): Transaction | undefined {
  return [...db.ordinary, ...db.children, ...db.parents].find((row) => row.id === id);
}

/** GET /transactions/{id}/split — §4.2 validation order, steps 1…5. */
function getSplitImpl(id: number): Promise<TransactionSplit> {
  const row = findRow(id);
  if (row === undefined) return reject(404, "Transaction not found", "not_found");
  if (row.parent_split_id !== null) {
    return reject(422, "A split part cannot be split again", "split_nested");
  }
  if (INVESTMENT_TYPES.has(accountTypeOf(row.account_id))) {
    return reject(
      422,
      `Account type ${accountTypeOf(row.account_id)} cannot be split`,
      "split_account_not_allowed",
    );
  }
  if (!row.is_split) return reject(409, "This transaction is not split", "not_split");
  const parent = db.parents.find((candidate) => candidate.id === id);
  if (parent === undefined) return reject(409, "This transaction is not split", "not_split");
  const children = db.children
    .filter((child) => child.parent_split_id === id)
    .sort((a, b) => a.id - b.id);
  const checksum = parent.amount_native;
  return Promise.resolve({
    parent,
    children,
    split_checksum: checksum,
    sum_ok: sumText(children.map((child) => child.amount_native)) === sumText([checksum]),
  });
}

/**
 * POST /transactions/{id}/split — §4.2 validation order, §3.1 sum rule.
 * The parent is read AT CALL TIME, so a row changed server-side after the
 * page loaded is refused exactly like the real one refuses it.
 */
function createSplitImpl(id: number, children: SplitChildInput[]): Promise<TransactionSplit> {
  const row = findRow(id);
  if (row === undefined) return reject(404, "Transaction not found", "not_found");
  if (row.parent_split_id !== null) {
    return reject(422, "A split part cannot be split again", "split_nested");
  }
  if (INVESTMENT_TYPES.has(accountTypeOf(row.account_id))) {
    return reject(
      422,
      `Account type ${accountTypeOf(row.account_id)} cannot be split`,
      "split_account_not_allowed",
    );
  }
  if (row.is_split) return reject(409, "This transaction is already split", "already_split");
  if (children.length < 2) return reject(422, "A split needs at least 2 parts", "split_too_few_legs");
  if (children.length > 20) return reject(422, "A split allows at most 20 parts", "split_too_many_legs");
  const expected = cents(row.amount_native);
  if (expected === 0n) return reject(422, "A zero-amount row cannot be split", "split_zero_parent");
  const amounts = children.map((child) => child.amount_native);
  for (const amount of amounts) {
    const value = cents(amount);
    if (value === 0n) return reject(422, "A part cannot be zero", "split_leg_zero");
    if (value < 0n !== expected < 0n) {
      return reject(422, `A part must have the same sign as ${row.amount_native}`, "split_leg_sign");
    }
  }
  const summed = sumText(amounts);
  if (cents(summed) !== expected) {
    const residual = cents(row.amount_native) - cents(summed);
    const negative = residual < 0n;
    const magnitude = negative ? -residual : residual;
    const residualText = `${negative ? "-" : ""}${magnitude / 100n}.${String(
      magnitude % 100n,
    ).padStart(2, "0")}`;
    return reject(
      422,
      `Children must sum to ${row.amount_native}; they sum to ${summed} (residual ${residualText}).`,
      "split_sum_mismatch",
    );
  }
  let nextId = 900;
  const created: Transaction[] = children.map((child) => {
    nextId += 1;
    return {
      ...makeTx({
        id: nextId,
        date: row.date,
        amount: child.amount_native,
        payee: row.normalized_payee,
        accountId: row.account_id,
        categoryId: child.category_id ?? null,
        parentSplitId: row.id,
        note: child.note ?? null,
      }),
      raw_description: row.raw_description,
    };
  });
  const parent: SplitParentPublic = {
    ...(db.parents.find((candidate) => candidate.id === row.id) ?? {
      ...(row as Transaction),
      child_count: 0,
    }),
    ...row,
    is_split: true,
    child_count: created.length,
  } as SplitParentPublic;
  db.parents = db.parents.filter((candidate) => candidate.id !== row.id).concat(parent);
  db.ordinary = db.ordinary.filter((candidate) => candidate.id !== row.id);
  db.children = db.children.concat(created);
  return Promise.resolve({
    parent,
    children: [...created].sort((a, b) => a.id - b.id),
    split_checksum: row.amount_native,
    sum_ok: true,
  });
}

function mergeSplitImpl(id: number): Promise<Transaction> {
  const parent = db.parents.find((candidate) => candidate.id === id);
  if (parent === undefined) return reject(409, "This transaction is not split", "not_split");
  db.children = db.children.filter((child) => child.parent_split_id !== id);
  db.parents = db.parents.filter((candidate) => candidate.id !== id);
  const restored: Transaction = { ...parent, is_split: false };
  db.ordinary = db.ordinary.concat(restored);
  return Promise.resolve(restored);
}

function page<T>(items: T[], total = items.length, split_parents: SplitParentPublic[] = []) {
  return { items, total, limit: 50, offset: 0, split_parents };
}

beforeEach(() => {
  seed();
  m.listTransactions.mockImplementation((filters: Record<string, unknown>, paging: { limit?: number; offset?: number }) =>
    listTransactionsImpl(filters, paging),
  );
  m.fetchAllTransactions.mockImplementation(() =>
    fetchAllPagesImpl().then((result) => result.items),
  );
  m.fetchAllTransactionPages.mockImplementation(() => fetchAllPagesImpl());
  m.getTransactionSplit.mockImplementation((id: number) => getSplitImpl(id));
  m.createTransactionSplit.mockImplementation((id: number, children: SplitChildInput[]) =>
    createSplitImpl(id, children),
  );
  m.updateTransactionSplit.mockImplementation((id: number, children: SplitChildInput[]) => {
    // PUT replaces: drop the old children, then run the POST rules again.
    db.children = db.children.filter((child) => child.parent_split_id !== id);
    const parent = db.parents.find((candidate) => candidate.id === id);
    if (parent !== undefined) parent.is_split = false;
    return createSplitImpl(id, children);
  });
  m.mergeTransactionSplit.mockImplementation((id: number) => mergeSplitImpl(id));
  m.listAccounts.mockResolvedValue(page([CHEQUING, TFSA]));
  m.listCategories.mockResolvedValue(page(CATEGORIES));
  m.listTags.mockResolvedValue(page([]));
  m.createTag.mockResolvedValue({ id: 99, user_id: 1, name: "new" });
  m.listSuggestions.mockResolvedValue([]);
  m.getUnreviewedCount.mockResolvedValue(0);
  m.updateTransaction.mockImplementation((id: number, patch: Partial<Transaction>) =>
    Promise.resolve({ ...findRow(id), ...patch }),
  );
  m.deleteTransaction.mockResolvedValue(undefined);
  m.createTransaction.mockResolvedValue(makeTx({ id: 9, date: "2026-01-08", amount: "-1.00", payee: "New" }));
  m.setTransactionTags.mockResolvedValue({ items: [] });
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.detectTransferPair.mockRejectedValue(new ApiError(404, "No pair found", "not_found"));
  m.linkTransfer.mockResolvedValue({} as Transaction);
  m.unlinkTransfer.mockResolvedValue({} as Transaction);
  m.setTransactionReviewed.mockImplementation((id: number, reviewed: boolean) =>
    Promise.resolve({
      ...findRow(id),
      reviewed_at: reviewed ? "2026-01-09T10:00:00Z" : null,
      reviewed_by: reviewed ? 1 : null,
    }),
  );
  m.listTransactionEvents.mockResolvedValue({ items: [], total: 0 });
  m.getRecurringCandidates.mockResolvedValue({
    groups: [],
    combined_monthly_total: "0.00",
    suppressed_count: 0,
    window_start: "2026-01-01",
  });
});

// ── harness ─────────────────────────────────────────────────────────────────

interface RenderOptions {
  /** `/transactions?q=…` — the fuzzy search view shares this page. */
  search?: string;
  /** Wrap in ThemeProvider + <main> so axe has a theme and a landmark. */
  a11y?: boolean;
}

function renderTransactions(options: RenderOptions = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  const search = options.search ?? "";
  let ui: ReactElement = (
    <MemoryRouter initialEntries={[search === "" ? "/transactions" : `/transactions?q=${search}`]}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <Transactions />
        </ToastProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
  if (options.a11y === true) {
    ui = (
      <ThemeProvider>
        <main>{ui}</main>
      </ThemeProvider>
    );
  }
  const view = render(ui);
  return { ...view, client };
}

function renderPartnerFeed() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <ThemeProvider>
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <SharedHousehold />
        </QueryClientProvider>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

// ── the six named tests ─────────────────────────────────────────────────────

describe("M107 split transactions (docs/44)", () => {
  it("splitGrouping", async () => {
    renderTransactions();
    await screen.findByRole("button", { name: "Edit transaction 60" });

    // ONE header for the three children: parent payee + date + total, and the
    // literal badge `Split · 3 parts` in place of the category chip.
    const headers = screen.getAllByRole("button", { name: /Split · 3 parts/ });
    expect(headers).toHaveLength(1);
    const header = headers[0];
    expect(header.textContent).toContain("COSTCO #123");
    expect(screen.getAllByText("Split · 3 parts", { selector: "span" })).toHaveLength(1);
    const headerRow = header.closest("tr") as HTMLElement;
    expect(headerRow).not.toBeNull();
    expect(headerRow.textContent).toContain("2026-01-05");
    expect(headerRow.textContent).toContain("187.42");
    // The parent's own category / note never render while it is split (D5).
    expect(headerRow.textContent).not.toContain("bulk shop");

    // Children are the drilldown: nothing of them is in the table yet.
    expect(screen.queryByRole("button", { name: "Edit transaction 812" })).toBeNull();

    // Expanding ALWAYS fetches GET /transactions/{id}/split (§6.3).
    fireEvent.click(header);
    await waitFor(() => expect(m.getTransactionSplit).toHaveBeenCalledWith(51));
    for (const id of [812, 813, 814]) {
      await screen.findByRole("button", { name: `Edit transaction ${id}` });
    }
    // …and the panel lists them id ASC even though the page served id DESC.
    const order = [812, 813, 814].map((id) =>
      screen.getByRole("button", { name: `Edit transaction ${id}` }),
    );
    expect(
      order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(header.getAttribute("aria-expanded")).toBe("true");
    // The panel shows each child's OWN amount, category and note (§6.3).
    expect(screen.getByText(/kids clothes/)).toBeTruthy();

    // Bulk-select on the header toggles every child present in the page.
    fireEvent.click(screen.getByLabelText("Select the split parts of COSTCO #123"));
    expect(await screen.findByText("3 selected")).toBeTruthy();
    const childBoxes = screen.getAllByLabelText("Select transaction: COSTCO #123");
    expect(childBoxes).toHaveLength(3);
    for (const box of childBoxes) {
      expect((box as HTMLInputElement).checked).toBe(true);
    }
    // …and the parent itself is never a summable row: LEDGER `items` carry the
    // children, `split_parents` carries the header (docs/44 D11/§6.1).
    const served = (await m.listTransactions.mock.results.at(-1)!.value) as TransactionPage;
    expect(served.items.some((row) => row.id === 51)).toBe(false);
    expect(served.split_parents.map((parent) => parent.id)).toEqual([51]);

    // Re-split reopens the editor prefilled with the CURRENT children and
    // saves through PUT (docs/44 §12).
    fireEvent.click(screen.getByRole("button", { name: "Re-split, COSTCO #123" }));
    const resplitDialog = await screen.findByRole("dialog", {
      name: "Split this transaction into parts",
    });
    const amounts = [1, 2, 3].map(
      (part) => (screen.getByLabelText(`Amount for part ${part}`) as HTMLInputElement).value,
    );
    expect(amounts).toEqual(["-120.00", "-40.00", "-27.42"]);
    expect(resplitDialog.textContent).toContain("Save split");

    // Saving the re-split goes through PUT (replace), never POST — and the
    // payload carries each part's own amount, category and note.
    fireEvent.click(screen.getByRole("button", { name: "Save split" }));
    await waitFor(() => expect(m.updateTransactionSplit).toHaveBeenCalledTimes(1));
    expect(m.updateTransactionSplit).toHaveBeenCalledWith(51, [
      { amount_native: "-120.00", category_id: 1, note: "kids clothes", tag_ids: [] },
      { amount_native: "-40.00", category_id: 2, note: null, tag_ids: [] },
      { amount_native: "-27.42", category_id: 3, note: null, tag_ids: [] },
    ]);
    expect(m.createTransactionSplit).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // Merge back is a TWO-STAGE confirm (M95's destructive-action pattern):
    // stage one arms the button and sends NOTHING, stage two fires the DELETE.
    const merge = screen.getByRole("button", { name: "Merge back, COSTCO #123" });
    fireEvent.click(merge);
    expect(m.mergeTransactionSplit).not.toHaveBeenCalled();
    const armed = screen.getByRole("button", { name: /Confirm merge\?/ });
    fireEvent.click(armed);
    await waitFor(() => expect(m.mergeTransactionSplit).toHaveBeenCalledWith(51));
    // …and the group is gone once the refetch lands.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Split · 3 parts/ })).toBeNull(),
    );

    // Restore the fixture for the views below — the merge really removed it.
    seed();
    cleanup();

    // The fuzzy search view groups the SAME way (docs/44 §6.3, surfaces in scope).
    renderTransactions({ search: "costco" });
    await screen.findByRole("button", { name: /Split · 3 parts/ });
    expect(screen.getAllByRole("button", { name: /Split · 3 parts/ })).toHaveLength(1);
    expect(screen.getAllByText("Split · 3 parts", { selector: "span" })).toHaveLength(1);

    // FAIL OPEN (docs/44 §6.3): a child whose header is missing from
    // `split_parents` (a server bug) renders as an ordinary row — never a
    // crash, never a header with nothing behind it.
    cleanup();
    m.listTransactions.mockResolvedValue(
      page([makeTx({ id: 812, date: "2026-01-05", amount: "-120.00", payee: "COSTCO #123", parentSplitId: 51 })], 1, []),
    );
    renderTransactions();
    await screen.findByRole("button", { name: "Edit transaction 812" });
    expect(screen.queryByRole("button", { name: /Split · 3 parts/ })).toBeNull();
  });

  it("splitEditor", async () => {
    renderTransactions();
    await screen.findByRole("button", { name: "Edit transaction 60" });

    fireEvent.click(screen.getByRole("button", { name: "Split transaction 60" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Split this transaction into parts",
    });
    expect(dialog.textContent).toContain("Split this transaction into parts");

    // Exact Decimal arithmetic, straight from the exported helper (a float
    // sum of 0.10 + 0.20 leaves `-0.00`, which must never gate the button).
    expect(remainingLabel("-187.42", ["-100.00", "-77.42"])).toBe("-10.00");
    expect(remainingLabel("-187.42", ["-120.00", "-40.00", "-27.42"])).toBe("0.00");
    expect(remainingLabel("0.30", ["0.10", "0.20"])).toBe("0.00");
    expect(remainingLabel("0.30", ["0.10", "0.19"])).toBe("0.01");

    const submit = screen.getByRole("button", { name: "Split" }) as HTMLButtonElement;
    expect(screen.getByText("Remaining: -187.42")).toBeTruthy();
    expect(submit.disabled).toBe(true);

    // Client-side convenience only: FIRST leg prefilled with the parent's
    // category, the rest empty — the server copies no category (D5).
    expect((screen.getByLabelText("Category for part 1") as HTMLInputElement).value).toBe(
      "Groceries",
    );
    expect((screen.getByLabelText("Category for part 2") as HTMLInputElement).value).toBe("");

    const part1 = screen.getByLabelText("Amount for part 1");
    const part2 = screen.getByLabelText("Amount for part 2");
    fireEvent.change(part1, { target: { value: "-100.00" } });
    fireEvent.change(part2, { target: { value: "-77.42" } });
    expect(screen.getByText("Remaining: -10.00")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Split" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.change(part2, { target: { value: "-87.42" } });
    expect(screen.getByText("Remaining: 0.00")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Split" }) as HTMLButtonElement).disabled).toBe(
      false,
    );

    // The server changed under us (another device) — its envelope must land
    // VERBATIM with its code, never a generic sentence (docs/44 §12/§4.3).
    db.ordinary.find((row) => row.id === 60)!.amount_native = "-200.00";
    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(
      "Children must sum to -200.00; they sum to -187.42 (residual -12.58).",
    );
    expect(alert.textContent).toContain("split_sum_mismatch");
    expect(alert.textContent).not.toContain("Something went wrong");
    await waitFor(() => expect(m.createTransactionSplit).toHaveBeenCalledTimes(1));
    expect(m.createTransactionSplit).toHaveBeenCalledWith(60, [
      { amount_native: "-100.00", category_id: 1, note: null, tag_ids: [] },
      { amount_native: "-87.42", category_id: null, note: null, tag_ids: [] },
    ]);

    // Second session, on the `+0.30` row: 0.10 + 0.20 reaches exactly 0.00
    // (a float implementation would print -0.00 and keep this disabled).
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Split transaction 61" }));
    await screen.findByRole("dialog", { name: "Split this transaction into parts" });
    fireEvent.change(screen.getByLabelText("Amount for part 1"), {
      target: { value: "0.10" },
    });
    fireEvent.change(screen.getByLabelText("Amount for part 2"), {
      target: { value: "0.20" },
    });
    expect(screen.getByText("Remaining: 0.00")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Split" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  // splitReportParity retired with the client report engines (M160). The
  // docs/44 §2 invariant this node used to prove client-side — a split's
  // parent never double-counts in any report — is pinned server-side
  // where the compute now lives:
  //   backend/tests/test_split_transactions.py::
  //     test_ledger_population_counts_children_and_never_parents
  //   (spending/categories + cashflow + net-worth over parent-vs-children)
  //   backend/tests/test_reports_tags.py (the double-count block sum).

  it("splitInvestmentHide", async () => {
    renderTransactions();
    await screen.findByRole("button", { name: "Edit transaction 70" });

    // No Split affordance on a row in an investment account (D10 — the client
    // hides it, the server refuses it).
    expect(screen.queryByRole("button", { name: "Split transaction 70" })).toBeNull();
    // …while the same affordance exists on the personal-surface row (the
    // accounts query decides it, so wait for it rather than race it).
    expect(await screen.findByRole("button", { name: "Split transaction 60" })).toBeTruthy();

    // Accessible-name sweep of the investment row: every control named, none
    // of them offering a split.
    const edit = screen.getByRole("button", { name: "Edit transaction 70" });
    const row = edit.closest("tr") as HTMLElement;
    expect(row).not.toBeNull();
    const controls = [...row.querySelectorAll<HTMLElement>("button, input, select, a")];
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) {
      // The accessible name, per accname: aria-label → content → placeholder
      // (the category combo-box carries its hint that way).
      const fallback =
        control instanceof HTMLInputElement ? control.placeholder || control.value : "";
      const name = (control.getAttribute("aria-label") || control.textContent || fallback).trim();
      expect(name, control.outerHTML.slice(0, 120)).not.toBe("");
      expect(name.toLowerCase()).not.toContain("split");
    }
    expect(row.textContent?.toLowerCase()).not.toContain("split");
  });

  it("splitA11y", async () => {
    seed();
    const runs: string[][] = [];
    for (const theme of ["dark", "light"] as const) {
      cleanup();
      localStorage.setItem("theme", theme);
      const { container } = renderTransactions({ a11y: true });

      const disclosure = await screen.findByRole("button", { name: /Split · 3 parts/ });
      expect(disclosure.getAttribute("aria-expanded")).toBe("false");
      const regionId = disclosure.getAttribute("aria-controls");
      expect(regionId).toBeTruthy();
      // aria-controls always resolves — even collapsed the region exists.
      expect(document.getElementById(regionId as string)).not.toBeNull();

      fireEvent.click(disclosure);
      await waitFor(() => expect(disclosure.getAttribute("aria-expanded")).toBe("true"));
      const region = document.getElementById(regionId as string);
      expect(region).not.toBeNull();
      expect(region?.getAttribute("role")).toBe("region");
      // Labelled BY the header button, whose name carries the badge text.
      expect(region?.getAttribute("aria-labelledby")).toBe(disclosure.id);
      expect(document.getElementById(region?.getAttribute("aria-labelledby") as string)).toBe(
        disclosure,
      );
      expect(disclosure.textContent).toContain("Split · 3 parts");

      const results = await axe.run(container, {
        resultTypes: ["violations"],
        rules: { "color-contrast": { enabled: false } },
      });
      runs.push(results.violations.map((violation) => `${violation.id}(${violation.impact})`));
    }
    expect(runs[0]).toEqual([]);
    expect(runs[1]).toEqual([]);
  });

  it("splitPartnerFeed", async () => {
    // Privacy mode (docs/43 §9): every money field crosses as JSON `null`.
    const partnerChild = (id: number): Transaction => {
      const row = makeTx({
        id,
        date: "2026-01-05",
        amount: "-40.00",
        payee: "COSTCO #123",
        categoryId: 1,
        parentSplitId: 51,
      });
      return { ...row, amount_native: null, cad_amount: null, raw_description: `PART ${id}` };
    };
    const parent = {
      ...makeParent(),
      amount_native: null,
      cad_amount: null,
      child_count: 2, // the number of children THIS partner payload carries
    } as unknown as SplitParentPublic;
    const view = {
      state: {
        linked: true,
        owner: { user_id: 1, name: "Owner", email: "owner@example.com" },
        account_ids: [CHEQUING.id],
        privacy_mode: true,
        link_id: 1,
        accepted_at: "2026-10-02",
      },
      accounts: [
        {
          ...CHEQUING,
          cash_balance: null,
          investment_value: null,
          total_value: null,
          starting_balance: null,
        },
      ],
      transactions: [partnerChild(814), partnerChild(813)],
      split_parents: [parent],
      budgetProgress: [],
    } as unknown as SharedView;
    m.loadSharedView.mockResolvedValue(view);

    renderPartnerFeed();
    const disclosure = await screen.findByRole("button", { name: /Split · 2 parts/ });
    // Groups the same way as the owner's register: ONE header for both rows.
    expect(screen.getAllByRole("button", { name: /Split · 2 parts/ })).toHaveLength(1);
    expect(screen.getAllByText("Split · 2 parts", { selector: "span" })).toHaveLength(1);

    // Privacy mode (docs/44 §9): the header amount is 🔒, never a number.
    const header = disclosure.parentElement as HTMLElement;
    expect(header.textContent).toContain("🔒");
    expect(header.textContent).not.toContain("187.42");

    // Local expansion only — a partner has no split route (docs/43 §6, D13),
    // so the region shows THIS page's children and never drills down.
    const region = document.getElementById(
      disclosure.getAttribute("aria-controls") as string,
    ) as HTMLElement;
    expect(region.getAttribute("role")).toBe("region");
    expect(region.children.length).toBe(0);
    fireEvent.click(disclosure);
    await waitFor(() => expect(region.children.length).toBe(2));
    expect(m.getTransactionSplit).not.toHaveBeenCalled();
    // Read-only: no merge control is offered on this surface.
    expect(screen.queryByRole("button", { name: /Merge back/ })).toBeNull();
  });
});
