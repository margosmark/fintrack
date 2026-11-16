// docs/58 named tests L1–L8 — the life-event registry, the per-type cash
// flows, the early-retirement switch, the timeline and the order-independence
// proof. Every expectation is hand-derivable from docs/58 §1–§4: the year ↔
// step mapping, the registry defaults/bounds, and the additive fold that
// makes event order irrelevant.

import { describe, expect, it } from "vitest";
import {
  LIFE_EVENT_TYPES,
  LifeEventInputError,
  buildEventFlow,
  effectiveRetirementAge,
  lifeEventType,
  planTimeline,
  validateLifeEvents,
  type EventPlanContext,
  type LifeEvent,
} from "../src/lib/lifeEventEngine";
import { PLAN_END_AGE } from "../src/lib/retirementEngine";

/** 40 today, 55 flow steps to 95 — the engine's own default shape. */
function ctx(overrides: Partial<EventPlanContext> = {}): EventPlanContext {
  return {
    baseYear: 2026,
    currentAge: 40,
    years: PLAN_END_AGE - 40,
    retirementAge: 65,
    monthlyContribution: 0,
    annualWithdrawal: 0,
    ...overrides,
  };
}

describe("L1 — the registry is the single source", () => {
  it("exactly the four types in order; every default inside its bounds; early retirement fieldless", () => {
    expect(LIFE_EVENT_TYPES.map((spec) => spec.type)).toEqual([
      "home_purchase",
      "new_child",
      "sabbatical",
      "early_retirement",
    ]);
    for (const spec of LIFE_EVENT_TYPES) {
      expect(spec.label.length).toBeGreaterThan(0);
      for (const field of spec.fields) {
        expect(field.label.length).toBeGreaterThan(0);
        expect(field.min).toBeLessThanOrEqual(field.defaultValue);
        expect(field.defaultValue).toBeLessThanOrEqual(field.max);
        expect(["money", "int"]).toContain(field.kind);
      }
    }
    expect(lifeEventType("early_retirement").fields).toEqual([]);
  });

  it("unknown types and broken identities throw LifeEventInputError naming the offender", () => {
    expect(() => lifeEventType("dream_yacht" as never)).toThrow(LifeEventInputError);
    expect(() => lifeEventType("dream_yacht" as never)).toThrow(/unknown event type dream_yacht/);
    const c = ctx();
    const noId = { type: "new_child", year: 2030, annualCost: 1 } as LifeEvent;
    expect(() => validateLifeEvents([noId], c)).toThrow(/event id must be a non-empty string/);
    const twin: LifeEvent[] = [
      { id: "x", type: "new_child", year: 2030, annualCost: 1 },
      { id: "x", type: "new_child", year: 2031, annualCost: 1 },
    ];
    expect(() => validateLifeEvents(twin, c)).toThrow(/duplicate event id x/);
    expect(() => validateLifeEvents([], c)).not.toThrow();
  });
});

describe("L2 — home purchase", () => {
  const home = (year: number, downPayment: number, annualHousingDelta: number): LifeEvent => ({
    id: `h-${year}-${downPayment}-${annualHousingDelta}`,
    type: "home_purchase",
    year,
    downPayment,
    annualHousingDelta,
  });

  it("down payment hits the event year; the cost change recurs to the plan end", () => {
    const c = ctx();
    const flow = buildEventFlow([home(2028, 100_000, 12_000)], c);
    expect(flow).toHaveLength(c.years);
    expect(flow[0]).toBe(0); // before the year
    expect(flow[1]).toBe(-112_000); // year 2028: −downPayment − delta
    expect(flow[2]).toBe(-12_000); // later years: delta only
    expect(flow[c.years - 1]).toBe(-12_000);
  });

  it("a negative delta adds cash", () => {
    const flow = buildEventFlow([home(2028, 100_000, -5_000)], ctx());
    expect(flow[1]).toBe(-95_000);
    expect(flow[2]).toBe(5_000);
  });

  it("an out-of-range year and a negative down payment each throw naming the field", () => {
    const c = ctx();
    expect(() => buildEventFlow([home(2026, 0, 0)], c)).toThrow(/year must be an integer in/);
    expect(() => buildEventFlow([home(2030, -1, 0)], c)).toThrow(/downPayment must be a number/);
    expect(() => buildEventFlow([home(2030, 0, 600_000)], c)).toThrow(/annualHousingDelta/);
  });
});

