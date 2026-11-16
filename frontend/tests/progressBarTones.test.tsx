/**
 * M69 (G4) — progress fills are semantically colour-coded, never accent:
 * docs/09 line 22 pins green (`--color-gain`) <80% / amber 80–100% / red >100%,
 * and line 125 keeps `--color-gain` for gains only. The demo tenant ships no
 * budget under 80%, so a screenshot cannot prove the green band — this pins it,
 * plus the "accent appears nowhere on a progress bar" half of the contract.
 */
import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BudgetCards } from "../src/components/reports/BudgetCards";
import { Budgets } from "../src/pages/Budgets";
import { Goals } from "../src/pages/Goals";
import type { BudgetWithSpent } from "../src/api/budgets";
import type { Category } from "../src/api/categories";
import type { GoalWithProgress } from "../src/api/goals";

vi.mock("../src/api/budgets", () => ({
  listBudgets: vi.fn(),
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
  // M137 P0-2: the M96 recurring label now reads its own tiny route instead
  // of downloading the ledger, so the Budgets page renders this too.
  getRecurringSummary: vi.fn().mockResolvedValue({ count: 0, monthly: 0 }),
}));
vi.mock("../src/api/categories", () => ({ listCategories: vi.fn() }));
vi.mock("../src/api/goals", () => ({
  listGoals: vi.fn(),
  createGoal: vi.fn(),
  updateGoal: vi.fn(),
  deleteGoal: vi.fn(),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { listBudgets } from "../src/api/budgets";
import { listCategories } from "../src/api/categories";
import { listGoals } from "../src/api/goals";

const BANDS = ["bg-gain", "bg-warn", "bg-danger", "bg-accent"];

function budget(id: number, pct_used: number, category_name: string): BudgetWithSpent {
  return {
    id,
    user_id: 1,
    category_id: id,
    amount: "250.00",
    period: "monthly",
    category_name,
    spent: ((250 * pct_used) / 100).toFixed(2),
    remaining: "0.00",
    pct_used,
  };
}

const category: Category = {
  id: 1,
  user_id: 1,
  name: "groceries",
  parent_id: null,
  is_system: true,
};

const goal: GoalWithProgress = {
  id: 1,
  user_id: 1,
  name: "Emergency fund",
  target_amount: "1000.00",
  target_date: null,
  account_id: null,
  current_amount_override: null,
  current_amount: "450.00",
  progress_pct: 45,
  monthly_needed: null,
  status: "on_track",
};

function renderWith(element: Parameters<typeof createElement>[0]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  // The Budgets page reads the M123 `?prefill=` deep link, so it renders
  // under a Router exactly as it does in the app.
  return render(
    createElement(
      QueryClientProvider,
      { client },
      createElement(MemoryRouter, null, element),
    ),
  );
}

/** Colour band of every progress fill, in DOM order (the h-2 track's child). */
function progressFills(container: HTMLElement): string[] {
  const fills = container.querySelectorAll<HTMLElement>(
    ".bg-surface-raised.overflow-hidden > div",
  );
  return Array.from(fills, (el) =>
    BANDS.find((cls) => el.classList.contains(cls)) ?? "(uncoloured)",
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("progress bar colour bands (docs/09 line 22)", () => {
  it("BudgetCards: <80% green, 80–100% amber, >100% red", async () => {
    vi.mocked(listBudgets).mockResolvedValue([
      budget(1, 10.35, "groceries"),
      budget(2, 93.7, "dining"),
      budget(3, 201.94, "entertainment"),
    ]);
    const { container } = renderWith(createElement(BudgetCards));

    await screen.findByText("groceries");
    expect(progressFills(container)).toEqual(["bg-gain", "bg-warn", "bg-danger"]);
    expect(progressFills(container)).not.toContain("bg-accent");
    // Regression (AGENTS §6): `<motion.div {...fadeUp}>` put the global `hidden`
    // attribute on every card WRAPPER, so the whole Budget progress section
    // rendered display:none — invisible in both themes while the classes and
    // the text still looked "right" to a querySelector.
    expect(container.querySelector("[hidden]")).toBeNull();
    expect(container.querySelectorAll(".grid > div").length).toBe(3);
  });

  it("Budgets page: same three bands, no accent on a progress fill", async () => {
    vi.mocked(listBudgets).mockResolvedValue([
      budget(1, 10.35, "groceries"),
      budget(2, 93.7, "dining"),
      budget(3, 201.94, "entertainment"),
    ]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [category],
      total: 1,
      limit: 200,
      offset: 0,
    });
    const { container } = renderWith(createElement(Budgets));

    await screen.findByText("groceries");
    expect(progressFills(container)).toEqual(["bg-gain", "bg-warn", "bg-danger"]);
    expect(progressFills(container)).not.toContain("bg-accent");
  });

  it("Goals: the progress fill is the semantic green", async () => {
    vi.mocked(listGoals).mockResolvedValue([goal]);
    const { container } = renderWith(createElement(Goals));

    await screen.findByText("Emergency fund");
    expect(progressFills(container)).toEqual(["bg-gain"]);
    expect(progressFills(container)).not.toContain("bg-accent");
  });
});
