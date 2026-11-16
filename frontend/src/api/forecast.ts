import { api } from "./client";

// forecast (M109, docs/45 §3): net-worth what-if scenarios. Money is Decimal
// end-to-end on the server, so every monetary field arrives as a string and is
// sent back as one — the UI never re-derives a total it was given.

/** The only three x-axis grains the engine samples (docs/45 §1.3). */
export type ForecastGranularity = "day" | "week" | "month";

/**
 * The v1 CLOSED event set (docs/45 §1.5). There is no formula editor and no
 * scripting: a scenario event is one of exactly these five discriminated
 * shapes, so this union IS the contract the picker offers.
 */
export type ForecastEvent =
  | {
      id: string;
      type: "house_purchase";
      date: string;
      home_price_cad: string;
      down_payment_cad: string;
      mortgage_rate_pct: string;
      amortization_years: number;
    }
  | { id: string; type: "career_break"; start_date: string; end_date: string }
  | { id: string; type: "salary_change"; date: string; monthly_delta_cad: string }
  | { id: string; type: "one_off_cash_event"; date: string; amount_cad: string }
  | {
      id: string;
      type: "recurring_cost_delta";
      start_date: string;
      monthly_amount_cad: string;
      end_date?: string;
    };

/** Display labels for the picker — the wire value is the `type` above. */
export const FORECAST_EVENT_TYPES: ReadonlyArray<{
  type: ForecastEvent["type"];
  label: string;
}> = [
  { type: "house_purchase", label: "House purchase" },
  { type: "career_break", label: "Career break" },
  { type: "salary_change", label: "Salary change" },
  { type: "one_off_cash_event", label: "One-off cash event" },
  { type: "recurring_cost_delta", label: "Recurring cost change" },
];

export interface ForecastScenario {
  id: string;
  name: string;
  events: ForecastEvent[];
}

/** GET /forecast/defaults — UI prefill (docs/45 §3.2). */
export interface ForecastDefaults {
  as_of: string;
  reporting_currency: string;
  net_worth_cad: string;
  cash_cad: string;
  positions_cad: string;
  income_monthly_cad: string;
  expense_monthly_cad: string;
  flow_window_months: number;
  recurring_rows: number;
  scenario_count: number;
}

/** GET /forecast/run query parameters (docs/45 §3.1), all bounds-clamped. */
export interface ForecastRunParams {
  scenario_ids: string[];
  horizon_months: number;
  granularity: ForecastGranularity;
  inflation_pct: string;
  annual_return_pct: string;
  flow_window_months: number;
}

/** The `inputs` block: baseline history plus the recurring/residual split. */
export interface ForecastRunInputs {
  cash_cad: string;
  positions_cad: string;
  net_worth_cad: string;
  history_from: string;
  history_to: string;
  income_monthly_cad: string;
  expense_monthly_cad: string;
  recurring_income_monthly_cad: string;
  recurring_expense_monthly_cad: string;
  residual_income_monthly_cad: string;
  residual_expense_monthly_cad: string;
  recurring_rows: number;
}

/** One overlay series: `values` in future dollars, `real_values` in today's. */
export interface ForecastSeries {
  id: string;
  name: string;
  values: string[];
  real_values: string[];
  final_cad: string;
  delta_cad: string;
}

export interface ForecastRun {
  as_of: string;
  horizon_months: number;
  granularity: ForecastGranularity;
  inflation_pct: string;
  annual_return_pct: string;
  flow_window_months: number;
  reporting_currency: string;
  /** Bucket-END dates, ascending; every series is the same length. */
  buckets: string[];
  inputs: ForecastRunInputs;
  baseline: ForecastSeries;
  scenarios: ForecastSeries[];
  disclaimer: string;
}

/**
 * Client-generated scenario/event id: `^[A-Za-z0-9_-]{8,64}$` (docs/45 §2,
 * the M108-P2 rule) — the id travels in the CREATE body so a later rename or
 * copy addresses the row the list already carries, exactly like a saved
 * report view's id (components/reports/SavedViewsControl.tsx).
 */
export function newForecastId(prefix = "sc"): string {
  const cryptoRandom =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().replace(/-/g, "")
      : "";
  return `${prefix}${cryptoRandom || `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`}`;
}

/** GET /forecast/defaults */
export function getForecastDefaults(): Promise<ForecastDefaults> {
  return api<ForecastDefaults>("/forecast/defaults");
}

/** GET /forecast/scenarios — only the caller's rows (docs/45 §Tenancy). */
export function listForecastScenarios(): Promise<{
  items: ForecastScenario[];
  count: number;
}> {
  return api<{ items: ForecastScenario[]; count: number }>("/forecast/scenarios");
}

/** POST /forecast/scenarios — 201; 409 `scenario_limit_reached` past 25. */
export function createForecastScenario(
  body: ForecastScenario,
): Promise<ForecastScenario> {
  return api<ForecastScenario>("/forecast/scenarios", {
    method: "POST",
    body,
  });
}

/** PATCH /forecast/scenarios/{id} — FULL replace; the path id wins. */
export function replaceForecastScenario(
  id: string,
  body: ForecastScenario,
): Promise<ForecastScenario> {
  return api<ForecastScenario>(`/forecast/scenarios/${id}`, {
    method: "PATCH",
    body,
  });
}

/** POST /forecast/scenarios/{id}/copy — 201; the server mints an id when absent. */
export function copyForecastScenario(
  id: string,
  body: { id?: string; name?: string } = {},
): Promise<ForecastScenario> {
  return api<ForecastScenario>(`/forecast/scenarios/${id}/copy`, {
    method: "POST",
    body,
  });
}

/** DELETE /forecast/scenarios/{id} — 204. */
export function deleteForecastScenario(id: string): Promise<void> {
  return api<void>(`/forecast/scenarios/${id}`, { method: "DELETE" });
}

/**
 * GET /forecast/run — a single query string assembled here so the page's
 * query key and the request can never disagree about the parameters.
 */
export function getForecastRun(params: ForecastRunParams): Promise<ForecastRun> {
  const query = new URLSearchParams();
  if (params.scenario_ids.length > 0) {
    query.set("scenario_ids", params.scenario_ids.join(","));
  }
  query.set("horizon_months", String(params.horizon_months));
  query.set("granularity", params.granularity);
  query.set("inflation_pct", params.inflation_pct);
  query.set("annual_return_pct", params.annual_return_pct);
  query.set("flow_window_months", String(params.flow_window_months));
  return api<ForecastRun>(`/forecast/run?${query.toString()}`);
}
