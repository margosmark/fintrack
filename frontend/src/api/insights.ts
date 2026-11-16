// insights — spending velocity and burn rate.
import { api } from "./client";

export interface CategoryDelta {
  category_id: number;
  category_name: string;
  current: string;
  previous: string;
  change_pct: number;
}

export interface SpendingInsights {
  current_month_spending: string;
  previous_month_spending: string;
  month_over_month_pct: number;
  daily_burn_rate: string;
  projected_month_end: string;
  days_elapsed: number;
  days_in_month: number;
  category_deltas: CategoryDelta[];
}

export function getSpendingInsights(): Promise<SpendingInsights> {
  return api<SpendingInsights>("/reports/insights");
}
