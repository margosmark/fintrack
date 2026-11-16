import { api, apiForm, apiWithResponse } from "./client";
import type { ImportBatch } from "./imports";

// trades (docs/02, docs/05): the action enum + signed-quantity conventions.
//
// Input conventions (docs/02): cash actions carry quantity=0 and the gross
// amount in price_native; commission applies only to buy|sell|drip. The
// backend normalizes signs on write: buy/drip stored positive, sell stored
// negative — the UI accepts positive quantities for sells.

export const TRADE_ACTIONS = [
  "buy",
  "sell",
  "dividend",
  "distribution",
  "return_of_capital",
  "drip",
  "fee",
  "contribution",
  "withdrawal",
  "transfer_in",
  "transfer_out",
  "corporate_action_adjustment",
] as const;

export type TradeAction = (typeof TRADE_ACTIONS)[number];

/** Actions where quantity is always 0 and price_native is a gross cash amount. */
export const CASH_ONLY_ACTIONS: readonly TradeAction[] = [
  "dividend",
  "distribution",
  "return_of_capital",
  "fee",
  "contribution",
  "withdrawal",
  "transfer_in",
  "transfer_out",
];

/** Actions that accept a commission field. */
export const COMMISSION_ACTIONS: readonly TradeAction[] = ["buy", "sell", "drip"];

export interface Trade {
  id: number;
  account_id: number;
  security_id: number;
  action: TradeAction;
  date: string;
  /** Decimal serialized as string; negative for sells (docs/02). */
  quantity: string;
  price_native: string;
  commission: string;
  /**
   * The currency `price_native`/`commission` are stated in (M132, docs/02).
   * null on rows written before M132 — read it as the ACCOUNT's currency,
   * exactly like the server, so both cash legs agree to the string.
   */
  currency: string | null;
  fx_rate_used: string | null;
}

export interface TradePage {
  items: Trade[];
  total: number;
  limit: number;
  offset: number;
}

export interface TradeInput {
  account_id: number;
  security_id: number;
  action: TradeAction;
  date: string;
  quantity?: string;
  price_native: string;
  commission?: string;
  fx_rate_used?: string | null;
}

export interface TradeFilters {
  limit?: number;
  offset?: number;
  account_id?: number | null;
  security_id?: number | null;
  action?: TradeAction | null;
  /** Exclude staged import rows (docs/03) — report inputs must be committed-only. */
  committed_only?: boolean;
}

export async function listTrades(filters: TradeFilters = {}): Promise<TradePage> {
  const params = new URLSearchParams();
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  if (filters.offset !== undefined) params.set("offset", String(filters.offset));
  if (filters.account_id != null) params.set("account_id", String(filters.account_id));
  if (filters.security_id != null) params.set("security_id", String(filters.security_id));
  if (filters.action != null) params.set("action", filters.action);
  if (filters.committed_only) params.set("committed_only", "true");
  const qs = params.toString();
  return api<TradePage>(`/trades${qs ? `?${qs}` : ""}`);
}

export async function createTrade(input: TradeInput): Promise<Trade> {
  return api<Trade>("/trades", { method: "POST", body: input });
}

export async function updateTrade(id: number, patch: Partial<TradeInput>): Promise<Trade> {
  return api<Trade>(`/trades/${id}`, { method: "PATCH", body: patch });
}

export async function deleteTrade(id: number): Promise<void> {
  await api<null>(`/trades/${id}`, { method: "DELETE" });
}

export interface DividendSuggestion {
  security_id: number;
  account_id: number;
  symbol: string;
  date: string;
  amount: string;
  quantity: string;
  gross_amount: string;
  currency: string;
}

export interface DividendHistoryStatus {
  pending: number;
  unavailable: number;
  empty: number;
  stale: number;
  /** M186: unavailable sources whose failure is a hard quote miss / dead
   * symbol — a verdict about the symbol, where retrying cannot help. */
  quote_miss?: number;
  as_of: string | null;
  /** Additive provenance keys (docs/07 §M184): sources that have NEVER
   *  answered, the provider those events came from, and the age of the oldest
   *  successful refresh. Older servers omit them — treat absent as 0/null. */
  missing?: number;
  source?: string | null;
  age_days?: number | null;
}

export type DividendSuggestionsResult = DividendSuggestion[] & {
  history: DividendHistoryStatus;
};

export async function getDividendSuggestions(
  accountId?: number,
): Promise<DividendSuggestionsResult> {
  const { data, response } = await apiWithResponse<DividendSuggestion[]>(
    accountId !== undefined
      ? `/trades/dividend-suggestions?account_id=${accountId}`
      : "/trades/dividend-suggestions",
  );
  const history: DividendHistoryStatus = {
    pending: 0,
    unavailable: 0,
    empty: 0,
    stale: 0,
    quote_miss: 0,
    missing: 0,
    source: null,
    age_days: null,
    as_of: null,
  };
  try {
    const encoded = response.headers.get("X-Dividend-History-State");
    if (encoded) Object.assign(history, JSON.parse(encoded));
  } catch {
    // A missing/invalid additive header must not make a valid suggestion body fail.
  }
  return Object.assign(data, { history });
}

