import { useEffect, useRef, useState } from "react";
import { createCategory } from "../../api/categories";
import type { Category } from "../../api/categories";
import { apiErrorMessage } from "./format";
import { DemoLock } from "../ui/DemoLock";

const INPUT_CLASS =
  "field-ring block w-full rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

const MENU_CLASS =
  "absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-control bg-surface shadow-lg";

/**
 * ONE normalization for both halves of the "don't make a duplicate" rule —
 * the comparison AND the value POSTed: trim the edges and collapse internal
 * whitespace runs, so `"  Gas "` and `"Gas"` are the same category and a
 * case-equivalent `"gas"` is too. The server stores names verbatim (there is
 * no per-user unique constraint), so the guard lives at the one control that
 * creates them rather than speculatively in the router.
 */
function normalizeName(raw: string): string {
  return raw.trim().replace(/\s+/g, " ");
}

interface CategoryPickerProps {
  value: string;
  categories: Category[];
  disabled?: boolean;
  onChange: (categoryId: string) => void;
  onCategoryCreated?: (category: Category) => void;
  inputClassName?: string;
  /**
   * Position classes for the dropdown. Defaults to opening BELOW the input.
   * Call sites whose box is pinned to the bottom of the viewport (the bulk
   * toolbar below `md`, where a downward menu is the only one off-screen) pass
   * a responsive string that flips at a breakpoint — a plain prop cannot read
   * the viewport, so the caller spells out both placements.
   */
  menuClassName?: string;
  /**
   * Accessible name for the search box, for the call sites that do NOT wrap
   * the picker in a `<label>` (the transaction/bill table rows render it bare
   * inside a `<td>`, where a wrapping label is impossible). Left unset where a
   * wrapping label already names it — `aria-label` would OVERRIDE that label,
   * so the two paths have to stay distinguishable rather than one defaulting
   * over the other.
   */
  ariaLabel?: string;
  /**
   * docs/47 §4 — the CALLER's §4 verdict for the write this picker drives
   * (its option click PATCHes the row; `Create` POSTs a category). When true
   * the input wears the padlock and binds itself to the caller's VISIBLE
   * reason via `lockReasonId` — the caller owns both, because one reason
   * element serves every row control on the page.
   */
  locked?: boolean;
  lockReasonId?: string;
  /**
   * The caller's demo-refusal guard (docs/47 §4). Returns true when the
   * failure is a `403 demo_account_restricted` the caller now renders as its
   * ONE `demo-refusal` + one `toast.info` — in that case the inline
   * `role="alert"` line must not be raised for it.
   */
  onError?: (error: unknown) => boolean;
}

export function CategoryPicker({
  value,
  categories,
  disabled = false,
  onChange,
  onCategoryCreated,
  inputClassName,
  menuClassName,
  ariaLabel,
  locked = false,
  lockReasonId,
  onError,
}: CategoryPickerProps) {
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  // A create failure is SHOWN at the control that caused it (M176). The old
  // `catch {}` deferred to a query error that never exists for a POST, so a
  // refused create looked exactly like a no-op.
  const [createError, setCreateError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  const selected = categories.find((c) => String(c.id) === value);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  const filtered = search.trim()
    ? categories.filter((c) => c.name.toLowerCase().includes(search.trim().toLowerCase()))
    : categories;

  const displayValue = selected ? selected.name : search;

  const candidate = normalizeName(search);
  const canCreate =
    candidate.length > 0 &&
    !categories.some(
      (c) => normalizeName(c.name).toLowerCase() === candidate.toLowerCase(),
    );

  async function handleCreate(): Promise<void> {
    const name = normalizeName(search);
    // An explicit Create is the ONLY path that writes, and it can only run
    // once at a time: the `creating` guard blocks a second click (double
    // submit) even before the disabled attribute lands.
    if (name === "" || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createCategory({ name });
      onChange(String(created.id));
      onCategoryCreated?.(created);
      setSearch("");
      setOpen(false);
    } catch (error) {
      // docs/47 §4: a demo refusal is the caller's refusal to render (one
      // `demo-refusal` + one `toast.info`) — never this alert line.
      if (onError !== undefined && onError(error)) return;
      // Visible, and retryable: the Create row stays put, so the same click
      // runs again. Never the swallowed catch.
      setCreateError(apiErrorMessage(error));
    } finally {
      setCreating(false);
    }
  }

  return (
    <div ref={ref} className="relative">
      <input
        disabled={disabled || creating}
        value={open ? search : displayValue}
        placeholder="Type to search…"
        // `pr-7` keeps typed text clear of the padlock glyph (docs/47 §4).
        className={`${inputClassName ?? INPUT_CLASS} ${locked ? "pr-7" : ""}`}
        aria-label={ariaLabel}
        aria-describedby={locked && lockReasonId !== undefined ? lockReasonId : undefined}
        autoComplete="off"
        onChange={(e) => {
          setSearch(e.target.value);
          setOpen(true);
          setCreateError(null);
          if (selected) onChange("");
        }}
        onFocus={() => {
          if (selected) setSearch(selected.name);
          setOpen(true);
        }}
      />
      {locked && (
        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">
          <DemoLock />
        </span>
      )}
      {open && (
        <ul
          data-testid="category-picker-menu"
          className={menuClassName ?? MENU_CLASS}
        >
          {filtered.map((category) => (
            <li key={category.id}>
              <button
                type="button"
                disabled={disabled || creating}
                className={`w-full px-2.5 py-1.5 text-left text-sm hover:bg-accent/10 ${
                  String(category.id) === value
                    ? "bg-accent/15 font-medium text-accent"
                    : "text-text"
                }`}
                // `onClick`, not `onMouseDown`: a button answers a keyboard
                // Enter/Space (and a tap) with `click` only, so the old
                // mousedown-only handler made options and creation
                // mouse-exclusive. The outside-click listener watches
                // mousedown and sees this target inside `ref`, so the menu
                // stays open until the handler itself closes it.
                onClick={() => {
                  onChange(String(category.id));
                  setSearch("");
                  setOpen(false);
                }}
              >
                {category.name}
              </button>
            </li>
          ))}
          {canCreate && (
            <li className="border-t border-border">
              <button
                type="button"
                disabled={disabled || creating}
                className="w-full px-2.5 py-1.5 text-left text-sm font-medium text-accent hover:bg-accent/10"
                onClick={() => void handleCreate()}
              >
                {creating ? "Creating…" : `Create "${candidate}"`}
              </button>
            </li>
          )}
          {createError !== null && (
            <li
              role="alert"
              data-testid="category-create-error"
              className="border-t border-border px-2.5 py-1.5 text-xs text-danger"
            >
              {`Couldn’t create “${candidate}” — ${createError}. Try again.`}
            </li>
          )}
          {filtered.length === 0 && !canCreate && (
            <li className="px-2.5 py-1.5 text-sm text-muted">No matches</li>
          )}
        </ul>
      )}
    </div>
  );
}
