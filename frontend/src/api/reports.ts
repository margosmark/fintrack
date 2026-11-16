// M160 (docs/33 §Server-first rule 5): the report ENGINE computes this module
// used to host moved out with the mirrors' retirement, and the shared
// fixed-point primitives moved to `lib/dec.ts`. M160-R2 (t_85311c34) closed
// the last gap: the three panel-backed loaders (dividends / sankey /
// total-return) left the deprecated `lib/reportClient.ts` shim — now deleted —
// and joined the thin-read layer below, so EVERY report the UI renders is one
// request to a `/reports/*` route: no ledger download, no browser
// re-derivation.
import { api, apiBinary, ApiError } from "./client";
import type { DividendProvenance } from "./dividendProvenance";
/** The graph shape `CashflowSankey` renders (docs/12 §8). The interface lives
 *  in `lib/sankeyEngine.ts`, which owns the compute the server twin was
 *  written against, so the chart, that engine and the loader below share ONE
 *  definition of the wire shape. */
import type { SankeyReport } from "../lib/sankeyEngine";

// reports (docs/12 + docs/06): aggregation endpoints (M8) and the annual
// capital-gains report. Aggregation routes do NOT exist until W6 — pages must
// render graceful "No data in range" states on their absence.
//
// All money fields are Decimal values serialized as strings.

// --- Time-bucketing contract (docs/12) --------------------------------------

export type Granularity = "day" | "week" | "month" | "year";

export const GRANULARITIES: readonly Granularity[] = ["day", "week", "month", "year"];

// --- Pinned response shapes (docs/12 §Endpoints) -----------------------------

// M124 (docs/12 §1): the spending reports group by ONE dimension. The item
// shape says which by which id field is present — that is also what lets
// `format=csv` pin its header to the fields actually emitted.
export type ReportGroupBy = "category" | "tag";

export interface SpendingItem {
  bucket: string;
  category_id: number | null;
  category_name: string;
  total_cad: string;
}

export interface TagSpendingItem {
  bucket: string;
  tag_id: number | null;
  tag_name: string;
  total_cad: string;
}

export type AnySpendingItem = SpendingItem | TagSpendingItem;

export interface SpendingReport {
  from: string;
  to: string;
  granularity: Granularity;
  group_by: ReportGroupBy;
  items: AnySpendingItem[];
  /** Sum of the item blocks. Under `group_by=tag` this can EXCEED real spend:
   *  a transaction with two tags lands in both blocks. */
  total_cad: string;
  /** Each transaction counted exactly ONCE — the honest denominator. */
  spend_total_cad: string;
}

/**
 * One bucket/series entry with the dimension flattened out, which is the
 * shape the chart components actually consume.
 *
 * `key` is `category_id` or `tag_id` depending on the report's `group_by`;
 * `name` is the label that series renders under. Keeping the charts on THIS
 * type is what makes donut/treemap/bars dimension-agnostic (docs/12 §1) —
 * they never learn what a tag is.
 */
export interface SpendSlice {
  bucket: string;
  id: number | null;
  name: string;
  total_cad: string;
}

/** Flatten a dimensioned report into the slices the charts render. */
export function toSlices(items: readonly AnySpendingItem[], groupBy: ReportGroupBy): SpendSlice[] {
  return groupBy === "tag"
    ? (items as TagSpendingItem[]).map((i) => ({
        bucket: i.bucket,
        id: i.tag_id,
        name: i.tag_name,
        total_cad: i.total_cad,
      }))
    : (items as SpendingItem[]).map((i) => ({
        bucket: i.bucket,
        id: i.category_id,
        name: i.category_name,
        total_cad: i.total_cad,
      }));
}

export interface CategoryBreakdownItem {
  category_id: number | null;
  name: string;
  cad_total: string;
}

export interface TagBreakdownItem {
  tag_id: number | null;
  name: string;
  cad_total: string;
}

