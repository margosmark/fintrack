// M76 / docs/38 — the YNAB migration card must preview before it writes:
// picking a file calls the read-only preview route and surfaces every row it
// cannot import with a reason; pressing Import is what calls commit.
import { createElement } from "react";
import type { ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../src/components/ui/Toast";
import { YnabMigrationCard } from "../src/components/imports/YnabMigrationCard";

const h = vi.hoisted(() => ({
  previewYnab: vi.fn(),
  commitYnab: vi.fn(),
  listAccounts: vi.fn(),
}));

vi.mock("../src/api/ynab", () => ({
  previewYnab: h.previewYnab,
  commitYnab: h.commitYnab,
}));

vi.mock("../src/api/accounts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/accounts")>()),
  listAccounts: h.listAccounts,
}));

function planFixture() {
  const counts = {
    accounts_create: 1,
    accounts_reuse: 1,
    accounts_unmapped: 1,
    groups_create: 1,
    groups_reuse: 0,
    categories_create: 2,
    categories_reuse: 1,
    tags_create: 1,
    tags_reuse: 0,
    budgets_create: 1,
    budgets_reuse: 0,
    goals_create: 0,
    goals_reuse: 0,
    targets_unsupported: 1,
  };
  return {
    source_format: "json",
    budget_name: "Household 2026",
    currency: "CAD",
    date_format: null,
    accounts: [
      { ynab_id: "a1", name: "Chequing", ynab_type: "checking", mapped_type: "chequing", action: "create", existing_id: null, currency: "CAD", closed: false, on_budget: true, type_assumed: false, reason: null },
      { ynab_id: "a2", name: "MasterCard", ynab_type: "creditCard", mapped_type: "credit_card", action: "reuse", existing_id: 4, currency: "CAD", closed: false, on_budget: true, type_assumed: false, reason: null },
      { ynab_id: "a3", name: "Weird Wallet", ynab_type: "otherWeird", mapped_type: null, action: "unmapped", existing_id: null, currency: "CAD", closed: false, on_budget: false, type_assumed: false, reason: "YNAB account type `otherWeird` has no fin_tracker equivalent" },
    ],
    category_groups: [],
    categories: [],
    targets: [],
    transactions: {
      total: 5,
      importable: 2,
      duplicates: 1,
      duplicates_pending: 0,
      transfer_pairs: 0,
      rows: [
        {
          index: 1,
          date: "2026-01-01",
          amount: "-1500.00",
          currency: "CAD",
          account: "Chequing",
          payee: "ACME Landlord — January rent",
          description: "ACME Landlord — January rent",
          category: "Rent",
          group: "Monthly Bills",
          tag: "Monthly Bills",
          cleared: "reconciled",
          transfer: false,
          cad_amount: "-1500.00",
          duplicate: false,
        },
      ],
      rows_truncated: false,
      unmappable: [
        { code: "account_unmappable", reason: "Account `Weird Wallet` cannot be imported: YNAB account type `otherWeird` has no fin_tracker equivalent", index: 4, detail: null },
        { code: "invalid_date", reason: "Date `2026-13-45` is not YYYY-MM-DD", index: 5, detail: null },
      ],
      warnings: [
        { code: "split_detail_dropped", reason: "Row is a split with 2 sub-row(s); the total imports but the per-part categories do not", index: 2, detail: null },
      ],
      warnings_truncated: false,
    },
    notes: [{ code: "deleted_rows_skipped", reason: "1 transaction(s) marked deleted in YNAB were excluded", index: null, detail: null }],
    counts,
  };
}

function renderCard(): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree: ReactElement = createElement(
    QueryClientProvider,
    { client },
    createElement(
      ToastProvider,
      null,
      createElement(MemoryRouter, null, createElement(YnabMigrationCard)),
    ),
  );
  return render(tree);
}

function pickFile(name = "budget.json"): void {
  const input = screen.getByTestId("ynab-file") as HTMLInputElement;
  const file = new File(["{}"], name, { type: "application/json" });
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  h.previewYnab.mockReset();
  h.commitYnab.mockReset();
  h.listAccounts.mockReset();
  h.listAccounts.mockResolvedValue({
    items: [{ id: 7, name: "Joint", type: "chequing", currency: "CAD" }],
    total: 1,
    limit: 200,
    offset: 0,
  });
});

describe("YNAB migration card", () => {
  it("previews the plan without writing, and lists every refused row with a reason", async () => {
    h.previewYnab.mockResolvedValue(planFixture());
    renderCard();

    expect(screen.getByText(/Import a YNAB budget/i)).toBeTruthy();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: /Preview/i }));

    await waitFor(() => expect(h.previewYnab).toHaveBeenCalledTimes(1));
    // Preview is read-only: the commit route is untouched until Import is pressed.
    expect(h.commitYnab).not.toHaveBeenCalled();

    expect(await screen.findByText("2 to import")).toBeTruthy();
    expect(screen.getByText("1 already here")).toBeTruthy();
    expect(screen.getByText("2 rows cannot be imported")).toBeTruthy();
    expect(screen.getByTestId("ynab-unmapped-accounts").textContent).toContain("Weird Wallet");

    const refused = await screen.findByTestId("ynab-danger-list");
    expect(refused.textContent).toContain("Row 4:");
    expect(refused.textContent).toContain("no fin_tracker equivalent");
    expect(refused.textContent).toContain("Row 5:");
    expect(refused.textContent).toContain("not YYYY-MM-DD");

    expect(screen.getByTestId("ynab-notes").textContent).toContain("deleted in YNAB");
  });

  it("shows caveats on demand and imports only after the plan is visible", async () => {
    h.previewYnab.mockResolvedValue(planFixture());
    h.commitYnab.mockResolvedValue({
      plan: planFixture(),
      batches: [
        { batch_id: 11, account_id: 3, account_name: "Chequing", imported: 2, duplicates_skipped: 0, unresolved: 0 },
      ],
      net_new: 2,
      duplicates_skipped: 0,
    });
    renderCard();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: /Preview/i }));

    const importButton = await screen.findByTestId("ynab-import");
    expect(importButton.textContent).toContain("Import 2 transactions");
    expect(h.commitYnab).not.toHaveBeenCalled();

    const warningsToggle = screen.getByRole("button", { name: /caveat/i });
    expect(screen.queryByTestId("ynab-warn-list")).toBeNull();
    fireEvent.click(warningsToggle);
    expect(screen.getByTestId("ynab-warn-list").textContent).toContain("split");

    fireEvent.click(importButton);
    await waitFor(() => expect(h.commitYnab).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("ynab-done")).toBeTruthy();
    expect(screen.getByTestId("ynab-done").textContent).toContain("2 new transactions");
  });

  it("says so when the whole file is already imported", async () => {
    const fixture = planFixture();
    fixture.transactions.importable = 0;
    h.previewYnab.mockResolvedValue(fixture);
    renderCard();
    pickFile();
    fireEvent.click(screen.getByRole("button", { name: /Preview/i }));

    expect(await screen.findByText("Everything in this file is already here.")).toBeTruthy();
    expect((screen.getByTestId("ynab-import") as HTMLButtonElement).disabled).toBe(true);
  });
});
