// Regression: the AccountDetail Transfer Suggestions panel rendered malformed
// (Mark's screenshot) — each row was its OWN inline flex row (amount | "on" |
// date | (account)), so nothing was shared between the two rows: the date
// column staggered 5 px against the pair's other row (the minus sign widens
// one amount), the account label broke mid-label ("(CIBC" / "Chequing)"), the
// date stacked "2026-" / "09-12", the card grew 14 px wider than a 360 px
// viewport, and a payee of "-" (a real value from the data) printed as its
// own stray truncated line.
//
// The contract now: ONE grid holds BOTH rows on a single shared track, every
// line carries data-side="source|candidate" + data-cell="amount|date|
// account|payee", every line is `truncate` (overflow hidden → ellipsis: never
// a mid-label break, never a shove past the buttons), and a "-" / blank payee
// renders no cell at all. jsdom has no layout engine, so geometry (both rows'
// cells at the same x, no text-node overlap at 320/360/390) is measured by
// `scripts/a11y/transfer_suggestions_probe.py`; this file pins the structure
// that makes that geometry hold.
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { TransferSuggestions } from "../src/components/transactions/TransferSuggestions";
import type { TransferSuggestion } from "../src/api/transactions";
import { ToastProvider } from "../src/components/ui/Toast";

function suggestion(overrides: Partial<TransferSuggestion> = {}): TransferSuggestion {
  return {
    source_id: 1,
    source_account_id: 10,
    source_account_name: "CIBC Chequing",
    source_amount: "85.00",
    source_date: "2026-09-12",
    source_payee: "Interac e-Transfer",
    candidate_id: 2,
    candidate_account_id: 20,
    candidate_account_name: "Tangerine Savings",
    candidate_amount: "-85.00",
    candidate_date: "2026-09-13",
    candidate_payee: "Interac e-Transfer",
    ...overrides,
  };
}

function renderPanel(items: TransferSuggestion[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <TransferSuggestions suggestions={items} accountCurrency="CAD" />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/** The pair's shared grid — exactly one per suggestion. */
function grids(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>("div")].filter((el) =>
    el.className.includes("grid-cols-"),
  );
}

function cell(container: HTMLElement, side: string, name: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-side="${side}"][data-cell="${name}"]`);
}

describe("transfer suggestions pair grid (t_06c510ef — malformed panel)", () => {
  it("renders the pair as ONE single-track grid holding both rows", () => {
    const { container } = renderPanel([suggestion()]);
    const all = grids(container);
    expect(all).toHaveLength(1);
    const grid = all[0];
    // one shared track: every cell of both rows is a direct child of it, so
    // amount/date/account/payee of row 1 and row 2 cannot help but align.
    expect(grid.className).toContain("grid-cols-1");
    expect([...grid.children]).toHaveLength(8);
    expect(
      [...grid.children].map(
        (c) => `${c.getAttribute("data-side")}/${c.getAttribute("data-cell")}`,
      ),
    ).toEqual([
      "source/amount",
      "source/date",
      "source/account",
      "source/payee",
      "candidate/amount",
      "candidate/date",
      "candidate/account",
      "candidate/payee",
    ]);
    expect(cell(container, "source", "amount")?.textContent).toBe("$85.00");
    expect(cell(container, "candidate", "amount")?.textContent).toBe("-$85.00");
    expect(cell(container, "source", "amount")?.className).toContain("tabular-nums");
    expect(cell(container, "candidate", "amount")?.className).toContain("tabular-nums");
  });

  it("keeps both rows on the shared track when an account name is missing", () => {
    const { container } = renderPanel([
      suggestion({ source_account_name: null, candidate_account_name: null }),
    ]);
    const [grid] = grids(container);
    // 6 cells (no account lines) — still one track, still labelled by side
    expect([...grid.children]).toHaveLength(6);
    expect(cell(container, "source", "account")).toBeNull();
    expect(cell(container, "candidate", "account")).toBeNull();
    expect(cell(container, "source", "amount")?.textContent).toBe("$85.00");
    expect(cell(container, "candidate", "amount")?.textContent).toBe("-$85.00");
  });

  it("separates the candidate row with its own gap", () => {
    const { container } = renderPanel([suggestion()]);
    expect(cell(container, "source", "amount")?.className).not.toContain("mt-1.5");
    expect(cell(container, "candidate", "amount")?.className).toContain("mt-1.5");
  });

  it('renders no payee cell for a "-" payee, on either side', () => {
    const { container } = renderPanel([
      suggestion({ source_payee: "-", candidate_payee: "-" }),
    ]);
    expect(cell(container, "source", "payee")).toBeNull();
    expect(cell(container, "candidate", "payee")).toBeNull();
    // no stray "-" text node anywhere in the card
    const card = grids(container)[0].parentElement as HTMLElement;
    const dashNodes = [...card.querySelectorAll("*")].filter(
      (el) => el.children.length === 0 && el.textContent?.trim() === "-",
    );
    expect(dashNodes).toHaveLength(0);
    // the date lines still render — only the description line is dropped
    expect(cell(container, "source", "date")?.textContent).toBe("2026-09-12");
    expect(cell(container, "candidate", "date")?.textContent).toBe("2026-09-13");
  });

  it("keeps a real payee and drops a blank one", () => {
    const { container } = renderPanel([
      suggestion({ source_payee: "COSTCO WHOLESALE #001", candidate_payee: "   " }),
    ]);
    expect(cell(container, "source", "payee")?.textContent).toBe("COSTCO WHOLESALE #001");
    expect(cell(container, "candidate", "payee")).toBeNull();
  });

  it("ellipsizes the account label instead of wrapping it mid-label", () => {
    const long = "Very Long Institutional Chequing (USD Foreign Currency Wrap Me)";
    const { container } = renderPanel([suggestion({ source_account_name: long })]);
    const account = cell(container, "source", "account") as HTMLElement;
    // one text node, overflow hidden + ellipsis + nowrap — "(Very" / "Long"
    // mid-label breaks are impossible by construction
    expect(account.textContent).toBe(`(${long})`);
    expect(account.children).toHaveLength(0);
    expect(account.className).toContain("truncate");
    expect(account.className).toContain("text-xs");
    expect(account.parentElement).toBe(grids(container)[0]);
  });

  it("keeps every line on the shared track and clipped (nothing can overflow)", () => {
    const { container } = renderPanel([suggestion()]);
    const [grid] = grids(container);
    for (const node of [...grid.children] as HTMLElement[]) {
      expect(node.className).toContain("truncate");
      expect(node.parentElement).toBe(grid);
    }
  });

  it("keeps the action buttons a shrink-0 sibling of the grid (never inside it)", () => {
    const { container } = renderPanel([suggestion()]);
    const grid = grids(container)[0];
    const card = grid.parentElement as HTMLElement;
    const buttonBar = [...card.children].at(-1) as HTMLElement;
    expect(buttonBar.className).toContain("shrink-0");
    const buttons = buttonBar.querySelectorAll("button");
    expect(buttons).toHaveLength(2);
    // the bar is NOT inside the grid — buttons can't be squeezed into a track
    expect(grid.contains(buttons[0])).toBe(false);
    expect(buttons[0].title).toBe("Accept transfer suggestion");
    expect(buttons[1].title).toBe("Dismiss");
  });

  it("renders the empty state when there is nothing to pair", () => {
    const { container } = renderPanel([]);
    expect(container.textContent).toBe("No transfer suggestions found");
  });
});
