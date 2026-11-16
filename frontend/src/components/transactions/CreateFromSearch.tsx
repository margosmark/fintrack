import { useEffect, useRef, useState } from "react";
import { Bookmark, ChevronDown, GitBranch, ListFilter, Plus, Receipt, Wallet } from "lucide-react";
import {
  criteriaLabel,
  onCreateFromSearch,
  targetHref,
  type CreateTargetId,
  type SearchCriteria,
} from "../../lib/createFromSearch";
import type { SavedReportView } from "../../types";
import { Button } from "../ui/Button";
import { SaveViewPanel, SavedViewListPanel, newViewId } from "../ui/SavedViewPanels";
import { CreateTargetMenu, type CreateTargetItem } from "./CreateTargetMenu";

/**
 * M123 (docs/09 §Create from search) — the Transactions header's one control
 * that turns the CURRENT search into something reusable:
 *
 *   Create  →  Saved view | Rule | Bill | Budget
 *   Views   →  the saved views that restore back to this search
 *
 * One component, one panel slot: choosing a target navigates to that editor
 * with a `?prefill=…` deep link rather than rendering four per-target forms
 * beside a filter bar. The only target that edits in place is "Saved view",
 * which uses the SAME save form and rename/delete list the Reports header
 * ships (`components/ui/SavedViewPanels`), so there is exactly one
 * implementation of M108's whole-list PATCH.
 *
 * A11y: the Create trigger is a real `menu` (`aria-haspopup="menu"` + a
 * roving focus + arrows/Home/End/Escape), because its four children are
 * mutually exclusive actions. The two panels are disclosures — `aria-expanded`
 * + `aria-controls`, Tab-reachable in DOM order, no Escape-close, which is the
 * rule docs/09 pins for this page's disclosures. A target whose criteria are
 * unmet stays focusable with `aria-disabled` and its reason printed inline,
 * so a keyboard or screen-reader user meets the explanation rather than
 * finding a gap in the menu.
 */

const MENU_ID = "create-from-search-menu";
const PANEL_ID = "create-from-search-panel";

interface CreateFromSearchProps {
  /** The search as it is APPLIED right now — what the rows on screen match. */
  criteria: SearchCriteria;
  /** Label sources, so menus and rows read "Groceries", not "category 5". */
  sources: {
    categories: { id: number; name: string }[];
    accounts: { id: number; name: string }[];
    tags: { id: number; name: string }[];
  };
  /**
   * Filter-bar fields this feature does not carry (docs/09 lists the four it
   * does). Named in the save panel's copy when any are set, so "save" never
   * quietly drops a date range.
   */
  excludedFilters: string[];
  /** Every saved view; this control keeps only the ones restoring here. */
  views: SavedReportView[];
  /** Persist the whole list (M108's client-side list edit). Reject → toast. */
  onPersist: (next: SavedReportView[]) => Promise<void>;
  /** Apply a saved view: reset the filter state and navigate to its URL. */
  onRestore: (criteria: SearchCriteria) => void;
}

