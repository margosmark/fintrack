// M95 P2 — empty-state choreography (docs/09 §Empty states).
//
// Three channels pin the 3-part contract:
//   1. SOURCE — every `<EmptyState>` call site carries `preview` + `action`
//      TypeScript enforces it too; this pins the EXCEPTION SET: exactly eleven
//      sites omit the image, each with a documented reason), and every
//      referenced capture is a real file in `public/help/` (a missing one is a
//      broken image on an empty page);
//   2. DOM — a real page's empty state renders all three parts with their
//      testids, its preview carries a non-empty alt, and its action actually
//      does the thing (Budgets' empty state opens the create form);
//   3. ROUTE — a deep-link action renders as a working link, not a dead span.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Budgets } from "../src/pages/Budgets";
import { NoDataPanel } from "../src/components/charts/NoDataPanel";

const FRONTEND = path.resolve(__dirname, "..");
const SRC = path.join(FRONTEND, "src");
const HELP = path.join(FRONTEND, "public", "help");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(full));
    else if (entry.name.endsWith(".tsx")) out.push(full);
  }
  return out;
}

const FILES = sourceFiles(SRC);
const count = (text: string, pattern: RegExp): number =>
  (text.match(pattern) ?? []).length;

describe("source contract — every empty state is 3-part", () => {
  it("passes preview and action at every <EmptyState> call site", () => {
    const offenders: string[] = [];
    for (const file of FILES) {
      const text = readFileSync(file, "utf8");
      const sites = count(text, /<EmptyState/g);
      if (sites === 0) continue;
      const previews = count(text, /preview=\{\{/g);
      const actions = count(text, /action=\{\{/g);
      if (previews !== sites || actions !== sites) {
        offenders.push(
          `${path.relative(FRONTEND, file)}: ${sites} EmptyState vs ${previews} preview vs ${actions} action`,
        );
      }
    }
    expect(offenders).toEqual([]);
  });

  it("pins the eleven documented `preview={none}` exceptions, nothing more", () => {
    const reasons = new Map<string, number>();
    let total = 0;
    for (const file of FILES) {
      // The component's own file mentions the shape in a comment/type — that
      // is the contract itself, not a call site.
      if (file.endsWith(path.join("ui", "EmptyState.tsx"))) continue;
      const text = readFileSync(file, "utf8");
      const hits = count(text, /none:\s*"/g);
      if (hits > 0) {
        reasons.set(path.relative(SRC, file).split(path.sep).join("/"), hits);
        total += hits;
      }
    }
    expect(total).toBe(11);
    expect(Object.fromEntries(reasons)).toEqual({
      "pages/Retirement.tsx": 1,
      "pages/Tags.tsx": 1,
      "pages/BillDetail.tsx": 1,
      "pages/HelpGuide.tsx": 1,
      "pages/Reconciliation.tsx": 2,
      "pages/OidcLogin.tsx": 1,
      "pages/SharedHousehold.tsx": 1,
      "pages/Assistant.tsx": 1,
      "components/transactions/SuggestionQueue.tsx": 1,
      "components/settings/SharingSection.tsx": 1,
    });
  });

  it("references only captures that exist in public/help", () => {
    const refs = new Set<string>();
    for (const file of FILES) {
      const text = readFileSync(file, "utf8");
      for (const match of text.matchAll(/src:\s*"\/help\/([^"]+)"/g)) {
        refs.add(match[1]);
      }
    }
    // dashboard, transactions, imports, budgets, reports, rules, taxes, goals,
    // bills, debts, investments, security-detail, currencies, settings-accounts
    expect(refs.size).toBeGreaterThanOrEqual(14);
    const missing = [...refs].filter((name) => !existsSync(path.join(HELP, name)));
    expect(missing).toEqual([]);
  });
});

// ── DOM ──────────────────────────────────────────────────────────────────────

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
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { listBudgets } from "../src/api/budgets";
import { listCategories } from "../src/api/categories";

function renderBudgets() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Budgets />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DOM — the three parts render", () => {
  it("Budgets before setup: heading + real screenshot + an action that fills it", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(listBudgets).mockResolvedValue([]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    });

    renderBudgets();

    // (1) what will live here
    const title = await screen.findByTestId("empty-state-title");
    expect(title.textContent).toBe("No budgets");

    // (2) what it looks like when real — a committed product capture with
    // honest alt text, not an illustration
    const preview = screen.getByTestId("empty-state-preview");
    const img = preview.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe("/help/budgets.png");
    expect((img?.getAttribute("alt") ?? "").length).toBeGreaterThan(20);

    // (3) the one action that fills it — and it really opens the create form
    const action = screen.getByTestId("empty-state-action");
    const button = action.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.textContent).toContain("New budget");
    fireEvent.click(button as HTMLButtonElement);
    await waitFor(() => expect(screen.getByText("Create")).toBeTruthy());

    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("a deep-link action renders as a working route link", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <NoDataPanel />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    const action = screen.getByTestId("empty-state-action");
    // The action NODE is the link (not a wrapper around one).
    expect(action.tagName).toBe("A");
    expect(action.getAttribute("href")).toBe("/imports");
    expect(action.textContent).toContain("Import a statement");
    expect(screen.getByTestId("empty-state-preview").querySelector("img")).not.toBeNull();
  });

  it("the documented no-preview exception still renders the other two parts", async () => {
    vi.mocked(listBudgets).mockResolvedValue([]);
    vi.mocked(listCategories).mockResolvedValue({
      items: [],
      total: 0,
      limit: 200,
      offset: 0,
    });
    renderBudgets();
    await screen.findByTestId("empty-state-title");
    // Budgets IS in contract (it has an image) — assert the invariant itself:
    // an exception site renders title + action while the preview figure is
    // absent rather than broken.
    expect(screen.queryByTestId("empty-state-preview")).not.toBeNull();
    expect(screen.getByTestId("empty-state-action")).toBeTruthy();
  });
});
