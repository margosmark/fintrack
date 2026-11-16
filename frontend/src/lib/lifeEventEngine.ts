// docs/58 — life-event planning: an extensible registry of events that reshape
// the retirement plan year by year (M189).
//
// Pure math over caller-supplied numbers: no I/O, no clock, no key material,
// no ../api/ imports (docs/33 rule 8 — lock-vacuous by construction, pinned by
// tests/lifeEventEngine.test.ts). Three rules this module exists to keep:
//
//   * the REGISTRY is the single source for a type's label, fields, bounds and
//     form defaults — the add form, the validation and the list labels all
//     render from it, so a fifth event type is one entry plus one arm below;
//   * the YEAR ↔ STEP mapping is exact (docs/58 §1): an event's `year` maps to
//     step `i = year − baseYear`, valid for `1 ≤ i ≤ years`, and the timeline
//     pairs the engine's year (`baseYear + i`, what the tax seam receives) with
//     the chart's age label (`currentAge + i`, what ages[] draws);
//   * event ORDER NEVER MATTERS — deltas are additive, so the tests shuffle.

/** The closed set of v1 event types; new types extend the registry below. */
export type LifeEventType =
  | "home_purchase"
  | "new_child"
  | "sabbatical"
  | "early_retirement";

export interface HomePurchaseEvent {
  id: string;
  type: "home_purchase";
  year: number;
  /** One-time cash outflow from the portfolio in `year` (CAD, 0..5,000,000). */
  downPayment: number;
  /**
   * Annual housing cost CHANGE vs the spending already in the plan (CAD/year,
   * SIGNED, -500,000..500,000): positive costs more (cash out), negative
   * means the new home costs less than what the plan already spends.
   */
  annualHousingDelta: number;
}

export interface NewChildEvent {
  id: string;
  type: "new_child";
  year: number;
  /** Extra yearly spending from `year` on (CAD/year, 0..500,000). */
  annualCost: number;
}

export interface SabbaticalEvent {
  id: string;
  type: "sabbatical";
  year: number;
  /** Whole years the sabbatical runs, starting at `year` (integer 1..10). */
  durationYears: number;
  /** Spending on top of the plan's own expense level (CAD/year, 0..500,000). */
  extraAnnualSpending: number;
}

export interface EarlyRetirementEvent {
  id: string;
  type: "early_retirement";
  /** The calendar year withdrawals begin — the whole event (docs/58 §4). */
  year: number;
}

export type LifeEvent =
  | HomePurchaseEvent
  | NewChildEvent
  | SabbaticalEvent
  | EarlyRetirementEvent;

/** The key union every type-specific field draws from (form + validation). */
export type LifeEventFieldKey =
  | "downPayment"
  | "annualHousingDelta"
  | "annualCost"
  | "durationYears"
  | "extraAnnualSpending";

export interface LifeEventFieldSpec {
  key: LifeEventFieldKey;
  /** Human label; the form renders it as `<label> (<min>–<max>)`. */
  label: string;
  /** `money` accepts fractions; `int` must be a whole number. */
  kind: "money" | "int";
  min: number;
  max: number;
  /** What the add/edit form prefills (also the "blank field" fallback). */
  defaultValue: number;
}

export interface LifeEventTypeSpec {
  type: LifeEventType;
  label: string;
  /** Ordered — the form renders the type's fields in this order. */
  fields: LifeEventFieldSpec[];
}

/**
 * THE registry (docs/58 §2). Ordered: the add-form's type dropdown renders
 * from it exactly as written. Adding a type = one entry here + one arm in
 * buildEventFlow; the form, bounds, list labels and validation follow.
 */
export const LIFE_EVENT_TYPES: readonly LifeEventTypeSpec[] = [
  {
    type: "home_purchase",
    label: "Home purchase",
    fields: [
      {
        key: "downPayment",
        label: "Down payment (CAD)",
        kind: "money",
        min: 0,
        max: 5_000_000,
        defaultValue: 100_000,
      },
      {
        key: "annualHousingDelta",
        label: "Annual housing cost change (CAD)",
        kind: "money",
        min: -500_000,
        max: 500_000,
        defaultValue: 0,
      },
    ],
  },
  {
    type: "new_child",
    label: "New child",
    fields: [
      {
        key: "annualCost",
        label: "Extra annual cost (CAD)",
        kind: "money",
        min: 0,
        max: 500_000,
        defaultValue: 12_000,
      },
    ],
  },
  {
    type: "sabbatical",
    label: "Sabbatical",
    fields: [
      {
        key: "durationYears",
        label: "Duration (years)",
        kind: "int",
        min: 1,
        max: 10,
        defaultValue: 1,
      },
      {
        key: "extraAnnualSpending",
        label: "Extra spending (CAD)",
        kind: "money",
        min: 0,
        max: 500_000,
        defaultValue: 10_000,
      },
    ],
  },
  {
    type: "early_retirement",
    label: "Early retirement",
    fields: [],
  },
];

