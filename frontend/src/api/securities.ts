import { api, tokens } from "./client";
import type { Position } from "./portfolio";

// securities (docs/02, docs/05): CRUD + yfinance symbol lookup.

export type SecurityKind = "stock" | "etf" | "mutual_fund";

export interface Security {
  id: number;
  symbol: string;
  name: string;
  kind: SecurityKind;
  listing_currency: string;
  exchange: string;
  /**
   * M141 (docs/07 §Dead symbols): Yahoo answered "quote not found" for this
   * symbol {@link DEAD_SYMBOL_MISSES} times in a row, so the automatic
   * fan-out has stopped fetching it. A price can still arrive via a manual
   * "Try refresh" — and clears the flag the moment one does.
   */
  dead_symbol: boolean;
  /** Consecutive HARD "quote not found" outcomes. Rate-limits never count. */
  quote_miss_count: number;
  /** The last "quote not found" verdict, verbatim. */
  dead_symbol_reason: string | null;
}

/** Consecutive hard "quote not found" outcomes before the fan-out stops (M141). */
export const DEAD_SYMBOL_MISSES = 3;

/** M141 add-time symbol validation (docs/08 §Quote check). */
export interface QuoteCheck {
  symbol: string;
  /**
   * Tri-state on purpose: `null` means we COULD NOT ASK (network failure,
   * rate-limit, privacy mode). An answer we do not have is never reported as
   * "no quote" — the form must not warn on a flaky connection.
   */
  quote_found: boolean | null;
  reason: string | null;
}

/**
 * GET /securities/quote-check — one yfinance probe, the same call the price
 * fan-out makes. Never a hard block: a symbol with no listing yet is a
 * legitimate thing to watch (docs/07).
 */
export async function checkQuote(symbol: string): Promise<QuoteCheck> {
  return api<QuoteCheck>(`/securities/quote-check?symbol=${encodeURIComponent(symbol)}`);
}

export interface SecurityPage {
  items: Security[];
  total: number;
  limit: number;
  offset: number;
}

export interface SecurityInput {
  symbol: string;
  name: string;
  kind: SecurityKind;
  listing_currency: string;
  exchange: string;
}

/**
 * yfinance autofill result (docs/05); failures degrade to 404
 * {code:"lookup_unavailable"} (docs/07).
 *
 * M142: one endpoint, three answers, told apart by `match` (docs/08).
 * - `direct` / `suffix` — the typed TICKER resolved; `symbol` is what was sent.
 * - `search` — a typed NAME resolved to ONE canonical listing, and `symbol`
 *   is that ticker (`APOTEX` → `APTX.TO`).
 * - `ambiguous` — several listings matched: the five result fields are null
 *   and `candidates` is the pick-one chooser. A guess is never stored.
 *
 * M143: `ambiguous` now also covers a bare-vs-`.TO` COLLISION — both markets
 * answer for `VGRO`, so which one is meant is a question only the user can
 * answer (docs/07 §Candidate resolution).
 */
export interface SecurityLookupCandidate {
  symbol: string;
  name: string;
  kind: SecurityKind;
  /** "USD" / "CAD" — M143: the chooser shows it, because that IS the choice. */
  listing_currency: string;
  exchange: string;
  /** Yahoo's human venue name ("Toronto"); null → render `exchange`. */
  exchange_display: string | null;
}

/** A chooser row IS a create-ready listing (M143) — no second lookup. */
export function candidateListing(candidate: SecurityLookupCandidate): SecurityInput {
  return {
    symbol: candidate.symbol,
    name: candidate.name,
    kind: candidate.kind,
    listing_currency: candidate.listing_currency,
    exchange: candidate.exchange,
  };
}

export interface SecurityLookupResult {
  symbol: string | null;
  name: string | null;
  kind: SecurityKind | null;
  listing_currency: string | null;
  exchange: string | null;
  exchange_display: string | null;
  match: "direct" | "suffix" | "search" | "ambiguous";
  candidates: SecurityLookupCandidate[];
}

