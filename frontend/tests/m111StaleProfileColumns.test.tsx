// M111 — the import Column-Mapping Wizard must never ship a column key that
// belongs to the OTHER amount mode, and never an empty-string mapping.
//
// Named after the truncate Mark hit live on prod: the draft record is
// persistent, so a blank `credit: ""` (left by the suggester or by a switch
// through Debit & Credit) rode along into the signed payload and the backend
// answered `422 invalid_profile` with pydantic's own record —
// "1 validation error for CsvProfile … columns.credit must be a non-empty
// header name [input_value={'columns': {'date': 'Tra…".
//
// Two independent assertions per flow: the KEYS the payload carries (mode-
// explicit) and the VALUES (no "" ever reaches the wire).
import { fireEvent, render, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ColumnMappingWizard,
  draftFromSuggestion,
  draftToProfile,
} from "../src/pages/Imports";
import type { SuggestProfileResponse } from "../src/api/imports";

afterEach(() => cleanup());

/** A suggestion in signed mode that still echoes split keys (the seed case). */
const SIGNED_WITH_STALE_SPLIT: SuggestProfileResponse = {
  columns: {
    date: "Transaction Date",
    raw_description: "Description",
    amount_native: "Amount",
    debit: "Withdrawal", // stale: split-mode value the UI no longer shows
    credit: "", // stale AND blank — the key that produced Mark's 422
  },
  date_format: "%Y-%m-%d",
  decimal_comma: false,
  credits_positive: true,
  confidence: 0.4,
  reasoning: "Heuristic fallback …",
};

const CLEAN_SIGNED: SuggestProfileResponse = {
  columns: {
    date: "Date",
    raw_description: "Description",
    amount_native: "Amount",
  },
  date_format: "%Y-%m-%d",
  decimal_comma: false,
  credits_positive: true,
  confidence: 0.9,
  reasoning: "Header names match",
};

const HEADERS = [
  { label: "Date", value: "Date" },
  { label: "Description", value: "Description" },
  { label: "Amount", value: "Amount" },
  { label: "Withdrawal", value: "Withdrawal" },
  { label: "Deposit", value: "Deposit" },
];

/**
 * The wizard's selects are wrapped in their <label>, and a wrapping label's
 * textContent includes every <option> — so match on the label's OWN text nodes
 * instead of getByLabelText's string match.
 */
function selectFor(container: HTMLElement, labelText: string): HTMLSelectElement {
  const label = Array.from(container.querySelectorAll("label")).find((el) => {
    const own = Array.from(el.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent ?? "")
      .join("");
    return own.trim() === labelText;
  });
  if (!label) throw new Error(`no label whose own text is ${labelText}`);
  const select = label.querySelector("select");
  if (!select) throw new Error(`label ${labelText} has no select`);
  return select as HTMLSelectElement;
}

describe("M111 — wizard payload is built per mode", () => {
  it("never emits an empty-string column value, even for a poisoned draft", () => {
    const draft = draftFromSuggestion(SIGNED_WITH_STALE_SPLIT);
    // Simulate the worst case the old spread shipped verbatim.
    draft.columns.posting_date = "";
    draft.columns.currency = "   ";

    const profile = draftToProfile(draft);

    expect(Object.values(profile.columns)).not.toContain("");
    expect(Object.values(profile.columns).every((v) => v.trim() !== "")).toBe(true);
    // Known keys only, and the OTHER mode's keys are absent.
    expect(Object.keys(profile.columns).sort()).toEqual([
      "amount_native",
      "date",
      "raw_description",
    ]);
  });

  it("drops the suggester's stale debit and blank credit at ingest", () => {
    const draft = draftFromSuggestion(SIGNED_WITH_STALE_SPLIT);

    expect(draft.mode).toBe("signed");
    expect(draft.columns).not.toHaveProperty("debit");
    expect(draft.columns).not.toHaveProperty("credit");
    expect(draftToProfile(draft).columns).not.toHaveProperty("credit");
  });

  it("keeps split when the suggester returns BOTH split columns populated", () => {
    const draft = draftFromSuggestion({
      ...CLEAN_SIGNED,
      columns: {
        date: "Date",
        raw_description: "Description",
        debit: "Withdrawal",
        credit: "Deposit",
        amount_native: "Amount", // conflict: split wins, signed is dropped
      },
    });

    expect(draft.mode).toBe("split");
    expect(draft.columns).not.toHaveProperty("amount_native");
    const columns = draftToProfile(draft).columns;
    expect(Object.keys(columns).sort()).toEqual(["credit", "date", "debit", "raw_description"]);
  });
});

