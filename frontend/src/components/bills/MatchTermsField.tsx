// MatchTermsField — the M121 rule editor control (docs/25 §M121 D1/D2/D8),
// extended by M122 T1 with suggestions from the user's OWN ledger.
//
// A bill's rule is a TERM LIST with an AND/OR mode, so the single "Match term"
// input becomes a CHIPS input beside the ALL/ANY segmented switch:
//
//   * Enter commits the typed text as one chip, Backspace on an EMPTY input
//     removes the last chip (the keyboard-complete path docs/09 §Tag UX
//     established), and every chip carries its own labelled remove button.
//   * The typed-but-uncommitted text is never lost: the parent composes it
//     into the payload (`composeTerms`), and `onBlur` commits it too, so a
//     Save click can never drop a term the user typed.
//   * The list is rate-limited to `BILL_MAX_TERMS` (docs/25 §M121 D7) — the
//     input stays usable so a half-typed term survives, but a full list
//     refuses the add and SAYS why instead of silently swallowing it.
//   * ANY/ALL are visible words, not icons: at 390px the switch still reads
//     as a two-word choice, and each segment's `title` spells out the effect
//     (wider / narrower set) for pointer users.
//
// M122 T1 — suggestions (the caller's own payees, server-ranked):
//   * an EMPTY draft shows the top candidate groups as QUICK-ADD chips
//     ("ROGERS COMMUNICATIONS ×14" — the count is social proof, and every
//     string comes straight out of the user's own data);
//   * a draft of ≥3 chars opens a `role="listbox"` under the input, walked
//     with ArrowUp/ArrowDown/Home/End and committed with Enter or a click —
//     the same combobox + `aria-activedescendant` contract the M104 TagPicker
//     pins (docs/09 §Tag UX), so the INPUT keeps real focus and typing never
//     stops. Enter with NO highlight still commits the typed text, exactly as
//     before; Escape dismisses the list first and only that.
//   * selecting a suggestion REPLACES the draft (the suggestion is the fuller
//     spelling of what was being typed).

