// Transactions (docs/09): filterable, paginated table with inline category
// assignment (PATCH /transactions/{id}) and the suggestion queue side panel.
// All filters AND-compose server-side; pagination follows the docs/08 page
// shape {items,total,limit,offset} with the limit-50 default.
// Presentation pass: chip-style filter bar with clear-all, row stagger on
// filter change, sticky header, skeleton rows, toast on category change.
// Also ingests ?q / ?category / ?account URL params (global nav search
// targets) on mount AND on param change — navigating here while already
// mounted does not remount the page.

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ChangeEvent, FormEvent, ReactNode } from "react";
import { Link as RouterLink, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Check, Circle, History, Link, Paperclip, Plus, Repeat, SearchX } from "lucide-react";
import { useDialogFocus, useEscapeToClose } from "../components/ui/dialogA11y";
import {
  type Transaction,
  createTransaction,
  deleteTransaction,
  type TransactionCreate,
  type TransactionPatch,
  type TransactionFilters,
  type SplitChildInput,
  type SplitParentPublic,
  listTransactions,
  updateTransaction,
  createTransactionSplit,
  updateTransactionSplit,
  mergeTransactionSplit,
  detectTransferPair,
  getUnreviewedCount,
  linkTransfer,
  setTransactionReviewed,
  unlinkTransfer,
  setTransactionTags,
} from "../api/transactions";
import { listAccounts, INVESTMENT_ACCOUNT_TYPES } from "../api/accounts";
import type { Account } from "../api/accounts";
import { listCategories } from "../api/categories";
import { createTag, listTags } from "../api/tags";
import type { Tag, TagWithUsage } from "../api/tags";
import { updateMe } from "../api/users";
import { useAuth } from "../auth/AuthContext";
import { scopedKey } from "../lib/tenantStorage";
import type { SavedReportView, User } from "../types";
import { noteTagUsed } from "../lib/tagRecency";
import { fetchReceiptBlob, uploadReceipt } from "../api/receipts";
import { encodeReceiptImage, ReceiptImageError } from "../lib/receiptImage";
import { SuggestionQueue } from "../components/transactions/SuggestionQueue";
import { SplitEditor } from "../components/transactions/SplitEditor";
import {
  REGISTER_SPLIT_LAYOUT,
  SplitGroupRow,
  groupSplitRows,
} from "../components/transactions/SplitGroupRow";
import { TransactionEditor } from "../components/transactions/TransactionEditor";
import { CreateFromSearch } from "../components/transactions/CreateFromSearch";
import { BulkActionsToolbar } from "../components/transactions/BulkActionsToolbar";
import type { BulkSettled } from "../components/transactions/BulkActionsToolbar";
import { intersectSelected, useRowSelection } from "../components/transactions/useRowSelection";
import { CategoryPicker } from "../components/transactions/CategoryPicker";
import { TagFilterPicker } from "../components/transactions/TagPicker";
import { QuickTagPopover } from "../components/transactions/QuickTagPopover";
import type { QuickTagAnchor } from "../components/transactions/QuickTagPopover";
import { TagManagerModal } from "../components/transactions/TagManagerModal";
import { HistoryDrawer } from "../components/transactions/HistoryDrawer";
import { RecurringPromptCard } from "../components/transactions/RecurringPromptCard";
import { SimilarExamples } from "../components/transactions/SimilarExamples";
import type { SimilarAnchor } from "../components/transactions/SimilarExamples";
import { apiErrorMessage, formatMoney } from "../components/transactions/format";
import { Button } from "../components/ui/Button";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  demoLocks,
  useDemoRefusal,
} from "../components/ui/DemoLock";
import {
  useResolvedAccounts,
  useResolvedTransactions,
} from "../components/e2ee/useResolved";
import { encryptTransaction } from "../lib/e2ee";
import { isUnlocked } from "../lib/vault";
import { VaultLocked } from "../components/e2ee/VaultLocked";
import { useVaultUnlocked } from "../components/e2ee/useUnlock";
import {
  criteriaFromParams,
  criteriaToParams,
  type SearchCriteria,
} from "../lib/createFromSearch";
import { CADENCE_LABELS, recurringBadgeTitle } from "../lib/recurring";
import { RangePicker } from "../components/ui/RangePicker";
import { EmptyState } from "../components/ui/EmptyState";
import { QueryErrorPanel } from "../components/ui/QueryErrorPanel";
import { SkeletonRows } from "../components/ui/Skeleton";
import { TableScroller } from "../components/ui/TableScroller";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { deferDelete } from "../components/ui/deferDelete";
import { useToast } from "../components/ui/Toast";
import { useMasked } from "../lib/mask";

const PAGE_SIZE = 50;

/**
 * M106 (docs/09 §Review queue): the "N unreviewed" hint is dismissible for
 * the SESSION — come back tomorrow with work still to do and it is here
 * again, but it never follows you around once you have waved it off.
 * The key is tenant-scoped (F-6): one tenant's dismissal must not swallow
 * another tenant's hint in the same browser (see `lib/tenantStorage.ts`).
 */
const HINT_DISMISS_KEY = "fintrack.m106.unreviewed-hint.dismissed";

/**
 * `aria-describedby` target of EVERY row-write padlock on this page (docs/47
 * §4): review toggle, category, transfer link/unlink, delete. One VISIBLE
 * reason element serves them all — an IDREF list may be shared, and a reason
 * per row would print the same sentence 50 times.
 */
const LEDGER_ROW_REASON_ID = "ledger-row-write-demo-reason";

/** `aria-describedby` target of the link modal's candidate padlocks. */
const LINK_CANDIDATE_REASON_ID = "link-candidate-write-demo-reason";

/** Raw string state of the filter inputs before they are applied. */
interface FilterDraft {
  accountId: string;
  categoryId: string; // "" = any, "uncategorized" = category_id null
  /** M104: multi-tag filter — ids are AND-composed server-side (docs/09 §Tag
   *  UX: the picker says "matches all selected", so the list does too). */
  tagIds: string[];
  from: string;
  to: string;
  payee: string;
  amountMin: string;
  amountMax: string;
  direction: string; // "" = all, "paid" | "received"
  /** M106: "" = all, "false" = the "Needs review" queue, "true" = reviewed. */
  reviewed: string;
}

const EMPTY_DRAFT: FilterDraft = {
  accountId: "",
  categoryId: "",
  tagIds: [],
  from: "",
  to: "",
  payee: "",
  amountMin: "",
  amountMax: "",
  direction: "",
  reviewed: "",
};

function draftToFilters(draft: FilterDraft): TransactionFilters {
  const filters: TransactionFilters = {};
  if (draft.accountId !== "") filters.accountId = Number(draft.accountId);
  if (draft.categoryId === "uncategorized") {
    filters.categoryId = "uncategorized";
  } else if (draft.categoryId !== "") {
    filters.categoryId = Number(draft.categoryId);
  }
  if (draft.tagIds.length > 0) filters.tagIds = draft.tagIds.map(Number);
  if (draft.from !== "") filters.from = draft.from;
  if (draft.to !== "") filters.to = draft.to;
  if (draft.payee.trim() !== "") filters.payee = draft.payee.trim();
  if (draft.amountMin !== "" && !Number.isNaN(Number(draft.amountMin))) {
    filters.amountMin = Number(draft.amountMin);
  }
  if (draft.amountMax !== "" && !Number.isNaN(Number(draft.amountMax))) {
    filters.amountMax = Number(draft.amountMax);
  }
  if (draft.direction !== "") {
    filters.direction = draft.direction as TransactionFilters["direction"];
  }
  // M106 (docs/09 §Review queue): the chip is just another AND-composed
  // filter, so it rides the same draft → applied → query path as every other
  // control on this bar.
  if (draft.reviewed !== "") filters.reviewed = draft.reviewed === "true";
  return filters;
}

const INPUT_CLASS =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

/**
 * M123 (docs/09 §Create from search): the four URL-addressable params a filter
 * state implies. Note what is NOT here — from/to, payee, amount, direction and
 * the review chip are bar state, not URL state, and the save panel says so
 * rather than dropping them silently.
 */
function criteriaOf(state: FilterDraft, q: string): SearchCriteria {
  return {
    ...(q.trim() !== "" ? { q: q.trim() } : {}),
    ...(state.categoryId !== "" ? { category: state.categoryId } : {}),
    ...(state.accountId !== "" ? { account: state.accountId } : {}),
    ...(state.tagIds.length > 0 ? { tag: state.tagIds.join(",") } : {}),
  };
}

