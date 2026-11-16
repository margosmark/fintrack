import { api } from "./client";
import type {
  ValueGranularity,
  ValueHistoryBucket,
  ValueHistoryEvent,
  ValueRequestGranularity,
} from "./accounts";

// portfolio (docs/05, docs/08): signed positions + currency-exposure views.
// Amounts are Decimal values serialized as strings.

export interface Position {
  account_id: number;
  account_name: string;
  security_id: number;
  symbol: string;
  listing_currency: string;
  /** Signed quantity; negative = short. Decimal string. */
  quantity: string;
  price_date: string | null;
  price: string | null;
  /** "manual" | "yfinance" — newest usable price source (docs/07). */
  price_source: string | null;
  price_age_days: number | null;
  /** REAL provider-fetch evidence (Option A, t_bd5ec95f): the instant a
   *  provider response actually landed for this cached row (ISO, docs/07
   *  §Fetch provenance). Absent or null = NO evidence — the UI omits the
   *  fetch-age segment entirely (never "0s"/"just now"/placeholder copy).
   *  Sub-day age is derived from THIS field only, never from `price_date`. */
  price_fetched_at?: string | null;
  price_stale: boolean | null;
  /** Cost basis in CAD (w12): persisted pool ACB for non-registered, pure
   * average-cost replay for registered; null when the pool cache is missing. */
  book_cost_cad: string | null;
  /** Up to the LAST 30 closes <= today ascending (Decimal strings); [] when
   * the security has no prices. */
  spark: string[];
  market_value_native: string | null;
  market_value_cad: string | null;
  /** M129 — every field below is SERVER-computed (docs/08); None means "no
   *  data", never a zero the UI may invent. */
  /** The exact listing→CAD rate the server used for this row — the ONLY rate
   *  a live-tick re-evaluation is allowed to multiply by (docs/09). */
  fx_rate_to_cad: string | null;
  /** Newest cached close STRICTLY before `price_date`; null on a first-day
   *  listing, which renders without a delta rather than with a fake one. */
  previous_close_native: string | null;
  /** M130 — the DATE of that baseline, so the portfolio header can label the
   *  move "vs Oct 7 close" with the real market day, never "yesterday". */
  previous_close_date: string | null;
  /** price − previous_close_native, in the listing currency. */
  day_change_native: string | null;
  /** day_change ÷ previous_close, percent, rounded to 2 dp. */
  day_change_pct: number | null;
  /** cents(quantity × Δ × rate) — the day's move on THIS holder's shares. */
  day_change_cad: string | null;
  /** market_value_cad − book_cost_cad (and the native / percent siblings). */
  unrealized_gain_cad: string | null;
  unrealized_gain_native: string | null;
  unrealized_gain_pct: number | null;
}

export interface PositionsResponse {
  items: Position[];
  total: number;
  /** M129: Σ unrealized_gain_cad across the items (rows with an unknown book
   *  cost contribute nothing); null when no row can compute one. */
  total_unrealized_cad: string | null;
  // --- M130: the header card's block (docs/08 §Portfolio summary) ----------
  // Every field below is computed SERVER-SIDE from the SAME `items` this
  // payload ships, so the header can never show a figure the table disagrees
  // with. The client adds nothing and subtracts nothing (docs/09).
  /** Σ market_value_cad over `items` — the holdings basis, exact. */
  total_value_cad: string;
  /** Cash of the in-scope investment accounts: docs/05's cash leg at today
   *  (starting balance + committed transactions + trade cash). `null` when a
   *  leg has no usable rate — never a fabricated zero. */
  cash_cad: string | null;
  /** total_value_cad + cash_cad — the headline. `null` iff `cash_cad` is. */
  portfolio_value_cad: string | null;
  /** Σ day_change_cad over `items` (rows without a prior close are excluded,
   *  the same rule as `total_unrealized_cad`); null when no row computes one. */
  total_day_change_cad: string | null;
  /** total_day_change_cad ÷ (total_value_cad − total_day_change_cad), 2 dp. */
  total_day_change_pct: number | null;
  /** The most recent prior-close DATE the Σ is measured from — the honest
   *  "vs Oct 7 close" label. Null when no row has a baseline. */
  day_change_as_of: string | null;
  /** True when the contributing rows do NOT share one baseline date: the
   *  label names the latest and this flag says the rest closed earlier. */
  day_change_mixed_baseline: boolean;
  /**
   * M126 L1 — CLIENT-ONLY. Set by the SSE price patch the moment a quote
   * moves (`usePriceStream`), and dropped by the next SERVER payload because
   * a refetch replaces this object outright. It is what lets the table say
   * "totals update on next refresh" without a timer or a second clock: the
   * marker's lifetime IS the gap between the live quote and the server's own
   * cadence. The server never sends this field.
   */
  livePatchedAt?: number;
}

