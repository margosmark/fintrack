// M96 B1 — bulk operations on /transactions (docs/09 §Bulk selection toolbar,
// docs/19). Three contracts:
//
//  1. selection lifecycle — single toggle, shift+click range over the visible
//     order, select-all-visible, and CLEAR on unmount (refresh/navigation) and
//     on any view change (filter/page/search). Selection is never persistent
//     state, so a bulk action can only ever see the page in front of the user.
//  2. execution — the toolbar drives the EXISTING single-row routes one row at
//     a time with a visible `n / total` progress readout (never a parallel
//     burst), and PARTIAL success is reported: accepted rows land, refused rows
//     are listed per row with the server's own `{detail, code}` text.
//  3. rollup invalidation — a bulk recategorize invalidates `budgets` and
//     `reports`, the category-rollup readers that print `pct_used`.
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
  };
});

import { ApiError } from "../src/api/client";
import {
  deleteTransaction,
  setTransactionReviewed,
  updateTransaction,
} from "../src/api/transactions";
import type { Transaction } from "../src/api/transactions";
import { BulkActionsToolbar } from "../src/components/transactions/BulkActionsToolbar";
import {
  extendFromAnchor,
  intersectSelected,
  setAllVisible,
  toggleOne,
  useRowSelection,
} from "../src/components/transactions/useRowSelection";

const CATEGORY = {
  id: 4,
  user_id: 1,
  name: "Groceries",
  parent_id: null,
  is_system: false,
};

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
    tags: [],
    payee_cipher: null,
    raw_desc_cipher: null,
    amount_cipher: null,
    cad_amount_cipher: null,
    payee_token: null,
  } as unknown as Transaction;
}

const ROWS = [1, 2, 3].map(tx);
const TAGS = [{ id: 9, user_id: 1, name: "vacation" }];

/** Hook probe: the exact wiring the page uses (click toggles, shift extends). */
function Probe({
  filterKey,
  pageKey = "0",
  ids,
  matchedTotal = null,
}: {
  filterKey: string;
  pageKey?: string;
  ids: number[];
  matchedTotal?: number | null;
}) {
  const selection = useRowSelection({ filterKey, pageKey, visibleIds: ids, matchedTotal });
  return (
    <div>
      {/* `count`, not `selected.size` — the hook's ONE number, which is the
          matched set's server total minus its exceptions while matched. */}
      <span data-testid="count">{selection.count}</span>
      <span data-testid="anchor">{selection.anchorId ?? "none"}</span>
      {ids.map((id) => (
        <input
          key={id}
          type="checkbox"
          aria-label={`row ${id}`}
          checked={selection.isSelected(id)}
          onChange={() => undefined}
          onClick={(event) => selection.toggle(id, event.shiftKey)}
        />
      ))}
      <button type="button" onClick={() => selection.setAllVisible(true)}>
        all
      </button>
      <button type="button" onClick={() => selection.headerToggle()}>
        header
      </button>
      <span data-testid="header-label">{selection.headerLabel}</span>
      <span data-testid="header-indeterminate">
        {String(selection.headerIndeterminate)}
      </span>
    </div>
  );
}

function mountProbe(filterKey = "v1", ids = [1, 2, 3, 4]) {
  return render(<Probe filterKey={filterKey} ids={ids} />);
}

const row = (id: number) => screen.getByLabelText(`row ${id}`) as HTMLInputElement;
const count = () => Number(screen.getByTestId("count").textContent);

