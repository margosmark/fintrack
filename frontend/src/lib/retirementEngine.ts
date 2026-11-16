// docs/57 §2 — retirement Monte Carlo, client-side.
//
// A seeded drawdown simulation over the user's REAL starting portfolio and
// spending (both already fetched by the page from reports/net-worth and
// reports/cashflow — this module fetches nothing, reads no clock, and touches
// no key material; `baseYear` is injected by the caller). Pure math: the tests
// pin the order of operations (grow → flow → ruin), the RNG stream and the
// percentile method, so the same seed reproduces the same result.

import type { TaxContext, TaxEngine } from "./taxEngine";

export interface RetirementInputs {
  /** Today's net worth (CAD, >= 0) — the step-0 value of every path. */
  startingPortfolio: number;
  /** Integer age today, 18..90. */
  currentAge: number;
  /** Integer age withdrawals begin, currentAge+1..94 (PLAN_END_AGE − 1). */
  retirementAge: number;
  /** CAD/month added during the accumulation phase, >= 0. */
  monthlyContribution: number;
  /** CAD/year of NET spending during retirement, >= 0. */
  annualWithdrawal: number;
  /** Annual expected return, % (-50..50). */
  expectedReturnPct: number;
  /** Annual volatility (sigma), % (0..100). */
  volatilityPct: number;
  /** Paths to simulate, integer 100..10000 (default 2000). */
  scenarios: number;
  /** RNG seed, integer 0..2147483647 (default 42). */
  seed: number;
  /** Calendar year of step 1 — the page supplies it; the engine reads no clock. */
  baseYear: number;
  /** The tax seam (docs/57 §3); the page passes the flat-rate stub in v1. */
  tax: TaxEngine;
  /**
   * Per-step net cash-flow deltas from life events (docs/58 §3): entry `i − 1`
   * applies at step `i` (i = 1..years), AFTER the phase flow and BEFORE the
   * ruin check. Optional — absent means the event-free plan (byte-identical
   * to M188). Validated for length + finiteness by validateRetirementInputs.
   */
  eventFlow?: number[];
}

export interface RetirementOptions {
  /** Test seam: replaces the seeded Gaussian draw (docs/57 named test R3). */
  drawNormal?: () => number;
}

export interface RetirementResult {
  scenarios: number;
  seed: number;
  /** Paths that never hit 0. */
  survived: number;
  /** 100 * survived / scenarios, rounded to 1 dp. */
  successPct: number;
  /** currentAge .. PLAN_END_AGE inclusive (years + 1 entries). */
  ages: number[];
  /** Bands aligned to `ages`, rounded to cents; step 0 is the starting value. */
  median: number[];
  p10: number[];
  p90: number[];
}

/** Every simulation plans to this age (docs/57 §2). */
export const PLAN_END_AGE = 95;
/** Default path count (docs/57 §4). */
export const DEFAULT_SCENARIOS = 2000;
/** Default seed — same inputs, same answer (docs/57 §4). */
export const DEFAULT_SEED = 42;
/** Assumption edits commit to the sim this long after the last keystroke. */
export const SIM_DEBOUNCE_MS = 300;

/** A value outside docs/57 §2's bounds — the page clamps first; this is the contract. */
export class RetirementInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RetirementInputError";
  }
}

function requireInt(name: string, value: number, min: number, max: number): void {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new RetirementInputError(`${name} must be an integer in ${min}..${max} (got ${value})`);
  }
}

function requireRange(name: string, value: number, min: number, max: number): void {
  if (!Number.isFinite(value) || value < min || value > max) {
    throw new RetirementInputError(`${name} must be a number in ${min}..${max} (got ${value})`);
  }
}

/** docs/57 §2 bounds — throws naming the offending field. */
export function validateRetirementInputs(inputs: RetirementInputs): void {
  requireRange("startingPortfolio", inputs.startingPortfolio, 0, Number.MAX_VALUE);
  requireInt("currentAge", inputs.currentAge, 18, 90);
  // Retirement must start after today and leave at least one plan year before
  // PLAN_END_AGE — so 94 is the ceiling, not 90 (docs/57 §2).
  requireInt("retirementAge", inputs.retirementAge, inputs.currentAge + 1, PLAN_END_AGE - 1);
  requireRange("monthlyContribution", inputs.monthlyContribution, 0, Number.MAX_VALUE);
  requireRange("annualWithdrawal", inputs.annualWithdrawal, 0, Number.MAX_VALUE);
  requireRange("expectedReturnPct", inputs.expectedReturnPct, -50, 50);
  requireRange("volatilityPct", inputs.volatilityPct, 0, 100);
  requireInt("scenarios", inputs.scenarios, 100, 10000);
  requireInt("seed", inputs.seed, 0, 2147483647);
  requireInt("baseYear", inputs.baseYear, 1900, 2200);
  if (typeof inputs.tax?.grossWithdrawalFor !== "function") {
    // docs/57 §3: the seam contract — the error text itself stays free of
    // docs/NN references (tests/jargonLeaks.test.tsx bans them in strings).
    throw new RetirementInputError("tax must be a TaxEngine with grossWithdrawalFor");
  }
  if (inputs.eventFlow !== undefined) {
    // docs/58 §3: one delta per step (i = 1..years → index i − 1), finite —
    // the page builds it from validated life events, this is the contract.
    const years = PLAN_END_AGE - inputs.currentAge;
    if (inputs.eventFlow.length !== years) {
      throw new RetirementInputError(
        `eventFlow must have ${years} entries (got ${inputs.eventFlow.length})`,
      );
    }
    for (let i = 0; i < inputs.eventFlow.length; i += 1) {
      if (!Number.isFinite(inputs.eventFlow[i])) {
        throw new RetirementInputError(
          `eventFlow[${i}] must be a finite number (got ${inputs.eventFlow[i]})`,
        );
      }
    }
  }
}

