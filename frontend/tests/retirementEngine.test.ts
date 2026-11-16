// docs/57 named tests R1–R6 — the retirement engine's math pins.
//
// Every number here is hand-derivable from docs/57 §2: the simulation order
// (grow → flow → sticky ruin), the RNG, R-7 percentiles and the tax seam's
// call contract. The scripted-draw tests (R3) are the "ruin probability on a
// fixed series" acceptance criterion — a fixed stream, an exact verdict.

import { describe, expect, it } from "vitest";
import {
  PLAN_END_AGE,
  RetirementInputError,
  quantile,
  runRetirementSimulation,
  validateRetirementInputs,
  type RetirementInputs,
} from "../src/lib/retirementEngine";
import { flatRateTaxEngine, type TaxContext, type TaxEngine } from "../src/lib/taxEngine";

function base(overrides: Partial<RetirementInputs> = {}): RetirementInputs {
  return {
    startingPortfolio: 100_000,
    currentAge: 40,
    retirementAge: 65,
    monthlyContribution: 0,
    annualWithdrawal: 48_000,
    expectedReturnPct: 5,
    volatilityPct: 15,
    scenarios: 2000,
    seed: 42,
    baseYear: 2026,
    tax: flatRateTaxEngine(0),
    ...overrides,
  };
}

describe("R1 — determinism: the seed is the whole story", () => {
  it("same inputs + same seed reproduce the result exactly", () => {
    const a = runRetirementSimulation(base());
    const b = runRetirementSimulation(base());
    expect(b).toEqual(a);
  });

  it("a different seed changes the paths (never a constant draw)", () => {
    const a = runRetirementSimulation(base());
    const c = runRetirementSimulation(base({ seed: 7 }));
    const differs =
      c.successPct !== a.successPct ||
      c.median.join(",") !== a.median.join(",") ||
      c.p90.join(",") !== a.p90.join(",");
    expect(differs).toBe(true);
  });
});

describe("R2 — ruin on a fixed (sigma = 0) series, both verdicts", () => {
  it("a withdrawal that outruns growth ruins EVERY path before 95", () => {
    // 100k, no contributions, 5%/yr: the portfolio decays toward the fixed
    // point 48k/0.05 = 960k from BELOW, so it crosses 0 around age 74.
    const r = runRetirementSimulation(base({ volatilityPct: 0 }));
    expect(r.successPct).toBe(0);
    expect(r.survived).toBe(0);
  });

  it("contributions that outrun the withdrawal keep every path alive", () => {
    // 120k/yr in vs 48k/yr out at a deterministic 5% — no path can die.
    const r = runRetirementSimulation(base({ volatilityPct: 0, monthlyContribution: 10_000 }));
    expect(r.successPct).toBe(100);
    expect(r.survived).toBe(r.scenarios);
  });
});

describe("R3 — a scripted draw series pins the ruin probability exactly", () => {
  it("even scenarios die on their first draw, odd ones double forever → 50.0%", () => {
    // The seam replaces the Gaussian: scenario s sees −1 on its FIRST draw
    // (r = 0 + 1.0·(−1) = −1 → the portfolio zeroes out, sticky ruin) and +1
    // afterwards (doubling, no withdrawal to drain it). 100 scenarios →
    // exactly 50 survivors.
    let call = 0;
    const years = PLAN_END_AGE - 40;
    const drawNormal = (): number => {
      const scenario = Math.floor(call / years);
      call += 1;
      return scenario % 2 === 0 ? -1 : 1;
    };
    const r = runRetirementSimulation(
      base({
        scenarios: 100,
        expectedReturnPct: 0,
        volatilityPct: 100,
        annualWithdrawal: 0,
      }),
      { drawNormal },
    );
    expect(r.survived).toBe(50);
    expect(r.successPct).toBe(50);
    expect(call).toBe(100 * years);
  });

  it("the step-0 band converges on the real starting portfolio", () => {
    const r = runRetirementSimulation(base({ scenarios: 500 }));
    expect(r.ages[0]).toBe(40);
    expect(r.p10[0]).toBe(100_000);
    expect(r.median[0]).toBe(100_000);
    expect(r.p90[0]).toBe(100_000);
    expect(r.ages[r.ages.length - 1]).toBe(PLAN_END_AGE);
    expect(r.ages).toHaveLength(PLAN_END_AGE - 40 + 1);
  });
});

describe("R4 — quantile: R-7 linear interpolation on a known series", () => {
  const sorted = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];

  it("pins the endpoints, the deciles and the median", () => {
    expect(quantile(sorted, 0)).toBe(0);
    expect(quantile(sorted, 0.1)).toBeCloseTo(0.9, 10);
    expect(quantile(sorted, 0.5)).toBeCloseTo(4.5, 10);
    expect(quantile(sorted, 0.9)).toBeCloseTo(8.1, 10);
    expect(quantile(sorted, 1)).toBe(9);
  });

  it("a one-element series is that element; empty and out-of-range q throw", () => {
    expect(quantile([7], 0.37)).toBe(7);
    expect(() => quantile([], 0.5)).toThrow(RetirementInputError);
    expect(() => quantile([1], 1.5)).toThrow(RetirementInputError);
  });
});

