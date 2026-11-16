import { api } from "./client";

// watchlist (docs/05, W11): securities tracked but not held. Same shape as the
// price fields on Position — Decimal amounts serialized as strings.

export interface WatchlistItem {
  security_id: number;
  symbol: string;
  name: string;
  listing_currency: string;
  latest_price: string | null;
  price_date: string | null;
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
  /** M141: Yahoo disowned this symbol — "no public quote", not "no price". */
  dead_symbol: boolean;
  dead_symbol_reason: string | null;
}

export interface WatchlistResponse {
  items: WatchlistItem[];
  total: number;
}

export async function getWatchlist(): Promise<WatchlistResponse> {
  return api<WatchlistResponse>("/watchlist");
}

/**
 * POST /watchlist → 201 body. Duplicate raises ApiError(409, …,
 * "already_watched"); an unknown/foreign security raises 404.
 */
export async function addWatchlistEntry(securityId: number): Promise<WatchlistItem> {
  return api<WatchlistItem>("/watchlist", { method: "POST", body: { security_id: securityId } });
}

/** DELETE /watchlist/{security_id} — 204-safe through the shared parse. */
export async function removeWatchlistEntry(securityId: number): Promise<void> {
  await api<null>(`/watchlist/${securityId}`, { method: "DELETE" });
}