describe("L3 — new child", () => {
  it("0 before the year, −annualCost from it to the plan end", () => {
    const c = ctx();
    const flow = buildEventFlow([{ id: "n", type: "new_child", year: 2030, annualCost: 8_000 }], c);
    expect(flow[2]).toBe(0); // 2029
    expect(flow[3]).toBe(-8_000); // 2030
    expect(flow[c.years - 1]).toBe(-8_000); // to the end
  });

  it("an annualCost above the registry bound throws naming the field", () => {
    expect(() =>
      buildEventFlow([{ id: "n2", type: "new_child", year: 2030, annualCost: 500_001 }], ctx()),
    ).toThrow(/annualCost must be a number in 0\.\.500000/);
    expect(() =>
      buildEventFlow([{ id: "n3", type: "new_child", year: 2030, annualCost: Number.NaN }], ctx()),
    ).toThrow(/annualCost/);
  });
});

describe("L4 — sabbatical", () => {
  const c = ctx({ monthlyContribution: 500, annualWithdrawal: 40_000 });

  it("accumulation years fund expenses + surplus loss + extra; retirement years only the extra", () => {
    // i = 4 (2030), duration 3: loop ages 43, 44, 45 — all pre-retirement.
    const flow = buildEventFlow(
      [{ id: "s", type: "sabbatical", year: 2030, durationYears: 3, extraAnnualSpending: 10_000 }],
      c,
    );
    expect(flow[2]).toBe(0);
    expect(flow[3]).toBe(-(500 * 12 + 40_000 + 10_000)); // −56,000
    expect(flow[4]).toBe(-56_000);
    expect(flow[5]).toBe(-56_000);
    expect(flow[6]).toBe(0);

    // Starting at loop age 63, the last three of five years cross the switch:
    // from the switch on, the base flow already draws the expense level.
    const crossing = buildEventFlow(
      [{ id: "s2", type: "sabbatical", year: 2050, durationYears: 5, extraAnnualSpending: 10_000 }],
      c,
    );
    expect(crossing[23]).toBe(-56_000); // loop age 63
    expect(crossing[24]).toBe(-56_000); // 64
    expect(crossing[25]).toBe(-10_000); // 65 = switch → extra only
    expect(crossing[26]).toBe(-10_000);
    expect(crossing[27]).toBe(-10_000);
    expect(crossing[28]).toBe(0);
  });

  it("a duration running past the plan horizon clamps at the horizon", () => {
    // 90 today → 5 steps; a 10-year sabbatical covers exactly those 5.
    const late = ctx({
      currentAge: 90,
      years: PLAN_END_AGE - 90,
      retirementAge: 94,
      annualWithdrawal: 40_000,
    });
    const flow = buildEventFlow(
      [{ id: "s3", type: "sabbatical", year: 2027, durationYears: 10, extraAnnualSpending: 10_000 }],
      late,
    );
    expect(flow).toHaveLength(5);
    expect(flow.slice(0, 4)).toEqual([-50_000, -50_000, -50_000, -50_000]);
    expect(flow[4]).toBe(-10_000); // loop age 94 = switch → extra only
  });

  it("duration 0 and a sabbatical starting at/after retirement each throw", () => {
    expect(() =>
      buildEventFlow([{ id: "s4", type: "sabbatical", year: 2030, durationYears: 0, extraAnnualSpending: 0 }], c),
    ).toThrow(/durationYears must be an integer in 1\.\.10/);
    expect(() =>
      buildEventFlow([{ id: "s5", type: "sabbatical", year: 2052, durationYears: 1, extraAnnualSpending: 0 }], c),
    ).toThrow(/sabbatical must start before retirement/);
  });
});

describe("L5 — early retirement is the phase switch", () => {
  const c = ctx();

  it("effective age = currentAge + (year − baseYear) − 1, capped by the knob", () => {
    expect(effectiveRetirementAge([], c)).toBe(65);
    expect(
      effectiveRetirementAge([{ id: "e", type: "early_retirement", year: 2036 }], c),
    ).toBe(40 + 10 - 1); // 49
    // Switching after the knob would not be early — the knob wins by throwing.
    expect(() =>
      effectiveRetirementAge([{ id: "e-late", type: "early_retirement", year: 2055 }], c),
    ).toThrow(/must fall before retirement age 65/);
  });

  it("year = baseYear+1 throws (no room for a first switch year); a second event throws", () => {
    expect(() =>
      effectiveRetirementAge([{ id: "e1", type: "early_retirement", year: 2027 }], c),
    ).toThrow(/early_retirement year must be an integer in 2028\.\.2081/);
    expect(() =>
      effectiveRetirementAge(
        [
          { id: "ea", type: "early_retirement", year: 2036 },
          { id: "eb", type: "early_retirement", year: 2040 },
        ],
        c,
      ),
    ).toThrow(/only one early_retirement event is allowed/);
  });

  it("the timeline phase flips on the event's row (loop age at the switch)", () => {
    const rows = planTimeline([{ id: "e", type: "early_retirement", year: 2036 }], c);
    expect(rows[8].phase).toBe("Accumulation"); // i=9, loop age 48
    expect(rows[9].phase).toBe("Retirement"); // i=10, loop age 49 = switch
    expect(rows[9].year).toBe(2036); // withdrawals begin ON the event's year
  });
});

