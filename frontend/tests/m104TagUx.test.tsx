// M104 (docs/09 §Tag UX) — the tagging flow:
//
//  1. TagPicker is a real combobox: listbox/option semantics, arrow-key
//     navigation over the filtered list, Enter attaches the highlight, and
//     Escape closes the LIST first (it stops propagation) so a picker inside
//     the editor modal never closes the modal on the first press.
//  2. The modal promotes Tags ABOVE raw description with a real section label.
//  3. Quick-tag from the row: click the row's `+` button → type → Enter is TWO
//     interactions and writes through the same PUT route the editor uses.
//  4. The filter bar carries the same picker in filter mode (chips + "matches
//     all selected") and drives `tagIds` into the list query.
//  5. The management modal shows usage, gates delete on usage and merges
//     through POST /tags/merge with the survivor chosen from the selection.
//  6. Recently-used tags sort first while the query is empty (session recency).

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  updateTransaction: vi.fn(),
  setTransactionTags: vi.fn(),
  getTransactionTags: vi.fn(),
  listAccounts: vi.fn(),
  listInstitutions: vi.fn(),
  listCategories: vi.fn(),
  listTags: vi.fn(),
  createTag: vi.fn(),
  updateTag: vi.fn(),
  deleteTag: vi.fn(),
  mergeTags: vi.fn(),
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
  setTransactionTags: m.setTransactionTags,
  getTransactionTags: m.getTransactionTags,
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
  updateTag: m.updateTag,
  deleteTag: m.deleteTag,
  mergeTags: m.mergeTags,
}));
vi.mock("../src/api/suggestions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/suggestions")>()),
  listSuggestions: m.listSuggestions,
}));

import type { Account } from "../src/api/accounts";
import type { Transaction } from "../src/api/transactions";
import { ToastProvider } from "../src/components/ui/Toast";
import { TagPicker, TagFilterPicker } from "../src/components/transactions/TagPicker";
import { TransactionEditor } from "../src/components/transactions/TransactionEditor";
import { TagManagerModal } from "../src/components/transactions/TagManagerModal";
import { Transactions } from "../src/pages/Transactions";
import { byRecency, noteTagUsed, resetTagRecency } from "../src/lib/tagRecency";

function tag(id: number, name: string) {
  return { id, user_id: 1, name, transaction_count: 0, rule_count: 0 };
}

const TAGS = [tag(42, "Groceries"), tag(43, "Gym"), tag(44, "Travel")];

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

const TXN = {
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
  pair_account_name: null,
  tags: [{ id: 5, user_id: 1, name: "existing" }],
} as unknown as Transaction;

