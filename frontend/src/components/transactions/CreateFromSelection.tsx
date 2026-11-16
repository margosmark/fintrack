import { useEffect, useRef, useState } from "react";
import { ChevronUp, GitBranch, Plus, Receipt } from "lucide-react";
import {
  selectionCriteria,
  targetHref,
  type SelectionRow,
} from "../../lib/createFromSearch";
import { Button } from "../ui/Button";
import { CreateTargetMenu, type CreateTargetItem } from "./CreateTargetMenu";

/**
 * M174 (docs/09 §Create from a selection) — M123's create menu applied to the
 * rows the user TICKED instead of to the search.
 *
 * Mark's ask, in his words: *"I thought we could create a rule or a bill from a
 * search filter or even bulk actions"*. The search half already existed (M123);
 * on a phone the bulk toolbar is where a selection lives, so the same menu
 * appears here with a different criteria source:
 *
 *   search   →  `criteriaLabel(applied + searchQ)`, four targets
 *   selection →  `selectionCriteria(selectedRows)`, rule + bill
 *
 * One shared menu component (`CreateTargetMenu`), one shared deep link
 * (`targetHref` → `?prefill=transactions`), one shared detail-link reader in
 * the target editors — so a rule created from a selection and one created from
 * a search land in the SAME form with the SAME provenance line.
 *
 * Why rule + bill and not the search's four: a selection carries payees, not a
 * saved preset or a category total. "Saved view" records a filter, and there is
 * no filter here; "Budget" is a per-category amount, which a set of rows cannot
 * state. Offering either would be a control whose click cannot mean anything.
 *
 * A selection spanning more than one merchant is the one blocked case, and it
 * is blocked WITH the reason (docs/09's menu contract): the pattern is derived
 * from the shared `lib/payeeKey.ts` normalize, so a two-merchant selection has
 * no single pattern to carry — and inventing one would create a rule that
 * silently swallows unrelated payees.
 */
const MENU_ID = "create-from-selection-menu";

interface CreateFromSelectionProps {
  /** The rows the user ticked — the criteria source. */
  rows: readonly SelectionRow[];
}

function blockedReason(merchants: number, pattern: string | null): string | undefined {
  if (pattern !== null) return undefined;
  if (merchants === 0) return "The selected rows carry no payee to match on";
  if (merchants > 1) return `${merchants} payees selected — pick rows from one merchant`;
  return "The selected rows share no usable payee pattern";
}

export function CreateFromSelection({ rows }: CreateFromSelectionProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const state = selectionCriteria(rows);
  const blocked = blockedReason(state.merchants, state.pattern);
  const count = rows.length;

  // Click-outside closes; the trigger is inside the container, so its own
  // mousedown is ignored and the following click still toggles.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent): void {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const items: CreateTargetItem[] = [
    {
      id: "rule",
      label: `Rule from these ${count}`,
      hint: "Categorize the payees these rows match",
      icon: GitBranch,
      blocked,
      href: targetHref("rule", state.criteria),
    },
    {
      id: "bill",
      label: `Bill from these ${count}`,
      hint: "A recurring bill for this payee",
      icon: Receipt,
      blocked,
      href: targetHref("bill", state.criteria),
    },
  ];

  return (
    <div ref={containerRef} className="flex flex-wrap items-center gap-2">
      <Button
        ref={triggerRef}
        size="sm"
        variant="outline"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={MENU_ID}
        onClick={() => setOpen((current) => !current)}
      >
        <Plus className="size-3.5" aria-hidden="true" />
        Create from selection
        <ChevronUp className="size-3.5" aria-hidden="true" />
      </Button>

      {open && (
        <div className="basis-full rounded-xl border border-control bg-surface-raised p-2">
          <p className="px-2 pb-1.5 pt-1 text-xs text-muted">
            {state.pattern !== null
              ? `These rows: “${state.pattern}”`
              : `These rows: ${blocked}`}
          </p>
          <CreateTargetMenu
            id={MENU_ID}
            label="Create from the selected rows"
            items={items}
            triggerRef={triggerRef}
            onClose={() => setOpen(false)}
          />
        </div>
      )}
    </div>
  );
}
