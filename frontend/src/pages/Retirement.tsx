// M188 (docs/57) — /retirement: Monte Carlo on the data fintrack already has.
//
// Three rules the page exists to keep honest:
//   * the four BOUNDARY strings render unconditionally — the sim never reads
//     as more certain than the engine it describes;
//   * the simulation is CLIENT-side over two cached report reads (docs/33
//     §Compute moves client-side) — no bank sync, no new data sources, no new
//     endpoints, and no key material anywhere in the path;
//   * money a reader SEES goes through the mask-aware `formatCad`; percentages,
//     ages and counts stay readable (docs/46 §D2). The assumption inputs are
//     controls, not figures — they keep their raw editable value under a mask,
//     exactly like ProjectionsPanel's "Start value (CAD)".

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { CalendarClock, TrendingUp } from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ApiError } from "../api/client";
import { getCashflow, getNetWorth, type CashflowBucket } from "../api/reports";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { inputClass } from "../components/ui/formStyles";
import { ChartFrame } from "../components/charts/ChartFrame";
import { chartAriaLabel } from "../components/charts/aria";
import { useChartChrome } from "../components/charts/chartDefaults";
import { useChartPalette } from "../components/charts/palette";
import { formatCad } from "../lib/format";
import { useMasked } from "../lib/mask";
import {
  DEFAULT_SCENARIOS,
  DEFAULT_SEED,
  PLAN_END_AGE,
  RetirementInputError,
  runRetirementSimulation,
  SIM_DEBOUNCE_MS,
  type RetirementInputs,
  type RetirementResult,
} from "../lib/retirementEngine";
import { CanadaTaxInputError, caTaxEngine } from "../lib/canadaTaxEngine";
import {
  LIFE_EVENT_TYPES,
  buildEventFlow,
  effectiveRetirementAge,
  lifeEventType,
  planTimeline,
  validateLifeEvents,
  type EventPlanContext,
  type LifeEvent,
  type LifeEventFieldKey,
  type LifeEventType,
  type PlanYearRow,
} from "../lib/lifeEventEngine";

/** Load-bearing boundary copy (docs/57 §4) — asserted verbatim by R8. */
export const RETIREMENT_HONESTY: readonly string[] = [
  "Simulation, not a prediction.",
  "Runs in your browser on the data you already track — no bank sync, no new data sources.",
  "Tax on withdrawals is estimated with Canadian tax tables (federal + Ontario) — a simplification, not a tax filing.",
  "Not financial advice.",
];

/** The seed note under the seed field (docs/57 §4). */
export const SEED_NOTE = "Same seed and assumptions always give the same result.";

/**
 * The page's tax seam instance (docs/57 §3) — exported so R15 can pin the
 * page boundary against a real `CanadaTaxInputError` thrown by this engine.
 */
export const TAX_ENGINE = caTaxEngine();
const TAX_LABEL = TAX_ENGINE.label;

/**
 * Annual-withdrawal field clamp (docs/57 §4). The ca-engine's gross-up
 * doubles the gross candidate until it covers the net spend and refuses once
 * that candidate passes its **$100,000,000** input ceiling (docs/58,
 * `input_too_large`) — with the current tables that trips once the net spend
 * exceeds **$25,000,000** (measured: 25,000,000 OK, 25,000,001 throws, every
 * projection year 2026–2090). $10,000,000 sits at 40% of that reachable
 * limit, so no value the field can hold reaches the engine's refusal path.
 */
export const MAX_ANNUAL_WITHDRAWAL = 10_000_000;

/** Every field's raw text lives here; parsing/clamping happens at parse time. */
interface FormState {
  currentAge: string;
  retirementAge: string;
  contribution: string;
  withdrawal: string;
  returnPct: string;
  volPct: string;
  scenarios: string;
  seed: string;
}

const DEFAULT_FORM: FormState = {
  currentAge: "40",
  retirementAge: "65",
  contribution: "0.00",
  withdrawal: "0.00",
  returnPct: "5.0",
  volPct: "15.0",
  scenarios: String(DEFAULT_SCENARIOS),
  seed: String(DEFAULT_SEED),
};

interface SimNums {
  currentAge: number;
  retirementAge: number;
  contribution: number;
  withdrawal: number;
  returnPct: number;
  volPct: number;
  scenarios: number;
  seed: number;
}

