// accounts — full CRUD client (docs/08 core resources; M9 docs/14).
// `taxable` is derived server-side from `type` (docs/14 C1) and is therefore
// absent from every input payload; it arrives read-only on Account.
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export type AccountType =
  | "chequing"
  | "savings"
  | "credit_card"
  | "line_of_credit"
  | "brokerage_non_registered"
  | "rrsp"
  | "tfsa"
  | "fhsa"
  | "resp"
  | "lira"
  | "rdsp";
/** Investment-capable account types (W11): mirror of backend INVESTMENT_ACCOUNT_TYPES frozenset. */
export const INVESTMENT_ACCOUNT_TYPES: Readonly<Partial<Record<AccountType, true>>> = {
  brokerage_non_registered: true,
  rrsp: true,
  tfsa: true,
  fhsa: true,
  resp: true,
  lira: true,
  rdsp: true,
};

/**
 * Pure filter for investment-capable accounts (W11): keeps only account types
 * in the INVESTMENT_ACCOUNT_TYPES membership set. Lifted out of the pages so
 * the rule lives in exactly one place.
 */
export function filterInvestmentAccounts<T extends { type: AccountType }>(
  accounts: T[],
): T[] {
  return accounts.filter((a) => INVESTMENT_ACCOUNT_TYPES[a.type] === true);
}

/**
 * Fetches accounts and returns the investment-capable subset plus the raw
 * query result, so callers that also need the full list (e.g. name maps) can
 * reuse a single request instead of re-querying.
 */
export function useInvestmentAccounts() {
  const accounts = useQuery({
    queryKey: ["accounts", 200],
    queryFn: () => listAccounts(200),
  });
  const investmentAccounts = useMemo(
    () => filterInvestmentAccounts(accounts.data?.items ?? []),
    [accounts.data],
  );
  return { accounts, investmentAccounts };
}

export interface Account {
  id: number;
  institution_id: number | null;
  name: string;
  type: AccountType;
  currency: string;
  taxable: boolean;
  llm_autopilot: boolean;
  /**
   * M145 (docs/08 §cash_balance): KIND-dependent — cash accounts report the
   * committed-LEDGER sum + starting_balance; investment accounts report the
   * value chart's own docs/05 cash chain, so the card and the chart cannot
   * disagree. `null` = no honest figure for an investment account (a foreign
   * cash leg past the M115 fx window) — render "—", never a fabricated $0.
   */
  cash_balance: string | null;
  /** Market value of securities held in the account, native currency, "0.00" if none. */
  investment_value: string;
  /** cash_balance + investment_value, native currency, quantized "0.00"; null exactly when cash_balance is. */
  total_value: string | null;
  /** Balance before earliest imported transaction; "0.00" if unset. */
  starting_balance: string;
  /** E2EE Phase 2 (docs/33): opaque name ciphertext; null = legacy row. */
  name_cipher: string | null;
  /**
   * M113 (docs/08): `pending_review` import batches on THIS account — a
   * count, never batch contents. Computed by the single-account responses
   * (`GET /accounts/{id}` / POST / PATCH); list responses and the partner
   * projection carry 0 (not computed there). The account page's banner is
   * the only consumer.
   */
  pending_import_count: number;
}

export interface AccountPage {
  items: Account[];
  total: number;
  limit: number;
  offset: number;
}

export interface AccountCreateInput {
  name: string;
  type: AccountType;
  /** ISO currency code, uppercased 3-letter. */
  currency: string;
  institution_id?: number | null;
  starting_balance?: number | null;
  name_cipher?: string | null;
}

export interface AccountUpdateInput {
  name?: string;
  type?: AccountType;
  currency?: string;
  institution_id?: number | null;
  llm_autopilot?: boolean;
  starting_balance?: number | null;
  name_cipher?: string | null;
}

export async function listAccounts(limit = 50, offset = 0): Promise<AccountPage> {
  return api<AccountPage>(`/accounts?limit=${limit}&offset=${offset}`);
}

/** POST /accounts → 201 body. */
export function createAccount(input: AccountCreateInput): Promise<Account> {
  return api<Account>("/accounts", { method: "POST", body: input });
}

/** PATCH /accounts/{id} — partial update; unknown fields rejected by the API. */
export function updateAccount(id: number, patch: AccountUpdateInput): Promise<Account> {
  return api<Account>(`/accounts/${id}`, { method: "PATCH", body: patch });
}

