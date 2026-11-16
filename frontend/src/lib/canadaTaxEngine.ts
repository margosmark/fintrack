/**
 * Canadian tax engine v1 — pure functions over the versioned tables in
 * canadaTaxTables.ts (docs/58-canada-tax-engine.md). Scope: federal +
 * provincial/territorial income tax (brackets, basic personal amounts,
 * Ontario surtax/health premium/low-income reduction, British Columbia tax
 * reduction, Quebec federal abatement), registered accounts (RRSP, TFSA,
 * FHSA, RESP/CESG/CLB, RRIF, LIFA/LIRA limits, spousal RRSP attribution)
 * and CPP/OAS timing. No corporate tax, dividends or RDTOH (v2).
 *
 * Model, stated once so tests can pin it: income tax credits are the BASIC
 * PERSONAL AMOUNTS only — no CPP/EI/age/spouse/employment credits (v1
 * limitation, documented in docs/58; tests cross-check against CRA's
 * T4032 worked example by exactly the missing-credit delta).
 *
 * Precision: money enters as dollars, converts immediately to integer
 * cents, and every intermediate is an integer — rates are basis-point
 * numerators divided once per line with HALF_UP rounding (the float
 * representation of k + 0.5 is exact at these magnitudes, so HALF_UP is
 * exact, not approximate). Inputs above $100,000,000 are refused rather
 * than risk a 2^53 overflow.
 */

import {
  LATEST_TAX_YEAR,
  RRIF_FACTORS_BP,
  TFSA_ANNUAL_LIMITS,
  resolveTaxYear,
  type BasicPersonalAmount,
  type Bracket,
  type HealthPremiumBand,
  type ProvinceCode,
  type ProvinceTables,
  type TaxYearTables,
} from "./canadaTaxTables";

/** Domain failure on the tax engine's inputs — carries a machine-readable
 * `code` in the house {detail, code} spirit (docs/01 §errors). */
export class CanadaTaxInputError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "CanadaTaxInputError";
    this.code = code;
  }
}

const MAX_DOLLARS = 100_000_000;

/** Dollars → integer cents, validating the domain. */
function toCents(dollars: number, field: string): number {
  if (!Number.isFinite(dollars)) {
    throw new CanadaTaxInputError("invalid_tax_input", `${field} must be a finite number`);
  }
  if (dollars < 0) {
    throw new CanadaTaxInputError("negative_amount", `${field} must not be negative (got ${dollars})`);
  }
  if (dollars > MAX_DOLLARS) {
    throw new CanadaTaxInputError(
      "input_too_large",
      `${field} exceeds the engine limit of $${MAX_DOLLARS.toLocaleString("en-CA")}`,
    );
  }
  return Math.round(dollars * 100);
}

/** HALF_UP division of an integer numerator (exact at our magnitudes). */
function roundDiv(numerator: number, denominator: number): number {
  const q = numerator / denominator;
  return q >= 0 ? Math.floor(q + 0.5) : Math.ceil(q - 0.5);
}

function toDollars(cents: number): number {
  return cents / 100;
}

/** Tables for a PUBLISHED year only — registered-account room cannot be
 * extrapolated from stale limits, so an unpublished year throws instead of
 * estimating (income tax and CPP/OAS estimates degrade differently; see
 * resolveTaxYear and the per-function docs). */
function publishedTables(year: number | undefined): TaxYearTables {
  const y = year ?? LATEST_TAX_YEAR;
  const { tables, usedFallback } = resolveTaxYear(y);
  if (usedFallback) {
    throw new CanadaTaxInputError(
      "tax_year_not_published",
      `no published tables for tax year ${y} (latest published: ${LATEST_TAX_YEAR})`,
    );
  }
  return tables;
}

function requireProvince(province: ProvinceCode | undefined): ProvinceCode {
  if (!province) {
    throw new CanadaTaxInputError("province_required", "province is required (e.g. \"ON\")");
  }
  return province;
}

// ---------------------------------------------------------------------------
// Income tax core (integer cents)
// ---------------------------------------------------------------------------

