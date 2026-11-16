// bills — upcoming bill tracking.
import { api } from "./client";

// ---------------------------------------------------------------------------
// M121 (docs/25 §M121) — a rule is a TERM LIST with AND/OR, not one term.
// ---------------------------------------------------------------------------

export const BILL_MATCH_LOGICS = ["any", "all"] as const;
export type BillMatchLogic = (typeof BILL_MATCH_LOGICS)[number];

/** The segmented control's visible labels — docs/09 pins these words. */
export const BILL_MATCH_LOGIC_LABELS: Record<BillMatchLogic, string> = {
  any: "ANY",
  all: "ALL",
};

/** One sentence per mode, shown under the chips (and as each segment's title). */
export const BILL_MATCH_LOGIC_HINTS: Record<BillMatchLogic, string> = {
  any: "matches a transaction when ANY of these terms matches (wider set)",
  all: "matches a transaction only when EVERY one of these terms matches (narrower set)",
};

/** The rule surface is rate-limited: 1..10 terms of 1..120 chars (§M121 D7). */
export const BILL_MAX_TERMS = 10;
export const BILL_MAX_TERM_LENGTH = 120;

/**
 * What a payload should carry: the committed chips plus whatever is still
 * typed in the input (a term typed but not yet committed is still a term the
 * user asked for — losing it on Save would be a silent wrong rule).
 */
export function composeTerms(terms: string[], draft: string): string[] {
  const typed = draft.trim().slice(0, BILL_MAX_TERM_LENGTH);
  if (typed === "" || terms.includes(typed)) return terms;
  return [...terms, typed];
}

/**
 * Terms read off a server bill. The server always answers with
 * `match_terms`, but a cached M116-era response (or a fixture) may carry only
 * `match_term` — that IS terms[0], so it degrades to the single-term rule
 * the pre-M121 UI would have shown (docs/25 §M121 D6).
 */
export function readTerms(bill: { match_terms?: string[]; match_term?: string }): string[] {
  if (bill.match_terms !== undefined && bill.match_terms.length > 0) return bill.match_terms;
  return bill.match_term ? [bill.match_term] : [];
}

/** `match_logic` read with the same tolerance — default ANY, the widening mode. */
export function readLogic(bill: { match_logic?: BillMatchLogic }): BillMatchLogic {
  return bill.match_logic ?? "any";
}

export interface BillReminder {
  id: number;
  user_id: number;
  name: string;
  amount: string | null;
  category_id: number | null;
  frequency: string;
  next_due_date: string;
  remind_days_before: number;
  enabled: boolean;
  /** M116 D1 — how a term is compared against a transaction's payee. */
  match_mode: BillMatchMode;
  /** M121 D1 — terms[0]; kept so a pre-M121 reader never sees a hole. */
  match_term: string;
  /** M121 D1 — the whole list, in the order the chips show. */
  match_terms: string[];
  /** M121 D2 — ANY (widening, default) or ALL (narrowing). */
  match_logic: BillMatchLogic;
  /** `null` = the bill's `amount` must match exactly. */
  amount_tolerance: string | null;
}

/** Every bill cadence `POST /bills` accepts (docs/25 schema). */
export const BILL_FREQUENCIES = ["monthly", "quarterly", "yearly", "one_time"] as const;

export interface BillPayload {
  name: string;
  amount?: number;
  /**
   * M123 (docs/08, docs/25): the matcher's CATEGORY SCOPE — which transactions
   * this rule may match, not a label. `number` sets it; `null` (PATCH only)
   * clears it, which is why the type is not bare `number`.
   */
  category_id?: number | null;
  frequency: string;
  next_due_date: string;
  remind_days_before?: number;
  /** M116 D1 / M121 D1 — the rule the bill is created with (defaults to `name`). */
  match_mode?: BillMatchMode;
  match_terms?: string[];
  match_logic?: BillMatchLogic;
  amount_tolerance?: number;
  /**
   * M55 (docs/25): detector `payee` of the detected subscription seeding this
   * draft. Create-only — `PATCH /bills/{id}` never carries it.
   */
  from_subscription_id?: string;
}

