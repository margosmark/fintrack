/**
 * Versioned Canadian tax-year tables — the DATA half of the Canadian tax
 * engine v1 (docs/58-canada-tax-engine.md). Pure data + one resolver; all
 * arithmetic lives in canadaTaxEngine.ts.
 *
 * Every table carries the primary-source URL(s) it was published from. The
 * figures were taken verbatim from those sources (CRA rate pages, T4032
 * payroll tables, TD1/TD1-WS forms, Revenu Québec, Service Canada/ESDC,
 * Income Tax Regulations s. 7308, Ontario Reg. 909, FSRA) — see the `sources`
 * field on each table. Adding a tax year means adding one TaxYearTables
 * entry here; no code changes (the year switch is data, not code).
 *
 * Precision model: every dollar figure is a whole number of dollars as
 * published; rates are basis points (bp) of a percent-of-dollar, i.e. 14% is
 * 1400. The engine converts to integer cents internally.
 */

/** Province/territory code — the 13 jurisdictions with their own income tax. */
export type ProvinceCode =
  | "AB"
  | "BC"
  | "MB"
  | "NB"
  | "NL"
  | "NT"
  | "NS"
  | "NU"
  | "ON"
  | "PE"
  | "QC"
  | "SK"
  | "YT";

/** One progressive bracket. `upTo` is the inclusive top of the bracket in
 * dollars; `null` means "unlimited" (the top bracket). */
export interface Bracket {
  readonly rateBp: number;
  readonly upTo: number | null;
}

/**
 * Non-refundable basic personal amount (BPA) credit base, in dollars.
 * `max` is the published maximum claim; `min` the published minimum. When
 * `phaseOutFrom`/`phaseOutTo` are present the BPA reduces linearly from
 * `max` at `phaseOutFrom` of net income to `min` at `phaseOutTo`
 * (federal Form TD1-WS "partial claim" — and Yukon, which mirrors the
 * federal amounts). All other jurisdictions publish a flat BPA.
 */
export interface BasicPersonalAmount {
  readonly max: number;
  readonly min: number;
  readonly phaseOutFrom?: number;
  readonly phaseOutTo?: number;
}

/** Ontario surtax band: `rateBp` applies to (basic provincial tax − over). */
export interface SurtaxBand {
  readonly over: number;
  readonly rateBp: number;
}

/**
 * Ontario health premium band, applied to TAXABLE INCOME. For the greatest
 * `from` with income > from: premium = min(cap, base + rateBp/10000 ×
 * (income − from)) in dollars. Band `from: 0, cap: 0` yields 0 below the
 * first threshold.
 */
export interface HealthPremiumBand {
  readonly from: number;
  readonly cap: number;
  readonly base: number;
  readonly rateBp: number;
}

/** Ontario low-income tax reduction: 2 × personalAmount − basic provincial
 * tax, floored at 0 and capped at that basic tax (T4032-ON "Tax reduction"). */
export interface OnLowIncomeReduction {
  readonly kind: "on-personal-amounts";
  readonly personalAmount: number;
}

/** British Columbia tax reduction: full `max` up to `fullUpTo` of income,
 * then reduced by `rateBp` per dollar of income, reaching 0 at `nilAt`. */
export interface BcTaxReduction {
  readonly kind: "bc-phaseout";
  readonly max: number;
  readonly fullUpTo: number;
  readonly rateBp: number;
  readonly nilAt: number;
}

export type TaxReduction = OnLowIncomeReduction | BcTaxReduction;

/** Per-jurisdiction tables for one tax year. */
export interface ProvinceTables {
  readonly brackets: readonly Bracket[];
  readonly bpa: BasicPersonalAmount;
  readonly surtax?: readonly SurtaxBand[];
  readonly healthPremium?: readonly HealthPremiumBand[];
  readonly taxReduction?: TaxReduction;
  /** Quebec residents: federal tax is reduced by this many bp (1650 = 16.5%). */
  readonly federalAbatementBp?: number;
  readonly sources: readonly string[];
}