/** Progressive bracket tax: Σ portion × rateBp, quantized once, HALF_UP. */
function bracketTaxCents(incomeCents: number, brackets: readonly Bracket[]): number {
  let numerator = 0;
  let prevTopCents = 0;
  for (const bracket of brackets) {
    const topCents = bracket.upTo === null ? Number.POSITIVE_INFINITY : bracket.upTo * 100;
    const portion = Math.min(incomeCents, topCents) - prevTopCents;
    if (portion > 0) numerator += portion * bracket.rateBp;
    prevTopCents = topCents;
    if (incomeCents <= topCents) break;
  }
  return roundDiv(numerator, 10000);
}

/** Basic personal amount in cents at this net income — linear phase-out
 * where the jurisdiction publishes one (federal + Yukon TD1-WS partial
 * claim), flat otherwise. */
function bpaCents(netCents: number, bpa: BasicPersonalAmount): number {
  const maxCents = bpa.max * 100;
  if (bpa.phaseOutFrom === undefined || bpa.phaseOutTo === undefined) return maxCents;
  const fromCents = bpa.phaseOutFrom * 100;
  const toCents = bpa.phaseOutTo * 100;
  if (netCents <= fromCents) return maxCents;
  if (netCents >= toCents) return bpa.min * 100;
  const spanCents = toCents - fromCents;
  return bpa.min * 100 + roundDiv((maxCents - bpa.min * 100) * (toCents - netCents), spanCents);
}

/** Non-refundable credit = BPA × the jurisdiction's lowest rate (bp). */
function bpaCreditCents(netCents: number, bpa: BasicPersonalAmount, lowestRateBp: number): number {
  return roundDiv(bpaCents(netCents, bpa) * lowestRateBp, 10000);
}

/** Ontario health premium on taxable income (T4032-ON schedule). */
function healthPremiumCents(incomeCents: number, bands: readonly HealthPremiumBand[]): number {
  let band = bands[0];
  for (const candidate of bands) {
    if (incomeCents > candidate.from * 100) band = candidate;
  }
  if (band.cap === 0) return 0;
  const fromCents = band.from * 100;
  const raw = band.base * 100 + ((incomeCents - fromCents) * band.rateBp) / 10000;
  return Math.round(Math.min(band.cap * 100, raw));
}

interface ProvincialResult {
  /** Brackets − BPA credit, floored at 0 — the line T4032-ON calls
   * "provincial tax payable" before surtax/premium/reduction. */
  readonly basicCents: number;
  readonly totalCents: number;
}

function provincialTaxCents(
  taxableCents: number,
  netCents: number,
  prov: ProvinceTables,
): ProvincialResult {
  const gross = bracketTaxCents(taxableCents, prov.brackets);
  const credit = bpaCreditCents(netCents, prov.bpa, prov.brackets[0].rateBp);
  const basicCents = Math.max(0, gross - credit);

  let totalCents = basicCents;
  if (prov.surtax) {
    for (const band of prov.surtax) {
      const overCents = band.over * 100;
      if (basicCents > overCents) {
        totalCents += roundDiv((basicCents - overCents) * band.rateBp, 10000);
      }
    }
  }
  if (prov.healthPremium) {
    totalCents += healthPremiumCents(taxableCents, prov.healthPremium);
  }
  if (prov.taxReduction?.kind === "on-personal-amounts") {
    // T4032-ON: "The reduction is equal to twice the individual's personal
    // amounts minus the provincial tax payable before reduction. The
    // reduction cannot be more than the provincial tax payable before
    // reduction. The reduction is nil when the provincial tax payable
    // before reduction is more than twice the personal amounts." The
    // comparison amount is line 22 (brackets − credits), not the premium.
    const personalCents = prov.taxReduction.personalAmount * 100;
    const reduction = Math.max(0, Math.min(basicCents, 2 * personalCents - basicCents));
    totalCents -= reduction;
  } else if (prov.taxReduction?.kind === "bc-phaseout") {
    const reductionTable = prov.taxReduction;
    const fullUpToCents = reductionTable.fullUpTo * 100;
    let reduction = reductionTable.max * 100;
    if (taxableCents > fullUpToCents) {
      reduction = Math.max(
        0,
        reductionTable.max * 100 -
          roundDiv((taxableCents - fullUpToCents) * reductionTable.rateBp, 10000),
      );
    }
    totalCents -= Math.min(reduction, totalCents);
  }
  return { basicCents, totalCents: Math.max(0, totalCents) };
}

