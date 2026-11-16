import { createElement } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TransactionEditor } from "../src/components/transactions/TransactionEditor";
// M108 P4: the editor announces attach/view/remove through `useToast`, which
// requires the provider — the same one `src/App.tsx` puts around every page.
import { ToastProvider } from "../src/components/ui/Toast";
import type { Account } from "../src/api/accounts";
import type { Category } from "../src/api/categories";
import type { Transaction } from "../src/api/transactions";

vi.mock("../src/api/transactions", async (importOriginal) => {
  const mod =
    await importOriginal<typeof import("../src/api/transactions")>();
  return {
    ...mod,
    getTransactionTags: vi.fn().mockResolvedValue({ items: [] }),
    setTransactionTags: vi.fn().mockImplementation((_id: number, ids: number[]) =>
      Promise.resolve({
        items: ids.map((tagId) => ({ id: tagId, user_id: 1, name: `tag-${tagId}` })),
      }),
    ),
    removeTransactionTag: vi.fn().mockResolvedValue(undefined),
  };
});
vi.mock("../src/api/tags", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/api/tags")>();
  return {
    ...mod,
    listTags: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 }),
    createTag: vi.fn().mockImplementation(({ name }: { name: string }) =>
      Promise.resolve({ id: 9, user_id: 1, name }),
    ),
  };
});

import { getTransactionTags, setTransactionTags } from "../src/api/transactions";
import { createTag } from "../src/api/tags";

const account: Account = {
  id: 7,
  institution_id: null,
  name: "Chequing",
  type: "chequing",
  currency: "CAD",
  taxable: false,
  llm_autopilot: false,
  cash_balance: "0.00",
  investment_value: "0.00",
  total_value: "0.00",
  starting_balance: "0.00",
};
const category: Category = {
  id: 3,
  user_id: 1,
  name: "Groceries",
  parent_id: null,
  is_system: false,
};

function renderEditor(onSave = vi.fn()) {
  render(
    createElement(
      ToastProvider,
      null,
      createElement(TransactionEditor, {
        target: null,
        accounts: [account],
        categories: [category],
        saving: false,
        onSave,
        onClose: vi.fn(),
      }),
    ),
  );

  return onSave;
}
afterEach(() => cleanup());

describe("TransactionEditor", () => {
  it("moves focus into the dialog when the editor opens (docs/09 keyboard contract)", async () => {
    renderEditor();
    // Regression (M58b): `ModalPortal` attached a commit after the editor
    // mounted, so `dialogRef.current` was null when `useDialogFocus` ran its
    // focus effect and focus stayed on the page behind the dialog (WCAG 2.4.3).
    await waitFor(() => {
      const active = document.activeElement as HTMLElement | null;
      expect(active?.getAttribute("role")).toBe("dialog");
      expect(active?.getAttribute("aria-modal")).toBe("true");
      expect(active?.getAttribute("tabindex")).toBe("-1");
    });
  });

  it("blocks submission when required ledger fields are missing", () => {
    const onSave = renderEditor();
    fireEvent.submit(screen.getByRole("dialog"));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("required");
  });

  it("maps create fields to the API payload", () => {
    const onSave = renderEditor();
    fireEvent.change(screen.getByLabelText("Transaction date *"), {
      target: { value: "2026-08-20" },
    });
    fireEvent.change(screen.getByLabelText("Raw description *"), {
      target: { value: "Coffee" },
    });
    fireEvent.change(screen.getByLabelText("Amount *"), { target: { value: "-4.25" } });
    fireEvent.change(screen.getByLabelText("Payee"), { target: { value: "Cafe" } });
    // Combo-box: focus → type search → click matching option (M176: the option
    // and Create rows activate on `click`, so keyboard Enter/Space and a tap
    // work too — `mousedown` alone was mouse-exclusive).
    fireEvent.focus(screen.getByLabelText("Category"));
    fireEvent.change(screen.getByLabelText("Category"), { target: { value: "Groceries" } });
    fireEvent.click(screen.getByText("Groceries"));
    fireEvent.submit(screen.getByRole("dialog"));
    expect(onSave).toHaveBeenCalledWith({
      account_id: 7,
      date: "2026-08-20",
      posting_date: null,
      raw_description: "Coffee",
      normalized_payee: "Cafe",
      amount_native: "-4.25",
      currency: "CAD",
      category_id: 3,
    });
  });

  it("attaches and creates tags through the unified picker", async () => {
    const promptSpy = vi.spyOn(window, "prompt");
    const target: Transaction = {
      id: 42,
      account_id: 7,
      date: "2026-08-20",
      posting_date: null,
      raw_description: "Coffee",
      normalized_payee: "CAFE LUNA",
      amount_native: "-4.25",
      currency: "CAD",
      cad_amount: "-4.25",
      category_id: 3,
      transfer_pair_id: null,
      pair_account_name: null,
      has_receipt: false,
      tags: [],
    };
    render(
      createElement(
        ToastProvider,
        null,
        createElement(TransactionEditor, {
          target,
          accounts: [account],
          categories: [category],
          saving: false,
          onSave: vi.fn(),
          onClose: vi.fn(),
        }),
      ),
    );

    // One control for both: type a new name, pick the Create row.
    fireEvent.focus(screen.getByLabelText("Add or create tag"));
    fireEvent.change(screen.getByLabelText("Add or create tag"), {
      target: { value: "receipt" },
    });
    fireEvent.mouseDown(await screen.findByText('Create "receipt"'));

    expect(await screen.findByText("receipt")).toBeTruthy();
    expect(promptSpy).not.toHaveBeenCalled();
    expect(createTag).toHaveBeenCalledWith({ name: "receipt" });
    expect(setTransactionTags).toHaveBeenCalledWith(42, [9]);
    expect(getTransactionTags).toHaveBeenCalled();
    // Picker resets after attach so it can't offer to re-create the name.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Add or create tag") as HTMLInputElement).value,
      ).toBe("");
    });
    promptSpy.mockRestore();
  });
});