/** mulberry32 — 32-bit state, uniform [0,1). Deterministic for a given seed. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Box–Muller with a clamped u1 (no redraw — the stream consumption stays fixed). */
function gaussian(rng: () => number): number {
  const u1 = Math.max(rng(), 1e-12);
  const u2 = rng();
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

/**
 * R-7 quantile (linear interpolation): `h = (n-1)*q`. `sorted` must be
 * ascending; endpoints are inclusive. Named test R4 pins this exactly.
 */
export function quantile(sorted: ArrayLike<number>, q: number): number {
  const n = sorted.length;
  if (n === 0) throw new RetirementInputError("quantile needs a non-empty series");
  if (!Number.isFinite(q) || q < 0 || q > 1) {
    throw new RetirementInputError(`quantile q must be in 0..1 (got ${q})`);
  }
  if (n === 1) return sorted[0];
  const h = (n - 1) * q;
  const lo = Math.floor(h);
  const hi = Math.min(lo + 1, n - 1);
  return sorted[lo] + (h - lo) * (sorted[hi] - sorted[lo]);
}

/**
 * Run the simulation (docs/57 §2 — the order below IS the contract):
 *
 *   per step t (age = currentAge + t - 1):
 *     1. GROW   p *= 1 + clamp(mu + sigma*z, -1, +inf)   — a year can't lose more than it holds
 *     2. FLOW   accumulation: p += monthlyContribution * 12
 *               retirement:    gross = tax.grossWithdrawalFor(annualWithdrawal, {age, year})
 *                              p >= gross ? p -= gross : p = 0 (ruined)
 *     3. EVENTS p += eventFlow[t - 1] (when supplied), floor at 0   — docs/58 §3
 *     4. RUIN   p <= 0 after any step ruins the path, STICKY
 *
 * Bands are R-7 percentiles across the scenario column at each step.
 */
export function runRetirementSimulation(
  inputs: RetirementInputs,
  options: RetirementOptions = {},
): RetirementResult {
  validateRetirementInputs(inputs);
  const {
    startingPortfolio,
    currentAge,
    retirementAge,
    monthlyContribution,
    annualWithdrawal,
    expectedReturnPct,
    volatilityPct,
    scenarios,
    seed,
    baseYear,
    tax,
    eventFlow,
  } = inputs;

  const years = PLAN_END_AGE - currentAge;
  const steps = years + 1;
  const mu = expectedReturnPct / 100;
  const sigma = volatilityPct / 100;
  const rng = mulberry32(seed);
  const draw = options.drawNormal ?? ((): number => gaussian(rng));

  const paths = new Float64Array(scenarios * steps);
  let survived = 0;

  for (let s = 0; s < scenarios; s += 1) {
    let p = startingPortfolio;
    let ruined = false;
    paths[s * steps] = p;
    for (let t = 1; t <= years; t += 1) {
      const age = currentAge + (t - 1);
      let r = mu + sigma * draw();
      if (r < -1) r = -1;
      p *= 1 + r;
      if (p < 0) p = 0;
      if (age < retirementAge) {
        p += monthlyContribution * 12;
      } else {
        const ctx: TaxContext = { age, year: baseYear + t };
        const gross = tax.grossWithdrawalFor(annualWithdrawal, ctx);
        if (p >= gross) {
          p -= gross;
        } else {
          p = 0;
          ruined = true;
        }
      }
      // docs/58 §3: life-event cash deltas land between the phase flow and
      // the ruin check — an outflow may empty the portfolio but never push a
      // path below zero into the bands (floor at 0, then the sticky rule).
      if (eventFlow !== undefined) {
        p += eventFlow[t - 1];
        if (p < 0) p = 0;
      }
      if (p <= 0) ruined = true;
      paths[s * steps + t] = p;
    }
    if (!ruined) survived += 1;
  }

  const round2 = (v: number): number => Math.round(v * 100) / 100;
  const ages: number[] = [];
  const median: number[] = [];
  const p10: number[] = [];
  const p90: number[] = [];
  const column = new Float64Array(scenarios);
  for (let t = 0; t <= years; t += 1) {
    for (let s = 0; s < scenarios; s += 1) column[s] = paths[s * steps + t];
    column.sort();
    ages.push(currentAge + t);
    p10.push(round2(quantile(column, 0.1)));
    median.push(round2(quantile(column, 0.5)));
    p90.push(round2(quantile(column, 0.9)));
  }

  return {
    scenarios,
    seed,
    survived,
    successPct: Math.round((100 * survived) / scenarios * 10) / 10,
    ages,
    median,
    p10,
    p90,
  };
}