describe("M111 — Mark's live flow: switch through Debit & Credit, then save", () => {
  it("signed → split → signed ships no debit/credit keys and no blanks", () => {
    const onConfirm = vi.fn();
    const { container, getByRole } = render(
      <ColumnMappingWizard
        headers={HEADERS}
        suggestedProfile={CLEAN_SIGNED}
        onConfirm={onConfirm}
        onCancel={() => {}}
        saving={false}
      />,
    );

    // Round trip through the other mode (the stale-key generator).
    fireEvent.click(getByRole("tab", { name: "Debit & Credit" }));
    fireEvent.change(selectFor(container, "Debit column"), {
      target: { value: "Withdrawal" },
    });
    fireEvent.change(selectFor(container, "Credit column"), {
      target: { value: "Deposit" },
    });
    fireEvent.click(getByRole("tab", { name: "Single amount" }));

    // The amount column was dropped with the split keys — re-map it, as a user
    // would, and confirm.
    fireEvent.change(selectFor(container, "Amount column"), {
      target: { value: "Amount" },
    });
    fireEvent.click(getByRole("button", { name: "Confirm mappings" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const [profile] = onConfirm.mock.calls[0];
    expect(Object.keys(profile.columns).sort()).toEqual([
      "amount_native",
      "date",
      "raw_description",
    ]);
    expect(profile.columns).not.toHaveProperty("debit");
    expect(profile.columns).not.toHaveProperty("credit");
    expect(Object.values(profile.columns)).not.toContain("");
  });

  it("signed → split ships debit/credit and drops amount_native", () => {
    const onConfirm = vi.fn();
    const { container, getByRole } = render(
      <ColumnMappingWizard
        headers={HEADERS}
        suggestedProfile={CLEAN_SIGNED}
        onConfirm={onConfirm}
        onCancel={() => {}}
        saving={false}
      />,
    );

    fireEvent.click(getByRole("tab", { name: "Debit & Credit" }));
    fireEvent.change(selectFor(container, "Debit column"), {
      target: { value: "Withdrawal" },
    });
    fireEvent.change(selectFor(container, "Credit column"), {
      target: { value: "Deposit" },
    });
    fireEvent.click(getByRole("button", { name: "Confirm mappings" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const [profile] = onConfirm.mock.calls[0];
    expect(Object.keys(profile.columns).sort()).toEqual([
      "credit",
      "date",
      "debit",
      "raw_description",
    ]);
    expect(profile.columns).not.toHaveProperty("amount_native");
    expect(Object.values(profile.columns)).not.toContain("");
  });

  it("confirms with the suggester's poisoned split keys in signed mode", () => {
    const onConfirm = vi.fn();
    const { getByRole } = render(
      <ColumnMappingWizard
        headers={HEADERS}
        suggestedProfile={SIGNED_WITH_STALE_SPLIT}
        onConfirm={onConfirm}
        onCancel={() => {}}
        saving={false}
      />,
    );

    fireEvent.click(getByRole("button", { name: "Confirm mappings" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const [profile] = onConfirm.mock.calls[0];
    expect(Object.keys(profile.columns).sort()).toEqual([
      "amount_native",
      "date",
      "raw_description",
    ]);
    expect(Object.values(profile.columns)).not.toContain("");
  });
});
