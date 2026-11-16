// projectionEngine — M65 (M61 #2), re-scoped by M157: the offline / E2EE
// FALLBACK for the projection run, not the shipped path.
//
// Exact port of `backend/app/services/projections.py::project_scenario` and
// `backend/app/api/projections.py::run_projections` (docs/12 §7).
//
// History, because the shape of this file is a trap otherwise: the Reports
// tab used to POST first, and in the read-only demo tenant that POST 403ed
// `demo_account_restricted`, so the headline calculator threw `ErrorPanel`
// and drew no chart at all — M61 #2 moved the whole computation into the
// browser to escape a DEMO WRITE GUARD, not an E2EE constraint (M135 §2 K-12
// says so in as many words). M157 closed that gap at the source: the route
// joined `DEMO_MUTATION_EXEMPT_PATHS` (it is pure request-body math — the
// handler takes no `Session`) and `ProjectionsPanel` now calls the server.
//
// This mirror therefore STAYS but is no longer called by the panel: docs/33
// §Server-first rule 5 keeps it as the last-resort path for offline
// rendering, output-identical to the server under the frozen goldens in
// `tests/fixtures/projection-parity.json` (7/7, both directions — the
// server side is pinned by `backend/tests/test_projections.py` and by
// `parity.ts --slice projection`). Retiring it is M160's ruling, not this
// card's.
//
// Numeric core — fixed-point BigInt at 40 FRACTION digits, not doubles.
// AGENTS §2 forbids floats touching money, and a double port measurably
// drifts: iterating `(1+m)^12` in IEEE-754 accumulates ~6e-14 relative error
// over 600 months, which lands a 25-year, $8M projection on the wrong side of
// the half-cent boundary ~10% of the time (measured against the Decimal
// reference — 440 mismatched rows in a 200-case randomized sweep). At 40
// fraction digits the engine is strictly MORE precise than the server's
// 28-significant-digit Decimal context at every magnitude, so the two agree
// to the cent.
//
//   * `m = (1 + r/100) ** (1/12) − 1` is irrational, so it is solved by
//     integer BINARY SEARCH on `(1+m)^12 ≤ y` (12 rounded multiplications per
//     probe) and then tie-broken to the nearer root — correctly rounded to 40
//     digits, with no ln/exp implementation to get wrong.
//   * `contributed = annual × k` and `total_gain = final − start −
//     contributed` stay EXACT (BigInt scaled decimals), so a half-cent tie
//     rounds away from zero exactly like the server's ROUND_HALF_UP —
//     IEEE-754's ties-to-even never gets a vote.
//   * Deliberate single deviation: a gain of exactly zero prints `0.00`,
//     where Python's Decimal carries a signed zero and prints `-0.00`
//     (docs/12 §7).
//
// Input bounds mirror `app/schemas/projections.py` Field constraints, so a
// payload the client accepts is one the server would accept (and vice versa);
// a violation surfaces as `ProjectionInputError` and renders in the panel's
// ErrorPanel instead of a 422 round-trip.

import type {
  ProjectionRequest,
  ProjectionResponse,
  ProjectionScenarioResult,
  ProjectionYearRow,
} from "../api/projections";

/** Same `code` the server's 422 envelope carries (docs/08 error envelope). */
export class ProjectionInputError extends Error {
  readonly code = "validation_error";

  constructor(message: string) {
    super(message);
    this.name = "ProjectionInputError";
  }
}

// --- exact scaled-decimal helpers (BigInt) ---------------------------------

interface Scaled {
  v: bigint;
  s: number;
}

const NUMERIC_RE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

/** Fraction digits carried by every intermediate. 40 > 28 (the server's
 *  Decimal context precision) + the 15 integer digits of any real balance. */
const SCALE = 40;
const ONE = 10n ** BigInt(SCALE);
/** `10n ** BigInt(SCALE - 2)` — scale factor between fraction digits and cents. */
const CENTS_DIVISOR = 10n ** BigInt(SCALE - 2);

/** Integer division, ties away from zero — `ROUND_HALF_UP` (the rounding mode
 *  `_cents` uses on the server; intermediate ties at 1e-40 are unreachable). */
function divRound(numerator: bigint, denominator: bigint): bigint {
  let n = numerator;
  let d = denominator;
  if (d < 0n) {
    n = -n;
    d = -d;
  }
  const negative = n < 0n;
  const magnitude = negative ? -n : n;
  const quotient = magnitude / d;
  const remainder = magnitude % d;
  const rounded = remainder * 2n >= d ? quotient + 1n : quotient;
  return negative ? -rounded : rounded;
}

