// TagPicker — one searchable input that attaches an existing tag or creates
// one inline. The mechanics of the old picker are kept (searchable, inline
// create behind an exact-match check, reset on attach) and it is promoted to
// a real combobox (M104, docs/09 §Tag UX):
//
//   * ArrowDown / ArrowUp / Home / End walk the filtered list, Enter attaches
//     the highlighted row (or the exact match / the create row when nothing is
//     highlighted): `role="listbox"` + `role="option"` + `aria-activedescendant`
//     over a virtual highlight, so the INPUT keeps real focus and typing never
//     stops. Mouse behavior is unchanged — an option still attaches on
//     `mousedown`, which is what keeps the input focused.
//   * Escape closes the list FIRST and only that (it stops propagation): a
//     picker inside the transaction editor must not close the whole modal on
//     the first press (docs/09 nesting rule). With the list already closed the
//     key bubbles, so the surrounding dialog DOES close.
//   * Recency: while the query is empty the list is ordered by the tags
//     attached most recently this session (`lib/tagRecency`).
//   * Wider type: `min-w-56` input, `w-64` list, `text-sm` (11px in a control
//     you type into is too small on a phone).
//
// `TagFilterPicker` is the filter-mode variant the PM spec asked for — same
// listbox, no inline-create, chips for the current selection and a
// "Manage tags…" footer — a variant, not a second component to keep in sync.

import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type { Tag } from "../../api/tags";
import { byRecency } from "../../lib/tagRecency";
import { DemoLock } from "../ui/DemoLock";

const INPUT_CLASS =
  "field-ring min-w-56 rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted";
const OPTION_CLASS =
  "cursor-pointer px-2.5 py-1.5 text-sm text-text";

interface TagPickerProps {
  /** Already attached — offered only for the remaining tags. */
  attached: Tag[];
  allTags: Tag[];
  disabled?: boolean;
  creating?: boolean;
  onAttach: (tagId: number) => void;
  onCreate: (name: string) => void;
  /** Accessible name of the input — unique within its page (docs/09 M92). */
  label?: string;
  placeholder?: string;
  /** Appended to the input / list classes (full-width inside the modal). */
  inputClassName?: string;
  listClassName?: string;
  /** Off → the picker only ever SELECTS an existing tag (filter mode). */
  allowCreate?: boolean;
  /** Close the list after a selection (multi-select pickers stay open). */
  closeOnSelect?: boolean;
  /** Dropdown footer entry — the "Manage tags…" link lives here. */
  footer?: { label: string; onClick: () => void };
  /**
   * docs/47 §4 — the CALLER's verdict that this picker's action (an option
   * click or Enter, which attaches immediately) is a blocked demo write: the
   * input then wears the padlock and binds itself to the caller's VISIBLE
   * reason through `hintId`. The caller owns both so one reason can serve the
   * whole surface.
   */
  locked?: boolean;
  hintId?: string;
}

/** Tags matching `query`, recency-ordered while the query is empty. */
function matchingOptions(allTags: Tag[], excluded: Set<number>, query: string): Tag[] {
  const q = query.trim().toLowerCase();
  const filtered = allTags.filter(
    (tag) => !excluded.has(tag.id) && (q === "" || tag.name.toLowerCase().includes(q)),
  );
  return q === "" ? byRecency(filtered) : filtered;
}

