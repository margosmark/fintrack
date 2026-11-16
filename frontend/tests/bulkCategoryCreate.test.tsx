// M176 — creating a category IN PLACE from the bulk toolbar (docs/09
// §Recategorize — create in place).
//
// Mark's bug: with a big filtered selection, the Recategorize control was a
// native `<select>` listing only names that already existed — the one place a
// batch of misclassified rows lands had no way to mint the category they
// actually need, even though the single-row cells' `CategoryPicker` could.
//
// The contract this file pins:
//
//  1. The mode renders the SHARED picker. Typing a new name offers an explicit
//     `Create "…"` row; clicking it (a keyboard Enter/Space or a tap — the same
//     `click`) writes a CATEGORY and NOTHING else. No transaction is touched
//     until the separate, explicit Apply.
//  2. The created category is SELECTED and VISIBLE immediately — merged over
//     the prop locally, so the control never reads blank while `["categories"]`
//     refetches.
//  3. Apply is unavailable without a valid id, including while a create is in
//     flight, and a create can only run once at a time (no double submit).
//  4. A refused create is SHOWN at the control and is retryable — never the
//     shared picker's old swallowed catch — and it keeps the selection.
//  5. Both execution scopes hold: an explicit id list drives the single-row
//     route per row; a MATCHED set sends its filter + exceptions in ONE call.
//  6. A name that case/whitespace-equivalently matches an existing category
//     offers no Create row (no duplicate minting).
//  7. The menu opens UPWARD below `md` (the bar is pinned to the viewport
//     bottom) and downward at `md`, and unrelated text (a payee) never creates.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => toastSpies,
}));

vi.mock("../src/api/categories", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/categories")>();
  return {
    ...mod,
    createCategory: vi.fn(),
  };
});

vi.mock("../src/api/transactions", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...mod,
    updateTransaction: vi.fn().mockResolvedValue({}),
    deleteTransaction: vi.fn().mockResolvedValue(undefined),
    setTransactionReviewed: vi.fn().mockResolvedValue({}),
    addTransactionTag: vi.fn().mockResolvedValue({ items: [] }),
    bulkRecategorize: vi.fn().mockResolvedValue({ matched: 345, updated: 345, skipped: [] }),
    bulkSetPayee: vi.fn().mockResolvedValue({ matched: 345, updated: 345, skipped: [] }),
    bulkSetReviewed: vi.fn().mockResolvedValue({ matched: 345, updated: 345, skipped: [] }),
    bulkAddTag: vi.fn().mockResolvedValue({ matched: 345, updated: 345, skipped: [] }),
    bulkDelete: vi.fn().mockResolvedValue({ matched: 345, updated: 345, skipped: [] }),
  };
});

import { ApiError } from "../src/api/client";
import { createCategory } from "../src/api/categories";
import {
  bulkRecategorize,
  updateTransaction,
  type Transaction,
  type TransactionFilters,
} from "../src/api/transactions";
import { BulkActionsToolbar } from "../src/components/transactions/BulkActionsToolbar";

const CATEGORY = {
  id: 4,
  user_id: 1,
  name: "Groceries",
  parent_id: null,
  is_system: false,
};

function category(id: number, name: string) {
  return { id, user_id: 1, name, parent_id: null, is_system: false };
}

function tx(id: number): Transaction {
  return {
    id,
    account_id: 7,
    date: "2026-09-01",
    posting_date: null,
    raw_description: `RAW ${id}`,
    normalized_payee: `Merchant ${id}`,
    amount_native: "-12.00",
    currency: "CAD",
    cad_amount: "-12.00",
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
  } as unknown as Transaction;
}

const ROWS = [1, 2, 3].map(tx);
const FILTER: TransactionFilters = { accountId: 7, categoryId: 12 };

