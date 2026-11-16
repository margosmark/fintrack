/**
 * Canadian tax engine v1 — CPP/OAS timing (docs/58-canada-tax-engine.md).
 * Age-65 dollar maxima are Service Canada/ESDC published figures for the
 * quarter; the 60/70 dollar figures are DERIVED from the published
 * factor rule (−0.6%/month early, +0.7%/month late) and pinned here —
 * Service Canada publishes those factors, not the dollar results.
 */
import { describe, expect, it } from "vitest";
import {
  CanadaTaxInputError,
  cppRetirementMonthly,
  oasMonthly,
  oasRecoveryTax,
} from "../src/lib/canadaTaxEngine";

describe("CPP retirement pension start-age timing", () => {
  // Sources: age-65 maximum for Oct–Dec 2026 — ESDC quarterly statistics
  // https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2026-quarterly-october-december.html
  // ($1,507.65) and the −0.6%/month +0.7%/month rule — Service Canada
  // https://www.canada.ca/en/services/benefits/publicpensions/cpp/when-start.html
  it("returns the published age-65 maximum for 2026", () => {
    expect(cppRetirementMonthly({ startAge: 65, year: 2026 })).toBe(1507.65);
  });

  it("applies −0.6%/month before 65 and +0.7%/month after (to the caps)", () => {
    // 60: −36% → 1,507.65 × 0.64 = 964.896 → $964.90 (HALF_UP).
    expect(cppRetirementMonthly({ startAge: 60, year: 2026 })).toBe(964.9);
    // 64: −7.2% → 1,507.65 × 0.928 = 1,399.0992 → $1,399.10.
    expect(cppRetirementMonthly({ startAge: 64, year: 2026 })).toBe(1399.1);
    // 67: +16.8% → 1,507.65 × 1.168 = 1,760.9352 → $1,760.94.
    expect(cppRetirementMonthly({ startAge: 67, year: 2026 })).toBe(1760.94);
    // 70: +42% → 1,507.65 × 1.42 = 2,140.863 → $2,140.86.
    expect(cppRetirementMonthly({ startAge: 70, year: 2026 })).toBe(2140.86);
  });

  it("switches years through the table, not code (2025 = $1,433.00)", () => {
    // ESDC Oct–Dec 2025 quarterly: age-65 maximum $1,433.00.
    expect(cppRetirementMonthly({ startAge: 65, year: 2025 })).toBe(1433);
    expect(cppRetirementMonthly({ startAge: 60, year: 2025 })).toBe(917.12); // ×0.64
  });

  it("freezes an unpublished year at the latest published table (estimate)", () => {
    // Documented in docs/58: CPP dollar amounts do not auto-index; the
    // engine refuses to invent a future maximum and uses 2026 instead.
    expect(cppRetirementMonthly({ startAge: 65, year: 2040 })).toBe(1507.65);
  });

  it("rejects ages outside 60–70", () => {
    expectErrorCode(() => cppRetirementMonthly({ startAge: 59 }), "cpp_start_age_out_of_range");
    expectErrorCode(() => cppRetirementMonthly({ startAge: 71 }), "cpp_start_age_out_of_range");
    expectErrorCode(() => cppRetirementMonthly({ startAge: 60.5 }), "cpp_start_age_out_of_range");
  });
});

describe("OAS start-age deferral (+0.6%/month, +36% max)", () => {
  // Sources: Q4 2026 OAS payment ($762.50 for 65–74) — ESDC quarterly
  // https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2026-quarterly-october-december.html
  // deferral rule — Service Canada https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/when-start.html
  it("returns the published Q4 2026 maximum at 65", () => {
    expect(oasMonthly({ startAge: 65, year: 2026 })).toBe(762.5);
  });

  it("adds 0.6% per month deferred, capped at +36% at 70", () => {
    // 66: +7.2% → 762.50 × 1.072 = 817.40.
    expect(oasMonthly({ startAge: 66, year: 2026 })).toBe(817.4);
    // 67: +14.4% → 762.50 × 1.144 = 872.30.
    expect(oasMonthly({ startAge: 67, year: 2026 })).toBe(872.3);
    // 70: +36% → 762.50 × 1.36 = 1,037.00.
    expect(oasMonthly({ startAge: 70, year: 2026 })).toBe(1037);
  });

  it("switches years through the table (2025 Q4 = $740.09)", () => {
    // ESDC Oct–Dec 2025 quarterly: $740.09 (65–74).
    expect(oasMonthly({ startAge: 65, year: 2025 })).toBe(740.09);
    expect(oasMonthly({ startAge: 70, year: 2025 })).toBe(1006.52); // ×1.36
  });

  it("freezes an unpublished year at the latest published table", () => {
    expect(oasMonthly({ startAge: 65, year: 2040 })).toBe(762.5);
  });

  it("rejects ages outside 65–70", () => {
    expectErrorCode(() => oasMonthly({ startAge: 64 }), "oas_start_age_out_of_range");
    expectErrorCode(() => oasMonthly({ startAge: 71 }), "oas_start_age_out_of_range");
    expectErrorCode(() => oasMonthly({ startAge: 65.5 }), "oas_start_age_out_of_range");
  });
});

describe("OAS recovery tax (clawback: 15% over the threshold, capped)", () => {
  // Sources: 2026 threshold $95,323 / 15% — CRA
  // https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html
  // (2025: $93,454).
  it("charges nothing at or below the threshold", () => {
    expect(oasRecoveryTax({ netIncome: 95323, annualOas: 9150, year: 2026 })).toBe(0);
    expect(oasRecoveryTax({ netIncome: 90000, annualOas: 9150, year: 2026 })).toBe(0);
  });

  it("charges 15% of net income over the threshold", () => {
    // 120,000 − 95,323 = 24,677 × 15% = 3,701.55 — under the annual OAS.
    expect(oasRecoveryTax({ netIncome: 120000, annualOas: 9150, year: 2026 })).toBe(3701.55);
  });

  it("caps the recovery at the OAS actually received", () => {
    // 160,000 − 95,323 = 64,677 × 15% = 9,701.55 → capped at 9,150.
    expect(oasRecoveryTax({ netIncome: 160000, annualOas: 9150, year: 2026 })).toBe(9150);
  });

  it("switches years through the table (2025 threshold $93,454)", () => {
    // 110,000 − 93,454 = 16,546 × 15% = 2,481.90.
    expect(oasRecoveryTax({ netIncome: 110000, annualOas: 8881.08, year: 2025 })).toBe(2481.9);
  });

  it("freezes an unpublished year at the latest published threshold", () => {
    expect(oasRecoveryTax({ netIncome: 120000, annualOas: 9150, year: 2040 })).toBe(3701.55);
  });

  it("rejects negative income", () => {
    expectErrorCode(
      () => oasRecoveryTax({ netIncome: -1, annualOas: 9150, year: 2026 }),
      "negative_amount",
    );
  });
});

/** Assert fn throws CanadaTaxInputError with the given domain code. */
function expectErrorCode(fn: () => unknown, code: string): void {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  expect(caught, `expected CanadaTaxInputError with code ${code}`).toBeInstanceOf(
    CanadaTaxInputError,
  );
  expect((caught as CanadaTaxInputError).code).toBe(code);
}