/**
 * DELETE /accounts/{id}. 204-safe through the shared parse; a referenced
 * account raises ApiError(409, detail, "account_in_use") — surface its message.
 */
export function deleteAccount(id: number): Promise<void> {
  return api<void>(`/accounts/${id}`, { method: "DELETE" }).then(() => undefined);
}

export interface LedgerEntry {
  transaction_id: number;
  date: string;
  description: string;
  amount: string;
  running_balance: string;
}

export interface LedgerPage {
  items: LedgerEntry[];
  starting_balance: string;
  current_balance: string;
  total: number;
}

export function getAccountLedger(accountId: number): Promise<LedgerPage> {
  return api<LedgerPage>(`/accounts/ledger?account_id=${accountId}`);
}

export function getAccount(id: number): Promise<Account> {
  return api<Account>(`/accounts/${id}`);
}


// --- M118: per-account value history (docs/08 §Account value history) --------

/** Query granularity the endpoint accepts; the ANSWER may be coarser (the
 *  response coarsens day → week → month → year past the ~400-point roofline). */
export type ValueRequestGranularity = "day" | "week" | "month";
export type ValueGranularity = ValueRequestGranularity | "year";

/** Closed set of marker labels (docs/08 §Event markers) — display copy lives
 *  in the chart, these are the wire tokens. */
export type ValueEventKind =
  | "contribution"
  | "withdrawal"
  | "transfer_in"
  | "transfer_out"
  | "deposit";

/**
 * WHY a bucket has no valuation (M144, docs/08 §Honesty — the two-reason
 * empty state). `no_price` / `fx` are DATA gaps; `not_held` is a HOLDINGS
 * gap — the account held nothing on that day, so nothing is missing.
 */
export type ValueHistoryGapReason = "no_price" | "fx" | "not_held";

export interface ValueHistoryBucket {
  bucket_end: string;
  /** null = the figure is unknown for this period (chart draws a GAP). */
  cash_cad: string | null;
  positions_cad: string | null;
  value_cad: string | null;
  /** Real figure, but an input was carried forward past docs/07 staleness. */
  estimated: boolean;
  valued: boolean;
  /** Null whenever `valued` is true. */
  gap_reason: ValueHistoryGapReason | null;
}

export interface ValueHistoryEvent {
  /** Stable within the response: `trade:<id>` | `transaction:<id>`. */
  id: string;
  source: "trade" | "transaction";
  trade_id: number | null;
  transaction_id: number | null;
  date: string;
  bucket_end: string;
  kind: ValueEventKind;
  /** +1 money/value in, −1 out — `amount_cad` carries the same sign. */
  direction: -1 | 1;
  amount_cad: string;
  amount_native: string;
  currency: string;
  payee: string | null;
  category_name: string | null;
  symbol: string | null;
  security_id: number | null;
  quantity: string | null;
  /** The raw trade action (`contribution` even when the amount is negative). */
  action: string | null;
}

export interface AccountValueHistory {
  account_id: number;
  from: string;
  to: string;
  granularity: ValueGranularity;
  requested_granularity: ValueGranularity;
  /** Currency of every *_cad field. */
  currency: string;
  native_currency: string;
  min_event_value: string;
  /** False when nothing is recorded yet — render the empty state, not $0. */
  has_data: boolean;
  buckets: ValueHistoryBucket[];
  events: ValueHistoryEvent[];
}

/**
 * GET /accounts/{id}/value-history — the account's value timeline plus its
 * significant-event markers. Defaults live server-side (`from` = the account's
 * creation date, `to` = today, `min_event_value` = 1000); the response is
 * read-only and carries `Cache-Control: private, max-age=60`, mirrored here by
 * the caller's `staleTime`.
 */
export function getAccountValueHistory(
  accountId: number,
  params: {
    from?: string;
    to?: string;
    granularity?: ValueRequestGranularity;
    minEventValue?: number;
  } = {},
): Promise<AccountValueHistory> {
  const qs = new URLSearchParams();
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.granularity) qs.set("granularity", params.granularity);
  if (params.minEventValue !== undefined) qs.set("min_event_value", String(params.minEventValue));
  const query = qs.toString();
  return api<AccountValueHistory>(`/accounts/${accountId}/value-history${query ? `?${query}` : ""}`);
}