function page<T>(items: T[], total = items.length) {
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

beforeEach(() => {
  vi.clearAllMocks();
  resetTagRecency();
  m.listTransactions.mockResolvedValue(page([TXN]));
  m.fetchAllTransactions.mockResolvedValue([TXN]);
  m.deleteTransaction.mockResolvedValue(undefined);
  m.updateTransaction.mockResolvedValue(TXN);
  m.setTransactionTags.mockImplementation((_id: number, ids: number[]) =>
    Promise.resolve({ items: ids.map((id) => ({ id, user_id: 1, name: `tag-${id}` })) }),
  );
  m.getTransactionTags.mockResolvedValue({ items: [] });
  m.listAccounts.mockResolvedValue(page([ACCOUNT], 1));
  m.listInstitutions.mockResolvedValue(page([]));
  m.listCategories.mockResolvedValue(page([]));
  m.listTags.mockResolvedValue(page(TAGS, TAGS.length));
  m.createTag.mockImplementation(({ name }: { name: string }) =>
    Promise.resolve({ id: 99, user_id: 1, name }),
  );
  m.updateTag.mockImplementation((id: number, patch: { name: string }) =>
    Promise.resolve({ id, user_id: 1, name: patch.name }),
  );
  m.deleteTag.mockResolvedValue(undefined);
  m.mergeTags.mockImplementation((payload: { target_id: number }) =>
    Promise.resolve({ target: { id: payload.target_id, user_id: 1, name: "Groceries" }, merged_tag_ids: [] }),
  );
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
  resetTagRecency();
  vi.clearAllMocks();
});

/* ------------------------------------------------------------ 1. listbox */

describe("M104 TagPicker — combobox/listbox keyboard contract", () => {
  function mountPicker(props: Partial<Parameters<typeof TagPicker>[0]> = {}) {
    const onAttach = vi.fn();
    const onCreate = vi.fn();
    render(
      <TagPicker
        attached={[]}
        allTags={TAGS}
        onAttach={onAttach}
        onCreate={onCreate}
        {...props}
      />,
    );
    const input = screen.getByRole("combobox", { name: "Add or create tag" });
    return { input, onAttach, onCreate };
  }

  it("exposes listbox/option semantics and moves a virtual highlight with the arrows", () => {
    const { input } = mountPicker();
    fireEvent.focus(input);

    expect(input.getAttribute("aria-expanded")).toBe("true");
    const list = screen.getByRole("listbox");
    expect(list.getAttribute("aria-label")).toBe("Add or create tag");
    expect(input.getAttribute("aria-controls")).toBe(list.id);
    // The id rides on the SCROLLING listbox (axe exempts a combobox popup by
    // `[role=combobox][aria-controls~=id]` on the scrollable node itself).
    expect(within(list).getAllByRole("option")).toHaveLength(3);

    expect(input.getAttribute("aria-activedescendant")).toBeNull();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const options = within(list).getAllByRole("option");
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0].id);
    expect(options[0].getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).toBe(options[1].id);
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0].id);
    // Wraps backwards past the top.
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(input.getAttribute("aria-activedescendant")).toBe(options[2].id);
  });

  it("Enter attaches the highlighted option, and falls back to the exact match", () => {
    const { input, onAttach } = mountPicker();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAttach).toHaveBeenCalledWith(42);

    // No highlight: an exact match still attaches (the old behavior).
    onAttach.mockClear();
    fireEvent.change(input, { target: { value: "gym" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAttach).toHaveBeenCalledWith(43);
  });

  it("lists the session's recently attached tags first while the query is empty", () => {
    noteTagUsed(43);
    noteTagUsed(44);
    const { input } = mountPicker();
    fireEvent.focus(input);
    const names = within(screen.getByRole("listbox"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(names).toEqual(["Travel", "Gym", "Groceries"]);
    expect(byRecency(TAGS).map((t) => t.name)).toEqual([
      "Travel",
      "Gym",
      "Groceries",
    ]);
  });

  it("Escape closes the list and stops there — the second press is the dialog's", async () => {
    const onClose = vi.fn();
    render(
      <MemoryRouter>
        <ToastProvider>
          <TransactionEditor
            target={TXN}
            accounts={[ACCOUNT]}
            categories={[]}
            saving={false}
            onSave={vi.fn()}
            onClose={onClose}
          />
        </ToastProvider>
      </MemoryRouter>,
    );

    const input = await screen.findByRole("combobox", { name: "Add or create tag" });
    fireEvent.focus(input);
    expect(screen.getByRole("listbox")).toBeTruthy();

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();

    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

/* -------------------------------------------------- 2. modal section order */

describe("M104 editor modal — tags promote above the raw description", () => {
  it("labels the section and renders it before the raw description field", async () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <TransactionEditor
            target={TXN}
            accounts={[ACCOUNT]}
            categories={[]}
            saving={false}
            onSave={vi.fn()}
            onClose={vi.fn()}
          />
        </ToastProvider>
      </MemoryRouter>,
    );
    const dialog = await screen.findByRole("dialog");
    const text = dialog.textContent ?? "";
    expect(text).toContain("Tags — as many as you like; category stays single");

    const labels = Array.from(dialog.querySelectorAll("label, span")).map((el) =>
      (el.textContent ?? "").trim(),
    );
    const tagsAt = labels.findIndex((t) => t.startsWith("Tags —"));
    const rawAt = labels.findIndex((t) => t.startsWith("Raw description"));
    expect(tagsAt).toBeGreaterThanOrEqual(0);
    expect(rawAt).toBeGreaterThan(tagsAt);

    // Full-width picker (not the old 160px input).
    const input = within(dialog).getByRole("combobox", { name: "Add or create tag" });
    expect(input.className).toContain("w-full");
  });
});

/* --------------------------------------------------------- 3. quick-tag */

describe("M104 row-level quick-tag", () => {
  it("opens from the row and attaches with click → type → Enter (two interactions)", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/);

    fireEvent.click(screen.getByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/));
    const input = await screen.findByRole("combobox", { name: "Add or create tag" });
    fireEvent.change(input, { target: { value: "groceries" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(m.setTransactionTags).toHaveBeenCalledTimes(1));
    // The row's existing tag stays, the picked one is appended — same PUT the
    // editor uses.
    expect(m.setTransactionTags).toHaveBeenCalledWith(1, [5, 42]);
    // The popover closes itself once the write lands.
    await waitFor(() =>
      expect(screen.queryByRole("combobox", { name: "Add or create tag" })).toBeNull(),
    );
  });

  it("names every row's trigger uniquely (M92)", async () => {
    m.listTransactions.mockResolvedValue(
      page([
        TXN,
        { ...TXN, id: 2, normalized_payee: "Beta Gas", raw_description: "BETA GAS" },
      ]),
    );
    renderWithProviders(<Transactions />);
    await screen.findByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/);
    expect(screen.getAllByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/)).toHaveLength(1);
    const triggers = screen.getAllByTitle("Add tags");
    expect(new Set(triggers.map((el) => el.getAttribute("aria-label"))).size).toBe(2);
    expect(document.querySelectorAll('[aria-label^="Add tags"]').length).toBe(2);
  });
});

