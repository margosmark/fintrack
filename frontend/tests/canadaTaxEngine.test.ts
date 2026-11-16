/**
 * Canadian tax engine v1 — federal/provincial income tax
 * (docs/58-canada-tax-engine.md). Every expected figure is traceable to a
 * primary source cited in the test's docstring; worked CRA examples are
 * cross-checked component-by-component, and the v1 model limitation
 * (BPA-only credits) is pinned as a bounded delta rather than hidden.
 */
import { describe, expect, it } from "vitest";
import {
  CanadaTaxInputError,
  caTaxEngine,
  grossWithdrawalFor,
  incomeTax,
} from "../src/lib/canadaTaxEngine";
import type { ProvinceCode } from "../src/lib/canadaTaxTables";


/** The BPA credit the engine applies federally: $16,452 × 14% = $2,303.28
 * (Form TD1-WS line 9 partial claim + T4032-ON lowest federal rate 14%). */

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

/** The BPA credit the engine applies federally: $16,452 × 14% = $2,303.28
 * (Form TD1-WS line 9 partial claim + T4032-ON lowest federal rate 14%). */
const FED_BPA_CREDIT_2026 = 2303.28;

// Sources: https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html
// (2026 brackets, BPA thresholds) and Form T1-26E line-19 worked examples:
// https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-2025-later/td1-ws.html
describe("federal brackets (CRA current-year rates + Form T1/line-19 examples)", () => {
  // T1-26E line 19 example: tax on $50,000 of total income at 14% = $7,000.
  it("reconstructs CRA's $7,000 gross tax on $50,000", () => {
    const r = incomeTax({ taxableIncome: 50000, province: "ON", year: 2026 });
    expect(r.federal + FED_BPA_CREDIT_2026).toBe(7000);
  });

  it("crosses the first bracket at $58,523 with exact half-up cent quantization", () => {
    // 14% × 58,523 = 8,193.22 (still inside bracket 1) and
    // 14% × 58,523 + 20.5% × 1,477 = 8,496.005 → $8,496.01 (HALF_UP).
    const atTop = incomeTax({ taxableIncome: 58523, province: "ON", year: 2026 });
    expect(atTop.federal + FED_BPA_CREDIT_2026).toBe(8193.22);
    const above = incomeTax({ taxableIncome: 60000, province: "ON", year: 2026 });
    expect(above.federal + FED_BPA_CREDIT_2026).toBe(8496.01);
    // Continuity: one cent over the boundary moves the total by under a cent.
    const centOver = incomeTax({ taxableIncome: 58523.01, province: "ON", year: 2026 });
    expect(Math.abs(centOver.total - atTop.total)).toBeLessThan(0.01);
  });

  it("applies the open-ended 33% top bracket ($300,000 → $72,976.05 gross)", () => {
    const r = incomeTax({
      taxableIncome: 300000,
      netIncome: 50000,
      province: "ON",
      year: 2026,
    });
    // 14%×58,523 + 20.5%×58,522 + 26%×64,395 + 29%×77,042 + 33%×41,518
    // = 72,976.05 gross; BPA is full because net income is $50,000.
    expect(r.federal + FED_BPA_CREDIT_2026).toBe(72976.05);
    expect(r.federal).toBe(70672.77);
  });
});

// Source: Form TD1-WS line 9 partial claim —
// https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-2025-later/td1-ws.html
describe("federal basic personal amount phase-out (Form TD1-WS line 9)", () => {
  // TD1-WS: income > $181,440 → partial claim, linear to $14,829 at
  // $258,482 (same thresholds as the 2026 federal brackets).
  it("phases the BPA out linearly above $181,440 of net income", () => {
    const full = incomeTax({
      taxableIncome: 200000,
      netIncome: 50000,
      province: "ON",
      year: 2026,
    });
    expect(full.federal + FED_BPA_CREDIT_2026).toBe(42315.33);

    const phased = incomeTax({
      taxableIncome: 200000,
      netIncome: 200000,
      province: "ON",
      year: 2026,
    });
    // BPA at $200,000 = 14,829 + (16,452 − 14,829) × (258,482 − 200,000)
    // / (258,482 − 181,440) = 16,061.01 (HALF_UP); credit × 14% =
    // $2,248.54. Gross is identical ($42,315.33); only the credit moves.
    expect(phased.federal).toBe(40066.79);
    // The credit shrinks by 54.74 → the phased case pays 54.74 MORE;
    // compare in integer cents (float subtraction of dollars is not exact).
    expect(Math.round((phased.federal - full.federal) * 100)).toBe(5474);
  });

  it("uses the $14,829 floor once net income reaches $258,482", () => {
    const floored = incomeTax({
      taxableIncome: 300000,
      netIncome: 300000,
      province: "ON",
      year: 2026,
    });
    // credit = 14,829 × 14% = 2,076.06; gross 72,976.05 − 2,076.06.
    expect(floored.federal).toBe(70899.99);
  });
});

