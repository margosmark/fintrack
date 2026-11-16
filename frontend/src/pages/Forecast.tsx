// M109 (docs/45) — /forecast: net-worth what-if scenarios over the recorded
// baseline.
//
// Three rules the page exists to keep honest:
//   * the four BOUNDARY strings (lib/forecastCopy) render unconditionally —
//     F15 asserts all four, so the page can never read as more certain than
//     the engine it describes;
//   * the future/today dollar toggle is a CLIENT-side switch between the two
//     series the run response already carries — F16 asserts the fetch count
//     does not move, because re-running the engine to read the same numbers
//     differently is exactly the kind of drift the spec forbids;
//   * money arrives as a Decimal serialised to a string and is DISPLAYED as
//     that string — except on a masked session, where every absolute figure
//     (end-value table, assumptions list, event rows) renders ••••• instead
//     (F-11, docs/46 §D2). The only Number() below feeds Recharts; every
//     figure a reader sees is the response's own string or the mask.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  CalendarDays,
  ChartLine,
  ChevronDown,
  ChevronUp,
  Copy,
  Info,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
import { ApiError } from "../api/client";
import {
  FORECAST_EVENT_TYPES,
  copyForecastScenario,
  createForecastScenario,
  deleteForecastScenario,
  getForecastDefaults,
  getForecastRun,
  listForecastScenarios,
  newForecastId,
  replaceForecastScenario,
} from "../api/forecast";
import type {
  ForecastEvent,
  ForecastGranularity,
  ForecastRun,
  ForecastRunParams,
  ForecastScenario,
  ForecastSeries,
} from "../api/forecast";
import { HONESTY_STRINGS, buildAssumptions } from "../lib/forecastCopy";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import { SkeletonRows } from "../components/ui/Skeleton";
import { useToast } from "../components/ui/Toast";
import { GlassTooltip } from "../components/ui/charts/ChartTooltip";
import { inputClass } from "../components/ui/formStyles";
import { ChartFrame } from "../components/charts/ChartFrame";
import { chartAriaLabel, dateRange, fromToFact } from "../components/charts/aria";
import { useChartChrome } from "../components/charts/chartDefaults";
import { fmtBucket, toNumber } from "../components/charts/format";
import { MASK_TEXT, useMasked } from "../lib/mask";
import { useChartPalette } from "../components/charts/palette";

/** The engine accepts at most six scenario ids per run (docs/45 §3.1). */
const MAX_OVERLAY = 6;
/** 0–12 events per scenario (docs/45 §2). */
const MAX_EVENTS = 12;
/** 25 scenarios per user (docs/45 §2). */
const MAX_SCENARIOS = 25;

/**
 * `aria.ts`'s `granularityLabel` has no `week` arm (it predates this page's
 * three grains), so a weekly run would silently describe itself as "monthly
 * buckets" in its own text alternative. Spelled out here instead.
 */
const BUCKET_KIND: Record<ForecastGranularity, string> = {
  day: "daily buckets",
  week: "weekly buckets",
  month: "monthly buckets",
};

type DollarMode = "future" | "real";

/** Which of the response's two series the chart is currently reading. */
function pickSeries(series: ForecastSeries, mode: DollarMode): string[] {
  return mode === "future" ? series.values : series.real_values;
}

/** ISO bucket-end → the day an event starts affecting the line. */
function eventDate(event: ForecastEvent): string {
  return event.type === "career_break" || event.type === "recurring_cost_delta"
    ? event.start_date
    : event.date;
}

function labelOf(type: ForecastEvent["type"]): string {
  return FORECAST_EVENT_TYPES.find((entry) => entry.type === type)?.label ?? type;
}

/**
 * The chart's text alternative — which, for assistive tech, IS the chart
 * (docs/46 §D2, F-9).
 *
 * Deliberately NOT a `useMemo`: its endpoint facts run through `formatCad`,
 * which reads the ambient mask at call time, so memoizing would freeze the
 * label at its mount state across a mask flip (masked-at-boot → an un-mask
 * never prints the figure back; un-masked-at-boot → a mask leaves
 * `$100,000.00 to $120,000.00` in the aria-label while every other surface
 * reads bullets). That is docs/46 §D6's "no useMemo around money" — a rule
 * `scripts/sweep_money.mjs` cannot enforce here, because the formatter call
 * sits behind `fromToFact` in another module.
 */