/* --------------------------------------------------- 4. filter bar (AND) */

describe("M104 multi-tag filter", () => {
  it("selects chips through the picker and sends tagIds to the list query", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/);

    expect(screen.getByText("matches all selected")).toBeTruthy();
    const input = screen.getByRole("combobox", { name: "Add tag to filter" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "gym" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(screen.getByLabelText("Remove tag filter — Gym")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      const last = m.listTransactions.mock.calls.at(-1)?.[0] as {
        tagIds?: number[];
      };
      expect(last.tagIds).toEqual([43]);
    });
  });

  it("opens the management modal from the picker's footer", async () => {
    renderWithProviders(<Transactions />);
    await screen.findByLabelText(/Add tags — Alpha Groceries \(transaction \d+\)/);
    fireEvent.focus(screen.getByRole("combobox", { name: "Add tag to filter" }));
    fireEvent.click(await screen.findByRole("button", { name: "Manage tags…" }));
    expect(await screen.findByRole("dialog", { name: "Manage tags" })).toBeTruthy();
  });
});

/* ----------------------------------------------- 5. filter-mode variant */

describe("M104 TagFilterPicker", () => {
  it("renders removable chips and toggles selection without inline-create", () => {
    const onToggle = vi.fn();
    const onRemove = vi.fn();
    const onManage = vi.fn();
    render(
      <TagFilterPicker
        allTags={TAGS}
        selected={[TAGS[1]]}
        onToggle={onToggle}
        onRemove={onRemove}
        onManage={onManage}
      />,
    );

    const remove = screen.getByLabelText("Remove tag filter — Gym");
    fireEvent.click(remove);
    expect(onRemove).toHaveBeenCalledTimes(1);

    const input = screen.getByRole("combobox", { name: "Add tag to filter" });
    fireEvent.focus(input);
    // The selected tag is offered nowhere, and there is no create row.
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Groceries", "Travel"]);
    fireEvent.mouseDown(options[0]);
    expect(onToggle).toHaveBeenCalledWith(TAGS[0]);
    expect(screen.queryByText(/^Create/)).toBeNull();
  });
});

/* ------------------------------------------------- 6. management surface */

describe("M104 TagManagerModal", () => {
  async function openManager() {
    renderWithProviders(<TagManagerModal onClose={vi.fn()} />);
    return await screen.findByRole("dialog", { name: "Manage tags" });
  }

  it("shows usage counts and refuses a delete for a tag in use", async () => {
    m.listTags.mockResolvedValue(
      page([
        { ...TAGS[0], transaction_count: 12 },
        { ...TAGS[1], transaction_count: 0, rule_count: 0 },
      ]),
    );
    await openManager();
    expect(await screen.findByText("12 transactions")).toBeTruthy();
    expect(screen.getByText("unused")).toBeTruthy();

    // In use → no delete control at all (the count IS the affordance).
    expect(screen.queryByRole("button", { name: "Delete Groceries" })).toBeNull();
    const del = screen.getByRole("button", { name: "Delete Gym" });
    fireEvent.click(del);
    await waitFor(() => expect(m.deleteTag).toHaveBeenCalledWith(43));
  });

  it("renames through PATCH", async () => {
    m.listTags.mockResolvedValue(page([tag(45, "Goceries"), TAGS[1]]));
    await openManager();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Goceries" }));
    const field = screen.getByRole("textbox", { name: "New name for Goceries" });
    fireEvent.change(field, { target: { value: "Groceries" } });
    fireEvent.click(screen.getByRole("button", { name: "Save name" }));
    await waitFor(() => expect(m.updateTag).toHaveBeenCalledWith(45, { name: "Groceries" }));
  });

  it("merges the selection into one survivor", async () => {
    m.listTags.mockResolvedValue(page([TAGS[0], TAGS[2]]));
    await openManager();
    fireEvent.click(await screen.findByLabelText("Select Groceries to merge"));
    fireEvent.click(screen.getByLabelText("Select Travel to merge"));

    const merge = screen.getByRole("button", { name: "Merge 1 into Groceries" });
    fireEvent.click(merge);
    await waitFor(() =>
      expect(m.mergeTags).toHaveBeenCalledWith({ source_ids: [44], target_id: 42 }),
    );
  });

  it("closes on Escape through the shared dialog contract", async () => {
    const onClose = vi.fn();
    renderWithProviders(<TagManagerModal onClose={onClose} />);
    await screen.findByRole("dialog", { name: "Manage tags" });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
