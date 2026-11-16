/**
 * Canadian tax engine v1 — registered accounts (docs/58-canada-tax-engine.md).
 * Expected figures are CRA-published (URLs in each docstring); the LIF
 * percentages are pinned against FSRA's published 6.00% table, which the
 * engine's annuity-due formula reproduces to five decimals.
 */
import { describe, expect, it } from "vitest";
import {
  CanadaTaxInputError,
  fhsaRoom,
  lifMaximumWithdrawal,
  liraSmallBalanceUnlock,
  respCesg,
  respClb,
  rrifMinimum,
  rrspOverContribution,
  rrspRoom,
  spousalRrspAttribution,
  tfsaRoom,
} from "../src/lib/canadaTaxEngine";


// Sources: RRSP deduction-limit formula —
// https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/contributions-affect-your-rrsp-prpp-deduction-limit.html
// and the 2026/2025 dollar limits ($33,810 / $32,490) —
// https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html

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

describe("RRSP deduction room (CRA deduction-limit formula)", () => {
  // RRSP_LIMITS_URL: 2026 limit = 18% of prior-year earned income up to
  // $33,810; room = prior unused + increment − pension adjustment.
  it("computes 18% of earned income up to the dollar limit", () => {
    expect(rrspRoom({ earnedIncome: 80000, year: 2026 })).toBe(14400);
    // 18% × 300,000 = 54,000 → capped at the 2026 limit of $33,810.
    expect(rrspRoom({ earnedIncome: 300000, year: 2026 })).toBe(33810);
    // 2025 limit is $32,490 — the switch is data, not code.
    expect(rrspRoom({ earnedIncome: 300000, year: 2025 })).toBe(32490);
  });

  it("adds prior unused room and subtracts the pension adjustment", () => {
    expect(rrspRoom({ earnedIncome: 80000, priorUnusedRoom: 5000, year: 2026 })).toBe(19400);
    expect(
      rrspRoom({ earnedIncome: 80000, priorUnusedRoom: 5000, pensionAdjustment: 2000, year: 2026 }),
    ).toBe(17400);
  });

  it("keeps a negative limit (PSPA excess) instead of flooring it", () => {
    // 18% × 10,000 = 1,800 + 1,000 unused − 5,000 PA = −2,200.
    expect(
      rrspRoom({ earnedIncome: 10000, priorUnusedRoom: 1000, pensionAdjustment: 5000, year: 2026 }),
    ).toBe(-2200);
  });

  it("refuses an unpublished year (room cannot be estimated)", () => {
    expectErrorCode(() => rrspRoom({ earnedIncome: 80000, year: 2031 }), "tax_year_not_published");
  });
});

// Source: CRA excess-contribution rules — https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/what-happens-you-over-your-rrsp-prpp-deduction-limit.html
describe("RRSP over-contribution (CRA: $2,000 grace, 1%/month on the rest)", () => {
  it("reports excess, taxable excess, and the monthly penalty", () => {
    // RRSP_URL: excess above limit+2,000 is taxable; 1% per month applies
    // to the amount over limit + $2,000.
    const r = rrspOverContribution({ contributions: 40000, deductionLimit: 35810 });
    expect(r.excess).toBe(4190);
    expect(r.taxableExcess).toBe(2190);
    expect(r.penaltyPerMonth).toBe(21.9);
  });

  it("graces the first $2,000 of excess", () => {
    const r = rrspOverContribution({ contributions: 36000, deductionLimit: 35810 });
    expect(r.excess).toBe(190);
    expect(r.taxableExcess).toBe(0);
    expect(r.penaltyPerMonth).toBe(0);
  });

  it("rejects negative inputs", () => {
    expectErrorCode(() => rrspOverContribution({ contributions: -1, deductionLimit: 0 }), "negative_amount");
  });
});