function chartLabelFor(data: ForecastRun | undefined, mode: DollarMode): string {
  if (data === undefined) return "Net worth forecast";
  const base = pickSeries(data.baseline, mode);
  const facts = [
    fromToFact(toNumber(base[0]), toNumber(base[base.length - 1])),
    ...data.scenarios.map((scenario) => {
      const values = pickSeries(scenario, mode);
      return fromToFact(toNumber(values[0]), toNumber(values[values.length - 1]), scenario.name);
    }),
  ];
  return chartAriaLabel({
    title: "Net worth forecast",
    kind: BUCKET_KIND[data.granularity],
    range: dateRange(data.buckets[0], data.buckets[data.buckets.length - 1]),
    points: data.buckets.length,
    facts,
  });
}

/**
 * One-line rendering of a stored event — every amount is the server's string,
 * or ••••• when the mask is on (F-11: this row is money in prose, and the
 * demo tenant seeds no scenarios, so the security sweep never rendered one).
 * Dates survive both ways — they are not balances.
 */
function eventSummary(event: ForecastEvent, masked: boolean): string {
  const label = labelOf(event.type);
  const money = (value: string): string => (masked ? MASK_TEXT : value);
  switch (event.type) {
    case "house_purchase":
      return `${label} ${event.date} — ${money(event.home_price_cad)} price, ${money(event.down_payment_cad)} down`;
    case "career_break":
      return `${label} ${event.start_date} → ${event.end_date}`;
    case "salary_change":
      return `${label} ${event.date} — ${money(event.monthly_delta_cad)}/month`;
    case "one_off_cash_event":
      return `${label} ${event.date} — ${money(event.amount_cad)}`;
    case "recurring_cost_delta":
      return `${label} ${event.start_date} — ${money(event.monthly_amount_cad)}/month${
        event.end_date !== undefined && event.end_date !== "" ? ` until ${event.end_date}` : ""
      }`;
  }
}

interface EventFieldSpec {
  name: string;
  label: string;
  kind: "date" | "text";
  required: boolean;
}

/**
 * The typed field set per event type (docs/45 §1.5 — a CLOSED set: the picker
 * offers these five shapes and their fields, nothing else is buildable).
 */
const EVENT_FIELDS: Record<ForecastEvent["type"], EventFieldSpec[]> = {
  house_purchase: [
    { name: "date", label: "Purchase date", kind: "date", required: true },
    { name: "home_price_cad", label: "Home price (CAD)", kind: "text", required: true },
    { name: "down_payment_cad", label: "Down payment (CAD)", kind: "text", required: true },
    { name: "mortgage_rate_pct", label: "Mortgage rate %", kind: "text", required: true },
    { name: "amortization_years", label: "Amortization (years)", kind: "text", required: true },
  ],
  career_break: [
    { name: "start_date", label: "Start date", kind: "date", required: true },
    { name: "end_date", label: "End date", kind: "date", required: true },
  ],
  salary_change: [
    { name: "date", label: "Effective date", kind: "date", required: true },
    { name: "monthly_delta_cad", label: "Monthly change (CAD)", kind: "text", required: true },
  ],
  one_off_cash_event: [
    { name: "date", label: "Date", kind: "date", required: true },
    { name: "amount_cad", label: "Amount (CAD)", kind: "text", required: true },
  ],
  recurring_cost_delta: [
    { name: "start_date", label: "Start date", kind: "date", required: true },
    { name: "monthly_amount_cad", label: "Monthly change (CAD)", kind: "text", required: true },
    // Nullable on the wire — an open-ended cost delta has no end date.
    { name: "end_date", label: "End date (optional)", kind: "date", required: false },
  ],
};

/** Untouched fields fall back to this seed rather than to an empty box. */
function seedValue(field: EventFieldSpec, defaultDate: string): string {
  if (field.kind === "date") return defaultDate;
  if (field.name === "mortgage_rate_pct") return "5.00";
  if (field.name === "amortization_years") return "25";
  return "";
}

