// M125 — select-all-matched: the header checkbox's third state, the matched
// set's server-side execution, and the account view's copy of the toolbar
// (docs/09 §Bulk selection toolbar §Select-all-matched, docs/08 §Bulk over a
// matched set).
//
// Three contracts:
//
//  1. STATE MACHINE — tick 1 selects the page, tick 2 selects EVERY row the
//     filter matches with the SERVER's `total` as the counter (never a client
//     page-sum), tick 3 clears. Deselecting a row out of the matched set
//     becomes an EXCEPTION: the count drops by one, the header goes
//     indeterminate, and the set stays server-described. When the matched set
//     is no bigger than the page there is no second tick — an "all matched"
//     state that could only re-select what tick 1 already selected is a lie
//     of a control, and so is one in a view the server never counts (the
//     fuzzy search scores in the browser, docs/33 S5).
//
//  2. EXECUTION — a marked matched set makes ONE request carrying the page's
//     filter plus its exceptions; the single-row routes are never driven. The
//     delete confirmation is count-first and the count it prints travels with
//     the request as `expected_count`.
//
//  3. THE ACCOUNT VIEW — the same toolbar, the same select column, and a bulk
//     body whose filter carries THIS page's account id.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}));

vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => toastSpies,
}));

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

import {
  addTransactionTag,
  bulkAddTag,
  bulkDelete,
  bulkRecategorize,
  bulkSetPayee,
  bulkSetReviewed,
  buildBulkFilter,
  deleteTransaction,
  setTransactionReviewed,
  updateTransaction,
  type Transaction,
} from "../src/api/transactions";
import { ApiError } from "../src/api/client";
import { BulkActionsToolbar } from "../src/components/transactions/BulkActionsToolbar";
import { useRowSelection } from "../src/components/transactions/useRowSelection";

const CATEGORY = {
  id: 4,
  user_id: 1,
  name: "Groceries",
  parent_id: null,
  is_system: false,
};
const TAG = { id: 9, user_id: 1, name: "vacation" };

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

const PAGE_IDS = [1, 2, 3, 4];