/** Parse a numeric input string EXACTLY as `v × 10^-s` (no float step). */
function exact(raw: string): Scaled {
  const trimmed = raw.trim();
  if (!NUMERIC_RE.test(trimmed)) {
    throw new ProjectionInputError(`"${raw}" is not a number.`);
  }
  const negative = trimmed.startsWith("-");
  const body = negative ? trimmed.slice(1) : trimmed;
  const dot = body.indexOf(".");
  const intPart = dot === -1 ? body : body.slice(0, dot);
  const fracPart = dot === -1 ? "" : body.slice(dot + 1);
  const digits = `${intPart || "0"}${fracPart}`;
  return { v: negative ? -BigInt(digits) : BigInt(digits), s: fracPart.length };
}

/** Move a scaled decimal onto the engine's fixed-point scale (ROUND_HALF_UP). */
function toFixedPoint(value: Scaled, scale = SCALE): bigint {
  if (value.s <= scale) return value.v * 10n ** BigInt(scale - value.s);
  return divRound(value.v, 10n ** BigInt(value.s - scale));
}

/** Round a fixed-point value to whole cents (ROUND_HALF_UP, ties away). */
function fixedToCents(value: bigint): bigint {
  return divRound(value, CENTS_DIVISOR);
}

/** Round a scaled decimal to whole cents (ROUND_HALF_UP, ties away). */
function toCents(value: Scaled): bigint {
  if (value.s <= 2) return value.v * 10n ** BigInt(2 - value.s);
  return divRound(value.v, 10n ** BigInt(value.s - 2));
}

function addScaled(a: Scaled, b: Scaled): Scaled {
  const s = Math.max(a.s, b.s);
  const av = a.s === s ? a.v : a.v * 10n ** BigInt(s - a.s);
  const bv = b.s === s ? b.v : b.v * 10n ** BigInt(s - b.s);
  return { v: av + bv, s };
}

function negScaled(a: Scaled): Scaled {
  return { v: -a.v, s: a.s };
}

/** `"123.45"` for 12345n cents — the Decimal-as-string wire format. */
function centsToString(cents: bigint): string {
  const negative = cents < 0n;
  const digits = (negative ? -cents : cents).toString().padStart(3, "0");
  return `${negative ? "-" : ""}${digits.slice(0, -2)}.${digits.slice(-2)}`;
}

// --- fixed-point arithmetic -------------------------------------------------

/** `a × b` at the engine scale (rounded). */
function mulF(a: bigint, b: bigint): bigint {
  return divRound(a * b, ONE);
}

/** `a ÷ b` where BOTH operands are fixed-point values (result is too). */
function divF(a: bigint, b: bigint): bigint {
  return divRound(a * ONE, b);
}

/** `base ** 12` at the engine scale — 12 rounded multiplications. */
function pow12(base: bigint): bigint {
  let acc = ONE;
  for (let i = 0; i < 12; i += 1) acc = mulF(acc, base);
  return acc;
}

/**
 * `(1 + r/100) ** (1/12)` as the RATE `m = root − 1`, correctly rounded to the
 * engine scale. `(1+m)^12` is strictly increasing for `m > −1`, so binary
 * search over the bracket `[−1, max(y, 1)]` (134 probes at scale 40) is
 * exact; the two roots either side are then compared against `y` so an
 * in-between value rounds to the nearer one.
 */
function monthlyRate(y: bigint): bigint {
  let lo = -ONE + 1n; // (1 + m) > 0 keeps pow12 defined
  let hi = y > ONE ? y : ONE; // pow12(hi) ≥ y holds for both branches
  while (hi - lo > 1n) {
    const mid = lo + (hi - lo) / 2n;
    if (pow12(ONE + mid) <= y) lo = mid;
    else hi = mid;
  }
  // Tie-break: pick whichever of `lo`/`hi` lands closer to `y`.
  const lowPow = pow12(ONE + lo);
  const highPow = pow12(ONE + hi);
  return y - lowPow <= highPow - y ? lo : hi; // m itself: base = 1 + m
}

// --- validation (mirrors app/schemas/projections.py) -----------------------

function requireNumber(raw: string, field: string): number {
  const trimmed = raw.trim();
  if (!NUMERIC_RE.test(trimmed)) throw new ProjectionInputError(`${field} must be a number.`);
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) throw new ProjectionInputError(`${field} must be a number.`);
  return parsed;
}

function requireRange(value: number, min: number, max: number, field: string): void {
  if (value < min || value > max) {
    throw new ProjectionInputError(`${field} must be between ${min} and ${max}.`);
  }
}