/**
 * The resolvable half of a lookup answer — a create-ready listing, or null
 * when the lookup came back ambiguous (docs/08). Callers narrow ONCE here
 * instead of re-checking five nullable fields at every use site.
 */
export function resolvedListing(result: SecurityLookupResult): SecurityInput | null {
  if (
    result.match === "ambiguous" ||
    result.symbol === null ||
    result.name === null ||
    result.kind === null ||
    result.listing_currency === null
  ) {
    return null;
  }
  return {
    symbol: result.symbol,
    name: result.name,
    kind: result.kind,
    listing_currency: result.listing_currency,
    exchange: result.exchange ?? "",
  };
}

/** "APOTEX HEALTH CORP (Toronto, CAD)" — the chip/row label (docs/09). */
export function listingLabel(
  listing: { name: string | null; exchange: string | null; exchange_display?: string | null },
  currency?: string | null,
): string {
  const venue = listing.exchange_display ?? listing.exchange ?? "";
  const name = listing.name ?? "";
  return currency ? `${name} (${venue}, ${currency})` : `${name} (${venue})`;
}

export async function listSecurities(limit = 50, offset = 0): Promise<SecurityPage> {
  return api<SecurityPage>(`/securities?limit=${limit}&offset=${offset}`);
}

export async function lookupSecurity(symbol: string): Promise<SecurityLookupResult> {
  return api<SecurityLookupResult>(`/securities/lookup?symbol=${encodeURIComponent(symbol)}`);
}

export async function createSecurity(input: SecurityInput): Promise<Security> {
  return api<Security>("/securities", { method: "POST", body: input });
}

export async function updateSecurity(
  id: number,
  patch: Partial<SecurityInput>,
): Promise<Security> {
  return api<Security>(`/securities/${id}`, { method: "PATCH", body: patch });
}

export async function deleteSecurity(id: number): Promise<void> {
  await api<null>(`/securities/${id}`, { method: "DELETE" });
}

/**
 * One payload for the security dashboard page (docs/05): latest cached price,
 * 365-day stats + history, holdings via the shared positions math, totals,
 * watchlist membership and recent trades. Decimal fields arrive as strings.
 */
export interface SecurityOverview {
  /** The full Security row, including the M141 dead-symbol state. */
  security: Security;
  /** Newest usable close; null when no prices yet. */
  latest: {
    date: string; // ISO date
    price: string;
    source: string; // "manual" | "yfinance"
    age_days: number;
    stale: boolean;
    /** M129 daily delta — server-computed vs the close strictly before
     *  `date` (docs/08); all three null on a first-day listing. */
    previous_close: string | null;
    day_change_native: string | null;
    day_change_pct: number | null;
  } | null;
  stats: {
    high_52w: string | null;
    low_52w: string | null;
    change_pct: number | null;
    cached_days: number;
  };
  /** Closes within the last 365 days, ascending. */
  history: { date: string; price: string }[];
  /** Same dicts as GET /portfolio/positions, filtered to this security. */
  positions: Position[];
  totals: {
    quantity: string;
    book_cost_cad: string | null;
    market_value_cad: string | null;
    /** M129 "own impact" block — server sums of the SAME per-position fields
     *  the holdings table renders (docs/08), so headline and table can never
     *  disagree. */
    unrealized_gain_cad: string | null;
    unrealized_gain_native: string | null;
    unrealized_gain_pct: number | null;
    /** Σ day_change_cad — the day's move on your shares of this security. */
    day_change_cad: string | null;
  };
  watchlisted: boolean;
  fundamentals: Fundamentals | null;
  holdings: EtfHoldingsBlock | null;
  recent_trades: {
    id: number;
    account_id: number;
    action: string;
    date: string; // ISO date
    quantity: string;
    price_native: string;
  }[];
  /**
   * M129 — CLIENT-ONLY, exactly like `PositionsResponse.livePatchedAt`:
   * stamped by the SSE price patch the moment a quote moves, dropped by the
   * next SERVER payload (a refetch replaces this object outright). While it
   * is present the security page re-evaluates the price-derived figures
   * (day Δ, market value, unrealized) from server primitives and labels them
   * "live"; the server's numbers win again on the next payload. The server
   * never sends this field.
   */
  livePatchedAt?: number;
}