export interface CategoryBreakdown {
  group_by: ReportGroupBy;
  /** category: root roll-up, descending. tag: flat, descending. */
  items: CategoryBreakdownItem[] | TagBreakdownItem[];
  /** Sum of the slices — under `group_by=tag` this can EXCEED real spend. */
  total_cad: string;
  /** Each transaction counted exactly ONCE — the honest denominator. */
  spend_total_cad: string;
}

/** Breakdown slices with the dimension flattened, for the donut. */
export function breakdownSlices(
  report: CategoryBreakdown,
): { id: number | null; name: string; cad_total: string }[] {
  return report.group_by === "tag"
    ? (report.items as TagBreakdownItem[]).map((i) => ({
        id: i.tag_id,
        name: i.name,
        cad_total: i.cad_total,
      }))
    : (report.items as CategoryBreakdownItem[]).map((i) => ({
        id: i.category_id,
        name: i.name,
        cad_total: i.cad_total,
      }));
}

// --- M124 R3 — the Category × Tag matrix (docs/12 §1) ------------------------

export interface MatrixColumn {
  tag_id: number | null;
  tag_name: string;
}

export interface MatrixRow {
  category_id: number | null;
  category_name: string;
}

/** ONE non-zero cell. Long form so `format=csv` is these rows with a pinned
 *  header, and so a mostly-empty grid does not ship a wall of zeros. */
export interface MatrixCell {
  category_id: number | null;
  category_name: string;
  tag_id: number | null;
  tag_name: string;
  cad_total: string;
}

export interface CategoryTagMatrix {
  from: string;
  to: string;
  columns: MatrixColumn[];
  rows: MatrixRow[];
  items: MatrixCell[];
  /** The range total, each transaction counted ONCE. Cells count a
   *  transaction once PER TAG, so they can sum to more than this. */
  total_cad: string;
}

export interface CashflowBucket {
  bucket: string;
  income_cad: string;
  expenses_cad: string;
  net_cad: string;
}

export interface CashflowReport {
  from: string;
  to: string;
  granularity: Granularity;
  buckets: CashflowBucket[];
}

export interface NetWorthBucket {
  bucket_end: string;
  cash_cad: string;
  positions_cad: string;
  net_cad: string;
  /** M97 (docs/12 §4): presentation split of the SAME signed sum, for the
   *  net-worth tooltip — `assets_cad − liabilities_cad == cash_cad +
   *  positions_cad` before quantizing; each cell rounds on its own. */
  assets_cad: string;
  liabilities_cad: string;
}

export interface NetWorthReport {
  from: string;
  to: string;
  granularity: Granularity;
  buckets: NetWorthBucket[];
}

export interface PerformanceItem {
  security_id: number;
  symbol: string;
  quantity: string;
  realized_cad: string;
  unrealized_cad: string | null;
  acb_pool_cad: string | null;
  market_value_cad: string;
}

export interface PerformanceTotals {
  realized_cad: string;
  unrealized_cad: string;
  market_value_cad: string;
}

export interface PerformanceReport {
  items: PerformanceItem[];
  totals: PerformanceTotals;
}

/** The R2 drill filter: a category subtree, or the NULL bucket. */
export interface DrillFilter {
  /** `null` means "uncategorized" — the deep-link token the server accepts. */
  categoryId: number | null;
}

/**
 * Query string builder for the server-first report routes (M137 P0-2).
 * `undefined`/`null` params drop out, so a defaulted argument never becomes
 * `category_id=undefined` on the wire.
 */
function qs(params: Record<string, string | number | null | undefined>): string {
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) out.set(key, String(value));
  }
  const encoded = out.toString();
  return encoded ? `?${encoded}` : "";
}

