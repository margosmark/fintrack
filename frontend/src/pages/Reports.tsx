import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { ApiError } from "../api/client";
import { updateMe } from "../api/users";
import {
  GRANULARITIES,
  breakdownSlices,
  downloadCsv,
  getCategoryTagMatrix,
  getCashflow,
  getInvestmentPerformance,
  getNetWorth,
  getSpending,
  getSpendingCategories,
  toSlices,
} from "../api/reports";
import type {
  Granularity,
  ReportGroupBy as ReportDimension,
  ReportScope,
  SpendingReport,
} from "../api/reports";
import { REPORT_TABS, isReportTab, type ReportTab } from "../lib/reportTabs";
import { getPartnerState } from "../api/sharing";
import { useAuth } from "../auth/AuthContext";
import type { SavedReportView, SavedReportViewState, User } from "../types";
import { BillHistoryBarChart } from "../components/charts/BillHistoryBarChart";
import { CategoriesDonut } from "../components/charts/CategoriesDonut";
import { CashflowComposedChart } from "../components/charts/CashflowComposedChart";
import { NetWorthAreaChart } from "../components/charts/NetWorthAreaChart";
import { NoDataPanel } from "../components/charts/NoDataPanel";
import { SpendingStackedBarChart } from "../components/charts/SpendingStackedBarChart";
import { SpendingTreemap } from "../components/charts/SpendingTreemap";
import { CategoryTagMatrixPanel } from "../components/reports/CategoryTagMatrixPanel";
import { DividendCalendarPanel } from "../components/reports/DividendCalendarPanel";
import { DividendsPanel } from "../components/reports/DividendsPanel";
import { ProjectionsPanel } from "../components/reports/ProjectionsPanel";
import { SavedViewsControl } from "../components/reports/SavedViewsControl";
import { SankeyPanel } from "../components/reports/SankeyPanel";
import { UnrealizedBarChart } from "../components/charts/UnrealizedBarChart";
import { fmtBucket, fmtCad, fmtQuantity, toNumber } from "../components/charts/format";
import { Button } from "../components/ui/Button";
import { RangePicker, rangeFor } from "../components/ui/RangePicker";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import { StateRow } from "../components/ui/StateRow";
import { EASE } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { useMasked } from "../lib/mask";

/**
 * Reports page (docs/09 row + docs/12 §Chart mapping exactly): one tab per
 * report family, granularity switcher, YTD-defaulting date range with
 * quick-picks, CSV downloads and "No data in range" panels everywhere.
 * Presentation pass: morphing tab underline, segmented granularity control,
 * route-level transition between tabs.
 */

// M95 P1: the tab ids/labels moved to `lib/reportTabs.ts` so the ⌘K command
// palette can list every tab as a jump target without importing this lazy page
// module into the shell's chunk — one array, both consumers, no drift.
type Tab = ReportTab;

const TABS: readonly { id: Tab; label: string }[] = REPORT_TABS;

/** Tabs whose endpoints take `granularity` (docs/12 §Endpoints). */
const GRANULAR_TABS: readonly Tab[] = ["spending", "cashflow", "networth"];

function isIsoDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

interface SortState {
  key: PerfColumn;
  desc: boolean;
}

type PerfColumn = "symbol" | "quantity" | "realized_cad" | "unrealized_cad" | "market_value_cad";

const PERF_COLUMNS: readonly PerfColumn[] = [
  "symbol",
  "quantity",
  "realized_cad",
  "unrealized_cad",
  "market_value_cad",
];

/** M108 P1: the Spending tab's two chart variants (docs/12 §Chart mapping). */
type SpendingView = "bars" | "treemap";

/**
 * The Group-by control's three settings (M124, docs/12 §1).
 *
 * `tag` is M124's addition and is available wherever the control is, because
 * tags need no partnership and no second query surface. `owner` stays
 * conditional on a link (M108 P3) — a lone tenant never sees a dimension it
 * cannot fill.
 *
 * Wider than the API's `ReportGroupBy` (category|tag) by exactly `owner`,
 * which has its own query path through the M93 partner projection.
 */
type GroupByOption = "category" | "tag" | "owner";

/** Tabs the Group-by control serves — see docs/12 for WHY it is these two. */
const OWNER_TABS: readonly Tab[] = ["spending", "categories"];

/** M124 R2 (docs/12 §2): an in-page drill into ONE category's periods. */
interface DrillState {
  /** `null` = the uncategorized bucket; otherwise a category id. */
  categoryId: number | null;
  name: string;
}

/**
 * The drill view's chart (M124 R2, docs/12 §2).
 *
 * Built on `BillHistoryBarChart` — the same component the Bills tab's history
 * chart uses — because it is already the house bar-chart-over-time with a
 * `<details>` table fallback, an aria sentence and zero-filled periods
 * (docs/09 §Accessibility: one chart family, one table precedent). The only
 * thing it needs from a category drill is its items, which carry no payment
 * `count`, so that column hides rather than printing zeros.
 */