describe("R5 — the TaxEngine seam (docs/57 §3)", () => {
  it("calls the engine once per retirement year per path, with {age, year}", () => {
    const calls: TaxContext[] = [];
    const recording: TaxEngine = {
      id: "recording",
      label: "Recording stub",
      grossWithdrawalFor(netSpend: number, ctx: TaxContext): number {
        calls.push(ctx);
        return netSpend;
      },
    };
    runRetirementSimulation(base({ scenarios: 100, tax: recording }));
    const retirementYears = PLAN_END_AGE - 65; // ages 65..94
    expect(calls.length).toBe(100 * retirementYears);
    // First withdrawal: age 65 is step t = 65 - 40 + 1 = 26 → baseYear + 26.
    expect(calls[0]).toEqual({ age: 65, year: 2052 });
    // Last plan step: age 94 is t = 55 → baseYear + 55 (paths keep calling
    // after a ruin — the loop runs to PLAN_END_AGE, sticky ruin or not).
    expect(calls[calls.length - 1]).toEqual({ age: 94, year: 2081 });
  });

  it("a heavier stub rate drains more and lowers success on the same seed", () => {
    const params = {
      startingPortfolio: 800_000,
      retirementAge: 55,
      scenarios: 500,
      volatilityPct: 15,
    } as const;
    const gentle = runRetirementSimulation(
      base({ ...params, tax: flatRateTaxEngine(0) }),
    );
    const heavy = runRetirementSimulation(
      base({ ...params, tax: flatRateTaxEngine(50) }),
    );
    expect(gentle.successPct).toBeGreaterThan(heavy.successPct);
    expect(gentle.successPct).toBeGreaterThan(0);
  });
});

describe("R6 — every bound is enforced, naming the field", () => {
  it("accepts a well-formed input", () => {
    expect(() => validateRetirementInputs(base())).not.toThrow();
  });

  it("rejects each out-of-bounds field with its name in the message", () => {
    expect(() => validateRetirementInputs(base({ currentAge: 17 }))).toThrow(/currentAge/);
    expect(() => validateRetirementInputs(base({ currentAge: 91 }))).toThrow(/currentAge/);
    expect(() => validateRetirementInputs(base({ retirementAge: 40 }))).toThrow(/retirementAge/);
    // 90 is a legal current age; 95 is not a legal retirement age (> 94).
    expect(() =>
      validateRetirementInputs(base({ currentAge: 90, retirementAge: 95 })),
    ).toThrow(/retirementAge/);
    expect(() => validateRetirementInputs(base({ startingPortfolio: -1 }))).toThrow(
      /startingPortfolio/,
    );
    expect(() => validateRetirementInputs(base({ monthlyContribution: -1 }))).toThrow(
      /monthlyContribution/,
    );
    expect(() => validateRetirementInputs(base({ annualWithdrawal: -1 }))).toThrow(
      /annualWithdrawal/,
    );
    expect(() => validateRetirementInputs(base({ expectedReturnPct: 51 }))).toThrow(
      /expectedReturnPct/,
    );
    expect(() => validateRetirementInputs(base({ volatilityPct: 101 }))).toThrow(/volatilityPct/);
    expect(() => validateRetirementInputs(base({ scenarios: 99 }))).toThrow(/scenarios/);
    expect(() => validateRetirementInputs(base({ scenarios: 10_001 }))).toThrow(/scenarios/);
    expect(() => validateRetirementInputs(base({ seed: -1 }))).toThrow(/seed/);
    expect(() => validateRetirementInputs(base({ baseYear: 1800 }))).toThrow(/baseYear/);
    expect(() => validateRetirementInputs(base({ tax: {} as TaxEngine }))).toThrow(/TaxEngine/);
  });
});

describe("R12 — eventFlow (docs/58 §3): validated, applied, verdict-changing", () => {
  const years = PLAN_END_AGE - 40;

  it("wrong length and non-finite entries throw RetirementInputError naming eventFlow", () => {
    expect(() => validateRetirementInputs(base({ eventFlow: [0, 0] }))).toThrow(
      /eventFlow must have \d+ entries/,
    );
    const short = new Array<number>(years - 1).fill(0);
    expect(() => validateRetirementInputs(base({ eventFlow: short }))).toThrow(/eventFlow/);
    const nan = new Array<number>(years).fill(0);
    nan[3] = Number.NaN;
    expect(() => validateRetirementInputs(base({ eventFlow: nan }))).toThrow(/eventFlow\[3\]/);
    // Well-formed passes.
    expect(() => validateRetirementInputs(base({ eventFlow: new Array<number>(years).fill(0) }))).not.toThrow();
  });

  it("a ruinous entry flips a surviving run to 0 while the identical run without it survives", () => {
    // Deterministic (σ = 0, 0% return, no flows): the portfolio sits at 100k
    // forever — until step 1's event spends 100,001 and zeroes it → sticky ruin.
    const flow = new Array<number>(years).fill(0);
    flow[0] = -100_001;
    const still = { volatilityPct: 0, expectedReturnPct: 0, annualWithdrawal: 0 };
    const withEvent = runRetirementSimulation(base({ ...still, eventFlow: flow }));
    expect(withEvent.successPct).toBe(0);
    expect(withEvent.survived).toBe(0);
    const without = runRetirementSimulation(base(still));
    expect(without.successPct).toBe(100);
    expect(without.survived).toBe(without.scenarios);
  });

  it("eventFlow never touches the RNG stream: R3's scripted draws, same 50/50, same call count", () => {
    const flow = new Array<number>(years).fill(0);
    let call = 0;
    const drawNormal = (): number => {
      const scenario = Math.floor(call / years);
      call += 1;
      return scenario % 2 === 0 ? -1 : 1;
    };
    const r = runRetirementSimulation(
      base({
        scenarios: 100,
        expectedReturnPct: 0,
        volatilityPct: 100,
        annualWithdrawal: 0,
        eventFlow: flow,
      }),
      { drawNormal },
    );
    expect(r.survived).toBe(50);
    expect(r.successPct).toBe(50);
    expect(call).toBe(100 * years);
  });
});