export function TagPicker({
  attached,
  allTags,
  disabled = false,
  creating = false,
  onAttach,
  onCreate,
  label = "Add or create tag",
  placeholder,
  inputClassName = "",
  listClassName = "w-64",
  allowCreate = true,
  closeOnSelect = true,
  footer,
  locked = false,
  hintId,
}: TagPickerProps) {
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  /** Virtual highlight: -1 = nothing highlighted (Enter falls back to the
   *  exact match / create row, exactly as the old picker behaved). */
  const [active, setActive] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const listId = useId();

  // After a successful attach/create the parent's attached list grows:
  // reset the search so it never offers to create a name just attached.
  // Multi-select mode (filter picker) keeps the LIST open — clearing the
  // query there would close it under the user's cursor on the first pick.
  const attachedCount = attached.length;
  useEffect(() => {
    setSearch("");
    setActive(-1);
    if (closeOnSelect) setOpen(false);
  }, [attachedCount, closeOnSelect]);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  const attachedIds = new Set(attached.map((t) => t.id));
  const query = search.trim().toLowerCase();
  const filtered = matchingOptions(allTags, attachedIds, search);
  const exact = allTags.find(
    (t) => !attachedIds.has(t.id) && t.name.toLowerCase() === query,
  );
  const canCreate = allowCreate && query !== "" && exact === undefined;
  const optionCount = filtered.length + (canCreate ? 1 : 0);

  // The list re-filters under a highlight that no longer exists.
  useEffect(() => {
    if (active >= optionCount) setActive(optionCount - 1);
  }, [active, optionCount]);
  // A new query is a new list: drop the stale highlight so Enter keeps meaning
  // "the exact match / the create row" until the user actually arrows.
  useEffect(() => {
    setActive(-1);
  }, [search]);

  function reset(): void {
    setSearch("");
    setOpen(false);
    setActive(-1);
  }

  function activate(index: number): void {
    if (disabled || creating) return;
    if (index >= 0 && index < filtered.length) {
      onAttach(filtered[index].id);
      if (closeOnSelect) reset();
      else setSearch("");
      return;
    }
    if (canCreate && index === filtered.length) onCreate(search.trim());
  }

  function commit(): void {
    if (disabled || creating) return;
    if (active >= 0) {
      activate(active);
      return;
    }
    if (exact) {
      onAttach(exact.id);
      if (closeOnSelect) reset();
      else setSearch("");
    } else if (canCreate) {
      onCreate(search.trim());
    }
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLInputElement>): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (optionCount === 0) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = active < 0 ? (step > 0 ? 0 : optionCount - 1) : active + step;
      setActive((next + optionCount) % optionCount);
    } else if (event.key === "Home" && open && optionCount > 0) {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End" && open && optionCount > 0) {
      event.preventDefault();
      setActive(optionCount - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      commit();
    } else if (event.key === "Escape") {
      if (open) {
        // Innermost layer first: close ONLY this list. Stopping propagation
        // keeps the editor modal open on this press (docs/09 §Tag UX).
        event.stopPropagation();
        reset();
      }
      // List already closed → let it bubble to the surrounding dialog.
    }
  }

  return (
    <div ref={ref} className="relative">
      <input
        disabled={disabled || creating}
        value={search}
        placeholder={creating ? "Creating…" : placeholder ?? "Add or create tag…"}
        aria-label={label}
        role="combobox"
        aria-expanded={open}
        // A combobox MUST carry `aria-controls` while expanded (axe
        // `aria-required-attr`; a CLOSED combobox is exempt — the same shape
        // SecurityPicker ships). With zero options the popup is still the
        // "No matches" panel, so THAT node carries the id instead of a listbox
        // that does not exist.
        aria-controls={open ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={
          open && active >= 0 ? `${listId}-opt-${active}` : undefined
        }
        autoComplete="off"
        onChange={(event) => {
          setSearch(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
        // `pr-7` keeps typed text clear of the padlock glyph (docs/47 §4).
        aria-describedby={locked && hintId !== undefined ? hintId : undefined}
        className={`${INPUT_CLASS} rounded-lg ${inputClassName} ${locked ? "pr-7" : ""}`}
      />
      {locked && (
        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">
          <DemoLock />
        </span>
      )}
      {open && (
        <div
          id={optionCount > 0 ? undefined : listId}
          className={`absolute z-10 mt-1 overflow-hidden rounded-lg border border-control bg-surface shadow-lg ${listClassName}`}
        >
          {optionCount > 0 && (
            /* The id sits on the SCROLLING listbox, not its wrapper: axe's
               `scrollable-region-focusable` exempts a combobox popup by
               looking for `[role=combobox][aria-controls~=id]` on the
               scrollable node itself, and a listbox of non-focusable options
               would otherwise be a serious violation while it overflows. */
            <ul
              id={listId}
              role="listbox"
              aria-label={label}
              className="max-h-48 overflow-y-auto py-0.5"
            >
              {filtered.map((tag, index) => (
                <li
                  key={tag.id}
                  id={`${listId}-opt-${index}`}
                  role="option"
                  aria-selected={active === index}
                  className={`${OPTION_CLASS} ${active === index ? "bg-accent/10" : ""}`}
                  onMouseMove={() => setActive(index)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    activate(index);
                  }}
                >
                  {tag.name}
                </li>
              ))}
              {canCreate && (
                <li
                  id={`${listId}-opt-${filtered.length}`}
                  role="option"
                  aria-selected={active === filtered.length}
                  className={`${OPTION_CLASS} font-medium text-accent ${
                    active === filtered.length ? "bg-accent/10" : ""
                  }`}
                  onMouseMove={() => setActive(filtered.length)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    activate(filtered.length);
                  }}
                >
                  {creating ? "Creating…" : `Create "${search.trim()}"`}
                </li>
              )}
            </ul>
          )}
          {optionCount === 0 && (
            <p className="px-2.5 py-1.5 text-sm text-muted">No matches</p>
          )}
          {footer && (
            <div className="border-t border-border p-1">
              <button
                type="button"
                disabled={disabled}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  footer.onClick();
                  if (closeOnSelect) reset();
                  else setOpen(false);
                }}
                className="w-full rounded-md px-2 py-1.5 text-left text-sm text-accent outline-none hover:bg-accent/10 focus-visible:ring-2 focus-visible:ring-accent"
              >
                {footer.label}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface TagFilterPickerProps {
  allTags: Tag[];
  /** Current selection — rendered as removable chips (the bar's own state). */
  selected: Tag[];
  onToggle: (tag: Tag) => void;
  onRemove: (tag: Tag) => void;
  onManage?: () => void;
  label?: string;
  placeholder?: string;
}

/** Filter mode (M104): the same listbox as a multi-select with chips, plus
 *  the "Manage tags…" entry the PM spec hangs off this picker. */
export function TagFilterPicker({
  allTags,
  selected,
  onToggle,
  onRemove,
  onManage,
  label = "Add tag to filter",
  // Placeholder and accessible name stay the same sentence: the visible text
  // of the control is part of its name (WCAG 2.5.3, docs/09 naming contract).
  placeholder = "Add tag to filter…",
}: TagFilterPickerProps) {
  return (
    <div className="flex flex-col gap-1.5">
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent"
            >
              {tag.name}
              <button
                type="button"
                aria-label={`Remove tag filter — ${tag.name}`}
                onClick={() => onRemove(tag)}
                className="ml-0.5 text-accent outline-none hover:text-danger focus-visible:ring-2 focus-visible:ring-accent"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <TagPicker
        attached={selected}
        allTags={allTags}
        allowCreate={false}
        closeOnSelect={false}
        label={label}
        placeholder={placeholder}
        onAttach={(tagId) => {
          const tag = allTags.find((t) => t.id === tagId);
          if (tag) onToggle(tag);
        }}
        onCreate={() => undefined}
        footer={onManage ? { label: "Manage tags…", onClick: onManage } : undefined}
      />
    </div>
  );
}