/** The page's wiring: header checkbox + row checkboxes + the counter. */
function Probe({
  filterKey = "f:v1",
  pageKey = "0",
  ids = PAGE_IDS,
  matchedTotal = null,
}: {
  filterKey?: string;
  pageKey?: string;
  ids?: number[];
  matchedTotal?: number | null;
}) {
  const selection = useRowSelection({ filterKey, pageKey, visibleIds: ids, matchedTotal });
  return (
    <div>
      <span data-testid="count">{selection.count}</span>
      <span data-testid="mode">{selection.mode}</span>
      <span data-testid="label">{selection.headerLabel}</span>
      <span data-testid="indeterminate">{String(selection.headerIndeterminate)}</span>
      <span data-testid="checked">{String(selection.headerChecked)}</span>
      <button type="button" onClick={() => selection.headerToggle()}>
        header
      </button>
      <ul>
        {ids.map((id) => (
          <li key={id}>
            <input
              type="checkbox"
              aria-label={`row ${id}`}
              checked={selection.isSelected(id)}
              onChange={() => undefined}
              onClick={(event) => selection.toggle(id, event.shiftKey)}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

const row = (id: number) => screen.getByLabelText(`row ${id}`) as HTMLInputElement;
const count = () => Number(screen.getByTestId("count").textContent);
const label = () => screen.getByTestId("label").textContent ?? "";
const indeterminate = () => screen.getByTestId("indeterminate").textContent === "true";
const headerBox = () => screen.getByRole("button", { name: "header" });

describe("select-all-matched — the header checkbox's three states", () => {
  it("cycles page → matched → clear, with the SERVER total as the counter", () => {
    render(<Probe matchedTotal={500} />);

    // Nothing yet: the label offers the page, and only the page.
    expect(count()).toBe(0);
    expect(label()).toBe("Select every transaction on this page");

    // Tick 1 — the visible page (M96, unchanged).
    fireEvent.click(headerBox());
    expect(count()).toBe(4);
    expect(screen.getByTestId("mode").textContent).toBe("page");
    expect(screen.getByTestId("checked").textContent).toBe("true");
    // …and now the NEXT state is named: a second tick reaches 500.
    expect(label()).toBe("Select all 500 matched transactions");

    // Tick 2 — EVERY matched row, counted by the server, not summed here.
    fireEvent.click(headerBox());
    expect(count()).toBe(500);
    expect(screen.getByTestId("mode").textContent).toBe("matched");
    expect(indeterminate()).toBe(false);
    expect(row(1).checked).toBe(true);
    expect(row(4).checked).toBe(true);
    expect(label()).toBe("Clear selection — 500 matched transactions selected");

    // Tick 3 — clear.
    fireEvent.click(headerBox());
    expect(count()).toBe(0);
    expect(screen.getByTestId("mode").textContent).toBe("page");
    expect(row(1).checked).toBe(false);
  });

  it("drops a deselected row as an EXCEPTION — count adjusted, header indeterminate", () => {
    render(<Probe matchedTotal={500} />);
    fireEvent.click(headerBox());
    fireEvent.click(headerBox());
    expect(count()).toBe(500);

    // Deselecting one row leaves the "all matched" state: the count is
    // adjusted by exactly one, the header reports the partial truth, and the
    // selection is STILL the server-described set (matched minus one), so the
    // body carries `{filter, exclude_ids}` rather than collapsing to a page.
    fireEvent.click(row(3));
    expect(screen.getByTestId("mode").textContent).toBe("matched");
    expect(count()).toBe(499);
    expect(row(3).checked).toBe(false);
    expect(row(1).checked).toBe(true);
    expect(indeterminate()).toBe(true);
    expect(label()).toBe("Select all 500 matched transactions");

    // Indeterminate → checked: the exception comes off and the whole set is
    // marked again, without leaving matched mode.
    fireEvent.click(headerBox());
    expect(count()).toBe(500);
    expect(indeterminate()).toBe(false);
    expect(screen.getByTestId("mode").textContent).toBe("matched");
  });

  it("offers no second tick when the matched set fits on the page", () => {
    render(<Probe matchedTotal={4} />);
    fireEvent.click(headerBox());
    expect(count()).toBe(4);
    // No bigger set exists — the next tick clears instead of pretending to
    // reach further.
    expect(label()).toBe("Clear selection");
    fireEvent.click(headerBox());
    expect(count()).toBe(0);
  });

  it("keeps M96's page behaviour where the server never counts the set", () => {
    // `matchedTotal === null` is the fuzzy-search view: its results are scored
    // in the browser (docs/33 S5), so there is no server `total` to select.
    render(<Probe matchedTotal={null} />);
    fireEvent.click(headerBox());
    expect(count()).toBe(4);
    expect(label()).toBe("Clear selection");
    fireEvent.click(headerBox());
    expect(count()).toBe(0);
  });

  it("survives a PAGE change while matched, and only a filter change clears", () => {
    const view = render(<Probe filterKey="f:v1" pageKey="0" matchedTotal={500} />);
    fireEvent.click(headerBox());
    fireEvent.click(headerBox());
    expect(count()).toBe(500);

    view.rerender(<Probe filterKey="f:v1" pageKey="50" matchedTotal={500} />);
    expect(count()).toBe(500);
    expect(row(1).checked).toBe(true);

    view.rerender(<Probe filterKey="f:v2" pageKey="50" matchedTotal={500} />);
    expect(count()).toBe(0);
  });
});

describe("buildBulkFilter — one mapping, two routes", () => {
  it("turns the page's filters into the body `GET /transactions` takes as a query", () => {
    const filters = {
      accountId: 7,
      categoryId: "uncategorized" as const,
      from: "2026-04-01",
      to: "2026-06-30",
      payee: "metro",
      amountMin: -14,
      amountMax: -13,
      direction: "paid" as const,
      tagId: 3,
      tagIds: [3, 4],
      reviewed: false,
    };
    expect(buildBulkFilter(filters)).toEqual({
      account_id: 7,
      category_id: "null",
      from: "2026-04-01",
      to: "2026-06-30",
      payee: "metro",
      amount_min: -14,
      amount_max: -13,
      direction: "paid",
      tag_id: 3,
      tag_ids: [3, 4],
      reviewed: false,
    });
    // A filter with nothing set is an EMPTY body — "no filter", not "all
    // fields defaulted to 0", which is what would silently widen the set.
    expect(buildBulkFilter({})).toEqual({});
  });
});

describe("BulkActionsToolbar — the matched set's single request", () => {
  const FILTER = { accountId: 7, categoryId: 12 };

  function mountToolbar({
    matched = null,
    matchedTotal = null,
    selectedIds = [] as number[],
    rows = PAGE_IDS.map(tx),
    onSelectAllMatched,
  }: {
    matched?: {
      filter: typeof FILTER;
      excludeIds: number[];
      total: number;
    } | null;
    matchedTotal?: number | null;
    selectedIds?: number[];
    rows?: Transaction[];
    onSelectAllMatched?: () => void;
  } = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const onSettled = vi.fn();
    const onClear = vi.fn();
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <BulkActionsToolbar
            rows={rows}
            selectedIds={selectedIds}
            matched={matched}
            matchedTotal={matchedTotal}
            onSelectAllMatched={onSelectAllMatched}
            categories={[CATEGORY]}
            tags={[TAG]}
            onClear={onClear}
            onSettled={onSettled}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    return { invalidateSpy, onSettled, onClear };
  }

  const MATCHED = { filter: FILTER, excludeIds: [] as number[], total: 345 };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.mocked(window.confirm).mockRestore();
  });

  it("recategorizes the MATCHED set in ONE request and never drives a single-row route", async () => {
    const { onSettled } = mountToolbar({ matched: MATCHED, matchedTotal: 345 });
    expect(screen.getByText("345 selected")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Recategorize" }));
    // M176: the mode's control is the shared CategoryPicker — choose the
    // existing category through it (focus, then click its row).
    fireEvent.focus(screen.getByLabelText("Category for the selected transactions"));
    fireEvent.click(screen.getByRole("button", { name: CATEGORY.name }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(bulkRecategorize)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(bulkRecategorize)).toHaveBeenCalledWith(FILTER, [], CATEGORY.id);
    // 345 rows, one request — the per-row route is the PAGE path only.
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
    expect(toastSpies.success).toHaveBeenCalledWith("Recategorized 345 of 345");
    expect(onSettled).toHaveBeenCalledWith({
      action: "recategorize",
      outcome: { okIds: [], okCount: 345, attempted: 345, conflicts: [] },
    });
  });

  it("confirms delete with the EXACT count and sends it back as expected_count", async () => {
    const { onSettled } = mountToolbar({ matched: MATCHED, matchedTotal: 345 });
    fireEvent.click(screen.getByRole("button", { name: /Delete 345 selected/ }));

    expect(window.confirm).toHaveBeenCalledWith(
      "Delete 345 transactions? This cannot be undone via this batch.",
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(bulkDelete)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(bulkDelete)).toHaveBeenCalledWith(FILTER, [], 345);
    expect(vi.mocked(deleteTransaction)).not.toHaveBeenCalled();
    expect(toastSpies.success).toHaveBeenCalledWith("Deleted 345 of 345");
  });

  it("counts the exceptions, not the raw total, in the confirmation", async () => {
    mountToolbar({
      matched: { filter: FILTER, excludeIds: [9, 8, 7], total: 345 },
      matchedTotal: 345,
    });
    expect(screen.getByText("342 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Delete 342 selected/ }));
    expect(window.confirm).toHaveBeenCalledWith(
      "Delete 342 transactions? This cannot be undone via this batch.",
    );
    await waitFor(() =>
      expect(vi.mocked(bulkDelete)).toHaveBeenCalledWith(FILTER, [9, 8, 7], 342),
    );
  });

  it("drives review, unreview and the NEW add-tag action through the matched path", async () => {
    const { onSettled } = mountToolbar({ matched: MATCHED, matchedTotal: 345 });

    fireEvent.click(
      screen.getByRole("button", { name: "Mark 345 selected transactions reviewed" }),
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(bulkSetReviewed)).toHaveBeenCalledWith(FILTER, [], true);
    expect(vi.mocked(setTransactionReviewed)).not.toHaveBeenCalled();
    expect(toastSpies.success).toHaveBeenCalledWith("Reviewed 345 of 345");

    onSettled.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Add tag" }));
    fireEvent.change(screen.getByLabelText("Tag to add to the selected transactions"), {
      target: { value: String(TAG.id) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(bulkAddTag)).toHaveBeenCalledWith(FILTER, [], TAG.id);
    expect(vi.mocked(addTransactionTag)).not.toHaveBeenCalled();
    expect(toastSpies.success).toHaveBeenCalledWith("Tagged 345 of 345");
  });

  it("renames the matched set with ONE payee computation", async () => {
    mountToolbar({ matched: MATCHED, matchedTotal: 345 });
    fireEvent.click(screen.getByRole("button", { name: "Set payee" }));
    fireEvent.change(screen.getByLabelText("Payee for the selected transactions"), {
      target: { value: "Superstore" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(vi.mocked(bulkSetPayee)).toHaveBeenCalledTimes(1));
    // Vault locked → no cipher, exactly like the page path's plaintext branch.
    expect(vi.mocked(bulkSetPayee)).toHaveBeenCalledWith(FILTER, [], "Superstore", undefined);
    expect(vi.mocked(updateTransaction)).not.toHaveBeenCalled();
  });

  it("keeps the selection when the server refuses the run (409 bulk_count_mismatch)", async () => {
    vi.mocked(bulkDelete).mockRejectedValueOnce(
      new ApiError(
        409,
        "This filter now matches 347 transactions, not 345. Nothing was changed.",
        "bulk_count_mismatch",
      ),
    );
    const { onSettled, invalidateSpy } = mountToolbar({
      matched: MATCHED,
      matchedTotal: 345,
    });
    fireEvent.click(screen.getByRole("button", { name: /Delete 345 selected/ }));

    await waitFor(() => expect(toastSpies.error).toHaveBeenCalled());
    expect(toastSpies.error.mock.calls[0][0]).toBe("Bulk action did not run");
    // The SERVER's sentence, verbatim — it names the new count to confirm.
    expect(toastSpies.error.mock.calls[0][1]).toContain(
      "This filter now matches 347 transactions",
    );
    // Nothing settled: the selection is kept so the NEW count can be confirmed.
    expect(onSettled).not.toHaveBeenCalled();
    expect(invalidateSpy).toHaveBeenCalled();
    expect(screen.getByText("345 selected")).toBeTruthy();
  });

  it("names a skipped row the browser never loaded, from the server's own label", async () => {
    vi.mocked(bulkDelete).mockResolvedValueOnce({
      matched: 345,
      updated: 340,
      skipped: [
        {
          id: 4711,
          label: "COSTCO WHOLESALE",
          code: "split_leg_immutable",
          detail: "This row is part of a split. Change an amount by re-splitting, or merge the parts back.",
        },
      ],
    });
    mountToolbar({ matched: MATCHED, matchedTotal: 345 });
    fireEvent.click(screen.getByRole("button", { name: /Delete 345 selected/ }));

    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("Deleted 340 of 345 — 1 skipped");
    expect(notice.textContent).toContain("COSTCO WHOLESALE");
    expect(notice.textContent).toContain("(split_leg_immutable)");
  });

  it("offers the visible 'Select all N matched' affordance only while the page is fully selected", () => {
    const onSelectAllMatched = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (selectedIds: number[]) => (
      <QueryClientProvider client={client}>
        <BulkActionsToolbar
          rows={PAGE_IDS.map(tx)}
          selectedIds={selectedIds}
          matchedTotal={500}
          onSelectAllMatched={onSelectAllMatched}
          categories={[CATEGORY]}
          tags={[TAG]}
          onClear={vi.fn()}
          onSettled={vi.fn()}
        />
      </QueryClientProvider>
    );
    // Partially selected: the affordance would promise more than the tick does.
    const view = render(tree([1, 2]));
    expect(screen.queryByRole("button", { name: "Select all 500 matched transactions" })).toBeNull();
    view.rerender(tree([1, 2, 3, 4]));
    const button = screen.getByRole("button", { name: "Select all 500 matched transactions" });
    fireEvent.click(button);
    expect(onSelectAllMatched).toHaveBeenCalledTimes(1);
  });
});

describe("the account view mounts the SAME machinery (source scan)", () => {
  const account = readFileSync(
    path.resolve(__dirname, "../src/pages/AccountDetail.tsx"),
    "utf8",
  );
  const split = readFileSync(
    path.resolve(__dirname, "../src/components/transactions/SplitGroupRow.tsx"),
    "utf8",
  );
  const register = readFileSync(
    path.resolve(__dirname, "../src/pages/Transactions.tsx"),
    "utf8",
  );

  it("mounts the toolbar with a bulk filter that carries THIS page's account id", () => {
    expect(account).toContain("<BulkActionsToolbar");
    expect(account).toContain("const bulkFilter = useMemo(() => ({ ...filters, accountId: id })");
    // The two-step proof's server half: the body's filter is the list's, so
    // "apply an account filter → select all matched → bulk applies" can only
    // ever reach this account's rows.
    expect(account).toContain("filter: bulkFilter");
    expect(account).toContain("matchedTotal={matchedTotal}");
  });

  it("adds the selection column — 8 spans, and the split layout agrees", () => {
    expect(account.match(/colSpan=\{8\}/g)?.length).toBe(3);
    expect(account).not.toContain("colSpan={7}");
    expect(account.match(/min-h-11 min-w-11/g)?.length).toBe(2); // header + row
    const layout = split.split("ACCOUNT_SPLIT_LAYOUT")[1] ?? "";
    expect(layout).toContain('"select"');
    // The register keeps its own 10-column contract (M64 + M106); M136's
    // locked-search notice is the fourth full-width row and spans them too.
    expect(register.match(/colSpan=\{10\}/g)?.length).toBe(4);
  });
});
