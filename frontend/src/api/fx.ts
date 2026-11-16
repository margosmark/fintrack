import { api } from "./client";

// fx (docs/07, docs/08): cached BoC Valet rates + manual gap-filling entries.

export type FxSource = "boc_valet" | "manual";

export interface FxRate {
  base: string;
  quote: string;
  date: string;
  /** Decimal serialized as string. */
  rate: string;
  source: FxSource;
}

/** Rate with cache/fallback semantics (docs/07); date optional = latest. */
export async function getFxRate(base: string, quote: string, date?: string): Promise<FxRate> {
  const params = new URLSearchParams({ base, quote });
  if (date) params.set("date", date);
  return api<FxRate>(`/fx?${params.toString()}`);
}

/**
 * Manual rate entry -> source=manual; fills gaps only (first writer wins): an
 * existing row — BoC or manual — fails with 409 {code:"fx_date_not_gap"}, and
 * a date after today fails with 422 {code:"fx_future_date"}.
 */
export async function createManualRate(input: {
  base: string;
  quote: string;
  date: string;
  rate: string;
}): Promise<FxRate> {
  return api<FxRate>("/fx", { method: "POST", body: input });
}

export interface FxBoardPoint {
  date: string;
  rate: string;
}

export interface FxBoardItem {
  base: string;
  latest_rate: string | null;
  as_of: string | null;
  series: FxBoardPoint[];
}

export interface FxBoard {
  currencies: FxBoardItem[];
}

/**
 * GET /fx range — cache-only (`fetch=false`): reports use this so rendering
 * never triggers a BoC fetch (docs/12). Rates come back ascending by date.
 */
export async function getFxRange(
  base: string,
  quote: string,
  start: string,
  end: string,
): Promise<FxRate[]> {
  const params = new URLSearchParams({ base, quote, start_date: start, end_date: end, fetch: "false" });
  const res = await api<{ items: FxRate[] }>(`/fx?${params.toString()}`);
  return res.items;
}

export async function getFxBoard(days = 90): Promise<FxBoard> {
  return api<FxBoard>(`/fx/board?days=${days}`);
}

export interface FxPairStatus {
  base: string;
  quote: string;
  /** Newest cached row on or before today — never a carried-forward value. */
  as_of: string | null;
  age_days: number | null;
  /** Past the 7-day carry-forward window, or no row at all (docs/07). */
  stale: boolean;
  source: FxSource | null;
}

export interface FxStatus {
  reporting_currency: string;
  /** The WORST-covered pair in use — the one that decides if numbers drift. */
  as_of: string | null;
  age_days: number | null;
  stale: boolean;
  /** Empty when the tenant holds no foreign currency (no FX dependency). */
  pairs: FxPairStatus[];
}

/**
 * GET /fx/status — how old the rate book is for the currencies that actually
 * price this tenant's money (M150, docs/07). Cache-only server-side, so this
 * is a plain read that can never trigger a BoC fetch.
 */
export async function getFxStatus(): Promise<FxStatus> {
  return api<FxStatus>("/fx/status");
}