import { useEffect, useId, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { X } from "lucide-react";
import {
  BILL_MAX_TERM_LENGTH,
  BILL_MAX_TERMS,
  BILL_MATCH_LOGIC_HINTS,
  BILL_MATCH_LOGIC_LABELS,
  BILL_MATCH_LOGICS,
  BILL_SUGGEST_MIN_CHARS,
  type BillMatchLogic,
  type BillSuggestTerm,
} from "../../api/bills";
import { SegmentedControl } from "../ui/SegmentedControl";

export interface MatchTermsFieldProps {
  /** Committed terms, in order — one chip each. */
  terms: string[];
  /** The text currently typed in the input (not yet a chip). */
  draft: string;
  logic: BillMatchLogic;
  onDraftChange: (value: string) => void;
  onAdd: (term: string) => void;
  onRemove: (index: number) => void;
  onLogicChange: (logic: BillMatchLogic) => void;
  /** Accessible name of the input AND of the chip list (unique per page). */
  label?: string;
  placeholder?: string;
  disabled?: boolean;
  /** The page's own field styling, so the control looks like its neighbours. */
  fieldClassName: string;
  /**
   * M122 T1 — server-ranked payee suggestions (already filtered by the parent
   * through `useTermSuggestions`). Omitted = no suggestion UI at all.
   */
  suggestions?: BillSuggestTerm[];
}

/** `ROGERS COMMUNICATIONS ×14` — the frequency rides with the name. */
function suggestionText(suggestion: BillSuggestTerm): string {
  return `${suggestion.normalized_payee} ×${suggestion.count}`;
}

export function MatchTermsField({
  terms,
  draft,
  logic,
  onDraftChange,
  onAdd,
  onRemove,
  onLogicChange,
  label = "Match terms",
  placeholder,
  disabled = false,
  fieldClassName,
  suggestions,
}: MatchTermsFieldProps) {
  const inputId = useId();
  const hintId = `${inputId}-hint`;
  const layoutId = useId();
  const listId = `${inputId}-listbox`;
  const full = terms.length >= BILL_MAX_TERMS;

  /** Virtual highlight into the suggestion list (-1 = none). */
  const [active, setActive] = useState(-1);
  /** Escape dismissed the list for the current draft only. */
  const [dismissed, setDismissed] = useState(false);

  const trimmed = draft.trim();
  const listOpen =
    !disabled &&
    !dismissed &&
    trimmed.length >= BILL_SUGGEST_MIN_CHARS &&
    (suggestions?.length ?? 0) > 0;
  const options = listOpen ? (suggestions ?? []) : [];
  // The empty-draft candidates: quick-add chips, no focus needed.
  const quickAdd = !disabled && trimmed === "" ? (suggestions ?? []) : [];

  // A new draft is a new list: drop the stale highlight and the dismissal.
  useEffect(() => {
    setActive(-1);
    setDismissed(false);
  }, [draft]);

  // The list re-filters under a highlight that no longer exists.
  useEffect(() => {
    if (active >= options.length) setActive(options.length - 1);
  }, [active, options.length]);

  function commit(): void {
    const value = draft.trim();
    // Nothing typed, a duplicate, or a full list: keep the text and say why —
    // never clear an input whose term was not added.
    if (value === "" || terms.includes(value) || full || disabled) return;
    onAdd(value);
    onDraftChange("");
  }

  function pickSuggestion(index: number): void {
    const suggestion = options[index];
    if (!suggestion || full || disabled) return;
    onAdd(suggestion.normalized_payee);
    // The suggestion is the fuller spelling of the draft it replaces.
    onDraftChange("");
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLInputElement>): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!listOpen) return; // caret movement is the right default without a list
      event.preventDefault();
      if (options.length === 0) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = active < 0 ? (step > 0 ? 0 : options.length - 1) : active + step;
      setActive((next + options.length) % options.length);
      return;
    }
    if ((event.key === "Home" || event.key === "End") && listOpen && options.length > 0) {
      event.preventDefault();
      setActive(event.key === "Home" ? 0 : options.length - 1);
      return;
    }
    if (event.key === "Enter") {
      // A term is not a form submission — the form has its own buttons.
      event.preventDefault();
      if (listOpen && active >= 0) {
        pickSuggestion(active);
        return;
      }
      commit();
      return;
    }
    if (event.key === "Escape" && listOpen) {
      // Dismiss ONLY the suggestion list (the TagPicker nesting rule, docs/09).
      event.preventDefault();
      setDismissed(true);
      setActive(-1);
      return;
    }
    if (event.key === "Backspace" && draft === "" && terms.length > 0 && !disabled) {
      event.preventDefault();
      onRemove(terms.length - 1);
    }
  }

  const hint = full
    ? `Up to ${BILL_MAX_TERMS} terms — remove one before adding another.`
    : `Enter to add · Backspace on an empty field removes the last term · up to ${BILL_MAX_TERMS} terms · suggestions come from your own transactions`;

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-end gap-2">
        <label htmlFor={inputId} className="block min-w-40 flex-1 text-xs text-muted">
          {label}
          <input
            id={inputId}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commit}
            maxLength={BILL_MAX_TERM_LENGTH}
            disabled={disabled}
            placeholder={placeholder}
            aria-describedby={hintId}
            role="combobox"
            aria-expanded={listOpen}
            // A combobox MUST carry `aria-controls` while expanded (axe
            // `aria-required-attr`; a CLOSED combobox is exempt — the same
            // shape TagPicker ships, docs/09 §Tag UX).
            aria-controls={listOpen ? listId : undefined}
            aria-autocomplete="list"
            aria-activedescendant={
              listOpen && active >= 0 ? `${listId}-opt-${active}` : undefined
            }
            autoComplete="off"
            className={fieldClassName}
          />
        </label>
        <SegmentedControl
          options={BILL_MATCH_LOGICS.map((value) => ({
            value,
            label: BILL_MATCH_LOGIC_LABELS[value],
            title: BILL_MATCH_LOGIC_HINTS[value],
          }))}
          value={logic}
          onChange={onLogicChange}
          layoutId={layoutId}
          size="sm"
          disabled={disabled}
          ariaLabel="Match logic — ANY or ALL"
          className="mb-0.5"
        />
      </div>

      {/* M122 T1: the empty-draft candidates, as quick-add chips — every
          string is a real payee from the user's own ledger, with its
          frequency as social proof. */}
      {quickAdd.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          <span className="text-[11px] text-muted">In your ledger:</span>
          {quickAdd.map((suggestion) => (
            <button
              key={suggestion.normalized_payee}
              type="button"
              disabled={full}
              aria-label={`Add suggested term — ${suggestionText(suggestion)}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onAdd(suggestion.normalized_payee)}
              className="cursor-pointer rounded-full border border-control bg-surface px-2 py-0.5 text-[11px] text-text outline-none hover:border-accent hover:text-accent focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              {suggestion.normalized_payee}{" "}
              <span className="tabular text-muted">×{suggestion.count}</span>
            </button>
          ))}
        </div>
      )}

      {/* M122 T1: the as-you-type listbox. The id sits on the SCROLLING
          listbox (axe `scrollable-region-focusable` exempts a combobox popup
          by matching `[role=combobox][aria-controls~=id]` on the scrollable
          node itself — the TagPicker lesson, docs/09). */}
      {listOpen && (
        <ul
          id={listId}
          role="listbox"
          aria-label={`${label} suggestions`}
          className="mt-1 max-h-48 overflow-y-auto rounded-lg border border-control bg-surface py-0.5"
        >
          {options.map((suggestion, index) => (
            <li
              key={suggestion.normalized_payee}
              id={`${listId}-opt-${index}`}
              role="option"
              aria-selected={active === index}
              onMouseMove={() => setActive(index)}
              onMouseDown={(event) => {
                // Keep the input focused: a blur would commit the DRAFT text
                // instead of the suggestion the user is clicking.
                event.preventDefault();
                pickSuggestion(index);
              }}
              className={`flex cursor-pointer items-center justify-between gap-2 px-2.5 py-1.5 text-sm text-text ${
                active === index ? "bg-accent/10" : ""
              }`}
            >
              <span className="truncate">{suggestion.normalized_payee}</span>
              <span className="shrink-0 tabular text-[11px] text-muted">
                ×{suggestion.count}
              </span>
            </li>
          ))}
        </ul>
      )}

      {terms.length > 0 && (
        <ul aria-label={label} className="mt-2 flex flex-wrap gap-1">
          {terms.map((term, index) => (
            <li
              key={`${index}-${term}`}
              className="inline-flex max-w-full items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent"
            >
              <span className="max-w-[14rem] truncate">{term}</span>
              <button
                type="button"
                aria-label={`Remove term — ${term}`}
                disabled={disabled}
                onClick={() => onRemove(index)}
                className="shrink-0 cursor-pointer rounded-full p-0.5 outline-none hover:bg-accent/20 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                <X className="size-3" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}

      <p id={hintId} className="mt-1 text-[11px] leading-relaxed text-muted">
        {hint}
      </p>
    </div>
  );
}