export interface FederalTables {
  readonly brackets: readonly Bracket[];
  readonly bpa: BasicPersonalAmount;
  readonly sources: readonly string[];
}

/** RESP/CEESP parameters for one year. */
export interface RespTables {
  /** Lifetime RESP contribution limit (personal contributions only). */
  readonly lifetimeContributionLimit: number;
  readonly cesgAnnualBasicMax: number;
  readonly cesgLifetimeMax: number;
  /** Basic CESG base: 20% is paid on the first $2,500 contributed … */
  readonly cesgBasicBase: number;
  /** … and on up to $5,000 when prior-year grant room is carried forward. */
  readonly cesgCatchupBase: number;
  /** Additional CESG: 20% below income 1, 10% up to income 2, none above. */
  readonly additionalCesgLowThreshold: number;
  readonly additionalCesgHighThreshold: number;
  /** CLB income thresholds indexed by family size (1–3, 4, 5 children). */
  readonly clbThresholds: readonly {
    readonly limit: number;
    readonly inclusive: boolean;
  }[];
  readonly sources: readonly string[];
}

/** LIRA / locked-in parameters for one year. */
export interface LiraTables {
  /** Small-balance unlocking: eligible from this age. */
  readonly unlockAge: number;
  /** … when total locked-in assets are under this fraction of YMPE (bp). */
  readonly unlockFracBp: number;
  readonly sources: readonly string[];
}

export interface RegisteredTables {
  readonly rrspDollarLimit: number;
  readonly tfsaAnnualLimit: number;
  readonly fhsaAnnualLimit: number;
  readonly fhsaLifetimeLimit: number;
  readonly resp: RespTables;
  readonly lira: LiraTables;
  readonly sources: readonly string[];
}

/** CPP parameters for one year. */
export interface CppTables {
  readonly ympe: number;
  readonly yampe: number;
  readonly ybe: number;
  /** Employee contribution rate, bp of pensionable earnings (595 = 5.95%). */
  readonly rateBp: number;
  readonly cpp2RateBp: number;
  /** Maximum retirement pension at 65, dollars per month (published). */
  readonly maxMonthlyAt65: number;
  /** Start-age adjustment: 0.6%/month early (60bp), 0.7%/month late (70bp). */
  readonly earlyBpPerMonth: number;
  readonly lateBpPerMonth: number;
  /** Absolute factor caps: 6400 = 64% at age 60 (−36%), 14200 = 142% at
   * age 70 (+42%). */
  readonly maxEarlyBp: number;
  readonly maxLateBp: number;
  readonly sources: readonly string[];
}

/** OAS parameters for one year. Monthly amounts are the Q4 (Oct–Dec)
 * figures for that calendar year — the only quarter a whole-year table can
 * pin; quarterly indexation is documented in docs/58. */
export interface OasTables {
  readonly monthly65To74: number;
  readonly monthly75Plus: number;
  /** Net-income threshold above which the recovery tax (clawback) starts. */
  readonly recoveryThreshold: number;
  readonly recoveryRateBp: number;
  /** Deferral: 0.6%/month after 65 (60bp), absolute ceiling 13600 = 136%
   * (+36% at 70). */
  readonly deferralBpPerMonth: number;
  readonly maxDeferralBp: number;
  readonly sources: readonly string[];
}

/** All tables for one tax year. */
export interface TaxYearTables {
  readonly year: number;
  readonly federal: FederalTables;
  readonly provinces: Readonly<Record<ProvinceCode, ProvinceTables>>;
  readonly registered: RegisteredTables;
  readonly cpp: CppTables;
  readonly oas: OasTables;
}

/** Primary sources. Verified reachable (HTTP 200) 2026-11-10 except the
 * Revenu Québec and FSRA pages, which answer 403 to scripted clients but are
 * live in a browser. */