export interface ExposureRow {
  listing_currency: string;
  total_market_value_native: string;
  holdings_count: number;
}

export interface ExposureResponse {
  items: ExposureRow[];
  total: number;
}

export async function getPositions(accountId?: number): Promise<PositionsResponse> {
  return api<PositionsResponse>(
    accountId !== undefined
      ? `/portfolio/positions?account_id=${accountId}`
      : "/portfolio/positions",
  );
}

export async function getExposure(accountId?: number): Promise<ExposureResponse> {
  return api<ExposureResponse>(
    accountId !== undefined
      ? `/portfolio/exposure?account_id=${accountId}`
      : "/portfolio/exposure",
  );
}

/** M18 look-through breakdown (docs/05): one exposure bucket. */
export interface BreakdownRow {
  key: string;
  market_value_cad: string;
  weight_pct: number;
  /** M166: the part of THIS bucket allocated from a fund's own published
   * sector mix instead of a sourced per-holding column. A bucket can be
   * part sourced and part estimated; `"0.00"` when it is all sourced. */
  estimated_cad?: string;
  /** True when `estimated_cad > 0` — the legend marks those rows. */
  estimated?: boolean;
}

/** M147 honesty copy (docs/09): a fund position that landed in
 * Unclassified — the UI names it and says WHY. `missing` = zero
 * composition rows (an Import fills it); `partial` = the provider only
 * publishes the fund's top slice, so the remainder waits on a fuller
 * source (`imported_weight_pct` is what DID import). */
export interface UnclassifiedFund {
  security_id: number;
  symbol: string;
  name: string;
  market_value_cad: string;
  reason?: "missing" | "partial";
  imported_weight_pct?: string | null;
}

export interface BreakdownResponse {
  dimension: "sector" | "geography" | "asset_class" | "company";
  items: BreakdownRow[];
  total_cad: string;
  unclassified_cad: string;
  /** M166: total value allocated from funds' own published sector mixes.
   * Zero for every dimension except sector, and for portfolios whose funds
   * all publish their holdings per-row. */
  estimated_cad?: string;
  unclassified_funds?: UnclassifiedFund[];
  /** M169: present ONLY when the request carried `drill` — the same walk's
   * attribution trail for that one bucket (docs/16 §Exposure drill). */
  drill?: DrillBlock;
}

// --- M169: the exposure drill-down (docs/08 §Breakdown drill, docs/16) --------

/**
 * How a row got into the bucket. `direct` — you hold the security itself.
 * `fund` — a position the look-through walked into (its `holdings` follow).
 * `sub_fund` — a fund the walk STOPPED at: the depth cap, a cycle back-edge,
 * or a fund with no rows of its own. Never a company.
 */
export type DrillPositionKind = "direct" | "fund" | "sub_fund";

/**
 * One row of a drill — shared by a contribution (a top-level position) and
 * by a holding (a leaf inside a fund). A contribution adds `holdings`; a
 * holding adds `via` and `native_weight_share`.
 */
export interface DrillRow {
  position_kind: DrillPositionKind;
  security_id: number | null;
  symbol: string | null;
  name: string | null;
  /** The accounts the TOP-LEVEL position sits in — a fund's leaves are held
   * through the fund's accounts, never one of their own. */
  account_ids: number[];
  account_count: number;
  /** The provider's own published weight for this row inside its parent, as a
   * Decimal string — before this walk's renormalization. null on a position
   * (which has no parent) and on the estimate/residual rows (which have no
   * row at all). */
  native_weight_share: string | null;
  market_value_cad: string;
  /** Share of the bucket THIS row sits in, 2 dp. A contribution's is a share
   * of the bucket; a holding's is also a share of the bucket (not of its
   * fund), so every percentage on the wire describes the same whole. */
  weight_pct: number;
  /** M149 provenance of the composition row: vanguard | issuer | yfinance |
   * manual. null when the row has no source of its own (a position, or the
   * estimate/residual). */
  provenance_source: string | null;
  /** M166: this slice came from a fund's PUBLISHED sector mix, not a
   * per-holding figure. */
  estimated: boolean;
  /** The denominator rule left this slice in Unclassified — a residual, or a
   * fund with no rows. Real dollars, honestly named. */
  unattributed: boolean;
  /** The funds the money passed through to reach here, outermost first. A
   * Microsoft leaf reached as VEQT → VXUS carries ["VEQT","VXUS"], so a leaf
   * is never mistaken for something held directly. */
  via: string[];
  /** `via.length` — 0 on a top-level position, capped at `depth_cap`. */
  depth: number;
  /** Contributions only: the leaves this position contributed, value desc,
   * capped at the server's DRILL_N. Empty for a `direct` position (the
   * position IS the leaf) and for a fund with no rows. */
  holdings?: DrillRow[];
  /** The folded tail of `holdings` — its exact cent-sum and its count. */
  holdings_other?: DrillOther | null;
  /** Contributions only. */
  estimated_cad?: string;
  unattributed_cad?: string;
}