// Source: CRA TFSA room calculation + annual dollar-limit table —
// https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html
describe("TFSA contribution room (CRA cumulative-limit table)", () => {
  // TFSA_URL: room = cumulative limits since 2009 − contributions +
  // withdrawals (withdrawals restore room the following January).
  it("sums the published annual limits through the year", () => {
    // 2009–2026: 4×5,000 + 2×5,500 + 10,000 + 3×5,500 + 4×6,000 +
    // 6,500 + 3×7,000 = 109,000 (2024 = 95,000, the published cumulative
    // figure, is a mid-check).
    expect(tfsaRoom({ contributions: 0, withdrawals: 0, year: 2026 })).toBe(109000);
    expect(tfsaRoom({ contributions: 0, withdrawals: 0, year: 2024 })).toBe(95000);
    expect(tfsaRoom({ contributions: 0, withdrawals: 0, year: 2025 })).toBe(102000);
    expect(tfsaRoom({ contributions: 0, withdrawals: 0, year: 2009 })).toBe(5000);
  });

  it("nets contributions and restores withdrawn room", () => {
    expect(tfsaRoom({ contributions: 20000, withdrawals: 5000, year: 2026 })).toBe(94000);
    expect(tfsaRoom({ contributions: 110000, withdrawals: 0, year: 2026 })).toBe(0);
  });

  it("refuses an unpublished year", () => {
    expectErrorCode(() => tfsaRoom({ contributions: 0, withdrawals: 0, year: 2031 }), "tax_year_not_published");
  });
});

// Source: CRA contributing to an FHSA — https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html
describe("FHSA participation room (CRA: $8,000/yr, $40,000 lifetime)", () => {
  it("grants the annual room when unused", () => {
    expect(fhsaRoom({ contributions: 0, year: 2026 })).toBe(8000);
    expect(fhsaRoom({ contributions: 0, unusedRoom: 10000, year: 2026 })).toBe(18000);
  });

  it("caps by the $40,000 lifetime limit", () => {
    expect(fhsaRoom({ contributions: 35000, year: 2026 })).toBe(5000);
    expect(fhsaRoom({ contributions: 40000, year: 2026 })).toBe(0);
    // Over-contributed lifetime: floor at 0.
    expect(fhsaRoom({ contributions: 50000, year: 2026 })).toBe(0);
  });

  it("refuses an unpublished year", () => {
    expectErrorCode(() => fhsaRoom({ contributions: 0, year: 2031 }), "tax_year_not_published");
  });
});

// Source: CRA Canada Education Savings Grant — https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html
describe("RESP CESG matching (CRA CESG: 20% basic + additional by income)", () => {
  // CESG_URL: basic 20% on the first $2,500 ($500); additional 20% below
  // the year's low threshold, 10% up to the high threshold, 0 above —
  // on the first $500; $7,200 lifetime.
  it("pays $600 on a full basic contribution at low income", () => {
    // $2,500 → $500 basic + 20% × $500 additional = $600 (2026 threshold
    // $58,523: family income $50,000 is below it).
    expect(respCesg({ contribution: 2500, familyNetIncome: 50000, year: 2026 })).toBe(600);
  });

  it("steps the additional grant down with income", () => {
    // Between $58,523 and $117,045: 10% additional → $550.
    expect(respCesg({ contribution: 2500, familyNetIncome: 60000, year: 2026 })).toBe(550);
    // Above $117,045: basic only → $500.
    expect(respCesg({ contribution: 2500, familyNetIncome: 120000, year: 2026 })).toBe(500);
    // 2025 thresholds differ ($57,375 / $114,750) — data, not code.
    expect(respCesg({ contribution: 2500, familyNetIncome: 58000, year: 2025 })).toBe(550);
    expect(respCesg({ contribution: 2500, familyNetIncome: 58000, year: 2026 })).toBe(600);
  });

  it("prorates when the contribution is under $2,500", () => {
    // $1,000 → 20% × 1,000 = $200 basic + 20% × min(1,000, 500) = $100.
    expect(respCesg({ contribution: 1000, familyNetIncome: 50000, year: 2026 })).toBe(300);
  });

  it("uses carried-forward room up to the $5,000 catch-up base", () => {
    // unused $2,500 → base = min(5,000, 2,500 + 2,500) → $1,000 basic.
    expect(
      respCesg({
        contribution: 5000,
        familyNetIncome: 50000,
        unusedCesgRoom: 2500,
        year: 2026,
      }),
    ).toBe(1100);
  });

  it("caps at the $7,200 lifetime grant", () => {
    expect(respCesg({ contribution: 2500, familyNetIncome: 50000, priorCesg: 7000, year: 2026 })).toBe(
      200,
    );
    expect(respCesg({ contribution: 2500, familyNetIncome: 50000, priorCesg: 7200, year: 2026 })).toBe(
      0,
    );
  });

  it("ends the grant after age 17", () => {
    expect(
      respCesg({ contribution: 2500, familyNetIncome: 50000, beneficiaryAge: 17, year: 2026 }),
    ).toBe(600);
    expect(
      respCesg({ contribution: 2500, familyNetIncome: 50000, beneficiaryAge: 18, year: 2026 }),
    ).toBe(0);
  });
});