/** Registry lookup; throws on an unknown type (never a silent undefined). */
export function lifeEventType(type: LifeEventType): LifeEventTypeSpec {
  const spec = LIFE_EVENT_TYPES.find((candidate) => candidate.type === type);
  if (spec === undefined) throw new LifeEventInputError(`unknown event type ${type}`);
  return spec;
}

/** Everything the event math needs — supplied by the page, read no clock. */
export interface EventPlanContext {
  /** The page's current calendar year (the year of step 0). */
  baseYear: number;
  currentAge: number;
  /** PLAN_END_AGE − currentAge — the number of flow steps. */
  years: number;
  /** The assumptions knob, PRE-event (docs/57 §4). */
  retirementAge: number;
  /** CAD/month — the accumulation surplus the engine adds each year. */
  monthlyContribution: number;
  /** CAD/year NET spending — the plan's own expense level (docs/57 §1). */
  annualWithdrawal: number;
}

/** A value outside a registry bound — the page shows the message verbatim. */
export class LifeEventInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LifeEventInputError";
  }
}

/** `i = year − baseYear` — the event's step (docs/58 §1). Valid range checked first. */
function stepOfYear(year: number, ctx: EventPlanContext): number {
  if (!Number.isInteger(year) || year < ctx.baseYear + 1 || year > ctx.baseYear + ctx.years) {
    throw new LifeEventInputError(
      `year must be an integer in ${ctx.baseYear + 1}..${ctx.baseYear + ctx.years} (got ${year})`,
    );
  }
  return year - ctx.baseYear;
}

function requireField(event: LifeEvent, spec: LifeEventFieldSpec): number {
  const value = (event as unknown as Record<string, number | undefined>)[spec.key];
  if (value === undefined || typeof value !== "number" || !Number.isFinite(value)) {
    throw new LifeEventInputError(`${spec.key} must be a number in ${spec.min}..${spec.max} (got ${value})`);
  }
  if (spec.kind === "int" && !Number.isInteger(value)) {
    throw new LifeEventInputError(
      `${spec.key} must be an integer in ${spec.min}..${spec.max} (got ${value})`,
    );
  }
  if (value < spec.min || value > spec.max) {
    const kind = spec.kind === "int" ? "integer" : "number";
    const article = spec.kind === "int" ? "an" : "a";
    throw new LifeEventInputError(`${spec.key} must be ${article} ${kind} in ${spec.min}..${spec.max} (got ${value})`);
  }
  return value;
}

/** The unchecked switch age: loop age at the early-retirement event's step. */
function earlyRetirementAge(events: LifeEvent[], ctx: EventPlanContext): number | null {
  const event = events.find((candidate) => candidate.type === "early_retirement");
  if (event === undefined) return null;
  return ctx.currentAge + (event.year - ctx.baseYear) - 1;
}

/**
 * Full validation (docs/58 §4): identity, year range, every registry field
 * bound, then the cross-event rules (one early retirement, it must be EARLY,
 * a sabbatical must start before the switch). Throws LifeEventInputError
 * naming the offending field; a valid (possibly empty) set returns void.
 */
export function validateLifeEvents(events: LifeEvent[], ctx: EventPlanContext): void {
  const seen = new Set<string>();
  for (const event of events) {
    if (typeof event.id !== "string" || event.id === "") {
      throw new LifeEventInputError("event id must be a non-empty string");
    }
    if (seen.has(event.id)) throw new LifeEventInputError(`duplicate event id ${event.id}`);
    seen.add(event.id);
    const spec = lifeEventType(event.type);
    stepOfYear(event.year, ctx);
    for (const field of spec.fields) requireField(event, field);
  }

  const early = events.filter((candidate) => candidate.type === "early_retirement");
  if (early.length > 1) {
    throw new LifeEventInputError("only one early_retirement event is allowed");
  }
  if (early.length === 1) {
    // The switch needs loop age >= currentAge + 1 (the engine's own lower
    // bound on retirementAge), i.e. year >= baseYear + 2 — the general range
    // was already checked above, this is the tighter early-retirement bound.
    if (early[0].year < ctx.baseYear + 2) {
      throw new LifeEventInputError(
        `early_retirement year must be an integer in ${ctx.baseYear + 2}..${ctx.baseYear + ctx.years} (got ${early[0].year})`,
      );
    }
    const switchAge = ctx.currentAge + (early[0].year - ctx.baseYear) - 1;
    if (switchAge >= ctx.retirementAge) {
      throw new LifeEventInputError(
        `early_retirement year must fall before retirement age ${ctx.retirementAge} (got ${early[0].year})`,
      );
    }
  }

  const switchAge = earlyRetirementAge(events, ctx) ?? ctx.retirementAge;
  for (const event of events) {
    if (event.type === "sabbatical") {
      const startLoopAge = ctx.currentAge + (event.year - ctx.baseYear) - 1;
      if (startLoopAge >= switchAge) {
        throw new LifeEventInputError(
          `sabbatical must start before retirement (year ${event.year})`,
        );
      }
    }
  }
}