/** M167's truncation rule, on the wire: the tail's exact arithmetic. */
export interface DrillOther {
  count: number;
  market_value_cad: string;
  weight_pct: number;
}

export interface DrillBlock {
  key: string;
  dimension: "sector" | "geography" | "asset_class" | "company";
  bucket_market_value_cad: string;
  bucket_weight_pct: number;
  /** THE drill total — the server's number. Never re-add the rows: each one
   * rounds independently and they can drift a cent from this. */
  contributions_cad: string;
  contributions: DrillRow[];
  /** The folded tail of `contributions`. */
  other: DrillOther | null;
  /** How far the M19 walk goes. A deeper chain stays hidden server-side. */
  depth_cap: number;
}
export type BreakdownDimension = BreakdownResponse["dimension"];

/**
 * GET /portfolio/breakdown?dimension=… — the M18 look-through.
 *
 * M169: passing `drill` (a bucket's exact `key`, URL-encoded) asks the SAME
 * endpoint for that bucket's attribution trail. It is one extra query
 * parameter, not a second endpoint, so a caller that omits it gets a payload
 * byte-identical to the pre-M169 one. A key that is not a bucket in this
 * dimension answers 404 `drill_bucket_not_found`.
 */
export async function getBreakdown(
  dimension: BreakdownDimension,
  accountId?: number,
  drill?: string,
): Promise<BreakdownResponse> {
  const query = new URLSearchParams({ dimension });
  if (accountId !== undefined) {
    query.set("account_id", String(accountId));
  }
  if (drill !== undefined) {
    query.set("drill", drill);
  }
  return api<BreakdownResponse>(`/portfolio/breakdown?${query.toString()}`);
}

// --- M130: the whole portfolio's value timeline (docs/08 §Portfolio value
// history) — M118's per-account series summed across every investment account.

/** The window's move, server-computed from the series it ships with — the
 *  header's "over <range>" figure is this object, never client subtraction. */
export interface PortfolioValueStats {
  from_bucket: string | null;
  to_bucket: string | null;
  from_value_cad: string | null;
  to_value_cad: string | null;
  change_cad: string | null;
  change_pct: number | null;
}

/** One marker with EVERY account that produced it (deduped by `id`, so a
 *  multi-account event renders once and its tooltip names them all). */
export interface PortfolioValueEvent extends ValueHistoryEvent {
  account_ids: number[];
  account_names: string[];
}

export interface PortfolioValueHistory {
  /** The investment accounts in the sum, ascending. */
  account_ids: number[];
  from: string;
  to: string;
  granularity: ValueGranularity;
  requested_granularity: ValueGranularity;
  /** Currency of every *_cad field. `native_currency` mirrors it: a portfolio
   *  has no single native currency, so the chart's "values in …" badge stays
   *  off rather than claim one account's. */
  currency: string;
  native_currency: string;
  min_event_value: string;
  /** False when no investment account has anything recorded — render the
   *  three-part empty state, never a flat $0 line. */
  has_data: boolean;
  buckets: ValueHistoryBucket[];
  events: PortfolioValueEvent[];
  stats: PortfolioValueStats;
}

/**
 * GET /portfolio/value-history?from=&to=&granularity= — the summed
 * investment-account timeline (M130). Defaults, the 400-point coarsening and
 * the `Cache-Control: private, max-age=60` header all mirror
 * `/accounts/{id}/value-history`; callers mirror it with `staleTime`.
 */
export function getPortfolioValueHistory(
  params: {
    from?: string;
    to?: string;
    granularity?: ValueRequestGranularity;
    minEventValue?: number;
  } = {},
): Promise<PortfolioValueHistory> {
  const qs = new URLSearchParams();
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.granularity) qs.set("granularity", params.granularity);
  if (params.minEventValue !== undefined) qs.set("min_event_value", String(params.minEventValue));
  const query = qs.toString();
  return api<PortfolioValueHistory>(`/portfolio/value-history${query ? `?${query}` : ""}`);
}

/**
 * POST /portfolio/refresh → 202 {queued, skipped_dead}: BackgroundTasks refresh
 * prices for every DISTINCT security held OR watched by the caller (W11).
 *
 * M141: rows flagged `dead_symbol` are NOT queued (docs/07 §Dead symbols) —
 * this sweep is the fan-out whose repeated 404s the flag exists to stop. They
 * are counted in `skipped_dead` so the UI can state what it did not do rather
 * than imply every row was refreshed.
 */
export async function refreshPrices(): Promise<{ queued: number; skipped_dead: number }> {
  return api<{ queued: number; skipped_dead: number }>("/portfolio/refresh", {
    method: "POST",
  });
}
