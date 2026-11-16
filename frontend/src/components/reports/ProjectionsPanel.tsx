import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useInRouterContext } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartLine, Trash2 } from "lucide-react";
import { Card, CardTitle } from "../ui/Card";
import { Button } from "../ui/Button";
import { EmptyState, ErrorPanel } from "../ui/EmptyState";
import { isRetryableError, useRetryAction } from "../../lib/retry";
import { useToast } from "../ui/Toast";
import { useChartPalette } from "../charts/palette";
import { useChartChrome } from "../charts/chartDefaults";
import { fmtCad, toNumber } from "../charts/format";
import { GlassTooltip } from "../ui/charts/ChartTooltip";
import { inputClass } from "../ui/formStyles";
import { getProjectionDefaults, runProjections } from "../../api/projections";
import type {
  ProjectionRequest,
  ProjectionResponse,
  ProjectionScenarioResult,
} from "../../api/projections";
import { ApiError } from "../../api/client";
import { isReadOnlySession } from "../../auth/readOnlySession";
import { useAuth } from "../../auth/AuthContext";
import { ProjectionInputError } from "../../lib/projectionEngine";
import { useMasked } from "../../lib/mask";

// Preset return scenarios (M15): client constants, inserted-or-replaced by
// label so repeated clicks never duplicate a chip.
const PRESET_SCENARIOS = [
  { label: "Conservative", annual_return_pct: "4" },
  { label: "Balanced", annual_return_pct: "6.5" },
  { label: "Aggressive", annual_return_pct: "9" },
] as const;

// M157: the server owns the bounds now (app/schemas/projections.py Field
// constraints), and its 422 `detail` is a pydantic-shaped LIST, not a
// sentence. This is the copy the ErrorPanel shows for one — every bound in
// one place, so the panel never prints a status text instead of a reason.
const OUT_OF_RANGE_COPY =
  "An input is outside the allowed range: years 1–50, inflation 0–20%, " +
  "annual return −50 to 50%, and 1–8 scenarios with 1–40 character labels.";

/**
 * M157: which sentence a failed run renders (docs/12 §7).
 *
 * The run is server-computed, so the failure is the server's envelope. A
 * `ProjectionInputError` still carries its own precise copy for the offline /
 * E2EE path where `lib/projectionEngine.ts` runs the math locally, and a
 * `validation_error` 422 from the endpoint maps to the bounds copy above.
 * Everything else — a network failure, a 500 — keeps the generic line.
 */
function runErrorMessage(err: unknown): string {
  if (err instanceof ProjectionInputError) return err.message;
  if (err instanceof ApiError && err.code === "validation_error") return OUT_OF_RANGE_COPY;
  return "Something went wrong";
}

// Alias the shared base so existing usages (which append width modifiers) are
// unchanged. Adds text-text + placeholder styling (harmless on these inputs).
const INPUT_CLASS = inputClass;

interface EditableScenario {
  label: string;
  annual_return_pct: string;
}

/** Chart rows merge every scenario's nominal series plus the FIRST
 * scenario's real-value series (rendered dashed). */
function buildChartData(scenarios: ProjectionScenarioResult[]) {
  const rows: Record<string, number>[] = [];
  const realKey = scenarios[0] !== undefined ? `${scenarios[0].label} (real)` : "";
  scenarios.forEach((scenario, index) => {
    scenario.series.forEach((point, i) => {
      if (rows[i] === undefined) rows[i] = { year: point.year };
      rows[i][scenario.label] = toNumber(point.value_cad);
      if (index === 0) rows[i][realKey] = toNumber(point.value_real_cad);
    });
  });
  return { rows, realKey };
}

