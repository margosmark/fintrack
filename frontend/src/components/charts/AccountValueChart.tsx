// AccountValueChart — the account's own value timeline (M118, docs/08
// §Account value history, docs/09 §Account value history).
//
// One line: cash + holdings for THIS account at each bucket end, in the
// reporting currency, plus marker pins for the significant cash flows that
// moved it. The chart is a VIEW over a server-computed series — every number
// and every marker is derived from committed rows on the wire (never cached to
// a table), so cause and evidence stay connected: click a pin (or the matching
// row in the table below) and the page scrolls to the transaction or trade
// that produced it.
//
// Honesty, in the three places a reader can be misled (docs/08 §Honesty):
//   * a period with NO valuation draws a GAP — `value_cad: null` reaches the
//     chart as `null`, so Recharts breaks the line instead of interpolating
//     through it — and the tooltip/table say WHY (M144's two-reason state:
//     "no valuation for this period" for a data gap, "not held yet" for a day
//     the account held nothing);
//   * a period valued from a carried-forward price carries the amber
//     `estimated` badge (header chip + tooltip + table note);
//   * the `<details>` table underneath is the data-table fallback
//     (BillHistoryBarChart precedent): keyboard-reachable through the native
//     `summary`, one labelled scroll region, and every marker has a real
//     `<button>` there so pins are reachable without a pointer.
//
// Chrome is the shared family: `ChartFrame` (role="img" + one sentence),
// `useChartChrome`, `fmtBucket`, `Badge` and the same glass tooltip shell.
import { useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { TrendingUp } from "lucide-react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  getAccountValueHistory,
  type AccountValueHistory,
  type ValueEventKind,
  type ValueGranularity,
  type ValueHistoryBucket,
  type ValueHistoryEvent,
  type ValueHistoryGapReason,
} from "../../api/accounts";
import { formatCad } from "../../lib/format";
import { Badge } from "../ui/Badge";
import { Card, CardTitle } from "../ui/Card";
import { ChartRangeControl } from "../ui/ChartRangeControl";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { ChartGradient, safeId } from "../ui/charts/ChartTooltip";
import { rangeWindow, type ChartRange } from "../../lib/chartRange";
import { chartAriaLabel, dateRange, granularityLabel } from "./aria";
import { formatAxisMoney, useChartChrome } from "./chartDefaults";
import { ChartFrame } from "./ChartFrame";
import { axisTickInterval, useViewportWidth } from "./thinning";
import { fmtBucket, fmtQuantity, toNumber } from "./format";
import { useChartPalette } from "./palette";
import { MASK_TEXT, useMasked } from "../../lib/mask";

/** Wire token → the words the tooltip and table print. */
const KIND_LABELS: Record<ValueEventKind, string> = {
  contribution: "contribution",
  withdrawal: "withdrawal",
  transfer_in: "transfer in",
  transfer_out: "transfer out",
  deposit: "deposit",
};

interface Point {
  bucket: string;
  /** null = the period could not be valued → the line BREAKS here. */
  value: number | null;
  cash: number | null;
  holdings: number | null;
  estimated: boolean;
  valued: boolean;
  /** WHY the period has no value — null whenever it has one (M144). */
  gapReason: ValueHistoryGapReason | null;
  events: ValueHistoryEvent[];
}

/**
 * M144 (docs/08 §Honesty — the two-reason empty state): ONE owner for the
 * words an unvalued period prints. A data gap (`no_price` / `fx`) is red and
 * says so; a day the account simply held nothing on is a HOLDINGS gap and says
 * "not held yet" — telling a reader their cache is broken when nothing was
 * held yet is the exact misreading this pair exists to prevent.
 */
export function gapReasonNote(gapReason: ValueHistoryGapReason | null): {
  text: string;
  tone: "danger" | "muted";
} {
  if (gapReason === "not_held") return { text: "not held yet", tone: "muted" };
  return { text: "no valuation for this period", tone: "danger" };
}