describe("row selection lifecycle (docs/09 §Bulk selection toolbar)", () => {
  it("toggles a single row on and off", () => {
    mountProbe();
    fireEvent.click(row(2));
    expect(row(2).checked).toBe(true);
    expect(count()).toBe(1);
    fireEvent.click(row(2));
    expect(row(2).checked).toBe(false);
    expect(count()).toBe(0);
  });

  it("extends with shift+click across the visible order and re-anchors on a plain click", () => {
    mountProbe();
    fireEvent.click(row(1));
    fireEvent.click(row(4), { shiftKey: true });
    expect(count()).toBe(4);
    expect(screen.getByTestId("anchor").textContent).toBe("1");

    // Plain click drops that row AND re-anchors the range here.
    fireEvent.click(row(4));
    expect(count()).toBe(3);
    expect(screen.getByTestId("anchor").textContent).toBe("4");
    // A shift from the NEW anchor adds its span back.
    fireEvent.click(row(2), { shiftKey: true });
    expect([row(2).checked, row(3).checked, row(4).checked]).toEqual([
      true,
      true,
      true,
    ]);
    expect(count()).toBe(4);
  });

  it("select-all covers exactly the visible page", () => {
    mountProbe();
    fireEvent.click(screen.getByText("all"));
    expect(count()).toBe(4);
    // Header semantics: dropping one visible row leaves the rest selected.
    fireEvent.click(row(3));
    expect(count()).toBe(3);
  });

  it("clears on unmount (refresh/navigation) — selection never survives the mount", () => {
    const view = mountProbe();
    fireEvent.click(row(1));
    expect(count()).toBe(1);
    view.unmount();
    mountProbe();
    expect(count()).toBe(0);
  });

  it("clears when the FILTER key changes (filter / search mode)", () => {
    const view = render(<Probe filterKey="filters:v1" ids={[1, 2]} />);
    fireEvent.click(row(1));
    expect(count()).toBe(1);
    view.rerender(<Probe filterKey="filters:v2" ids={[1, 2]} />);
    expect(count()).toBe(0);
    expect(screen.getByTestId("anchor").textContent).toBe("none");
  });

  // M125 (docs/09 §Select-all-matched): a PAGE change is a different set of
  // visible ids, so a page-scoped selection dies with its page — but the
  // matched set is defined by the FILTER and survives paging, because that is
  // the entire point of "select all 345". Two scopes, two rules, no state
  // where a row looks selected while being outside what an action would touch.
  it("drops a page-scoped selection on a page change and keeps the matched one", () => {
    const view = render(
      <Probe filterKey="f" pageKey="0" ids={[1, 2]} matchedTotal={500} />,
    );
    fireEvent.click(row(1));
    expect(count()).toBe(1);
    view.rerender(<Probe filterKey="f" pageKey="50" ids={[3, 4]} matchedTotal={500} />);
    expect(count()).toBe(0);

    fireEvent.click(screen.getByText("all"));
    expect(count()).toBe(2);
    fireEvent.click(screen.getByText("header"));
    expect(count()).toBe(500);
    view.rerender(<Probe filterKey="f" pageKey="100" ids={[5, 6]} matchedTotal={500} />);
    expect(count()).toBe(500);
    expect(row(5).checked).toBe(true);
  });
});

describe("selection helpers", () => {
  it("extendFromAnchor refuses a stale anchor instead of selecting an arbitrary span", () => {
    expect(extendFromAnchor(new Set([1]), [1, 2, 3], 99, 3)).toBeNull();
    expect(extendFromAnchor(new Set(), [1, 2, 3], null, 3)).toBeNull();
    expect([...extendFromAnchor(new Set(), [1, 2, 3], 3, 1)!].sort()).toEqual([
      1, 2, 3,
    ]);
  });

  it("setAllVisible drops only visible ids and intersectSelected is the action set", () => {
    const selected = setAllVisible(new Set<number>(), [1, 2], true);
    expect([...selected]).toEqual([1, 2]);
    expect([...setAllVisible(selected, [1, 2], false)]).toEqual([]);
    // A selection carrying an id that left the view can never reach the API.
    expect(intersectSelected(new Set([1, 99]), [1, 2])).toEqual([1]);
    expect([...toggleOne(new Set([1]), 1)]).toEqual([]);
  });
});

