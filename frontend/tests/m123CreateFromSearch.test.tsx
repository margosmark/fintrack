/**
 * M123 — "Create from search" (docs/09 §Create from search).
 *
 * What the card asks to see before merging:
 *   * one menu with four targets, and the ARIA that makes it a menu;
 *   * a rule/bill/budget deep link carrying ONLY what that editor can express,
 *     with a provenance line that says out loud what it did not carry;
 *   * a saved view that persists as `kind="transactions"` (the four search
 *     params verbatim), restores, renames and deletes;
 *   * the ⌘K palette offering those views plus the contextual action.
 */
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CreateFromSearch } from "../src/components/transactions/CreateFromSearch";
import { paletteEntries } from "../src/components/layout/CommandPalette";
import { requestCreateFromSearch, type SearchCriteria } from "../src/lib/createFromSearch";
import { Rules } from "../src/pages/Rules";
import { Bills } from "../src/pages/Bills";
import { Budgets } from "../src/pages/Budgets";
import type { SavedReportView } from "../src/types";

vi.mock("../src/api/rules", () => ({
  listRules: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  deleteRule: vi.fn(),
  previewRule: vi.fn(),
  applyRule: vi.fn(),
}));
vi.mock("../src/api/categories", () => ({ listCategories: vi.fn() }));
vi.mock("../src/api/tags", () => ({ listTags: vi.fn(), createTag: vi.fn() }));
vi.mock("../src/api/budgets", () => ({
  listBudgets: vi.fn(),
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
  deleteBudget: vi.fn(),
  // M137 P0-2: the M96 recurring label now reads its own tiny route instead
  // of downloading the ledger, so the Budgets page renders this too.
  getRecurringSummary: vi.fn().mockResolvedValue({ count: 0, monthly: 0 }),
}));
vi.mock("../src/api/bills", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/bills")>();
  return {
    ...actual,
    listBills: vi.fn(),
    createBill: vi.fn(),
    updateBill: vi.fn(),
    deleteBill: vi.fn(),
    markPaid: vi.fn(),
    updateBillRule: vi.fn(),
  };
});
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { listBills } from "../src/api/bills";
import { listBudgets } from "../src/api/budgets";
import { listCategories } from "../src/api/categories";
import { listRules } from "../src/api/rules";
import { listTags } from "../src/api/tags";

// --- fixtures ---------------------------------------------------------------

const SOURCES = {
  categories: [
    { id: 5, name: "Groceries" },
    { id: 7, name: "Utilities" },
  ],
  accounts: [{ id: 3, name: "Chequing" }],
  tags: [
    { id: 1, name: "wifi" },
    { id: 2, name: "household" },
  ],
};

const FULL: SearchCriteria = { q: "rogers", category: "5", account: "3", tag: "1,2" };

const VIEW: SavedReportView = {
  id: "v-abcdefgh",
  title: "Rogers and groceries",
  tab: "transactions",
  kind: "transactions",
  state: { q: "rogers", category: "5" },
};

function renderAt(node: ReactNode, path = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </MemoryRouter>,
  );
}

function renderMenu(overrides: Record<string, unknown> = {}) {
  return renderAt(
    <CreateFromSearch
      criteria={FULL}
      sources={SOURCES}
      excludedFilters={[]}
      views={[]}
      onPersist={vi.fn().mockResolvedValue(undefined)}
      onRestore={vi.fn()}
      {...overrides}
    />,
    "/transactions",
  );
}

function openMenu(): HTMLElement {
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  return screen.getByRole("menu", { name: "Create from this search" });
}

function hrefOf(name: RegExp): URLSearchParams {
  const link = screen.getByRole("menuitem", { name }) as HTMLAnchorElement;
  const [, query = ""] = (link.getAttribute("href") ?? "").split("?");
  return new URLSearchParams(query);
}

afterEach(() => {
  vi.clearAllMocks();
});

// --- C: the menu itself -----------------------------------------------------

