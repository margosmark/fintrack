// M109 (docs/45 §Boundary) — the four honesty strings are LOAD-BEARING copy:
// the spec pins them verbatim and tests/m109Forecast.test.tsx (F15) asserts all
// four render on /forecast. One source, exported as constants, so the header
// and the assumptions panel below it cannot drift apart — a wording change
// lands exactly once and the assertion that guards it keeps guarding it.
//
// F-11 (docs/46 §D2): this module also composes the ASSUMPTIONS list, whose
// first five lines are absolute CAD figures the server handed over as strings.
// Before F-11 it had zero mask references and printed them raw on a masked
// session, so `buildAssumptions` takes the mask like every other formatter
// does — see the note on the function.

import { MASK_TEXT, isMasked } from "./mask";

/** The page's headline caveat: a run is arithmetic over recorded history. */
export const PROJECTION_NOT_PREDICTION = "Projection, not a prediction.";

/** Income changes in a scenario never reach the Canadian tax projection. */
export const NOT_A_TAX_SIMULATOR =
  "Not a tax simulator — a scenario that changes income does not recalculate your tax projection.";

/** No randomness exists anywhere in the engine (docs/45 §Context). */
export const DETERMINISTIC_NOT_MONTE_CARLO =
  "Deterministic, not Monte Carlo — the same inputs always give the same output.";

/** Reading a number off this page is not a recommendation to act on it. */
export const NOT_FINANCIAL_ADVICE = "Not financial advice.";

/** All four, in the order the spec lists them. */
export const HONESTY_STRINGS = [
  PROJECTION_NOT_PREDICTION,
  NOT_A_TAX_SIMULATOR,
  DETERMINISTIC_NOT_MONTE_CARLO,
  NOT_FINANCIAL_ADVICE,
] as const;

/**
 * Assumptions-panel disclosures that are true whether or not the current
 * scenario contains the event they describe: the panel is the honesty surface,
 * so it states the model's shape rather than waiting for a reader to ask.
 * (b)/(c) name the two simplifications a user would otherwise have to discover
 * by comparing the output against a real mortgage or a real final paycheque.
 */
export const HOUSE_VALUE_DISCLOSURE =
  "House value is held constant at the purchase price — no appreciation, no depreciation and no maintenance reserve.";
export const MORTGAGE_MODEL_DISCLOSURE =
  "The mortgage is a flat-rate monthly model — no Canadian semi-annual compounding and no stress test.";
export const CAREER_BREAK_DISCLOSURE =
  "A career break suppresses ALL income between its dates: salary and any other income are indistinguishable at row level, so “no income from A to B” is the honest reading. Expenses continue.";
export const TAX_PROJECTION_DISCLOSURE =
  "Tax projection not recalculated by scenarios.";
export const MONTE_CARLO_DISCLOSURE =
  "Not Monte Carlo — no randomness anywhere, so identical inputs give identical output.";

/** The five fixed disclosures, in the order the panel renders them. */
export const ASSUMPTION_DISCLOSURES = [
  HOUSE_VALUE_DISCLOSURE,
  MORTGAGE_MODEL_DISCLOSURE,
  CAREER_BREAK_DISCLOSURE,
  TAX_PROJECTION_DISCLOSURE,
  MONTE_CARLO_DISCLOSURE,
] as const;

/**
 * The `inputs` block of a run response — declared structurally here rather
 * than imported from api/forecast so `lib/` stays free of an `api/` dependency
 * and the builder can be unit-tested against any conforming payload.
 *
 * Every money field is a Decimal serialised as a string; the builder splices
 * those strings in verbatim — or splices ••••• in their place on a masked
 * session (F-11). Nothing is parsed, rounded or re-formatted either way, so
 * the panel can never disagree with the API it is describing.
 */
export interface AssumptionInputs {
  net_worth_cad: string;
  cash_cad: string;
  positions_cad: string;
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

export interface AssumptionParams {
  reporting_currency: string;
  inflation_pct: string;
  annual_return_pct: string;
  flow_window_months: number;
}

/**
 * The assumptions list, head first:
 *
 *   1. starting figures (net worth split into cash + positions);
 *   2. the flow decomposition — each total is printed ALONGSIDE the two
 *      numbers it is made of, so "income = recurring + residual" reads as an
 *      equation instead of two unrelated stats (docs/45 §1.2's double-count
 *      rule made visible);
 *   3. the run parameters the user chose;
 *   4. the fixed disclosures above.
 *
 * F-11 / docs/46 §D2: the five absolute figures (net worth, cash, positions,
 * the income total and the expense total — with their recurring/residual
 * parts) are MONEY and mask; the percentages, the row count and the history
 * dates are not (the operator ruling: proportional values are not sensitive,
 * and a date is not a balance), so they read either way.
 *
 * `masked` is OPTIONAL exactly like every other formatter's (docs/46 §D6):
 * omitted → consult the ambient session mask, so a new call site masks by
 * default and cannot silently regress. The caller still has to SUBSCRIBE
 * (`useMasked()`) or a flip will not re-render it — this function only
 * decides what the strings say, it cannot make anybody repaint.
 */
export function buildAssumptions(
  inputs: AssumptionInputs,
  params: AssumptionParams,
  masked?: boolean,
): string[] {
  const hide = masked ?? isMasked();
  /** A server string, or bullets — never parsed, rounded or re-formatted. */
  const money = (value: string): string => (hide ? MASK_TEXT : value);
  return [
    `Net worth (${params.reporting_currency}): ${money(inputs.net_worth_cad)}`,
    `Cash (${params.reporting_currency}): ${money(inputs.cash_cad)}`,
    `Positions (${params.reporting_currency}): ${money(inputs.positions_cad)}`,
    `Income (${params.reporting_currency}): ${money(inputs.income_monthly_cad)} = recurring ${money(inputs.recurring_income_monthly_cad)} + residual ${money(inputs.residual_income_monthly_cad)}`,
    `Expense (${params.reporting_currency}): ${money(inputs.expense_monthly_cad)} = recurring ${money(inputs.recurring_expense_monthly_cad)} + residual ${money(inputs.residual_expense_monthly_cad)}`,
    `Recurring rows in the history window: ${inputs.recurring_rows} (${inputs.history_from} to ${inputs.history_to})`,
    `History window: ${params.flow_window_months} months`,
    `Inflation: ${params.inflation_pct}% — one constant rate over the whole path, not inflation modelling`,
    `Flat annual return: ${params.annual_return_pct}% — applied to the whole positions line, no per-asset forecast`,
    ...ASSUMPTION_DISCLOSURES,
  ];
}
