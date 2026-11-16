// AccountDetail (docs/09): per-account detail page. Dispatches to a
// bank-style view (running balance, starting balance, transactions, transfer
// suggestions) or an investment-style view (holdings, allocation, trades,
// watchlist, cash activity) based on the account type.
import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { ArrowLeftRight, History, Pencil, SearchX, TrendingUp, Wallet } from "lucide-react";
import {
  getAccount,
  getAccountLedger,
  INVESTMENT_ACCOUNT_TYPES,
  updateAccount,
  listAccounts,
} from "../api/accounts";
import type { Account, AccountType, ValueHistoryEvent } from "../api/accounts";
import {
  createTransaction,
  deleteTransaction,
  detectTransferPair,
  getTransferSuggestions,
  listTransactions,
  unlinkTransfer,
  updateTransaction,
  addTransactionTag,
  removeTransactionTag,
  createTransactionSplit,
  updateTransactionSplit,
  mergeTransactionSplit,
} from "../api/transactions";
import { listTags } from "../api/tags";
import type {
  SplitChildInput,
  SplitParentPublic,
  Transaction,
  TransactionCreate,
  TransactionFilters,
  TransactionPatch,
} from "../api/transactions";
import { listCategories } from "../api/categories";
import { useOptionalUser } from "../auth/authUser";
import { Investments } from "./Investments";
import { PendingImportBanner } from "../components/imports/PendingImportBanner";
import { TransferSuggestions } from "../components/transactions/TransferSuggestions";
import { TransactionEditor } from "../components/transactions/TransactionEditor";
import { HistoryDrawer } from "../components/transactions/HistoryDrawer";
import { SplitEditor } from "../components/transactions/SplitEditor";
import {
  ACCOUNT_SPLIT_LAYOUT,
  SplitGroupRow,
  groupSplitRows,
} from "../components/transactions/SplitGroupRow";
import { CategoryPicker } from "../components/transactions/CategoryPicker";
import { BulkActionsToolbar } from "../components/transactions/BulkActionsToolbar";
import type { BulkSettled } from "../components/transactions/BulkActionsToolbar";
import { intersectSelected, useRowSelection } from "../components/transactions/useRowSelection";
import { apiErrorMessage, formatMoney } from "../components/transactions/format";
import { AccountValueChart } from "../components/charts/AccountValueChart";
import { Card, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  demoLocks,
  useDemoRefusal,
} from "../components/ui/DemoLock";
import { RangePicker, rangeFor } from "../components/ui/RangePicker";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { TableScroller } from "../components/ui/TableScroller";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { deferDelete } from "../components/ui/deferDelete";
import { useToast } from "../components/ui/Toast";
import { useMasked } from "../lib/mask";

const TYPE_LABELS: Record<AccountType, string> = {
  chequing: "Chequing",
  savings: "Savings",
  credit_card: "Credit Card",
  line_of_credit: "Line of Credit",
  brokerage_non_registered: "Brokerage",
  rrsp: "RRSP",
  tfsa: "TFSA",
  fhsa: "FHSA",
  resp: "RESP",
  lira: "LIRA",
  rdsp: "RDSP",
};

const INPUT_CLASS =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

/**
 * `aria-describedby` target of EVERY row-write padlock in the account's
 * Activity table (docs/47 §4): category, transfer link/unlink, tag attach /
 * detach and delete. One VISIBLE reason element serves them all — an IDREF
 * list may be shared, and a reason per row would print the same sentence
 * 50 times.
 */
const ACTIVITY_ROW_REASON_ID = "account-activity-row-write-demo-reason";

/** `aria-describedby` target of the starting-balance form's Save padlock. */
const STARTING_BALANCE_REASON_ID = "account-starting-balance-demo-reason";

interface FilterDraft {
  from: string;
  to: string;
  payee: string;
  amountMin: string;
  amountMax: string;
  direction: "" | "paid" | "received";
}

const EMPTY_FILTER: FilterDraft = {
  from: "",
  to: "",
  payee: "",
  amountMin: "",
  amountMax: "",
  direction: "",
};

function draftToFilters(draft: FilterDraft): TransactionFilters {
  const filters: TransactionFilters = {};
  if (draft.from) filters.from = draft.from;
  if (draft.to) filters.to = draft.to;
  if (draft.payee) filters.payee = draft.payee;
  if (draft.amountMin !== "") filters.amountMin = Number(draft.amountMin);
  if (draft.amountMax !== "") filters.amountMax = Number(draft.amountMax);
  if (draft.direction) filters.direction = draft.direction;
  return filters;
}

const PAGE_SIZE = 50;

