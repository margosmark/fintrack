// Dashboard (docs/09): KPI strip (net worth, range net, positions value) with
// animated numbers + delta chips, spending-by-category donut with center
// label, net-worth trend with pulsing active dot, recent transactions.
// Charts read the docs/12 pinned shapes from /reports/*; failures degrade to
// the pinned "No data in range" panels instead of crashing.

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Inbox } from "lucide-react";
import { Transaction, listTransactions, mergeTransactionSplit } from "../api/transactions";
import {
  INBOX_QUERY_KEY,
  criticalBudgetItems,
  getNotificationInbox,
  markNotificationRead,
} from "../api/notifications";
import { apiErrorMessage, formatWithCad } from "../components/transactions/format";
import { SplitGroupRow, groupSplitRows } from "../components/transactions/SplitGroupRow";
import { useToast } from "../components/ui/Toast";
import { CategoriesDonut } from "../components/charts/CategoriesDonut";
import { NetWorthAreaChart } from "../components/charts/NetWorthAreaChart";
import { Card, CardTitle } from "../components/ui/Card";
import { AnimatedNumber, fmtCadCompact } from "../components/ui/AnimatedNumber";
import { fmtBucket, fmtCad } from "../components/charts/format";
import { ComparisonChip } from "../components/ui/ComparisonChip";
import { usePublishPageMeta } from "../lib/pageMeta";
import { EmptyState } from "../components/ui/EmptyState";
import { QueryErrorPanel } from "../components/ui/QueryErrorPanel";
import { HelpHint } from "../components/ui/HelpHint";
import { SegmentedControl, type SegmentedOption } from "../components/ui/SegmentedControl";
import { useAuth } from "../auth/AuthContext";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  useDemoRefusal,
  useDemoWriteLock,
} from "../components/ui/DemoLock";
import { updateMe } from "../api/users";
import type { DashboardPreset } from "../types";
import { Skeleton } from "../components/ui/Skeleton";
import { StateRow } from "../components/ui/StateRow";
import { BudgetCards } from "../components/reports/BudgetCards";
import { SubscriptionsPanel } from "../components/reports/SubscriptionsPanel";
import { InsightsCard } from "../components/reports/InsightsCard";
import { RangePicker, rangeFor } from "../components/ui/RangePicker";
import { granularityForWindow } from "../lib/chartRange";
import { getCashflow, getNetWorth, getSpendingCategories } from "../api/reports";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { MASK_TEXT, isMasked, maskAmountsInText, useMasked } from "../lib/mask";

/**
 * `aria-describedby` target of the preset picker's padlock (docs/47 §4) —
 * every segment binds to this ONE visible reason in the header row.
 */
const PRESET_REASON_ID = "dashboard-preset-demo-reason";

/** docs/12 §Endpoints — shapes pinned in src/api/reports.ts; the S2a/S2b
 *  client engines return them unchanged (network count: 0 report calls). */

/**
 * Delta chips share the KPI header row with a `whitespace-nowrap` label, so at
 * the 1280px reference width they must stay short (M69/G6): compact at ANY
 * magnitude (`$5.9K`, `$276`) instead of `fmtCadCompact`'s "standard below
 * $10k", which rendered `$5,743.07` and pushed the chip out of its card.
 *
 * The row WRAPS rather than clips: at the 1280px reference width a 4-up card
 * is 288px, and the longest label ("Positions value" + hint) plus a full chip
 * exceed it — a fixed single line hard-clipped the chip's basis mid-glyph
 * ("vs N"). With `flex-wrap` the chip drops to its own line (right-aligned
 * via the wrapper's `ml-auto`) whenever the pair does not fit, at every
 * width and zoom; the amount↔basis gap inside the chip is never squeezed
 * away to make the row fit (t_6263190b).
 *
 * M139/M140 (docs/46 §D2): a delta is an absolute CAD amount — this builder
 * formats currency itself rather than calling the shared helpers, so it has to
 * consult the session mask itself. It was the one currency formatter on the
 * Dashboard that did not, which is how the KPI chips kept printing `$5.9K`
 * beside a masked net worth.
 */