const SRC = {
  fedRates2026:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html",
  fedRates2025:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/last-year.html",
  td1Ws: "https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-2025-later/td1-ws.html",
  td1Forms2026:
    "https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-later.html",
  td1Forms2025:
    "https://www.canada.ca/en/revenue-agency/services/forms-publications/td1-personal-tax-credits-returns/td1-forms-pay-received-on-january-1-2025-later.html",
  t4032Jan2026: (p: string): string =>
    `https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables/t4032${p}-jan/t4032${p}-january-general-information.html`,
  t4032Jul2025: (p: string): string =>
    `https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4032-payroll-deductions-tables-previous-years/t4032${p}-july-2025/t4032${p}-july-general-information.html`,
  qcRates:
    "https://www.revenuquebec.ca/en/citizens/income-tax-return/completing-your-income-tax-return/income-tax-rates/",
  qcBpa2025:
    "https://www.revenuquebec.ca/documents/en/formulaires/tp/2025-12/TP-1.G-V%282025-12%29.pdf",
  qcBpa2026:
    "https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/employers-kit/principal-changes-for-2026-employers-kit/",
  mbFinance: "https://www.gov.mb.ca/finance/personal/ptaxes.html",
  peiTaxes:
    "https://www.princeedwardisland.ca/en/information/finance-and-affordability/provincial-personal-income-tax",
  rrspsLimits:
    "https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html",
  rrspDeductionLimit:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/contributions-affect-your-rrsp-prpp-deduction-limit.html",
  rrspOverContribution:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/what-happens-you-over-your-rrsp-prpp-deduction-limit.html",
  tfsaRoom:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html",
  tfsaBefore:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/before.html",
  fhsaContrib:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html",
  cesg:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html",
  cesgEstimate:
    "https://www.canada.ca/en/services/benefits/education/education-savings/estimating-amounts.html",
  clb: "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-learning-bond.html",
  respContrib:
    "https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/resp-contributions.html",
  rrifMinimum:
    "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/completing-slips-summaries/t4rsp-t4rif-information-returns/payments/minimum-amount-a-rrif.html",
  itr7308:
    "https://laws-lois.justice.gc.ca/eng/regulations/C.R.C.,_c._945/section-7308.html",
  onReg909: "https://www.ontario.ca/laws/regulation/900909",
  fsraLifMax:
    "https://www.fsrao.ca/industry/pensions/regulatory-framework/guidance-pensions/life-income-fund-lif-and-locked-retirement-income-fund-lrif-maximum-annual-income-payment-amount-table",
  fsraSmallBalance: "https://www.fsrao.ca/media/8321/download",
  cppRates:
    "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/canada-pension-plan-cpp/cpp-contribution-rates-maximums-exemptions.html",
  cpp2Rates:
    "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html",
  esdcQuarter2026:
    "https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2026-quarterly-october-december.html",
  esdcQuarter2025:
    "https://www.canada.ca/en/employment-social-development/programs/pensions/pension/statistics/2025-quarterly-october-december.html",
  cppWhenStart: "https://www.canada.ca/en/services/benefits/publicpensions/cpp/when-start.html",
  oasPayments:
    "https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/payments.html",
  oasWhenStart:
    "https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/when-start.html",
  oasRecovery:
    "https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html",
} as const;

/**
 * Prescribed RRIF withdrawal factors (Income Tax Regulations s. 7308(4),
 * "all other" RRIFs) in basis points of the fund value, ages 71–94;
 * age 95+ is 20.00%. Ages under 71 use the statutory formula
 * 1/(90 − age) — see rrifMinimum in canadaTaxEngine.ts.
 */
export const RRIF_FACTORS_BP: Readonly<Record<number, number>> = {
  71: 528,
  72: 540,
  73: 553,
  74: 567,
  75: 582,
  76: 598,
  77: 617,
  78: 636,
  79: 658,
  80: 682,
  81: 708,
  82: 738,
  83: 771,
  84: 808,
  85: 851,
  86: 899,
  87: 955,
  88: 1021,
  89: 1099,
  90: 1192,
  91: 1306,
  92: 1449,
  93: 1634,
  94: 1879,
  95: 2000,
};

/**
 * TFSA annual dollar limits (CRA TFSA and ALDA dollar limits page; the
 * same source publishes every row). Used to compute cumulative room.
 */
