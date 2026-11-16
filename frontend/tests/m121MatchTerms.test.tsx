// M121 — the multi-term chips input and its ALL/ANY switch (docs/25 §M121,
// docs/09 §Match terms).
//
// What is pinned here, because each is a way the control could look right and
// be wrong:
//
//   * keyboard completeness — Enter commits, Backspace on an EMPTY field
//     removes the last chip (never a character the user did not type), and
//     every chip is removable by its own labelled button;
//   * the typed-but-uncommitted term is never lost (`composeTerms`);
//   * the 10-term rate limit REFUSES loudly instead of dropping a term;
//   * ANY/ALL are visible words with `aria-selected` state, defaulting to ANY
//     — the widening mode a single-term rule has always behaved as.

import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  BILL_MAX_TERMS,
  BILL_MATCH_LOGIC_LABELS,
  composeTerms,
  describeTerms,
  readTerms,
  type BillMatchLogic,
} from "../src/api/bills";
import { MatchTermsField } from "../src/components/bills/MatchTermsField";

// framer-motion (the segmented control's morphing pill) needs matchMedia;
// jsdom does not implement it. Same guard the M116 page tests use.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

/** Stateful wrapper — the control is driven exactly as a page drives it. */
function Harness({ initial = [] }: { initial?: string[] }) {
  const [terms, setTerms] = useState<string[]>(initial);
  const [draft, setDraft] = useState("");
  const [logic, setLogic] = useState<BillMatchLogic>("any");
  return (
    <MatchTermsField
      terms={terms}
      draft={draft}
      logic={logic}
      onDraftChange={setDraft}
      onAdd={(term) => setTerms((prev) => (prev.includes(term) ? prev : [...prev, term]))}
      onRemove={(index) =>
        setTerms((prev) => prev.filter((_term, position) => position !== index))
      }
      onLogicChange={setLogic}
      fieldClassName="field"
    />
  );
}

function input(): HTMLInputElement {
  // M122 T1: the input carries the combobox role (aria-expanded /
  // aria-activedescendant for the suggestion listbox).
  return screen.getByRole("combobox", { name: "Match terms" }) as HTMLInputElement;
}

function typeAndCommit(value: string): void {
  fireEvent.change(input(), { target: { value } });
  fireEvent.keyDown(input(), { key: "Enter" });
}

function chips(): string[] {
  const list = screen.queryByRole("list", { name: "Match terms" });
  // No chips → the list is not rendered at all (docs/09: an empty list is
  // noise, not a state).
  if (list === null) return [];
  return within(list)
    .getAllByRole("listitem")
    .map((item) => item.textContent ?? "");
}