function CategoryDrillChart({
  items,
  granularity,
  name,
}: {
  items: SpendingReport["items"];
  granularity: Granularity;
  name: string;
}) {
  const points = items.map((item) => ({
    period: item.bucket,
    label: fmtBucket(item.bucket, granularity),
    total: item.total_cad,
  }));
  return (
    <BillHistoryBarChart
      points={points}
      granularity={granularity}
      title={`${name} — spending by period`}
      emptyMessage="Nothing was spent in this category during the selected range."
    />
  );
}

/** M159: the Owner lens' chart title — it says WHICH population, not which
 *  owner, because the server-computed household scope sums both (docs/12
 *  §Owner dimension). */
const HOUSEHOLD_TITLE = "Spending by category — own + shared household";

export function Reports() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const initial = useMemo(() => rangeFor("YTD"), []);
  const toast = useToast();
  // M95 P1 — the palette deep-links a tab (`/reports?tab=<id>`). Read on mount,
  // followed when the param changes in-page, and every tab click writes it
  // back with `replace` so Back leaves the page instead of walking the tabs.
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTabState] = useState<Tab>(() => {
    const param = searchParams.get("tab");
    return isReportTab(param) ? param : "spending";
  });
  useEffect(() => {
    const param = searchParams.get("tab");
    if (isReportTab(param) && param !== tab) setTabState(param);
  }, [searchParams, tab]);
  const setTab = (next: Tab) => {
    setTabState(next);
    setSearchParams({ tab: next }, { replace: true });
  };
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  const [granularity, setGranularity] = useState<Granularity>("month");
  const [sort, setSort] = useState<SortState>({ key: "market_value_cad", desc: true });
  /** M108 P1: the Spending tab's chart variant — part of the saved filter
   *  state, so a preset restores it with the range (docs/09). */
  const [spendingView, setSpendingView] = useState<SpendingView>("bars");
  /** M108 P2: the Dividends tab's year, owned HERE rather than inside
   *  `DividendsPanel` so a preset can restore it (docs/09). `undefined` is the
   *  panel's "Latest". */
  const [dividendsYear, setDividendsYear] = useState<number | undefined>(undefined);
  /** M108 P3: the Owner dimension's switch. Reset to `category` whenever the
   *  link disappears, so a revoked partnership can never leave the page asking
   *  for rows it may no longer fetch. */
  const [groupBy, setGroupBy] = useState<GroupByOption>("category");
  /** M124 R2 (docs/12 §2): an in-page drill into one category's periods.
   *  Page-level so a slice clicked on Categories and a block clicked on
   *  Spending both land in the same view; cleared on tab change so the
   *  breadcrumb's "back" always means the tab you came from. */
  const [drill, setDrill] = useState<DrillState | null>(null);

  const { user, setUser } = useAuth();
  const savedViews = user?.saved_report_views ?? [];

  const rangeValid = isIsoDate(from) && isIsoDate(to) && from <= to;

  // M124: the dimension is part of the query key, so switching Category↔Tag
  // refetches (recomputes) rather than silently re-labeling the old numbers.
  // Owner has its own queries below (it needs the M93 partner rows), so only
  // the two engine-servable dimensions reach `getSpending`/`getSpendingCategories`.
  const engineDimension: ReportDimension = groupBy === "tag" ? "tag" : "category";
  const spending = useQuery({
    queryKey: ["reports", "spending", from, to, granularity, engineDimension],
    queryFn: () => getSpending(from, to, granularity, engineDimension),
    enabled: rangeValid && tab === "spending" && groupBy !== "owner",
    retry: false,
  });
  const categories = useQuery({
    queryKey: ["reports", "categories", from, to, engineDimension],
    queryFn: () => getSpendingCategories(from, to, engineDimension),
    enabled: rangeValid && tab === "categories" && groupBy !== "owner",
    retry: false,
  });

  // --- M108 P3 (docs/12 §Owner dimension) -----------------------------------
  //
  // `GET /partner/state` answers 200 {linked:false} for a caller with no
  // partnership (M92), so it is safe to ask on every mount and it is the ONLY
  // thing that can turn the Owner option on — no link, no dimension, no empty
  // group-by (the card's "zero UI" gate).
  const partnerState = useQuery({
    queryKey: ["partner", "state"],
    queryFn: getPartnerState,
    retry: false,
    staleTime: 60_000,
  });
  const linked = partnerState.data?.linked === true;
  /** The dimension is only meaningful — and only offered — on the two tabs
   *  whose whole population is the rows the M93 projection delivers. */
  const ownerGrouping = linked && groupBy === "owner" && OWNER_TABS.includes(tab);
  /**
   * M179 — the population EVERY panel on this tab is summed over.
   *
   * The Owner lens is a TAB-level lens (docs/12 §Owner dimension): the chart,
   * the donut, the Category × Tag matrix and the category drill are four views
   * of one population, so one `scope` decides all of them. Before M179 only the
   * first two asked for it, which put two populations on one screen — a
   * household donut above an own-rows matrix — and dropped the drill entirely.
   * `own` is the default and stays OFF the wire, so a lone tenant's requests
   * are byte-identical to the pre-M159 ones.
   */
  const lensScope: ReportScope = ownerGrouping ? "household" : "own";

  // M159 (docs/12 §Owner dimension): the household population is COMPUTED
  // SERVER-SIDE — `scope=household` unions the caller's own rows with the
  // owner's allowlisted projection rows (docs/43 §6) behind one call, so the
  // browser renders a report the server derived instead of downloading the
  // projection to re-derive it. The lens still means exactly what docs/12
  // pins: the same two report definitions over the household population.
  // `getSpendingOver` / `getSpendingCategoriesOver` stay in `api/reports.ts`
  // as the documented client path (docs/33 §Server-first rule 5) — M160 owns
  // their retirement, and nothing here deletes them.
  const ownerSpending = useQuery({
    queryKey: ["reports", "spending", "owner", from, to, granularity],
    queryFn: () => getSpending(from, to, granularity, "category", undefined, "household"),
    enabled: ownerGrouping && tab === "spending",
    retry: false,
  });
  const ownerCategories = useQuery({
    queryKey: ["reports", "categories", "owner", from, to],
    queryFn: () => getSpendingCategories(from, to, "category", "household"),
    enabled: ownerGrouping && tab === "categories",
    retry: false,
  });

  // --- M124 R2 (docs/12 §2) — the drill-down --------------------------------
  //
  // Computed over the rows the page ALREADY holds, so drilling costs no round
  // trip and can never disagree with the chart that produced it: same engine,
  // same rows, one extra filter. The server's `?category_id=` exists for the
  // CSV export and for API consumers (docs/08).
  //
  // M179: "the rows the page already holds" are the LENS's rows — under the
  // Owner lens the drill asks the server for the same household scope the
  // chart above it rendered, so a drilled period can never fall back to the
  // caller's own ledger. `lensScope` is part of the query key: switching the
  // lens refetches rather than silently relabelling the previous population.
  const drillReport = useQuery({
    queryKey: ["reports", "spending", "drill", from, to, granularity, drill?.categoryId, lensScope],
    queryFn: () =>
      getSpending(
        from,
        to,
        granularity,
        "category",
        {
          categoryId: drill?.categoryId ?? null,
        },
        lensScope,
      ),
    // NOT gated on `tab === "spending"`: the donut on the Categories tab
    // drills too (R2 lists "donut slice" as a trigger), so the query follows
    // the drill state alone. The `groupBy !== "category"` effect above is what
    // keeps it from ever running under a dimension that has no categories.
    enabled: rangeValid && drill !== null,
    retry: false,
  });

  // --- M124 R3 (docs/12 §1) — the Category × Tag matrix ---------------------
  // M179: the same `lensScope` as the donut above it. The matrix is the
  // Categories tab's second panel over that tab's population, so leaving it on
  // the narrow scope under the Owner lens printed two populations on one
  // screen (and, in the Owner branch, printed none at all).
  const matrix = useQuery({
    queryKey: ["reports", "category-tag-matrix", from, to, lensScope],
    queryFn: () => getCategoryTagMatrix(from, to, lensScope),
    enabled: rangeValid && tab === "categories",
    retry: false,
  });

  /** Caption suffix for the Owner lens — says WHICH rows are in the number. */
  const ownerScopeNote =
    linked && partnerState.data !== undefined && "account_ids" in partnerState.data
      ? partnerState.data.account_ids.length === 0
        ? "The owner has shared no accounts yet, so only your own rows appear here."
        : "Rows come from your own ledger plus the owner's allowlisted accounts — never a third account."
      : "";

  // A partnership revoked mid-session makes the household scope answer the
  // caller's own rows again (docs/43 §6 — scope is re-read per request), and
  // the projection routes answer 401 `sharing_not_linked` (docs/43 §7) — fall
  // back to the caller's own rows instead of leaving the tab on a skeleton
  // forever.
  const linkLost =
    partnerState.data === undefined
      ? false
      : partnerState.data.linked === false && groupBy === "owner"
        ? true
        : ownerSpending.isError || ownerCategories.isError;
  useEffect(() => {
    if (linkLost) {
      // M179: a drill left open while the lens collapses would silently change
      // population underneath it (the same request now answers `own`), so it is
      // cleared WITH the lens rather than re-rendered narrower.
      setGroupBy("category");
      setDrill(null);
    }
  }, [linkLost]);
  // Leaving the tab leaves its drill behind: the breadcrumb's "back" must
  // mean "the view I came from on THIS tab", not a state from another one.
  useEffect(() => {
    setDrill(null);
  }, [tab]);
  // A drill is a category concept — switching to a dimension that has none
  // would strand the view on numbers that no longer describe it.
  useEffect(() => {
    if (groupBy !== "category") setDrill(null);
  }, [groupBy]);
  const cashflow = useQuery({
    queryKey: ["reports", "cashflow", from, to, granularity],
    queryFn: () => getCashflow(from, to, granularity),
    enabled: rangeValid && tab === "cashflow",
    retry: false,
  });
  const netWorth = useQuery({
    queryKey: ["reports", "networth", from, to, granularity],
    queryFn: () => getNetWorth(from, to, granularity),
    enabled: rangeValid && tab === "networth",
    retry: false,
  });
  const performance = useQuery({
    queryKey: ["reports", "performance", from, to],
    queryFn: () => getInvestmentPerformance(from, to),
    enabled: rangeValid && tab === "performance",
    retry: false,
  });

  // Tabs without a server-side CSV export (dividends owns its year, projections
  // is JSON-only) simply have no entry; the header button disables itself.
  // CSV export follows the SAME lens the page is showing — dimension, scope and
  // drill included — so the file and the chart can never disagree (M124,
  // docs/12 §CSV: headers are pinned to the JSON fields that get emitted;
  // M179 adds the scope so a household lens cannot export own-rows numbers).
  const csvScope: Record<string, string> = lensScope === "own" ? {} : { scope: lensScope };
  const csvPaths: Partial<Record<Tab, string>> = {
    spending: `/reports/spending?${new URLSearchParams({
      from,
      to,
      granularity,
      group_by: groupBy === "tag" ? "tag" : "category",
      ...(drill
        ? { category_id: drill.categoryId === null ? "uncategorized" : String(drill.categoryId) }
        : {}),
      ...csvScope,
    })}`,
    categories: `/reports/spending/categories?${new URLSearchParams({
      from,
      to,
      group_by: groupBy === "tag" ? "tag" : "category",
      ...csvScope,
    })}`,
    cashflow: `/reports/cashflow?${new URLSearchParams({ from, to, granularity })}`,
    networth: `/reports/net-worth?${new URLSearchParams({ from, to, granularity })}`,
    performance: `/reports/investment-performance?${new URLSearchParams({ from, to })}`,
  };

  async function handleDownload() {
    const path = csvPaths[tab];
    if (!path || !rangeValid) return;
    try {
      await downloadCsv(path, `${tab}_${from}_${to}.csv`);
      toast.success("Export ready", `${tab}_${from}_${to}.csv`);
    } catch (err) {
      toast.error("Export failed", err instanceof ApiError ? err.message : String(err));
    }
  }

  // --- M108 P2 (docs/09 §Saved report views) --------------------------------
  //
  // A preset stores ONLY what this tab restores: the raw range, the grain,
  // this tab's variant/selector and the Performance sort. The preset CHIPS
  // (YTD / 1M / …) are deliberately NOT stored — they are derived from the
  // range, and persisting derived UI state would let a chip and a range drift
  // apart after a preset rename.
  const currentState: SavedReportViewState = {
    ...(rangeValid ? { from, to } : {}),
    ...(GRANULAR_TABS.includes(tab) ? { granularity } : {}),
    ...(tab === "spending" ? { view: spendingView } : {}),
    ...(tab === "dividends" && dividendsYear !== undefined ? { year: dividendsYear } : {}),
    ...(tab === "performance" ? { sort_key: sort.key, sort_desc: sort.desc } : {}),
    // Only stored where the dimension is actually OFFERED — a lone tenant's
    // preset must not carry an Owner lens its Reports page cannot show. Tag
    // has no such gate, so it travels either way (M124, docs/02).
    ...(OWNER_TABS.includes(tab) ? { group_by: groupBy === "owner" && !linked ? "category" : groupBy } : {}),
  };

  /** One write shape: the whole list (docs/09). Re-throws so the control can
   *  roll its own busy state after the toast has been shown here. */
  async function persistViews(next: SavedReportView[]): Promise<void> {
    try {
      const updated: User = await updateMe({ saved_report_views: next });
      setUser(updated);
    } catch (err) {
      toast.error(
        "Could not save that view",
        err instanceof ApiError ? err.message : String(err),
      );
      throw err;
    }
  }

  function restoreView(view: SavedReportView): void {
    // The control only ever hands us rows restoring to THIS page, but the type
    // is shared with M123's transaction views — narrow, don't cast.
    if (view.tab === "transactions") return;
    const s = view.state;
    setTab(view.tab);
    if (s.from !== undefined) setFrom(s.from);
    if (s.to !== undefined) setTo(s.to);
    if (s.granularity !== undefined) setGranularity(s.granularity);
    if (view.tab === "spending") setSpendingView(s.view === "treemap" ? "treemap" : "bars");
    if (OWNER_TABS.includes(view.tab)) {
      setGroupBy(s.group_by === "tag" || s.group_by === "owner" ? s.group_by : "category");
    }
    if (view.tab === "dividends") setDividendsYear(s.year);
    if (view.tab === "performance" && s.sort_key !== undefined) {
      const key = PERF_COLUMNS.find((column) => column === s.sort_key);
      if (key !== undefined) setSort({ key, desc: s.sort_desc ?? true });
    }
    toast.success("View restored", view.title);
  }

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-xl font-semibold tracking-tight">Reports</h1>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {/* Shared range picker (ui-contract Reports must-have; YTD default). */}
          <RangePicker
            from={from}
            to={to}
            onChange={(r) => {
              setFrom(r.from);
              setTo(r.to);
            }}
          />
          <SegmentedControl<Granularity>
            layoutId="report-granularity"
            size="sm"
            ariaLabel="Granularity"
            disabled={!GRANULAR_TABS.includes(tab)}
            options={GRANULARITIES.map((g) => ({
              value: g,
              label: g,
              title: GRANULAR_TABS.includes(tab) ? undefined : "This report has no time buckets",
            }))}
            value={granularity}
            onChange={setGranularity}
          />
          {/* M108 P1: the Spending tab's chart variant. Rendered only there —
              a permanently-disabled control on six tabs would be noise, and
              the granularity control above already documents the pattern. */}
          {/* M108 P3 / M124 — the Group-by dimension. Rendered wherever the
              dimension means something: Category and Tag always (tags need no
              partnership and no extra query surface), Owner ONLY when `GET
              /partner/state` says there is a partnership to join — a lone
              tenant must never see a dimension it cannot fill. */}
          {OWNER_TABS.includes(tab) && (
            <SegmentedControl<GroupByOption>
              layoutId="report-group-by"
              size="sm"
              ariaLabel="Group by"
              options={[
                { value: "category", label: "Category", title: "Group by category" },
                {
                  value: "tag",
                  label: "Tag",
                  title: "Group by tag — a transaction with several tags counts in each",
                },
                ...(linked
                  ? [{ value: "owner" as const, label: "Owner", title: "Group by household member" }]
                  : []),
              ]}
              value={groupBy === "owner" && !linked ? "category" : groupBy}
              onChange={setGroupBy}
            />
          )}
          {tab === "spending" && (
            <SegmentedControl<SpendingView>
              layoutId="report-spending-view"
              size="sm"
              ariaLabel="Chart view"
              options={[
                { value: "bars", label: "Bars", title: "Stacked bars by bucket and category" },
                { value: "treemap", label: "Treemap", title: "One block per category, sized by amount" },
              ]}
              value={spendingView}
              onChange={setSpendingView}
            />
          )}
          <Button variant="outline" size="sm" disabled={!rangeValid || !csvPaths[tab]} onClick={() => void handleDownload()}>
            <Download className="size-3.5" aria-hidden />
            CSV
          </Button>
        </div>
      </div>

      {!rangeValid && (
        <p className="mt-2 text-sm text-danger">Invalid range: “from” must be ≤ “to”.</p>
      )}

      {/* M108 P2 — "Save this view" / "Saved views", on its own row so the
          disclosure panel can take the full width instead of being squeezed
          into the control cluster. */}
      <div className="mt-3">
        <SavedViewsControl
          tab={tab}
          tabLabel={TABS.find((entry) => entry.id === tab)?.label ?? "Report"}
          views={savedViews}
          state={currentState}
          canSave={rangeValid}
          onPersist={persistViews}
          onRestore={restoreView}
        />
      </div>

      {/* Tabs — underline morphs via shared layoutId (ui-contract move #5 style).
          The row must NEVER shrink a button below its label: at ≤768px the 7
          labels exceed the content width, and a shrinking button spills its
          glyphs into the neighbours (text-on-text overlap) with the last tab
          running off-screen. `flex-wrap` packs the row onto as many lines as
          the viewport needs, `shrink-0` + `whitespace-nowrap` keep each label
          inside its own button (M82-F1). */}
      <div className="mt-5 flex flex-wrap gap-1 border-b border-veil/[0.06]">
        {TABS.map(({ id, label }) => {
          const active = tab === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={`relative shrink-0 whitespace-nowrap px-2 pb-2.5 pt-1.5 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent sm:px-3.5 ${
                active ? "font-medium text-accent" : "text-muted hover:text-text"
              }`}
            >
              {label}
              {active && (
                <motion.span
                  layoutId="reports-tab-underline"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                  className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-gradient-to-r from-accent to-accent-soft shadow-glow-sm"
                  aria-hidden
                />
              )}
            </button>
          );
        })}
      </div>

      {/* Route-level transition on tab switch (ui-contract move #4). */}
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={tab}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.22, ease: EASE }}
          className="mt-6"
        >
          {rangeValid ? renderTab() : null}
        </motion.div>
      </AnimatePresence>
    </section>
  );

  /**
   * M124 R2 (docs/12 §2, docs/09 §Drill-down) — the drilled category's
   * period breakdown, replacing the tab's chart with a breadcrumb back to the
   * all-categories view.
   *
   * The ledger link is KEPT here rather than dropped: the treemap used to
   * navigate to `/transactions?category=`, and a drill that removed that
   * affordance would be a regression dressed up as a feature. It moves to a
   * secondary action so both paths stay available.
   *
   * **M179 — under the Owner lens the link is withheld, and the population is
   * named instead.** `/transactions?category=` describes the caller's OWN
   * ledger only (the M93 projection is a read-only summary, not a ledger
   * source for the transactions list), so a household drill offering it would
   * hand the reader a filtered list of a DIFFERENT population than the chart
   * above it — the silent-weaker-result failure docs/33 rule 4 forbids. The
   * chart itself is the household's numbers, and the caption says so.
   */
  function renderDrill() {
    if (drill === null) return null;
    if (drillReport.isError) {
      return <NoDataPanel hint={errorHint(drillReport.error)} />;
    }
    if (drillReport.isPending || drillReport.data === undefined) {
      return <StateRow variant="loading" title="Loading" />;
    }
    const { from: drillFrom, to: drillTo, granularity: drillGrain, items } = drillReport.data;
    return (
      <div>
        <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => setDrill(null)}
            className="inline-flex min-h-11 items-center rounded-lg px-2 text-muted outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
          >
            &larr; All categories
          </button>
          <span aria-hidden className="text-muted">
            /
          </span>
          <span className="font-medium text-text" aria-current="page">
            {drill.name}
          </span>
        </nav>
        <p className="mt-1 text-sm text-muted">
          {fmtBucket(drillFrom, drillGrain)} &ndash; {fmtBucket(drillTo, drillGrain)} ·{" "}
          <span className="font-medium tabular">{fmtCad(drillReport.data.total_cad)}</span> spent
          {lensScope === "household" ? (
            <>
              {" "}
              across the household (own rows + the owner&rsquo;s allowlisted accounts)
            </>
          ) : null}
        </p>
        <div className="mt-3">
          <CategoryDrillChart items={items} granularity={drillGrain} name={drill.name} />
        </div>
        {lensScope === "household" ? (
          <p className="mt-2 text-sm text-muted">
            The ledger link is hidden here on purpose: `/transactions` reads your own rows only, so
            it could not show this drill&rsquo;s population.
          </p>
        ) : (
          <p className="mt-2 text-sm">
            <a
              className="text-accent underline-offset-2 hover:underline"
              href={`/transactions?category=${drill.categoryId === null ? "uncategorized" : drill.categoryId}`}
            >
              View these transactions &rarr;
            </a>
          </p>
        )}
      </div>
    );
  }

  function renderTab() {
    switch (tab) {
      case "spending": {
        // M124 R2: a drill replaces the chart with that one category's
        // periods, with a breadcrumb back to the all-categories view. It is
        // checked BEFORE the Owner branch because M179 made the drill a
        // first-class part of the lens: `drillReport` carries `lensScope`, so a
        // household chart's block drills into the household's periods.
        if (drill !== null) return renderDrill();
        // M108 P3 — the Owner lens (docs/12 §Owner dimension). The Category
        // path below is the byte-identical today-path: nothing about it
        // changes for a lone tenant or for a linked one who leaves the toggle
        // where it was.
        if (linked && groupBy === "owner") {
          if (ownerSpending.isError) {
            return <NoDataPanel hint={errorHint(ownerSpending.error)} />;
          }
          if (ownerSpending.isPending) {
            return <StateRow variant="loading" title="Loading" />;
          }
          // M159: ONE series per bucket × category, over the household
          // population the SERVER computed (`scope=household`). The pre-M159
          // browser compute drew two series — "Me" vs "Partner" — because the
          // only way it could split the population was to hold both row sets;
          // the ruled server shape sums them (docs/12 §Owner dimension), so
          // the split is retired with the compute that drew it.
          const items = toSlices(ownerSpending.data.items, "category");
          if (items.length === 0) return <NoDataPanel />;
          // The label comes from the server's own slices — the chart hands
          // back an id, and only this component holds the names.
          const ownerNames = new Map(items.map((slice) => [slice.id, slice.name]));
          const drillFromOwnerChart = (id: number | null) =>
            setDrill({ categoryId: id, name: ownerNames.get(id ?? null) ?? "Uncategorized" });
          return (
            <>
              {spendingView === "treemap" ? (
                <SpendingTreemap
                  items={items}
                  title={HOUSEHOLD_TITLE}
                  onDrill={drillFromOwnerChart}
                />
              ) : (
                <SpendingStackedBarChart
                  items={items}
                  granularity={ownerSpending.data.granularity}
                  title={HOUSEHOLD_TITLE}
                  onDrill={drillFromOwnerChart}
                />
              )}
              <p className="mt-3 text-sm text-muted">
                Range total (own + shared household):{" "}
                <span className="font-medium tabular">{fmtCad(ownerSpending.data.total_cad)}</span>{" "}
                across {items.length} data points. {ownerScopeNote}
              </p>
            </>
          );
        }
        // M124 R2: a drill replaces the chart with that one category's
        // periods, with a breadcrumb back to the all-categories view.
        if (drill !== null) return renderDrill();
        if (spending.isPending) return <StateRow variant="loading" title="Loading" />;
        if (spending.isError || spending.data.items.length === 0) {
          return <NoDataPanel hint={errorHint(spending.error)} />;
        }
        // M124: the dimension decides the drill target. Category drills
        // in-page (R2); Tag has no drill of its own, so its blocks fall back
        // to the ledger deep link the app already uses for a tag (M104) —
        // Untagged has no such target and stays static, exactly like Owner.
        const slices = toSlices(spending.data.items, spending.data.group_by);
        // The charts pass back only the slice id — they are dimension-agnostic
        // and know nothing about breadcrumbs — so the page resolves the label
        // from its own data, which is the only place that has it.
        const nameOf = new Map(slices.map((slice) => [slice.id, slice.name]));
        const drillFromChart =
          groupBy === "category"
            ? (id: number | null) =>
                setDrill({ categoryId: id, name: nameOf.get(id ?? null) ?? "Uncategorized" })
            : (id: number | null) => {
                if (id !== null) navigate(`/transactions?tag=${id}`);
              };
        const tagMode = spending.data.group_by === "tag";
        const dimensionWord = tagMode ? "tag" : "category";
        return (
          <>
            {spendingView === "treemap" ? (
              <SpendingTreemap
                items={slices}
                title={`Spending by ${dimensionWord}`}
                onDrill={drillFromChart}
              />
            ) : (
              <SpendingStackedBarChart
                items={slices}
                granularity={spending.data.granularity}
                title={`Spending by ${dimensionWord}`}
                onDrill={drillFromChart}
              />
            )}
            {tagMode ? (
              /* docs/09 §honesty — the duplicated-totals label. A two-tag
                 transaction is in two blocks, so the block sum overstates
                 what actually left the account; say so, and name the real
                 figure rather than making the reader infer it. */
              <p className="mt-3 text-sm text-muted">
                Sum of tag totals: <span className="font-medium tabular">{fmtCad(spending.data.total_cad)}</span>{" "}
                across {spending.data.items.length} data points — totals per tag; multi-tag
                transactions count in each, so this can exceed the{" "}
                <span className="font-medium tabular">{fmtCad(spending.data.spend_total_cad)}</span>{" "}
                actually spent.
              </p>
            ) : (
              <p className="mt-3 text-sm text-muted">
                Range total:{" "}
                <span className="font-medium tabular">{fmtCad(spending.data.total_cad)}</span> across{" "}
                {spending.data.items.length} data points.
              </p>
            )}
          </>
        );
      }

      case "categories": {
        // M179: the drill is checked FIRST (as on Spending) — the Owner lens
        // owns the whole tab, so a household donut slice drills into the
        // household's periods through `drillReport`'s `lensScope`.
        if (drill !== null) return renderDrill();
        if (linked && groupBy === "owner") {
          if (ownerCategories.isError) {
            return <NoDataPanel hint={errorHint(ownerCategories.error)} />;
          }
          if (ownerCategories.isPending) {
            return <StateRow variant="loading" title="Loading" />;
          }
          // M159: the donut slices the SERVER-computed household population
          // (`scope=household`), rolled up to roots through the merged
          // category namespace (docs/43 §6: a projected row carries the
          // owner's ids, so both trees resolve). The pre-M159 two-slice
          // "Me"/"Partner" collapse went with the browser compute.
          const items = breakdownSlices(ownerCategories.data);
          // M179: the Category × Tag matrix is this same tab's second panel
          // over the same population, so the lens drives it too — it is the
          // `scope=household` answer, with the caption that names it. An empty
          // donut must not hide it (docs/09 §Empty states are labelled).
          const ownerMatrix =
            matrix.data !== undefined && matrix.data.rows.length > 0 ? (
              <CategoryTagMatrixPanel
                matrix={matrix.data}
                from={from}
                to={to}
                scope="household"
              />
            ) : null;
          if (items.length === 0) {
            return (
              <>
                <NoDataPanel />
                {ownerMatrix}
              </>
            );
          }
          return (
            <>
              <CategoriesDonut
                items={items}
                total={ownerCategories.data.total_cad}
                centerLabel="total spent"
                title={HOUSEHOLD_TITLE}
                onSelect={(id, name) => setDrill({ categoryId: id, name })}
              />
              <p className="mt-3 text-sm text-muted">
                Own rows + the owner&rsquo;s allowlisted accounts (M93 scope), summed over the range
                and rolled up to category ancestors — one household, not a per-owner split.{" "}
                {ownerScopeNote}
              </p>
              {ownerMatrix}
            </>
          );
        }
        if (categories.isPending) return <StateRow variant="loading" title="Loading" />;
        {
          // The matrix is its own panel with its own query, so an empty donut
          // must not hide it — "no slices" and "no category×tag cells" are
          // different facts and each renders its own empty state (docs/09
          // §Empty states are labelled).
          const matrixPanel =
            matrix.data !== undefined && matrix.data.rows.length > 0 ? (
              <CategoryTagMatrixPanel matrix={matrix.data} from={from} to={to} />
            ) : null;
          if (categories.isError || categories.data.items.length === 0) {
            return (
              <>
                <NoDataPanel hint={errorHint(categories.error)} />
                {matrixPanel}
              </>
            );
          }
          const tagMode = categories.data.group_by === "tag";
          const slices = breakdownSlices(categories.data);
          return (
            <>
              <CategoriesDonut
                items={slices}
                total={categories.data.total_cad}
                centerLabel={tagMode ? "sum of tag totals" : "total spent"}
                title={`Spending by ${tagMode ? "tag" : "category"}`}
                onSelect={
                  groupBy === "category"
                    ? (id, name) => setDrill({ categoryId: id, name })
                    : undefined
                }
              />
              {tagMode ? (
                <p className="mt-3 text-sm text-muted">
                  Rolled up per tag across the range — a transaction with several tags is a slice of
                  each, so the slices sum to{" "}
                  <span className="font-medium tabular">{fmtCad(categories.data.total_cad)}</span>{" "}
                  against <span className="font-medium tabular">{fmtCad(categories.data.spend_total_cad)}</span>{" "}
                  actually spent.
                </p>
              ) : (
                <p className="mt-3 text-sm text-muted">
                  Rolled up to top-level ancestors across the range.
                </p>
              )}
              {matrixPanel}
            </>
          );
        }
      }

      case "cashflow": {
        if (cashflow.isPending) return <StateRow variant="loading" title="Loading" />;
        const chart =
          cashflow.isError || cashflow.data.buckets.length === 0 ? (
            <NoDataPanel hint={errorHint(cashflow.error)} />
          ) : (
            <CashflowComposedChart
              buckets={cashflow.data.buckets}
              granularity={cashflow.data.granularity}
            />
          );
        return (
          <>
            {chart}
            {/* M77 (docs/12 §Sankey cash-flow) — own query, loading and error
                states so an empty graph never hides the composed chart. */}
            <SankeyPanel from={from} to={to} />
          </>
        );
      }

      case "networth": {
        if (netWorth.isPending) return <StateRow variant="loading" title="Loading" />;
        if (netWorth.isError || netWorth.data.buckets.length === 0) {
          return <NoDataPanel hint={errorHint(netWorth.error)} />;
        }
        return (
          <NetWorthAreaChart
            buckets={netWorth.data.buckets}
            granularity={netWorth.data.granularity}
          />
        );
      }

      case "performance": {
        if (performance.isPending) return <StateRow variant="loading" title="Loading" />;
        if (performance.isError || performance.data.items.length === 0) {
          return <NoDataPanel hint={errorHint(performance.error)} />;
        }
        const sorted = [...performance.data.items].sort((a, b) => {
          const va = a[sort.key];
          const vb = b[sort.key];
          const cmp =
            sort.key === "symbol"
              ? (va ?? "").localeCompare(vb ?? "")
              : toNumber(va) - toNumber(vb);
          return sort.desc ? -cmp : cmp;
        });
        return (
          <div className="space-y-8">
            <UnrealizedBarChart items={performance.data.items} />
            <div role="region" aria-label="Performance table" tabIndex={0} className="glass overflow-x-auto rounded-xl">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                    {(
                      [
                        ["symbol", "Symbol"],
                        ["quantity", "Quantity"],
                        ["realized_cad", "Realized (CAD)"],
                        ["unrealized_cad", "Unrealized (CAD)"],
                        ["market_value_cad", "Market value (CAD)"],
                      ] as const satisfies readonly [PerfColumn, string][]
                    ).map(([key, label]) => (
                      <th
                        key={key}
                        className={`px-3 py-2.5 font-medium ${key !== "symbol" ? "text-right" : ""}`}
                        aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}
                      >
                        <button
                          type="button"
                          className="transition-colors hover:text-text"
                          onClick={() =>
                            setSort((prev) =>
                              prev.key === key
                                ? { key, desc: !prev.desc }
                                : { key, desc: true },
                            )
                          }
                        >
                          {label}
                          {sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
                        </button>
                      </th>
                    ))}
                    <th className="px-3 py-2.5 text-right font-medium">ACB pool (CAD)</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((item) => (
                    <tr
                      key={item.security_id}
                      className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
                    >
                      <td className="px-3 py-2 font-medium">{item.symbol}</td>
                      <td className="px-3 py-2 text-right tabular">{fmtQuantity(item.quantity)}</td>
                      <td className={`px-3 py-2 text-right tabular ${Number(item.realized_cad) < 0 ? "text-danger" : Number(item.realized_cad) > 0 ? "text-gain" : ""}`}>
                        {fmtCad(item.realized_cad)}
                      </td>
                      <td className={`px-3 py-2 text-right tabular ${item.unrealized_cad !== null && Number(item.unrealized_cad) < 0 ? "text-danger" : item.unrealized_cad !== null && Number(item.unrealized_cad) > 0 ? "text-gain" : ""}`}>
                        {item.unrealized_cad === null
                          ? "— (registered)"
                          : fmtCad(item.unrealized_cad)}
                      </td>
                      <td className="px-3 py-2 text-right tabular">{fmtCad(item.market_value_cad)}</td>
                      <td className="px-3 py-2 text-right text-muted tabular">
                        {item.acb_pool_cad === null ? "—" : fmtCad(item.acb_pool_cad)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-veil/[0.08] font-medium">
                    <td className="px-3 py-2">Totals</td>
                    <td />
                    <td className={`px-3 py-2 text-right tabular ${Number(performance.data.totals.realized_cad) < 0 ? "text-danger" : Number(performance.data.totals.realized_cad) > 0 ? "text-gain" : ""}`}>
                      {fmtCad(performance.data.totals.realized_cad)}
                    </td>
                    <td className={`px-3 py-2 text-right tabular ${Number(performance.data.totals.unrealized_cad) < 0 ? "text-danger" : Number(performance.data.totals.unrealized_cad) > 0 ? "text-gain" : ""}`}>
                      {fmtCad(performance.data.totals.unrealized_cad)}
                    </td>
                    <td className="px-3 py-2 text-right tabular">
                      {fmtCad(performance.data.totals.market_value_cad)}
                    </td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>
        );
      }

      case "dividends":
        // M29 (docs/24): paid-dividends analytics plus the upcoming-payment
        // calendar — DividendCalendarPanel was built but never mounted
        // (QA finding, 2026-09-28); it owns its own query + loading/error
        // states and reads GET /reports/dividend-calendar.
        return (
          <>
            <DividendsPanel year={dividendsYear} onYearChange={setDividendsYear} />
            <div className="mt-4">
              <DividendCalendarPanel />
            </div>
          </>
        );

      case "projections":
        return <ProjectionsPanel />;
    }
  }
}

/** Until W6 the aggregation routes 404; surface that without crashing (docs/12). */
function errorHint(error: unknown): string | undefined {
  if (error instanceof ApiError) return error.message;
  return undefined;
}
