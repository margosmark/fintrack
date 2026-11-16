import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Budgets } from "../src/pages/Budgets";
import type { BudgetWithSpent } from "../src/api/budgets";
import type { Category } from "../src/api/categories";

vi.mock("../src/api/budgets", () => ({
  listBudgets: vi.fn(),
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
  // M137 P0-2: the M96 recurring label now reads its own tiny route instead
  // of downloading the ledger, so the Budgets page renders this too.
  getRecurringSummary: vi.fn().mockResolvedValue({ count: 0, monthly: 0 }),
}));
vi.mock("../src/api/categories", () => ({
  listCategories: vi.fn(),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { listBudgets, updateBudget } from "../src/api/budgets";
import { listCategories } from "../src/api/categories";

const category: Category = {
  id: 1,
  user_id: 1,
  name: "groceries",
  parent_id: null,
  is_system: true,
};
const budget: BudgetWithSpent = {
  id: 9,
  user_id: 1,
  category_id: 1,
  amount: "600.00",
  period: "monthly",
  carry: "off",
  category_name: "groceries",
  spent: "62.12",
  carried_in: null,
  remaining: "537.88",
  pct_used: 10.35,
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  // Returned so a test can read `container.textContent` (M110 carry layers).
  return render(
    createElement(QueryClientProvider, {
      client,
      children: createElement(MemoryRouter, null, createElement(Budgets)),
    }),
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("Budgets edit form", () => {
  it("populates category, amount, and period when edit is clicked", async () => {
    vi.mocked(listBudgets).mockResolvedValue([budget]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    renderPage();

    await screen.findByText("groceries");
    const card = screen.getByText("groceries").closest("div")?.parentElement
      ?.parentElement as HTMLElement;
    // The card now opens with the M97 help-hint trigger, so "the first button"
    // is no longer the edit control — select it by ITS label.
    const editBtn = Array.from(card.querySelectorAll("button")).find((b) =>
      (b.getAttribute("aria-label") ?? "").startsWith("Edit budget"),
    ) as HTMLButtonElement;
    expect(editBtn).toBeDefined();
    fireEvent.click(editBtn);

    await waitFor(() => {
      expect(screen.getByText("Update")).toBeTruthy();
    });
    const selects = document.querySelectorAll("select");
    expect(selects[0].value).toBe("1");
    expect(selects[0].disabled).toBe(true);
    const amountInput = document.querySelector(
      'input[placeholder="Amount"]',
    ) as HTMLInputElement;
    expect(amountInput.value).toBe("600.00");
    expect(selects[1].value).toBe("monthly");
    expect(screen.getByText("Update").closest("button")?.disabled).toBe(false);
  });
});

describe("Over-budget labels report the true ratio (M65 / m61 #3)", () => {
  it("prints 202% used at 201.94% actual — only the bar fill clamps", async () => {
    // 302.91 / 150.00 = 201.94% — self-consistent with the server's
    // pct_used (it used to be capped at 100 before it ever reached here).
    vi.mocked(listBudgets).mockResolvedValue([
      {
        ...budget,
        amount: "150.00",
        spent: "302.91",
        remaining: "-152.91",
        pct_used: 201.94,
      },
    ]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    renderPage();

    // The label is the true ratio, rounded to a whole per cent (docs/09)…
    expect(await screen.findByText("202% used")).toBeTruthy();
    expect(screen.queryByText("100% used")).toBeNull();
    expect(screen.queryByText("201.94% used")).toBeNull();
    // …and only the progress bar clamps its fill at 100% (never 201.94%).
    expect(document.querySelector('div[style*="width: 100%"]')).not.toBeNull();
    expect(document.querySelector('div[style*="width: 201"]')).toBeNull();
  });
});


describe("Budget carry (M110, docs/09 §Budget carry)", () => {
  it("opts a row in with ONE click — no wizard, aria-pressed tells the state", async () => {
    vi.mocked(listBudgets).mockResolvedValue([budget]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(updateBudget).mockResolvedValue({ ...budget, carry: "forward" });
    renderPage();

    const toggle = await screen.findByRole("button", {
      name: "Carry leftover — groceries",
    }) as HTMLButtonElement;
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(updateBudget).toHaveBeenCalledWith(9, { carry: "forward" });
    });
  });

  it("shows carried-in as its OWN layer, and spends against the pool", async () => {
    vi.mocked(listBudgets).mockResolvedValue([
      {
        ...budget,
        carry: "forward",
        spent: "100.00",
        carried_in: "60.00",
        remaining: "560.00",
        pct_used: 15.151515151515152,
      },
    ]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    const { container } = renderPage();

    await screen.findByText("groceries");
    // assigned + carried_in = 660 spendable — the denominator, shown as such,
    // while the carried figure keeps its own line (never merged into `amount`).
    expect(container.textContent).toContain("/ $660");
    expect(container.textContent).toContain("spendable");
    expect(container.textContent).toContain("+$60 carried in — extra room this period");
    // 100 / 660 = 15% of the pool — not 100/600.
    expect(await screen.findByText("15% used")).toBeTruthy();
  });

  it("tones a carried DEFICIT as a debt, never silently absorbed", async () => {
    vi.mocked(listBudgets).mockResolvedValue([
      {
        ...budget,
        carry: "forward",
        spent: "0.00",
        carried_in: "-100.00",
        remaining: "500.00",
        pct_used: 0,
      },
    ]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    const { container } = renderPage();

    await screen.findByText("groceries");
    expect(container.textContent).toContain("−$100 carried in — a debt to pay down first");
    expect(container.textContent).toContain("/ $500");
    // The debt line carries the danger tone (docs/09).
    const debtLine = Array.from(container.querySelectorAll("p")).find((el) =>
      (el.textContent ?? "").includes("carried in"),
    );
    expect(debtLine?.className).toContain("text-danger");
    expect(screen.queryByText("over budget — the carried debt is bigger than this period's limit")).toBeNull();
  });

  it("says `over budget` instead of inventing a percent when the pool is gone", async () => {
    vi.mocked(listBudgets).mockResolvedValue([
      {
        ...budget,
        carry: "forward",
        spent: "0.00",
        carried_in: "-150.00",
        remaining: "-50.00",
        pct_used: null,
      },
    ]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    renderPage();

    expect(
      await screen.findByText(
        "over budget — the carried debt is bigger than this period's limit",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("% used")).toBeNull();
    expect(screen.queryByText("0% used")).toBeNull();
    // The bar stays full-width danger rather than a fabricated 0%.
    expect(document.querySelector('div[style*="width: 100%"]')).not.toBeNull();
  });

  it("carry off renders the pre-M110 row: no carry line, math on `amount`", async () => {
    vi.mocked(listBudgets).mockResolvedValue([budget]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    const { container } = renderPage();

    await screen.findByText("groceries");
    expect(container.textContent).not.toContain("carried in");
    expect(container.textContent).toContain("/ $600");
    expect(container.textContent).not.toContain("spendable");
    expect(await screen.findByText("10% used")).toBeTruthy();
  });
});
