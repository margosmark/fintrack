// Bulk actions toolbar for the ledger tables (docs/09 §Bulk selection toolbar).
//
// Placement contract: ONE element that is an inline header row on desktop
// (`md:static`, above the table) and a sticky bar pinned above the mobile
// bottom nav below `md`. The count is always rendered inside an `aria-live`
// region, so a screen-reader user hears "3 selected" the moment selection
// starts, without hunting for it.
//
// Execution contract — TWO paths, one outcome (M125, docs/09):
//
//   * PAGE (an explicit set of the ids in front of you): the EXISTING
//     single-row routes through `runSequential`, one row at a time with a
//     visible `n / total` counter — M96's contract, unchanged. 50 rows is 50
//     requests and that is deliberate (docs/09 §Actions v1).
//   * MATCHED ("every row the filter matches"): ONE request to
//     `/transactions/bulk/*` carrying `{filter, exclude_ids}`. 345 rows is one
//     request, because 345 authenticated requests on the surface an
//     import-heavy user hammers hardest is what this path exists to avoid.
//
// Both answer into the same `BulkOutcome`, so partial success — accepted rows
// land, refused rows listed per row with the server's own `{detail, code}` —
// is reported identically either way. Nothing is ever silently dropped.
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Category } from "../../api/categories";
import type { Tag } from "../../api/tags";
import {
  addTransactionTag,
  bulkAddTag,
  bulkDelete,
  bulkRecategorize,
  bulkSetPayee,
  bulkSetReviewed,
  deleteTransaction,
  setTransactionReviewed,
  updateTransaction,
  type Transaction,
  type TransactionFilters,
} from "../../api/transactions";
import { encryptPayee, encryptTransaction } from "../../lib/e2ee";
import { isUnlocked } from "../../lib/vault";
import {
  VaultLocked,
  VAULT_LOCK_COPY,
  VAULT_LOCK_DESTINATION,
} from "../e2ee/VaultLocked";
import { useEncryptionRequired, useVaultUnlocked } from "../e2ee/useUnlock";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import { CategoryPicker } from "./CategoryPicker";
import { CreateFromSelection } from "./CreateFromSelection";
import { apiErrorMessage } from "./format";
import {
  outcomeFromResult,
  runSequential,
  summarizeOutcome,
  type BulkAction,
  type BulkConflict,
  type BulkOutcome,
} from "./bulkActions";

const FIELD_CLASS =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

/**
 * M176 — the category menu's placement inside this bar.
 *
 * Below `md` the bar is `fixed` to the BOTTOM of the viewport, so a menu that
 * opens downward (`mt-1`, the picker's default) would render past the bottom
 * edge — present in the DOM, invisible to the user. It opens UPWARD there
 * (`bottom-full`) and keeps the downward placement once the bar is the inline
 * desktop row (`md:static`). The classes are responsive rather than a JS
 * measurement because the placement flips with the same breakpoint as the bar.
 */
const BULK_CATEGORY_MENU_CLASS =
  "absolute z-20 bottom-full mb-1 max-h-48 w-full overflow-y-auto rounded-lg border border-control bg-surface shadow-lg md:bottom-auto md:top-full md:mb-0 md:mt-1";

/**
 * Every bulk action ends by invalidating the WHOLE read surface that reads a
 * category rollup or a balance — `budgets` and `reports` included, because a
 * bulk recategorize moves `pct_used` (docs/08 §Budgets reads the same rows).
 */
const INVALIDATE_KEYS: readonly string[] = [
  "transactions",
  "accounts",
  "ledger",
  "reports",
  "budgets",
  "search",
  "suggestions",
  // M106: a flip writes a receipt, so the drawer's cache moves too.
  "transaction-events",
];

export interface BulkSettled {
  action: BulkAction;
  outcome: BulkOutcome;
}

/** The matched set, as the server needs to hear it (M125). */
export interface MatchedSelection {
  /** The page's own filter — the same object `GET /transactions` was called with. */
  filter: TransactionFilters;
  /** Rows the user unchecked out of the matched set. */
  excludeIds: readonly number[];
  /** The SERVER's `total` behind the counter the confirmation will print. */
  total: number;
}