export function AccountDetail() {
  const { accountId } = useParams<{ accountId: string }>();
  const id = Number(accountId);

  const account = useQuery({
    queryKey: ["accounts", id],
    queryFn: () => getAccount(id),
    enabled: !isNaN(id),
  });

  if (account.isLoading) {
    return <SkeletonRows rows={3} />;
  }
  if (account.isError || !account.data) {
    return <ErrorPanel message="Account not found" />;
  }

  const acct = account.data;
  const investment = INVESTMENT_ACCOUNT_TYPES[acct.type];
  return (
    <>
      {/* M113 (docs/09 §Account page pending-import banner): staged-but-not-
          committed batches are invisible from here — the banner is the one
          hint that an import is waiting. Count only, both account views. */}
      <PendingImportBanner
        accountId={acct.id}
        count={acct.pending_import_count ?? 0}
        label={TYPE_LABELS[acct.type] ?? acct.type}
      />
      {investment ? <InvestmentAccountView account={acct} /> : <BankAccountView account={acct} />}
    </>
  );
}

function BankAccountView({ account }: { account: Account }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const id = account.id;
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal for every write
  // this view dispatches — a refused demo write is one local `role="status"`
  // element + one `toast.info`, never a `role="alert"` toast (Toast renders
  // an error tone AS `role="alert"`).
  const demo = useDemoRefusal();
  // The session scope is the M136 optional user (the same scope
  // TransactionEditor/SplitEditor read): outside an `AuthProvider` the scope
  // is null and the verdict degrades to "no session, no lock" — the honest
  // degradation `auth/authUser.ts` documents — instead of throwing.
  const user = useOptionalUser();
  // docs/47 §4 condition 2: this view's one write is `PATCH /accounts/{id}`,
  // a mutating request on a path `deps.py` does not exempt.
  const startingBalanceLocked = demoLocks(user, "PATCH", "/accounts/{id}");
  // M118: a marker pin points at the row behind it — the chart hands the
  // event down to the Activity table, which scrolls to and highlights it.
  const [focusEvent, setFocusEvent] = useState<ValueHistoryEvent | null>(null);

  const ledger = useQuery({
    queryKey: ["ledger", id],
    queryFn: () => getAccountLedger(id),
    enabled: !isNaN(id),
  });

  const suggestions = useQuery({
    queryKey: ["transfer-suggestions", id],
    queryFn: () => getTransferSuggestions(id),
    enabled: !isNaN(id),
  });

  // Starting balance
  const [editingStarting, setEditingStarting] = useState(false);
  const [startingValue, setStartingValue] = useState("");
  const initialRange = useMemo(() => rangeFor("1M"), []);
  const [chartRange, setChartRange] = useState(initialRange);

  const updateStartingBalance = useMutation({
    mutationFn: (value: number) => updateAccount(id, { starting_balance: value }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["accounts", id] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["ledger", id] });
      toast.success("Starting balance updated");
      setEditingStarting(false);
    },
    onError: (err) => {
      // docs/47 §4: a demo refusal is not a failure — one `demo-refusal` +
      // one `toast.info`, never an error toast (which renders `role="alert"`).
      if (demo.onError("Update starting balance", err)) return;
      toast.error("Update failed", err instanceof Error ? err.message : "Unknown error");
    },
  });

  return (
    <section className="grid gap-6 lg:grid-cols-[1fr_20rem]">
      {/* docs/47 §4: this view's one refusal, above the card it is about. The
          editor dialog this view never opens, so there is no second place. */}
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      {/* min-w-0: lets the 1fr column shrink (see Transactions fix). */}
      <div className="min-w-0">
        {/* Header */}
        <div className="mb-6 flex items-start gap-4">
          <div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-accent/10">
            <Wallet className="size-6 text-accent" aria-hidden />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-2xl font-semibold">{account.name}</h1>
            <div className="mt-1 flex items-center gap-2 text-sm text-muted">
              <span className="rounded-md bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                {TYPE_LABELS[account.type as AccountType] ?? account.type}
              </span>
              <span>{account.currency}</span>
            </div>
          </div>
          <div className="text-right">
            {/* M68: a client-side route — an <a> here reloaded the whole SPA. */}
            <Link to={`/reconciliation/${account.id}`} className="text-xs text-accent hover:underline">Reconcile</Link>
            <div className="text-2xl font-bold">{formatMoney(account.cash_balance, account.currency)}</div>
            <div className="text-xs text-muted">Current balance</div>
          </div>
        </div>

        {/* M118 (docs/09 §Account value history): the value timeline REPLACES
            the old ledger "Balance Over Time" chart — same slot, same
            RangePicker, but it is now the shared series (cash + holdings, in
            the reporting currency, with event markers) instead of a second
            opinion about the same number. The ledger query above still feeds
            the Activity table and its running-balance column. */}
        <div className="mb-6">
          <AccountValueChart
            accountId={id}
            from={chartRange.from || undefined}
            to={chartRange.to || undefined}
            headerExtra={
              <RangePicker
                allowAll
                from={chartRange.from}
                to={chartRange.to}
                onChange={setChartRange}
                size="sm"
              />
            }
            onSelectEvent={setFocusEvent}
            selectedEventId={focusEvent?.id ?? null}
          />
        </div>

        {/* Starting balance */}
        <Card hover className="mb-6 p-4">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Starting Balance</CardTitle>
              <p className="mt-1 text-xs text-muted">
                Balance before the earliest imported transaction
              </p>
            </div>
            {editingStarting ? (
              <form onSubmit={(e) => { e.preventDefault(); const num = Number(startingValue); if (isNaN(num)) { toast.error("Invalid number"); return; } updateStartingBalance.mutate(num); }} className="flex items-center gap-2">
                <input
                  type="text"
                  value={startingValue}
                  onChange={(e) => setStartingValue(e.target.value)}
                  autoFocus
                  className={`${INPUT_CLASS} w-32`}
                  placeholder="0.00"
                />
                <button
                  type="submit"
                  // docs/47 §4: Save's ACTION is the blocked write
                  // (`PATCH /accounts/{id}`) — padlock bound to the card's one
                  // visible reason; it stays ENABLED so it can explain itself.
                  aria-describedby={
                    startingBalanceLocked ? STARTING_BALANCE_REASON_ID : undefined
                  }
                  className="inline-flex items-center gap-1 rounded-lg bg-accent px-3 py-1 text-sm font-medium text-accent-ink hover:bg-accent/90"
                >
                  {startingBalanceLocked && <DemoLock />}
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => setEditingStarting(false)}
                  className="rounded-lg px-3 py-1 text-sm text-muted hover:text-text"
                >
                  Cancel
                </button>
              </form>
            ) : (
              <button
                type="button"
                onClick={() => { setStartingValue(account.starting_balance); setEditingStarting(true); }}
                className="flex items-center gap-2 rounded-lg border border-control px-3 py-1.5 text-sm text-muted transition-colors hover:border-accent/70 hover:text-text"
              >
                <span>{formatMoney(account.starting_balance, account.currency)}</span>
                <Pencil className="size-3.5" aria-hidden />
              </button>
            )}
          </div>
          {/* docs/47 §4 (F-4): the visible reason the Save padlock binds to —
              same panel, non-zero box, never sr-only. */}
          {startingBalanceLocked && (
            <DemoReason id={STARTING_BALANCE_REASON_ID}>
              The demo account is read-only — the starting balance is not saved.
            </DemoReason>
          )}
        </Card>

        <AccountActivity account={account} focusEvent={focusEvent} />
      </div>

      {/* Right sidebar — transfer suggestions */}
      <div className="hidden lg:block">
        <div className="sticky top-6 space-y-4">
          {suggestions.data && suggestions.data.items.length > 0 && (
            <Card hover className="p-4">
              <div className="mb-3 flex items-center gap-2">
                <ArrowLeftRight className="size-4 text-accent" aria-hidden />
                <h3 className="text-sm font-semibold">Transfer Suggestions</h3>
              </div>
              <TransferSuggestions
                suggestions={suggestions.data.items}
                accountCurrency={account.currency}
              />
            </Card>
          )}
          <Card hover className="p-4">
            <h3 className="mb-3 text-sm font-semibold">Account Info</h3>
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between">
                <dt className="text-muted">Type</dt>
                <dd>{TYPE_LABELS[account.type as AccountType] ?? account.type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">Currency</dt>
                <dd>{account.currency}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">Starting balance</dt>
                <dd>{formatMoney(account.starting_balance, account.currency)}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-muted">Transactions</dt>
                <dd>{ledger.data?.total ?? 0}</dd>
              </div>
            </dl>
          </Card>
        </div>
      </div>

      {/* Mobile transfer suggestions (below table on small screens) */}
      {suggestions.data && suggestions.data.items.length > 0 && (
        <div className="lg:hidden">
          <Card hover className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <ArrowLeftRight className="size-4 text-accent" aria-hidden />
              <h3 className="text-sm font-semibold">Transfer Suggestions</h3>
            </div>
            <TransferSuggestions
              suggestions={suggestions.data.items}
              accountCurrency={account.currency}
            />
          </Card>
        </div>
      )}
    </section>
  );
}