// Source: CRA Canada Learning Bond — https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-learning-bond.html
describe("RESP CLB (CRA: $500 first year, $100 after, $2,000 lifetime)", () => {
  // CLB_URL thresholds: 2026 (1–3 children) ≤ $58,523; $58,524–$66,035;
  // $66,036–$73,576 (the $100 continuation), none above $73,577.
  it("pays $500 in the first eligible year, $100 after", () => {
    expect(respClb({ familyNetIncome: 50000, childCount: 2, year: 2026 })).toBe(500);
    expect(respClb({ familyNetIncome: 50000, childCount: 2, priorClb: 500, year: 2026 })).toBe(100);
    expect(
      respClb({
        familyNetIncome: 50000,
        childCount: 2,
        priorClb: 500,
        beneficiaryAge: 10,
        year: 2026,
      }),
    ).toBe(100);
  });

  it("stops the $100 continuation after age 15", () => {
    expect(
      respClb({
        familyNetIncome: 50000,
        childCount: 2,
        priorClb: 500,
        beneficiaryAge: 16,
        year: 2026,
      }),
    ).toBe(0);
  });

  it("caps at the $2,000 lifetime maximum", () => {
    expect(respClb({ familyNetIncome: 50000, childCount: 2, priorClb: 1950, year: 2026 })).toBe(50);
    expect(respClb({ familyNetIncome: 50000, childCount: 2, priorClb: 2000, year: 2026 })).toBe(0);
  });

  it("respects the inclusive threshold at the family-size boundary", () => {
    // Exactly $58,523 is still eligible (1–3 children band is inclusive);
    // $58,524 is not.
    expect(respClb({ familyNetIncome: 58523, childCount: 1, year: 2026 })).toBe(500);
    expect(respClb({ familyNetIncome: 58524, childCount: 1, year: 2026 })).toBe(0);
    // 4 children: threshold $66,036 (exclusive) → 60,000 pays.
    expect(respClb({ familyNetIncome: 60000, childCount: 4, year: 2026 })).toBe(500);
    // 5 children: threshold $73,577 (exclusive) → 70,000 pays.
    expect(respClb({ familyNetIncome: 70000, childCount: 5, year: 2026 })).toBe(500);
  });

  it("rejects an impossible family size", () => {
    expectErrorCode(() => respClb({ familyNetIncome: 50000, childCount: 6, year: 2026 }), "invalid_child_count");
  });
});

// Source: Income Tax Regulations s. 7308 — https://laws-lois.justice.gc.ca/eng/regulations/C.R.C.,_c._945/section-7308.html
describe("RRIF minimums (Income Tax Regulations s. 7308(4) 'all other' RRIFs)", () => {
  // RRIF_URL: 1/(90 − age) before 71; the prescribed table from 71
  // (5.28% at 71 … 18.79% at 94); 20% from 95.
  it("uses the statutory formula below age 71", () => {
    expect(rrifMinimum({ age: 65, value: 100000 })).toBe(4000); // 1/25
    expect(rrifMinimum({ age: 70, value: 100000 })).toBe(5000); // 1/20
    expect(rrifMinimum({ age: 54, value: 100000 })).toBe(2777.78); // 1/36, HALF_UP
  });

  it("uses the prescribed factors from age 71", () => {
    expect(rrifMinimum({ age: 71, value: 100000 })).toBe(5280);
    expect(rrifMinimum({ age: 72, value: 100000 })).toBe(5400);
    expect(rrifMinimum({ age: 75, value: 100000 })).toBe(5820);
    expect(rrifMinimum({ age: 94, value: 100000 })).toBe(18790);
    expect(rrifMinimum({ age: 95, value: 100000 })).toBe(20000);
    // Past 95 the table stops at 20%.
    expect(rrifMinimum({ age: 100, value: 100000 })).toBe(20000);
  });

  it("rejects impossible ages", () => {
    expectErrorCode(() => rrifMinimum({ age: -1, value: 100 }), "invalid_age");
    expectErrorCode(() => rrifMinimum({ age: 71.5, value: 100 }), "invalid_age");
  });
});

