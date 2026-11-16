// budgets — per-category spending limits (docs/24).
//
// M137 P0-2 (docs/33 §Server-first computing): `GET /budgets` is the ONLY
// read path AND the only math. M48-S3 made the browser throw the server's
// `spent`/`remaining`/`pct_used` away and re-derive them from a FULL-LEDGER
// download (`budgetsProgress`); that download is gone — the network
// panel now shows one small `GET /budgets` and no `/transactions?all`.
//
// The client twin now lives in `lib/budgets.ts` — M160 (P2-5) carved it out
// of the retired `s3Engine` as an AUDITED CLIENT-ONLY compute (there is no
// server twin of the partner view's progress math, docs/43 §9), still pinned
// to the same golden `tests/fixtures/s3-vectors.json`.
import { api } from "./client";

export interface Budget {
  id: number;
  user_id: number;
  category_id: number;
  amount: string;
  period: string;
  /** M110 rollover: `off` (default — behaves exactly as before) | `forward`. */
  carry: string;
}

export interface BudgetWithSpent extends Budget {
  category_name: string;
  spent: string;
  /**
   * M110: the frozen value carried INTO this period, as its own field — never
   * merged into `amount`. `null` = nothing carried (docs/09 §Budget carry).
   */
  carried_in: string | null;
  /** assigned + carried_in − spent — what is left to spend (docs/09). */
  remaining: string;
  /**
   * `spent ÷ (amount + carried_in) × 100`, unclamped (M65) — `null` when that
   * pool is non-positive, where no ratio exists at all.
   */
  pct_used: number | null;
}

export interface BudgetPayload {
  category_id: number;
  amount: number;
  period?: string;
  carry?: "off" | "forward";
}

export async function listBudgets(): Promise<BudgetWithSpent[]> {
  // The server already computed `spent` / `carried_in` / `remaining` /
  // `pct_used` (docs/08 §Budgets, docs/24) — render them, don't re-derive them.
  return api<BudgetWithSpent[]>("/budgets");
}

/** M96's "committed monthly" label (docs/09 §Recurring prompt) — one tiny
 *  server read instead of the full-ledger download it used to ride on.
 *
 *  `monthly` is a MONEY value, so the server sends a decimal STRING — the
 *  shape every other money field in docs/08 has. It was a JSON number until
 *  the fix that removed a `float()` accumulator from the server service
 *  (AGENTS.md §2). Render with the shared `formatMoney`, never parseFloat it
 *  into arithmetic. */
export interface RecurringSummary {
  count: number;
  monthly: string;
}

export function getRecurringSummary(): Promise<RecurringSummary> {
  return api<RecurringSummary>("/budgets/recurring-summary");
}

export function createBudget(payload: BudgetPayload): Promise<Budget> {
  return api<Budget>("/budgets", { method: "POST", body: payload });
}

export function updateBudget(id: number, patch: Partial<BudgetPayload>): Promise<Budget> {
  return api<Budget>(`/budgets/${id}`, { method: "PATCH", body: patch });
}

export function deleteBudget(id: number): Promise<void> {
  return api<void>(`/budgets/${id}`, { method: "DELETE" }).then(() => undefined);
}