function intOf(raw: string, fallback: number, min: number, max: number): number {
  const n = Number(raw.trim());
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

function numOf(raw: string, fallback: number, min: number, max: number): number {
  const n = Number(raw.trim());
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

/** Raw form text → in-bounds numbers (docs/57 §2 bounds) for the engine. */
function parseForm(form: FormState): SimNums {
  const currentAge = intOf(form.currentAge, 40, 18, 90);
  return {
    currentAge,
    // The knob can never sit at/below the current age, and must leave at
    // least one year before PLAN_END_AGE (94 = 95 - 1).
    retirementAge: intOf(form.retirementAge, 65, currentAge + 1, PLAN_END_AGE - 1),
    contribution: numOf(form.contribution, 0, 0, Number.MAX_VALUE),
    // Clamped at the engine-safe max (docs/57 §4): the ca-engine refuses a
    // gross-up for a net spend above $25M, and a refusal thrown inside the
    // sim used to escape render — the field keeps it out of reach.
    withdrawal: numOf(form.withdrawal, 0, 0, MAX_ANNUAL_WITHDRAWAL),
    returnPct: numOf(form.returnPct, 5, -50, 50),
    volPct: numOf(form.volPct, 15, 0, 100),
    scenarios: intOf(form.scenarios, DEFAULT_SCENARIOS, 100, 10000),
    seed: intOf(form.seed, DEFAULT_SEED, 0, 2147483647),
  };
}

/** Canonical display string for a field — the "Using X" hint compares against it. */
function canonical(key: keyof FormState, nums: SimNums): string {
  switch (key) {
    case "currentAge":
      return String(nums.currentAge);
    case "retirementAge":
      return String(nums.retirementAge);
    case "contribution":
    case "withdrawal":
      return nums[key].toFixed(2);
    case "returnPct":
    case "volPct":
      return nums[key].toFixed(1);
    case "scenarios":
      return String(nums.scenarios);
    case "seed":
      return String(nums.seed);
  }
}

/** The life-event add/edit form's raw text (docs/58 §5). */
interface EventDraft {
  type: LifeEventType;
  /** Raw year text; blank means "the earliest year this type can take". */
  year: string;
  fields: Record<string, string>;
}

/** Canonical text for a field's value — the form prefills exactly what its
 * "Using X" hint would echo, so a pristine field shows no hint (docs/58 §5). */
function formatDraftValue(kind: "money" | "int", value: number): string {
  return kind === "money" ? value.toFixed(2) : String(value);
}

function defaultDraft(type: LifeEventType): EventDraft {
  return {
    type,
    year: "",
    fields: Object.fromEntries(
      lifeEventType(type).fields.map((field) => [
        field.key,
        formatDraftValue(field.kind, field.defaultValue),
      ]),
    ),
  };
}

/** Earliest selectable year for a type — the switch needs one extra year. */
function minEventYear(type: LifeEventType, baseYear: number): number {
  return baseYear + (type === "early_retirement" ? 2 : 1);
}

/**
 * Raw draft → a typed event. Blank year falls back to the type's minimum,
 * blank fields to their registry defaults; out-of-range and NaN values pass
 * through UNCLAMPED so validateLifeEvents rejects them with the exact message
 * (strict validation beats silent tolerance — no blur-clamp hides a typo).
 */
function draftToEvent(draft: EventDraft, id: string, baseYear: number): LifeEvent {
  const spec = lifeEventType(draft.type);
  const rawYear = draft.year.trim();
  const year = rawYear === "" ? minEventYear(draft.type, baseYear) : Number(rawYear);
  const value = (key: LifeEventFieldKey): number => {
    const raw = (draft.fields[key] ?? "").trim();
    const fallback = spec.fields.find((field) => field.key === key)?.defaultValue ?? 0;
    return raw === "" ? fallback : Number(raw);
  };
  switch (draft.type) {
    case "home_purchase":
      return {
        id,
        type: "home_purchase",
        year,
        downPayment: value("downPayment"),
        annualHousingDelta: value("annualHousingDelta"),
      };
    case "new_child":
      return { id, type: "new_child", year, annualCost: value("annualCost") };
    case "sabbatical":
      return {
        id,
        type: "sabbatical",
        year,
        durationYears: value("durationYears"),
        extraAnnualSpending: value("extraAnnualSpending"),
      };
    case "early_retirement":
      return { id, type: "early_retirement", year };
  }
}

/** The `{detail, code}` envelope, spelled out (docs/08 §Error envelope). */
function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.code === null ? error.message : `${error.message} (${error.code})`;
  }
  return error instanceof Error ? error.message : "Request failed";
}

const isoDay = (d: Date): string => d.toISOString().slice(0, 10);

/** Commit a value to the sim `delay` ms after the last change (docs/57 §4). */
function useDebounced<T>(value: T, delay: number): T {
  const [committed, setCommitted] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setCommitted(value), delay);
    return () => window.clearTimeout(id);
  }, [value, delay]);
  return committed;
}

interface NumFieldProps {
  label: string;
  raw: string;
  effective: string;
  onChange: (raw: string) => void;
  onBlur: () => void;
  inputMode?: "decimal" | "numeric";
  testId?: string;
}