interface FederalResult {
  readonly federalCents: number;
  readonly provincialCents: number;
}

/** Combined tax in cents, Quebec's 16.5% federal abatement applied after
 * the federal credits (T4032-QC: "The Quebec tax abatement rate remains at
 * 16.5%"). */
function taxCents(taxableCents: number, netCents: number, province: ProvinceCode, tables: TaxYearTables): FederalResult {
  const prov = tables.provinces[province];
  if (!prov) {
    throw new CanadaTaxInputError("unknown_province", `unknown province code: ${province}`);
  }
  const grossFederal = bracketTaxCents(taxableCents, tables.federal.brackets);
  const federalCredit = bpaCreditCents(netCents, tables.federal.bpa, tables.federal.brackets[0].rateBp);
  let federalCents = Math.max(0, grossFederal - federalCredit);

  if (prov.federalAbatementBp) {
    federalCents -= roundDiv(federalCents * prov.federalAbatementBp, 10000);
  }
  const { totalCents: provincialCents } = provincialTaxCents(taxableCents, netCents, prov);
  return { federalCents, provincialCents };
}

// ---------------------------------------------------------------------------
// Public income-tax API
// ---------------------------------------------------------------------------

export interface TaxInput {
  /** Taxable income for the year, dollars (line 26000 basis). */
  readonly taxableIncome: number;
  /** Net income (line 23400 basis) for the BPA phase-out and clawback
   * comparisons; defaults to taxableIncome when the caller has no separate
   * figure. */
  readonly netIncome?: number;
  readonly province: ProvinceCode;
  /** Tax year; defaults to LATEST_TAX_YEAR. An unpublished year resolves
   * to the latest tables and sets usedFallbackYear (estimate, flagged). */
  readonly year?: number;
}

export interface TaxBreakdown {
  readonly federal: number;
  readonly provincial: number;
  readonly total: number;
  readonly province: ProvinceCode;
  /** The year the figures were actually computed from (post-fallback). */
  readonly year: number;
  readonly usedFallbackYear: boolean;
}

/**
 * Combined federal + provincial income tax for one year. Pure: same inputs
 * → same cents, no I/O. See the model note at the top of this file
 * (BPA-only credits).
 */
export function incomeTax(input: TaxInput): TaxBreakdown {
  const province = requireProvince(input.province);
  const { tables, usedFallback } = resolveTaxYear(input.year ?? LATEST_TAX_YEAR);
  const taxableCents = toCents(input.taxableIncome, "taxableIncome");
  const netCents =
    input.netIncome === undefined ? taxableCents : toCents(input.netIncome, "netIncome");
  const { federalCents, provincialCents } = taxCents(taxableCents, netCents, province, tables);
  return {
    federal: toDollars(federalCents),
    provincial: toDollars(provincialCents),
    total: toDollars(federalCents + provincialCents),
    province,
    year: tables.year,
    usedFallbackYear: usedFallback,
  };
}

export interface WithdrawalContext {
  /** Projection year (may be past the last published tables — falls back
   * to the latest, flagged in incomeTax). */
  readonly year: number;
  /** Unused in v1's BPA-only credit model; accepted for the retirement
   * simulator's context shape and future CPP/OAS-aware gross-ups. */
  readonly age?: number;
  /** Overrides the engine factory's default province. */
  readonly province?: ProvinceCode;
  /** Other taxable income already on the return (e.g. RRIF/TFSA-adjacent
   * income); the withdrawal is grossed up on TOP of it. */
  readonly otherIncome?: number;
}