function mountToolbar({
  selectedIds = [1, 2, 3],
  matched = null as null | { filter: TransactionFilters; excludeIds: number[]; total: number },
  categories = [CATEGORY],
} = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const onSettled = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <BulkActionsToolbar
        rows={ROWS}
        selectedIds={selectedIds}
        matched={matched}
        matchedTotal={matched ? matched.total : null}
        categories={categories}
        tags={[]}
        onClear={vi.fn()}
        onSettled={onSettled}
      />
    </QueryClientProvider>,
  );
  return { invalidateSpy, onSettled };
}

/** Open the recategorize mode and type a fresh name into the shared picker. */
function openRecategorizeWith(name: string) {
  fireEvent.click(screen.getByRole("button", { name: "Recategorize" }));
  const input = screen.getByLabelText("Category for the selected transactions");
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: name } });
  return input as HTMLInputElement;
}

const createRow = () => screen.getByRole("button", { name: /^Create "/ });
const applyButton = () => screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;

describe("bulk category creation (M176)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(createCategory).mockResolvedValue(category(99, "Kid Stuff"));
  });

  afterEach(() => {
    vi.mocked(createCategory).mockReset();
  });

  it("creates a category, keeps it selected and visible, and assigns ONLY on explicit Apply", async () => {
    const { invalidateSpy, onSettled } = mountToolbar();
    openRecategorizeWith("Kid Stuff");

    // The create row is offered, and Apply is inert until a valid id exists.
    expect(createRow()).toBeDefined();
    expect(applyButton().disabled).toBe(true);

    fireEvent.click(createRow());
    await waitFor(() => expect(createCategory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createCategory)).toHaveBeenCalledWith({ name: "Kid Stuff" });

    // Creating alone mutates NOTHING on the ledger.
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
    expect(vi.mocked(bulkRecategorize)).not.toHaveBeenCalled();

    // Immediate visibility: the new name is the control's value before any
    // refetch could have answered.
    const input = screen.getByLabelText("Category for the selected transactions") as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("Kid Stuff"));
    expect(applyButton().disabled).toBe(false);

    // The category cache is refreshed for every other reader.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["categories"] });

    fireEvent.click(applyButton());
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledTimes(3);
    for (const id of [1, 2, 3]) {
      expect(vi.mocked(updateTransaction)).toHaveBeenCalledWith(id, { category_id: 99 });
    }
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalledWith(1, {
      category_id: CATEGORY.id,
    });
    expect(toastSpies.success).toHaveBeenCalledWith("Recategorized 3 of 3");
  });

  it("Cancel after creating keeps the selection and never assigns", async () => {
    mountToolbar();
    openRecategorizeWith("Kid Stuff");
    fireEvent.click(createRow());
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Category for the selected transactions") as HTMLInputElement).value,
      ).toBe("Kid Stuff"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
    // Selection and scope survived the create/cancel round trip.
    expect(screen.getByText("3 selected")).toBeDefined();
    expect(screen.getByRole("button", { name: "Recategorize" })).toBeDefined();
  });

  it("shows a refused create at the control, keeps the selection, and RETRIES", async () => {
    vi.mocked(createCategory)
      .mockRejectedValueOnce(new ApiError(409, "Category already exists", "duplicate_category"))
      .mockResolvedValueOnce(category(99, "Kid Stuff"));

    mountToolbar();
    openRecategorizeWith("Kid Stuff");
    fireEvent.click(createRow());

    const error = await screen.findByTestId("category-create-error");
    expect(error.textContent).toContain("Category already exists");
    expect(error.textContent).toContain("Kid Stuff");
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
    expect(screen.getByText("3 selected")).toBeDefined();
    // No id landed, so Apply is still unavailable.
    expect(applyButton().disabled).toBe(true);

    // Retry through the same control.
    fireEvent.click(createRow());
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Category for the selected transactions") as HTMLInputElement).value,
      ).toBe("Kid Stuff"),
    );
    expect(vi.mocked(createCategory)).toHaveBeenCalledTimes(2);
    expect(applyButton().disabled).toBe(false);
  });

  it("guards Apply while a create is pending and blocks a double submit", async () => {
    let resolveCreate: (value: ReturnType<typeof category>) => void = () => {};
    vi.mocked(createCategory).mockImplementation(
      () => new Promise((resolve) => (resolveCreate = resolve)),
    );

    mountToolbar();
    openRecategorizeWith("Kid Stuff");
    const button = createRow();
    fireEvent.click(button);
    // A second click while the first is still in flight is a no-op.
    fireEvent.click(button);
    expect(vi.mocked(createCategory)).toHaveBeenCalledTimes(1);
    expect(applyButton().disabled).toBe(true);

    resolveCreate(category(99, "Kid Stuff"));
    await waitFor(() => expect(applyButton().disabled).toBe(false));
    expect(vi.mocked(createCategory)).toHaveBeenCalledTimes(1);
  });

  it("matches the server scope: create then Apply is ONE request over filter + exceptions", async () => {
    const matched = { filter: FILTER, excludeIds: [2], total: 345 };
    const { onSettled } = mountToolbar({ matched });
    expect(screen.getByText("344 selected")).toBeDefined();

    openRecategorizeWith("Kid Stuff");
    fireEvent.click(createRow());
    await waitFor(() => expect(applyButton().disabled).toBe(false));

    fireEvent.click(applyButton());
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(bulkRecategorize)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(bulkRecategorize)).toHaveBeenCalledWith(FILTER, [2], 99);
    // The page path's per-row write is never driven for a matched set.
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
  });

  it("offers no Create row for a case/whitespace-equivalent existing name", () => {
    mountToolbar();
    const input = openRecategorizeWith("  groceries ");
    // The typed value stays as typed; the OFFER is normalized away.
    expect(input.value).toBe("  groceries ");
    expect(screen.queryByRole("button", { name: /^Create "/ })).toBeNull();

    fireEvent.change(input, { target: { value: "Green Grocer" } });
    expect(screen.getByRole("button", { name: 'Create "Green Grocer"' })).toBeDefined();
    expect(vi.mocked(createCategory)).not.toHaveBeenCalled();
  });

  it("opens the menu upward below md and downward at md", () => {
    mountToolbar();
    openRecategorizeWith("");
    const menu = screen.getByTestId("category-picker-menu");
    expect(menu.className).toContain("bottom-full");
    expect(menu.className).toContain("md:top-full");
  });

  it("creation controls are real buttons (keyboard Enter/Space and tap all fire click)", () => {
    mountToolbar();
    openRecategorizeWith("Kid Stuff");
    const create = createRow();
    expect(create.tagName).toBe("BUTTON");
    expect(create.getAttribute("type")).toBe("button");

    // Enter inside the text box does NOT create — the write stays an explicit
    // click on the Create control, so unrelated typing can't mint a category.
    fireEvent.keyDown(screen.getByLabelText("Category for the selected transactions"), {
      key: "Enter",
    });
    expect(vi.mocked(createCategory)).not.toHaveBeenCalled();
  });

  it("never creates a category from unrelated payee text", () => {
    mountToolbar();
    fireEvent.click(screen.getByRole("button", { name: "Set payee" }));
    fireEvent.change(screen.getByLabelText("Payee for the selected transactions"), {
      target: { value: "Kid Stuff" },
    });
    expect(screen.queryByRole("button", { name: /^Create "/ })).toBeNull();
    expect(vi.mocked(createCategory)).not.toHaveBeenCalled();
  });

  it("still assigns an EXISTING category through the shared picker (regression)", async () => {
    const { onSettled } = mountToolbar();
    fireEvent.click(screen.getByRole("button", { name: "Recategorize" }));
    fireEvent.focus(screen.getByLabelText("Category for the selected transactions"));
    fireEvent.click(screen.getByRole("button", { name: CATEGORY.name }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(createCategory)).not.toHaveBeenCalled();
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledTimes(3);
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledWith(1, { category_id: CATEGORY.id });
  });
});