describe("M123 C — one menu, four targets", () => {
  it("is a menu by ARIA, opens focused on its first target, and closes on Escape", () => {
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Create" });
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    const menu = openMenu();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: /Saved view/ }));

    // The criteria being carried are stated where the choice is made.
    expect(menu.parentElement?.textContent).toContain(
      "This search: “rogers” · Groceries · Chequing · tags wifi, household",
    );

    expect(screen.getAllByRole("menuitem")).toHaveLength(4);

    // ArrowDown moves within the menu; Escape leaves it and restores the trigger.
    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: /Rule/ }));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("also opens from the ⌘K palette's contextual action", () => {
    renderMenu();
    const entry = paletteEntries({ pathname: "/transactions" }).find(
      (candidate) => candidate.id === "action-create-from-search",
    );
    expect(entry?.run).toBeTypeOf("function");

    act(() => entry?.run?.());
    expect(screen.getByRole("menu", { name: "Create from this search" })).toBeTruthy();
  });

  it("disables Budget — and says why — until a category filter exists", () => {
    renderMenu({ criteria: { q: "rogers" } });
    openMenu();

    const budget = screen.getByRole("menuitem", { name: /Budget/ });
    expect(budget.getAttribute("aria-disabled")).toBe("true");
    expect(budget.textContent).toContain("Set a category filter first");
    expect(budget.tagName).not.toBe("A");

    const rule = screen.getByRole("menuitem", { name: /Rule/ });
    expect(rule.getAttribute("aria-disabled")).toBeNull();
  });

  it("refuses to budget `uncategorized` either, with the accurate reason", () => {
    renderMenu({ criteria: { category: "uncategorized" } });
    openMenu();
    const budget = screen.getByRole("menuitem", { name: /Budget/ });
    expect(budget.getAttribute("aria-disabled")).toBe("true");
    expect(budget.textContent).toContain("Uncategorized cannot carry a budget");
  });

  it("deep-links each target with the criteria THAT editor can carry", () => {
    renderMenu();
    openMenu();

    // A rule: search term + category. The account and tag ride along for the
    // provenance line even though no rule schema has a column for them.
    const rule = hrefOf(/Rule/);
    expect(rule.get("prefill")).toBe("transactions");
    expect(rule.get("q")).toBe("rogers");
    expect(rule.get("category")).toBe("5");
    expect(rule.get("account")).toBe("3");
    expect(rule.get("tag")).toBe("1,2");

    const bill = hrefOf(/Bill/);
    expect(bill.get("q")).toBe("rogers");
    expect(bill.get("category")).toBe("5");

    // A budget is a per-category amount: category only, but the SAME criteria
    // so its provenance can name what was dropped.
    const budget = hrefOf(/Budget/);
    expect(budget.get("category")).toBe("5");
    expect(budget.get("account")).toBe("3");
    expect(budget.get("q")).toBe("rogers");
  });
});

// --- C1: the saved view -----------------------------------------------------

