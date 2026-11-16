import { api } from "./client";

// prices (docs/02, docs/07, docs/08): manual + yfinance cache history.
// POST /prices is an upsert keyed (security_id, date) that always writes
// source='manual' — human data overwrites machine data.

export type PriceSource = "manual" | "yfinance";

export interface Price {
  id: number;
  security_id: number;
  date: string;
  /** Decimal serialized as string. */
  price: string;
  source: PriceSource;
}

export interface PricePage {
  items: Price[];
  total: number;
  limit: number;
  offset: number;
}

export async function listPrices(
  securityId?: number | null,
  limit = 50,
  offset = 0,
): Promise<PricePage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (securityId != null) params.set("security_id", String(securityId));
  return api<PricePage>(`/prices?${params.toString()}`);
}

/** Manual upsert (docs/08): sets price + source='manual', even over existing rows. */
export async function upsertManualPrice(input: {
  security_id: number;
  date: string;
  price: string;
}): Promise<Price> {
  return api<Price>("/prices", { method: "POST", body: input });
}

/** One chart point of the M126 series; `price` is a Decimal-as-string. */
export interface PriceHistoryPoint {
  date: string;
  price: string;
}

/**
 * `GET /prices/history` (docs/08 §Price history): cache-only closes for one
 * security, bucketed server-side. `from` is the window actually SERVED and
 * `granularity` may differ from `requested_granularity` when the server
 * auto-picked it or coarsened an explicit ask past the point roofline
 * (docs/12 §Range → granularity).
 */
export interface PriceHistoryPage {
  security_id: number;
  from: string;
  to: string;
  granularity: "day" | "week" | "month" | "year";
  requested_granularity: "auto" | "day" | "week" | "month" | "year";
  items: PriceHistoryPoint[];
  count: number;
}

export type PriceGranularity = PriceHistoryPage["requested_granularity"];

/** Cache-only history read — never refreshes the security's backfill, so it
 *  is safe under privacy mode and has no write side effect. */
export async function getPriceHistory(input: {
  securityId: number;
  /** ISO date floor; omit/`null` for the whole cached history. */
  from?: string | null;
  /** ISO date ceiling; defaults to the server's today. */
  to?: string | null;
  granularity?: PriceGranularity;
}): Promise<PriceHistoryPage> {
  const params = new URLSearchParams({ security_id: String(input.securityId) });
  if (input.from) params.set("from", input.from);
  if (input.to) params.set("to", input.to);
  params.set("granularity", input.granularity ?? "auto");
  return api<PriceHistoryPage>(`/prices/history?${params.toString()}`);
}
