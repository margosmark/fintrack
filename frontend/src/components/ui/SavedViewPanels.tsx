import { useState } from "react";
import { Button } from "./Button";
import { inputClass } from "./formStyles";

/**
 * M108 P2 / M123 (docs/09 §Saved report views, §Create from search) — the two
 * panels every saved-view control is made of: the save form and the
 * rename/delete list.
 *
 * Extracted so the Reports header's `SavedViewsControl` and the Transactions
 * header's `Create from search` menu share ONE implementation of
 * "client-side list edit" (M108's whole-list PATCH precedent) instead of two
 * that can drift apart.
 *
 * Deliberately NOT a portal popover: both panels render inline on a
 * `basis-full` row directly under the trigger, so the surface needs no
 * positioning maths, no click-outside listener and no focus trap, is reachable
 * by Tab in DOM order, and cannot be clipped by a 390px viewport. The
 * trigger's `aria-expanded`/`aria-controls` pair is what makes it a disclosure
 * rather than a mystery box.
 */

/** Client-side preset id: 8+ chars of `[A-Za-z0-9_-]` (the server's pattern). */
export function newViewId(): string {
  const cryptoRandom =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "")
      : "";
  return `v${cryptoRandom || `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`}`;
}

interface SaveViewPanelProps {
  /** The panel's DOM id — the trigger's `aria-controls` target. */
  id: string;
  title: string;
  onTitleChange: (title: string) => void;
  placeholder: string;
  /** What this control actually keeps — stated where the save happens. */
  hint: string;
  busy: boolean;
  /** Called with the trimmed title (≤80 chars) once the form is submitted. */
  onSave: (trimmedTitle: string) => void;
  onCancel: () => void;
}

export function SaveViewPanel({
  id,
  title,
  onTitleChange,
  placeholder,
  hint,
  busy,
  onSave,
  onCancel,
}: SaveViewPanelProps) {
  return (
    <div id={id} className="basis-full rounded-xl border border-control bg-surface-raised p-3">
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = title.trim();
          if (trimmed.length === 0) return;
          onSave(trimmed.slice(0, 80));
        }}
      >
        <label className="flex flex-1 flex-col gap-1 text-xs">
          <span className="text-muted">Name this view</span>
          <input
            autoFocus
            value={title}
            maxLength={80}
            onChange={(event) => onTitleChange(event.target.value)}
            className={`${inputClass} bg-surface`}
            placeholder={placeholder}
          />
        </label>
        <Button type="submit" size="sm" disabled={busy || title.trim().length === 0}>
          Save
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </form>
      <p className="mt-2 text-xs text-muted">{hint}</p>
    </div>
  );
}

/** The minimum every saved view carries — reports add `tab`/`state`. */
export interface SavedViewLike {
  id: string;
  title: string;
}

interface SavedViewListPanelProps<T extends SavedViewLike> {
  id: string;
  /** Already filtered to this control's surface by the caller. */
  views: T[];
  /** One muted line saying what the preset restores (its shape differs per kind). */
  describe: (view: T) => string;
  emptyHint: string;
  busy: boolean;
  onRestore: (view: T) => void;
  /** Resolve `true` when the rename landed; the row leaves edit mode then. */
  onRename: (view: T, title: string) => Promise<boolean>;
  onDelete: (view: T) => void;
}

/**
 * The list keeps its own rename state: the panel unmounts whenever the
 * disclosure closes, which is exactly when M108's control cleared it.
 */
export function SavedViewListPanel<T extends SavedViewLike>({
  id,
  views,
  describe,
  emptyHint,
  busy,
  onRestore,
  onRename,
  onDelete,
}: SavedViewListPanelProps<T>) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  return (
    <div id={id} className="basis-full rounded-xl border border-control bg-surface-raised p-3">
      {views.length === 0 ? (
        <p className="text-sm text-muted">{emptyHint}</p>
      ) : (
        <ul className="space-y-2">
          {views.map((view) => {
            const renaming = renamingId === view.id;
            return (
              <li key={view.id} className="flex flex-wrap items-center gap-2">
                {renaming ? (
                  <>
                    <input
                      autoFocus
                      value={renameValue}
                      maxLength={80}
                      aria-label={`New name for ${view.title}`}
                      onChange={(event) => setRenameValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") setRenamingId(null);
                      }}
                      className={`${inputClass} bg-surface flex-1`}
                    />
                    <Button
                      size="sm"
                      disabled={busy || renameValue.trim().length === 0}
                      onClick={() => {
                        const next = renameValue.trim();
                        if (next.length === 0) return;
                        void onRename(view, next.slice(0, 80)).then((ok) => {
                          if (ok) setRenamingId(null);
                        });
                      }}
                    >
                      Rename
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setRenamingId(null)}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => onRestore(view)}
                      className="min-h-11 flex-1 rounded-lg border border-control bg-veil/[0.04] px-3 py-2 text-left text-sm outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      <span className="block font-medium text-text">{view.title}</span>
                      <span className="block text-xs text-muted tabular">{describe(view)}</span>
                    </button>
                    <button
                      type="button"
                      aria-label={`Rename saved view ${view.title}`}
                      onClick={() => {
                        setRenamingId(view.id);
                        setRenameValue(view.title);
                      }}
                      className="min-h-11 min-w-11 rounded-lg px-2 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      Rename
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete saved view ${view.title}`}
                      disabled={busy}
                      onClick={() => onDelete(view)}
                      className="min-h-11 min-w-11 rounded-lg px-2 text-xs text-danger outline-none hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger"
                    >
                      Delete
                    </button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