describe("M123 C1 — a saved view is a bookmark", () => {
  it("persists kind=transactions with the four params verbatim", async () => {
    const onPersist = vi.fn().mockResolvedValue(undefined);
    renderMenu({ onPersist });

    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Saved view/ }));

    const title = screen.getByLabelText("Name this view") as HTMLInputElement;
    // The default is the criteria as the user reads them, not raw ids.
    expect(title.value).toBe("“rogers” · Groceries · Chequing · tags wifi, household");
    // Never silent about the filters it does NOT keep.
    expect(
      screen.getByText(/Keeps the search term, category, account and tag filters\./),
    ).toBeTruthy();
    fireEvent.change(title, { target: { value: "  Rogers grocery run  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onPersist).toHaveBeenCalledTimes(1));
    const next = onPersist.mock.calls[0][0] as SavedReportView[];
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({
      title: "Rogers grocery run", // trimmed
      tab: "transactions",
      kind: "transactions",
      state: FULL,
    });
    expect(next[0].id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    // The panel closes on success — nothing is left open behind the save.
    expect(screen.queryByLabelText("Name this view")).toBeNull();
  });

  it("names the bar filters it does not carry", () => {
    renderMenu({ excludedFilters: ["date range", "review state"] });
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Saved view/ }));
    expect(screen.getByText(/Not saved: date range, review state\./)).toBeTruthy();
  });

  it("lists, restores, renames and deletes as one whole-list edit", async () => {
    const onPersist = vi.fn().mockResolvedValue(undefined);
    const onRestore = vi.fn();
    renderMenu({ views: [VIEW], onPersist, onRestore });

    fireEvent.click(screen.getByRole("button", { name: "Views (1)" }));
    const panel = document.getElementById("create-from-search-panel");
    expect(panel).not.toBeNull();
    const row = within(panel as HTMLElement).getAllByRole("button");
    // restore · rename · delete, in that order.
    expect(row).toHaveLength(3);
    expect(row[0].textContent).toContain("Rogers and groceries");
    expect(row[0].textContent).toContain("“rogers” · Groceries");

    fireEvent.click(row[0]);
    expect(onRestore).toHaveBeenCalledWith(VIEW.state);
    expect(screen.queryByRole("button", { name: "Rename saved view Rogers and groceries" })).toBeNull();

    // Reopen for the list edits — restore did not change what is saved.
    fireEvent.click(screen.getByRole("button", { name: "Views (1)" }));
    fireEvent.click(
      screen.getByRole("button", { name: `Rename saved view ${VIEW.title}` }),
    );
    const input = screen.getByLabelText(`New name for ${VIEW.title}`) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Rogers" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(onPersist).toHaveBeenCalledTimes(1));
    expect((onPersist.mock.calls[0][0] as SavedReportView[])[0].title).toBe("Rogers");

    // The row's label comes from the `views` prop (in production, back from
    // `users/me`), so it still shows the title that was SAVED — what this pins
    // is the write, then the delete that empties the list.
    fireEvent.click(screen.getByRole("button", { name: `Delete saved view ${VIEW.title}` }));
    await waitFor(() => expect(onPersist).toHaveBeenCalledTimes(2));
    expect(onPersist.mock.calls[1][0]).toEqual([]);
  });
});

// --- the palette ------------------------------------------------------------

describe("M123 C1/C2 — the palette", () => {
  it("offers every saved transaction view as its own bookmark URL", () => {
    const entries = paletteEntries({
      savedViews: [VIEW, { ...VIEW, id: "v-report01", tab: "spending", kind: "reports", state: {} }],
    });
    const views = entries.filter((entry) => entry.id.startsWith("view-"));
    expect(views).toHaveLength(1);
    expect(views[0]).toMatchObject({
      id: "view-v-abcdefgh",
      label: "Rogers and groceries",
      group: "View",
      to: "/transactions?q=rogers&category=5",
    });
  });

  it("offers the contextual action only where a search exists", () => {
    expect(
      paletteEntries({ pathname: "/transactions" }).some(
        (entry) => entry.id === "action-create-from-search",
      ),
    ).toBe(true);
    expect(
      paletteEntries({ pathname: "/dashboard" }).some(
        (entry) => entry.id === "action-create-from-search",
      ),
    ).toBe(false);
    expect(
      paletteEntries().some((entry) => entry.id === "action-create-from-search"),
    ).toBe(false);
  });

  it("leaves the empty-query default list untouched", () => {
    const bare = paletteEntries().map((entry) => entry.id);
    const contextual = paletteEntries({
      pathname: "/transactions",
      savedViews: [VIEW],
    }).map((entry) => entry.id);
    expect(contextual.slice(0, 8)).toEqual(bare.slice(0, 8));
    expect(bare.slice(0, 8)).toEqual([
      "page-transactions",
      "page-budgets",
      "page-reports",
      "page-goals",
      "page-settings",
      "page-help",
      "page-accounts",
      "action-import",
    ]);
    // Still exactly ONE run-only entry when nothing is contextual.
    expect(paletteEntries().filter((entry) => entry.to === undefined)).toHaveLength(1);
  });
});