describe("BulkActionsToolbar", () => {
  function mountToolbar(selectedIds: number[] = [1, 2, 3]) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const onSettled = vi.fn();
    const onClear = vi.fn();
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <BulkActionsToolbar
            rows={ROWS}
            selectedIds={selectedIds}
            categories={[CATEGORY]}
            tags={TAGS}
            onClear={onClear}
            onSettled={onSettled}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    return { invalidateSpy, onSettled, onClear };
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.mocked(window.confirm).mockRestore();
  });

  it("always shows and announces the count, and clears on request", () => {
    const { onClear } = mountToolbar([1, 2]);
    const live = screen.getByText("2 selected");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.getAttribute("aria-atomic")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalled();
  });

  it("bulk recategorize patches every row and invalidates the rollup caches", async () => {
    const { invalidateSpy, onSettled } = mountToolbar();
    fireEvent.click(screen.getByRole("button", { name: "Recategorize" }));
    // M176: the mode's control is now the shared CategoryPicker, so an
    // existing category is chosen by focusing the combo and clicking its row.
    fireEvent.focus(screen.getByLabelText("Category for the selected transactions"));
    fireEvent.click(screen.getByRole("button", { name: CATEGORY.name }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledTimes(3);
    for (const id of [1, 2, 3]) {
      expect(vi.mocked(updateTransaction)).toHaveBeenCalledWith(id, {
        category_id: CATEGORY.id,
      });
    }
    // The category-rollup readers (`pct_used`, report splits) must re-read.
    for (const key of ["budgets", "reports", "transactions"]) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: [key] });
    }
    expect(toastSpies.success).toHaveBeenCalledWith("Recategorized 3 of 3");
    expect(onSettled).toHaveBeenCalledWith({
      action: "recategorize",
      outcome: { okIds: [1, 2, 3], okCount: 3, attempted: 3, conflicts: [] },
    });
  });

  it("runs DELETE rows SEQUENTIALLY with a visible progress readout", async () => {
    const pending: Array<() => void> = [];
    vi.mocked(deleteTransaction).mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          pending.push(() => resolve());
        }),
    );
    const { onSettled } = mountToolbar();
    fireEvent.click(screen.getByRole("button", { name: /Delete 3 selected/ }));
    // Count-first, and honest that a BATCH has no undo (docs/09 §Destructive
    // actions) — the M95 per-row undo toast never covers a run.
    expect(window.confirm).toHaveBeenCalledWith(
      "Delete 3 transactions? This cannot be undone via this batch.",
    );

    // One in-flight request, not three: the batch never bursts the route.
    await waitFor(() => expect(deleteTransaction).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("bulk-progress").textContent).toBe("0 / 3");

    pending.shift()?.();
    await waitFor(() => expect(deleteTransaction).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("bulk-progress").textContent).toBe("1 / 3");

    pending.shift()?.();
    await waitFor(() => expect(deleteTransaction).toHaveBeenCalledTimes(3));
    expect(screen.getByTestId("bulk-progress").textContent).toBe("2 / 3");

    pending.shift()?.();
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(toastSpies.success).toHaveBeenCalledWith("Deleted 3 of 3");
  });

  it("reports a refused row as partial success with its own notice, never silently", async () => {
    vi.mocked(deleteTransaction).mockImplementation((id: number) =>
      id === 2
        ? Promise.reject(
            new ApiError(
              409,
              "Transaction belongs to a pending import batch",
              "transaction_in_pending_batch",
            ),
          )
        : Promise.resolve(),
    );
    const { onSettled } = mountToolbar();
    fireEvent.click(screen.getByRole("button", { name: /Delete 3 selected/ }));

    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("Deleted 2 of 3 — 1 skipped");
    expect(notice.textContent).toContain("the skipped rows are still in your ledger");
    // Per-row line: which row, the server's detail, and its envelope code.
    expect(notice.textContent).toContain("Merchant 2");
    expect(notice.textContent).toContain(
      "Transaction belongs to a pending import batch",
    );
    expect(notice.textContent).toContain("(transaction_in_pending_batch)");
    expect(toastSpies.error).toHaveBeenCalledWith(
      "Deleted 2 of 3 — 1 skipped",
      "Skipped rows are listed below.",
    );
    expect(onSettled).toHaveBeenCalledWith({
      action: "delete",
      outcome: {
        okIds: [1, 3],
        okCount: 2,
        attempted: 3,
        conflicts: [
          {
            id: 2,
            status: 409,
            code: "transaction_in_pending_batch",
            detail: "Transaction belongs to a pending import batch",
          },
        ],
      },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss skipped-rows notice" }),
    );
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("bulk payee rename patches every row with the typed payee", async () => {
    const { onSettled } = mountToolbar([1, 2]);
    fireEvent.click(screen.getByRole("button", { name: "Set payee" }));
    fireEvent.change(screen.getByLabelText("Payee for the selected transactions"), {
      target: { value: "Superstore" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(updateTransaction)).toHaveBeenCalledWith(1, {
      normalized_payee: "Superstore",
    });
    expect(toastSpies.success).toHaveBeenCalledWith("Renamed 2 of 2");
  });

  it("bulk review flips every selected row through the ONE single-row route", async () => {
    const { onSettled } = mountToolbar([1, 2]);
    fireEvent.click(
      screen.getByRole("button", { name: "Mark 2 selected transactions reviewed" }),
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());

    // N rows are N requests — no bulk schema, so each row leaves its own
    // `reviewed` receipt server-side (docs/09 §Bulk selection toolbar).
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenNthCalledWith(1, 1, true);
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenNthCalledWith(2, 2, true);
    expect(toastSpies.success).toHaveBeenCalledWith("Reviewed 2 of 2");
    expect(onSettled).toHaveBeenCalledWith({
      action: "review",
      outcome: { okIds: [1, 2], okCount: 2, attempted: 2, conflicts: [] },
    });

    // The inverse direction is its own action, not a toggle guess.
    vi.mocked(setTransactionReviewed).mockClear();
    onSettled.mockClear();
    fireEvent.click(
      screen.getByRole("button", { name: "Mark 2 selected transactions unreviewed" }),
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenNthCalledWith(1, 1, false);
    expect(vi.mocked(setTransactionReviewed)).toHaveBeenNthCalledWith(2, 2, false);
    expect(toastSpies.success).toHaveBeenCalledWith("Unreviewed 2 of 2");
  });

  it("shows nothing at all when nothing is selected", () => {
    mountToolbar([]);
    expect(screen.queryByText(/selected/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });
});

describe("checkbox touch targets + column contract (source scan)", () => {
  const source = readFileSync(
    path.resolve(__dirname, "../src/pages/Transactions.tsx"),
    "utf8",
  );

  it("gives every selection checkbox a 44px hit area below md", () => {
    // jsdom has no layout, so the box is asserted where it is declared — the
    // same method m72Mobile uses for the CSS media-query floor.
    const cells = source.match(
      /<span className="flex min-h-11 min-w-11[^"]*">/g,
    );
    expect(cells).not.toBeNull();
    expect(cells!.length).toBe(2); // header select-all + row checkbox
    for (const cell of cells!) {
      expect(cell).toContain("md:min-h-0");
      expect(cell).toContain("md:min-w-0");
    }
  });

  // M106 added the review column, so the wide-table spans moved 9 → 10 — the
  // assertion is the SAME honesty check against the column count in docs/09.
  // M136 (A18) added a FOURTH full-width row: the locked-search notice that
  // replaces the empty state. Every one of them still spans all 10 columns.
  it("keeps the wide-table spans honest: one new column, colSpan 10", () => {
    expect(source.match(/colSpan=\{10\}/g)?.length).toBe(4);
    expect(source).not.toContain("colSpan={9}");
  });
});
