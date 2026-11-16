// recurring — shared display + label math for M96 (docs/09 §Recurring prompt).
//
// Pure helpers only: the detection itself runs on the server
// (`GET /transactions/recurring/candidates`) and nothing here talks to the
// network. The one number this module computes is a LABEL for the Budgets
// page — "committed monthly" over rows already loaded — and it deliberately
// does not touch `budgetsProgress` (`lib/budgets.ts`): docs/09 says label, don't
// recompute.
import type { RecurringCadence, Transaction } from "../api/transactions";

/**
 * M138 (docs/09 §Bills you probably have) — the bulk-dismissal Undo window,
 * shared by BOTH suggestion surfaces so "dismiss" means one thing everywhere.
 *
 * Deliberately 6 seconds rather than M95's 5 (§Destructive actions): a bulk
 * dismissal can hide many rows at once, and the person needs to read the count
 * before deciding they did not mean it. Nothing is sent while the window is
 * open, so Undo needs no unsuppress route — `recurring_suppressions` stays
 * write-once (docs/09).
 */
export const DISMISS_UNDO_MS = 6000;

/** Human label per cadence. Neutral tone: cadence is not a semantic state. */
export const CADENCE_LABELS: Record<RecurringCadence, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  four_weekly: "Every 4 weeks",
  monthly: "Monthly",
};

/** Occurrences per month for each fitted cadence (30.4375-day average month). */
const PER_MONTH: Record<RecurringCadence, number> = {
  weekly: 30.4375 / 7,
  biweekly: 30.4375 / 14,
  four_weekly: 30.4375 / 28,
  monthly: 1,
};

export interface CommittedMonthly {
  /** Tagged rows counted (excluded rows are the ones with no CAD value). */
  count: number;
  /** Signed monthly commitment: debits negative, credits positive. */
  monthly: number;
}

/**
 * What the user has already committed to, per month, from the rows tagged
 * `recurring`.
 *
 * Amount choice follows docs/12: reporting currency only — a foreign row
 * counts at its `cad_amount` mirror, and a row with no mirror is EXCLUDED,
 * never counted at face value. Budgets' own progress math is untouched.
 */
export function committedMonthly(rows: readonly Transaction[]): CommittedMonthly {
  let count = 0;
  let monthly = 0;
  for (const row of rows) {
    if (!row.is_recurring || row.recurring_cadence === null) continue;
    const native = Number(row.amount_native);
    const value =
      row.currency === "CAD" ? native : row.cad_amount === null ? null : Number(row.cad_amount);
    if (value === null || Number.isNaN(value)) continue;
    count += 1;
    monthly += value * PER_MONTH[row.recurring_cadence];
  }
  return { count, monthly: Math.round(monthly * 100) / 100 };
}

/** Badge title for one tagged row: `Recurring · Monthly · next 2026-11-03`. */
export function recurringBadgeTitle(
  cadence: RecurringCadence | null,
  nextDate: string | null,
): string {
  const label = cadence === null ? "Recurring" : CADENCE_LABELS[cadence];
  return nextDate === null ? label : `${label} · next ${nextDate}`;
}