/** `+ $12,600.00` / `− $24,318.76` — sign outside the number, as the reader reads it. */
export function valueEventAmount(event: ValueHistoryEvent): string {
  const amount = Number(event.amount_cad);
  return `${amount < 0 ? "−" : "+"} ${formatCad(Math.abs(amount))}`;
}

export function valueEventHeading(event: ValueHistoryEvent): string {
  return `${valueEventAmount(event)} ${KIND_LABELS[event.kind]}`;
}

/**
 * The evidence line under a marker: WHY that money is marked, straight from
 * the row that caused it. Nothing here is invented — an absent category or
 * payee simply drops its clause instead of printing a placeholder.
 */
export function valueEventEvidence(event: ValueHistoryEvent): string | null {
  if (event.source === "transaction") {
    const parts: string[] = [];
    if (event.category_name) parts.push(`categorized ${event.category_name}`);
    if (event.payee) parts.push(`from ${event.payee}`);
    return parts.length > 0 ? parts.join(" — ") : null;
  }
  if (event.quantity !== null && Number(event.quantity) !== 0) {
    return event.symbol
      ? `${fmtQuantity(event.quantity)} ${event.symbol} transferred in kind`
      : "shares transferred in kind";
  }
  return "recorded on the trades ledger";
}

/** Marker tooltip content — the M97 honest-tooltip pattern at chart scale. */
function ValueTooltip({
  active,
  payload,
  label,
  granularity,
}: {
  active?: boolean;
  payload?: Array<{ payload: Point }>;
  label?: string;
  granularity: ValueGranularity;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  if (active !== true || payload === undefined || payload.length === 0) return null;
  const point = payload[0]?.payload;
  if (point === undefined) return null;
  const gapNote = gapReasonNote(point.gapReason);
  return (
    <div className="glass-strong min-w-44 rounded-lg px-3 py-2 text-xs shadow-card">
      <p className="mb-1.5 font-medium text-muted">
        {fmtBucket(label ?? point.bucket, granularity)}
      </p>
      {point.valued ? (
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="text-muted">Value</span>
            <span className="ml-auto pl-4 font-medium tabular">{formatCad(point.value)}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-muted">Cash</span>
            <span className="ml-auto pl-4 tabular">{formatCad(point.cash)}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-muted">Holdings</span>
            <span className="ml-auto pl-4 tabular">{formatCad(point.holdings)}</span>
          </div>
        </div>
      ) : (
        // docs/08 §Honesty — never a fabricated zero, never an interpolation.
        // M144: a HOLDINGS gap is not a data hole, and must not read as one.
        <p className={gapNote.tone === "danger" ? "text-danger" : "text-muted"}>{gapNote.text}</p>
      )}
      {point.estimated && (
        <p className="mt-1.5 border-t border-veil/[0.08] pt-1.5 text-warn">
          estimated — a price for this period was carried forward
        </p>
      )}
      {point.events.length > 0 && (
        <div className="mt-1.5 space-y-1.5 border-t border-veil/[0.08] pt-1.5">
          {point.events.map((event) => {
            const evidence = valueEventEvidence(event);
            return (
              <div key={event.id}>
                <p className="font-medium text-text">{valueEventHeading(event)}</p>
                {evidence !== null && <p className="text-muted">({evidence})</p>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * The SHAPE the drawing body needs — exactly what M118's per-account payload
 * and M130's summed portfolio payload have in common. Deliberately narrower
 * than either wire contract: the body never reads the scope id (one account vs
 * a list) nor `stats` (the header above the chart renders that), so both
 * responses feed the same component with no adapter and no `as`.
 */
export interface ValueChartPayload {
  granularity: ValueGranularity;
  requested_granularity: ValueGranularity;
  currency: string;
  native_currency: string;
  has_data: boolean;
  buckets: ValueHistoryBucket[];
  events: ValueHistoryEvent[];
}

export interface AccountValueChartProps {
  accountId: number;
  /** Window override — the cash view's RangePicker drives these. */
  from?: string;
  to?: string;
  title?: string;
  /** Rendered in the card header (range picker on the cash view). */
  headerExtra?: ReactNode;
  /** Marker click → the page scrolls to the row behind it. */
  onSelectEvent?: (event: ValueHistoryEvent) => void;
  selectedEventId?: string | null;
  /** Test hook: skip the request and render this payload directly. */
  history?: AccountValueHistory;
}

export function AccountValueChart({
  accountId,
  from,
  to,
  title = "Account value over time",
  headerExtra,
  onSelectEvent,
  selectedEventId = null,
  history: pinnedHistory,
}: AccountValueChartProps) {
  // M126 (docs/12 §Chart ranges): when the CALLER supplies a control (the
  // cash view's RangePicker with its free-form dates) it stays the only one.
  // With none — the plain account timeline — the chart owns its own window,
  // defaulting to `All`, which is byte-identical to the old full-history
  // request until someone picks a range.
  const [range, setRange] = useState<ChartRange>("All");
  const ownsRange = headerExtra === undefined;
  const span = rangeWindow(range);
  const windowFrom = ownsRange ? (range === "All" ? undefined : span.from ?? undefined) : from;
  const windowTo = ownsRange ? (range === "All" ? undefined : span.to) : to;

  // One literal key → one callee (docs/09 "Query keys"); 60s mirrors the
  // endpoint's own `Cache-Control: private, max-age=60`.
  const query = useQuery({
    queryKey: ["account-value-history", accountId, windowFrom ?? "", windowTo ?? ""],
    queryFn: () => getAccountValueHistory(accountId, { from: windowFrom, to: windowTo }),
    enabled: pinnedHistory === undefined && Number.isFinite(accountId),
    staleTime: 60_000,
  });
  const history = pinnedHistory ?? query.data;
  const loading = pinnedHistory === undefined && query.isLoading;
  const failed = pinnedHistory === undefined && query.isError;

  if (loading) {
    return (
      <Card hover className="p-4">
        <CardTitle>{title}</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }
  if (failed) {
    return (
      <Card hover className="p-4">
        <CardTitle>{title}</CardTitle>
        <QueryErrorPanel query={query} />
      </Card>
    );
  }
  if (history === undefined) return null;
  // docs/09 §Empty states — the three-part contract, NOT a flat $0 line. It
  // lives out here so the drawing body (and its theme hooks) never mounts for
  // an account with nothing recorded yet.
  if (!history.has_data) {
    return (
      <Card hover className="p-4">
        <CardTitle>{title}</CardTitle>
        <EmptyState
          icon={<TrendingUp />}
          title="No value history yet"
          hint="The value line appears as soon as this account records a balance, an import or a trade."
          preview={{
            src: "/help/investments.png",
            alt: "The Investments page with holdings and totals",
          }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      </Card>
    );
  }
  return (
    <AccountValueChartBody
      history={history}
      title={title}
      headerExtra={
        headerExtra ?? (
          <ChartRangeControl
            value={range}
            onChange={setRange}
            ariaLabel={`${title} range`}
          />
        )
      }
      onSelectEvent={onSelectEvent}
      selectedEventId={selectedEventId}
    />
  );
}

/**
 * The drawing body, exported for M130: the PORTFOLIO chart is this exact
 * component fed M130's summed payload — same chrome, same range control, same
 * marker pins, same data-table fallback (docs/09 §Portfolio header). Only the
 * fetch and the numbers differ; the rendering has one owner.
 *
 * Callers that own the window (like the portfolio header, whose range control
 * also drives its "over <range>" figure) pass `headerExtra`, which is what
 * turns the body's own control off — the M126 cash-view escape hatch.
 */
export function AccountValueChartBody({
  history,
  title,
  headerExtra,
  onSelectEvent,
  selectedEventId,
}: {
  history: ValueChartPayload;
  title: string;
  headerExtra?: ReactNode;
  onSelectEvent?: (event: ValueHistoryEvent) => void;
  selectedEventId: string | null;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  //
  // B1 (QA blocker): re-rendering this component is NOT enough for the AXIS.
  // Recharts memoizes `YAxis` with `axisPropsAreEqual`, and `chrome.moneyAxis`
  // is the stable module function `formatAxisMoney` — identical props, so the
  // memo bailed and the tick layer kept painting the mount state while every
  // other money string on the page flipped. The Dashboard's chart only escaped
  // by accident (its `data` array is rebuilt each render, churning the chart
  // store); this body memoizes `points`, so nothing else invalidated the axis.
  //
  // So the mask must reach Recharts as a REACTIVE PROP, not as a value a
  // callback sniffs at call time: the inline `tickFormatter` below is rebuilt
  // on every pass of this subscribed body, so its identity changes the moment
  // `masked` does, and that is the input the memo compares — prop changes →
  // the dispatcher re-runs → the axis re-draws its ticks from the new
  // closure. No refetch, no remount, no imperative global read deciding what
  // the layer already drew. (Deliberately NOT `useMemo`/`useCallback`: the D6
  // sweep fails on money built inside either, and the closure must be new on
  // every pass anyway.)
  const masked = useMasked();

  const palette = useChartPalette();
  const chrome = useChartChrome();
  const grad = safeId(useId());
  // M126: the range control above changes the bucket count; the axis thins
  // from (points, viewport) rather than relying on recharts' own gap heuristics.
  const width = useViewportWidth();
  const { buckets, events, granularity } = history;

  const points: Point[] = useMemo(() => {
    const byBucket = new Map<string, ValueHistoryEvent[]>();
    for (const event of events) {
      const bucket = byBucket.get(event.bucket_end);
      if (bucket === undefined) byBucket.set(event.bucket_end, [event]);
      else bucket.push(event);
    }
    return buckets.map((bucket) => ({
      bucket: bucket.bucket_end,
      value: bucket.valued && bucket.value_cad !== null ? toNumber(bucket.value_cad) : null,
      cash: bucket.valued && bucket.cash_cad !== null ? toNumber(bucket.cash_cad) : null,
      holdings:
        bucket.valued && bucket.positions_cad !== null ? toNumber(bucket.positions_cad) : null,
      estimated: bucket.estimated,
      valued: bucket.valued,
      gapReason: bucket.gap_reason ?? null,
      events: byBucket.get(bucket.bucket_end) ?? [],
    }));
  }, [buckets, events]);

  const valueByBucket = useMemo(() => {
    const index = new Map<string, number>();
    for (const point of points) {
      if (point.value !== null) index.set(point.bucket, point.value);
    }
    return index;
  }, [points]);

  const estimatedCount = buckets.filter((bucket) => bucket.estimated).length;
  // M144: only a DATA gap counts as "without a valuation" — a day the account
  // held nothing on is not a hole in the cache and never wears the red badge.
  const gapCount = buckets.filter(
    (bucket) => !bucket.valued && bucket.gap_reason !== "not_held",
  ).length;
  const coarsened = history.granularity !== history.requested_granularity;
  const valuedPoints = points.filter((point) => point.value !== null);

  const label = chartAriaLabel({
    title,
    kind: granularityLabel(granularity),
    range: dateRange(buckets[0]?.bucket_end, buckets[buckets.length - 1]?.bucket_end),
    points: buckets.length,
    facts: [
      valuedPoints.length > 1
        ? `from ${formatCad(valuedPoints[0]?.value)} to ${formatCad(
            valuedPoints[valuedPoints.length - 1]?.value,
          )}`
        : "",
      events.length > 0
        ? `${events.length} marker${events.length === 1 ? "" : "s"} for contributions, withdrawals and transfers`
        : "",
      estimatedCount > 0
        ? `${estimatedCount} estimated period${estimatedCount === 1 ? "" : "s"}`
        : "",
      gapCount > 0 ? `${gapCount} period${gapCount === 1 ? "" : "s"} without a valuation` : "",
    ],
  });

  return (
    <Card hover className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>{title}</CardTitle>
        <div className="flex flex-wrap items-center justify-end gap-1.5">
          {coarsened && (
            <span className="text-[11px] text-muted">
              {granularityLabel(granularity)} — the finer ask would be over 400 points
            </span>
          )}
          {history.currency !== history.native_currency && (
            <Badge tone="neutral">values in {history.currency}</Badge>
          )}
          {estimatedCount > 0 && <Badge tone="warn">~ {estimatedCount} estimated</Badge>}
          {gapCount > 0 && <Badge tone="danger">{gapCount} without a valuation</Badge>}
          {headerExtra}
        </div>
      </div>

      <figure className="m-0 mt-3">
        {/* M102 scroll region: labelled + focusable, and the min-width is what
            keeps axis labels from being clipped at 390/412 — the chart pans
            horizontally instead of squeezing its ticks. */}
        <div role="region" aria-label={`${title} chart`} tabIndex={0} className="overflow-x-auto">
          <div className="min-w-[36rem]">
            <ChartFrame label={label}>
              <ResponsiveContainer width="100%" height={280}>
                <ComposedChart data={points} margin={{ top: 8, right: 24, bottom: 4, left: 4 }}>
                  <ChartGradient id={`value-${grad}`} from={palette.ACCENT} />
                  <CartesianGrid {...chrome.grid} vertical={false} />
                  <XAxis
                    dataKey="bucket"
                    {...chrome.axis}
                    interval={axisTickInterval(points.length, width)}
                    tickFormatter={(bucket: string) => fmtBucket(bucket, granularity)}
                  />
                  <YAxis
                    {...chrome.axis}
                    width={80}
                    domain={["auto", "auto"]}
                    /* B1 — the reactive contract, in one line: this closure
                       is rebuilt on every pass of this SUBSCRIBED body, so a
                       mask flip hands Recharts a NEW `tickFormatter` and the
                       `axisPropsAreEqual` memo misses instead of bailing on
                       the stable module function. The branch is taken from
                       `masked` — the value useMasked() returned THIS render —
                       not from a global read at call time, and nothing here
                       changes the data, the query or the chart instance. */
                    tickFormatter={(value) =>
                      masked ? MASK_TEXT : formatAxisMoney(value)
                    }
                  />
                  <Tooltip
                    content={<ValueTooltip granularity={granularity} />}
                    cursor={chrome.cursorStroke}
                  />
                  <Area
                    dataKey="value"
                    name="Value"
                    type="monotone"
                    fill={`url(#value-${grad})`}
                    stroke={palette.ACCENT}
                    strokeWidth={2}
                    dot={false}
                    /* The Area's hover dot lands on the very point a marker
                       pins to and paints OVER it, so it swallowed every click
                       on a pin (measured: elementsFromPoint returned the
                       active dot). Keep the affordance, let clicks through. */
                    activeDot={{ pointerEvents: "none" }}
                    isAnimationActive={chrome.anim.isAnimationActive}
                    animationDuration={900}
                  />
                  {/* Marker pins sit ON the line at the event's bucket. A
                      bucket we could not value has no position to pin to — its
                      event is still listed in the table below. */}
                  {onSelectEvent !== undefined &&
                    events
                      .filter((event) => valueByBucket.has(event.bucket_end))
                      .map((event) => (
                        <ReferenceDot
                          key={event.id}
                          x={event.bucket_end}
                          y={valueByBucket.get(event.bucket_end)}
                          r={5}
                          fill={event.direction > 0 ? palette.GAIN : palette.LOSS}
                          stroke={
                            selectedEventId === event.id ? palette.ACCENT : palette.AXIS_TICK.fill
                          }
                          strokeWidth={selectedEventId === event.id ? 3 : 1.5}
                          className="cursor-pointer"
                          onClick={() => onSelectEvent(event)}
                        >
                          <title>{valueEventHeading(event)}</title>
                        </ReferenceDot>
                      ))}
                </ComposedChart>
              </ResponsiveContainer>
            </ChartFrame>
          </div>
        </div>

        {/* Data-table fallback (M116 precedent) — the keyboard path to every
            marker and every number behind the line. */}
        <details className="mt-1 rounded-lg border border-control">
          <summary className="cursor-pointer px-3 py-2 text-xs text-muted outline-none focus-visible:ring-2 focus-visible:ring-accent">
            Show the numbers behind this chart
          </summary>
          <div
            className="overflow-x-auto px-3 pb-3"
            tabIndex={0}
            aria-label={`${title} — periods and events, as a table`}
          >
            <table className="w-full text-left text-xs">
              <caption className="sr-only">{label}</caption>
              <thead>
                <tr className="border-b border-control text-muted">
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Period
                  </th>
                  <th scope="col" className="py-1 pr-3 text-right font-medium">
                    Value
                  </th>
                  <th scope="col" className="py-1 pr-3 text-right font-medium">
                    Cash
                  </th>
                  <th scope="col" className="py-1 pr-3 text-right font-medium">
                    Holdings
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Notes
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((point) => {
                  const gapNote = gapReasonNote(point.gapReason);
                  return (
                    <tr key={point.bucket} className="border-b border-veil/[0.05] last:border-b-0">
                      <th scope="row" className="py-1 pr-3 font-normal text-text">
                        {fmtBucket(point.bucket, granularity)}
                      </th>
                      <td className="py-1 pr-3 text-right tabular text-text">
                        {point.value === null ? "—" : formatCad(point.value)}
                      </td>
                      <td className="py-1 pr-3 text-right tabular text-muted">
                        {point.cash === null ? "—" : formatCad(point.cash)}
                      </td>
                      <td className="py-1 pr-3 text-right tabular text-muted">
                        {point.holdings === null ? "—" : formatCad(point.holdings)}
                      </td>
                      <td className="py-1 text-muted">
                        {/* M144: two reasons, two sentences — a data gap is
                            red, a day nothing was held is simply "not held
                            yet" (docs/08 §Honesty, docs/09 §Empty states). */}
                        {!point.valued && (
                          <span
                            className={gapNote.tone === "danger" ? "text-danger" : "text-muted"}
                          >
                            {gapNote.text}
                          </span>
                        )}
                        {point.valued && point.estimated && (
                          <span className="text-warn">estimated</span>
                        )}
                        {point.valued && !point.estimated && "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {events.length > 0 && (
              <table className="mt-3 w-full text-left text-xs">
                <caption className="sr-only">Significant events behind the markers</caption>
                <thead>
                  <tr className="border-b border-control text-muted">
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Date
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Event
                    </th>
                    <th scope="col" className="py-1 pr-3 text-right font-medium">
                      Amount
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      Evidence
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      <span className="sr-only">Show in the table</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => {
                    const evidence = valueEventEvidence(event);
                    return (
                      <tr
                        key={event.id}
                        className={`border-b border-veil/[0.05] last:border-b-0 ${
                          selectedEventId === event.id ? "bg-accent/10" : ""
                        }`}
                      >
                        <td className="whitespace-nowrap py-1 pr-3 tabular text-text">
                          {event.date}
                        </td>
                        <td className="py-1 pr-3 text-text">{KIND_LABELS[event.kind]}</td>
                        <td className="py-1 pr-3 text-right tabular text-text">
                          {valueEventAmount(event)}
                        </td>
                        <td className="py-1 text-muted">{evidence ?? "—"}</td>
                        <td className="py-1 text-right">
                          {onSelectEvent !== undefined && (
                            <button
                              type="button"
                              onClick={() => onSelectEvent(event)}
                              aria-label={`Show the ${KIND_LABELS[event.kind]} of ${event.date} in the table`}
                              className="rounded px-2 py-1 text-xs text-accent outline-none hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                            >
                              Show
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </details>
      </figure>
    </Card>
  );
}