// Source: FSRA LIF/LRIF maximum annual income payment table (6.00% basis) —
// https://www.fsrao.ca/industry/pensions/regulatory-framework/guidance-pensions/life-income-fund-lif-and-locked-retirement-income-fund-lrif-maximum-annual-income-payment-amount-table
describe("LIF maximums (Ontario Reg. 909 / FSRA published 6.00% table)", () => {
  // LIF_URL: FSRA's table is C/F with F = annuity-due present value to
  // age 90 at the 6% floor — the engine reproduces each figure to five
  // decimals: 65 → 7.25513%, 71 → 8.22496%, 75 → 9.33511%,
  // 80 → 11.96160%, 90 → 100%.
  it("matches FSRA's published percentages", () => {
    expect(lifMaximumWithdrawal({ age: 65, value: 100000 })).toBe(7255.13);
    expect(lifMaximumWithdrawal({ age: 71, value: 100000 })).toBe(8224.96);
    expect(lifMaximumWithdrawal({ age: 75, value: 100000 })).toBe(9335.11);
    expect(lifMaximumWithdrawal({ age: 80, value: 100000 })).toBe(11961.6);
    expect(lifMaximumWithdrawal({ age: 90, value: 100000 })).toBe(100000);
  });

  it("takes the greater of prior-year earnings (A) and the formula (B)", () => {
    expect(lifMaximumWithdrawal({ age: 65, value: 50000, priorYearEarnings: 9000 })).toBe(9000);
    expect(lifMaximumWithdrawal({ age: 71, value: 100000, priorYearEarnings: 500 })).toBe(8224.96);
  });

  it("rejects impossible ages", () => {
    expectErrorCode(() => lifMaximumWithdrawal({ age: -1, value: 100 }), "invalid_age");
  });
});

// Source: FSRA small-balance unlocking guidance (40% of YMPE from age 55) —
// https://www.fsrao.ca/media/8321/download
describe("Ontario LIRA small-balance unlocking (FSRA: 55+, under 40% of YMPE)", () => {
  it("computes the 40%-of-YMPE limit for the year", () => {
    // 2026 YMPE $74,600 → limit $29,840 (2025: $71,300 → $28,520).
    const r = liraSmallBalanceUnlock({ age: 56, lockedInTotal: 20000, year: 2026 });
    expect(r.limit).toBe(29840);
    expect(r.eligible).toBe(true);
    expect(liraSmallBalanceUnlock({ age: 56, lockedInTotal: 20000, year: 2025 }).limit).toBe(28520);
  });

  it("gates on age and on being strictly under the limit", () => {
    expect(liraSmallBalanceUnlock({ age: 54, lockedInTotal: 20000, year: 2026 }).eligible).toBe(
      false,
    );
    // Exactly at the limit is NOT under it.
    expect(liraSmallBalanceUnlock({ age: 56, lockedInTotal: 29840, year: 2026 }).eligible).toBe(
      false,
    );
    expect(liraSmallBalanceUnlock({ age: 56, lockedInTotal: 29839.99, year: 2026 }).eligible).toBe(
      true,
    );
  });

  it("refuses an unpublished year (YMPE is year-published)", () => {
    expectErrorCode(() => liraSmallBalanceUnlock({ age: 56, lockedInTotal: 10000, year: 2031 }), "tax_year_not_published");
  });
});

// Source: CRA spousal/common-law partner RRSPs — https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/funding-your-rrsp/prpps-mrps/spousal-common-law-partners-rrsps.html
describe("spousal RRSP 3-year attribution (CRA)", () => {
  // SPousal_URL: withdrawals within the contribution year and the two
  // preceding years are attributed to the contributor.
  it("attributes within the three-year window", () => {
    expect(spousalRrspAttribution({ lastContributionYear: 2026, withdrawalYear: 2026 })).toBe(true);
    expect(spousalRrspAttribution({ lastContributionYear: 2024, withdrawalYear: 2026 })).toBe(true);
  });

  it("releases attribution after two full years have passed", () => {
    // 2026 − 2023 = 3 → the 2023 contribution is outside the window.
    expect(spousalRrspAttribution({ lastContributionYear: 2023, withdrawalYear: 2026 })).toBe(false);
  });
});

describe("validation codes", () => {
  it("rejects negative account inputs with the shared code", () => {
    expectErrorCode(() => rrspRoom({ earnedIncome: -1, year: 2026 }), "negative_amount");
    expectErrorCode(() => tfsaRoom({ contributions: -1, withdrawals: 0, year: 2026 }), "negative_amount");
    expectErrorCode(() => fhsaRoom({ contributions: -1, year: 2026 }), "negative_amount");
    expectErrorCode(() => rrifMinimum({ age: 71, value: -1 }), "negative_amount");
  });

  it("is exported as a class for instanceof checks", () => {
    expect(new CanadaTaxInputError("x", "y")).toBeInstanceOf(Error);
  });
});