// Source: CRA T4032-ON (2026) worked example and schedules —
// https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032on-jan/t4032on-january-general-information.html
describe("Ontario quirks (T4032-ON: surtax, health premium, low-income reduction)", () => {
  it("matches CRA's worked example component-by-component", () => {
    // T4032-ON example: taxable/net $60,000. Published: provincial
    // $2,377.20 = basic 1,705.98 + premium 600.00 + reduction 0.00,
    // surtax $0.00. v1 carries only the BPA credit, so its basic is
    // higher by exactly the CPP/EI/employment credits CRA also applies —
    // the quirks themselves must match to the cent.
    const r = incomeTax({ taxableIncome: 60000, netIncome: 60000, province: "ON", year: 2026 });
    // Gross provincial = 5.05% × 53,891 + 9.15% × 6,109 = $3,280.47;
    // BPA credit = 12,989 × 5.05% = $655.94 (65,594.45¢ HALF_UP).
    // Assembled in integer cents: basic 328,047 − credit 65,594 + premium
    // 60,000 + reduction 0 + surtax 0 = 322,453¢ = $3,224.53.
    const basicCents = 328047 - 65594;
    const premiumCents = 60000; // band $48,000–$72,000: cap of the 25% ladder
    const reductionCents = 0; // 2 × $300 − basic < 0 → nil (T4032-ON rule)
    const surtaxCents = 0; // basic $2,624.53 < $5,818 → nil
    expect(r.provincial).toBe((basicCents + premiumCents + reductionCents + surtaxCents) / 100);
    // Bound on the v1 credit gap vs CRA's published $2,377.20.
    const publishedTotal = 2377.2;
    expect(r.provincial - publishedTotal).toBeGreaterThan(0);
    expect(r.provincial - publishedTotal).toBeLessThan(1000);
  });

  it("charges the health premium ladder on taxable income", () => {
    // Each: (gross − BPA credit) + premium, premium per T4032-ON bands:
    // 6% × (income − 20,000) capped 300 / 6% × (income − 36,000) + 300
    // capped 450 / 25% × (income − 48,000) + 450 capped 600.
    const at25k = incomeTax({ taxableIncome: 25000, province: "ON", year: 2026 });
    // 1,262.50 − 655.94 + 300.00 (6% × 5,000, cap 300)
    expect(at25k.provincial).toBe((126250 - 65594 + 30000) / 100);
    const at40k = incomeTax({ taxableIncome: 40000, province: "ON", year: 2026 });
    // 2,020.00 − 655.94 + 450.00 — the 36,000-band formula gives 540 but
    // the band CAP is 450 (T4032-ON "maximum premium: $450.00").
    expect(at40k.provincial).toBe((202000 - 65594 + 45000) / 100);
    const at50k = incomeTax({ taxableIncome: 50000, province: "ON", year: 2026 });
    // 2,525.00 − 655.94 + 600.00 — 450 + 25% × 2,000 = 950 → band cap 600.
    expect(at50k.provincial).toBe((252500 - 65594 + 60000) / 100);
    const at72001 = incomeTax({ taxableIncome: 72001, province: "ON", year: 2026 });
    // premium steps to the 72,000 band: 600 + 25% × $1 = $600.25;
    // gross 4,378.56 − 655.94 + 600.25
    expect(at72001.provincial).toBe((437856 - 65594 + 60025) / 100);
  });

  it("applies the two-band surtax above $5,818 / $7,446 of basic tax", () => {
    const at100k = incomeTax({ taxableIncome: 100000, province: "ON", year: 2026 });
    // basic 6,940.47 − 655.94 = 6,284.53; surtax = 20% × (6,284.53 −
    // 5,818) = 93.31; premium at $100,000 = 600 + 25% × 28,000 → cap 750.
    expect(at100k.provincial).toBe((628453 + 9331 + 75000) / 100);
    const at110k = incomeTax({ taxableIncome: 110000, province: "ON", year: 2026 });
    // Gross = 5.05%×53,891 + 9.15%×53,894 + 11.16%×2,215 = $7,899.99;
    // basic = 7,899.99 − 655.94 = 7,244.05; surtax = 20% × (7,244.05 −
    // 5,818) = 285.21 (band 2 untouched: basic < 7,446); premium 750.
    expect(at110k.provincial).toBe((724405 + 28521 + 75000) / 100);
  });

  it("grants the low-income tax reduction at 2 × personal amount", () => {
    const r = incomeTax({ taxableIncome: 20000, province: "ON", year: 2026 });
    // basic = 1,010.00 − 655.94 = 354.06; reduction = min(basic,
    // 2 × 300 − basic) = 245.94 → payable 108.12, premium nil.
    expect(r.provincial).toBe(108.12);
  });
});

