// M108 P4 — receipt images in the UI (docs/09 §Receipt images, docs/08).
//
// What is pinned here:
//   * the row's chip is the ONLY signal that an image exists, and it is
//     driven by `Transaction.has_receipt` — never by fetched bytes;
//   * the page keeps ONE shared file picker, not one per row;
//   * the chip's label names its row (M92), because two rows can share a
//     payee and a probe selecting by name must not hit the wrong one;
//   * clicking the chip asks the API for the blob — the image only ever
//     travels through the owner's session, never a bare URL;
//   * the editor's field flips between "Attach image" and
//     "One image attached / View / Remove";
//   * `fitWithin` never upscales and never returns a zero edge;
//   * `uploadReceipt` PUTs multipart `file` to the pinned path.

import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  listTransactions: vi.fn(),
  fetchAllTransactions: vi.fn(),
  listAccounts: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  listSuggestions: vi.fn(),
  getTransactionTags: vi.fn(),
  setTransactionTags: vi.fn(),
  removeTransactionTag: vi.fn(),
  // receipts.ts is exercised for REAL — only its HTTP transport is swapped.
  apiBinary: vi.fn(),
}));

vi.mock("../src/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, setUser: () => {} }),
}));

vi.mock("../src/api/transactions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/transactions")>()),
  listTransactions: m.listTransactions,
  fetchAllTransactions: m.fetchAllTransactions,
  getTransactionTags: m.getTransactionTags,
  setTransactionTags: m.setTransactionTags,
  removeTransactionTag: m.removeTransactionTag,
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
}));
vi.mock("../src/api/suggestions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/suggestions")>()),
  listSuggestions: m.listSuggestions,
}));
vi.mock("../src/api/trades", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/trades")>()),
  listTrades: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
}));
vi.mock("../src/api/acb", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/acb")>()),
  listAcbPools: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
}));
vi.mock("../src/api/imports", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/imports")>()),
  listImports: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
  revertBatch: vi.fn(),
}));
vi.mock("../src/api/debts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/debts")>()),
  listDebts: vi.fn().mockResolvedValue([]),
}));
vi.mock("../src/api/goals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/goals")>()),
  listGoals: vi.fn().mockResolvedValue([]),
}));
vi.mock("../src/api/reconciliation", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/reconciliation")>()),
  getReconciliation: vi.fn().mockResolvedValue({
    items: [],
    summary: { total: 0, matched: 0, unmatched: 0, matched_sum: "0.00", unmatched_sum: "0.00" },
  }),
}));
vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  apiBinary: m.apiBinary,
}));

// jsdom has no matchMedia; framer-motion asks for it.
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

import { ToastProvider } from "../src/components/ui/Toast";
import { Transactions } from "../src/pages/Transactions";
import { TransactionEditor } from "../src/components/transactions/TransactionEditor";
import { uploadReceipt } from "../src/api/receipts";
import { fitWithin, MAX_EDGE, ReceiptImageError } from "../src/lib/receiptImage";

type Row = Record<string, unknown>;

function txn(id: number, payee: string, hasReceipt: boolean): Row {
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
    has_receipt: hasReceipt,
    tags: [],
    is_recurring: false,
    reviewed_at: null,
    pending: false,
    notes: null,
    duplicate_of_id: null,
    source: "manual",
  };
}

const ACCOUNT = {
  id: 7,
  institution_id: null,
  name: "RBC Main",
  type: "chequing",
  currency: "CAD",
  taxable: true,
  llm_autopilot: false,
  cash_balance: "1500.00",
  investment_value: "0.00",
  total_value: "1500.00",
  starting_balance: "0.00",
  name_cipher: null,
};

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 200, offset: 0 };
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

const TX_WITH = txn(1, "Loblaws", true);
const TX_WITHOUT = txn(2, "Petro Canada", false);

beforeEach(() => {
  vi.clearAllMocks();
  m.listTransactions.mockResolvedValue(page([TX_WITH, TX_WITHOUT]));
  m.fetchAllTransactions.mockResolvedValue([TX_WITH, TX_WITHOUT]);
  m.listAccounts.mockResolvedValue(page([ACCOUNT]));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page([]));
  m.listSuggestions.mockResolvedValue([]);
  m.getTransactionTags.mockResolvedValue(page([]));
  m.apiBinary.mockResolvedValue({
    ok: true,
    json: async () => ({ transaction_id: 1, content_type: "image/webp", size_bytes: 4096 }),
    blob: async () => new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" }),
  });
});

// --- the row -----------------------------------------------------------------