/**
 * Smallest whole-dollar-cent gross withdrawal whose after-tax value covers
 * `netSpend`: minimal G ≥ 0 with G − (tax(other + G) − tax(other)) ≥ netSpend.
 * Exact by integer bisection over the cent grid (tax is monotone in income,
 * so the first cent satisfying the inequality is THE answer — not an
 * approximation loop).
 */
export function grossWithdrawalFor(
  netSpend: number,
  ctx: WithdrawalContext & { readonly province: ProvinceCode },
): number {
  const province = requireProvince(ctx.province);
  const { tables } = resolveTaxYear(ctx.year);
  const netCents = toCents(netSpend, "netSpend");
  const otherCents = ctx.otherIncome === undefined ? 0 : toCents(ctx.otherIncome, "otherIncome");

  const taxFor = (grossCents: number): number => {
    const { federalCents, provincialCents } = taxCents(
      otherCents + grossCents,
      otherCents + grossCents,
      province,
      tables,
    );
    return federalCents + provincialCents;
  };
  const otherTax = taxFor(0);
  const netAfter = (grossCents: number): number => grossCents - (taxFor(grossCents) - otherTax);

  if (netAfter(0) >= netCents) return 0;

  let lo = 0;
  let hi = Math.max(netCents, 1);
  while (netAfter(hi) < netCents) {
    hi *= 2;
    if (hi > MAX_DOLLARS * 100) {
      throw new CanadaTaxInputError(
        "input_too_large",
        `gross-up for netSpend ${toDollars(netCents)} exceeds the engine limit`,
      );
    }
  }
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (netAfter(mid) >= netCents) hi = mid;
    else lo = mid + 1;
  }
  return toDollars(lo);
}

export interface CanadaTaxEngineContext {
  readonly year: number;
  readonly age?: number;
  readonly province?: ProvinceCode;
  readonly otherIncome?: number;
}

/** The seam the retirement simulator (docs/57 §3) consumes: id + label +
 * grossWithdrawalFor(net, ctx). Structurally satisfies that doc's TaxEngine
 * without importing it (no ../api or cross-branch dependency). */
export interface CanadaTaxEngine {
  readonly id: "ca-engine";
  readonly label: string;
  grossWithdrawalFor(netSpend: number, ctx: CanadaTaxEngineContext): number;
}

/** Build the simulator seam for one province (default "ON", mirroring the
 * server-side DEFAULT_PROVINCE in docs/27). */
export function caTaxEngine(options?: { readonly province?: ProvinceCode }): CanadaTaxEngine {
  const defaultProvince: ProvinceCode = options?.province ?? "ON";
  return {
    id: "ca-engine",
    label: `Canadian tax — ${defaultProvince}`,
    grossWithdrawalFor(netSpend: number, ctx: CanadaTaxEngineContext): number {
      return grossWithdrawalFor(netSpend, {
        ...ctx,
        province: ctx.province ?? defaultProvince,
      });
    },
  };
}

// ---------------------------------------------------------------------------
// CPP / OAS timing
// ---------------------------------------------------------------------------

/** Maximum CPP retirement pension if starting at `startAge` (60–70), for
 * the year's published age-65 maximum: −0.6%/month before 65 (−36% at 60),
 * +0.7%/month after (+42% at 70). Service Canada publishes only the age-65
 * dollar maximum; 60/70 dollar figures are DERIVED from the published
 * factor rule (docs/58 "derived, not published"). An unpublished `year`
 * freezes at the latest published table (estimate). */
export function cppRetirementMonthly(input: {
  readonly startAge: number;
  readonly year?: number;
}): number {
  if (!Number.isInteger(input.startAge) || input.startAge < 60 || input.startAge > 70) {
    throw new CanadaTaxInputError(
      "cpp_start_age_out_of_range",
      `CPP start age must be an integer in [60, 70] (got ${input.startAge})`,
    );
  }
  const { tables } = resolveTaxYear(input.year ?? LATEST_TAX_YEAR);
  const cpp = tables.cpp;
  const months = Math.abs(input.startAge - 65) * 12;
  const factorBp =
    input.startAge < 65
      ? Math.max(cpp.maxEarlyBp, 10000 - cpp.earlyBpPerMonth * months)
      : Math.min(cpp.maxLateBp, 10000 + cpp.lateBpPerMonth * months);
  return toDollars(roundDiv(Math.round(cpp.maxMonthlyAt65 * 100) * factorBp, 10000));
}