function clampNumber(value: number, min: number, max: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

/**
 * Bounds-clamp a control string into the decimal the API expects (docs/45
 * §3.1). Inflation and return are rates, not money, so parsing them is the
 * point — but a blank or unparseable box falls back to the documented default
 * instead of sending a value the server would 422.
 */
function boundedDecimal(raw: string, min: number, max: number, fallback: string): string {
  const trimmed = raw.trim();
  if (trimmed === "") return fallback;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return fallback;
  return clampNumber(parsed, min, max, Number(fallback)).toFixed(2);
}

/** The `{detail, code}` envelope, spelled out — a rejected bound must name
 * WHICH bound it broke rather than collapse to a generic failure line. */
function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.code === null ? error.message : `${error.message} (${error.code})`;
  }
  return error instanceof Error ? error.message : "Request failed";
}

interface EventDraft {
  scenarioId: string;
  type: ForecastEvent["type"];
  fields: Record<string, string>;
}

export function Forecast() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const palette = useChartPalette();
  const chrome = useChartChrome();
  // F-11 (docs/46 §D6): the end-value table, the assumptions list and the
  // event rows all branch on THIS render's mask, so the page subscribes
  // itself. `useChartChrome()` subscribes too, but a money surface may not
  // rely on an incidental re-render from a sibling hook — that is the M140 bug
  // shape. Deliberately NOT wrapped in useMemo/useCallback: the D6 sweep bans
  // money built inside either.
  const masked = useMasked();

  // Reading, not running: both dollar readings arrive in ONE response, so the
  // toggle lives outside the query key on purpose (F16).
  const [dollarMode, setDollarMode] = useState<DollarMode>("future");
  const [horizon, setHorizon] = useState(24);
  const [granularity, setGranularity] = useState<ForecastGranularity>("day");
  const [inflationInput, setInflationInput] = useState("2.00");
  const [returnInput, setReturnInput] = useState("0.00");
  const [flowWindow, setFlowWindow] = useState(12);
  const [included, setIncluded] = useState<string[]>([]);
  const [assumptionsOpen, setAssumptionsOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [newName, setNewName] = useState("");
  // One editor open at a time — the field state is shared, so a second
  // scenario's picker would silently re-target the first's draft.
  const [draft, setDraft] = useState<EventDraft | null>(null);

  const defaults = useQuery({
    queryKey: ["forecast-defaults"],
    queryFn: getForecastDefaults,
  });

  const scenariosQuery = useQuery({
    queryKey: ["forecast-scenarios"],
    queryFn: listForecastScenarios,
  });

  const inflation = boundedDecimal(inflationInput, 0, 20, "2.00");
  const annualReturn = boundedDecimal(returnInput, -50, 50, "0.00");
  const runParams: ForecastRunParams = {
    scenario_ids: included,
    horizon_months: horizon,
    granularity,
    inflation_pct: inflation,
    annual_return_pct: annualReturn,
    flow_window_months: flowWindow,
  };

  const run = useQuery({
    queryKey: ["forecast-run", runParams],
    queryFn: () => getForecastRun(runParams),
  });

  const scenarios = scenariosQuery.data?.items ?? [];
  const defaultDate = defaults.data?.as_of ?? "";

  /** Scenario writes touch BOTH read models: the list and the overlay. */
  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["forecast-scenarios"] });
    void queryClient.invalidateQueries({ queryKey: ["forecast-run"] });
  }

  const create = useMutation({
    mutationFn: (name: string) =>
      createForecastScenario({ id: newForecastId(), name, events: [] }),
    onSuccess: (scenario) => {
      invalidate();
      toast.success("Scenario created", scenario.name);
    },
    onError: (error) => toast.error("Could not create scenario", describeError(error)),
  });

  const copy = useMutation({
    mutationFn: (scenario: ForecastScenario) =>
      copyForecastScenario(scenario.id, { name: `${scenario.name} (copy)` }),
    onSuccess: (scenario) => {
      invalidate();
      toast.success("Scenario copied", scenario.name);
    },
    onError: (error) => toast.error("Could not copy scenario", describeError(error)),
  });

  const rename = useMutation({
    mutationFn: (input: { scenario: ForecastScenario; name: string }) =>
      replaceForecastScenario(input.scenario.id, { ...input.scenario, name: input.name }),
    onSuccess: () => {
      setRenamingId(null);
      invalidate();
    },
    onError: (error) => toast.error("Could not rename scenario", describeError(error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteForecastScenario(id),
    onSuccess: (_data: void, id: string) => {
      // A deleted scenario can no longer be part of the overlay's id list.
      setIncluded((current) => current.filter((entry) => entry !== id));
      if (renamingId === id) setRenamingId(null);
      if (draft?.scenarioId === id) setDraft(null);
      invalidate();
    },
    onError: (error) => toast.error("Could not delete scenario", describeError(error)),
  });

  const saveEvents = useMutation({
    mutationFn: (input: { scenario: ForecastScenario; events: ForecastEvent[] }) =>
      replaceForecastScenario(input.scenario.id, { ...input.scenario, events: input.events }),
    onSuccess: () => invalidate(),
    onError: (error) => toast.error("Could not save the event", describeError(error)),
  });

  function createScenario() {
    const suggested = `Scenario ${scenarios.length + 1}`;
    create.mutate(newName.trim() === "" ? suggested : newName.trim());
    setNewName("");
  }

  function startRename(scenario: ForecastScenario) {
    setRenamingId(scenario.id);
    setRenameValue(scenario.name);
  }

  function toggleIncluded(id: string) {
    setIncluded((current) =>
      current.includes(id)
        ? current.filter((entry) => entry !== id)
        : current.length >= MAX_OVERLAY
          ? current
          : [...current, id],
    );
  }

  function openDraft(scenario: ForecastScenario) {
    setDraft({ scenarioId: scenario.id, type: "one_off_cash_event", fields: {} });
  }

  function resolvedField(field: EventFieldSpec): string {
    if (draft === null) return "";
    return draft.fields[field.name] ?? seedValue(field, defaultDate);
  }

  function draftIsComplete(): boolean {
    if (draft === null) return false;
    return EVENT_FIELDS[draft.type].every(
      (field) => !field.required || resolvedField(field).trim() !== "",
    );
  }

  function addDraftEvent(scenario: ForecastScenario) {
    if (draft === null || !draftIsComplete()) return;
    const read = (name: string) => {
      const spec = EVENT_FIELDS[draft.type].find((field) => field.name === name);
      return spec === undefined ? "" : resolvedField(spec).trim();
    };
    const id = newForecastId("ev");
    let event: ForecastEvent;
    switch (draft.type) {
      case "house_purchase":
        event = {
          id,
          type: "house_purchase",
          date: read("date"),
          home_price_cad: read("home_price_cad"),
          down_payment_cad: read("down_payment_cad"),
          mortgage_rate_pct: read("mortgage_rate_pct"),
          amortization_years: Number(read("amortization_years")),
        };
        break;
      case "career_break":
        event = { id, type: "career_break", start_date: read("start_date"), end_date: read("end_date") };
        break;
      case "salary_change":
        event = { id, type: "salary_change", date: read("date"), monthly_delta_cad: read("monthly_delta_cad") };
        break;
      case "one_off_cash_event":
        event = { id, type: "one_off_cash_event", date: read("date"), amount_cad: read("amount_cad") };
        break;
      case "recurring_cost_delta":
        event = {
          id,
          type: "recurring_cost_delta",
          start_date: read("start_date"),
          monthly_amount_cad: read("monthly_amount_cad"),
          ...(read("end_date") === "" ? {} : { end_date: read("end_date") }),
        };
        break;
    }
    saveEvents.mutate({ scenario, events: [...scenario.events, event] });
    setDraft(null);
  }

  // ---- chart -----------------------------------------------------------------

  const chartRows = useMemo(() => {
    const data = run.data;
    if (data === undefined) return [];
    return data.buckets.map((bucket, index) => {
      const row: Record<string, number | string> = { bucket };
      // Number() HERE ONLY: Recharts plots numbers. Every figure printed in
      // the DOM further down stays the exact string the server sent.
      row.baseline = toNumber(pickSeries(data.baseline, dollarMode)[index]);
      for (const scenario of data.scenarios) {
        row[`sc:${scenario.id}`] = toNumber(pickSeries(scenario, dollarMode)[index]);
      }
      return row;
    });
  }, [run.data, dollarMode]);

  // Rebuilt every render — see `chartLabelFor`: the money in it must answer a
  // mask flip on the pass the flip causes (docs/46 §D6).
  const chartLabel = chartLabelFor(run.data, dollarMode);

  /** Events that land past the last rendered bucket — a neutral heads-up. */
  const beyondHorizon = useMemo(() => {
    const data = run.data;
    if (data === undefined || data.buckets.length === 0) return [];
    const end = data.buckets[data.buckets.length - 1];
    const found: string[] = [];
    for (const scenario of scenarios) {
      for (const event of scenario.events) {
        const date = eventDate(event);
        if (date > end) found.push(`${scenario.name} — ${labelOf(event.type)} ${date}`);
      }
    }
    return found;
  }, [run.data, scenarios]);

  const asOf = run.data?.as_of ?? defaults.data?.as_of ?? null;
  const horizonEnd = run.data?.buckets[run.data.buckets.length - 1] ?? null;
  const assumptionLines =
    run.data !== undefined
      ? buildAssumptions(
          run.data.inputs,
          {
            reporting_currency: run.data.reporting_currency,
            inflation_pct: run.data.inflation_pct,
            annual_return_pct: run.data.annual_return_pct,
            flow_window_months: run.data.flow_window_months,
          },
          masked,
        )
      : [];

  return (
    <section className="space-y-6">
      {/* Header: who this is for and what it is NOT — the four boundary
          strings are load-bearing copy, so they render whether or not the
          queries ever resolve (F15). */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Forecast</h1>
          <p className="flex items-center gap-1.5 text-sm text-muted">
            <CalendarDays className="size-4" aria-hidden />
            {asOf !== null ? (
              <span>
                As of {asOf} · {horizon}-month horizon{horizonEnd !== null ? ` through ${horizonEnd}` : ""}
              </span>
            ) : (
              <span>Loading your baseline…</span>
            )}
            {asOf === null && defaults.isError && (
              <span>Baseline unavailable — {describeError(defaults.error)}</span>
            )}
          </p>
        </div>
        <ul className="flex flex-wrap gap-2">
          {HONESTY_STRINGS.map((line) => (
            <li
              key={line}
              className="max-w-full rounded-lg border border-border px-2.5 py-1 text-xs text-text [overflow-wrap:anywhere]"
            >
              {line}
            </li>
          ))}
        </ul>
      </div>

      {/* Run settings. Bounds are clamped here, so a half-typed box can never
          put an out-of-range value on the wire (the server's 422 is still
          rendered as an ErrorPanel if it ever arrives). */}
      <Card className="p-4">
        <CardTitle>Run settings</CardTitle>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <p className="mb-1.5 text-xs uppercase tracking-wide text-muted">Dollars</p>
            <SegmentedControl
              ariaLabel="Dollars"
              layoutId="forecast-dollars"
              value={dollarMode}
              onChange={setDollarMode}
              options={[
                { value: "future", label: "Future dollars" },
                { value: "real", label: "Today's dollars" },
              ]}
            />
          </div>
          <div>
            <p className="mb-1.5 text-xs uppercase tracking-wide text-muted">Granularity</p>
            <SegmentedControl
              ariaLabel="Granularity"
              layoutId="forecast-granularity"
              value={granularity}
              onChange={setGranularity}
              options={[
                { value: "day", label: "Day" },
                { value: "week", label: "Week" },
                { value: "month", label: "Month" },
              ]}
            />
          </div>
          <label className="block text-sm text-muted">
            Horizon (months): <span className="font-medium text-text">{horizon}</span>
            <input
              type="range"
              min={1}
              max={120}
              value={horizon}
              onChange={(event) =>
                setHorizon(clampNumber(Math.trunc(Number(event.target.value)), 1, 120, 24))
              }
              className="mt-3 block w-full accent-accent"
            />
          </label>
          <label className="block text-sm text-muted">
            Flow window (months)
            <select
              value={flowWindow}
              onChange={(event) => setFlowWindow(Number(event.target.value))}
              className={`${inputClass} mt-1 block w-full`}
            >
              <option value={6}>6</option>
              <option value={12}>12</option>
            </select>
          </label>
          <label className="block text-sm text-muted">
            Inflation % (0–20)
            <input
              value={inflationInput}
              onChange={(event) => setInflationInput(event.target.value)}
              onBlur={() => setInflationInput((current) => boundedDecimal(current, 0, 20, "2.00"))}
              inputMode="decimal"
              className={`${inputClass} mt-1 block w-full`}
            />
            {inflationInput.trim() !== "" && inflationInput.trim() !== inflation && (
              <span className="mt-1 block text-xs text-muted">Using {inflation}%</span>
            )}
          </label>
          <label className="block text-sm text-muted">
            Flat annual return % (−50–50)
            <input
              value={returnInput}
              onChange={(event) => setReturnInput(event.target.value)}
              onBlur={() => setReturnInput((current) => boundedDecimal(current, -50, 50, "0.00"))}
              inputMode="decimal"
              className={`${inputClass} mt-1 block w-full`}
            />
            {returnInput.trim() !== "" && returnInput.trim() !== annualReturn && (
              <span className="mt-1 block text-xs text-muted">Using {annualReturn}%</span>
            )}
          </label>
        </div>
      </Card>

      {run.isError && <ErrorPanel message={describeError(run.error)} />}

      {/* No card at all on a failed run — an empty "Net worth" shell beside
          the ErrorPanel would read as "no data" rather than "no answer". */}
      {!run.isError && (
        <Card className="p-4">
          <CardTitle>
            Net worth — {dollarMode === "future" ? "future dollars" : "today's dollars"}
          </CardTitle>
          {run.isPending ? (
            <SkeletonRows rows={4} />
          ) : run.data !== undefined && (
            <>
              <ChartFrame label={chartLabel}>
                <ResponsiveContainer width="100%" height={340}>
                  <LineChart data={chartRows} margin={{ top: 8, right: 8 }}>
                    <CartesianGrid stroke={palette.GRID_STROKE} vertical={false} />
                    <XAxis
                      dataKey="bucket"
                      tick={palette.AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      interval="preserveStartEnd"
                      minTickGap={48}
                      tickFormatter={(bucket: string) => fmtBucket(bucket, granularity)}
                    />
                    <YAxis
                      tick={palette.AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      width={80}
                      domain={["auto", "auto"]}
                      tickFormatter={chrome.moneyAxis}
                    />
                    <Tooltip
                      content={<GlassTooltip />}
                      cursor={{ stroke: palette.CURSOR_STROKE }}
                    />
                    <Line
                      dataKey="baseline"
                      name="Baseline"
                      type="monotone"
                      stroke={palette.SERIES_COLORS[0]}
                      strokeWidth={2}
                      dot={false}
                      {...chrome.anim}
                      animationDuration={600}
                    />
                    {run.data.scenarios.map((scenario, index) => (
                      <Line
                        key={scenario.id}
                        dataKey={`sc:${scenario.id}`}
                        name={scenario.name}
                        type="monotone"
                        stroke={
                          palette.SERIES_COLORS[(index + 1) % palette.SERIES_COLORS.length]
                        }
                        strokeWidth={2}
                        dot={false}
                        {...chrome.anim}
                        animationDuration={600}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </ChartFrame>

              {beyondHorizon.length > 0 && (
                <p className="mt-2 text-xs text-muted">
                  After {horizonEnd}: {beyondHorizon.join(" · ")} — raise the horizon to see{" "}
                  {beyondHorizon.length === 1 ? "it" : "them"} on the chart.
                </p>
              )}

              {/* End values are printed as the response's own strings — or as
                  ••••• while the mask is on (F-11: this table was left raw
                  when the axis above it was fixed, so a masked session read
                  bullets over a table of dollars); the delta column is
                  labelled for what it is — the server reports it in future
                  dollars, whatever the chart above is currently showing. */}
              <div
                role="region"
                aria-label="End value per series"
                tabIndex={0}
                className="mt-4 overflow-x-auto"
              >
                <table className="w-full min-w-[420px] text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                      <th className="py-2 pr-4">Series</th>
                      <th className="py-2 pr-4">
                        End value ({dollarMode === "future" ? "future" : "today's"} dollars)
                      </th>
                      <th className="py-2">Δ vs baseline (future dollars)</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr className="border-b border-border last:border-0">
                      <td className="py-2 pr-4 font-medium text-text">Baseline</td>
                      <td className="py-2 pr-4 tabular">
                        {masked
                          ? MASK_TEXT
                          : (pickSeries(run.data.baseline, dollarMode).at(-1) ?? "—")}
                      </td>
                      <td className="py-2 tabular">
                        {masked ? MASK_TEXT : run.data.baseline.delta_cad}
                      </td>
                    </tr>
                    {run.data.scenarios.map((scenario) => (
                      <tr key={scenario.id} className="border-b border-border last:border-0">
                        <td className="py-2 pr-4 font-medium text-text">{scenario.name}</td>
                        <td className="py-2 pr-4 tabular">
                          {masked
                            ? MASK_TEXT
                            : (pickSeries(scenario, dollarMode).at(-1) ?? "—")}
                        </td>
                        <td className="py-2 tabular">
                          {masked ? MASK_TEXT : scenario.delta_cad}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Card>
      )}

      {/* Scenarios */}
      <Card className="p-4">
        <CardTitle
          right={
            <span className="text-xs text-muted">
              {scenarios.length} of {MAX_SCENARIOS}
            </span>
          }
        >
          Scenarios
        </CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
            aria-label="New scenario name"
            placeholder="Scenario name"
            maxLength={80}
            className={`${inputClass} w-56`}
          />
          <Button variant="primary" size="sm" onClick={createScenario} loading={create.isPending}>
            <Plus className="size-4" aria-hidden />
            New scenario
          </Button>
          <span className="text-xs text-muted">
            {included.length} of {MAX_OVERLAY} on the chart
          </span>
        </div>

        {scenariosQuery.isPending && <SkeletonRows rows={2} />}

        {!scenariosQuery.isPending && scenarios.length === 0 && (
          <EmptyState
            icon={<ChartLine />}
            title="No scenarios yet"
            hint="A scenario is a handful of events — a house purchase, a career break, a salary change — laid over your baseline so you can see what it does to net worth."
            preview={{
              src: "/help/reports.png",
              alt: "The Reports page, whose projection card links to these what-if scenarios",
            }}
            action={{ label: "Create a scenario", onClick: createScenario }}
          />
        )}

        <div className="mt-4 space-y-4">
          {scenarios.map((scenario) => {
            const checked = included.includes(scenario.id);
            const blocked = !checked && included.length >= MAX_OVERLAY;
            const editing = draft?.scenarioId === scenario.id;
            return (
              <div key={scenario.id} className="rounded-lg border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    type="checkbox"
                    className="size-4 accent-accent"
                    checked={checked}
                    disabled={blocked}
                    onChange={() => toggleIncluded(scenario.id)}
                    aria-label={`Overlay ${scenario.name}`}
                  />
                  {renamingId === scenario.id ? (
                    <>
                      <input
                        value={renameValue}
                        onChange={(event) => setRenameValue(event.target.value)}
                        aria-label={`New name for ${scenario.name}`}
                        maxLength={80}
                        className={`${inputClass} w-44`}
                      />
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={renameValue.trim() === ""}
                        onClick={() =>
                          rename.mutate({ scenario, name: renameValue.trim() })
                        }
                      >
                        Save
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setRenamingId(null)}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <span className="min-w-0 flex-1 truncate font-medium text-text">
                      {scenario.name}
                    </span>
                  )}
                  {renamingId !== scenario.id && (
                    <>
                      <Button variant="ghost" size="sm" onClick={() => startRename(scenario)}>
                        <Pencil className="size-3.5" aria-hidden />
                        Rename
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => copy.mutate(scenario)}
                        loading={copy.isPending}
                      >
                        <Copy className="size-3.5" aria-hidden />
                        Copy
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => remove.mutate(scenario.id)}
                        loading={remove.isPending}
                        aria-label={`Delete ${scenario.name}`}
                      >
                        <Trash2 className="size-3.5" aria-hidden />
                        Delete
                      </Button>
                    </>
                  )}
                </div>

                <p className="mt-2 text-xs text-muted">
                  {scenario.events.length} of {MAX_EVENTS} events
                </p>
                {scenario.events.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {scenario.events.map((event) => (
                      <li
                        key={event.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-veil/[0.04] px-2.5 py-1.5 text-sm"
                      >
                        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
                          {eventSummary(event, masked)}
                        </span>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Remove ${eventSummary(event, masked)}`}
                          onClick={() =>
                            saveEvents.mutate({
                              scenario,
                              events: scenario.events.filter((entry) => entry.id !== event.id),
                            })
                          }
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}

                {editing && draft !== null ? (
                  <div className="mt-3 rounded-lg border border-border p-3">
                    <label className="block text-sm text-muted">
                      Event type
                      <select
                        value={draft.type}
                        onChange={(event) =>
                          setDraft({
                            scenarioId: scenario.id,
                            type: event.target.value as ForecastEvent["type"],
                            fields: {},
                          })
                        }
                        className={`${inputClass} mt-1 block w-full`}
                      >
                        {FORECAST_EVENT_TYPES.map((entry) => (
                          <option key={entry.type} value={entry.type}>
                            {entry.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="mt-3 grid gap-3 sm:grid-cols-2">
                      {EVENT_FIELDS[draft.type].map((field) => (
                        <label key={field.name} className="block text-sm text-muted">
                          {field.label}
                          <input
                            type={field.kind}
                            value={resolvedField(field)}
                            onChange={(event) =>
                              setDraft({
                                ...draft,
                                fields: { ...draft.fields, [field.name]: event.target.value },
                              })
                            }
                            inputMode={field.kind === "date" ? undefined : "decimal"}
                            className={`${inputClass} mt-1 block w-full`}
                          />
                        </label>
                      ))}
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={!draftIsComplete() || scenario.events.length >= MAX_EVENTS}
                        onClick={() => addDraftEvent(scenario)}
                      >
                        <Plus className="size-4" aria-hidden />
                        Add event
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
                        Cancel
                      </Button>
                      {scenario.events.length >= MAX_EVENTS && (
                        <span className="text-xs text-muted">
                          {MAX_EVENTS}-event limit reached
                        </span>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="mt-3">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={scenario.events.length >= MAX_EVENTS}
                      onClick={() => openDraft(scenario)}
                    >
                      <Plus className="size-4" aria-hidden />
                      Add event
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      {/* Assumptions — collapsed by default so the chart leads, but one press
          away and never hidden behind a tooltip. */}
      <Card className="p-4">
        <button
          type="button"
          aria-expanded={assumptionsOpen}
          aria-controls="forecast-assumptions"
          onClick={() => setAssumptionsOpen((open) => !open)}
          className="flex w-full items-center justify-between gap-3 rounded-lg px-1 py-0.5 text-left text-sm font-semibold text-text outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span className="flex items-center gap-2">
            <Info className="size-4 text-muted" aria-hidden />
            Assumptions
          </span>
          {assumptionsOpen ? (
            <ChevronUp className="size-4 text-muted" aria-hidden />
          ) : (
            <ChevronDown className="size-4 text-muted" aria-hidden />
          )}
        </button>
        {/* The container is always in the DOM so `aria-controls` always
            resolves; `hidden` keeps it out of the accessibility tree while
            collapsed (jsdom/axe both read the attribute, not the CSS). */}
        <div id="forecast-assumptions" className="mt-3 space-y-1.5" hidden={!assumptionsOpen}>
          {assumptionLines.length === 0 ? (
            <p className="text-sm text-muted">Run the forecast to list its assumptions.</p>
          ) : (
            <ul className="space-y-1.5">
              {assumptionLines.map((line) => (
                <li
                  key={line}
                  className="text-sm text-muted [overflow-wrap:anywhere]"
                >
                  {line}
                </li>
              ))}
            </ul>
          )}
          {run.data?.disclaimer !== undefined && (
            <p className="text-sm font-medium text-text">{run.data.disclaimer}</p>
          )}
        </div>
      </Card>

      <p className="text-xs text-muted">
        <Link
          to="/reports"
          className="rounded text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          Back to Reports
        </Link>
      </p>
    </section>
  );
}