describe("M108 P4 — the transactions row", () => {
  it("shows a chip only when has_receipt is true, and names its own row", async () => {
    renderWithProviders(<Transactions />);

    await screen.findByLabelText("Delete transaction 1");

    // Row 1 has an image, row 2 does not — and the labels are unique (M92).
    const chip = screen.getByLabelText("View the image attached to Loblaws (transaction 1)");
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.textContent).toContain("image");
    expect(screen.queryByLabelText("View the image attached to Petro Canada (transaction 2)")).toBeNull();
  });

  it("keeps exactly ONE file picker for the whole page", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText("Delete transaction 1");

    const pickers = document.querySelectorAll('input[type="file"]');
    expect(pickers.length).toBe(1);
    // sr-only, not display:none — a keyboard user can still reach it.
    expect((pickers[0] as HTMLElement).className).toContain("sr-only");
  });

  it("offers an attach control on every row, including ones already carrying an image", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText("Delete transaction 1");

    // Replace, not "attach or remove": the second upload PUTs over the first.
    expect(screen.getByLabelText("Attach an image — Loblaws (transaction 1)")).not.toBeNull();
    expect(screen.getByLabelText("Attach an image — Petro Canada (transaction 2)")).not.toBeNull();
  });

  it("fetches the blob through the API when the chip is clicked", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText("Delete transaction 1");

    m.apiBinary.mockClear();
    fireEvent.click(screen.getByLabelText("View the image attached to Loblaws (transaction 1)"));

    await waitFor(() => expect(m.apiBinary).toHaveBeenCalled());
    expect(m.apiBinary.mock.calls[0][0]).toBe("/transactions/1/receipt");
    // Never a URL: the bytes come back through the session, and the caller
    // opens them from an object URL it revokes.
    expect(m.apiBinary.mock.calls[0][1]?.method ?? "GET").toBe("GET");
  });
});

// --- the editor field --------------------------------------------------------

describe("M108 P4 — the editor's image field", () => {
  function editor(hasReceipt: boolean) {
    renderWithProviders(
      <TransactionEditor
        target={txn(1, "Loblaws", hasReceipt) as never}
        defaultAccountId={7}
        accounts={[ACCOUNT] as never}
        categories={[]}
        saving={false}
        error={null}
        onSave={() => {}}
        onClose={() => {}}
      />,
    );
  }

  it("offers Attach when there is no image", async () => {
    editor(false);
    expect(await screen.findByText("Attach image")).not.toBeNull();
    expect(screen.queryByText("One image attached")).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
  });

  it("flips to One image attached with View and Remove when there is one", async () => {
    editor(true);
    expect(await screen.findByText("One image attached")).not.toBeNull();
    expect(screen.getByRole("button", { name: "View" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Remove" })).not.toBeNull();
    expect(screen.queryByText("Attach image")).toBeNull();
  });

  it("tells the list to re-read its chip after an attach or a Remove", async () => {
    // docs/09 §Receipt images: the editor's field AND the row's chip must
    // change together — the list is the chip's only source, so a mutation
    // here has to invalidate it or the row keeps a chip the editor just
    // reported as gone.
    const onReceiptChanged = vi.fn();
    renderWithProviders(
      <TransactionEditor
        target={txn(1, "Loblaws", true) as never}
        defaultAccountId={7}
        accounts={[ACCOUNT] as never}
        categories={[]}
        saving={false}
        error={null}
        onSave={() => {}}
        onClose={() => {}}
        onReceiptChanged={onReceiptChanged}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    await waitFor(() => expect(onReceiptChanged).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Attach image")).not.toBeNull();
  });
});

// --- the transport -----------------------------------------------------------

describe("M108 P4 — uploadReceipt", () => {
  it("PUTs a multipart file to the pinned path", async () => {
    m.apiBinary.mockClear();
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: "image/webp" });

    const meta = await uploadReceipt(42, blob, "receipt.webp");

    expect(meta).toEqual({ transaction_id: 1, content_type: "image/webp", size_bytes: 4096 });
    const [path, init] = m.apiBinary.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/transactions/42/receipt");
    expect(init.method).toBe("PUT");
    // FormData, so the browser writes its OWN boundary — a hand-set
    // Content-Type would break multipart parsing server-side.
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string> | undefined)?.["Content-Type"]).toBeUndefined();
  });
});

// --- the pre-upload size plan ------------------------------------------------

describe("M108 P4 — fitWithin", () => {
  it("shrinks the longest edge to MAX_EDGE and keeps the aspect ratio", () => {
    expect(fitWithin(4000, 3000)).toEqual({ width: MAX_EDGE, height: 1200 });
    expect(fitWithin(3000, 4000)).toEqual({ width: 1200, height: MAX_EDGE });
  });

  it("never upscales an already-small image", () => {
    expect(fitWithin(800, 600)).toEqual({ width: 800, height: 600 });
    expect(fitWithin(1600, 1600)).toEqual({ width: 1600, height: 1600 });
  });

  it("never returns a zero edge for degenerate input", () => {
    expect(fitWithin(0, 0)).toEqual({ width: 1, height: 1 });
    expect(fitWithin(-5, Number.NaN)).toEqual({ width: 1, height: 1 });
  });
});

describe("M108 P4 — ReceiptImageError", () => {
  it("carries a machine reason AND a human sentence", () => {
    const error = new ReceiptImageError("output_too_large", "crop it");
    expect(error.reason).toBe("output_too_large");
    expect(error.message).toBe("crop it");
    expect(error.name).toBe("ReceiptImageError");
  });
});