describe("match terms field (M121 D1/D2)", () => {
  it("adds a chip on Enter, clears the field, and never submits the form", () => {
    render(<Harness />);
    expect(chips()).toEqual([]);
    typeAndCommit("ROGERS");
    expect(chips()).toEqual(["ROGERS"]);
    expect(input().value).toBe("");
    typeAndCommit("  RCOM  ");
    expect(chips()).toEqual(["ROGERS", "RCOM"]);
    // The typed text is TRIMMED: a chip is the term, spaces and all.
    expect(screen.getByText("RCOM")).toBeTruthy();
  });

  it("removes the last chip on Backspace only when the field is empty", () => {
    render(<Harness initial={["ROGERS", "RCOM"]} />);
    // Not empty → Backspace edits the text, it does not delete a chip.
    fireEvent.change(input(), { target: { value: "RO" } });
    fireEvent.keyDown(input(), { key: "Backspace" });
    expect(chips()).toEqual(["ROGERS", "RCOM"]);
    expect(input().value).toBe("RO");
    // Empty → Backspace walks the list backwards, one chip per press.
    fireEvent.change(input(), { target: { value: "" } });
    fireEvent.keyDown(input(), { key: "Backspace" });
    expect(chips()).toEqual(["ROGERS"]);
    fireEvent.keyDown(input(), { key: "Backspace" });
    expect(chips()).toEqual([]);
    // A third press on an empty list is a no-op, never an error.
    fireEvent.keyDown(input(), { key: "Backspace" });
    expect(chips()).toEqual([]);
  });

  it("removes a middle chip by its labelled button, leaving the rest in order", () => {
    render(<Harness initial={["ROGERS", "RCOM", "WIRELESS"]} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove term — RCOM" }));
    expect(chips()).toEqual(["ROGERS", "WIRELESS"]);
    expect(screen.queryByRole("button", { name: "Remove term — RCOM" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove term — WIRELESS" })).toBeTruthy();
  });

  it("refuses an 11th term and says why instead of dropping it", () => {
    const ten = Array.from({ length: BILL_MAX_TERMS }, (_value, index) => `TERM${index}`);
    render(<Harness initial={ten} />);
    typeAndCommit("ONE_TOO_MANY");
    expect(chips()).toHaveLength(BILL_MAX_TERMS);
    expect(screen.getByText(/Up to 10 terms — remove one/)).toBeTruthy();
    // The text the user typed is still there — nothing was swallowed.
    expect(input().value).toBe("ONE_TOO_MANY");
  });

  it("does not add the same term twice", () => {
    render(<Harness initial={["ROGERS"]} />);
    typeAndCommit("ROGERS");
    expect(chips()).toEqual(["ROGERS"]);
    typeAndCommit("rogers");
    // Case-sensitive dedupe: the shared normalizer runs server-side, so a
    // differently-cased chip is a different TERM here and is kept.
    expect(chips()).toEqual(["ROGERS", "rogers"]);
  });

  it("shows ANY/ALL as words, ANY selected by default, and switches on click", () => {
    render(<Harness initial={["ROGERS"]} />);
    const control = screen.getByRole("tablist", { name: /Match logic/i });
    const any = within(control).getByRole("tab", { name: BILL_MATCH_LOGIC_LABELS.any });
    const all = within(control).getByRole("tab", { name: BILL_MATCH_LOGIC_LABELS.all });
    // The words themselves (docs/09 pins the labels) — what a 390px viewport
    // has to be able to read, and what the live probe asserts at that width.
    expect(any.textContent).toBe("ANY");
    expect(all.textContent).toBe("ALL");
    expect(any.getAttribute("aria-selected")).toBe("true");
    expect(all.getAttribute("aria-selected")).toBe("false");
    fireEvent.click(all);
    expect(any.getAttribute("aria-selected")).toBe("false");
    expect(all.getAttribute("aria-selected")).toBe("true");
  });

  it("keeps a typed term that was never committed (the payload composes it)", () => {
    expect(composeTerms([], "")).toEqual([]);
    expect(composeTerms(["ROGERS"], "")).toEqual(["ROGERS"]);
    expect(composeTerms(["ROGERS"], "  RCOM  ")).toEqual(["ROGERS", "RCOM"]);
    // Never a duplicate chip, never an over-length term.
    expect(composeTerms(["ROGERS"], "ROGERS")).toEqual(["ROGERS"]);
    expect(composeTerms([], "x".repeat(500))).toHaveLength(1);
    expect(composeTerms([], "x".repeat(500))[0]).toHaveLength(120);
  });
});

describe("the wire and the header sentence (M121 D6/D9)", () => {
  it("reads an M116-era payload as the single-term, ANY rule it was", () => {
    expect(readTerms({ match_term: "NETFLIX" })).toEqual(["NETFLIX"]);
    expect(readTerms({ match_terms: ["A", "B"], match_term: "A" })).toEqual(["A", "B"]);
    expect(readTerms({ match_term: "" })).toEqual([]);
  });

  it("says the conjunction out loud: or for ANY, and for ALL", () => {
    expect(describeTerms(["ROGERS"], "any")).toBe("“ROGERS”");
    expect(describeTerms(["ROGERS", "RCOM"], "any")).toBe("“ROGERS” or “RCOM”");
    expect(describeTerms(["ROGERS", "WIRELESS"], "all")).toBe("“ROGERS” and “WIRELESS”");
  });
});