/** 422-equivalent bounds check; throws `ProjectionInputError` (docs/12 §7). */
export function validateProjectionRequest(payload: ProjectionRequest): void {
  const start = requireNumber(payload.start_value_cad, "Start value");
  if (start < 0) throw new ProjectionInputError("Start value must be 0 or more.");
  const contribution = requireNumber(payload.annual_contribution_cad, "Annual contribution");
  if (contribution < 0) throw new ProjectionInputError("Annual contribution must be 0 or more.");
  if (!Number.isInteger(payload.years)) {
    throw new ProjectionInputError("Years must be a whole number.");
  }
  requireRange(payload.years, 1, 50, "Years");
  requireRange(requireNumber(payload.inflation_pct, "Inflation"), 0, 20, "Inflation");

  if (payload.scenarios.length < 1 || payload.scenarios.length > 8) {
    throw new ProjectionInputError("Run between 1 and 8 scenarios.");
  }
  for (const scenario of payload.scenarios) {
    if (scenario.label.length < 1 || scenario.label.length > 40) {
      throw new ProjectionInputError("A scenario label must be 1–40 characters.");
    }
    requireRange(requireNumber(scenario.annual_return_pct, "Annual return"), -50, 50, "Annual return");
  }
}

// --- the run ----------------------------------------------------------------

/** `1 + inflation/100` as an exact scaled decimal. */
function deflatorBase(inflation: Scaled): Scaled {
  return { v: 10n ** BigInt(inflation.s + 2) + inflation.v, s: inflation.s + 2 };
}

/** `(1 + inflation/100) ** years` at the engine scale (docs/12 §7). */
function deflator(inflation: Scaled, years: number): bigint {
  const base = toFixedPoint(deflatorBase(inflation));
  let acc = ONE;
  for (let i = 0; i < years; i += 1) acc = mulF(acc, base);
  return acc;
}

/**
 * One scenario: monthly compounding, growth FIRST then the month-end
 * contribution (`value = value × (1 + monthly_rate); value += annual/12`),
 * one row per year k = 1..years (docs/12 §7).
 */
function projectScenario(
  startValue: Scaled,
  annualContribution: Scaled,
  annualReturnPct: Scaled,
  years: number,
  inflation: Scaled,
): ProjectionYearRow[] {
  // y = 1 + r/100, exact; m solves (1 + m)^12 = y.
  const y = toFixedPoint(deflatorBase(annualReturnPct));
  const rate = monthlyRate(y);
  const onePlusRate = ONE + rate;
  // annual / 12 as a fixed-point ÷ integer (Python: `annual_contribution / 12`).
  const monthlyContribution = divRound(toFixedPoint(annualContribution), 12n);

  let value = toFixedPoint(startValue);
  const rows: ProjectionYearRow[] = [];

  for (let year = 1; year <= years; year += 1) {
    for (let month = 0; month < 12; month += 1) {
      value = mulF(value, onePlusRate) + monthlyContribution;
    }
    rows.push({
      year,
      // contributed is EXACT: annual_contribution × year, then half-up cents.
      contributed_cad: centsToString(
        toCents({ v: annualContribution.v * BigInt(year), s: annualContribution.s }),
      ),
      value_cad: centsToString(fixedToCents(value)),
      value_real_cad: centsToString(fixedToCents(divF(value, deflator(inflation, year)))),
    });
  }
  return rows;
}

/** `POST /projections/run` minus the POST — same response shape (docs/12). */
export function runProjectionEngine(payload: ProjectionRequest): ProjectionResponse {
  validateProjectionRequest(payload);

  const startValue = exact(payload.start_value_cad);
  const annualContribution = exact(payload.annual_contribution_cad);
  const inflation = exact(payload.inflation_pct);
  const years = payload.years;

  const results: ProjectionScenarioResult[] = payload.scenarios.map((scenario) => {
    const series = projectScenario(
      startValue,
      annualContribution,
      exact(scenario.annual_return_pct),
      years,
      inflation,
    );
    const last = series[series.length - 1];
    // total_gain = final − start − contributed, exact to the cent —
    // `_cents(final_value - body.start_value_cad - contributed)`, where
    // `start_value_cad` is NOT pre-quantized on the server either.
    const gain = toCents(
      addScaled(addScaled(exact(last.value_cad), negScaled(startValue)), negScaled(exact(last.contributed_cad))),
    );
    return {
      label: scenario.label,
      series,
      final_value_cad: last.value_cad,
      total_contributed_cad: last.contributed_cad,
      total_gain_cad: centsToString(gain),
    };
  });

  return {
    start_value_cad: centsToString(toCents(startValue)),
    years,
    inflation_pct: centsToString(toCents(inflation)),
    scenarios: results,
  };
}