function fmtDelta(n: number): string {
  if (isMasked()) return MASK_TEXT;
  const abs = Math.abs(n);
  return new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency: "CAD",
    notation: "compact",
    maximumFractionDigits: abs >= 1000 ? 1 : 0,
  }).format(n);
}

/**
 * Delta chip vs the previous bucket: semantic green/red only, arrow by
 * direction; hidden entirely when no prior bucket exists.
 */
/**
 * M185 (docs/47 §9.3 / G1): a comparison NEVER appears without its basis. The
 * basis is the PREVIOUS BUCKET'S OWN DATE (payload, never the wall clock), and
 * the accessible text is the full sentence, not the arrow glyph (§6.5).
 */
function DeltaChip({
  current,
  previous,
  basis,
  label,
}: {
  current: number;
  previous?: number;
  /** The previous bucket's payload date, already formatted ("vs Sep"). */
  basis?: string;
  /** The KPI's name — the sentence assistive tech reads. */
  label?: string;
}) {
  if (previous === undefined || Number.isNaN(previous)) return null;
  const delta = current - previous;
  const up = delta >= 0;
  const magnitude = fmtDelta(Math.abs(delta));
  if (basis === undefined) return null;
  return (
    <ComparisonChip
      delta={magnitude}
      tone={up ? "gain" : "loss"}
      basis={basis}
      ariaLabel={
        label !== undefined
          ? `${label} ${up ? "up" : "down"} ${magnitude} ${basis.replace(/^vs\s/, "versus ")}`
          : undefined
      }
    />
  );
}

/** M97 (docs/09 §Dashboard layout presets): the six cards of the dashboard,
 *  named. A preset reorders these — it never creates, removes or hides one. */
export type CardKey =
  | "kpi"
  | "insights"
  | "budgets"
  | "subscriptions"
  | "charts"
  | "recent";

/** The three fixed orders. `overview` is TODAY's default, byte-for-byte — the
 *  G1 tour teaches on it, so first visit never changes what its marks point
 *  at (docs/10 §M97 rejected scope: no drag-and-drop, no card hiding, no
 *  custom card sets). The other two are "the rest below" in default order. */
export const CARD_ORDER: Record<DashboardPreset, readonly CardKey[]> = {
  overview: ["kpi", "insights", "budgets", "subscriptions", "charts", "recent"],
  import_first: ["recent", "kpi", "insights", "budgets", "subscriptions", "charts"],
  budget_first: ["budgets", "insights", "kpi", "subscriptions", "charts", "recent"],
};

const PRESETS = ["overview", "import_first", "budget_first"] as const;

function isPreset(value: unknown): value is DashboardPreset {
  return (PRESETS as readonly unknown[]).includes(value);
}

const PRESET_OPTIONS: readonly SegmentedOption<DashboardPreset>[] = [
  { value: "overview", label: "Overview" },
  { value: "import_first", label: "Import first" },
  { value: "budget_first", label: "Budget first" },
];

function Kpi({
  label,
  value,
  format,
  accentGlow = false,
  children,
}: {
  label: ReactNode;
  value: number | null;
  format: (n: number) => string;
  accentGlow?: boolean;
  children?: ReactNode;
}) {
  return (
    <motion.div variants={fadeUp}>
      <Card hover className="p-4">
        <div className="flex min-h-6 flex-wrap items-start justify-between gap-2">
          <p className="whitespace-nowrap text-xs font-medium uppercase tracking-widest text-muted">{label}</p>
          {children !== undefined && <div className="ml-auto">{children}</div>}
        </div>
        <p
          className={`mt-2 font-mono text-2xl font-semibold tracking-tight ${
            accentGlow ? "text-accent glow-text" : ""
          }`}
        >
          {value === null ? <Skeleton className="h-8 w-32" /> : <AnimatedNumber value={value} format={format} />}
        </p>
      </Card>
    </motion.div>
  );
}