export const TFSA_ANNUAL_LIMITS: Readonly<Record<number, number>> = {
  2009: 5000,
  2010: 5000,
  2011: 5000,
  2012: 5000,
  2013: 5500,
  2014: 5500,
  2015: 10000,
  2016: 5500,
  2017: 5500,
  2018: 5500,
  2019: 6000,
  2020: 6000,
  2021: 6000,
  2022: 6000,
  2023: 6500,
  2024: 7000,
  2025: 7000,
  2026: 7000,
};

/** Ontario health premium band schedule — byte-identical for 2025 and 2026
 * (T4032-ON: "the premium is whichever amount is less …"). */
const ON_HEALTH_PREMIUM: readonly HealthPremiumBand[] = [
  { from: 0, cap: 0, base: 0, rateBp: 0 },
  { from: 20000, cap: 300, base: 0, rateBp: 600 },
  { from: 36000, cap: 450, base: 300, rateBp: 600 },
  { from: 48000, cap: 600, base: 450, rateBp: 2500 },
  { from: 72000, cap: 750, base: 600, rateBp: 2500 },
  { from: 200000, cap: 900, base: 750, rateBp: 2500 },
];

const FEDERAL_SOURCES_2026 = [SRC.fedRates2026, SRC.td1Ws, SRC.td1Forms2026];
const FEDERAL_SOURCES_2025 = [SRC.fedRates2025, SRC.td1Ws, SRC.td1Forms2025];

/** Tax-year tables. The two published years; see resolveTaxYear for how an
 * unpublished year degrades (fallback to the latest, flagged). */