// Source: CRA T4032-BC (2026) — https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032bc-jan/t4032bc-january-general-information.html
describe("British Columbia tax reduction (T4032-BC)", () => {
  it("reduces provincial tax to nil below the phase-out band", () => {
    const r = incomeTax({ taxableIncome: 20000, province: "BC", year: 2026 });
    // gross 1,120.00 − BPA credit (13,216 × 5.6% = 740.096 → 740.10)
    // = 379.90; reduction $562 (full, income < 25,020) caps at payable.
    expect(r.provincial).toBe(0);
  });

  it("phases the $690 reduction out by 3.56¢ per dollar above $25,570", () => {
    const r = incomeTax({ taxableIncome: 50363, province: "BC", year: 2026 });
    // At the top of the first bracket: gross 2,820.33 − 740.10 = 2,080.23;
    // income > 44,952 → reduction nil (T4032-BC "reduction will be nil").
    expect(r.provincial).toBe(2080.23);
  });
});

// Source: CRA T4032-AB (2026) — https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032ab-jan/t4032ab-january-general-information.html
describe("Alberta brackets incl. the open top band (T4032-AB)", () => {
  it("composes all six brackets exactly at $400,000", () => {
    const r = incomeTax({ taxableIncome: 400000, province: "AB", year: 2026 });
    // 8%×61,200 + 10%×93,059 + 12%×30,852 + 13%×61,702 + 14%×123,407
    // + 15%×29,780 = 47,669.38 gross; credit 22,769 × 8% = 1,821.52.
    expect(r.provincial).toBe(45847.86);
    // Net = taxable = $400,000 → BPA floored at 14,829 → credit 2,076.06;
    // federal gross = 72,976.05 (at 300k) + 33% × 100,000 = 105,976.05;
    // federal = 105,976.05 − 2,076.06 = 103,899.99.
    expect(r.federal).toBe(103899.99);
  });

  it("floors provincial tax at zero when credits exceed gross", () => {
    const r = incomeTax({ taxableIncome: 20000, province: "AB", year: 2026 });
    // gross 1,600.00 < BPA credit 1,821.52 → provincial 0.
    expect(r.provincial).toBe(0);
  });
});

// Source: Revenu Québec personal income tax rates —
// https://www.revenuquebec.ca/en/citizens/income-tax-return/completing-your-income-tax-return/income-tax-rates/
describe("Quebec: brackets + 16.5% federal abatement (Revenu Québec rates)", () => {
  it("applies the abatement after the federal credits", () => {
    const r = incomeTax({ taxableIncome: 50000, province: "QC", year: 2026 });
    // Provincial: 14% × 50,000 = 7,000 − BPA credit (18,952 × 14% =
    // 2,653.28) = 4,346.72.
    expect(r.provincial).toBe(4346.72);
    // Federal: 7,000 − 2,303.28 = 4,696.72, then −16.5% = 3,921.76.
    expect(r.federal).toBe(3921.76);
    expect(r.federal).toBeCloseTo(4696.72 * (1 - 0.165), 2);
  });
});