// --- M137 P0-2 (docs/33 §Server-first computing) -----------------------------
//
// The seven shadowed computes below are now THIN READS of their own
// `/reports/*` route: the browser renders a payload the server already
// computed instead of downloading the ledger (plus trades, accounts,
// securities, prices and fx) to re-derive it. The screen-shape is the
// contract — every interface above is unchanged, so no page moved. M160-R2
// added the last three (dividends / cashflow-sankey / total-return, at the
// bottom of this section) to the same pattern.
//
// The engines they replaced retired with M160 (P2-5, docs/33 §Server-first
// rule 5): the client mirrors and their parity harness adapters are gone, the
// frozen goldens became server-route replay pins (backend/tests/test_m160_replay_*.py),
// and the rule-5 fallback posture now lives only where a server twin does not
// exist (`lib/budgets.ts`). M160-R2 (t_85311c34) closed the last exception:
// the dividends / cashflow-sankey / total-return loaders below are server
// reads too, and the shim that carried them is deleted.

export function getSpending(
  from: string,
  to: string,
  granularity: Granularity,
  groupBy: ReportGroupBy = "category",
  drill?: DrillFilter,
  scope: ReportScope = "own",
): Promise<SpendingReport> {
  const categoryId =
    drill === undefined
      ? undefined
      : drill.categoryId === null
        ? "uncategorized"
        : drill.categoryId;
  return api<SpendingReport>(
    // `own` stays OFF the wire (qs drops null), so every pre-M159 request is
    // byte-identical and a defaulted argument can never widen a population.
    `/reports/spending${qs({ from, to, granularity, group_by: groupBy, category_id: categoryId, scope: scope === "own" ? null : scope })}`,
  );
}

export function getSpendingCategories(
  from: string,
  to: string,
  groupBy: ReportGroupBy = "category",
  scope: ReportScope = "own",
): Promise<CategoryBreakdown> {
  return api<CategoryBreakdown>(
    `/reports/spending/categories${qs({ from, to, group_by: groupBy, scope: scope === "own" ? null : scope })}`,
  );
}

/** The Category × Tag matrix (M124 R3, docs/12 §1).
 *
 * M179: the matrix is the Categories tab's second panel over the same
 * population as the donut above it, so the Owner lens passes `household` here
 * too — `own` stays OFF the wire (qs drops null) so every pre-M179 request is
 * byte-identical.
 */
export function getCategoryTagMatrix(
  from: string,
  to: string,
  scope: ReportScope = "own",
): Promise<CategoryTagMatrix> {
  return api<CategoryTagMatrix>(
    `/reports/category-tag-matrix${qs({ from, to, scope: scope === "own" ? null : scope })}`,
  );
}

// M108 P3's owner-dimension client computes (`getSpendingOver` /
// `getSpendingCategoriesOver`) retired here: M159 moved that compute
// server-side (`scope=household` on the two `/reports/*` routes) and M160
// deleted the engines they ran on.

/** M159: which population a spending report sums (docs/08 §Report routes). */
export type ReportScope = "own" | "household";

export function getCashflow(
  from: string,
  to: string,
  granularity: Granularity,
): Promise<CashflowReport> {
  return api<CashflowReport>(`/reports/cashflow${qs({ from, to, granularity })}`);
}

function assertRange(from: string, to: string): void {
  // Mirrors the server's `_check_range` — same 422 envelope for bad ranges
  // so errorHint renders identically whether the compute ran client- or
  // server-side (CSV exports still hit the route and re-run this check).
  if (from > to) {
    throw new ApiError(422, `Invalid date range: ${from} > ${to}`, "invalid_date_range");
  }
}

export function getNetWorth(
  from: string,
  to: string,
  granularity: Granularity,
): Promise<NetWorthReport> {
  // `assertRange` still runs first so a bad range answers the SAME 422
  // envelope without a round trip (the route re-checks it anyway).
  assertRange(from, to);
  return api<NetWorthReport>(`/reports/net-worth${qs({ from, to, granularity })}`);
}

export function getInvestmentPerformance(from: string, to: string): Promise<PerformanceReport> {
  assertRange(from, to);
  return api<PerformanceReport>(`/reports/investment-performance${qs({ from, to })}`);
}