// --- the three targets read the deep link -----------------------------------

describe("M123 C2/C3/C4 — the targets read `?prefill=` once", () => {
  beforeEach(() => {
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [
        { id: 5, user_id: 1, name: "Groceries", parent_id: null, is_system: false },
        { id: 7, user_id: 1, name: "Utilities", parent_id: null, is_system: false },
      ],
      total: 2,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listBills).mockResolvedValue([]);
    vi.mocked(listBudgets).mockResolvedValue([]);
  });

  it("C2 — prefills a `contains` rule and states what a rule cannot carry", async () => {
    renderAt(<Rules />, "/rules?prefill=transactions&q=rogers&category=5&account=3&tag=1,2");

    const provenance = await screen.findByTestId("prefill-provenance");
    const pattern = document.getElementById("rule-pattern") as HTMLInputElement;
    expect(pattern.value).toBe("rogers");
    // match_mode=contains is the default AND what the deep link claims.
    expect((screen.getByLabelText("Match type") as HTMLSelectElement).value).toBe("contains");
    expect(provenance.textContent).toContain("Created from search: “rogers” · Groceries");
    expect(provenance.textContent).toContain("account, tags not carried into a rule");
  });

  it("C3 — prefills the bill's match term, leaves amount blank, scopes by category", async () => {
    renderAt(<Bills />, "/bills?prefill=transactions&q=rogers&category=5&account=3");

    const provenance = await screen.findByTestId("prefill-provenance");
    // `amount→None`: the search has no amount, and none is invented.
    expect((screen.getByPlaceholderText("Amount (optional)") as HTMLInputElement).value).toBe("");
    // match_mode=contains + match_term=<q>
    expect(screen.getByLabelText("Remove term — rogers")).toBeTruthy();
    // The select can only hold "5" once the category options have loaded.
    await waitFor(() =>
      expect((screen.getByLabelText("Match category") as HTMLSelectElement).value).toBe("5"),
    );
    expect(provenance.textContent).toContain("Created from search: “rogers” · Groceries");
    expect(provenance.textContent).toContain("account not carried into a bill");
    // Opened as a DRAFT — nothing was created by landing here.
    expect(screen.getByRole("button", { name: "Create" })).toBeTruthy();
    expect(screen.getByPlaceholderText("Bill name")).toBeTruthy();
  });

  it("C4 — opens budget-create with the category pre-selected and monthly period", async () => {
    renderAt(<Budgets />, "/budgets?prefill=transactions&category=5&q=rogers&account=3&tag=1,2");

    await screen.findByLabelText("Budget category");
    await waitFor(() =>
      expect((screen.getByLabelText("Budget category") as HTMLSelectElement).value).toBe("5"),
    );
    // "the current month as period" — a monthly budget IS the current month.
    expect((screen.getByLabelText("Budget period") as HTMLSelectElement).value).toBe("monthly");
    // Opened, not created: the amount is still the user's to type.
    expect((screen.getByPlaceholderText("Amount") as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Create" }).hasAttribute("disabled")).toBe(true);
    // The search's own criteria explain themselves rather than vanish.
    expect((await screen.findByTestId("prefill-provenance")).textContent).toContain(
      "search term, account, tags not carried into a budget",
    );
  });

  it("drops the deep link on Cancel so a reload cannot reopen a stale draft", async () => {
    renderAt(<Budgets />, "/budgets?prefill=transactions&category=5");
    await screen.findByLabelText("Budget category");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Budget category")).toBeNull();
  });

  it("is silent when the link is not ours", async () => {
    renderAt(<Rules />, "/rules?category=5");
    await waitFor(() => expect(document.getElementById("rule-pattern")).not.toBeNull());
    expect(screen.queryByTestId("prefill-provenance")).toBeNull();
  });
});

// --- the handoff event ------------------------------------------------------

describe("M123 — the ⌘K handoff", () => {
  it("is a no-op with no listener, and never throws", () => {
    expect(() => requestCreateFromSearch()).not.toThrow();
  });
});