export function Dashboard() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const initial = useMemo(() => rangeFor("YTD"), []);
  const [range, setRange] = useState(initial);
  // M185 (docs/47 §1.3): the truth bar's range segment is this page's OWN
  // state — published from the payload-driven picker, never a wall clock.
  usePublishPageMeta({ range });

  const categories = useQuery({
    queryKey: ["reports", "spending-categories", range.from, range.to],
    queryFn: () => getSpendingCategories(range.from, range.to),
    retry: false,
    staleTime: 60_000,
  });

  // M126 (docs/12 §Range → granularity): the bucket size FOLLOWS the page
  // range. A hard-coded `month` gave a 1W range a single monthly bucket — the
  // axis was technically correct and practically useless.
  const netWorth = useQuery({
    queryKey: ["reports", "net-worth", range.from, range.to],
    queryFn: () => getNetWorth(range.from, range.to, granularityForWindow(range.from, range.to)),
    retry: false,
    staleTime: 60_000,
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });
  const recent = useQuery({
    queryKey: ["transactions", "recent"],
    queryFn: () => listTransactions({}, { limit: 10 }),
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });

  const donutItems = categories.data?.items ?? [];

  // Deliberately still `month`: this payload feeds the "Range net" KPI and
  // its delta chip (latest bucket vs the one before), not a chart — daily
  // buckets would turn that chip into a day-over-day number under a label
  // that says "Range net". The formula applies where the buckets are DRAWN
  // (net worth below); docs/12 §Range → granularity pins both halves.
  // M138 D-5: the KPI SUMS every bucket in the range (`monthNet` below), so
  // the old "Month net" label simply lied about any range wider than a month.
  const cashflow = useQuery({
    queryKey: ["reports", "cashflow", range.from, range.to],
    queryFn: () => getCashflow(range.from, range.to, "month"),
    retry: false,
    staleTime: 60_000,
  });

  const buckets = netWorth.data?.buckets ?? [];
  const lastBucket = buckets.at(-1);
  const prevBucket = buckets.at(-2);
  const netWorthValue = lastBucket === undefined ? null : Number(lastBucket.net_cad);
  const positionsValue = lastBucket === undefined ? null : Number(lastBucket.positions_cad);
  // M138 D-5: a "Positions value $0.00" card is noise on an account set with
  // no holdings. Hide it once we KNOW there is nothing to show; while the
  // query is still pending keep the slot (with its skeleton) so the KPI row
  // does not reflow under the reader.
  const showPositionsKpi =
    netWorth.isPending || (positionsValue !== null && positionsValue !== 0);
  // Period net = income − expenses across the selected range (cashflow
  // report); stays correct for single-bucket ranges where a net-worth
  // delta is undefined.
  const monthNet = cashflow.data
    ? cashflow.data.buckets.reduce((sum, b) => sum + Number(b.net_cad), 0)
    : null;
  // M69 (G6, docs/09 line 9 — "delta chips" plural): MONTH NET gets the same
  // "delta vs previous bucket" contract the other KPI chips use, measured on
  // the cashflow buckets that produce `monthNet` (latest month vs the one
  // before it). Hidden on a single-bucket range, exactly like the others.
  const cfBuckets = cashflow.data?.buckets ?? [];
  const cfLast = cfBuckets.at(-1);
  const cfPrev = cfBuckets.at(-2);

  const recentItems = recent.data?.items ?? [];
  /** M107 (docs/44 D11): the group headers riding the SAME payload. */
  const recentUnits = groupSplitRows(recentItems, recent.data?.split_parents ?? []);

  // M51 (docs/35): the critical-alert banner reads the SAME inbox query the
  // AppShell badge uses. The server excludes `budget_threshold` entirely when
  // its toggle is OFF, so "respects the toggle" needs no client settings read.
  const inbox = useQuery({
    queryKey: INBOX_QUERY_KEY,
    queryFn: () => getNotificationInbox(50),
    staleTime: 30_000,
    retry: false,
  });
  const queryClient = useQueryClient();
  const toast = useToast();
  // docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal for this page's
  // writes (preset save, banner mark-read, split merge). A refused demo write
  // is one `role="status"` element + one `toast.info` — never `toast.error`,
  // which renders AS `role="alert"`.
  const demo = useDemoRefusal();
  // The preset picker's action IS the blocked write (`PATCH /users/me`), so on
  // the demo tenant every segment is bound to the padlock + the visible reason.
  const presetLocked = useDemoWriteLock("PATCH", "/users/me");
  const criticalBudget = criticalBudgetItems(inbox.data);
  const dismissCritical = useMutation({
    // Marks read exactly the items the banner is showing — nothing else.
    mutationFn: (ids: string[]) => Promise.all(ids.map((id) => markNotificationRead(id))),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: INBOX_QUERY_KEY });
    },
    onError: (err) => {
      if (demo.onError("Mark read", err)) return;
      toast.error("Could not mark as read", apiErrorMessage(err));
    },
  });

  /** M107: the one split group whose parts panel is open in this list. */
  const [expandedSplit, setExpandedSplit] = useState<number | null>(null);
  /** Merge back — one DELETE, then the ledger caches re-read (docs/44 §5). */
  const splitMerge = useMutation({
    mutationFn: (parentId: number) => mergeTransactionSplit(parentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["reports"] });
      void queryClient.invalidateQueries({ queryKey: ["budgets"] });
      void queryClient.invalidateQueries({ queryKey: ["search"] });
      setExpandedSplit(null);
      toast.success("Split merged back");
    },
    onError: (err) => {
      if (demo.onError("Merge split", err)) return;
      toast.error("Merge failed", apiErrorMessage(err));
    },
  });

  /**
   * One ordinary ledger row in the recent list — the SAME markup a split
   * child renders with inside the parts panel (docs/44 §6.3).
   */
  function renderRecentRow(transaction: Transaction): ReactNode {
    return (
    <motion.li
      key={transaction.id}
      variants={fadeUp}
      className="flex items-center rounded px-1 py-2 text-sm transition-colors hover:bg-veil/[0.03]"
    >
      {/* F6: payee and date each own their line inside the flexible
          column, so a long merchant name or a raised Android font
          scale can ellipsize the payee — never the date. The date
          + amount no longer compete for the payee's line, which is
          what made 360px rows cut text even with demo data. */}
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="min-w-0 truncate font-medium">
          {transaction.normalized_payee}
        </span>
        <span className="text-muted">{transaction.date}</span>
      </span>
      <span
        className={`shrink-0 pl-4 tabular ${
          transaction.amount_native.startsWith("-") ? "text-danger" : ""
        }`}
      >
        {formatWithCad(
          transaction.amount_native,
          transaction.currency,
          transaction.cad_amount,
        )}
      </span>
    </motion.li>
    );
  }


  /** The cards themselves, unchanged — a preset only picks an order. */
  const cards: Record<CardKey, ReactNode> = {
    // KPI strip
    kpi: (
          <motion.div
            variants={staggerContainer(0.06)}
            initial="hidden"
            animate="show"
            className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
          >
            <Kpi
              label={
                <>
                  Net worth
                  {/* M97 honest derivation: the tooltip carries the two numbers the
                      sum is made of (assets − liabilities, docs/12 §4) plus the
                      exclusions, instead of the old "sum of balances" definition —
                      which also never said what happens to an account whose FX
                      rate or holding price is missing. The as-of date is MIN(bucket
                      end, range end): the last month bucket's end is in the future
                      mid-month, while its data is only as new as the range. */}
                  {lastBucket !== undefined && (
                    <HelpHint
                      text={`Assets − Liabilities, as of ${
                        lastBucket.bucket_end <= range.to ? lastBucket.bucket_end : range.to
                      }: assets ${fmtCad(lastBucket.assets_cad)} − liabilities ${fmtCad(
                        lastBucket.liabilities_cad,
                      )}. Credit cards, lines of credit and overdrafts are negative cash, so they count as liabilities. An account with an unresolved exchange rate, or a holding with no cached price, is excluded — never fetched.`}
                    />
                  )}
                </>
              }
              value={netWorthValue}
              format={fmtCadCompact}
              accentGlow
            >
              {lastBucket !== undefined && prevBucket !== undefined && (
                <DeltaChip
                  current={Number(lastBucket.net_cad)}
                  previous={Number(prevBucket.net_cad)}
                  // The payload's OWN bucket_end — a date, never the clock.
                  basis={`vs ${fmtBucket(prevBucket.bucket_end, granularityForWindow(range.from, range.to))}`}
                  label="Net worth"
                />
              )}
            </Kpi>
            <Kpi
              label={
                <>
                  Range net
                  <HelpHint text="Income minus spending summed over EVERY bucket in the selected range (from the range's start to its end), from committed cash-flow rows only: buys, sells and transfers move money around but are not spending. The delta chip compares the last bucket with the one before it." />
                </>
              }
              value={monthNet}
              format={(n) =>
                `${n < 0 ? "−" : "+"}${fmtCadCompact(Math.abs(n)).replace("-", "")}`
              }
            >
              {cfLast !== undefined && cfPrev !== undefined && (
                <DeltaChip
                  current={Number(cfLast.net_cad)}
                  previous={Number(cfPrev.net_cad)}
                  basis={`vs ${fmtBucket(cfPrev.bucket, "month")}`}
                  label="Range net"
                />
              )}
            </Kpi>
            {showPositionsKpi && (
              <Kpi
                label={
                  <>
                    Positions value
                    <HelpHint text="Market value of every holding at the period end: newest cached price on or before that date, converted at the cached exchange rate. A holding with no price, or an account with no rate, is excluded — never fetched. Hidden entirely when there are no positions to value." />
                  </>
                }
                value={positionsValue}
                format={fmtCadCompact}
              >
                {lastBucket !== undefined && prevBucket !== undefined && (
                  <DeltaChip
                    current={Number(lastBucket.positions_cad)}
                    previous={Number(prevBucket.positions_cad)}
                    basis={`vs ${fmtBucket(prevBucket.bucket_end, granularityForWindow(range.from, range.to))}`}
                    label="Positions value"
                  />
                )}
              </Kpi>
            )}
            <Kpi
              label={
                <>
                  Cash
                  <HelpHint text="Every account's ledger balance — starting balance plus committed transactions — converted to CAD. Credit cards and lines of credit show up here as negative cash." />
                </>
              }
              value={lastBucket === undefined ? null : Number(lastBucket.cash_cad)}
              format={fmtCadCompact}
            />
          </motion.div>

      
    ),

    // Spending insights
    insights: (
          <InsightsCard />

      
    ),

    // Budget progress
    budgets: (
          <BudgetCards />

      
    ),

    // Subscriptions
    subscriptions: (
          <SubscriptionsPanel />

      
    ),

    // Charts
    charts: (
          <motion.div
            variants={staggerContainer(0.08)}
            initial="hidden"
            animate="show"
            className="grid gap-4 lg:grid-cols-2"
          >
            <motion.div variants={fadeUp}>
              <Card hover className="p-4">
                <CardTitle>Spending by category (CAD)</CardTitle>
                {categories.isPending ? (
                  <StateRow variant="loading" title="Loading" />
                ) : categories.isError ? (
                  <QueryErrorPanel query={categories} />
                ) : donutItems.length === 0 ? (
                  <EmptyState
                    icon={<Inbox />}
                    title="No data in range"
                    hint="Import statements or adjust the range to see spending."
                    preview={{
                      src: "/help/dashboard.png",
                      alt: "The Dashboard with a spending donut, a net-worth trend and recent transactions",
                    }}
                    action={{
                      label: "Import a statement",
                      to: "/imports",
                      ariaLabel: "Import a statement — No data in range",
                    }}
                  />
                ) : (
                  <>
                    <CategoriesDonut
                      items={donutItems}
                      total={categories.data?.total_cad}
                      centerLabel="total spent"
                      title="Spending by category"
                    />
                  </>
                )}
              </Card>
            </motion.div>

            <motion.div variants={fadeUp}>
              <Card hover className="p-4">
                <CardTitle>Net worth (CAD)</CardTitle>
                {netWorth.isPending ? (
                  <StateRow variant="loading" title="Loading" />
                ) : netWorth.isError ? (
                  <QueryErrorPanel query={netWorth} />
                ) : buckets.length === 0 ? (
                  <EmptyState
                    icon={<Inbox />}
                    title="No data in range"
                    hint="Record transactions and trades to build your net-worth trend."
                    preview={{
                      src: "/help/dashboard.png",
                      alt: "The Dashboard with a spending donut, a net-worth trend and recent transactions",
                    }}
                    action={{ label: "Record a transaction", to: "/transactions" }}
                  />
                ) : (
                  <div className="mt-2">
                    <NetWorthAreaChart
                      buckets={buckets}
                      granularity={netWorth.data?.granularity ?? "month"}
                    />
                  </div>
                )}
              </Card>
            </motion.div>
          </motion.div>

      
    ),

    // Recent transactions
    recent: (
          <motion.div variants={fadeUp} initial="hidden" animate="show">
            <Card hover className="p-4">
              <CardTitle>Recent transactions</CardTitle>
              {recent.isPending && (
                <div className="space-y-2.5">
                  {Array.from({ length: 5 }, (_, i) => (
                    <Skeleton key={i} className="h-6" style={{ width: `${92 - i * 9}%` }} />
                  ))}
                </div>
              )}
              {recent.isError && <QueryErrorPanel query={recent} />}
              {recent.data !== undefined && recentItems.length === 0 && (
                <EmptyState
                  icon={<Inbox />}
                  title="No transactions yet"
                  hint="Import your first statement to get started."
                  preview={{
                    src: "/help/dashboard.png",
                    alt: "The Dashboard with a spending donut, a net-worth trend and recent transactions",
                  }}
                  action={{
                    label: "Import a statement",
                    to: "/imports",
                    ariaLabel: "Import a statement — No transactions yet",
                  }}
                />
              )}
              {recentItems.length > 0 && (
                <motion.ul
                  variants={staggerContainer(0.03)}
                  initial="hidden"
                  animate="show"
                  className="divide-y divide-veil/[0.05]"
                >
                  {recentUnits.map((unit) =>
                    unit.kind === "group" ? (
                      <SplitGroupRow
                        key={unit.key}
                        group={unit.group}
                        variant="list"
                        expanded={expandedSplit === unit.group.parent.id}
                        onToggleExpand={() =>
                          setExpandedSplit((current) =>
                            current === unit.group.parent.id ? null : unit.group.parent.id,
                          )
                        }
                        renderRow={renderRecentRow}
                        merge={{
                          pending: splitMerge.isPending,
                          onMerge: () => splitMerge.mutate(unit.group.parent.id),
                        }}
                      />
                    ) : (
                      renderRecentRow(unit.tx)
                    ),
                  )}
                </motion.ul>
              )}
            </Card>
          </motion.div>
    ),
  };

  // M97: which of the three fixed orders the cards render in. The SERVER
  // owns it (`users.dashboard_preset`) so it roams across devices; this
  // local copy keeps the switch instant and rolls the change back when the
  // write is refused — the demo tenant's 403 arrives here as a real
  // explanation instead of a layout that silently forgets itself.
  const { user, setUser } = useAuth();
  const [preset, setPreset] = useState<DashboardPreset>(
    isPreset(user?.dashboard_preset) ? user.dashboard_preset : "overview",
  );
  useEffect(() => {
    if (isPreset(user?.dashboard_preset)) setPreset(user.dashboard_preset);
  }, [user]);
  const changePreset = (next: DashboardPreset) => {
    if (next === preset) return;
    const previous = preset;
    setPreset(next);
    updateMe({ dashboard_preset: next })
      .then((updated) => setUser(updated))
      .catch((error: unknown) => {
        // The layout ALWAYS rolls back on a refusal (M97): a local order the
        // server refused would be the UI drifting from the boundary.
        setPreset(previous);
        // docs/47 §4: a demo refusal is the page's ONE refusal + one info
        // toast, never the error path (an error toast renders `role="alert"`).
        if (demo.onError("Save layout", error)) return;
        toast.error("Could not save that layout", apiErrorMessage(error));
      });
  };

  return (
    <section>
      {/* docs/47 §4: the page's ONE refusal, above the picker it explains.
          Wrapper carries the gap so a live tenant's layout is untouched. */}
      {demo.refusal !== null && (
        <div className="mb-4">
          <DemoRefusal id={demo.refusalId} action={demo.refusal} />
        </div>
      )}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {/* M97 (docs/09 §Dashboard layout presets): the picker lives here so
              it stays reachable at 390px — it only reorders the cards below. */}
          {/* docs/47 §4: the segments' ACTION is the blocked write, so the
              group wears the padlock, every segment stays ENABLED and binds
              itself to the visible reason below (F-4). */}
          <span className="inline-flex items-center gap-1.5">
            {presetLocked && <DemoLock />}
            <SegmentedControl
              options={PRESET_OPTIONS}
              value={preset}
              onChange={changePreset}
              layoutId="dashboard-preset"
              size="sm"
              ariaLabel="Dashboard layout"
              ariaDescribedBy={presetLocked ? PRESET_REASON_ID : undefined}
            />
          </span>
          {/* div, not p: RangePicker renders a SegmentedControl of buttons and
              a <div> inside a <p> is invalid nesting (React warns, and the HTML
              parser would auto-close the paragraph around it). */}
          <div className="text-sm text-muted">
            <RangePicker from={range.from} to={range.to} onChange={setRange} />
          </div>
        </div>
      </div>
      {/* docs/47 §4 (F-4): the VISIBLE reason every segment's padlock binds
          to — own line under the row, non-zero box, never sr-only. */}
      {presetLocked && (
        <div className="mt-1">
          <DemoReason id={PRESET_REASON_ID}>
            The demo account is read-only — the layout is not saved.
          </DemoReason>
        </div>
      )}

      {/* Critical budget alerts (M51, docs/35). Gated SERVER-SIDE by the
          `budget_threshold` toggle: with it OFF these items are not in the
          inbox at all, so no banner renders and no settings fetch is needed. */}
      {criticalBudget.length > 0 && (
        <motion.div variants={fadeUp} initial="hidden" animate="show" className="mt-4">
          <div
            role="alert"
            className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-danger/40 bg-danger/[0.07] p-4"
          >
            <div className="flex min-w-0 items-start gap-3">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
              <div className="min-w-0">
                <p className="text-sm font-semibold text-text">
                  {criticalBudget.length === 1
                    ? "Budget threshold breached"
                    : `${criticalBudget.length} budget thresholds breached`}
                </p>
                <ul className="mt-1 space-y-0.5">
                  {criticalBudget.map((item) => (
                    <li key={item.id} className="text-sm text-muted">
                      {/* The body is server-composed money PROSE ("Spent 312.84
                          of 150.00 CAD…") — the exact docs/46 §D4 shape the
                          inbox already runs through `maskAmountsInText`
                          (AppShell.tsx). Rendering it raw put real dollar
                          figures beside a live region saying "Amounts masked"
                          (F-1). The TITLE goes through the same helper: it
                          leaves non-amount prose untouched ("Budget alert:
                          Groceries" prints unchanged) and the line is one run
                          of text, so an amount-shaped token inside a category
                          name cannot bypass the mask either. The banner
                          subscribes via `useMasked()` above, so a flip
                          re-renders this line in place (docs/46 §D6). */}
                      {maskAmountsInText(item.title)} — {maskAmountsInText(item.body)}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            <button
              type="button"
              onClick={() => dismissCritical.mutate(criticalBudget.map((item) => item.id))}
              disabled={dismissCritical.isPending}
              className="shrink-0 rounded-lg border border-danger/40 px-3 py-1.5 text-xs font-medium text-danger outline-none transition-colors hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-40"
            >
              Mark read
            </button>
          </div>
        </motion.div>
      )}

      {/* M97 (docs/09 §Dashboard layout presets): the SAME cards, in the
          preset's order — nothing is created, removed or hidden, and the
          DOM order IS the visual order at every width (a CSS `order`
          reflow would separate what a screen reader reads from what is
          drawn). Section rhythm (docs/09 §Dashboard layout): EVERY slot
          carries the same `mt-4`, so sections are separated by one
          consistent gap in every preset — three of the six cards (insights,
          budgets, subscriptions) ship no margin of their own, and the old
          leading-slot-only rule left them butting against the card above. */}
      {CARD_ORDER[preset].map((key) => (
        <div key={key} className="mt-4">
          {cards[key]}
        </div>
      ))}
          </section>
  );
}