/** Raw-on-change, normalize-on-blur text field with the "Using X" hint. */
function NumField({ label, raw, effective, onChange, onBlur, inputMode = "decimal", testId }: NumFieldProps) {
  return (
    <label className="block text-sm text-muted">
      {label}
      <input
        value={raw}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        inputMode={inputMode}
        data-testid={testId}
        className={`${inputClass} mt-1 block w-full`}
      />
      {raw.trim() !== "" && raw.trim() !== effective && (
        <span className="mt-1 block text-xs text-muted">Using {effective}</span>
      )}
    </label>
  );
}

interface FanRow {
  age: number;
  p10: number;
  band: number;
  median: number;
}

/** Mask-aware tooltip over the fan rows (docs/46 §D2: money masks here too). */
function FanTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ payload?: FanRow }>;
  label?: string | number;
}) {
  useMasked();
  const palette = useChartPalette();
  if (active !== true || payload === undefined || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (row === undefined) return null;
  const entries: Array<[string, number, string]> = [
    ["p10", row.p10, palette.ACCENT],
    ["Median", row.median, palette.WARN],
    ["p90", row.p10 + row.band, palette.ACCENT],
  ];
  return (
    <div className="glass-strong min-w-36 rounded-lg px-3 py-2 text-xs shadow-card">
      <p className="mb-1.5 font-medium text-muted">Age {String(label)}</p>
      <div className="space-y-1">
        {entries.map(([name, value, color]) => (
          <div key={name} className="flex items-center gap-2">
            <span
              className="size-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: color }}
              aria-hidden
            />
            <span className="text-muted">{name}</span>
            <span className="ml-auto pl-4 font-medium tabular">{formatCad(value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Retirement() {
  // M140 (docs/46 §D6): the page renders money itself (readouts, seed note),
  // so it must subscribe or those strings would freeze on a mask flip — the
  // chart axis repaints through ChartFrame, this covers the rest.
  useMasked();
  const chrome = useChartChrome();
  const palette = useChartPalette();

  const today = isoDay(new Date());
  const yearAgo = isoDay(new Date(Date.now() - 365 * 86_400_000));
  // The calendar year of step 0 — event years and the timeline key off it
  // (docs/58 §1). Read per render, so a session crossing New Year re-derives
  // instead of holding a stale horizon.
  const baseYear = new Date().getFullYear();

  const netWorth = useQuery({
    queryKey: ["net-worth", "day", today, today],
    queryFn: () => getNetWorth(today, today, "day"),
  });
  const cashflow = useQuery({
    queryKey: ["cashflow", "month", yearAgo, today],
    queryFn: () => getCashflow(yearAgo, today, "month"),
  });

  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  const [touched, setTouched] = useState<{ contribution?: boolean; withdrawal?: boolean }>({});
  // Debounce only a HUMAN edit: the seeded defaults must land in the verdict
  // with the data (delay 0), or the first paint would run the sim on the
  // pre-seed zeros and flash a fake 100% before the real numbers commit.
  const [userEdited, setUserEdited] = useState(false);

  // Life events (docs/58): PAGE STATE, committed immediately on add/save/
  // remove — the 300 ms debounce belongs to assumption keystrokes only.
  const [events, setEvents] = useState<LifeEvent[]>([]);
  const [draft, setDraft] = useState<EventDraft>(() => defaultDraft("home_purchase"));
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const eventIdRef = useRef(0);
  const nextEventId = (): string => {
    eventIdRef.current += 1;
    return `evt-${eventIdRef.current}`;
  };

  const onField = (key: keyof FormState) => (raw: string): void => {
    setUserEdited(true);
    if (key === "contribution" || key === "withdrawal") {
      setTouched((current) => ({ ...current, [key]: true }));
    }
    setForm((current) => ({ ...current, [key]: raw }));
  };
  const onBlurField = (key: keyof FormState) => (): void =>
    setForm((current) => ({ ...current, [key]: canonical(key, parseForm(current)) }));

  // Starting portfolio + the two money defaults come from the data fintrack
  // already has (docs/57 §1); an edit the user already made always wins.
  const seeded = useMemo(() => {
    if (netWorth.data === undefined || cashflow.data === undefined) return null;
    const bucket = netWorth.data.buckets[netWorth.data.buckets.length - 1];
    if (bucket === undefined) return null;
    const rows: CashflowBucket[] = cashflow.data.buckets;
    const mean = (pick: (b: CashflowBucket) => string): number =>
      rows.length === 0 ? 0 : rows.reduce((sum, b) => sum + Number(pick(b)), 0) / rows.length;
    const meanExpenses = mean((b) => b.expenses_cad);
    const meanIncome = mean((b) => b.income_cad);
    return {
      startingPortfolio: Number(bucket.net_cad),
      contribution: Math.max(0, meanIncome - meanExpenses).toFixed(2),
      withdrawal: (Math.max(0, meanExpenses) * 12).toFixed(2),
    };
  }, [netWorth.data, cashflow.data]);

  useEffect(() => {
    if (seeded === null) return;
    setForm((current) => ({
      ...current,
      ...(touched.contribution ? {} : { contribution: seeded.contribution }),
      ...(touched.withdrawal ? {} : { withdrawal: seeded.withdrawal }),
    }));
  }, [seeded, touched]);

  const nums = useMemo(() => parseForm(form), [form]);
  const committed = useDebounced(nums, userEdited ? SIM_DEBOUNCE_MS : 0);

  // The event math's context (docs/58 §2) — derived from the COMMITTED
  // assumptions, so an event set is always judged against what the sim runs.
  const eventCtx: EventPlanContext | null = useMemo(
    () =>
      seeded === null
        ? null
        : {
            baseYear,
            currentAge: committed.currentAge,
            years: PLAN_END_AGE - committed.currentAge,
            retirementAge: committed.retirementAge,
            monthlyContribution: committed.contribution,
            annualWithdrawal: committed.withdrawal,
          },
    [seeded, committed, baseYear],
  );

  // An event set can go stale against EDITED assumptions (raising the current
  // age shrinks the horizon past an event's year) — re-validate every render:
  // the message shows, the plan falls back to the event-free baseline, and
  // nothing crashes or silently drops an event (docs/58 §4).
  const eventsIssue: string | null = useMemo(() => {
    if (eventCtx === null) return null;
    try {
      validateLifeEvents(events, eventCtx);
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : "Invalid event set";
    }
  }, [events, eventCtx]);
  const issue = eventsIssue ?? formError;
  const eventsUsable = eventsIssue === null;

  // --- event form handlers: state transitions only, never a network call ---
  const commitDraft = (): void => {
    if (eventCtx === null) return;
    try {
      const event = draftToEvent(draft, editingId ?? nextEventId(), eventCtx.baseYear);
      const next =
        editingId === null
          ? [...events, event]
          : events.map((candidate) => (candidate.id === editingId ? event : candidate));
      validateLifeEvents(next, eventCtx);
      setEvents(next);
      setEditingId(null);
      setDraft(defaultDraft(event.type));
      setFormError(null);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Invalid event");
    }
  };

  const startEdit = (event: LifeEvent): void => {
    const record = event as unknown as Record<string, number>;
    setDraft({
      type: event.type,
      year: String(event.year),
      fields: Object.fromEntries(
        lifeEventType(event.type).fields.map((field) => [
          field.key,
          formatDraftValue(field.kind, record[field.key]),
        ]),
      ),
    });
    setEditingId(event.id);
    setFormError(null);
  };

  const cancelEdit = (): void => {
    setEditingId(null);
    setDraft(defaultDraft(draft.type));
    setFormError(null);
  };

  const removeEvent = (id: string): void => {
    setEvents((current) => current.filter((event) => event.id !== id));
    if (editingId === id) {
      setEditingId(null);
      setDraft(defaultDraft(draft.type));
    }
    setFormError(null);
  };

  const onTypeChange = (type: LifeEventType): void => {
    setDraft(defaultDraft(type));
    setFormError(null);
  };

  // One memo: the plan WITH the events, its event-free twin (same seed, so
  // the delta isolates the events on an identical RNG stream), the timeline
  // table, and the early-retirement knob the plan actually ran at.
  const sim = useMemo(() => {
    if (seeded === null || eventCtx === null) return null;
    const startingPortfolio = seeded.startingPortfolio;
    if (!Number.isFinite(startingPortfolio) || startingPortfolio <= 0) return null;
    const inputs: RetirementInputs = {
      startingPortfolio,
      currentAge: committed.currentAge,
      retirementAge: committed.retirementAge,
      monthlyContribution: committed.contribution,
      annualWithdrawal: committed.withdrawal,
      expectedReturnPct: committed.returnPct,
      volatilityPct: committed.volPct,
      scenarios: committed.scenarios,
      seed: committed.seed,
      baseYear,
      tax: TAX_ENGINE,
    };
    const run = (candidate: RetirementInputs): RetirementResult | null => {
      try {
        return runRetirementSimulation(candidate);
      } catch (error) {
        // Both classes mean "these inputs cannot be simulated": the retirement
        // engine's own validation error, and the tax engine's input error — a
        // DIFFERENT class (docs/58), not a subclass, so before this arm an
        // engine refusal escaped render and the route showed its misleading
        // "the page could not be loaded" panel (security F1, t_daede909).
        // Anything else is a real defect and still surfaces loudly.
        if (error instanceof RetirementInputError || error instanceof CanadaTaxInputError) {
          return null;
        }
        throw error;
      }
    };
    // A stale event set contributes NOTHING — the error string above is the
    // feedback; the plan degrades to the event-free baseline instead of dying.
    const usable = eventsUsable ? events : [];
    const withEvents = usable.length > 0;
    const plan = run(
      withEvents
        ? {
            ...inputs,
            retirementAge: effectiveRetirementAge(usable, eventCtx),
            eventFlow: buildEventFlow(usable, eventCtx),
          }
        : inputs,
    );
    if (plan === null) return null;
    const baseline = withEvents ? run(inputs) : plan;
    return {
      plan,
      baseline,
      timeline: planTimeline(withEvents ? usable : [], eventCtx),
      delta:
        withEvents && baseline !== null
          ? { withPct: plan.successPct, basePct: baseline.successPct }
          : null,
      planRetirementAge: withEvents
        ? effectiveRetirementAge(usable, eventCtx)
        : committed.retirementAge,
    };
  }, [seeded, committed, events, eventsUsable, eventCtx, baseYear]);

  const result: RetirementResult | null = sim?.plan ?? null;
  const successDelta = sim?.delta ?? null;
  const timeline: PlanYearRow[] = sim?.timeline ?? [];
  const planRetirementAge = sim?.planRetirementAge ?? committed.retirementAge;

  const chartData = useMemo(() => {
    if (result === null) return [];
    return result.ages.map((age, i) => ({
      age,
      p10: result.p10[i],
      band: result.p90[i] - result.p10[i],
      median: result.median[i],
    }));
  }, [result]);

  const pick = (series: number[], age: number): number => {
    if (result === null) return 0;
    const index = age - committed.currentAge;
    return series[Math.min(Math.max(index, 0), series.length - 1)];
  };

  // Rebuilt every render on purpose — the money in the facts must answer a
  // mask flip on the pass the flip causes (docs/46 §D6, never memoize this).
  const chartLabel =
    result === null
      ? ""
      : chartAriaLabel({
          title: "Retirement net worth",
          kind: "simulation fan",
          range: `age ${committed.currentAge} to ${PLAN_END_AGE}`,
          points: chartData.length,
          facts: [
            `probability of success ${result.successPct.toFixed(1)}%`,
            `median at ${planRetirementAge} ${formatCad(pick(result.median, planRetirementAge))}`,
            `median at ${PLAN_END_AGE} ${formatCad(pick(result.median, PLAN_END_AGE))}`,
          ],
        });

  // The verdict may only render once the COMMITTED assumptions carry the
  // seeded money defaults — otherwise the first paint runs the sim on the
  // pre-seed zeros and flashes a fake 100% for a beat (docs/57 §4).
  const seedCaughtUp =
    seeded === null ||
    ((touched.contribution || committed.contribution === Number(seeded.contribution)) &&
      (touched.withdrawal || committed.withdrawal === Number(seeded.withdrawal)));
  const hasPortfolio =
    seeded !== null && Number.isFinite(seeded.startingPortfolio) && seeded.startingPortfolio > 0;
  const loading = netWorth.isPending || cashflow.isPending;
  const queryError = netWorth.isError ? netWorth.error : cashflow.isError ? cashflow.error : null;

  const ageTicks = chartData.map((d) => d.age).filter((age) => age % 5 === 0);

  // Life-event form bounds (docs/58 §5): the type's earliest year through
  // the last plan year. The "Using X" hints clamp like the assumptions do,
  // but nothing normalizes on blur — submit validates the RAW text.
  const eventYearMin = minEventYear(draft.type, baseYear);
  const eventYearMax = baseYear + (PLAN_END_AGE - committed.currentAge) - 1;

  return (
    <section className="space-y-6">
      {/* Header: what this is and what it is NOT — the boundary strings
          render whether or not the queries ever resolve (docs/57 §4). */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Retirement</h1>
          <p className="flex items-center gap-1.5 text-sm text-muted">
            <CalendarClock className="size-4" aria-hidden />
            <span>
              {loading
                ? "Loading your net worth…"
                : `As of ${today} · ${committed.scenarios.toLocaleString("en-CA")} scenarios · age ${committed.currentAge} → ${PLAN_END_AGE}`}
            </span>
          </p>
        </div>
        <ul className="flex flex-wrap gap-2">
          {RETIREMENT_HONESTY.map((line) => (
            <li
              key={line}
              className="max-w-full rounded-lg border border-border px-2.5 py-1 text-xs text-text [overflow-wrap:anywhere]"
            >
              {line}
            </li>
          ))}
        </ul>
      </div>

      {queryError !== null && (
        <ErrorPanel
          message={describeError(queryError)}
          onRetry={() => {
            if (netWorth.isError) void netWorth.refetch();
            if (cashflow.isError) void cashflow.refetch();
          }}
          retrying={netWorth.isFetching || cashflow.isFetching}
        />
      )}

      <div className="grid gap-4 lg:grid-cols-[20rem_minmax(0,1fr)]">
        {/* Assumptions — every field is clamped to docs/57 §2 bounds at parse
            time, so a half-typed box can never reach the engine out of range. */}
        <Card className="p-4 lg:self-start">
          <CardTitle>Assumptions</CardTitle>
          <div className="space-y-4">
            <NumField
              label="Current age (18–90)"
              raw={form.currentAge}
              effective={canonical("currentAge", nums)}
              inputMode="numeric"
              onChange={onField("currentAge")}
              onBlur={onBlurField("currentAge")}
            />
            <label className="block text-sm text-muted">
              Retirement age: <span className="font-medium text-text">{nums.retirementAge}</span>
              <input
                type="range"
                min={nums.currentAge + 1}
                max={PLAN_END_AGE - 1}
                value={nums.retirementAge}
                onChange={(event) =>
                  onField("retirementAge")(
                    String(intOf(event.target.value, 65, nums.currentAge + 1, PLAN_END_AGE - 1)),
                  )
                }
                className="mt-3 block w-full accent-accent"
              />
            </label>
            <NumField
              label="Monthly contribution (CAD)"
              raw={form.contribution}
              effective={canonical("contribution", nums)}
              onChange={onField("contribution")}
              onBlur={onBlurField("contribution")}
            />
            <NumField
              label={`Annual withdrawal (CAD, 0–${MAX_ANNUAL_WITHDRAWAL.toLocaleString("en-CA")})`}
              raw={form.withdrawal}
              effective={canonical("withdrawal", nums)}
              onChange={onField("withdrawal")}
              onBlur={onBlurField("withdrawal")}
            />
            <NumField
              label="Expected annual return % (−50–50)"
              raw={form.returnPct}
              effective={canonical("returnPct", nums)}
              onChange={onField("returnPct")}
              onBlur={onBlurField("returnPct")}
            />
            <NumField
              label="Volatility % (0–100)"
              raw={form.volPct}
              effective={canonical("volPct", nums)}
              onChange={onField("volPct")}
              onBlur={onBlurField("volPct")}
            />
            <NumField
              label="Scenarios (100–10,000)"
              raw={form.scenarios}
              effective={canonical("scenarios", nums)}
              inputMode="numeric"
              onChange={onField("scenarios")}
              onBlur={onBlurField("scenarios")}
            />
            <NumField
              label="Seed"
              raw={form.seed}
              effective={canonical("seed", nums)}
              inputMode="numeric"
              onChange={onField("seed")}
              onBlur={onBlurField("seed")}
            />
            <div>
              <span className="block text-xs text-muted">Tax engine: {TAX_LABEL}</span>
            </div>
            <p className="text-xs text-muted [overflow-wrap:anywhere]">{SEED_NOTE}</p>
          </div>
        </Card>

        <div className="min-w-0 space-y-4">
          {loading ? (
            <Card className="p-4">
              <CardTitle>Probability of success</CardTitle>
              <SkeletonRows rows={6} />
            </Card>
          ) : queryError !== null ? null : !hasPortfolio ? (
            <Card className="p-4">
              <EmptyState
                icon={<TrendingUp className="size-8" aria-hidden />}
                title="Nothing to simulate yet"
                hint={`Your net worth is ${formatCad(seeded?.startingPortfolio ?? 0)} — the simulation needs a positive portfolio to project. Record your accounts and holdings first.`}
                preview={{
                  none: "The simulator has no positive net worth to project yet",
                }}
                action={{ label: "Go to Accounts", to: "/accounts" }}
              />
            </Card>
          ) : !seedCaughtUp ? (
            <Card className="p-4">
              <SkeletonRows rows={4} />
            </Card>
          ) : result === null ? (
            // The inputs were refused (docs/57 §3): the retirement engine's
            // validation and the tax engine's own input error both land here
            // as `null`. Say so — before t_daede909 the engine's error escaped
            // render instead and the route showed its misleading "the page
            // could not be loaded, reload to fetch it fresh" panel.
            <Card className="p-4">
              <p
                data-testid="retirement-input-issue"
                role="alert"
                className="rounded-lg border border-danger/30 px-2.5 py-1.5 text-xs text-danger"
              >
                These assumptions are outside the simulator's supported range — adjust them in the
                Assumptions panel and the plan will re-run.
              </p>
            </Card>
          ) : (
            <>
              <Card className="p-4">
                <CardTitle>Probability of success</CardTitle>
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                  <p
                    data-testid="retirement-success"
                    className="text-4xl font-semibold tabular text-text"
                  >
                    {result.successPct.toFixed(1)}%
                  </p>
                  <p className="max-w-xl text-sm text-muted">
                    {result.survived.toLocaleString("en-CA")} of{" "}
                    {result.scenarios.toLocaleString("en-CA")} simulated paths never ran out of
                    money before age {PLAN_END_AGE}.
                  </p>
                </div>
                <table className="mt-4 w-full text-sm">
                  <caption className="mb-1.5 text-left text-xs uppercase tracking-wide text-muted">
                    Net worth by percentile (CAD)
                  </caption>
                  <thead>
                    <tr className="text-xs text-muted">
                      <th scope="col" className="py-1 text-left font-medium">
                        Age
                      </th>
                      <th scope="col" className="py-1 text-right font-medium">
                        p10
                      </th>
                      <th scope="col" className="py-1 text-right font-medium">
                        Median
                      </th>
                      <th scope="col" className="py-1 text-right font-medium">
                        p90
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {[planRetirementAge, PLAN_END_AGE].map((age) => (
                      <tr key={age} className="border-t border-border">
                        <td className="py-1.5 text-muted">{age}</td>
                        <td className="py-1.5 text-right tabular">{formatCad(pick(result.p10, age))}</td>
                        <td className="py-1.5 text-right tabular">
                          {formatCad(pick(result.median, age))}
                        </td>
                        <td className="py-1.5 text-right tabular">{formatCad(pick(result.p90, age))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>

              <Card className="p-4">
                <CardTitle>Net worth through retirement</CardTitle>
                <ChartFrame label={chartLabel}>
                  <ResponsiveContainer width="100%" height={320}>
                    <AreaChart data={chartData} margin={{ top: 8, right: 8 }}>
                      <CartesianGrid {...chrome.grid} vertical={false} />
                      <XAxis
                        dataKey="age"
                        type="number"
                        domain={[committed.currentAge, PLAN_END_AGE]}
                        ticks={ageTicks}
                        {...chrome.axis}
                        height={40}
                        tickMargin={8}
                      />
                      <YAxis {...chrome.axis} width={80} domain={[0, "auto"]} tickFormatter={chrome.moneyAxis} />
                      <Tooltip content={<FanTooltip />} cursor={chrome.cursorStroke} />
                      <ReferenceLine
                        x={planRetirementAge}
                        stroke={palette.WARN}
                        strokeDasharray="4 3"
                        label={{
                          value: "Retirement",
                          position: "insideTopRight",
                          fill: palette.WARN,
                          fontSize: 11,
                        }}
                      />
                      {/* The band: p10 carries the stack, band fills p10→p90. */}
                      <Area
                        dataKey="p10"
                        stackId="band"
                        stroke="none"
                        fillOpacity={0}
                        isAnimationActive={false}
                      />
                      <Area
                        dataKey="band"
                        name="p10–p90"
                        type="monotone"
                        stackId="band"
                        stroke="none"
                        fill={palette.ACCENT}
                        fillOpacity={0.18}
                        isAnimationActive={false}
                      />
                      <Area
                        dataKey="median"
                        name="Median"
                        type="monotone"
                        stroke={palette.WARN}
                        strokeWidth={2}
                        fill={palette.WARN}
                        fillOpacity={0.34}
                        isAnimationActive={false}
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </ChartFrame>
                <p className="mt-2 text-xs text-muted">
                  Shaded band: p10–p90 of {result.scenarios.toLocaleString("en-CA")} paths · line:
                  median · dashed marker: retirement age.{" "}
                  <Link className="underline-offset-2 hover:underline" to="/forecast">
                    Deterministic forecast
                  </Link>{" "}
                  is separate.
                </p>
              </Card>

              {/* Life events (docs/58): add/remove/edit commits immediately;
                  the plan, the delta line and the timeline below all re-run. */}
              <Card className="p-4">
                <CardTitle>Life events</CardTitle>
                <p className="mt-1 text-xs text-muted">
                  Events reshape the plan — each one changes the year-by-year cash flow below and
                  the success rate.
                </p>
                {issue !== null && (
                  <p
                    data-testid="life-event-error"
                    role="alert"
                    className="mt-2 rounded-lg border border-danger/30 px-2.5 py-1.5 text-xs text-danger"
                  >
                    {issue}
                  </p>
                )}
                <div className="mt-3 space-y-3">
                  <label className="block text-sm text-muted">
                    Event type
                    <select
                      data-testid="life-event-type"
                      value={draft.type}
                      onChange={(event) => onTypeChange(event.target.value as LifeEventType)}
                      className={`${inputClass} mt-1 block w-full`}
                    >
                      {LIFE_EVENT_TYPES.map((spec) => (
                        <option key={spec.type} value={spec.type}>
                          {spec.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <NumField
                    label={`Event year (${eventYearMin}–${eventYearMax})`}
                    raw={draft.year}
                    effective={String(intOf(draft.year, eventYearMin, eventYearMin, eventYearMax))}
                    inputMode="numeric"
                    onChange={(raw) => setDraft((current) => ({ ...current, year: raw }))}
                    onBlur={() => undefined}
                    testId="life-event-year"
                  />
                  {lifeEventType(draft.type).fields.map((field) => (
                    <NumField
                      key={field.key}
                      label={`${field.label} (${field.min.toLocaleString("en-CA")}–${field.max.toLocaleString("en-CA")})`}
                      raw={draft.fields[field.key] ?? ""}
                      effective={
                        field.kind === "money"
                          ? numOf(
                              draft.fields[field.key] ?? "",
                              field.defaultValue,
                              field.min,
                              field.max,
                            ).toFixed(2)
                          : String(
                              intOf(draft.fields[field.key] ?? "", field.defaultValue, field.min, field.max),
                            )
                      }
                      inputMode={field.kind === "int" ? "numeric" : "decimal"}
                      onChange={(raw) =>
                        setDraft((current) => ({
                          ...current,
                          fields: { ...current.fields, [field.key]: raw },
                        }))
                      }
                      onBlur={() => undefined}
                      testId={`life-event-field-${field.key}`}
                    />
                  ))}
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="primary"
                      size="sm"
                      data-testid="life-event-submit"
                      onClick={commitDraft}
                    >
                      {editingId === null ? "Add event" : "Save event"}
                    </Button>
                    {editingId !== null && (
                      <Button variant="ghost" size="sm" onClick={cancelEdit}>
                        Cancel
                      </Button>
                    )}
                  </div>
                </div>
                {events.length === 0 ? (
                  <p className="mt-3 text-xs text-muted">
                    No events yet — the timeline below is the plan without any.
                  </p>
                ) : (
                  <ul className="mt-3 space-y-2">
                    {events.map((event) => {
                      const spec = lifeEventType(event.type);
                      const record = event as unknown as Record<string, number>;
                      const summary = spec.fields
                        .map((field) =>
                          field.kind === "money"
                            ? `${field.label}: ${formatCad(record[field.key])}`
                            : `${field.label}: ${record[field.key]}`,
                        )
                        .join(" · ");
                      return (
                        <li
                          key={event.id}
                          data-testid="life-event-item"
                          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border px-2.5 py-1.5 text-sm"
                        >
                          <span className="font-medium text-text">
                            {spec.label} {event.year}
                          </span>
                          {summary !== "" && <span className="text-xs text-muted">{summary}</span>}
                          <span className="ml-auto flex gap-1.5">
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-label={`Edit ${spec.label} ${event.year}`}
                              onClick={() => startEdit(event)}
                            >
                              Edit
                            </Button>
                            <Button
                              variant="danger"
                              size="sm"
                              aria-label={`Remove ${spec.label} ${event.year}`}
                              onClick={() => removeEvent(event.id)}
                            >
                              Remove
                            </Button>
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
                {successDelta !== null && (
                  <p data-testid="life-event-success-delta" className="mt-3 text-sm text-muted">
                    Success with events:{" "}
                    <span className="font-medium text-text">{successDelta.withPct.toFixed(1)}%</span>{" "}
                    · without events:{" "}
                    <span className="font-medium text-text">{successDelta.basePct.toFixed(1)}%</span>{" "}
                    · delta{" "}
                    <span className="font-medium text-text">
                      {successDelta.withPct - successDelta.basePct > 0 ? "+" : ""}
                      {(successDelta.withPct - successDelta.basePct).toFixed(1)} pp
                    </span>
                  </p>
                )}
              </Card>

              {/* Year-by-year plan (docs/58 §6): engine years on a page axis
                  that starts at the CURRENT age — first row = age + 1. */}
              <Card className="p-4">
                <CardTitle>Year-by-year cash flow</CardTitle>
                <div className="mt-2 max-h-80 overflow-y-auto">
                  <table data-testid="life-event-timeline" className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-muted">
                        <th scope="col" className="py-1 text-left font-medium">
                          Year
                        </th>
                        <th scope="col" className="py-1 text-right font-medium">
                          Age
                        </th>
                        <th scope="col" className="py-1 text-left font-medium">
                          Phase
                        </th>
                        <th scope="col" className="py-1 text-left font-medium">
                          Starts this year
                        </th>
                        <th scope="col" className="py-1 text-right font-medium">
                          Event impact
                        </th>
                        <th scope="col" className="py-1 text-right font-medium">
                          Net cash flow
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {timeline.map((row) => (
                        <tr key={row.year} className="border-t border-border">
                          <td className="py-1.5 text-muted">{row.year}</td>
                          <td className="py-1.5 text-right text-muted">{row.age}</td>
                          <td className="py-1.5 text-muted">{row.phase}</td>
                          <td className="py-1.5 text-xs text-muted">
                            {row.startsHere.length === 0
                              ? "—"
                              : row.startsHere
                                  .map((event) => lifeEventType(event.type).label)
                                  .join(", ")}
                          </td>
                          <td className="py-1.5 text-right tabular">{formatCad(row.eventImpact)}</td>
                          <td className="py-1.5 text-right tabular">{formatCad(row.netFlow)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-2 text-xs text-muted">
                  One row per plan year: the phase flow plus every event's impact, net of tax on
                  withdrawals.
                </p>
              </Card>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