/** OAS at `startAge` (65–70): +0.6%/month deferred (+36% at 70), from the
 * year's Q4 published maximum for ages 65–74. Ages 75+ top-up is not
 * modelled on the start-age ladder (v1 limitation, docs/58). An unpublished
 * `year` freezes at the latest published table. */
export function oasMonthly(input: {
  readonly startAge: number;
  readonly year?: number;
}): number {
  if (!Number.isInteger(input.startAge) || input.startAge < 65 || input.startAge > 70) {
    throw new CanadaTaxInputError(
      "oas_start_age_out_of_range",
      `OAS start age must be an integer in [65, 70] (got ${input.startAge})`,
    );
  }
  const { tables } = resolveTaxYear(input.year ?? LATEST_TAX_YEAR);
  const oas = tables.oas;
  const months = (input.startAge - 65) * 12;
  const factorBp = Math.min(oas.maxDeferralBp, 10000 + oas.deferralBpPerMonth * months);
  return toDollars(roundDiv(Math.round(oas.monthly65To74 * 100) * factorBp, 10000));
}

/** OAS recovery tax (clawback) estimate: 15% of net income over the year's
 * threshold, capped at the OAS actually received. Reported on line 23500;
 * withheld monthly from OAS by Service Canada (docs/58). Unpublished years
 * freeze at the latest published threshold. */
export function oasRecoveryTax(input: {
  readonly netIncome: number;
  /** The annual OAS actually received (oasMonthly × 12 for the start age). */
  readonly annualOas: number;
  readonly year?: number;
}): number {
  const { tables } = resolveTaxYear(input.year ?? LATEST_TAX_YEAR);
  const netCents = toCents(input.netIncome, "netIncome");
  const oasCents = toCents(input.annualOas, "annualOas");
  const thresholdCents = tables.oas.recoveryThreshold * 100;
  if (netCents <= thresholdCents) return 0;
  const recovery = roundDiv((netCents - thresholdCents) * tables.oas.recoveryRateBp, 10000);
  return toDollars(Math.min(recovery, oasCents));
}

// ---------------------------------------------------------------------------
// Registered accounts
// ---------------------------------------------------------------------------

/** RRSP deduction room for `year` = prior unused room + min(18% × earned
 * income, the year's dollar limit) − pension adjustment (CRA deduction-limit
 * formula; PAR/PSPA terms are caller-supplied via priorUnusedRoom and
 * pensionAdjustment). Can be negative when a PSPA exceeded the increment —
 * CRA's limit can go negative; v1 does not floor it (docs/58). Unpublished
 * years throw `tax_year_not_published`. */
export function rrspRoom(input: {
  readonly earnedIncome: number;
  readonly priorUnusedRoom?: number;
  readonly pensionAdjustment?: number;
  readonly year?: number;
}): number {
  const tables = publishedTables(input.year);
  const earnedCents = toCents(input.earnedIncome, "earnedIncome");
  const priorCents = toCents(input.priorUnusedRoom ?? 0, "priorUnusedRoom");
  const paCents = toCents(input.pensionAdjustment ?? 0, "pensionAdjustment");
  const incrementCents = Math.min(
    roundDiv(earnedCents * 1800, 10000),
    tables.registered.rrspDollarLimit * 100,
  );
  return toDollars(priorCents + incrementCents - paCents);
}

/** RRSP over-contribution: `excess` above the deduction limit, the
 * taxable portion above limit + $2,000, and the 1%/month penalty on it
 * (CRA: "you will have an excess contribution … more than your RRSP
 * deduction limit … plus $2,000"; "a tax of 1% per month"). */
