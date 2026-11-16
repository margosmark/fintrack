// dividend-calendar — upcoming expected dividend income.
import type { DividendProvenance } from "./dividendProvenance";
import { api } from "./client";

export interface DividendEntry {
  security_id: number;
  symbol: string;
  frequency: string;
  currency: string | null;
  avg_amount: string | null;
  annual_projection: string | null;
  last_payment_date: string;
  next_expected_date: string;
  num_payments: number;
}

export interface DividendCurrencySubtotal {
  currency: string;
  this_month: string;
  this_quarter: string;
  this_year: string;
}

export type DividendPeriod = "this_month" | "this_quarter" | "this_year";

export interface DividendCalendarResponse {
  upcoming: DividendEntry[];
  /** Which source backs these projections, when it last answered, stale-or-missing (docs/07 §M184). */
  provenance: DividendProvenance;
  summary: {
    this_month: null;
    this_quarter: null;
    this_year: null;
    by_currency: DividendCurrencySubtotal[];
    unresolved_counts: Record<DividendPeriod, number>;
  };
}

export function getDividendCalendar(): Promise<DividendCalendarResponse> {
  return api<DividendCalendarResponse>("/reports/dividend-calendar");
}