export const TAX_YEARS: Readonly<Record<number, TaxYearTables>> = {
  2026: {
    year: 2026,
    federal: {
      brackets: [
        { rateBp: 1400, upTo: 58523 },
        { rateBp: 2050, upTo: 117045 },
        { rateBp: 2600, upTo: 181440 },
        { rateBp: 2900, upTo: 258482 },
        { rateBp: 3300, upTo: null },
      ],
      bpa: { max: 16452, min: 14829, phaseOutFrom: 181440, phaseOutTo: 258482 },
      sources: FEDERAL_SOURCES_2026,
    },
    provinces: {
      ON: {
        brackets: [
          { rateBp: 505, upTo: 53891 },
          { rateBp: 915, upTo: 107785 },
          { rateBp: 1116, upTo: 150000 },
          { rateBp: 1216, upTo: 220000 },
          { rateBp: 1316, upTo: null },
        ],
        bpa: { max: 12989, min: 12989 },
        surtax: [
          { over: 5818, rateBp: 2000 },
          { over: 7446, rateBp: 3600 },
        ],
        healthPremium: ON_HEALTH_PREMIUM,
        taxReduction: { kind: "on-personal-amounts", personalAmount: 300 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("on"), SRC.td1Forms2026],
      },
      BC: {
        brackets: [
          { rateBp: 560, upTo: 50363 },
          { rateBp: 770, upTo: 100728 },
          { rateBp: 1050, upTo: 115648 },
          { rateBp: 1229, upTo: 140430 },
          { rateBp: 1470, upTo: 190405 },
          { rateBp: 1680, upTo: 265545 },
          { rateBp: 2050, upTo: null },
        ],
        bpa: { max: 13216, min: 13216 },
        taxReduction: {
          kind: "bc-phaseout",
          max: 690,
          fullUpTo: 25570,
          rateBp: 356,
          nilAt: 44952,
        },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("bc"), SRC.td1Forms2026],
      },
      AB: {
        brackets: [
          { rateBp: 800, upTo: 61200 },
          { rateBp: 1000, upTo: 154259 },
          { rateBp: 1200, upTo: 185111 },
          { rateBp: 1300, upTo: 246813 },
          { rateBp: 1400, upTo: 370220 },
          { rateBp: 1500, upTo: null },
        ],
        bpa: { max: 22769, min: 22769 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("ab"), SRC.td1Forms2026],
      },
      QC: {
        brackets: [
          { rateBp: 1400, upTo: 54345 },
          { rateBp: 1900, upTo: 108680 },
          { rateBp: 2400, upTo: 132245 },
          { rateBp: 2575, upTo: null },
        ],
        bpa: { max: 18952, min: 18952 },
        federalAbatementBp: 1650,
        sources: [SRC.qcRates, SRC.qcBpa2026, SRC.t4032Jan2026("qc")],
      },
      SK: {
        brackets: [
          { rateBp: 1050, upTo: 54532 },
          { rateBp: 1250, upTo: 155805 },
          { rateBp: 1450, upTo: null },
        ],
        bpa: { max: 20381, min: 20381 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("sk"), SRC.td1Forms2026],
      },
      MB: {
        brackets: [
          { rateBp: 1080, upTo: 47000 },
          { rateBp: 1275, upTo: 100000 },
          { rateBp: 1740, upTo: null },
        ],
        bpa: { max: 15780, min: 15780 },
        sources: [SRC.t4032Jan2026("mb"), SRC.mbFinance, SRC.td1Forms2026],
      },
      NB: {
        brackets: [
          { rateBp: 940, upTo: 52333 },
          { rateBp: 1400, upTo: 104666 },
          { rateBp: 1600, upTo: 193861 },
          { rateBp: 1950, upTo: null },
        ],
        bpa: { max: 13664, min: 13664 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("nb"), SRC.td1Forms2026],
      },
      NS: {
        brackets: [
          { rateBp: 879, upTo: 30995 },
          { rateBp: 1495, upTo: 61991 },
          { rateBp: 1667, upTo: 97417 },
          { rateBp: 1750, upTo: 157124 },
          { rateBp: 2100, upTo: null },
        ],
        bpa: { max: 11932, min: 11932 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("ns"), SRC.td1Forms2026],
      },
      PE: {
        brackets: [
          { rateBp: 950, upTo: 33928 },
          { rateBp: 1347, upTo: 65820 },
          { rateBp: 1660, upTo: 106890 },
          { rateBp: 1762, upTo: 142520 },
          { rateBp: 1900, upTo: 200000 },
          { rateBp: 2000, upTo: null },
        ],
        bpa: { max: 15000, min: 15000 },
        sources: [SRC.fedRates2026, SRC.peiTaxes, SRC.td1Forms2026],
      },
      NL: {
        brackets: [
          { rateBp: 870, upTo: 44678 },
          { rateBp: 1450, upTo: 89354 },
          { rateBp: 1580, upTo: 159528 },
          { rateBp: 1780, upTo: 223340 },
          { rateBp: 1980, upTo: 285319 },
          { rateBp: 2080, upTo: 570638 },
          { rateBp: 2130, upTo: 1141275 },
          { rateBp: 2180, upTo: null },
        ],
        bpa: { max: 11188, min: 11188 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("nl"), SRC.td1Forms2026],
      },
      YT: {
        brackets: [
          { rateBp: 640, upTo: 58523 },
          { rateBp: 900, upTo: 117045 },
          { rateBp: 1090, upTo: 181440 },
          { rateBp: 1280, upTo: 500000 },
          { rateBp: 1500, upTo: null },
        ],
        // Yukon mirrors the federal amounts, including the TD1-WS partial
        // claim (TD1YT-26: "If your income from all sources will be greater
        // than $181,440 you have the option to calculate a partial claim").
        bpa: { max: 16452, min: 14829, phaseOutFrom: 181440, phaseOutTo: 258482 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("yt"), SRC.td1Forms2026],
      },
      NT: {
        brackets: [
          { rateBp: 590, upTo: 53003 },
          { rateBp: 860, upTo: 106009 },
          { rateBp: 1220, upTo: 172346 },
          { rateBp: 1405, upTo: null },
        ],
        bpa: { max: 18198, min: 18198 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("nt"), SRC.td1Forms2026],
      },
      NU: {
        brackets: [
          { rateBp: 400, upTo: 55801 },
          { rateBp: 700, upTo: 111602 },
          { rateBp: 900, upTo: 181439 },
          { rateBp: 1150, upTo: null },
        ],
        bpa: { max: 19659, min: 19659 },
        sources: [SRC.fedRates2026, SRC.t4032Jan2026("nu"), SRC.td1Forms2026],
      },
    },
    registered: {
      rrspDollarLimit: 33810,
      tfsaAnnualLimit: 7000,
      fhsaAnnualLimit: 8000,
      fhsaLifetimeLimit: 40000,
      resp: {
        lifetimeContributionLimit: 50000,
        cesgAnnualBasicMax: 500,
        cesgLifetimeMax: 7200,
        cesgBasicBase: 2500,
        cesgCatchupBase: 5000,
        additionalCesgLowThreshold: 58523,
        additionalCesgHighThreshold: 117045,
        clbThresholds: [
          { limit: 58523, inclusive: true },
          { limit: 66036, inclusive: false },
          { limit: 73577, inclusive: false },
        ],
        sources: [SRC.rrspsLimits, SRC.cesg, SRC.cesgEstimate, SRC.clb, SRC.respContrib],
      },
      lira: {
        unlockAge: 55,
        unlockFracBp: 4000,
        sources: [SRC.onReg909, SRC.fsraSmallBalance, SRC.cppRates],
      },
      sources: [
        SRC.rrspsLimits,
        SRC.rrspDeductionLimit,
        SRC.rrspOverContribution,
        SRC.tfsaRoom,
        SRC.tfsaBefore,
        SRC.fhsaContrib,
        SRC.rrifMinimum,
        SRC.itr7308,
        SRC.onReg909,
        SRC.fsraLifMax,
      ],
    },
    cpp: {
      ympe: 74600,
      yampe: 85000,
      ybe: 3500,
      rateBp: 595,
      cpp2RateBp: 400,
      maxMonthlyAt65: 1507.65,
      earlyBpPerMonth: 60,
      lateBpPerMonth: 70,
      maxEarlyBp: 6400,
      maxLateBp: 14200,
      sources: [SRC.cppRates, SRC.cpp2Rates, SRC.esdcQuarter2026, SRC.cppWhenStart],
    },
    oas: {
      monthly65To74: 762.5,
      monthly75Plus: 838.75,
      recoveryThreshold: 95323,
      recoveryRateBp: 1500,
      deferralBpPerMonth: 60,
      maxDeferralBp: 13600,
      sources: [SRC.oasPayments, SRC.oasWhenStart, SRC.oasRecovery, SRC.esdcQuarter2026],
    },
  },
  2025: {
    year: 2025,
    federal: {
      brackets: [
        { rateBp: 1450, upTo: 57375 },
        { rateBp: 2050, upTo: 114750 },
        { rateBp: 2600, upTo: 177882 },
        { rateBp: 2900, upTo: 253414 },
        { rateBp: 3300, upTo: null },
      ],
      bpa: { max: 16129, min: 14538, phaseOutFrom: 177882, phaseOutTo: 253414 },
      sources: FEDERAL_SOURCES_2025,
    },
    provinces: {
      ON: {
        brackets: [
          { rateBp: 505, upTo: 52886 },
          { rateBp: 915, upTo: 105775 },
          { rateBp: 1116, upTo: 150000 },
          { rateBp: 1216, upTo: 220000 },
          { rateBp: 1316, upTo: null },
        ],
        bpa: { max: 12747, min: 12747 },
        surtax: [
          { over: 5710, rateBp: 2000 },
          { over: 7307, rateBp: 3600 },
        ],
        healthPremium: ON_HEALTH_PREMIUM,
        taxReduction: { kind: "on-personal-amounts", personalAmount: 294 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("on"), SRC.td1Forms2025],
      },
      BC: {
        brackets: [
          { rateBp: 506, upTo: 49279 },
          { rateBp: 770, upTo: 98560 },
          { rateBp: 1050, upTo: 113158 },
          { rateBp: 1229, upTo: 137407 },
          { rateBp: 1470, upTo: 186306 },
          { rateBp: 1680, upTo: 259829 },
          { rateBp: 2050, upTo: null },
        ],
        bpa: { max: 12932, min: 12932 },
        taxReduction: {
          kind: "bc-phaseout",
          max: 562,
          fullUpTo: 25020,
          rateBp: 356,
          nilAt: 40807,
        },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("bc"), SRC.td1Forms2025],
      },
      AB: {
        brackets: [
          { rateBp: 800, upTo: 60000 },
          { rateBp: 1000, upTo: 151234 },
          { rateBp: 1200, upTo: 181481 },
          { rateBp: 1300, upTo: 241974 },
          { rateBp: 1400, upTo: 362961 },
          { rateBp: 1500, upTo: null },
        ],
        bpa: { max: 22323, min: 22323 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("ab"), SRC.td1Forms2025],
      },
      QC: {
        brackets: [
          { rateBp: 1400, upTo: 53255 },
          { rateBp: 1900, upTo: 106495 },
          { rateBp: 2400, upTo: 129590 },
          { rateBp: 2575, upTo: null },
        ],
        bpa: { max: 18571, min: 18571 },
        federalAbatementBp: 1650,
        sources: [SRC.qcRates, SRC.qcBpa2025, SRC.t4032Jul2025("qc")],
      },
      SK: {
        brackets: [
          { rateBp: 1050, upTo: 53463 },
          { rateBp: 1250, upTo: 152750 },
          { rateBp: 1450, upTo: null },
        ],
        bpa: { max: 19491, min: 19491 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("sk"), SRC.td1Forms2025],
      },
      MB: {
        brackets: [
          { rateBp: 1080, upTo: 47000 },
          { rateBp: 1275, upTo: 100000 },
          { rateBp: 1740, upTo: null },
        ],
        bpa: { max: 15780, min: 15780 },
        sources: [SRC.t4032Jul2025("mb"), SRC.mbFinance, SRC.td1Forms2025],
      },
      NB: {
        brackets: [
          { rateBp: 940, upTo: 51306 },
          { rateBp: 1400, upTo: 102614 },
          { rateBp: 1600, upTo: 190060 },
          { rateBp: 1950, upTo: null },
        ],
        bpa: { max: 13396, min: 13396 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("nb"), SRC.td1Forms2025],
      },
      NS: {
        brackets: [
          { rateBp: 879, upTo: 30507 },
          { rateBp: 1495, upTo: 61015 },
          { rateBp: 1667, upTo: 95883 },
          { rateBp: 1750, upTo: 154650 },
          { rateBp: 2100, upTo: null },
        ],
        bpa: { max: 11744, min: 11744 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("ns"), SRC.td1Forms2025],
      },
      PE: {
        brackets: [
          { rateBp: 950, upTo: 33328 },
          { rateBp: 1347, upTo: 64656 },
          { rateBp: 1660, upTo: 105000 },
          { rateBp: 1762, upTo: 140000 },
          { rateBp: 1900, upTo: null },
        ],
        bpa: { max: 14650, min: 14650 },
        sources: [SRC.fedRates2025, SRC.peiTaxes, SRC.td1Forms2025],
      },
      NL: {
        brackets: [
          { rateBp: 870, upTo: 44192 },
          { rateBp: 1450, upTo: 88382 },
          { rateBp: 1580, upTo: 157792 },
          { rateBp: 1780, upTo: 220910 },
          { rateBp: 1980, upTo: 282214 },
          { rateBp: 2080, upTo: 564429 },
          { rateBp: 2130, upTo: 1128858 },
          { rateBp: 2180, upTo: null },
        ],
        bpa: { max: 11067, min: 11067 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("nl"), SRC.td1Forms2025],
      },
      YT: {
        brackets: [
          { rateBp: 640, upTo: 57375 },
          { rateBp: 900, upTo: 114750 },
          { rateBp: 1090, upTo: 177882 },
          { rateBp: 1280, upTo: 500000 },
          { rateBp: 1500, upTo: null },
        ],
        // Yukon mirrors the federal amounts, partial claim included
        // (TD1YT-25: "If your income from all sources will be greater than
        // $177,882 you have the option to calculate a partial claim").
        bpa: { max: 16129, min: 14538, phaseOutFrom: 177882, phaseOutTo: 253414 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("yt"), SRC.td1Forms2025],
      },
      NT: {
        brackets: [
          { rateBp: 590, upTo: 51964 },
          { rateBp: 860, upTo: 103930 },
          { rateBp: 1220, upTo: 168967 },
          { rateBp: 1405, upTo: null },
        ],
        bpa: { max: 17842, min: 17842 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("nt"), SRC.td1Forms2025],
      },
      NU: {
        brackets: [
          { rateBp: 400, upTo: 54707 },
          { rateBp: 700, upTo: 109413 },
          { rateBp: 900, upTo: 177881 },
          { rateBp: 1150, upTo: null },
        ],
        bpa: { max: 19274, min: 19274 },
        sources: [SRC.fedRates2025, SRC.t4032Jul2025("nu"), SRC.td1Forms2025],
      },
    },
    registered: {
      rrspDollarLimit: 32490,
      tfsaAnnualLimit: 7000,
      fhsaAnnualLimit: 8000,
      fhsaLifetimeLimit: 40000,
      resp: {
        lifetimeContributionLimit: 50000,
        cesgAnnualBasicMax: 500,
        cesgLifetimeMax: 7200,
        cesgBasicBase: 2500,
        cesgCatchupBase: 5000,
        additionalCesgLowThreshold: 57375,
        additionalCesgHighThreshold: 114750,
        clbThresholds: [
          { limit: 57375, inclusive: true },
          { limit: 64733, inclusive: false },
          { limit: 72123, inclusive: false },
        ],
        sources: [SRC.rrspsLimits, SRC.cesg, SRC.cesgEstimate, SRC.clb, SRC.respContrib],
      },
      lira: {
        unlockAge: 55,
        unlockFracBp: 4000,
        sources: [SRC.onReg909, SRC.fsraSmallBalance, SRC.cppRates],
      },
      sources: [
        SRC.rrspsLimits,
        SRC.rrspDeductionLimit,
        SRC.rrspOverContribution,
        SRC.tfsaRoom,
        SRC.tfsaBefore,
        SRC.fhsaContrib,
        SRC.rrifMinimum,
        SRC.itr7308,
        SRC.onReg909,
        SRC.fsraLifMax,
      ],
    },
    cpp: {
      ympe: 71300,
      yampe: 81200,
      ybe: 3500,
      rateBp: 595,
      cpp2RateBp: 400,
      maxMonthlyAt65: 1433.0,
      earlyBpPerMonth: 60,
      lateBpPerMonth: 70,
      maxEarlyBp: 6400,
      maxLateBp: 14200,
      sources: [SRC.cppRates, SRC.cpp2Rates, SRC.esdcQuarter2025, SRC.cppWhenStart],
    },
    oas: {
      monthly65To74: 740.09,
      monthly75Plus: 814.1,
      recoveryThreshold: 93454,
      recoveryRateBp: 1500,
      deferralBpPerMonth: 60,
      maxDeferralBp: 13600,
      sources: [SRC.oasPayments, SRC.oasWhenStart, SRC.oasRecovery, SRC.esdcQuarter2025],
    },
  },
};

/** Newest tax year with published tables. */
export const LATEST_TAX_YEAR: number = Math.max(...Object.keys(TAX_YEARS).map(Number));

/**
 * Resolve a tax year to tables. An unpublished year NEVER silently pretends
 * to be exact: callers get the latest published tables plus
 * `usedFallback: true`, so an estimate can say so (the retirement simulator
 * runs decades past the last published year). Registered-account ROOM
 * functions use requirePublishedYear instead — room cannot be estimated
 * from stale limits, so they throw.
 */
export function resolveTaxYear(year: number): {
  readonly tables: TaxYearTables;
  readonly usedFallback: boolean;
} {
  const exact = TAX_YEARS[year];
  if (exact) return { tables: exact, usedFallback: false };
  return { tables: TAX_YEARS[LATEST_TAX_YEAR], usedFallback: true };
}