export function rrspOverContribution(input: {
  readonly contributions: number;
  readonly deductionLimit: number;
}): {
  readonly excess: number;
  readonly taxableExcess: number;
  readonly penaltyPerMonth: number;
} {
  const contributedCents = toCents(input.contributions, "contributions");
  const limitCents = toCents(input.deductionLimit, "deductionLimit");
  const excessCents = Math.max(0, contributedCents - limitCents);
  const taxableCents = Math.max(0, excessCents - 200000);
  return {
    excess: toDollars(excessCents),
    taxableExcess: toDollars(taxableCents),
    penaltyPerMonth: toDollars(roundDiv(taxableCents * 100, 10000)),
  };
}

/** TFSA contribution room available IN `year`: cumulative limits through
 * that year minus contributions, plus withdrawals (both input totals are
 * measured through the END OF THE PRIOR YEAR — withdrawals restore room on
 * January 1 of the following year). Clamped at 0. Unpublished years throw. */
export function tfsaRoom(input: {
  /** Total TFSA contributions through the end of the prior year. */
  readonly contributions: number;
  /** Total TFSA withdrawals through the end of the prior year. */
  readonly withdrawals: number;
  readonly year?: number;
}): number {
  const year = input.year ?? LATEST_TAX_YEAR;
  if (year < 2009) {
    throw new CanadaTaxInputError(
      "tax_year_not_published",
      `the TFSA did not exist before 2009 (got ${year})`,
    );
  }
  let cumulativeLimitCents = 0;
  for (let y = 2009; y <= year; y += 1) {
    const annual = TFSA_ANNUAL_LIMITS[y];
    if (annual === undefined) {
      throw new CanadaTaxInputError("tax_year_not_published", `no TFSA limits published for ${y}`);
    }
    cumulativeLimitCents += annual * 100;
  }
  const contributedCents = toCents(input.contributions, "contributions");
  const withdrawnCents = toCents(input.withdrawals, "withdrawals");
  return toDollars(Math.max(0, cumulativeLimitCents - contributedCents + withdrawnCents));
}

/** FHSA participation room for `year`: the lesser of (unused annual room +
 * $8,000) and ($40,000 − contributions to date), floored at 0. Unpublished
 * years throw. */
export function fhsaRoom(input: {
  /** Total FHSA contributions (incl. RRSP transfers) through the prior year. */
  readonly contributions: number;
  /** Unused annual participation room carried forward. */
  readonly unusedRoom?: number;
  readonly year?: number;
}): number {
  const tables = publishedTables(input.year);
  const contributedCents = toCents(input.contributions, "contributions");
  const unusedCents = toCents(input.unusedRoom ?? 0, "unusedRoom");
  const annualCents = tables.registered.fhsaAnnualLimit * 100;
  const lifetimeCents = tables.registered.fhsaLifetimeLimit * 100;
  const room = Math.min(annualCents + unusedCents, lifetimeCents - contributedCents);
  return toDollars(Math.max(0, room));
}

/** CESG payable on `contribution` for one beneficiary: basic 20% on the
 * first $2,500 (up to $5,000 of base with carry-forward room) + additional
 * 20%/10%/0 by adjusted family net income on the first $500, capped by the
 * $7,200 lifetime grant. `beneficiaryAge` over 17 earns nothing (grant ends
 * the end of the year the child turns 17). Unpublished years throw. */
