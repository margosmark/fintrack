// spending-comparison — period-over-period comparison.
import { api } from "./client";

export interface PeriodDelta {
  category_id: number;
  category_name: string;
  current: string;
  previous: string;
  change: string;
  change_pct: number;
}

export interface SpendingComparison {
  current_period: { start: string; end: string; total: string };
  previous_period: { start: string; end: string; total: string };
  total_change: string;
  total_change_pct: number;
  deltas: PeriodDelta[];
}

export function getSpendingComparison(
  currentStart: string,
  currentEnd: string,
  previousStart?: string,
  previousEnd?: string,
): Promise<SpendingComparison> {
  const params = new URLSearchParams({
    current_start: currentStart,
    current_end: currentEnd,
  });
  if (previousStart) params.set("previous_start", previousStart);
  if (previousEnd) params.set("previous_end", previousEnd);
  return api<SpendingComparison>(`/reports/spending-comparison?${params.toString()}`);
}