export function ProjectionsPanel() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { user } = useAuth();
  const toast = useToast();
  const palette = useChartPalette();
  // M109: the entry point to /forecast has to survive a router-less mount —
  // see the link at the bottom of the render.
  const inRouter = useInRouterContext();
  // Reduced-motion switch for the <Line> primitives below (docs/09
  // "Reduced-motion contract" layer 4 — every Recharts primitive spreads
  // `chrome.anim` so nothing tweens under prefers-reduced-motion).
  const chrome = useChartChrome();

  const defaults = useQuery({
    queryKey: ["projection-defaults"],
    queryFn: getProjectionDefaults,
  });

  const [startValue, setStartValue] = useState("");
  const [contribution, setContribution] = useState("1200");
  const [years, setYears] = useState(25);
  const [inflation, setInflation] = useState("2");
  // Start empty; the defaults prefill lands once, only while untouched.
  const [scenarios, setScenarios] = useState<EditableScenario[]>([
    { label: "Balanced", annual_return_pct: "6.5" },
  ]);
  const [prefilled, setPrefilled] = useState(false);

  useEffect(() => {
    if (!prefilled && defaults.data?.start_value_cad !== undefined) {
      setPrefilled(true);
      if (defaults.data.start_value_cad !== null) {
        setStartValue(defaults.data.start_value_cad);
      }
    }
  }, [defaults.data, prefilled]);

  // M157 (M135 §2 K-12): the run is SERVER-computed — `POST /projections/run`
  // is the source of truth for every tenant, the demo one included (it is a
  // documented demo-write exemption now: pure request-body math, no row is
  // touched). `lib/projectionEngine.ts` stays as the offline/E2EE fallback
  // that M160 will rule on (docs/33 §Server-first rule 5), so this panel
  // stopped running it but nothing was deleted.
  const [result, setResult] = useState<ProjectionResponse | undefined>(undefined);
  const [runError, setRunError] = useState<string | null>(null);
  // F-RETRY: the raw rejection, kept beside its human copy so the panel can
  // tell a transient failure (a repeat is honest) from a policy refusal or a
  // validation error (no button) — the rendered string carries no `code`.
  const [runFailure, setRunFailure] = useState<unknown>(null);
  const [running, setRunning] = useState(false);

  function applyPreset(preset: (typeof PRESET_SCENARIOS)[number]) {
    // Insert-or-replace by label (M15 contract): an active chip re-applies
    // its preset values, it never removes the row — the list only shrinks
    // via the per-row remove buttons.
    setScenarios((current) =>
      current.some((s) => s.label === preset.label)
        ? current.map((s) => (s.label === preset.label ? { ...preset } : s))
        : [...current, { ...preset }],
    );
  }

  function updateScenario(index: number, patch: Partial<EditableScenario>) {
    setScenarios((current) =>
      current.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    );
  }

  async function submit() {
    const payload: ProjectionRequest = {
      start_value_cad: startValue === "" ? "0" : startValue,
      annual_contribution_cad: contribution === "" ? "0" : contribution,
      years,
      inflation_pct: inflation === "" ? "0" : inflation,
      scenarios: scenarios.map((s) => ({
        label: s.label.trim(),
        annual_return_pct: s.annual_return_pct,
      })),
    };
    // The run is a real round trip now, so a second click while one is in
    // flight would post the same payload twice and swap the chart out from
    // under the first answer.
    setRunning(true);
    try {
      setResult(await runProjections(payload));
      setRunError(null);
      setRunFailure(null);
      // Nothing is ever persisted for a read-only session — say so with a
      // non-blocking toast WHILE the results are on screen (M61 #2: never let
      // a write boundary hide the calculation).
      if (isReadOnlySession(user)) toast.info("Demo is read-only — scenario not saved");
    } catch (err) {
      setResult(undefined);
      setRunError(runErrorMessage(err));
      setRunFailure(err);
    } finally {
      setRunning(false);
    }
  }
  // F-RETRY: the run is a real POST whose payload is derived from the inputs
  // still on screen, so a transient failure has an honest re-run — `submit()`
  // again. `useRetryAction` suppresses the button for a refusal (`code` in
  // `NON_RETRYABLE_CODES`) or any deterministic 4xx, exactly as on a query.
  const retryRun = useRetryAction({
    retry: runFailure !== null && isRetryableError(runFailure) ? () => void submit() : null,
    pending: running,
  });

  const chartData =
    result !== undefined && result.scenarios.length > 0
      ? buildChartData(result.scenarios)
      : null;

  return (
    <div className="space-y-6">
      <Card className="p-4">
        <CardTitle>Projection inputs</CardTitle>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="block text-sm text-muted">
            Start value (CAD)
            <input
              value={startValue}
              onChange={(e) => setStartValue(e.target.value)}
              inputMode="decimal"
              placeholder={defaults.isPending ? "Loading…" : "0"}
              className={`${INPUT_CLASS} mt-1 block w-full`}
            />
          </label>
          <label className="block text-sm text-muted">
            Annual contribution (CAD)
            <input
              value={contribution}
              onChange={(e) => setContribution(e.target.value)}
              inputMode="decimal"
              className={`${INPUT_CLASS} mt-1 block w-full`}
            />
          </label>
          <label className="block text-sm text-muted">
            Years: <span className="font-medium text-text">{years}</span>
            <input
              type="range"
              min={1}
              max={50}
              value={years}
              onChange={(e) => setYears(Number(e.target.value))}
              className="mt-3 block w-full accent-accent"
            />
          </label>
          <label className="block text-sm text-muted">
            Inflation %
            <input
              value={inflation}
              onChange={(e) => setInflation(e.target.value)}
              inputMode="decimal"
              className={`${INPUT_CLASS} mt-1 block w-full`}
            />
          </label>
        </div>

        <div className="mt-4">
          <p className="mb-1.5 text-xs uppercase tracking-wide text-muted">Scenarios</p>
          <div className="flex flex-wrap items-center gap-2">
            {PRESET_SCENARIOS.map((preset) => {
              const active = scenarios.some((s) => s.label === preset.label);
              return (
                <Button
                  key={preset.label}
                  variant={active ? "accent-soft" : "outline"}
                  size="sm"
                  onClick={() => applyPreset(preset)}
                >
                  {preset.label} · {preset.annual_return_pct}%
                </Button>
              );
            })}
          </div>
          <div className="mt-3 space-y-2">
            {scenarios.map((scenario, index) => (
              // M82-F4: this row is 396px of fixed-width controls inside a
              // 286px card column at 360 (248px at 320), so a nowrap row threw
              // the "% / yr" unit and the remove button past the right edge —
              // main#main-content clips (overflow-x: clip) and the page has no
              // horizontal scroll, so the delete was genuinely unreachable.
              // flex-wrap lets the row break onto a second line instead: at
              // 360 the label input stays on line 1 and the return input +
              // unit + remove button wrap to line 2; at >=468 everything still
              // fits on one line exactly as before.
              <div key={index} className="flex flex-wrap items-center gap-2">
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ backgroundColor: palette.SERIES_COLORS[index % palette.SERIES_COLORS.length] }}
                  aria-hidden
                />
                <input
                  value={scenario.label}
                  onChange={(e) => updateScenario(index, { label: e.target.value })}
                  maxLength={40}
                  aria-label="Scenario label"
                  className={`${INPUT_CLASS} w-44`}
                />
                <input
                  value={scenario.annual_return_pct}
                  onChange={(e) =>
                    updateScenario(index, { annual_return_pct: e.target.value })
                  }
                  inputMode="decimal"
                  aria-label="Annual return percent"
                  className={`${INPUT_CLASS} w-24`}
                />
                <span className="text-xs text-muted">% / yr</span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    setScenarios((current) => current.filter((_, i) => i !== index))
                  }
                  aria-label={`Remove ${scenario.label || "scenario"}`}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            ))}
          </div>
          {scenarios.length === 0 && (
            <p className="mt-2 text-xs text-muted">
              Add at least one scenario (preset chip or custom row) to run.
            </p>
          )}
        </div>

        <div className="mt-4 flex items-center gap-3">
          <Button
            variant="primary"
            disabled={scenarios.length === 0 || running}
            onClick={() => void submit()}
          >
            {running ? "Running…" : "Run projections"}
          </Button>
          {/* M61 #2: the read-only note used to render TWICE on failure (an
              inline copy beside this button plus the ErrorPanel below). The
              only read-only message left is the toast — at most one per page,
              by construction — and a failed run has exactly one ErrorPanel. */}
        </div>
      </Card>

      {/* M109 (docs/45): the what-if surface grew out of this card, so the
          door to it lives here — the card's own controls are untouched. The
          panel is also mounted bare by its tests (no router), so the entry
          point renders as a plain anchor there and as an SPA link inside the
          app — the same rule EmptyState's deep-link action follows. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted">
          Same inputs, same output — laid over life events instead of returns.
        </p>
        {inRouter ? (
          <Link
            to="/forecast"
            className="inline-flex min-h-11 items-center rounded-lg px-3 text-sm font-medium text-accent outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
          >
            Open what-if scenarios
          </Link>
        ) : (
          <a
            href="/forecast"
            className="inline-flex min-h-11 items-center rounded-lg px-3 text-sm font-medium text-accent outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
          >
            Open what-if scenarios
          </a>
        )}
      </div>

      {runError !== null && (
        <ErrorPanel
          message={runError}
          onRetry={retryRun.onRetry}
          retrying={retryRun.retrying}
        />
      )}

      {result !== undefined && chartData !== null && (
        <>
          <Card className="p-4">
            <CardTitle>Projected value over {result.years} years</CardTitle>
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={chartData.rows} margin={{ top: 8 }}>
                <CartesianGrid stroke={palette.GRID_STROKE} vertical={false} />
                <XAxis
                  dataKey="year"
                  tick={palette.AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(year: number) => `Y${year}`}
                />
                <YAxis
                  tick={palette.AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  width={80}
                  domain={["auto", "auto"]}
                  tickFormatter={chrome.moneyAxis}
                />
                <Tooltip content={<GlassTooltip />} cursor={{ stroke: palette.CURSOR_STROKE }} />
                {result.scenarios.map((scenario, index) => (
                  <Line
                    key={scenario.label}
                    dataKey={scenario.label}
                    name={scenario.label}
                    type="monotone"
                    stroke={
                      palette.SERIES_COLORS[index % palette.SERIES_COLORS.length]
                    }
                    strokeWidth={2}
                    dot={false}
                    {...chrome.anim}
                    animationDuration={600}
                  />
                ))}
                {chartData.realKey !== "" && (
                  <Line
                    dataKey={chartData.realKey}
                    name={chartData.realKey}
                    type="monotone"
                    stroke={palette.SERIES_COLORS[0]}
                    strokeWidth={1.5}
                    strokeDasharray="6 4"
                    dot={false}
                    {...chrome.anim}
                    animationDuration={600}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          </Card>

          <Card role="region" aria-label="Outcome per scenario table" tabIndex={0} className="overflow-x-auto p-4">
            <CardTitle>Outcome per scenario</CardTitle>
            <table className="w-full min-w-[500px] text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                  <th className="py-2 pr-4">Scenario</th>
                  <th className="py-2 pr-4">Final value</th>
                  <th className="py-2 pr-4">Total contributed</th>
                  <th className="py-2">Total gain</th>
                </tr>
              </thead>
              <tbody>
                {result.scenarios.map((scenario) => (
                  <tr key={scenario.label} className="border-b border-border last:border-0">
                    <td className="py-2 pr-4 font-medium text-text">{scenario.label}</td>
                    <td className="py-2 pr-4 tabular">{fmtCad(scenario.final_value_cad)}</td>
                    <td className="py-2 pr-4 tabular">{fmtCad(scenario.total_contributed_cad)}</td>
                    <td className="py-2 tabular">{fmtCad(scenario.total_gain_cad)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </>
      )}

      {result !== undefined && result.scenarios.length === 0 && (
        <EmptyState
          icon={<ChartLine />}
          title="No scenarios returned"
          hint="Add at least one scenario and run again."
          preview={{ src: "/help/reports.png", alt: "The Reports page with charts for the selected range" }}
          action={{
            label: "Run projections",
            onClick: () => void submit(),
            ariaLabel: "Run projections — No scenarios returned",
          }}
        />
      )}
    </div>
  );
}
