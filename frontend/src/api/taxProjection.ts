// tax-projection — estimate annual tax liability.
import { api } from "./client";

export interface StoredProjectionInputs {
  year: number;
  province: string;
  estimated_income: string;
  deductions: string;
  capital_gains_override: string | null;
  dividend_income_override: string | null;
}

export interface TaxProjectionResult {
  year: number;
  province: string;
  taxable_income: string;
  employment_income: string;
  taxable_capital_gains: string;
  grossed_dividends: string;
  deductions: string;
  federal_tax: string;
  provincial_tax: string;
  dividend_tax_credit: string;
  total_tax: string;
  average_rate: string;
  /** Raw stored row values (Decimals as strings) — form prefill source. */
  stored?: StoredProjectionInputs;
}

export function getProjection(year: number): Promise<TaxProjectionResult> {
  return api(`/taxes/projection?year=${year}`);
}

/**
 * PATCH /taxes/projection takes query params (docs/29 F9): omitted numeric
 * params keep the stored value, so save sends ONLY the fields the user
 * edited — money travels as its raw string for the backend Decimal.
 */
export interface ProjectionSaveParams {
  year: number;
  province: string;
  estimated_income?: string;
  deductions?: string;
  capital_gains_override?: string | null;
  dividend_income_override?: string | null;
}

export function updateProjection(params: ProjectionSaveParams): Promise<TaxProjectionResult> {
  const q = new URLSearchParams({
    year: String(params.year),
    province: params.province,
  });
  if (params.estimated_income !== undefined) q.set("estimated_income", params.estimated_income);
  if (params.deductions !== undefined) q.set("deductions", params.deductions);
  if (params.capital_gains_override !== undefined) {
    // Empty value (`?capital_gains_override=`) is the backend's
    // clear-to-null convention; omitting the param keeps the stored value.
    q.set("capital_gains_override", params.capital_gains_override ?? "");
  }
  if (params.dividend_income_override !== undefined) {
    q.set("dividend_income_override", params.dividend_income_override ?? "");
  }
  return api(`/taxes/projection?${q}`, { method: "PATCH" });
}