export function respCesg(input: {
  /** Personal RESP contribution for the year. */
  readonly contribution: number;
  readonly familyNetIncome: number;
  /** CESG already received for this beneficiary (lifetime tracking). */
  readonly priorCesg?: number;
  /** Unused basic grant room carried forward (catch-up). */
  readonly unusedCesgRoom?: number;
  /** Beneficiary's age in the taxation year (gate at 17). */
  readonly beneficiaryAge?: number;
  readonly year?: number;
}): number {
  const tables = publishedTables(input.year);
  const resp = tables.registered.resp;
  if (input.beneficiaryAge !== undefined && input.beneficiaryAge > 17) return 0;
  const contributionCents = toCents(input.contribution, "contribution");
  const priorCesgCents = toCents(input.priorCesg ?? 0, "priorCesg");
  const unusedRoomCents = toCents(input.unusedCesgRoom ?? 0, "unusedCesgRoom");

  const basicBaseCents = Math.min(
    contributionCents,
    resp.cesgBasicBase * 100 + Math.min(unusedRoomCents, (resp.cesgCatchupBase - resp.cesgBasicBase) * 100),
  );
  const basicCents = roundDiv(basicBaseCents * 2000, 10000);

  const incomeCents = toCents(input.familyNetIncome, "familyNetIncome");
  const additionalRateBp =
    incomeCents < resp.additionalCesgLowThreshold * 100
      ? 2000
      : incomeCents <= resp.additionalCesgHighThreshold * 100
        ? 1000
        : 0;
  const additionalCents = roundDiv(Math.min(contributionCents, 500 * 100) * additionalRateBp, 10000);

  const lifetimeRemaining = resp.cesgLifetimeMax * 100 - priorCesgCents;
  return toDollars(
    Math.max(0, Math.min(basicCents + additionalCents, lifetimeRemaining)),
  );
}

/** CLB payable for one child: $500 first eligible year, $100 after, to a
 * $2,000 lifetime maximum, gated by the adjusted-family-income threshold
 * for the family size (thresholds are the published July–June windows
 * keyed to the tax year — docs/58). No contribution required. The $100
 * continuation stops after age 15; the first-year $500 has no age gate in
 * v1 (the 18/21 claim windows are not modelled). Unpublished years throw. */
export function respClb(input: {
  readonly familyNetIncome: number;
  /** Number of children in the family (1–5; 4 and 5 have higher thresholds). */
  readonly childCount: number;
  /** CLB already received for this child (lifetime tracking). */
  readonly priorClb?: number;
  /** Beneficiary's age in the taxation year (gates the $100 continuation). */
  readonly beneficiaryAge?: number;
  readonly year?: number;
}): number {
  const tables = publishedTables(input.year);
  const resp = tables.registered.resp;
  if (!Number.isInteger(input.childCount) || input.childCount < 1 || input.childCount > 5) {
    throw new CanadaTaxInputError(
      "invalid_child_count",
      `childCount must be an integer in [1, 5] (got ${input.childCount})`,
    );
  }
  const threshold = resp.clbThresholds[input.childCount <= 3 ? 0 : input.childCount === 4 ? 1 : 2];
  const incomeCents = toCents(input.familyNetIncome, "familyNetIncome");
  const thresholdCents = threshold.limit * 100;
  const eligible = threshold.inclusive ? incomeCents <= thresholdCents : incomeCents < thresholdCents;
  if (!eligible) return 0;

  const priorCents = toCents(input.priorClb ?? 0, "priorClb");
  const lifetimeCents = 2000 * 100;
  if (priorCents >= lifetimeCents) return 0;
  let amountCents: number;
  if (priorCents === 0) {
    amountCents = 500 * 100;
  } else if (input.beneficiaryAge !== undefined && input.beneficiaryAge > 15) {
    return 0;
  } else {
    amountCents = 100 * 100;
  }
  return toDollars(Math.min(amountCents, lifetimeCents - priorCents));
}

/** RRIF minimum withdrawal: value × the prescribed factor — 1/(90 − age)
 * under 71, the statutory table (RRIF_FACTORS_BP) from 71, 20% from 95
 * (Income Tax Regulations s. 7308(4) "all other RRIFs"; age at the
 * beginning of the year). */
export function rrifMinimum(input: { readonly age: number; readonly value: number }): number {
  if (!Number.isInteger(input.age) || input.age < 0 || input.age > 130) {
    throw new CanadaTaxInputError("invalid_age", `age must be an integer in [0, 130] (got ${input.age})`);
  }
  const valueCents = toCents(input.value, "value");
  if (input.age < 71) return toDollars(roundDiv(valueCents, 90 - input.age));
  const factorBp = RRIF_FACTORS_BP[Math.min(input.age, 95)] ?? RRIF_FACTORS_BP[95];
  return toDollars(roundDiv(valueCents * factorBp, 10000));
}