export function CreateFromSearch({
  criteria,
  sources,
  excludedFilters,
  views,
  onPersist,
  onRestore,
}: CreateFromSearchProps) {
  const [panel, setPanel] = useState<null | "menu" | "save" | "views">(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  // Bumped when ⌘K asks for the menu while it is already open, so focus still
  // lands on the first target instead of the request being swallowed.
  const [openNonce, setOpenNonce] = useState(0);

  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const criteriaText = criteriaLabel(criteria, sources);
  const forPage = views.filter((view) => view.tab === "transactions");

  const blockedReason = (target: CreateTargetId): string | undefined => {
    if (target !== "budget") return undefined;
    if (criteria.category === undefined) return "Set a category filter first";
    if (criteria.category === "uncategorized") return "Uncategorized cannot carry a budget";
    return undefined;
  };

  const targets: CreateTargetItem[] = [
    {
      id: "view",
      label: "Saved view",
      hint: "Keep this search as a bookmark",
      icon: Bookmark,
      onSelect: () => toggle("save"),
    },
    {
      id: "rule",
      label: "Rule",
      hint: "Categorize the payees this search matches",
      icon: GitBranch,
      blocked: blockedReason("rule"),
      href: targetHref("rule", criteria),
    },
    {
      id: "bill",
      label: "Bill",
      hint: "A recurring bill for this payee",
      icon: Receipt,
      blocked: blockedReason("bill"),
      href: targetHref("bill", criteria),
    },
    {
      id: "budget",
      label: "Budget",
      hint: "A monthly amount for this category",
      icon: Wallet,
      blocked: blockedReason("budget"),
      href: targetHref("budget", criteria),
    },
  ];

  function toggle(next: "menu" | "save" | "views"): void {
    setPanel((current) => (current === next ? null : next));
    if (next === "save") setTitle(criteriaText || "All transactions");
  }

  // The palette's "Create from this search" action: hand the open menu back to
  // this page, which is the only surface that OWNS the live search.
  useEffect(() => onCreateFromSearch(() => {
    setPanel("menu");
    setOpenNonce((value) => value + 1);
  }), []);

  // Click-outside closes. The trigger is inside the container, so its own
  // mousedown is ignored and the following click still toggles.
  useEffect(() => {
    if (panel === null) return;
    function onPointerDown(event: MouseEvent): void {
      if (!containerRef.current?.contains(event.target as Node)) setPanel(null);
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [panel]);

  async function persist(next: SavedReportView[]): Promise<boolean> {
    setBusy(true);
    try {
      await onPersist(next);
      return true;
    } catch {
      return false;
    } finally {
      setBusy(false);
    }
  }

  const saveHint =
    "Keeps the search term, category, account and tag filters. Saved to your profile, not to an account." +
    (excludedFilters.length > 0 ? ` Not saved: ${excludedFilters.join(", ")}.` : "");

  return (
    <div ref={containerRef} className="flex flex-wrap items-center gap-2">
      <Button
        ref={triggerRef}
        variant="outline"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={panel === "menu"}
        aria-controls={MENU_ID}
        onClick={() => toggle("menu")}
      >
        <Plus className="size-3.5" aria-hidden="true" />
        Create
        <ChevronDown className="size-3.5" aria-hidden="true" />
      </Button>
      <Button
        variant="outline"
        size="sm"
        aria-expanded={panel === "views"}
        aria-controls={PANEL_ID}
        onClick={() => toggle("views")}
      >
        <ListFilter className="size-3.5" aria-hidden="true" />
        Views{forPage.length > 0 ? ` (${forPage.length})` : ""}
      </Button>

      {panel === "menu" && (
        <div className="basis-full rounded-xl border border-control bg-surface-raised p-2">
          <p className="px-2 pb-1.5 pt-1 text-xs text-muted">
            This search: {criteriaText || "nothing filtered"}
          </p>
          <CreateTargetMenu
            id={MENU_ID}
            label="Create from this search"
            items={targets}
            focusNonce={openNonce}
            triggerRef={triggerRef}
            onClose={() => setPanel(null)}
          />
        </div>
      )}

      {panel === "save" && (
        <SaveViewPanel
          id={PANEL_ID}
          title={title}
          onTitleChange={setTitle}
          placeholder={criteriaText || "e.g. All transactions"}
          hint={saveHint}
          busy={busy}
          onSave={(trimmed) => {
            void persist([
              ...views,
              {
                id: newViewId(),
                title: trimmed,
                tab: "transactions",
                kind: "transactions",
                state: criteria,
              },
            ]).then((ok) => {
              if (ok) {
                setPanel(null);
                setTitle("");
              }
            });
          }}
          onCancel={() => setPanel(null)}
        />
      )}

      {panel === "views" && (
        <SavedViewListPanel
          id={PANEL_ID}
          views={forPage}
          describe={(view) => criteriaLabel(view.state, sources) || "No filters"}
          emptyHint={
            "No saved views yet — filter this search, then press “Create” and pick “Saved view”."
          }
          busy={busy}
          onRestore={(view) => {
            onRestore(view.state);
            setPanel(null);
          }}
          onRename={(view, next) =>
            persist(
              views.map((candidate) =>
                candidate.id === view.id ? { ...candidate, title: next } : candidate,
              ),
            )
          }
          onDelete={(view) => void persist(views.filter((candidate) => candidate.id !== view.id))}
        />
      )}
    </div>
  );
}