/**
 * The phase-switch age WITH events: `min(knob, early-retirement switch age)`.
 * Validates first — same rules as validateLifeEvents.
 */
export function effectiveRetirementAge(events: LifeEvent[], ctx: EventPlanContext): number {
  validateLifeEvents(events, ctx);
  const early = earlyRetirementAge(events, ctx);
  return early === null ? ctx.retirementAge : Math.min(ctx.retirementAge, early);
}

/**
 * Per-step cash deltas (docs/58 §3): length `ctx.years`, entry `i − 1` applies
 * at step `i` — exactly the shape retirementEngine's `eventFlow` consumes.
 * Additive over events, so order never matters; validates first.
 */
export function buildEventFlow(events: LifeEvent[], ctx: EventPlanContext): number[] {
  validateLifeEvents(events, ctx);
  const switchAge = earlyRetirementAge(events, ctx) ?? ctx.retirementAge;
  const flow = new Array<number>(ctx.years).fill(0);

  for (const event of events) {
    const i = event.year - ctx.baseYear; // 1..years (validated)
    switch (event.type) {
      case "home_purchase": {
        flow[i - 1] -= event.downPayment;
        for (let t = i; t <= ctx.years; t += 1) flow[t - 1] -= event.annualHousingDelta;
        break;
      }
      case "new_child": {
        for (let t = i; t <= ctx.years; t += 1) flow[t - 1] -= event.annualCost;
        break;
      }
      case "sabbatical": {
        const end = Math.min(i + event.durationYears - 1, ctx.years);
        for (let t = i; t <= end; t += 1) {
          const loopAge = ctx.currentAge + t - 1;
          if (loopAge < switchAge) {
            // No income: the portfolio funds the plan's expense level PLUS the
            // event's extra spending, where the base flow would only ADD the
            // surplus (docs/58 §3) — hence the surplus comes out too.
            flow[t - 1] -=
              ctx.monthlyContribution * 12 + ctx.annualWithdrawal + event.extraAnnualSpending;
          } else {
            // In retirement the base flow already draws the expense level.
            flow[t - 1] -= event.extraAnnualSpending;
          }
        }
        break;
      }
      case "early_retirement":
        // Phase switch only — effectiveRetirementAge carries it (docs/58 §4).
        break;
    }
  }
  return flow;
}

export interface PlanYearRow {
  /** `baseYear + i` — the year the tax seam receives for this step. */
  year: number;
  /** `currentAge + i` — the chart's age label for this step (docs/58 §1). */
  age: number;
  phase: "Accumulation" | "Retirement";
  /** Events whose `year` is this row (empty when none start here). */
  startsHere: LifeEvent[];
  /** Sum of every event's CAD effect in this row (0 when no events). */
  eventImpact: number;
  /** Base phase flow (net, display convention) + eventImpact. */
  netFlow: number;
}

/**
 * The year-by-year plan table (docs/58 §5): one row per flow step, in plan
 * order. Phase flips on the step where withdrawals actually start (the loop
 * age, which is the `ages[]` label minus one — the simulator's own
 * convention, docs/58 §1). Validates first.
 */
export function planTimeline(events: LifeEvent[], ctx: EventPlanContext): PlanYearRow[] {
  const flow = buildEventFlow(events, ctx);
  const switchAge = effectiveRetirementAge(events, ctx);
  const rows: PlanYearRow[] = [];
  for (let i = 1; i <= ctx.years; i += 1) {
    const loopAge = ctx.currentAge + i - 1;
    const phase: PlanYearRow["phase"] = loopAge < switchAge ? "Accumulation" : "Retirement";
    const base = phase === "Accumulation" ? ctx.monthlyContribution * 12 : -ctx.annualWithdrawal;
    const impact = flow[i - 1];
    rows.push({
      year: ctx.baseYear + i,
      age: ctx.currentAge + i,
      phase,
      startsHere: events.filter((event) => event.year === ctx.baseYear + i),
      eventImpact: impact,
      netFlow: base + impact,
    });
  }
  return rows;
}