/** LIF maximum annual withdrawal (Ontario Reg. 909): the greater of A =
 * prior-year investment earnings and B = C/F, where F is the annuity-due
 * present value of $1/year to the year the owner reaches 90 at the 6% floor
 * rate — B = value × (100 / ä)%, matching FSRA's published 6.00% table to
 * five decimals (65 → 7.25513%, 71 → 8.22496%, 75 → 9.33511%,
 * 80 → 11.96160%, 90 → 100%). The first-15-years bond-rate floor above 6%
 * is not modelled (v1: the FSRA 6% basis, which the published table uses).
 * Note Reg. 909 (4): if the RRIF-factor minimum exceeds this maximum, the
 * minimum must still be paid — callers take max(min, max). */
export function lifMaximumWithdrawal(input: {
  readonly age: number;
  readonly value: number;
  /** A: the fund's prior-fiscal-year investment earnings, if known. */
  readonly priorYearEarnings?: number;
}): number {
  if (!Number.isInteger(input.age) || input.age < 0 || input.age > 130) {
    throw new CanadaTaxInputError("invalid_age", `age must be an integer in [0, 130] (got ${input.age})`);
  }
  const valueCents = toCents(input.value, "value");
  const earningsCents = toCents(input.priorYearEarnings ?? 0, "priorYearEarnings");
  const factorPct5 = lifMaxFactorPct5(input.age);
  if (valueCents > Math.floor(Number.MAX_SAFE_INTEGER / factorPct5)) {
    throw new CanadaTaxInputError(
      "input_too_large",
      `LIF value ${toDollars(valueCents)} is too large for exact factor arithmetic`,
    );
  }
  const bCents = roundDiv(valueCents * factorPct5, 10_000_000);
  return toDollars(Math.max(earningsCents, bCents));
}

/** FSRA's published LIF/LRIF maximum percentage at the 6% basis, scaled by
 * 100,000 (5 decimals of a percent): 100 / annuity-due(91 − age)@6%. */
function lifMaxFactorPct5(age: number): number {
  if (age >= 90) return 100 * 100000;
  const n = 91 - age;
  const v = Math.pow(1.06, -n);
  const annuityDue = (1 - v) / (0.06 / 1.06);
  return Math.round((100 / annuityDue) * 100000);
}

/** Ontario LIRA small-balance unlocking: from `unlockAge` (55), all
 * locked-in money may be withdrawn when total Ontario locked-in assets are
 * under 40% of the year's YMPE (FSRA). A LIRA itself permits no
 * withdrawals at all (Reg. 909 Sch. 3 s.3) — this predicate is about the
 * unlocking exception. Unpublished years throw (YMPE is year-published). */
export function liraSmallBalanceUnlock(input: {
  readonly age: number;
  /** Combined value of ALL of the owner's Ontario locked-in accounts. */
  readonly lockedInTotal: number;
  readonly year?: number;
}): { readonly eligible: boolean; readonly limit: number } {
  const tables = publishedTables(input.year);
  const lira = tables.registered.lira;
  const limitCents = roundDiv(tables.cpp.ympe * 100 * lira.unlockFracBp, 10000);
  const totalCents = toCents(input.lockedInTotal, "lockedInTotal");
  const eligible =
    Number.isInteger(input.age) && input.age >= lira.unlockAge && totalCents < limitCents;
  return { eligible, limit: toDollars(limitCents) };
}

/** Spousal RRSP 3-year attribution: TRUE while a withdrawal would be
 * attributed to the CONTRIBUTOR — i.e. a spousal contribution was made in
 * the withdrawal year or either of the two preceding years
 * (withdrawalYear − lastContributionYear < 3). */
export function spousalRrspAttribution(input: {
  /** Year of the most recent spousal contribution. */
  readonly lastContributionYear: number;
  readonly withdrawalYear: number;
}): boolean {
  return input.withdrawalYear - input.lastContributionYear < 3;
}