export interface BulkActionsToolbarProps {
  /** Rows currently visible — labels for the conflict list. */
  rows: readonly Transaction[];
  /** Selection ∩ visible — the page path's action set (and its labels). */
  selectedIds: readonly number[];
  /** Non-null while the MATCHED set is marked; then `selectedIds` is unused. */
  matched?: MatchedSelection | null;
  /** The server's `total` for this filter, whether or not it is marked yet. */
  matchedTotal?: number | null;
  /**
   * Present only while a second tick WOULD reach past this page — the visible
   * half of the header checkbox's "Select all N matched" state (the header
   * itself carries the same state for keyboard and screen-reader users).
   */
  onSelectAllMatched?: () => void;
  categories: readonly Category[];
  tags: readonly Tag[];
  onClear: () => void;
  /** Called after each completed action (clear selection, fix pagination). */
  onSettled: (settled: BulkSettled) => void;
}

type Mode = "idle" | "recategorize" | "rename" | "tag";

interface ConflictState {
  action: BulkAction;
  outcome: BulkOutcome;
}

export function BulkActionsToolbar({
  rows,
  selectedIds,
  matched = null,
  matchedTotal = null,
  onSelectAllMatched,
  categories,
  tags,
  onClear,
  onSettled,
}: BulkActionsToolbarProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [mode, setMode] = useState<Mode>("idle");
  const [categoryId, setCategoryId] = useState("");
  const [payee, setPayee] = useState("");
  const [tagId, setTagId] = useState("");
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [conflicts, setConflicts] = useState<ConflictState | null>(null);
  // M176 — a category created in this bar, held locally so its name shows in
  // the picker the instant it exists. The `["categories"]` invalidation below
  // refetches the real list, but a refetch is a round trip and the control
  // would read blank until it lands; the local copy is merged over the prop
  // until the refetched list already contains the id.
  const [createdCategories, setCreatedCategories] = useState<readonly Category[]>([]);

  const categoryOptions = useMemo<Category[]>(() => {
    if (createdCategories.length === 0) return [...categories];
    const known = new Set(categories.map((c) => c.id));
    return [...categories, ...createdCategories.filter((c) => !known.has(c.id))];
  }, [categories, createdCategories]);

  const labels = useMemo(
    () => new Map<number, string>(rows.map((row) => [row.id, row.normalized_payee])),
    [rows],
  );
  // The counter's ONE definition: the matched set's SERVER count minus its
  // exceptions, or the visible page's selection. Never a client sum of a set
  // the client never enumerated (docs/09 §Select-all-matched).
  const count = matched !== null ? matched.total - matched.excludeIds.length : selectedIds.length;
  const busy = progress !== null;

  // M174 (docs/09 §Create from a selection): the rows the criteria are derived
  // from — the explicit id set intersected with what is on screen, the same set
  // every other page-path action touches (never an id that left the view).
  const selectedRows = useMemo(() => {
    const wanted = new Set(selectedIds);
    return rows.filter((row) => wanted.has(row.id));
  }, [rows, selectedIds]);

  // G2 (M134 §5.1 / M136 P0-3) — "locked vault destroys work".
  //
  // The MATCHED rename sends the payee half only ("the matched path cannot
  // [send a per-row bundle] — it never holds the rows", the comment in
  // `rename()`), and the PAGE path drops to payee-only the moment the vault is
  // locked. Under `encryption_required` BOTH are a guaranteed 422
  // (`backend/app/api/transactions.py:878`), so an encryption-enrolled tenant
  // lost every rename it queued. Gate BEFORE submit; the toast below is the
  // defense-in-depth branch and carries the canonical destination string.
  const vaultUnlocked = useVaultUnlocked();
  const cipherWriteBlocked = useEncryptionRequired() && !vaultUnlocked;

  function invalidate(): void {
    for (const key of INVALIDATE_KEYS) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }

  /** The M96 path — one request per row, in order, with a visible counter. */
  async function run(action: BulkAction, call: (id: number) => Promise<unknown>): Promise<void> {
    setProgress({ done: 0, total: count });
    const outcome = await runSequential(selectedIds, call, (done, total) =>
      setProgress({ done, total }),
    );
    setProgress(null);
    invalidate();
    report(action, outcome);
    setMode("idle");
    onSettled({ action, outcome });
  }

  /** The M125 path — ONE request over the matched set, reported identically. */
  async function runMatched(
    action: BulkAction,
    call: () => Promise<{
      matched: number;
      updated: number;
      skipped: Array<{ id: number; label: string; code: string | null; detail: string }>;
    }>,
  ): Promise<void> {
    if (matched === null) return;
    setProgress({ done: 0, total: count });
    try {
      const result = await call();
      setProgress(null);
      invalidate();
      const outcome = outcomeFromResult(result);
      report(action, outcome);
      setMode("idle");
      onSettled({ action, outcome });
    } catch (error) {
      // Nothing (or an unknown amount) happened — say so and let the caller's
      // refetch re-read the count. A `409 bulk_count_mismatch` in particular
      // means the set MOVED between the confirmation and the click, and the
      // selection is deliberately kept so the user can confirm the new number.
      setProgress(null);
      invalidate();
      setMode("idle");
      toast.error("Bulk action did not run", apiErrorMessage(error));
    }
  }

  function report(action: BulkAction, outcome: BulkOutcome): void {
    if (outcome.conflicts.length === 0) {
      toast.success(summarizeOutcome(action, outcome));
      setConflicts(null);
    } else {
      toast.error(summarizeOutcome(action, outcome), "Skipped rows are listed below.");
      setConflicts({ action, outcome });
    }
  }

  function recategorize(): void {
    const target = Number(categoryId);
    if (Number.isNaN(target)) return;
    if (matched !== null) {
      void runMatched("recategorize", () =>
        bulkRecategorize(matched.filter, matched.excludeIds, target),
      );
      return;
    }
    void run("recategorize", (id) => updateTransaction(id, { category_id: target }));
  }

  async function rename(): Promise<void> {
    const next = payee.trim();
    if (next === "") return;
    if (cipherWriteBlocked) {
      toast.error(
        "Payee not set — the vault is locked",
        `${VAULT_LOCK_COPY.write} ${VAULT_LOCK_DESTINATION}`,
      );
      return;
    }
    if (matched !== null) {
      // ONE ciphertext for the whole set: every row gets the same payee, and
      // the rows' own amount/raw-description ciphers are left untouched.
      const cipher = isUnlocked() ? await encryptPayee(next) : undefined;
      await runMatched("rename", () =>
        bulkSetPayee(matched.filter, matched.excludeIds, next, cipher),
      );
      return;
    }
    // E2EE (docs/33): dual-write the ciphertext for the new payee when the
    // vault is unlocked — same rule the single-row editor follows, and the
    // SAME per-row bundle M96 sent (the row's own amount/description ciphers
    // ride along so a row that predates the backfill still satisfies
    // `encryption_required`). The matched path cannot do that — it never holds
    // the rows — which is why it sends the payee half only.
    await run("rename", async (id) => {
      const row = rows.find((r) => r.id === id);
      if (!isUnlocked() || row === undefined) {
        return updateTransaction(id, { normalized_payee: next });
      }
      const bundle = await encryptTransaction({
        normalizedPayee: next,
        rawDescription: row.raw_description,
        amountNative: row.amount_native,
        cadAmount: row.cad_amount,
      });
      return updateTransaction(id, { normalized_payee: next, ...bundle });
    });
  }

  function flipReview(reviewed: boolean): void {
    const action: BulkAction = reviewed ? "review" : "unreview";
    if (matched !== null) {
      void runMatched(action, () => bulkSetReviewed(matched.filter, matched.excludeIds, reviewed));
      return;
    }
    void run(action, (id) => setTransactionReviewed(id, reviewed));
  }

  function attachTag(): void {
    const target = Number(tagId);
    if (Number.isNaN(target)) return;
    if (matched !== null) {
      void runMatched("tag", () => bulkAddTag(matched.filter, matched.excludeIds, target));
      return;
    }
    void run("tag", (id) => addTransactionTag(id, target));
  }

  /**
   * Count-first, and honest about the blast radius (docs/09 §Destructive
   * actions). The M95 undo toast does NOT cover a batch — there is no per-row
   * undo for a run — so the confirmation says so out loud. Single-row deletes
   * keep their own wording: ordinary operations are unchanged.
   */
  function remove(): void {
    const question = `Delete ${count} transactions? This cannot be undone via this batch.`;
    if (!window.confirm(question)) return;
    if (matched !== null) {
      // The number just confirmed travels with the request; the server
      // refuses to run if the filter no longer matches exactly that many.
      void runMatched("delete", () =>
        bulkDelete(matched.filter, matched.excludeIds, count),
      );
      return;
    }
    void run("delete", (id) => deleteTransaction(id));
  }

  const showToolbar = count > 0;

  return (
    <>
      {showToolbar && (
        <div className="fixed inset-x-0 bottom-[calc(3.5rem_+_env(safe-area-inset-bottom))] z-30 flex flex-wrap items-center gap-2 border-t border-veil/[0.08] bg-surface/95 px-3 py-2 backdrop-blur-xl md:static md:z-auto md:mb-3 md:rounded-xl md:border md:border-control md:bg-surface/80 md:px-3 md:py-2.5">
          <span
            aria-live="polite"
            aria-atomic="true"
            className="text-sm font-medium text-text tabular"
          >
            {count} selected
          </span>
          {/* The page is fully selected and the filter reaches further — say
              so where a person can see it. The header checkbox does the same
              job for keyboard and screen readers; one state, two doors. */}
          {onSelectAllMatched !== undefined &&
            matched === null &&
            rows.length > 0 &&
            selectedIds.length === rows.length &&
            matchedTotal !== null &&
            matchedTotal > rows.length && (
              <Button
                size="sm"
                variant="ghost"
                onClick={onSelectAllMatched}
                aria-label={`Select all ${matchedTotal} matched transactions`}
              >
                {`Select all ${matchedTotal} matched`}
              </Button>
            )}
          {matched !== null && (
            <span className="sr-only">
              {`all ${matched.total} transactions matching the current filters${
                matched.excludeIds.length > 0
                  ? `, minus ${matched.excludeIds.length} deselected`
                  : ""
              }`}
            </span>
          )}

          {mode === "idle" && (
            <>
              {/* M174 (docs/09 §Create from a selection): M123's menu with the
                  SELECTED rows as its criteria source. Only on the explicit-id
                  path — a MATCHED set carries no rows, and the page's own M123
                  Create menu already encodes the filter that defines it. */}
              {matched === null && count > 0 && (
                <CreateFromSelection rows={selectedRows} />
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  // A fresh session each time the mode opens: a stale
                  // selection from a previous pass must not look like the
                  // current one.
                  setCategoryId("");
                  setMode("recategorize");
                }}
              >
                Recategorize
              </Button>
              {/* G2: disabled WITH the reason, up front — the control that
                  cannot run says why it cannot, before anything is typed. */}
              {cipherWriteBlocked ? (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled
                    aria-describedby="bulk-rename-locked"
                  >
                    Set payee
                  </Button>
                  <VaultLocked
                    id="bulk-rename-locked"
                    variant="blocked"
                    capability="write:bulk-rename"
                  />
                </>
              ) : (
                <Button size="sm" variant="outline" onClick={() => setMode("rename")}>
                  Set payee
                </Button>
              )}
              {/* M106 (docs/09 §Bulk selection toolbar): review flips drive the
                  ONE single-row route, N times — no bulk schema, and N receipts. */}
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => flipReview(true)}
                aria-label={`Mark ${count} selected transactions reviewed`}
              >
                Mark reviewed
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => flipReview(false)}
                aria-label={`Mark ${count} selected transactions unreviewed`}
              >
                Mark unreviewed
              </Button>
              <Button size="sm" variant="outline" onClick={() => setMode("tag")}>
                Add tag
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={busy}
                onClick={remove}
                aria-label={`Delete ${count} selected transactions`}
              >
                Delete
              </Button>
              <Button size="sm" variant="ghost" onClick={onClear}>
                Clear
              </Button>
            </>
          )}

          {mode === "recategorize" && (
            <>
              {/* M176 (docs/09 §Recategorize — create in place): the mode's
                  control is the SHARED CategoryPicker — the same type-to-filter,
                  create-on-the-fly combo the single-row cells render — not a
                  native `<select>` that could only choose a name that already
                  existed. Creating here writes a CATEGORY and nothing else; the
                  only control that touches transactions is Apply, below. */}
              <CategoryPicker
                value={categoryId}
                categories={categoryOptions}
                ariaLabel="Category for the selected transactions"
                disabled={busy}
                menuClassName={BULK_CATEGORY_MENU_CLASS}
                onChange={setCategoryId}
                onCategoryCreated={(created) => {
                  setCreatedCategories((prev) =>
                    prev.some((c) => c.id === created.id) ? prev : [...prev, created],
                  );
                  void queryClient.invalidateQueries({ queryKey: ["categories"] });
                }}
              />
              <Button
                size="sm"
                variant="primary"
                // No target, no Apply: an empty id (including while a create is
                // in flight) cannot mean a category, and a run is already in
                // flight behind `busy`.
                disabled={categoryId === "" || Number.isNaN(Number(categoryId)) || busy}
                onClick={recategorize}
              >
                Apply
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setMode("idle")}>
                Cancel
              </Button>
            </>
          )}

          {mode === "rename" && (
            <>
              <label className="sr-only" htmlFor="bulk-payee">
                Payee for the selected transactions
              </label>
              <input
                id="bulk-payee"
                value={payee}
                onChange={(event) => setPayee(event.target.value)}
                placeholder="Common payee name"
                className={`${FIELD_CLASS} min-w-40`}
              />
              {/* Defense-in-depth: this mode is already unreachable while
                  the vault is locked (the "Set payee" entry button is gone),
                  so the Apply control simply cannot be pressed either. */}
              <Button
                size="sm"
                variant="primary"
                disabled={payee.trim() === "" || busy || cipherWriteBlocked}
                onClick={() => void rename()}
              >
                Apply
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setMode("idle")}>
                Cancel
              </Button>
            </>
          )}

          {mode === "tag" && (
            <>
              <label className="sr-only" htmlFor="bulk-tag">
                Tag to add to the selected transactions
              </label>
              <select
                id="bulk-tag"
                value={tagId}
                onChange={(event) => setTagId(event.target.value)}
                className={`${FIELD_CLASS} min-w-36`}
              >
                <option value="">Choose a tag…</option>
                {tags.map((tag) => (
                  <option key={tag.id} value={String(tag.id)}>
                    {tag.name}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="primary"
                disabled={tagId === "" || busy}
                onClick={attachTag}
              >
                Apply
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setMode("idle")}>
                Cancel
              </Button>
            </>
          )}

          {busy && progress !== null && (
            <span className="text-xs text-muted tabular" data-testid="bulk-progress">
              {/* A matched run is ONE request — there is no per-row counter to
                  tick, so it says what it is doing instead of lying about 0 / N. */}
              {matched !== null ? `Applying to ${count} transactions…` : `${progress.done} / ${progress.total}`}
            </span>
          )}
        </div>
      )}

      {conflicts !== null && (
        <div
          role="status"
          aria-live="polite"
          className="mt-3 rounded-xl border border-control border-l-2 border-l-danger bg-surface p-3"
        >
          <div className="flex items-start justify-between gap-3">
            <p className="text-sm text-text">
              <span className="font-medium">{summarizeOutcome(conflicts.action, conflicts.outcome)}</span>{" "}
              <span className="text-muted">
                — the skipped rows are still in your ledger, nothing was changed for them.
              </span>
            </p>
            <button
              type="button"
              aria-label="Dismiss skipped-rows notice"
              onClick={() => setConflicts(null)}
              className="rounded-lg p-1 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
            >
              ×
            </button>
          </div>
          <ul className="mt-2 grid gap-1 text-xs text-muted">
            {conflicts.outcome.conflicts.map((conflict: BulkConflict) => (
              <li key={conflict.id}>
                <span className="text-text">
                  {/* The page's own label first; a matched run may name a row
                      from a page the browser never loaded, and `#id` last. */}
                  {labels.get(conflict.id) ?? conflict.label ?? `#${conflict.id}`}
                </span>{" "}
                — {conflict.detail}
                {conflict.code !== null && (
                  <span className="ml-1 uppercase tracking-wider">({conflict.code})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