function LinkModal({
  source,
  candidates,
  accountNames,
  onLink,
  onClose,
  isPending,
  locked = false,
}: {
  source: Transaction;
  transfersCategoryId: number | null;
  candidates: Transaction[];
  accountNames: Map<number, string>;
    onLink: (pairId: number) => void;
  onClose: () => void;
  isPending: boolean;
  /** docs/47 §4: a candidate click IS `POST /transactions/{id}/link-transfer`. */
  locked?: boolean;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(dialogRef);
  useEscapeToClose(onClose);
  return createPortal(
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 flex items-center justify-center bg-scrim outline-none"
      role="dialog"
      aria-modal="true"
      aria-labelledby="link-modal-title"
    >
      <div className="glass w-full max-w-md rounded-xl p-5">
        <h2 id="link-modal-title" className="text-lg font-semibold">Link transfer pair</h2>
        <p className="mt-1 text-sm text-muted">
          Select the matching transaction for{" "}
          <span className="font-medium text-text">
            {formatMoney(source.amount_native, source.currency)}
          </span>{" "}
          on {accountNames.get(source.account_id) ?? `#${source.account_id}`}.
        </p>
        {candidates.length === 0 ? (
          <p className="mt-4 text-sm text-muted">
            No unmatched transfers with the opposite amount found on the current page.
            Try auto-detect or narrow your filters.
          </p>
        ) : (
          <ul className="mt-4 grid max-h-64 gap-1.5 overflow-y-auto">
            {candidates.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  disabled={isPending}
                  aria-describedby={locked ? LINK_CANDIDATE_REASON_ID : undefined}
                  onClick={() => onLink(c.id)}
                  className="w-full rounded-lg border border-control px-3 py-2 text-left text-sm transition-colors hover:border-accent/70 hover:bg-veil/[0.04] disabled:opacity-50"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1 font-medium">
                      {locked && <DemoLock />}
                      {c.normalized_payee}
                    </span>
                    <span
                      className={`tabular ${c.amount_native.startsWith("-") ? "text-danger" : ""}`}
                    >
                      {formatMoney(c.amount_native, c.currency)}
                    </span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                    <span>{c.date}</span>
                    <span>·</span>
                    <span>{accountNames.get(c.account_id) ?? `#${c.account_id}`}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
        {/* docs/47 §4 (F-4): the visible reason every candidate's padlock
            binds to — same modal, non-zero box, never sr-only. */}
        {locked && (
          <DemoReason id={LINK_CANDIDATE_REASON_ID}>
            The demo account is read-only — the pair will not be linked.
          </DemoReason>
        )}
        <div className="mt-4 flex justify-end">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function Transactions() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  // M123 (docs/09 §Create from search): the saved views live on the user
  // object, exactly like M108's — one column, whole-list PATCH.
  const { user, setUser } = useAuth();
  const savedViews = user?.saved_report_views ?? [];
  // F-6: the hint's dismissal flag is tenant-scoped (lib/tenantStorage.ts).
  const hintDismissKey = scopedKey(HINT_DISMISS_KEY, user);
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal for every write
  // this page dispatches — a refused demo row mutation is one local
  // `role="status"` element + one `toast.info`, never a `role="alert"` toast
  // (Toast renders an error tone AS `role="alert"`).
  const demo = useDemoRefusal();
  /** The §4 verdict for a row-scoped control (a `.map()` cannot call hooks). */
  const writeLocked = (method: string, path: string): boolean =>
    demoLocks(user, method, path);
  // The ONE verdict every row-level write shares: they are all
  // `POST|PATCH|DELETE /transactions…`, and no transaction path is in
  // `deps.py`'s exemption sets — the `{id}` segment is only there to show the
  // shape the check runs against; the verdict does not depend on its value.
  const rowWritesLocked = writeLocked("PATCH", "/transactions/{id}");

  const [draft, setDraft] = useState<FilterDraft>(EMPTY_DRAFT);
  const [applied, setApplied] = useState<FilterDraft>(EMPTY_DRAFT);
  const [offset, setOffset] = useState(0);

  const [searchParams, setSearchParams] = useSearchParams();
  const [similarAnchor, setSimilarAnchor] = useState<SimilarAnchor | null>(null);
  /** Fuzzy search view (M12): /transactions?q=… lands here relevance-ordered. */
  const [searchQ, setSearchQ] = useState<string>("");

  const [editorOpen, setEditorOpen] = useState(false);
  const [editorTarget, setEditorTarget] = useState<Transaction | null>(null);
  /** M104: the tag management modal, opened from the filter bar's picker. */
  const [tagManagerOpen, setTagManagerOpen] = useState(false);
  /** M104: the row-level quick-tag popover — the row plus its trigger rect. */
  const [quickTag, setQuickTag] = useState<{
    transaction: Transaction;
    anchor: QuickTagAnchor;
  } | null>(null);
  /** M106: the History drawer — the one row whose receipts are on screen. */
  const [historyTx, setHistoryTx] = useState<Transaction | null>(null);
  // --- M107 (docs/44 §12) ----------------------------------------------------
  /** The split group whose parts panel is open (at most one, like the drawer). */
  const [expandedSplit, setExpandedSplit] = useState<number | null>(null);
  /** The split editor — the parent row, plus children when RE-splitting. */
  const [splitEditor, setSplitEditor] = useState<{
    parent: Transaction;
    children?: Transaction[];
  } | null>(null);
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal element for the
  // whole page, rendered in exactly ONE place — inside the dialog that is open
  // (where the error line used to be), otherwise at the top of the table. The
  // two never stack, so a refused write is always exactly one `demo-refusal`.
  const refusalNode =
    demo.refusal !== null ? (
      <DemoRefusal id={demo.refusalId} action={demo.refusal} />
    ) : null;
  const refusalPlace = editorOpen ? "editor" : splitEditor !== null ? "split" : "page";
  // --- M108 P4 (docs/09 §Receipt images) ------------------------------------
  // ONE hidden file input for the whole page: a `<input type=file>` per row
  // would put a focusable control on every line of a 200-row table. Arming it
  // remembers which row asked, so the change handler knows what to upload.
  const receiptInputRef = useRef<HTMLInputElement>(null);
  const [receiptTarget, setReceiptTarget] = useState<Transaction | null>(null);
  /** M106: the header hint, remembered for this tab only. */
  const [hintDismissed, setHintDismissed] = useState(
    () => sessionStorage.getItem(hintDismissKey) === "1",
  );

  function invalidateLedgerData(): void {
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    void queryClient.invalidateQueries({ queryKey: ["reports"] });
    // Budget progress is a category rollup (docs/08 §Budgets reads the same
    // rows the browser holds) — a category change must move `pct_used`.
    void queryClient.invalidateQueries({ queryKey: ["budgets"] });
    void queryClient.invalidateQueries({ queryKey: ["search"] });
    void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
  }

  /** A category edit touches nothing but the category rollups. */
  function invalidateCategoryRollups(): void {
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
    void queryClient.invalidateQueries({ queryKey: ["budgets"] });
    void queryClient.invalidateQueries({ queryKey: ["reports"] });
  }

  function openEditor(target: Transaction | null): void {
    save.reset();
    setEditorTarget(target);
    setEditorOpen(true);
  }

  // M185 (docs/47 §1.2): the phone tab bar's centre `Add` is a REAL control —
  // it hands this page `?new=1` and the existing create editor opens over the
  // current route (the deviation path the spec allows when the editor cannot
  // mount globally; the control is never a no-op). Consumed with `replace`, so
  // Back/refresh never resurrects a form the user closed.
  useEffect(() => {
    if (searchParams.get("new") !== "1") return;
    openEditor(null);
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    setSearchParams(next, { replace: true });
  }, [searchParams]);

  const save = useMutation({
    mutationFn: async ({
      target,
      payload,
    }: {
      target: Transaction | null;
      payload: TransactionCreate | TransactionPatch;
    }) => {
      // E2EE Phase 2 (docs/33): dual-write ciphertext when unlocked so the
      // server always has the opaque bundle; plaintext stays for server-side
      // features until Phase 3 moves compute client-side.
      let body = payload;
      if (isUnlocked()) {
        const payee =
          payload.normalized_payee ?? target?.normalized_payee ?? payload.raw_description;
        const raw = payload.raw_description ?? target?.raw_description ?? "";
        const amount =
          payload.amount_native ?? target?.amount_native ?? null;
        if (payee !== undefined && raw !== undefined && amount != null) {
          const bundle = await encryptTransaction({
            normalizedPayee: payee,
            rawDescription: raw,
            amountNative: amount,
            cadAmount: target?.cad_amount ?? null,
          });
          body = { ...payload, ...bundle };
        }
      }
      return target
        ? updateTransaction(target.id, body)
        : createTransaction(body as TransactionCreate);
    },
    onSuccess: (_data, variables) => {
      invalidateLedgerData();
      setEditorOpen(false);
      setEditorTarget(null);
      toast.success(variables.target ? "Transaction updated" : "Transaction added");
    },
    onError: (err) => {
      // docs/47 §4: the editor owns this refusal (it renders the one
      // `demo-refusal` + `toast.info`). The host only skips its own error
      // path — an error toast renders `role="alert"`.
      if (demo.onError("Save transaction", err)) return;
      toast.error("Transaction save failed", apiErrorMessage(err));
    },
  });

  // M106 (docs/02 §transaction_events): the row toggle. One receipt per real
  // flip — asking for the state a row already has writes nothing, so hammering
  // the button does not bury the log.
  const review = useMutation({
    mutationFn: ({ id, reviewed }: { id: number; reviewed: boolean }) =>
      setTransactionReviewed(id, reviewed),
    onSuccess: (_data, variables) => {
      // The count rides the `transactions` family (see the `unreviewed`
      // query's key), so invalidating the ledger moves the rows AND the
      // "N unreviewed" surfaces together — one flip, one number.
      invalidateLedgerData();
      void queryClient.invalidateQueries({ queryKey: ["transaction-events"] });
      toast.success(variables.reviewed ? "Marked reviewed" : "Marked unreviewed");
    },
    onError: (err) => {
      if (demo.onError("Review change", err)) return;
      toast.error("Review change failed", apiErrorMessage(err));
    },
  });

  // --- M107 (docs/44 §4) ----------------------------------------------------
  // Create / re-split. The SERVER's envelope is rendered by the editor itself
  // (`detail` verbatim + `code`), so a refused sum never becomes a generic
  // toast (docs/44 §12) — that is why this mutation has no onError.
  const splitSave = useMutation({
    mutationFn: ({
      parent,
      children,
      replace,
    }: {
      parent: Transaction;
      children: SplitChildInput[];
      replace: boolean;
    }) =>
      replace
        ? updateTransactionSplit(parent.id, children)
        : createTransactionSplit(parent.id, children),
    onSuccess: () => {
      invalidateLedgerData();
      setExpandedSplit(null);
      setSplitEditor(null);
      toast.success("Split saved");
    },
    onError: (err) => {
      // docs/44 §12 still holds for every OTHER envelope: SplitEditor renders
      // the server's detail verbatim, so there is no generic toast here. The
      // ONE case that changes shape is a demo refusal (docs/47 §4): it becomes
      // the page's `demo-refusal` + one `toast.info`, never an error.
      demo.onError("Save split", err);
    },
  });

  /** Merge back — one DELETE, then every ledger cache re-reads (docs/44 §5). */
  const splitMerge = useMutation({
    mutationFn: (parentId: number) => mergeTransactionSplit(parentId),
    onSuccess: () => {
      invalidateLedgerData();
      setExpandedSplit(null);
      toast.success("Split merged back");
    },
    onError: (err) => {
      if (demo.onError("Merge split", err)) return;
      toast.error("Merge failed", apiErrorMessage(err));
    },
  });

  // Global nav search and the popover's "See all" land on
  // /transactions?q=…&category=…&account=…&tag=…. Apply on mount AND on param
  // change (in-page navigation does not remount); invalid numbers are
  // ignored. `q` drives the fuzzy search view, NOT the payee filter.
  useEffect(() => {
    // One parser for the four criteria — the same one the saved views and the
    // `?prefill=` deep links use (docs/09 §Create from search). A malformed
    // value is simply not part of the criteria rather than a NaN filter.
    const criteria = criteriaFromParams(searchParams);
    setSearchQ(criteria.q ?? "");
    setOffset(0);
    // Present params apply; ABSENT ones are left alone, so an inbound link that
    // carries only `q` does not quietly wipe the filter you already had.
    if (
      criteria.category === undefined &&
      criteria.account === undefined &&
      criteria.tag === undefined
    ) {
      return;
    }
    const patch = {
      ...(criteria.category !== undefined ? { categoryId: criteria.category } : {}),
      ...(criteria.account !== undefined ? { accountId: criteria.account } : {}),
      // M104: `?tag=` accepts one id or a comma list — both mean "carries ALL".
      ...(criteria.tag !== undefined ? { tagIds: criteria.tag.split(",") } : {}),
    };
    setDraft((prev) => ({ ...prev, ...patch }));
    setApplied((prev) => ({ ...prev, ...patch }));
    setOffset(0);
  }, [searchParams]);

  function clearSearchQ(): void {
    const next = new URLSearchParams(searchParams);
    next.delete("q");
    const qs = next.toString();
    setSearchParams(qs ? `?${qs}` : "");
  }

  /**
   * M123 (docs/09 §Create from search): the address bar tracks the four
   * criteria whenever `applied` moves, so a reload — and therefore a saved
   * view's bookmark — shows exactly what is on screen. `replace`: a filter
   * change is a state edit, not a history entry.
   */
  function syncCriteria(criteria: SearchCriteria): void {
    setSearchParams(criteriaToParams(criteria, searchParams), { replace: true });
  }

  /**
   * Applying a saved view is a NAVIGATION: the four params become the URL (the
   * view IS a bookmark) and the filter state is set directly as well, because
   * the inbound effect only ever OVERWRITES params that are present — a view
   * that does not carry `account` must clear it here or it would survive.
   */
  function restoreView(criteria: SearchCriteria): void {
    const next: FilterDraft = {
      ...EMPTY_DRAFT,
      categoryId: criteria.category ?? "",
      accountId: criteria.account ?? "",
      tagIds: criteria.tag !== undefined ? criteria.tag.split(",") : [],
    };
    setDraft(next);
    setApplied(next);
    setSearchQ(criteria.q ?? "");
    setOffset(0);
    setSearchParams(criteriaToParams(criteria));
    toast.success("View restored", criteria.q ? `“${criteria.q}”` : "filters applied");
  }

  /**
   * M108's client-side list edit: rename and delete are edits on the WHOLE
   * list, so there is one write shape and no per-preset CRUD.
   */
  async function persistViews(next: SavedReportView[]): Promise<void> {
    try {
      const updated: User = await updateMe({ saved_report_views: next });
      setUser(updated);
    } catch (err) {
      if (!demo.onError("Save view", err)) {
        toast.error("Could not save that view", apiErrorMessage(err));
      }
      throw err;
    }
  }

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(200),
  });
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(200),
  });
  const tags = useQuery({
    queryKey: ["tags"],
    queryFn: () => listTags(200),
  });

  /** M104: the multi-tag filter is draft state as ids (like every other
   *  control in the bar) resolved to tags for the chips + picker. */
  const selectedFilterTags = useMemo(() => {
    const all = tags.data?.items ?? [];
    return draft.tagIds
      .map((id) => all.find((tag) => String(tag.id) === id))
      .filter((tag): tag is TagWithUsage => tag !== undefined);
  }, [draft.tagIds, tags.data]);

  function toggleFilterTag(tag: Tag): void {
    setDraft((prev) =>
      prev.tagIds.includes(String(tag.id))
        ? prev
        : { ...prev, tagIds: [...prev.tagIds, String(tag.id)] },
    );
  }

  function removeFilterTag(tag: Tag): void {
    setDraft((prev) => ({
      ...prev,
      tagIds: prev.tagIds.filter((id) => id !== String(tag.id)),
    }));
  }

  const filters = useMemo(() => draftToFilters(applied), [applied]);
  // M123: the CURRENT search as the four URL params — the same shape the saved
  // view stores, the restore URL carries and `?prefill=` deep-links. Derived
  // from `applied` (what the rows match), never from `draft` (what is typed but
  // not yet applied), so the menu never offers a search the screen is not in.
  const currentCriteria = criteriaOf(applied, searchQ);
  /** Fields this feature does not carry — named in the save panel's copy. */
  const excludedFilters = [
    applied.from !== "" || applied.to !== "" ? "date range" : null,
    applied.payee.trim() !== "" ? "payee" : null,
    applied.amountMin !== "" || applied.amountMax !== "" ? "amount" : null,
    applied.direction !== "" ? "direction" : null,
    applied.reviewed !== "" ? "review state" : null,
  ].filter((label): label is string => label !== null);

  // M106 (docs/09 §Review queue): ONE number behind the header hint AND the
  // queue chip, over the same scope the list runs with — the endpoint ignores
  // its own `reviewed` filter so the two surfaces cannot drift.
  //
  // The key deliberately sits INSIDE the `transactions` family (alongside
  // `["transactions", "list"]`, `["transactions", "split"]`,
  // `["transactions", "all-rows"]`): every code path that invalidates the
  // ledger — the row toggle, the bulk toolbar, an inline edit, a delete, an
  // import commit — therefore refetches this count as well. A count keyed on
  // its own is a count that goes stale the moment the ROWS move: the header
  // hint would keep printing the pre-flip number while the table in front of
  // the user already disagrees with it.
  const unreviewed = useQuery({
    queryKey: ["transactions", "unreviewed-count", JSON.stringify(filters)],
    queryFn: () => getUnreviewedCount(filters),
  });
  const unreviewedCount = unreviewed.data ?? 0;
  // M137 (docs/33 §Server-first computing): `q` is a first-class filter again
  // — the list query below carries it, the server thresholds (0.3) AND ranks
  // (`score desc, id desc`), and the SAME predicate backs the chip count and
  // a bulk run over the matched set. The browser never downloads the ledger
  // to re-derive a page the server already computed; `lib/searchMatch.ts`
  // stays as the E2EE/offline fallback (P2-5 retires it).
  const vaultUnlocked = useVaultUnlocked();
  const page = useQuery({
    // The search term is part of the key: flipping `q` must never paint the
    // previous term's rows while the new page loads.
    //
    // M156 (docs/33 §Server-first rule 7c, M135 §3.4-5): the ANSWER depends on
    // the lock state — a locked search sends `hide_encrypted` — so the lock
    // state has to be in the key too. Without it the A18 notice's own inline
    // unlock re-rendered the view against the LOCKED page still in the cache
    // and the withheld rows never came back until a reload, which is exactly
    // the rule-7d violation "a remedy named in the copy must actually restore
    // the feature at that moment". AppShell's ⌘K query has carried the same
    // key element since M136; this one was missed.
    queryKey: ["transactions", "list", applied, searchQ, offset, vaultUnlocked],
    queryFn: () =>
      listTransactions(
        searchQ === "" ? filters : { ...filters, q: searchQ },
        { limit: PAGE_SIZE, offset },
        // A locked vault asks for only the rows it could display, and the
        // page reports `withheld` — the measured count the M136 `partial`
        // copy names. The REGISTER (searchQ === "") never hides: a locked
        // row renders its 🔒 marker in place (docs/33 §Mixed-mode display).
        { hideEncrypted: searchQ !== "" && !vaultUnlocked },
      ),
  });

  const patch = useMutation({
    mutationFn: ({ id, category_id }: { id: number; category_id: number | null }) =>
      updateTransaction(id, { category_id }),
    onSuccess: (_data, variables) => {
      invalidateCategoryRollups();
      const label =
        variables.category_id === null
          ? "Category cleared"
          : `Category set to ${
              (categories.data?.items ?? []).find((c) => c.id === variables.category_id)?.name ??
              `#${variables.category_id}`
            }`;
      toast.success(label);
    },
    onError: (err) => {
      if (demo.onError("Update category", err)) return;
      toast.error("Category update failed", apiErrorMessage(err));
    },
  });


  // M104 (docs/09 §Tag UX): quick-tag from the row. One PUT of the row's
  // whole tag set (the same route the editor uses), then the ledger caches
  // refresh so the chip appears without opening anything.
  const quickTagAttach = useMutation({
    mutationFn: ({ transaction, tagId }: { transaction: Transaction; tagId: number }) =>
      setTransactionTags(transaction.id, [
        ...(transaction.tags ?? []).map((tag) => tag.id),
        tagId,
      ]),
    onSuccess: (_data, variables) => {
      noteTagUsed(variables.tagId);
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["search"] });
      setQuickTag(null);
      toast.success("Tag added");
    },
    onError: (err) => {
      if (demo.onError("Add tag", err)) return;
      toast.error("Could not add tag", apiErrorMessage(err));
    },
  });

  const quickTagCreate = useMutation({
    mutationFn: async ({ transaction, name }: { transaction: Transaction; name: string }) => {
      const tag = await createTag({ name });
      await setTransactionTags(transaction.id, [
        ...(transaction.tags ?? []).map((t) => t.id),
        tag.id,
      ]);
      return tag;
    },
    onSuccess: (tag) => {
      noteTagUsed(tag.id);
      void queryClient.invalidateQueries({ queryKey: ["tags"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["search"] });
      setQuickTag(null);
      toast.success("Tag created", tag.name);
    },
    onError: (err) => {
      if (demo.onError("Create tag", err)) return;
      toast.error("Could not create tag", apiErrorMessage(err));
    },
  });

  const transfersCategoryId = useMemo(
    () =>
      (categories.data?.items ?? []).find(
        (c) => c.name === "transfers" && c.is_system,
      )?.id ?? null,
    [categories.data],
  );

  const [linkModalTxn, setLinkModalTxn] = useState<Transaction | null>(null);

  // --- M108 P4 (docs/09 §Receipt images) ------------------------------------
  // The chip in the row is the ONLY thing that changes on success: it comes
  // from `Transaction.has_receipt`, which the list refetch re-reads. The
  // bytes are fetched only when someone actually asks to see them.
  const receiptUpload = useMutation({
    mutationFn: async ({ transaction, file }: { transaction: Transaction; file: File }) => {
      // Re-encoded in the browser first, so the server's 2 MB cap is never
      // the thing that tells a person their photo did not fit.
      const encoded = await encodeReceiptImage(file);
      return uploadReceipt(transaction.id, encoded.blob, encoded.filename);
    },
    onSuccess: () => {
      void invalidateLedgerData();
      toast.success("Image attached");
    },
    onError: (error: unknown) => {
      if (demo.onError("Attach image", error)) return;
      toast.error(
        "Could not attach that image",
        error instanceof ReceiptImageError ? error.message : apiErrorMessage(error),
      );
    },
    onSettled: () => setReceiptTarget(null),
  });

  const viewReceipt = useMutation({
    mutationFn: fetchReceiptBlob,
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      // An anchor click rather than `window.open`: the response lands after
      // the click gesture has finished, and popups opened then are blocked.
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
    onError: (error: unknown) => toast.error("Could not open that image", apiErrorMessage(error)),
  });

  /** One shared picker: remember the row, then let the input do its job. */
  const armReceiptUpload = (transaction: Transaction) => {
    setReceiptTarget(transaction);
    receiptInputRef.current?.click();
  };

  const onReceiptFilePicked = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Clear first: re-picking the SAME file must still fire `change`.
    event.target.value = "";
    if (file === undefined || receiptTarget === null) return;
    receiptUpload.mutate({ transaction: receiptTarget, file });
  };

  const detectTransfer = useMutation({
    mutationFn: (id: number) => detectTransferPair(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      toast.success("Transfer pair linked");
    },
    onError: (err) => {
      if (demo.onError("Auto-detect transfer", err)) return;
      toast.error("Auto-detect failed", apiErrorMessage(err));
    },
  });

  const linkTransferMutation = useMutation({
    mutationFn: ({ txnId, pairId }: { txnId: number; pairId: number }) =>
      linkTransfer(txnId, pairId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      setLinkModalTxn(null);
      toast.success("Transfer pair linked");
    },
    onError: (err) => {
      if (demo.onError("Link transfer", err)) return;
      toast.error("Link failed", apiErrorMessage(err));
    },
  });

  const unlinkTransferMutation = useMutation({
    mutationFn: (id: number) => unlinkTransfer(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      toast.success("Transfer pair unlinked");
    },
    onError: (err) => {
      if (demo.onError("Unlink transfer", err)) return;
      toast.error("Unlink failed", apiErrorMessage(err));
    },
  });
  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApplied(draft);
    setOffset(0);
    syncCriteria(criteriaOf(draft, searchQ));
  }

  /** Chip bar clear-all: wipes draft and applied state, resets pagination. */
  function clearAllFilters() {
    setDraft(EMPTY_DRAFT);
    setApplied(EMPTY_DRAFT);
    setOffset(0);
    // The fuzzy search has its own clear, so `q` rides along rather than
    // being wiped by a button that never touched it.
    syncCriteria(criteriaOf(EMPTY_DRAFT, searchQ));
  }

  /**
   * M106 (docs/09 §Review queue): the queue chip is a SHORTCUT, so it applies
   * the moment it is pressed instead of waiting for "Apply filters" — and it
   * only moves its own key, so a half-typed filter beside it is not silently
   * submitted along for the ride.
   */
  function toggleReviewQueue(): void {
    const next = applied.reviewed === "false" ? "" : "false";
    setDraft((prev) => ({ ...prev, reviewed: next }));
    setApplied((prev) => ({ ...prev, reviewed: next }));
    setOffset(0);
  }

  // E2EE Phase 2 (docs/33): decrypt-on-display for rows carrying ciphertext.
  // One payload serves both modes now (M137): the server ranks search mode,
  // so the register page and the search page are the SAME envelope.
  // Split PARENTS ride the same payload but are not in `items`, so they join
  // the resolution pass — a group header decrypts its payee exactly like the
  // rows around it (and shows 🔒 in a locked vault).
  const resolveOver = useMemo(
    () => [...(page.data?.items ?? []), ...(page.data?.split_parents ?? [])],
    [page.data],
  );
  const resolvedTx = useResolvedTransactions(resolveOver);
  const acctItems = useMemo(() => accounts.data?.items ?? [], [accounts.data]);
  const resolvedAccounts = useResolvedAccounts(acctItems);
  const accountNames = useMemo(
    () =>
      new Map<number, string>(
        (acctItems).map((account: Account) => [
          account.id,
          resolvedAccounts.get(account.id) ?? account.name,
        ]),
      ),
    [accounts.data, resolvedAccounts],
  );

  // A18 / F4 (docs/33 §Hardening audit) — the M136 fix, now server-measured.
  //
  // The view used to print the generic "No transactions match these filters."
  // with a "Clear all" action that deliberately PRESERVES `q` — a dead end by
  // construction: the offered remedy could not change the outcome. M136 made
  // both halves honest; M137 keeps the honesty and moves the numbers to the
  // server: a locked search sends `hide_encrypted`, the page withholds the
  // cipher rows and REPORTS the count — `searchWithheld` is the server's
  // measurement, never a client guess.
  //   * the primary action stays the inline unlock, which genuinely restores
  //     the capability at that moment (docs/33 rule 7c/7d).
  const searchPopulation =
    searchQ === "" ? 0 : (page.data?.total ?? 0) + (page.data?.withheld ?? 0);
  const searchWithheld = searchQ === "" ? 0 : (page.data?.withheld ?? 0);
  const searchLockedOut = searchWithheld > 0;

  const list = page;
  // M95 P3 (docs/09 §Destructive actions): ids hidden by a PENDING delete —
  // the row leaves the table the instant the button is pressed, before
  // anything is sent. Undo (or a failed commit) puts it back exactly; a
  // successful commit lets the refetch replace this optimistic state.
  const [pendingDeletes, setPendingDeletes] = useState<Set<number>>(
    () => new Set(),
  );
  const items: Transaction[] = (page.data?.items ?? []).filter(
    (transaction) => !pendingDeletes.has(transaction.id),
  );
  const total = page.data?.total ?? 0;
  /** M107 (docs/44 D11): the group headers riding the SAME payload. */
  const splitParents = page.data?.split_parents ?? [];
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + PAGE_SIZE, total);

  // Remount key: row stagger replays whenever the filter/page view changes.
  //
  // The SELECTION is keyed more finely than the stagger (M125, docs/09):
  // `filterKey` (filters + search mode) wipes both scopes, while `pageKey`
  // (the offset) wipes only the page-scoped one — so "select all 345 matched"
  // survives paging to check the next 50 rows, and a filter change still
  // never leaves a stale selection behind.
  const filterKey = `${JSON.stringify(applied)}:${searchQ}`;
  const pageKey = String(offset);
  const viewKey = `${filterKey}:${pageKey}`;
  const visibleIds = useMemo(() => items.map((row) => row.id), [items]);
  // The SERVER's count behind the whole matched set — `total` is what the
  // pagination reads, so the counter and the pager can never disagree.
  // M137: search mode has a server total too — `q` thresholds the same
  // `_ledger_scope` the list runs, so "select all matched" over a ranked set
  // is the SAME set the server ranked (and the bulk body carries `q`).
  const matchedTotal = page.data?.total ?? null;
  const selection = useRowSelection({ filterKey, pageKey, visibleIds, matchedTotal });
  const selectedIds = useMemo(
    () => intersectSelected(selection.selected, visibleIds),
    [selection.selected, visibleIds],
  );
  /**
   * M107 (docs/44 §6.3): ONE grouping component over `items` +
   * `split_parents` — a header where the group first appears, ordinary rows
   * everywhere else (a child whose header is missing renders as an ordinary
   * row: fail open, never crash).
   */
  const units = groupSplitRows(items, splitParents);

  /** After a bulk run: drop the selection and keep pagination on a real page. */
  function handleBulkSettled({ action, outcome }: BulkSettled): void {
    selection.clear();
    if (action === "delete" && outcome.okCount > 0) {
      const remaining = total - outcome.okCount;
      if (remaining <= offset) setOffset(Math.max(0, offset - PAGE_SIZE));
    }
  }

  /**
   * One ordinary ledger row — the SAME markup a split child renders with
   * inside the parts panel (docs/44 §6.3), so a child keeps its inline
   * category, tags, history and review controls.
   */
  function renderRow(transaction: Transaction, ctx: { child: boolean }): ReactNode {
    const isChild = ctx.child || transaction.parent_split_id !== null;
    const r = resolvedTx.get(transaction.id);
    const payee = r?.payee ?? transaction.normalized_payee;
    const raw = r?.raw ?? transaction.raw_description;
    const amount = r?.amount ?? transaction.amount_native;

    const rowAccount = acctItems.find((account) => account.id === transaction.account_id);
    const investmentRow =
      rowAccount !== undefined && INVESTMENT_ACCOUNT_TYPES[rowAccount.type] === true;
    // Docs/44 §12: the Split affordance is hidden wherever the server refuses
    // it — investment accounts (D10, client hides / server refuses) and
    // children (a nested split is never accepted, §4.2 step 3). While the
    // account type is not known yet the button stays hidden: never offer an
    // action the server would turn down.
    const canSplit =
      !isChild && rowAccount !== undefined && !investmentRow;
    return (
    <motion.tr
      key={transaction.id}
      variants={fadeUp}
      className={`border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04] ${
        selection.isSelected(transaction.id) ? "bg-veil/[0.06]" : ""
      }`}
    >
      <td className="px-1 py-2">
        <span className="flex min-h-11 min-w-11 items-center justify-center md:min-h-0 md:min-w-0">
          <input
            type="checkbox"
            aria-label={`Select transaction: ${payee}`}
            className="size-5 cursor-pointer accent-accent"
            checked={selection.isSelected(transaction.id)}
            // React's checkbox `change` is dispatched from the click,
            // so the SHIFT state has to be read off `click` — the
            // generic `change` event carries no modifier keys.
            onChange={() => undefined}
            onClick={(event) =>
              selection.toggle(transaction.id, event.shiftKey)
            }
          />
        </span>
      </td>
      {/* M106 (docs/09 §Review queue): the row-level toggle —
          one click, no modal, and the same ≥44px phone hit area
          the quick-tag button carries (md: shrinks to 28px like
          every other row control). The label names ITS row, so two
          rows sharing a payee never read alike (M92). */}
      <td className="px-1 py-2">
        <button
          type="button"
          aria-pressed={transaction.reviewed_at !== null}
          aria-label={`Reviewed — ${payee} (transaction ${transaction.id})`}
          title={
            transaction.reviewed_at !== null
              ? "Reviewed — click to mark it unreviewed"
              : "Needs review — click to mark it reviewed"
          }
          disabled={review.isPending}
          // docs/47 §4: this control's ACTION is the blocked write
          // (`POST /transactions/{id}/review`) — padlock + the page's one
          // visible row reason; it stays ENABLED so it can explain itself.
          aria-describedby={rowWritesLocked ? LEDGER_ROW_REASON_ID : undefined}
          onClick={() =>
            review.mutate({
              id: transaction.id,
              reviewed: transaction.reviewed_at === null,
            })
          }
          className={`inline-flex min-h-11 min-w-11 items-center justify-center gap-1 rounded-lg border outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 md:min-h-7 md:min-w-7 ${
            transaction.reviewed_at !== null
              ? "border-accent/40 bg-accent/15 text-accent"
              : "border-control bg-veil/[0.04] text-muted hover:border-accent/70 hover:text-accent"
          }`}
        >
          {rowWritesLocked && <DemoLock />}
          {transaction.reviewed_at !== null ? (
            <Check className="size-4" aria-hidden />
          ) : (
            <Circle className="size-4" aria-hidden />
          )}
        </button>
      </td>
      <td className="whitespace-nowrap px-3 py-2 tabular">{transaction.date}</td>
      <td className="px-3 py-2 font-medium">{payee}</td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <span
          className={`tabular ${
            amount.startsWith("-") ? "text-danger" : ""
          }`}
        >
          {r?.locked ? amount : formatMoney(amount, transaction.currency)}
        </span>
        {transaction.currency !== "CAD" && transaction.cad_amount !== null && (
          <span className="ml-1 text-xs text-muted tabular">
            (≈ {formatMoney(transaction.cad_amount, "CAD")})
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-1">
          <CategoryPicker
            value={transaction.category_id === null ? "" : String(transaction.category_id)}
            categories={categories.data?.items ?? []}
            disabled={patch.isPending}
            inputClassName={`${INPUT_CLASS} max-w-44 cursor-pointer`}
            // docs/47 §4: picking a category PATCHes this row, and `Create`
            // POSTs — both are the blocked write, so the input wears the
            // padlock bound to the page's one visible row reason.
            locked={rowWritesLocked}
            lockReasonId={LEDGER_ROW_REASON_ID}
            onError={(error) => demo.onError("Create category", error)}
            onChange={(id) =>
              patch.mutate({
                id: transaction.id,
                category_id: id === "" ? null : Number(id),
              })
            }
            onCategoryCreated={() => {
              void queryClient.invalidateQueries({ queryKey: ["categories"] });
            }}
          />
          <button
            type="button"
            aria-label="Similar past entries"
            title="Similar past entries"
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              setSimilarAnchor({
                id: transaction.id,
                payee,
                left: rect.left,
                top: rect.bottom,
              });
            }}
            className={`rounded-lg p-1 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-accent focus-visible:ring-2 focus-visible:ring-accent ${
              similarAnchor?.id === transaction.id ? "text-accent" : ""
            }`}
          >
            <History className="size-4" aria-hidden />
          </button>
          {transaction.category_id === transfersCategoryId && (
            <>
              {transaction.transfer_pair_id ? (
                <button
                  type="button"
                  aria-label="Unlink transfer pair"
                  title="Unlink transfer pair"
                  disabled={unlinkTransferMutation.isPending}
                  aria-describedby={rowWritesLocked ? LEDGER_ROW_REASON_ID : undefined}
                  onClick={() => unlinkTransferMutation.mutate(transaction.id)}
                  className="inline-flex items-center gap-1 rounded-lg p-1 text-accent outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {rowWritesLocked && <DemoLock />}
                  <Link className="size-4" aria-hidden />
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    aria-label="Auto-detect transfer pair"
                    title="Auto-detect transfer pair"
                    disabled={detectTransfer.isPending}
                    aria-describedby={rowWritesLocked ? LEDGER_ROW_REASON_ID : undefined}
                    onClick={() => detectTransfer.mutate(transaction.id)}
                    className="inline-flex items-center gap-1 rounded-lg p-1 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {rowWritesLocked && <DemoLock />}
                    <Link className="size-4" aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label="Manually link transfer"
                    title="Manually link transfer"
                    onClick={() => setLinkModalTxn(transaction)}
                    className="rounded px-1 py-0.5 text-[10px] text-accent outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    Link
                  </button>
                </>
              )}
            </>
          )}
        </div>
      </td>
      {/* M64 (docs/09): description + account trail the core
          columns so Category/Actions land inside a 1280px viewport. */}
      <td className="max-w-64 truncate px-3 py-2 text-muted">
        {raw}
        {/* M107 (docs/44 §6.3): the parts panel shows each child's OWN note —
            v1 exposes `note` on children and in the split editor only. */}
        {isChild && transaction.note !== null && transaction.note !== "" && (
          <span className="ml-1 text-xs text-accent">· {transaction.note}</span>
        )}
      </td>
      <td className="px-3 py-2 text-muted">
        <RouterLink
          to={`/accounts/${transaction.account_id}`}
          className="hover:text-accent transition-colors"
        >
          {accountNames.get(transaction.account_id) ?? `#${transaction.account_id}`}
        </RouterLink>
        {transaction.pair_account_name && (
          <span className="ml-1 text-xs text-accent">
            → {transaction.pair_account_name}
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          {transaction.is_recurring && (
            <span
              className="inline-flex items-center gap-1 rounded-full border border-control bg-veil/[0.06] px-1.5 py-0.5 text-[10px] font-medium text-muted"
              title={recurringBadgeTitle(
                transaction.recurring_cadence,
                transaction.recurring_next_date,
              )}
            >
              <Repeat className="size-3" aria-hidden />
              recurring
              {transaction.recurring_cadence !== null &&
                ` · ${CADENCE_LABELS[transaction.recurring_cadence].toLowerCase()}`}
            </span>
          )}
          {(transaction.tags ?? []).map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center gap-1 rounded-full border border-accent/20 bg-accent/5 px-1.5 py-0.5 text-[10px] font-medium text-accent"
            >
              {tag.name}
            </span>
          ))}
          {/* M108 P4 (docs/09 §Receipt images): an image is
              attached. Clicking it fetches the bytes through the
              owner's session and opens them — no `/receipt` URL is
              ever put in an address bar, so nothing token-free can
              be shared or bookmarked. Kept in the tag area because
              that is where this row already carries its badges. */}
          {transaction.has_receipt && (
            <button
              type="button"
              title="View attached image"
              aria-label={`View the image attached to ${payee} (transaction ${transaction.id})`}
              onClick={() => viewReceipt.mutate(transaction.id)}
              className="inline-flex min-h-11 min-w-11 items-center justify-center gap-1 rounded-full border border-control bg-veil/[0.06] px-2 py-0.5 text-[10px] font-medium text-muted outline-none transition-colors hover:border-accent/70 hover:text-accent focus-visible:ring-2 focus-visible:ring-accent md:min-h-6 md:min-w-6"
            >
              <Paperclip className="size-3" aria-hidden />
              image
            </button>
          )}
          {/* M104: the tag area IS the affordance — one click opens
              the same picker in a popover anchored to this row
              (≥44px hit area on a phone, the title/aria-label pair
              docs/09 requires for icon-only controls, and a name
              unique per row: M92). */}
          <button
            type="button"
            title="Add tags"
            // Unique per row even when two rows share a payee
            // (M92): "Edit transaction N" names its target for the
            // same reason, and a probe selecting by name must not
            // match the wrong row.
            aria-label={`Add tags — ${payee} (transaction ${transaction.id})`}
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              setQuickTag({
                transaction,
                anchor: {
                  left: rect.left,
                  top: rect.top,
                  bottom: rect.bottom,
                },
              });
            }}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg border border-control bg-veil/[0.04] text-muted outline-none transition-colors hover:border-accent/70 hover:text-accent focus-visible:ring-2 focus-visible:ring-accent md:min-h-7 md:min-w-7"
          >
            <Plus className="size-4" aria-hidden />
          </button>
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <div className="flex justify-end gap-1">
          {/* M108 P4: attach (or replace) the row's one image.
              The same shared picker the editor uses — arming it
              remembers which row, so one input serves 200 rows.
              ≥44px on a phone, the floor docs/09 §Minimum Sizes
              sets for anything clickable. */}
          <button
            type="button"
            title="Attach an image"
            aria-label={`Attach an image — ${payee} (transaction ${transaction.id})`}
            onClick={() => armReceiptUpload(transaction)}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg border border-control bg-veil/[0.04] text-muted outline-none transition-colors hover:border-accent/70 hover:text-accent focus-visible:ring-2 focus-visible:ring-accent md:min-h-7 md:min-w-7"
          >
            <Paperclip className="size-4" aria-hidden />
          </button>
          {canSplit && (
            <button
              type="button"
              aria-label={`Split transaction ${transaction.id}`}
              title="Split this transaction into parts"
              onClick={() => {
                splitSave.reset();
                setSplitEditor({ parent: transaction });
              }}
              className="inline-flex min-h-11 items-center rounded px-2 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
            >
              Split
            </button>
          )}
          <button
            type="button"
            aria-label={`Edit transaction ${transaction.id}`}
            onClick={() => openEditor(transaction)}
            className="rounded px-2 py-1 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
          >
            Edit
          </button>
          {/* M106 (docs/09 §History drawer): the row's receipts,
              one click from the row itself — the second of the two
              entry points the spec names (the edit modal is the
              other). */}
          <button
            type="button"
            aria-label={`History — ${payee} (transaction ${transaction.id})`}
            title="History"
            onClick={() => setHistoryTx(transaction)}
            className="rounded px-2 py-1 text-xs text-muted outline-none hover:bg-veil/[0.06] hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
          >
            History
          </button>
          <button
            type="button"
            aria-label={`Delete transaction ${transaction.id}`}
            // docs/47 §4: `DELETE /transactions/{id}` is the blocked write —
            // the padlock rides with the row's one visible reason.
            aria-describedby={rowWritesLocked ? LEDGER_ROW_REASON_ID : undefined}
            onClick={() =>
              deferDelete(toast, {
                title: "Deleted transaction",
                detail: payee,
                hide: () =>
                  setPendingDeletes((prev) => {
                    const next = new Set(prev);
                    next.add(transaction.id);
                    return next;
                  }),
                restore: () =>
                  setPendingDeletes((prev) => {
                    const next = new Set(prev);
                    next.delete(transaction.id);
                    return next;
                  }),
                commit: () => deleteTransaction(transaction.id),
                onCommitted: () => {
                  if (offset > 0 && total <= offset + 1) {
                    setOffset(Math.max(0, offset - PAGE_SIZE));
                  }
                  invalidateLedgerData();
                },
                onError: (message, error) => {
                  if (demo.onError("Delete transaction", error)) return;
                  toast.error("Transaction delete failed", message);
                },
              })
            }
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-danger outline-none hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger"
          >
            {rowWritesLocked && <DemoLock />}
            Delete
          </button>
        </div>
      </td>
    </motion.tr>
    );
  }

  return (
    <section className="grid gap-6 lg:grid-cols-[1fr_20rem]">
      {/* min-w-0 lets the 1fr column shrink so the wide table scrolls
          internally instead of pushing the aside off-screen. */}
      <div className={`min-w-0 ${selection.count > 0 ? "pb-20 md:pb-0" : ""}`}>
        {/* docs/47 §4: the page's one refusal, above the table it is about. */}
        {refusalPlace === "page" && refusalNode}
        {/* docs/47 §4 (F-4): the ONE visible reason every row write's padlock
            binds to — same panel, non-zero box, never sr-only. Row controls
            repeat per row; a single reason element keeps the table readable
            (an `aria-describedby` IDREF is shared legally by many controls). */}
        {rowWritesLocked && (
          <DemoReason id={LEDGER_ROW_REASON_ID}>
            The demo account is read-only — row changes are not saved.
          </DemoReason>
        )}
        <div className="flex items-center justify-between gap-3">
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight">Transactions</h1>
            {/* M106 (docs/09 §Review queue): a small persistent hint, not a
                nag — it only shows while there is something to review, and it
                stays dismissed for the rest of this session. */}
            {unreviewedCount > 0 && !hintDismissed && (
              <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
                <span role="status" className="tabular">
                  {unreviewedCount} unreviewed
                </span>
                <button
                  type="button"
                  aria-label="Dismiss the unreviewed hint"
                  onClick={() => {
                    setHintDismissed(true);
                    sessionStorage.setItem(hintDismissKey, "1");
                  }}
                  className="rounded-full p-0.5 outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
                >
                  ×
                </button>
              </span>
            )}
          </span>
          <Button type="button" variant="primary" size="sm" onClick={() => openEditor(null)}>
            New transaction
          </Button>
        </div>

        {/* M123 (docs/09 §Create from search): the search → saved view / rule /
            bill / budget menu, plus the views that restore back here. It owns
            the row above the filter card and sits OUTSIDE the <form> — a title
            input in there would submit "Apply filters" on Enter — and its
            panels are `basis-full`, which only works with the full column. */}
        <div className="mt-4">
          <CreateFromSearch
            criteria={currentCriteria}
            sources={{
              categories: categories.data?.items ?? [],
              accounts: acctItems,
              tags: tags.data?.items ?? [],
            }}
            excludedFilters={excludedFilters}
            views={savedViews}
            onPersist={persistViews}
            onRestore={restoreView}
          />
          <form onSubmit={applyFilters} className="glass mt-2 rounded-xl p-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted">
              Account
              <select
                value={draft.accountId}
                onChange={(e) => setDraft({ ...draft, accountId: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block max-w-44`}
              >
                <option value="">All accounts</option>
                {(acctItems).map((account) => (
                  <option key={account.id} value={String(account.id)}>
                    {account.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-muted">
              Category
              <select
                value={draft.categoryId}
                onChange={(e) => setDraft({ ...draft, categoryId: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block max-w-44`}
              >
                <option value="">Any category</option>
                <option value="uncategorized">Uncategorized</option>
                {(categories.data?.items ?? []).map((category) => (
                  <option key={category.id} value={String(category.id)}>
                    {category.name}
                  </option>
                ))}
              </select>
            </label>
            {/* M104 (docs/09 §Tag UX): the single-tag equality select is gone —
                the rows carry MULTI-tags, so the filter is the same picker in
                filter mode, and the semantics are stated where the choice is
                made ("matches all selected") instead of explained away in a
                tooltip. */}
            <div className="text-xs text-muted">
              <span className="flex flex-wrap items-baseline gap-1">
                <span>Tags</span>
                <span className="text-accent">matches all selected</span>
              </span>
              <div className="mt-1">
                <TagFilterPicker
                  allTags={tags.data?.items ?? []}
                  selected={selectedFilterTags}
                  onToggle={toggleFilterTag}
                  onRemove={removeFilterTag}
                  onManage={() => setTagManagerOpen(true)}
                />
              </div>
            </div>
            <div className="text-xs text-muted">
              Quick range
              <RangePicker
                allowAll
                from={draft.from}
                to={draft.to}
                onChange={(r) => setDraft({ ...draft, from: r.from, to: r.to })}
              />
            </div>
            {/* M106 (docs/09 §Review queue): chip-family FILTER shortcut beside
                the quick-filters — applies on click, carries the same count the
                header hint shows, and turns the list into the queue. */}
            <div className="text-xs text-muted">
              Queue
              <div className="mt-1">
                <button
                  type="button"
                  aria-pressed={applied.reviewed === "false"}
                  onClick={toggleReviewQueue}
                  className={`inline-flex min-h-11 items-center gap-1.5 rounded-full border px-3 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
                    applied.reviewed === "false"
                      ? "border-accent bg-accent/15 text-accent"
                      : "border-control bg-veil/[0.04] text-muted hover:border-accent/70 hover:text-accent"
                  }`}
                >
                  Needs review
                  {unreviewedCount > 0 && (
                    <span className="rounded-full bg-accent/15 px-1.5 py-0.5 text-[10px] tabular text-accent">
                      {unreviewedCount}
                    </span>
                  )}
                </button>
              </div>
            </div>
            <label className="text-xs text-muted">
              From
              <input
                type="date"
                value={draft.from}
                onChange={(e) => setDraft({ ...draft, from: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block`}
              />
            </label>
            <label className="text-xs text-muted">
              To
              <input
                type="date"
                value={draft.to}
                onChange={(e) => setDraft({ ...draft, to: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block`}
              />
            </label>
            <label className="text-xs text-muted">
              Payee contains
              <input
                value={draft.payee}
                onChange={(e) => setDraft({ ...draft, payee: e.target.value })}
                placeholder="e.g. metro"
                className={`${INPUT_CLASS} mt-1 block w-40`}
              />
            </label>
            <label className="text-xs text-muted">
              Amount ≥
              <input
                type="number"
                step="any"
                value={draft.amountMin}
                onChange={(e) => setDraft({ ...draft, amountMin: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block w-24`}
              />
            </label>
            <label className="text-xs text-muted">
              Amount ≤
              <input
                type="number"
                step="any"
                value={draft.amountMax}
                onChange={(e) => setDraft({ ...draft, amountMax: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block w-24`}
              />
            </label>
            <label className="text-xs text-muted">
              Direction
              <select
                value={draft.direction}
                onChange={(e) => setDraft({ ...draft, direction: e.target.value })}
                className={`${INPUT_CLASS} mt-1 block`}
              >
                <option value="">All</option>
                <option value="paid">Paid</option>
                <option value="received">Received</option>
              </select>
            </label>
          </div>
          <div className="mt-2.5 flex items-center gap-2 border-t border-veil/[0.05] pt-2.5">
            <Button type="submit" variant="primary" size="sm">
              Apply filters
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={clearAllFilters}>
              Clear all
            </Button>
            {Object.values(applied).some((value) =>
              Array.isArray(value) ? value.length > 0 : value !== "",
            ) && (
              <span className="ml-auto text-[11px] uppercase tracking-widest text-accent">
                filtered
              </span>
            )}
          </div>
          </form>
        </div>

        {/* M96 B2 (docs/09 §Recurring prompt): one session, one scan,
            dismissible, and it says out loud that nothing leaves. */}
        <RecurringPromptCard />

        {searchQ !== "" && (
          <div className="mt-4 flex items-center gap-2 text-sm">
            <span className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-accent">
              Similar to “{searchQ}”
            </span>
            <span className="text-xs text-muted">relevance-ordered fuzzy match</span>
            <button
              type="button"
              onClick={clearSearchQ}
              className="text-xs text-muted outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
            >
              Clear
            </button>
          </div>
        )}

        {/* Bulk actions (docs/09): inline header row on desktop, sticky bar
            above the mobile bottom nav; count always announced.

            M125: `matched` is the SERVER-described set — while it is marked the
            toolbar makes ONE request whose body carries this page's filter plus
            the ids the user unchecked, and the counter is the server's `total`
            minus those exceptions. */}
        <BulkActionsToolbar
          rows={items}
          selectedIds={selectedIds}
          matched={
            selection.mode === "matched" && matchedTotal !== null
              ? {
                  // M137: in search mode the filter carries `q`, so the bulk
                  // run's scope is the ranked set the page shows — one
                  // predicate for list, chip and bulk (`_ledger_scope`).
                  filter: searchQ === "" ? filters : { ...filters, q: searchQ },
                  excludeIds: [...selection.excluded],
                  total: matchedTotal,
                }
              : null
          }
          matchedTotal={matchedTotal}
          onSelectAllMatched={
            matchedTotal !== null && matchedTotal > visibleIds.length
              ? selection.headerToggle
              : undefined
          }
          categories={categories.data?.items ?? []}
          tags={tags.data?.items ?? []}
          onClear={selection.clear}
          onSettled={handleBulkSettled}
        />

        {/* M64 (docs/09): the wrapper owns horizontal scrolling — labelled +
            focusable region, custom track, "Scroll for actions" affordance. */}
        <TableScroller label="Transactions table" className="mt-4 min-w-0">
          <table className="w-full text-sm">
            {/* No backdrop-blur on the sticky header: it repaints the blur over
              every scrolling row — a known Firefox stall (perf fix 2026-09-25). */}
            {/* M64 (docs/09): column order follows the workflow — the scan +
                categorize columns (Date, Payee, Amount, Category) come first so
                they sit inside the viewport at 1280px; reference columns
                (Description, Account, Tags) and the row actions trail. */}
            <thead className="sticky top-0 z-10 bg-surface/95">
              <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                <th className="px-1 py-2.5 font-medium">
                  <span className="flex min-h-11 min-w-11 items-center justify-center md:min-h-0 md:min-w-0">
                    {/* M125 (docs/09 §Select-all-matched): the three-state
                        cycle — page → matched → clear. The label NAMES the
                        next state, so a screen reader is told that a second
                        tick reaches 345 rows instead of only hearing "checked";
                        `title` is the same sentence for a mouse. Indeterminate
                        is carried in JS (`element.indeterminate`) — a property
                        no attribute can express. */}
                    <input
                      type="checkbox"
                      aria-label={selection.headerLabel}
                      title={selection.headerLabel}
                      className="size-5 cursor-pointer accent-accent"
                      checked={selection.headerChecked}
                      ref={(element) => {
                        if (element !== null) {
                          element.indeterminate = selection.headerIndeterminate;
                        }
                      }}
                      onChange={() => selection.headerToggle()}
                    />
                  </span>
                </th>
                {/* M106 (docs/09 §Review queue): the review column. The header
                    is screen-reader-only because every cell carries its own
                    toggle label — a visible "Review" would be the fourth
                    one-word column competing with Date/Payee/Amount. */}
                <th className="px-1 py-2.5 font-medium">
                  <span className="sr-only">Reviewed</span>
                </th>
                <th className="px-3 py-2.5 font-medium">Date</th>
                <th className="px-3 py-2.5 font-medium">Payee</th>
                <th className="px-3 py-2.5 text-right font-medium">Amount</th>
                <th className="px-3 py-2.5 font-medium">Category</th>
                <th className="px-3 py-2.5 font-medium">Description</th>
                <th className="px-3 py-2.5 font-medium">Account</th>
                <th className="px-3 py-2.5 font-medium">Tags</th>
                <th className="px-3 py-2.5 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <motion.tbody
              key={viewKey}
              variants={staggerContainer(0.02)}
              initial="hidden"
              animate="show"
            >
              {list.isPending && (
                <tr>
                  <td colSpan={10}>
                    <SkeletonRows rows={8} />
                  </td>
                </tr>
              )}
              {list.isError && (
                <tr>
                  <td colSpan={10} className="p-4">
                    <QueryErrorPanel query={list} />
                  </td>
                </tr>
              )}
              {/* A18 fix (M136): a LOCKED search view says which rows the
                  vault is withholding, with a measured count, and offers the
                  one action that actually restores them. Rule 7f — this
                  REPLACES the empty state; the two never stack. */}
              {!list.isPending && !list.isError && items.length === 0 && searchLockedOut && (
                <tr>
                  <td colSpan={10}>
                    <VaultLocked
                      variant="partial"
                      capability="search"
                      total_count={searchPopulation}
                      withheld_count={searchWithheld}
                    />
                  </td>
                </tr>
              )}
              {!list.isPending && !list.isError && items.length === 0 && !searchLockedOut && (
                <tr>
                  <td colSpan={10}>
                    <EmptyState
                      icon={<SearchX />}
                      title="No transactions match these filters."
                      hint="Loosen a filter or clear them all to see everything."
                      preview={{
                        src: "/help/transactions.png",
                        alt: "The Transactions table with a page of real rows",
                      }}
                      action={{
                        label: "Clear all",
                        onClick: clearAllFilters,
                        ariaLabel: "Clear all — No transactions match these filters.",
                      }}
                    />
                  </td>
                </tr>
              )}
              {units.map((unit) =>
                unit.kind === "group" ? (
                  <SplitGroupRow
                    key={unit.key}
                    group={unit.group}
                    layout={REGISTER_SPLIT_LAYOUT}
                    variant="table"
                    expanded={expandedSplit === unit.group.parent.id}
                    onToggleExpand={() =>
                      setExpandedSplit((current) =>
                        current === unit.group.parent.id ? null : unit.group.parent.id,
                      )
                    }
                    renderRow={renderRow}
                    selection={{
                      isSelected: selection.isSelected,
                      // `isSelected`, not `selected.has` — in matched mode the
                      // explicit id set is empty and the group's children are
                      // selected by the FILTER (M125).
                      onToggleGroup: (ids) =>
                        selection.toggleMany(ids, !ids.every((id) => selection.isSelected(id))),
                    }}
                    merge={{
                      pending: splitMerge.isPending,
                      onMerge: () => splitMerge.mutate(unit.group.parent.id),
                    }}
                    resplit={{
                      onResplit: (children) => {
                        splitSave.reset();
                        setSplitEditor({
                          parent: unit.group.parent as SplitParentPublic,
                          children,
                        });
                      },
                    }}
                    accountName={(accountId) => accountNames.get(accountId) ?? `#${accountId}`}
                    resolved={{
                      payee: resolvedTx.get(unit.group.parent.id)?.payee,
                      locked: resolvedTx.get(unit.group.parent.id)?.locked ?? false,
                    }}
                  />
                ) : (
                  renderRow(unit.tx, { child: unit.tx.parent_split_id !== null })
                ),
              )}
            </motion.tbody>
          </table>
        </TableScroller>

        <div className="mt-3 flex items-center justify-between text-sm text-muted">
          <span className="tabular">
            Showing {rangeStart}–{rangeEnd} of {total}
          </span>
          <span className="flex gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={offset === 0 || list.isPending}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={rangeEnd >= total || list.isPending}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </span>
        </div>
      </div>

      <div className="min-h-0 overflow-hidden">
        <SuggestionQueue />
      </div>
      {similarAnchor !== null && (
        <SimilarExamples anchor={similarAnchor} onClose={() => setSimilarAnchor(null)} />
      )}
      {quickTag !== null && (
        <QuickTagPopover
          anchor={quickTag.anchor}
          attached={quickTag.transaction.tags ?? []}
          allTags={tags.data?.items ?? []}
          creating={quickTagCreate.isPending}
          onAttach={(tagId) =>
            quickTagAttach.mutate({ transaction: quickTag.transaction, tagId })
          }
          onCreate={(name) =>
            quickTagCreate.mutate({ transaction: quickTag.transaction, name })
          }
          locked={rowWritesLocked}
          onClose={() => setQuickTag(null)}
        />
      )}
      {/* M108 P4: the page's one and only file picker. Hidden but FOCUSABLE
          (never `display:none` — that would strand keyboard users who reach
          it), so `sr-only` rather than a fixed `hidden` attribute. Armed by
          either the row's paperclip or the editor's attach control. */}
      <input
        ref={receiptInputRef}
        type="file"
        accept="image/webp,image/jpeg,image/png,image/gif"
        className="sr-only"
        aria-label="Choose an image to attach"
        onChange={onReceiptFilePicked}
      />
      {historyTx !== null && (
        <HistoryDrawer
          transactionId={historyTx.id}
          title={historyTx.normalized_payee}
          onClose={() => setHistoryTx(null)}
        />
      )}
      {tagManagerOpen && (
        <TagManagerModal onClose={() => setTagManagerOpen(false)} />
      )}
      {linkModalTxn !== null && (
        <LinkModal
          source={linkModalTxn}
          transfersCategoryId={transfersCategoryId}
          accountNames={accountNames}
          candidates={items.filter(
            (t) =>
              t.category_id === transfersCategoryId &&
              t.id !== linkModalTxn.id &&
              t.account_id !== linkModalTxn.account_id &&
              t.transfer_pair_id === null &&
              t.amount_native === `-${linkModalTxn.amount_native}`,
          )}
          onLink={(pairId) =>
            linkTransferMutation.mutate({ txnId: linkModalTxn.id, pairId })
          }
          onClose={() => setLinkModalTxn(null)}
          isPending={linkTransferMutation.isPending}
          locked={rowWritesLocked}
        />
      )}
      {editorOpen && (
        <TransactionEditor
          target={editorTarget}
          accounts={accounts.data?.items ?? []}
          categories={categories.data?.items ?? []}
          saving={save.isPending}
          error={save.error}
          demoRefusal={refusalPlace === "editor" ? refusalNode ?? undefined : undefined}
          onWriteError={(action, error) => demo.onError(action, error)}
          onSave={(payload) => save.mutate({ target: editorTarget, payload })}
          onClose={() => setEditorOpen(false)}
          onHistory={(transaction) => {
            // One dialog at a time: the editor hands the row over and gets out
            // of the way, so focus lands in the drawer, not behind both.
            setEditorOpen(false);
            setEditorTarget(null);
            setHistoryTx(transaction);
          }}
          onCategoryCreated={() => {
            void queryClient.invalidateQueries({ queryKey: ["categories"] });
          }}
          onReceiptChanged={() => void invalidateLedgerData()}
        />
      )}
      {/* M107 (docs/44 §12): Split this transaction into parts. */}
      {splitEditor !== null && (
        <SplitEditor
          parent={splitEditor.parent}
          initialChildren={splitEditor.children}
          categories={categories.data?.items ?? []}
          saving={splitSave.isPending}
          error={splitSave.error}
          demoRefusal={refusalPlace === "split" ? refusalNode ?? undefined : undefined}
          onWriteError={(action, error) => demo.onError(action, error)}
          onSave={(children) =>
            splitSave.mutate({
              parent: splitEditor.parent,
              children,
              replace: splitEditor.children !== undefined,
            })
          }
          onClose={() => setSplitEditor(null)}
          onCategoryCreated={() => {
            void queryClient.invalidateQueries({ queryKey: ["categories"] });
          }}
        />
      )}
    </section>
  );
}