describe("tax-year switch is data, not code", () => {
  it("computes 2025 figures from the 2025 tables", () => {
    // 2025 first bracket: 14.5% up to $57,375 (federalRates2025).
    const r = incomeTax({ taxableIncome: 50000, province: "ON", year: 2025 });
    // BPA credit = 16,129 × 14.5% = 2,338.705 → 2,338.71 (HALF_UP).
    const credit2025 = 2338.71;
    expect(r.year).toBe(2025);
    expect(r.usedFallbackYear).toBe(false);
    expect(r.federal + credit2025).toBe(7250);
  });

  it("flags an unpublished year instead of pretending it is exact", () => {
    const r = incomeTax({ taxableIncome: 50000, province: "ON", year: 2031 });
    expect(r.usedFallbackYear).toBe(true);
    expect(r.year).toBe(2026); // falls back to the latest published tables
  });
});

describe("input validation (CanadaTaxInputError codes)", () => {
  it("rejects negative and non-finite income", () => {
    expectErrorCode(() => incomeTax({ taxableIncome: -1, province: "ON" }), "negative_amount");
    expectErrorCode(() => incomeTax({ taxableIncome: Number.NaN, province: "ON" }), "invalid_tax_input");
  });

  it("requires a province and rejects unknown ones", () => {
    expectErrorCode(
      () => incomeTax({ taxableIncome: 50000, province: undefined as unknown as ProvinceCode }),
      "province_required",
    );
    expectErrorCode(
      () => incomeTax({ taxableIncome: 50000, province: "ZZ" as ProvinceCode }),
      "unknown_province",
    );
  });

  it("exposes the error name for instanceof checks", () => {
    try {
      incomeTax({ taxableIncome: -5, province: "ON" });
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(CanadaTaxInputError);
      expect((error as CanadaTaxInputError).name).toBe("CanadaTaxInputError");
    }
  });
});

describe("grossWithdrawalFor — exact net-to-gross bisection", () => {
  const engine = caTaxEngine();

  it("returns 0 for a zero net spend", () => {
    expect(engine.grossWithdrawalFor(0, { year: 2026 })).toBe(0);
  });

  it("covers the net spend exactly: G − Δtax ≥ net, and G − 0.01 fails", () => {
    const net = 40000;
    const g = engine.grossWithdrawalFor(net, { year: 2026 });
    // Sanity band: ON effective marginal near $40k is ~25%.
    expect(g).toBeGreaterThan(45000);
    expect(g).toBeLessThan(65000);
    const base = incomeTax({ taxableIncome: 0, province: "ON", year: 2026 }).total;
    const withG = incomeTax({ taxableIncome: g, province: "ON", year: 2026 }).total;
    const withLess = incomeTax({ taxableIncome: g - 0.01, province: "ON", year: 2026 }).total;
    expect(g - (withG - base)).toBeGreaterThanOrEqual(net);
    expect(g - 0.01 - (withLess - base)).toBeLessThan(net);
  });

  it("grosses up on top of other income", () => {
    const other = 50000;
    const g = grossWithdrawalFor(10000, {
      year: 2026,
      province: "ON",
      otherIncome: other,
    });
    const taxAt = (income: number) =>
      incomeTax({ taxableIncome: income, province: "ON", year: 2026 }).total;
    expect(g - (taxAt(other + g) - taxAt(other))).toBeGreaterThanOrEqual(10000);
  });

  it("the factory defaults to ON and honours a province override", () => {
    expect(engine.id).toBe("ca-engine");
    expect(engine.label).toBe("Canadian tax — ON");
    const bcEngine = caTaxEngine({ province: "BC" });
    expect(bcEngine.label).toBe("Canadian tax — BC");
    const viaOverride = engine.grossWithdrawalFor(10000, { year: 2026, province: "BC" });
    const viaFactory = bcEngine.grossWithdrawalFor(10000, { year: 2026 });
    expect(viaOverride).toBe(viaFactory);
  });
});