function AccountActivity({
  account,
  focusEvent = null,
}: {
  account: Account;
  /** M118: a marker pin on the value chart points at ONE row in here. */
  focusEvent?: ValueHistoryEvent | null;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const id = account.id;
  const queryClient = useQueryClient();
  const toast = useToast();
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal for every write
  // this table dispatches — a refused demo row mutation is one local
  // `role="status"` element + one `toast.info`, never a `role="alert"` toast
  // (Toast renders an error tone AS `role="alert"`).
  const demo = useDemoRefusal();
  // The M136 optional user scope (see BankAccountView): a `.map()` cannot
  // call hooks, so the row verdict is the NON-hook `demoLocks` below.
  const user = useOptionalUser();
  // The ONE verdict every row-level write shares: they are all
  // PATCH|POST|DELETE /transactions…, and no transaction path is in
  // `deps.py`'s exemption sets — the `{id}` segment is only there to show the
  // shape the check runs against; the verdict does not depend on its value.
  const rowWritesLocked = demoLocks(user, "PATCH", "/transactions/{id}");
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(200),
  });

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(200),
  });

  const transfersCategoryId = useMemo(
    () =>
      (categories.data?.items ?? []).find(
        (c) => c.name === "transfers" && c.is_system,
      )?.id ?? null,
    [categories.data],
  );

  // All available tags for inline picker
  const allTags = useQuery({
    queryKey: ["tags"],
    queryFn: () => listTags(200),
  });

    // Filter state
  const [draft, setDraft] = useState<FilterDraft>(EMPTY_FILTER);
  const [applied, setApplied] = useState<FilterDraft>(EMPTY_FILTER);
  const [offset, setOffset] = useState(0);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editorTarget, setEditorTarget] = useState<Transaction | null>(null);
  // --- M106 (docs/09 §History drawer) ---------------------------------------
  /** The one row whose receipts are on screen — one drawer at a time, so the
   *  editor hands the row over and closes first (same rule as /transactions). */
  const [historyTx, setHistoryTx] = useState<Transaction | null>(null);
  // --- M107 (docs/44 §12) ----------------------------------------------------
  /** The split group whose parts panel is open. */
  const [expandedSplit, setExpandedSplit] = useState<number | null>(null);
  /** Split editor — the parent row, plus children when RE-splitting. */
  const [splitEditor, setSplitEditor] = useState<{
    parent: Transaction;
    children?: Transaction[];
  } | null>(null);
  /** Splits are a personal-surface feature (docs/44 §13): hidden on investments. */
  const canSplitAccount = INVESTMENT_ACCOUNT_TYPES[account.type] !== true;
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal element for the
  // whole table, rendered in exactly ONE place — inside the dialog that is
  // open (where the error line used to be), otherwise above the table. The
  // two never stack, so a refused write is always exactly one `demo-refusal`.
  const refusalNode =
    demo.refusal !== null ? (
      <DemoRefusal id={demo.refusalId} action={demo.refusal} />
    ) : null;
  const refusalPlace = editorOpen ? "editor" : splitEditor !== null ? "split" : "page";

  function invalidateLedgerData(): void {
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    void queryClient.invalidateQueries({ queryKey: ["reports"] });
    void queryClient.invalidateQueries({ queryKey: ["search"] });
    void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
    void queryClient.invalidateQueries({ queryKey: ["transfer-suggestions"] });
  }

  function openEditor(target: Transaction | null): void {
    save.reset();
    setEditorTarget(target);
    setEditorOpen(true);
  }

  const filters = useMemo(() => draftToFilters(applied), [applied]);

  const transactions = useQuery({
    queryKey: ["transactions", id, filters, offset],
    queryFn: () => listTransactions({ ...filters, accountId: id }, { limit: PAGE_SIZE, offset }),
    enabled: !isNaN(id),
  });

  const save = useMutation({
    mutationFn: ({
      target,
      payload,
    }: {
      target: Transaction | null;
      payload: TransactionCreate | TransactionPatch;
    }) => (target ? updateTransaction(target.id, payload) : createTransaction(payload as TransactionCreate)),
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

  // Mutations
  const patch = useMutation({
    mutationFn: ({ txnId, category_id }: { txnId: number; category_id: number | null }) =>
      updateTransaction(txnId, { category_id }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
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

  const detectTransfer = useMutation({
    mutationFn: (txnId: number) => detectTransferPair(txnId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      toast.success("Transfer pair linked");
    },
    onError: (err) => {
      if (demo.onError("Auto-detect transfer", err)) return;
      toast.error("Auto-detect failed", apiErrorMessage(err));
    },
  });

  const unlinkTransferMutation = useMutation({
    mutationFn: (txnId: number) => unlinkTransfer(txnId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      toast.success("Transfer pair unlinked");
    },
    onError: (err) => {
      if (demo.onError("Unlink transfer", err)) return;
      toast.error("Unlink failed", apiErrorMessage(err));
    },
  });

  // --- M107 (docs/44 §4) ----------------------------------------------------
  // The SERVER's envelope is rendered by the editor (`detail` verbatim +
  // `code`), so a refused sum never degrades into a generic toast (§12).
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

  /** Merge back — one DELETE, then the ledger caches re-read (docs/44 §5). */
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

  function applyFilters(e: FormEvent) {
    e.preventDefault();
    setApplied({ ...draft });
    setOffset(0);
  }

  function clearAllFilters() {
    setDraft(EMPTY_FILTER);
    setApplied(EMPTY_FILTER);
    setOffset(0);
  }

  // M95 P3 (docs/09 §Destructive actions): rows hidden by a PENDING delete —
  // optimistic removal first, the real request only after the 5s window.
  const [pendingDeletes, setPendingDeletes] = useState<Set<number>>(
    () => new Set(),
  );
  const txnItems = (transactions.data?.items ?? []).filter(
    (txn) => !pendingDeletes.has(txn.id),
  );
  const txnTotal = transactions.data?.total ?? 0;
  const rangeStart = txnTotal === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + PAGE_SIZE, txnTotal);
  /** M107 (docs/44 D11): the group headers riding the SAME page payload. */
  const splitParents = transactions.data?.split_parents ?? [];
  const units = groupSplitRows(txnItems, splitParents);

  // --- M125 (docs/09 §Bulk selection toolbar §On the account view) ----------
  // The SAME multi-select and the SAME toolbar `/transactions` uses: this is
  // the same ledger table with `account_id` pinned, so the filter that reaches
  // a bulk run's body is this page's own — account id included, which is what
  // makes "select all N matched" on CIBC Chequing act on CIBC Chequing only.
  const matchedTotal = transactions.data?.total ?? null;
  const filterKey = `acct:${id}:${JSON.stringify(applied)}`;
  const pageKey = String(offset);
  const staggerKey = `${filterKey}:${pageKey}`;
  const visibleIds = useMemo(() => txnItems.map((txn) => txn.id), [txnItems]);
  const selection = useRowSelection({ filterKey, pageKey, visibleIds, matchedTotal });
  const selectedIds = useMemo(
    () => intersectSelected(selection.selected, visibleIds),
    [selection.selected, visibleIds],
  );
  /** The bulk body's filter — the list query's, account id and all. */
  const bulkFilter = useMemo(() => ({ ...filters, accountId: id }), [filters, id]);

  /** After a bulk run: drop the selection, keep pagination on a real page. */
  function handleBulkSettled({ action, outcome }: BulkSettled): void {
    selection.clear();
    if (action === "delete" && outcome.okCount > 0) {
      const remaining = txnTotal - outcome.okCount;
      if (remaining <= offset) setOffset(Math.max(0, offset - PAGE_SIZE));
    }
    invalidateLedgerData();
  }

  /** The row a value-chart marker pointed at — ringed until another pin wins. */
  const [highlightId, setHighlightId] = useState<number | null>(null);

  // M118 (docs/09 §Marker → row): a pin jump narrows the table to the event's
  // OWN day first. Without that, an older row can sit pages away behind the
  // default sort and the "jump" would silently land on nothing — and the filter
  // bar showing From/To = that date makes the narrowing visible, not magic.
  useEffect(() => {
    if (focusEvent === null || focusEvent.source !== "transaction") return;
    if (focusEvent.transaction_id === null) return;
    setDraft({ ...EMPTY_FILTER, from: focusEvent.date, to: focusEvent.date });
    setApplied({ ...EMPTY_FILTER, from: focusEvent.date, to: focusEvent.date });
    setOffset(0);
    setHighlightId(focusEvent.transaction_id);
  }, [focusEvent]);

  // Run again when the narrowed page lands: the row only exists in the DOM
  // once its query has resolved. Instant scroll (a long smooth pan is motion
  // the user did not ask for).
  useEffect(() => {
    if (highlightId === null) return;
    const row = document.querySelector(`[data-txn-id="${highlightId}"]`);
    // Optional call: jsdom (the test runtime) has no layout and therefore no
    // scrollIntoView; every real browser does.
    row?.scrollIntoView?.({ block: "center" });
  }, [highlightId, txnItems]);

  /**
   * One ordinary ledger row — the SAME markup a split child renders with
   * inside the parts panel (docs/44 §6.3).
   */
  function renderRow(txn: Transaction, ctx: { child: boolean }): ReactNode {
    const isChild = ctx.child || txn.parent_split_id !== null;
    // Docs/44 §12: Split is hidden wherever the server refuses it —
    // investment accounts (D10) and children (no nested split, §4.2).
    const canSplit = canSplitAccount && !isChild;
    return (
    <motion.tr
      key={txn.id}
      variants={fadeUp}
      data-txn-id={txn.id}
      aria-current={highlightId === txn.id ? "true" : undefined}
      className={`border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]${
        highlightId === txn.id ? " bg-accent/10 ring-1 ring-inset ring-accent" : ""
      }${selection.isSelected(txn.id) ? " bg-veil/[0.06]" : ""}`}
    >
      {/* M125: the SAME selection column /transactions carries — one checkbox
          first, in a ≥44px wrapper below `md` (M72 exempts checkboxes from the
          global floor, so the hit area is declared here, M96's rule). */}
      <td className="px-1 py-2">
        <span className="flex min-h-11 min-w-11 items-center justify-center md:min-h-0 md:min-w-0">
          <input
            type="checkbox"
            aria-label={`Select transaction: ${txn.normalized_payee}`}
            className="size-5 cursor-pointer accent-accent"
            checked={selection.isSelected(txn.id)}
            // React dispatches `change` from the click, so the SHIFT state has
            // to be read off `click` — `change` carries no modifier keys.
            onChange={() => undefined}
            onClick={(event) => selection.toggle(txn.id, event.shiftKey)}
          />
        </span>
      </td>
      <td className="whitespace-nowrap px-3 py-2 tabular">{txn.date}</td>
      <td className="px-3 py-2 font-medium">{txn.normalized_payee}</td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <span
          className={`tabular ${
            txn.amount_native.startsWith("-") ? "text-danger" : ""
          }`}
        >
          {formatMoney(txn.amount_native, txn.currency)}
        </span>
        {txn.currency !== "CAD" && txn.cad_amount !== null && (
          <span className="ml-1 text-xs text-muted tabular">
            (≈ {formatMoney(txn.cad_amount, "CAD")})
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-1">
          <CategoryPicker
            value={txn.category_id === null ? "" : String(txn.category_id)}
            categories={categories.data?.items ?? []}
            disabled={patch.isPending}
            inputClassName={`${INPUT_CLASS} max-w-44 cursor-pointer`}
            // docs/47 §4: picking a category PATCHes this row, and `Create`
            // POSTs — both are the blocked write, so the input wears the
            // padlock bound to the table's one visible row reason.
            locked={rowWritesLocked}
            lockReasonId={ACTIVITY_ROW_REASON_ID}
            onError={(error) => demo.onError("Create category", error)}
            onChange={(id) =>
              patch.mutate({
                txnId: txn.id,
                category_id: id === "" ? null : Number(id),
              })
            }
            onCategoryCreated={() => {
              void queryClient.invalidateQueries({ queryKey: ["categories"] });
            }}
          />
          {txn.category_id === transfersCategoryId && (
            <>
              {txn.transfer_pair_id ? (
                <button
                  type="button"
                  aria-label="Unlink transfer pair"
                  title="Unlink transfer pair"
                  disabled={unlinkTransferMutation.isPending}
                  // docs/47 §4: `POST /transactions/{id}/unlink-transfer` is
                  // the blocked write — padlock + the table's one visible row
                  // reason; it stays ENABLED so it can explain itself.
                  aria-describedby={
                    rowWritesLocked ? ACTIVITY_ROW_REASON_ID : undefined
                  }
                  onClick={() => unlinkTransferMutation.mutate(txn.id)}
                  className="rounded-lg p-1 text-accent outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {rowWritesLocked && <DemoLock />}
                  <History className="size-4" aria-hidden />
                </button>
              ) : (
                <button
                  type="button"
                  aria-label="Auto-detect transfer pair"
                  title="Auto-detect transfer pair"
                  disabled={detectTransfer.isPending}
                  // docs/47 §4: `POST /transactions/{id}/detect-transfer` is
                  // the blocked write — same row reason as every other lock.
                  aria-describedby={
                    rowWritesLocked ? ACTIVITY_ROW_REASON_ID : undefined
                  }
                  onClick={() => detectTransfer.mutate(txn.id)}
                  className="rounded-lg p-1 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {rowWritesLocked && <DemoLock />}
                  <History className="size-4" aria-hidden />
                </button>
              )}
              {txn.pair_account_name && (
                <span className="ml-1 text-xs text-accent">
                  → {txn.pair_account_name}
                </span>
              )}
            </>
          )}
        </div>
      </td>
      {/* M64 (docs/09): description trails the core columns so
          Category/Actions land inside a 1280px viewport. */}
      <td className="max-w-64 truncate px-3 py-2 text-muted">
        {txn.raw_description}
        {/* M107 (docs/44 §6.3): a part's own note (children + split editor only). */}
        {isChild && txn.note !== null && txn.note !== "" && (
          <span className="ml-1 text-xs text-accent">· {txn.note}</span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          {(txn.tags ?? []).map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center gap-1 rounded-full border border-accent/20 bg-accent/5 px-1.5 py-0.5 text-[10px] font-medium text-accent"
            >
              {tag.name}
              <button
                type="button"
                // docs/47 §4: `DELETE /transactions/{id}/tags/{tagId}` is the
                // blocked write — padlock bound to the row's one visible reason.
                aria-describedby={
                  rowWritesLocked ? ACTIVITY_ROW_REASON_ID : undefined
                }
                onClick={() => {
                  removeTransactionTag(txn.id, tag.id)
                    .then(() => {
                      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
                    })
                    .catch((error) => {
                      // docs/47 §4: the demo 403 becomes this page's ONE
                      // refusal; every OTHER failure keeps the shape it had
                      // (an unhandled rejection) rather than vanishing.
                      if (demo.onError("Remove tag", error)) return;
                      throw error;
                    });
                }}
                className="ml-0.5 inline-flex items-center gap-0.5 text-accent hover:text-danger"
              >
                {rowWritesLocked && <DemoLock />}
                ×
              </button>
            </span>
          ))}
          {/* docs/47 §4: adding a tag POSTs — the select cannot nest the
              glyph, so it rides the inline wrapper while the control itself
              binds (aria-describedby) to the row's one visible reason. */}
          <span className="inline-flex items-center gap-1">
            {rowWritesLocked && <DemoLock />}
            <select aria-label="Add tag"
              aria-describedby={rowWritesLocked ? ACTIVITY_ROW_REASON_ID : undefined}
              value=""
              onChange={(e) => {
                const tagId = Number(e.target.value);
                if (tagId) {
                  addTransactionTag(txn.id, tagId)
                    .then(() => {
                      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
                    })
                    .catch((error) => {
                      // docs/47 §4: same one-refusal rule as the detach ✕.
                      if (demo.onError("Add tag", error)) return;
                      throw error;
                    });
                }
              }}
              className="field-ring rounded-lg border border-control bg-surface px-1.5 py-0.5 text-[10px] text-muted outline-none"
            >
              <option value="">+ Tag</option>
              {(allTags.data?.items ?? [])
                .filter((t) => !(txn.tags ?? []).some((et) => et.id === t.id))
                .map((tag) => (
                  <option key={tag.id} value={tag.id}>
                    {tag.name}
                  </option>
                ))}
            </select>
          </span>
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <div className="flex justify-end gap-1">
                    {canSplit && (
                      <button
                        type="button"
                        aria-label={`Split transaction ${txn.id}`}
                        title="Split this transaction into parts"
                        onClick={() => {
                          splitSave.reset();
                          setSplitEditor({ parent: txn });
                        }}
                        className="inline-flex min-h-11 items-center rounded px-2 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        Split
                      </button>
                    )}
          <button
            type="button"
            aria-label={`Edit transaction ${txn.id}`}
            onClick={() => openEditor(txn)}
            className="rounded px-2 py-1 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
          >
            Edit
          </button>
          {/* M106 (docs/09 §History drawer): the account activity list is the
              second surface carrying the same two entry points — this row
              control and the edit modal's footer link below. */}
          <button
            type="button"
            aria-label={`History — ${txn.normalized_payee ?? txn.raw_description ?? `#${txn.id}`} (transaction ${txn.id})`}
            title="History"
            onClick={() => setHistoryTx(txn)}
            className="rounded px-2 py-1 text-xs text-muted outline-none hover:bg-veil/[0.06] hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
          >
            History
          </button>
          <button
            type="button"
            aria-label={`Delete transaction ${txn.id}`}
            // docs/47 §4: `DELETE /transactions/{id}` is the blocked write —
            // the padlock rides with the table's one visible row reason.
            aria-describedby={rowWritesLocked ? ACTIVITY_ROW_REASON_ID : undefined}
            onClick={() =>
              deferDelete(toast, {
                title: "Deleted transaction",
                detail:
                  txn.normalized_payee ?? txn.raw_description ?? `#${txn.id}`,
                hide: () =>
                  setPendingDeletes((prev) => {
                    const next = new Set(prev);
                    next.add(txn.id);
                    return next;
                  }),
                restore: () =>
                  setPendingDeletes((prev) => {
                    const next = new Set(prev);
                    next.delete(txn.id);
                    return next;
                  }),
                commit: () => deleteTransaction(txn.id),
                onCommitted: () => {
                  if (offset > 0 && txnTotal <= offset + 1) {
                    setOffset(Math.max(0, offset - PAGE_SIZE));
                  }
                  invalidateLedgerData();
                },
                onError: (message, error) => {
                  // docs/47 §4: the demo 403 becomes this page's ONE refusal
                  // (the raw error rides the second argument here); every
                  // OTHER failure keeps its title + message exactly.
                  if (demo.onError("Delete transaction", error)) return;
                  toast.error("Transaction delete failed", message);
                },
              })
            }
            className="rounded px-2 py-1 text-xs text-danger outline-none hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger"
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
    <>
      {/* docs/47 §4: the table's one refusal, above the table it is about
          (or inside the open editor/split dialog — never both). */}
      {refusalPlace === "page" && refusalNode}
      {/* docs/47 §4 (F-4): the ONE visible reason every row write's padlock
          binds to — same panel, non-zero box, never sr-only. Row controls
          repeat per row; a single reason element keeps the table readable
          (an `aria-describedby` IDREF is shared legally by many controls). */}
      {rowWritesLocked && (
        <DemoReason id={ACTIVITY_ROW_REASON_ID}>
          The demo account is read-only — row changes are not saved.
        </DemoReason>
      )}
      {/* Filter bar — matching Transactions page glass style */}
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Activity</h2>
        <Button type="button" variant="primary" size="sm" onClick={() => openEditor(null)}>
          New transaction
        </Button>
      </div>
      <form onSubmit={applyFilters} className="glass mt-4 rounded-xl p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="text-xs text-muted">
            Quick range
            <RangePicker
              allowAll
              from={draft.from}
              to={draft.to}
              onChange={(r) => setDraft({ ...draft, from: r.from, to: r.to })}
            />
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
              onChange={(e) => setDraft({ ...draft, direction: e.target.value as FilterDraft["direction"] })}
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
          {txnTotal > 0 && (
            <span className="ml-auto text-xs text-muted">
              {rangeStart}–{rangeEnd} of {txnTotal}
            </span>
          )}
        </div>
      </form>
      {/* M125 (docs/09 §Bulk selection toolbar): the SAME toolbar the
          register mounts — inline header row on desktop, sticky bar above the
          mobile bottom nav below `md`, count always announced. Its filter is
          this page's, account id included. */}
      <BulkActionsToolbar
        rows={txnItems}
        selectedIds={selectedIds}
        matched={
          selection.mode === "matched" && matchedTotal !== null
            ? {
                filter: bulkFilter,
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
        tags={allTags.data?.items ?? []}
        onClear={selection.clear}
        onSettled={handleBulkSettled}
      />
      {/* The sticky bar owns the bottom of the screen below `md`, so the table
          and the pager stand down for it — the same `pb-20` rule
          /transactions uses (M96). */}
      <div className={selection.count > 0 ? "pb-20 md:pb-0" : ""}>
      {/* M64 (docs/09): the wrapper owns horizontal scrolling — labelled +
          focusable region, custom track, "Scroll for actions" affordance. */}
      <TableScroller label="Account transactions table" className="mt-4">
        <table className="w-full text-sm">
          <thead className="sticky top-0 z-10 bg-surface/95 backdrop-blur-sm">
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="px-1 py-2.5 font-medium">
                <span className="flex min-h-11 min-w-11 items-center justify-center md:min-h-0 md:min-w-0">
                  {/* M125 (docs/09 §Select-all-matched): the SAME three-state
                      cycle the register's header runs — page → matched → clear —
                      with the next state named in the label and the tooltip. */}
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
              <th className="px-3 py-2.5 font-medium">Date</th>
              <th className="px-3 py-2.5 font-medium">Payee</th>
              <th className="px-3 py-2.5 text-right font-medium">Amount</th>
              <th className="px-3 py-2.5 font-medium">Category</th>
              <th className="px-3 py-2.5 font-medium">Description</th>
              <th className="px-3 py-2.5 font-medium">Tags</th>
              <th className="px-3 py-2.5 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <motion.tbody
            key={staggerKey}
            variants={staggerContainer(0.02)}
            initial="hidden"
            animate="show"
          >
            {transactions.isLoading && (
              <tr>
                <td colSpan={8}>
                  <SkeletonRows rows={8} />
                </td>
              </tr>
            )}
            {transactions.isError && (
              <tr>
                <td colSpan={8} className="p-4">
                  <ErrorPanel message={apiErrorMessage(transactions.error)} />
                </td>
              </tr>
            )}
            {!transactions.isLoading && !transactions.isError && txnItems.length === 0 && (
              <tr>
                <td colSpan={8}>
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
                  layout={ACCOUNT_SPLIT_LAYOUT}
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
                    onToggleGroup: (groupIds) =>
                      selection.toggleMany(
                        groupIds,
                        !groupIds.every((childId) => selection.isSelected(childId)),
                      ),
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
                />
              ) : (
                renderRow(unit.tx, { child: unit.tx.parent_split_id !== null })
              ),
            )}
          </motion.tbody>
        </table>
      </TableScroller>

      {/* Pagination */}
      {txnTotal > PAGE_SIZE && (
        <div className="mt-3 flex items-center justify-between">
          <Button
            variant="ghost"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </Button>
          <span className="text-xs text-muted">
            Page {Math.floor(offset / PAGE_SIZE) + 1} of {Math.ceil(txnTotal / PAGE_SIZE)}
          </span>
          <Button
            variant="ghost"
            size="sm"
            disabled={offset + PAGE_SIZE >= txnTotal}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      )}
      </div>
      {editorOpen && (
        <TransactionEditor
          target={editorTarget}
          defaultAccountId={account.id}
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
          onReceiptChanged={() => invalidateLedgerData()}
        />
      )}
      {/* M106 (docs/09 §History drawer): the row's receipts, opened from the
          account activity list or its edit modal. */}
      {historyTx !== null && (
        <HistoryDrawer
          transactionId={historyTx.id}
          title={historyTx.normalized_payee ?? historyTx.raw_description ?? `#${historyTx.id}`}
          onClose={() => setHistoryTx(null)}
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
    </>
  );
}

function InvestmentAccountView({ account }: { account: Account }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // M118: the chart's marker pins hand their event down to the two tables
  // below (Activity for ledger rows, Trades log for trade rows).
  const [focusEvent, setFocusEvent] = useState<ValueHistoryEvent | null>(null);
  return (
    <section className="space-y-6">
      {/* Header */}
      <div className="flex items-start gap-4">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-accent/10">
          <TrendingUp className="size-6 text-accent" aria-hidden />
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-2xl font-semibold">{account.name}</h1>
          <div className="mt-1 flex items-center gap-2 text-sm text-muted">
            <span className="rounded-md bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
              {TYPE_LABELS[account.type as AccountType] ?? account.type}
            </span>
            <span>{account.currency}</span>
          </div>
        </div>
      </div>

      {/* M118 (docs/09 §Account value history): the value timeline is the
          FIRST module — above holdings, full width, ~280px, ChartFrame. It is
          the account's own story (cash + holdings per period, markers for the
          contributions/withdrawals/transfers that moved it). */}
      <AccountValueChart
        accountId={account.id}
        onSelectEvent={setFocusEvent}
        selectedEventId={focusEvent?.id ?? null}
      />

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card hover className="p-4">
          <CardTitle>Total value</CardTitle>
          <div className="mt-2 text-2xl font-bold">{formatMoney(account.total_value, account.currency)}</div>
        </Card>
        <Card hover className="p-4">
          <CardTitle>Investments</CardTitle>
          <div className="mt-2 text-2xl font-bold">{formatMoney(account.investment_value, account.currency)}</div>
        </Card>
        <Card hover className="p-4">
          <CardTitle>Cash</CardTitle>
          <div className="mt-2 text-2xl font-bold">{formatMoney(account.cash_balance, account.currency)}</div>
        </Card>
      </div>

      <Investments
        accountId={account.id}
        showHeader={false}
        focusTrade={focusEvent?.source === "trade" ? focusEvent : null}
      />

      <Card hover className="p-4">
        <CardTitle>Activity</CardTitle>
        <div className="mt-3">
          <AccountActivity account={account} focusEvent={focusEvent} />
        </div>
      </Card>
    </section>
  );
}