export interface DividendRecordInput {
  account_id: number;
  security_id: number;
  date: string;
  gross_amount: string;
  action?: "dividend" | "distribution" | "return_of_capital";
  drip?: boolean;
  drip_price?: string;
  drip_commission?: string;
  fractional?: boolean;
  fx_rate_used?: string | null;
}

export interface DividendRecordResult {
  trade_id: number;
  drip_trade_id: number | null;
  transaction_id: number;
}

export async function recordDividend(input: DividendRecordInput): Promise<DividendRecordResult> {
  return api<DividendRecordResult>("/trades/record-dividend", { method: "POST", body: input });
}

// --- Investment CSV import (docs/05) ------------------------------------------

export interface TradePreviewRow {
  id: number;
  date: string;
  symbol: string;
  security_name: string;
  action: string;
  quantity: string;
  price_native: string;
  commission: string;
  /** The row's own currency (its listing currency), never the account's. */
  currency: string;
  duplicate: boolean;
}
export interface UnmappedRow {
  line: number;
  activity_type: string;
  activity_sub_type: string | null;
  symbol: string;
  date: string | null;
  reason: string;
  /** Profile label that produced the row; null on the legacy parser. */
  institution: string | null;
  /** "stuck" = staging refuses the whole file (422) instead of skipping it. */
  code: string | null;
}

export interface TradeImportResponse {
  batch_id: number;
  /** Brokerage profile slug, or null when the legacy parser ran. */
  profile: string | null;
  /** Institution label written to the import batch (null on the legacy path). */
  institution: string | null;
  imported: number;
  duplicates_skipped: number;
  preview: TradePreviewRow[];
  unmapped: UnmappedRow[];
  /** Per-file caveats, each prefixed with the institution label. */
  notices: string[];
}

// --- Brokerage CSV profiles (docs/41) ---------------------------------------

/**
 * One profile per brokerage — the picker's options, in the backend registry's
 * order (`PROFILE_SLUGS`, pinned by tests/test_brokerage_profiles.py).
 * `""` means auto-detect from the header.
 */
export const BROKERAGE_PROFILES: { slug: string; label: string }[] = [
  { slug: "questrade", label: "Questrade" },
  { slug: "wealthsimple", label: "Wealthsimple" },
  { slug: "ibkr", label: "Interactive Brokers" },
  { slug: "fidelity", label: "Fidelity (US)" },
  { slug: "td-di", label: "TD Direct Investing" },
  { slug: "rbc-di", label: "RBC Direct Investing" },
  { slug: "nbdb", label: "National Bank Direct Brokerage" },
];

/** One parsed row of a zero-write dry run. */
export interface TradeDryRunRow {
  index: number;
  date: string;
  symbol: string;
  security_name: string;
  action: string;
  quantity: string;
  price_native: string;
  commission: string;
  currency: string;
  verdict: "new" | "duplicate";
  security_action: "existing" | "create";
}

export interface TradeDryRunResponse {
  profile: string | null;
  rows: TradeDryRunRow[];
  unmapped: UnmappedRow[];
  notices: string[];
  summary: { total: number; new: number; duplicates: number; unmapped: number };
}

function tradeForm(file: File, accountId: number): FormData {
  const form = new FormData();
  form.append("file", file);
  form.append("account_id", String(accountId));
  return form;
}

function profileQuery(profile?: string | null): string {
  return profile ? `?profile=${encodeURIComponent(profile)}` : "";
}

/** Import trades from an investment / brokerage CSV file (stages a batch). */
export async function importTrades(
  file: File,
  accountId: number,
  profile?: string | null,
): Promise<TradeImportResponse> {
  return apiForm<TradeImportResponse>(
    `/trades/import${profileQuery(profile)}`,
    tradeForm(file, accountId),
  );
}

/** Dry run: what `importTrades` WOULD do. Writes nothing (docs/41). */
export async function importTradesDryRun(
  file: File,
  accountId: number,
  profile?: string | null,
): Promise<TradeDryRunResponse> {
  return apiForm<TradeDryRunResponse>(
    `/trades/import/preview${profileQuery(profile)}`,
    tradeForm(file, accountId),
  );
}
/** Commit a pending trade import batch, optionally force-including duplicate rows. */
export function commitTradeBatch(batchId: number, includeIds: number[] = []): Promise<ImportBatch> {
  return api<ImportBatch>(`/trades/import/${batchId}/commit`, { method: "POST", body: { include_ids: includeIds } });
}

/** Discard a pending trade import batch. */
export function discardTradeBatch(batchId: number): Promise<{ status: string; batch_id: number }> {
  return api(`/trades/import/${batchId}/discard`, { method: "POST" });
}