describe("L6 — the year-by-year timeline", () => {
  const c = ctx({ monthlyContribution: 500, annualWithdrawal: 40_000 });
  const events: LifeEvent[] = [
    { id: "h", type: "home_purchase", year: 2030, downPayment: 100_000, annualHousingDelta: 12_000 },
    { id: "n", type: "new_child", year: 2030, annualCost: 8_000 },
  ];

  it("years rows; first {baseYear+1, currentAge+1}; last {baseYear+years, currentAge+years}", () => {
    const rows = planTimeline(events, c);
    expect(rows).toHaveLength(c.years);
    expect(rows[0]).toMatchObject({ year: c.baseYear + 1, age: c.currentAge + 1 });
    expect(rows[c.years - 1]).toMatchObject({
      year: c.baseYear + c.years,
      age: c.currentAge + c.years,
    });
  });

  it("phase flips exactly at the effective retirement row; netFlow = base + impact everywhere", () => {
    const rows = planTimeline(events, c);
    expect(rows[24].phase).toBe("Accumulation"); // loop age 64
    expect(rows[25].phase).toBe("Retirement"); // loop age 65
    for (const row of rows) {
      const base = row.phase === "Accumulation" ? 500 * 12 : -40_000;
      expect(row.netFlow).toBe(base + row.eventImpact);
    }
  });

  it("overlapping events sum in eventImpact; startsHere carries the right events", () => {
    const rows = planTimeline(events, c);
    const shared = rows.find((row) => row.year === 2030);
    expect(shared).toBeDefined();
    expect(shared?.startsHere.map((event) => event.type)).toEqual([
      "home_purchase",
      "new_child",
    ]);
    expect(shared?.eventImpact).toBe(-100_000 - 12_000 - 8_000);
    expect(shared?.netFlow).toBe(500 * 12 + (shared?.eventImpact ?? 0));
    // The recurring legs keep landing after the start year…
    expect(rows[4].eventImpact).toBe(-12_000 - 8_000); // 2031
    // …and rows with nothing have zero impact.
    expect(rows[0].eventImpact).toBe(0);
    expect(rows[0].startsHere).toEqual([]);
  });
});

describe("L7 — order never matters", () => {
  it("the same events shuffled produce byte-equal flows", () => {
    const c = ctx({ monthlyContribution: 500, annualWithdrawal: 40_000 });
    const home: LifeEvent = {
      id: "h",
      type: "home_purchase",
      year: 2030,
      downPayment: 100_000,
      annualHousingDelta: 12_000,
    };
    const child: LifeEvent = { id: "n", type: "new_child", year: 2031, annualCost: 8_000 };
    const sabb: LifeEvent = {
      id: "s",
      type: "sabbatical",
      year: 2032,
      durationYears: 2,
      extraAnnualSpending: 10_000,
    };
    const early: LifeEvent = { id: "e", type: "early_retirement", year: 2040 };
    expect(buildEventFlow([child, home, sabb, early], c)).toEqual(
      buildEventFlow([home, sabb, early, child], c),
    );
    expect(buildEventFlow([early, sabb, child, home], c)).toEqual(
      buildEventFlow([sabb, home, early, child], c),
    );
    // …and the timeline agrees (it folds the same additive flow).
    expect(planTimeline([child, home, sabb, early], c)).toEqual(
      planTimeline([early, sabb, home, child], c),
    );
  });
});

describe("L8 — the empty set is exactly the M188 plan", () => {
  it("zero flow, knob-driven phases, effective age = knob, baseline rows", () => {
    const c = ctx({ monthlyContribution: 500, annualWithdrawal: 40_000 });
    const flow = buildEventFlow([], c);
    expect(flow).toHaveLength(c.years);
    expect(flow.every((value) => value === 0)).toBe(true);
    expect(effectiveRetirementAge([], c)).toBe(c.retirementAge);

    const rows = planTimeline([], c);
    expect(rows).toHaveLength(c.years);
    for (const row of rows) {
      expect(row.eventImpact).toBe(0);
      expect(row.startsHere).toEqual([]);
    }
    expect(rows[24].phase).toBe("Accumulation");
    expect(rows[25].phase).toBe("Retirement");
    expect(rows[0].netFlow).toBe(500 * 12);
    expect(rows[25].netFlow).toBe(-40_000);
  });
});