/** `GET /bills` answers with the M116 B4 numbers folded in. */
export function listBills(month?: string): Promise<Bill[]> {
  const q = month ? `?month=${month}` : "";
  return api<Bill[]>(`/bills${q}`);
}

export function createBill(payload: BillPayload): Promise<BillReminder> {
  return api<BillReminder>("/bills", { method: "POST", body: payload });
}

export function updateBill(id: number, patch: Partial<BillPayload>): Promise<BillReminder> {
  return api<BillReminder>(`/bills/${id}`, { method: "PATCH", body: patch });
}

export function deleteBill(id: number): Promise<void> {
  return api<void>(`/bills/${id}`, { method: "DELETE" }).then(() => undefined);
}

export function markPaid(id: number): Promise<BillReminder> {
  return api<BillReminder>(`/bills/${id}/mark-paid`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// M55 — subscription → bill draft autofill (docs/25)
// ---------------------------------------------------------------------------

/** The detector fields M55 seeds a draft from (`Subscription`, docs/24 M27). */
export interface SubscriptionDraftSource {
  payee: string;
  frequency: string;
  avg_amount: string;
  next_expected: string | null;
}

/**
 * Detected cadence → bill cadence (docs/25 M55 decision 9). `weekly` and
 * `biweekly` have no bill equivalent and must never fall back silently.
 */
export function billFrequencyFor(subscriptionFrequency: string): string | null {
  switch (subscriptionFrequency) {
    case "monthly":
    case "quarterly":
    case "yearly":
      return subscriptionFrequency;
    default:
      return null;
  }
}

/** The hand-off query the subscriptions surface writes onto `/bills`. */
export function billAutofillParams(sub: SubscriptionDraftSource): URLSearchParams | null {
  const frequency = billFrequencyFor(sub.frequency);
  if (frequency === null) return null;
  return new URLSearchParams({
    autofill: sub.payee,
    amount: sub.avg_amount,
    frequency,
    due: sub.next_expected ?? "",
  });
}

/** What the Bills page prefills from `/bills?autofill=…` (docs/25 M55). */
export interface BillAutofill {
  fromSubscriptionId: string;
  name: string;
  amount: string;
  frequency: string;
  dueDate: string;
  /**
   * M116: the term the rule is pre-filled with. Optional in the URL — when it
   * is absent the payee itself is used, because that is the whole point of the
   * hand-off ("create a reminder with match_term X").
   */
  matchTerm: string;
}

/**
 * Parse the hand-off. `autofill` is the only required param; the rest degrade
 * to the ordinary empty-form values rather than ever producing a bad payload
 * (blank amount, `monthly`, blank date). The server stays the authority.
 */
export function readBillAutofill(params: URLSearchParams): BillAutofill | null {
  const payee = params.get("autofill");
  if (!payee) return null;
  const amount = params.get("amount") ?? "";
  const frequency = params.get("frequency") ?? "";
  const due = params.get("due") ?? "";
  const match = params.get("match") ?? "";
  return {
    fromSubscriptionId: payee,
    name: payee,
    amount: amount !== "" && Number.isFinite(Number(amount)) ? amount : "",
    frequency: (BILL_FREQUENCIES as readonly string[]).includes(frequency) ? frequency : "monthly",
    dueDate: /^\d{4}-\d{2}-\d{2}$/.test(due) ? due : "",
    // Blank / over-long / digit-only `match` degrades to the payee rather than
    // to an unusable rule — the server stays the authority on submit.
    matchTerm: match.trim().slice(0, 120) !== "" ? match.trim().slice(0, 120) : payee,
  };
}

// ---------------------------------------------------------------------------
// M116 — match rules, the derived detail view and "find past payments"
// (docs/25 §M116). Nothing here is stored twice: every number below is the
// rule re-applied over the committed ledger at read time.
// ---------------------------------------------------------------------------

/** How `match_term` is compared to a transaction's normalized payee. */
export const BILL_MATCH_MODES = ["contains", "starts_with", "exact"] as const;
export type BillMatchMode = (typeof BILL_MATCH_MODES)[number];

export const BILL_MATCH_MODE_LABELS: Record<BillMatchMode, string> = {
  contains: "contains",
  starts_with: "starts with",
  exact: "is exactly",
};

/**
 * The header sentence's term list: `“ROGERS”`, `“A” or “B”`,
 * `“A” and “B”` — the conjunction is the rule's `match_logic`, said out loud
 * so a narrowed set can never look like a widened one (docs/25 §M121 D9).
 */
export function describeTerms(terms: string[], logic: BillMatchLogic): string {
  const quoted = terms.map((term) => `“${term}”`);
  if (quoted.length === 0) return "“”";
  if (quoted.length === 1) return quoted[0];
  const joiner = logic === "all" ? " and " : " or ";
  return quoted.join(joiner);
}

/**
 * The list row's extra numbers (M116 B4 + M122 T4). Present on `GET /bills`
 * only — a create/update response does not carry them, which is why both are
 * optional.
 */
export interface BillStats {
  /** Lifetime count of rows the rule found; `0` is the honest "none yet". */
  payment_count?: number;
  /** Mean of the last 12 period totals; `null` when nothing matched. */
  rolling_average?: string | null;
  /**
   * M122 T4 — calendar-YTD spend (Jan 1 → server today) from the same
   * matched set. `0.00` is the honest "nothing this year".
   */
  ytd_total?: string;
}

/** A bill as the LIST answers it: the rule plus the two M116 B4 numbers. */
export type Bill = BillReminder & BillStats;

export interface BillRulePayload {
  match_mode: BillMatchMode;
  match_terms: string[];
  match_logic: BillMatchLogic;
  amount_tolerance?: number | null;
}

/** One bar of the period chart: `missed` is the explicit zero-height marker. */
export interface BillChartPoint {
  period: string;
  label: string;
  total: string;
  /** Payments in the period. OPTIONAL (M124): the category drill-down reuses
   *  this chart family and has no "payments" notion, so its rows carry no
   *  count and the table hides the column rather than printing a fake zero. */
  count?: number;
  /** No payment due in this period (bills only). A drilled category's empty
   *  month is not a MISSED anything — it simply has no spend — so it stays
   *  false and draws a zero-height bar instead of a warning marker. */
  missed?: boolean;
  /**
   * M122 T3 — trailing ≤6-period mean ENDING at this point, server-computed.
   * The last point's value IS `rolling.six.mean`, so the chart line and the
   * card's stat are one number by construction.
   *
   * OPTIONAL (M124) for the same reason `count` is: the drill-down's rows are
   * spend per period, which has no trailing-payment average to compute, so it
   * omits the field and the chart hides the column and the line instead of
   * printing a row of em-dashes. Bills always send it, so their chart is
   * unchanged.
   */
  rolling_six?: string | null;
}

export interface BillChart {
  frequency: string;
  unit: "month" | "quarter" | "year";
  /** The granularity `fmtBucket` needs to label the X axis. */
  granularity: "month" | "quarter" | "year";
  first_period: string | null;
  last_period: string | null;
  points: BillChartPoint[];
}

export interface BillRollingWindow {
  periods: number;
  window_periods: number;
  payments: number;
  /** M122 — the window's SUM (missed periods as $0.00). */
  total: string | null;
  mean: string | null;
  median: string | null;
}

export interface BillRolling {
  frequency: string;
  six: BillRollingWindow;
  twelve: BillRollingWindow;
}

/** M122 T4 — the matched set summed over its whole life (docs/25 §M122 D4). */
export interface BillAllTimeStats {
  total: string;
  payments: number;
  /** Per payment; `null` while nothing matched. */
  average: string | null;
  /** Distinct calendar years holding ≥ 1 payment — the honest divisor. */
  years: number;
  yearly_average: string | null;
}

/** M122 T4 — calendar year to date (Jan 1 → server today). */
export interface BillYtdStats {
  year: number;
  total: string;
  payments: number;
}

export interface BillSpendingStats {
  all_time: BillAllTimeStats;
  ytd: BillYtdStats;
}

export interface BillDetail {
  bill: Bill & { payment_count: number; rolling_average: string | null };
  matched_count: number;
  transactions: import("./transactions").TransactionPage;
  rolling: BillRolling;
  chart: BillChart;
  /** M122 T4 — LIFETIME, recomputed server-side on every read. */
  stats: BillSpendingStats;
}

export interface BillFindPastItem {
  id: number;
  date: string;
  amount_native: string;
  currency: string;
  cad_amount: string | null;
  normalized_payee: string;
  account_id: number;
  account_name: string;
}

export interface BillFindPastGroup {
  period: string;
  label: string;
  count: number;
  total: string;
  items: BillFindPastItem[];
}

/** M122 T2 — one account's share of the rule's matches. */
export interface BillFindPastAccountCount {
  account_id: number;
  account_name: string;
  count: number;
}

/**
 * M122 T2 — the dry-run summary, computed over the WHOLE matched set (never
 * the truncated preview slice).
 */
export interface BillFindPastStats {
  first_date: string | null;
  last_date: string | null;
  /** Sum of the last 12 calendar period totals; `null` when nothing matched. */
  last_12_total: string | null;
  account_counts: BillFindPastAccountCount[];
}

export interface BillFindPast {
  match_mode: BillMatchMode;
  match_term: string;
  match_terms: string[];
  match_logic: BillMatchLogic;
  frequency: string;
  total_count: number;
  truncated: boolean;
  groups: BillFindPastGroup[];
  stats: BillFindPastStats;
}

export interface BillDetailQuery {
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}

export function getBillDetail(id: number, query: BillDetailQuery = {}): Promise<BillDetail> {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  const qs = params.toString();
  return api<BillDetail>(`/bills/${id}/detail${qs ? `?${qs}` : ""}`);
}

export function updateBillRule(id: number, payload: BillRulePayload): Promise<BillReminder> {
  return api<BillReminder>(`/bills/${id}/rule`, { method: "PUT", body: payload });
}

/**
 * Run the SAVED rule over the whole ledger (read-only preview).
 * `overrides` lets the edit form try a rule it has not saved yet — including
 * the M123 category scope, so an edit that moves it is testable before commit.
 */
export function findPastPayments(
  id: number,
  overrides: Partial<BillRulePayload> & { category_id?: number | null } = {},
): Promise<BillFindPast> {
  return api<BillFindPast>(`/bills/${id}/find-past`, {
    method: "POST",
    body: Object.keys(overrides).length ? overrides : {},
  });
}

/** The same preview for a rule with no bill row yet (the create form). */
export interface BillDraftPreview extends BillRulePayload {
  category_id?: number | null;
  amount?: number | null;
  frequency: string;
}

export function findPastForDraft(payload: BillDraftPreview): Promise<BillFindPast> {
  return api<BillFindPast>("/bills/find-past", { method: "POST", body: payload });
}

// ---------------------------------------------------------------------------
// M122 T1 — term suggestions from the caller's own ledger (docs/25 §M122)
// ---------------------------------------------------------------------------

/**
 * The editor only queries once the draft is this long (or empty — that asks
 * for the top candidate groups). Below it the draft cannot yet name anything
 * the ledger would confirm, so a fetch per keystroke would be pure noise.
 */
export const BILL_SUGGEST_MIN_CHARS = 3;

/** One payee group the user's OWN committed ledger actually contains. */
export interface BillSuggestTerm {
  /** A real normalized payee string from the data (newest spelling). */
  normalized_payee: string;
  /** How many committed payments carry it — the social proof. */
  count: number;
  /** CAD magnitude of the group (the same figure the chart's bars use). */
  total: string;
  last_date: string;
}

export interface BillSuggestQuery {
  /** The draft term; `""` asks for the top groups overall. */
  q: string;
  /** The bill being edited — its CURRENT matched payees are excluded. */
  billId?: number | null;
  /** The editor's mode, so suggestions follow the comparison in use. */
  matchMode?: BillMatchMode;
  limit?: number;
}

export function suggestTerms({ q, billId, matchMode, limit }: BillSuggestQuery): Promise<BillSuggestTerm[]> {
  const params = new URLSearchParams();
  if (q !== "") params.set("q", q);
  if (billId != null) params.set("bill_id", String(billId));
  if (matchMode) params.set("match_mode", matchMode);
  if (limit !== undefined) params.set("limit", String(limit));
  const qs = params.toString();
  return api<BillSuggestTerm[]>(`/bills/suggest-terms${qs ? `?${qs}` : ""}`);
}