// --- Capital gains (docs/06; shapes confirmed against the tax engine) --------

export type DenialOutcome = "deferral" | "registered";

export interface CapitalGainsItem {
  security_id: number;
  security: string;
  symbol: string;
  proceeds_cad: string;
  acb_cad: string;
  gain_loss_cad: string;
  outlays_commissions_cad: string;
}

export interface CapitalGainsDenial {
  security_id: number;
  symbol: string;
  sale_date: string;
  /** Magnitude of the denied loss as a decimal string. */
  denied_loss_cad: string;
  outcome: DenialOutcome;
  acquiring_account_id: number | null;
}

export interface CapitalGainsTotals {
  proceeds_cad: string;
  acb_cad: string;
  gain_loss_cad: string;
  outlays_commissions_cad: string;
}

export interface CapitalGainsReport {
  year: number;
  items: CapitalGainsItem[];
  totals: CapitalGainsTotals;
  denials: CapitalGainsDenial[];
  /** Informational disclaimer, displayed verbatim (docs/06). */
  disclaimer: string;
}

export function getCapitalGains(year: number): Promise<CapitalGainsReport> {
  // M137 P0-2 (docs/33 §Server-first computing): the tax engine's own route.
  // `format=csv` / `format=html` already went there for the exports, so the
  // screen and the export are now one answer rather than two implementations
  // pinned to each other by the s4 goldens (which STAY as server-route replay
  // pins — backend/tests/test_m160_replay_s4.py — after M160 retired the
  // client engine).
  //
  // `year` bounds (ge=1900 le=2200) are the route's own query constraint: its
  // envelope `detail` is a FastAPI validation LIST, which `ApiError` (string
  // detail) cannot reproduce — same as before, and the Taxes page only ever
  // offers 2014…current.
  return api<CapitalGainsReport>(`/reports/capital-gains${qs({ year })}`);
}

// --- Dividends (M14, docs/12 §Chart mapping) ---------------------------------

export interface DividendItem {
  security_id: number;
  symbol: string;
  name: string;
  dividend_cad: string;
  distribution_cad: string;
  roc_cad: string;
  total_cad: string;
  payment_count: number;
  last_payment_date: string | null;
}

export interface DividendTotals {
  dividend_cad: string;
  distribution_cad: string;
  roc_cad: string;
  total_cad: string;
}

export interface DividendMonth {
  month: string;
  total_cad: string;
}

export interface DividendReport {
  year: number;
  available_years: number[];
  items: DividendItem[];
  by_month: DividendMonth[];
  totals: DividendTotals;
  ttm_total_cad: string;
  unresolved_fx: number;
  /** Which sources back the CAD figures — ledger income trades and the
   *  trade-date FX cache — with each one's timestamp and stale-or-missing
   *  status (docs/07 §M184, docs/12 §Dividends). */
  provenance: DividendProvenance;
}

// M160-R2 (t_85311c34): `getDividendReport` / `getCashflowSankey` /
// `getTotalReturn` came BACK here from the deleted `lib/reportClient.ts`
// shim as thin reads of their own routes — the three panels they feed
// (Dividends / Sankey / Total return) now render a payload the server
// already computed. Their response shapes stay HERE: this module is the
// pinned docs/12 / docs/05 wire contract.
//
// `assertRange` still runs first on the two range routes so a bad range
// answers the SAME 422 envelope the server's `_check_range` would send,
// without a round trip (the route re-checks it anyway) — identical to
// `getNetWorth`.

/**
 * Yearly income overview per security (docs/12 §6, docs/22): trade-date CAD
 * conversion, TTM total and the unresolved-fx count all happen on
 * `GET /reports/dividends`. `year` stays OPTIONAL — omitted, the server
 * answers with the latest year that has income rows — and the same path with
 * `format=csv` is the export the panel downloads.
 */
