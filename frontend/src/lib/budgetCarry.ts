// M110 budget rollover — the DISPLAY layering of an honest budget row
// (docs/09 §Budget carry).
//
// The numbers themselves come from the server (and from `lib/budgets.ts`'s
// `budgetsProgress`, their exact port); this module only re-expresses the two wire
// fields a row is built from so the Budgets page, the Dashboard cards and the
// shared-household cards cannot drift apart in how they show a carry:
//
//   spendable pool = assigned + carried_in      (the pct denominator)
//   carried_in = null  -> nothing carried; the pre-M110 row, unchanged
//   carried_in <  0    -> a debt: its own tone, never absorbed
//   pct_used  = null   -> the debt swallowed the pool, so no ratio exists
//                         (docs/09: say so, never invent a percent)

export interface CarryFields {
  amount: string;
  carried_in?: string | null;
  pct_used: number | null;
}

export interface BudgetView {
  /** Parsed `carried_in` — `null` when nothing was carried into this period. */
  carried: number | null;
  /** The carried value is a debt (negative) — the danger-tone case. */
  debt: boolean;
  /** assigned + carried_in: the denominator the pct was measured against. */
  pool: number;
  /** The server answered `pct_used: null` — the debt swallowed the pool. */
  over: boolean;
}

export function budgetView(b: CarryFields): BudgetView {
  const carried =
    b.carried_in === undefined || b.carried_in === null ? null : parseFloat(b.carried_in);
  const amount = parseFloat(b.amount);
  return {
    carried,
    debt: carried !== null && carried < 0,
    pool: carried === null ? amount : amount + carried,
    over: b.pct_used === null,
  };
}

/** True when the row has a carried figure worth showing (a real, non-zero carry). */
export function hasCarry(view: BudgetView): boolean {
  return view.carried !== null && view.carried !== 0;
}

/** The `% used` hint when nothing carries — the pre-M110 string, unchanged. */
export const LIMIT_HINT =
  "spent ÷ limit — the true ratio, never clamped: over budget shows the real number (201.94% prints as 202%). Only the bar fill stops at 100%.";

/** The `% used` hint when a carry is in play — same honesty, real denominator. */
export const CARRY_HINT =
  "spent ÷ (limit + carried in) — the same true ratio, never clamped. A carried debt bigger than this period's limit leaves no ratio to print, so the row says \"over budget\" instead of inventing a percent.";

/** Help for the row's one-click "carry leftover" toggle. */
export const CARRY_TOGGLE_HINT =
  "Roll this period's leftover (or overspend) into the next period as its own carried-in amount. Off by default; the value itself is computed and frozen by the server at the period boundary.";