/** One fund-composition row (M17); weight arrives as a string percent. */
export interface EtfHolding {
  holding_symbol: string | null;
  holding_security_id: number | null;
  holding_name: string;
  asset_class: "stock" | "bond" | "cash" | "commodity" | "crypto" | "other";
  region: string | null;
  country: string | null;
  sector: string | null;
  weight_pct: string;
  asset_class_breakdown: Record<string, string> | null;
}

/** Editable subset sent to PUT /securities/{id}/holdings (manual rows). */
export interface EtfHoldingInput {
  holding_symbol?: string | null;
  holding_name: string;
  asset_class: EtfHolding["asset_class"];
  region?: string | null;
  country?: string | null;
  sector?: string | null;
  weight_pct: string;
}

/** Composition block embedded in the overview / returned by the holdings routes. */
export interface EtfHoldingsBlock {
  items: EtfHolding[];
  total_weight_pct: string;
  source: "manual" | "yfinance" | null;
}

/** Cached research snapshot (M16); Decimal fields arrive as strings. */
export interface Fundamentals {
  trailing_eps: string | null;
  pe_ratio: string | null;
  market_cap: string | null;
  dividend_yield_pct: string | null;
  beta: string | null;
  sector: string | null;
  industry: string | null;
  country: string | null;
  fifty_two_week_high: string | null;
  fifty_two_week_low: string | null;
  source: "yfinance" | "manual";
  fetched_at: string; // ISO date
}

/** PUT /securities/{id}/fundamentals — manual-wins upsert (source forced server-side). */
export async function updateFundamentals(
  id: number,
  patch: Partial<Omit<Fundamentals, "source" | "fetched_at">>,
): Promise<Fundamentals> {
  return api<Fundamentals>(`/securities/${id}/fundamentals`, {
    method: "PUT",
    body: { ...patch, source: "manual" },
  });
}

/** GET /securities/{id}/overview — dashboard payload for one security. */
export async function getSecurityOverview(id: number): Promise<SecurityOverview> {
  return api<SecurityOverview>(`/securities/${id}/overview`);
}
/** GET /securities/{id}/holdings — cached fund composition (M17). */
export async function getHoldings(id: number, recursive = false): Promise<EtfHoldingsBlock> {
  return api<EtfHoldingsBlock>(`/securities/${id}/holdings?recursive=${recursive}`);
}

/** PUT /securities/{id}/holdings — manual bulk replace; weights ≤ 100 total. */
export async function putHoldings(
  id: number,
  items: EtfHoldingInput[],
): Promise<EtfHoldingsBlock> {
  return api<EtfHoldingsBlock>(`/securities/${id}/holdings`, { method: "PUT", body: { items } });
}

/** POST /securities/{id}/holdings/import — yfinance rows replace yfinance rows only. */
export async function importHoldings(id: number): Promise<EtfHoldingsBlock> {
  return api<EtfHoldingsBlock>(`/securities/${id}/holdings/import`, { method: "POST" });
}

/** POST /securities/{id}/refresh — force refresh market data (price + fundamentals). */
export async function refreshSecurity(id: number): Promise<SecurityOverview> {
  return api<SecurityOverview>(`/securities/${id}/refresh`, { method: "POST" });
}

/** SSE transport for cached price updates, including quote freshness. */
export interface PriceUpdate {
  security_id: number;
  symbol: string;
  /** Cached latest close — Decimal serialized as a string (docs/29 F5). */
  price: string;
  date: string;
  source: "manual" | "yfinance";
  age_days: number;
  stale: boolean;
}

export function createPriceStream(securityIds: number[]): EventSource {
  const ids = securityIds.join(",");
  const token = tokens.access;
  const url = token
    ? `/api/v1/securities/stream/prices?security_ids=${ids}&token=${encodeURIComponent(token)}`
    : `/api/v1/securities/stream/prices?security_ids=${ids}`;
  return new EventSource(url);
}