export function getDividendReport(year?: number): Promise<DividendReport> {
  return api<DividendReport>(`/reports/dividends${qs({ year })}`);
}

/**
 * Three-column money-flow graph (M77, docs/12 §Sankey): income sources →
 * money in → destinations, computed by `GET /reports/cashflow-sankey`.
 * JSON-only — a graph has no flat table, so there is no `format=csv` twin.
 * The `SankeyReport` interface itself lives in `lib/sankeyEngine.ts`, which
 * owns the compute this route was written against, so the chart, that module
 * and this loader share ONE definition of the wire shape.
 */
export function getCashflowSankey(from: string, to: string): Promise<SankeyReport> {
  assertRange(from, to);
  return api<SankeyReport>(`/reports/cashflow-sankey${qs({ from, to })}`);
}

/** M77/M117 (docs/05 §Total return) — the wire contract, moved here with the
 *  loader: both returns are PERIOD (never annualized), fixed 8-decimal plain
 *  strings, `null` when undefined (M65 honesty, never a numeric 0). */
export interface TotalReturnReport {
  from: string;
  to: string;
  status: "ok" | "no_data";
  start_value_cad: string;
  end_value_cad: string;
  net_flow_cad: string;
  income_cad: string;
  /** Fixed 8-decimal-place string, or null when undefined (never "0.00…"). */
  twr_pct: string | null;
  mwr_pct: string | null;
  valuation_days: number;
  active_days: number;
  flow_days: number;
  account_count: number;
}

/**
 * Time-weighted + money-weighted period returns over `[from, to]`
 * (`GET /reports/total-return`, docs/05). Both are period, never annualized,
 * so they sit alongside the benchmark comparison's own period return for the
 * same window. JSON-only (no `format=csv`).
 */
export function getTotalReturn(from: string, to: string): Promise<TotalReturnReport> {
  assertRange(from, to);
  return api<TotalReturnReport>(`/reports/total-return${qs({ from, to })}`);
}

// --- Raw format=csv / format=html downloads ----------------------------------
//
// The JSON wrapper always parses responses as JSON, so CSV/HTML variants go
// through `apiBinary` (docs/05): it attaches the bearer token, runs the shared
// 401 → refresh → replay dance, parses the `{detail, code}` envelope, and
// returns the raw `Response` for `.text()`/`.blob()` consumption.

async function fetchText(path: string): Promise<string> {
  return (await apiBinary(path)).text();
}

/** Triggers a browser download of `format=csv` output for any report path. */
export function downloadCsv(path: string, filename: string): Promise<void> {
  return fetchText(`${path}${path.includes("?") ? "&" : "?"}format=csv`).then((csv) => {
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  });
}

/**
 * How long the printable document's blob URL is kept alive. A `blob:` document
 * INHERITS the app's origin, so while it exists it can read `localStorage` and
 * make authenticated same-origin requests; the URL is revoked as soon as the
 * new tab has committed the navigation, and a timer is the only handle left
 * because `noopener` deliberately gives us no window reference to hang a
 * `load` listener on. Every other `createObjectURL` in the app revokes inline
 * (a download anchor has already been clicked by then); this one cannot, so the
 * exposure is bounded by this delay instead of being unbounded.
 */
const PRINTABLE_BLOB_TTL_MS = 1000;

/**
 * Opens the printable HTML variant (docs/06) in a new tab via a blob URL —
 * a plain link could not carry the Authorization header.
 */
export function openPrintableHtml(path: string): Promise<void> {
  return fetchText(`${path}${path.includes("?") ? "&" : "?"}format=html`).then((html) => {
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    if (!window.open(url, "_blank", "noopener")) {
      // Popup blocked: nothing will ever load the blob, so drop it now.
      URL.revokeObjectURL(url);
      return;
    }
    setTimeout(() => URL.revokeObjectURL(url), PRINTABLE_BLOB_TTL_MS);
  });
}
