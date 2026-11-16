// PortfolioHeaderCard — the whole-portfolio headline (M130, docs/09
// §Portfolio header, docs/08 §Portfolio summary / §Portfolio value history).
//
// Three numbers the Investments page never had, in one honest order:
//
//   * the portfolio's value NOW (holdings at the latest cached prices + the
//     cash sitting in the investment accounts) — rendered from the SAME
//     `/portfolio/positions` payload the table below renders, so the two can
//     not disagree: `portfolio_value_cad` IS `total_value_cad + cash_cad`,
//     computed server-side, and when cash is zero the headline collapses onto
//     the Σ of the rows above it;
//   * the DAY change against the prior market day's close, with the real date
//     in the caption (`vs Oct 7 close`) — never "yesterday", which is a lie
//     from Friday onward — and withheld entirely when no row has a baseline;
//   * the selected RANGE's move, taken from `stats` — the server's own
//     subtraction of this chart's first and last point, so the sentence and
//     the line it describes are never computed twice.
//
// Live tick (M130's carve-out, docs/09 §Live prices): while the stream's
// `livePatchedAt` rides the payload, the value and the Δ re-evaluate from the
// server's own primitives (`quantity`, the patched `price`,
// `previous_close_native`, `fx_rate_to_cad`) and say "live". Cash never moves
// with a quote. The next server payload replaces every figure and the badge
// disappears — no timer anywhere.
//
// The chart below is M118's drawing body fed M130's summed payload: one
// range control (in this card) drives both the sentence above it and the line
// below it, because both come out of the same response.

import { useMemo, useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { TrendingUp } from "lucide-react";
import {
  getPortfolioValueHistory,
  type PositionsResponse,
  type PortfolioValueStats,
} from "../../api/portfolio";
import { AccountValueChartBody } from "../charts/AccountValueChart";
import { rangeWindow, type ChartRange } from "../../lib/chartRange";
import { formatCad } from "../../lib/format";
import { deltaArrow, fmtCadSigned, fmtSignedPct, livePortfolio } from "../../lib/impact";
import { Badge } from "../ui/Badge";
import { Card, CardTitle } from "../ui/Card";
import { ChartRangeControl } from "../ui/ChartRangeControl";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { HelpHint } from "../ui/HelpHint";
import { SkeletonRows } from "../ui/Skeleton";
import { useMasked } from "../../lib/mask";

/** The preset as the sentence under the headline says it. */
const RANGE_LABEL: Record<ChartRange, string> = {
  "1M": "1 month",
  "3M": "3 months",
  "6M": "6 months",
  YTD: "year to date",
  "1Y": "1 year",
  "5Y": "5 years",
  All: "all time",
};

/**
 * `2026-10-07` → `Oct 7`, in the BROWSER's calendar.
 *
 * Parsed by hand rather than through `new Date(iso)`: that reads the string as
 * UTC midnight, which lands on the PREVIOUS day in every North-American
 * timezone — the caption would name the wrong market day on exactly the
 * machines this app runs on.
 */
function formatCloseDay(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (year === undefined || month === undefined || day === undefined) return iso;
  return new Date(year, month - 1, day).toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
  });
}

interface Headline {
  /** The big number: portfolio value, or holdings alone when cash is unknown. */
  value: string | number | null;
  /** True when `value` is only the holdings because the cash leg is unknown. */
  holdingsOnly: boolean;
  holdings: string | null;
  cash: string | null;
  day: string | number | null;
  pct: number | null;
  live: boolean;
}

/** Server numbers at rest; server PRIMITIVES re-evaluated while a tick rides. */
function readHeadline(summary: PositionsResponse | undefined): Headline | null {
  if (summary === undefined) return null;
  const cash = summary.cash_cad ?? null;
  const holdings = summary.total_value_cad ?? null;
  if (summary.livePatchedAt === undefined) {
    return {
      // `portfolio_value_cad` is null only when the cash leg could not be
      // converted — then the honest number to show IS the holdings, and the
      // label below says so rather than pretending the total is known.
      value: summary.portfolio_value_cad ?? holdings,
      holdingsOnly: summary.portfolio_value_cad === undefined || summary.portfolio_value_cad === null,
      holdings,
      cash,
      day: summary.total_day_change_cad ?? null,
      pct: summary.total_day_change_pct ?? null,
      live: false,
    };
  }
  const recomputed = livePortfolio(summary.items, cash);
  const liveHoldings = recomputed.holdingsCad;
  const day = recomputed.dayCad;
  let pct: number | null = null;
  if (day !== null && liveHoldings !== null) {
    const base = liveHoldings - day;
    if (base !== 0) pct = Math.round((day / base) * 10000) / 100;
  }
  return {
    value: recomputed.valueCad ?? liveHoldings,
    holdingsOnly: recomputed.valueCad === null,
    holdings: liveHoldings === null ? null : String(liveHoldings),
    cash,
    day,
    pct,
    live: true,
  };
}

export function PortfolioHeaderCard({
  positions,
}: {
  positions: UseQueryResult<PositionsResponse>;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // M126's own control, owned HERE so one state drives both the sentence
  // ("over 6 months …") and the chart it describes — the spec's "the chart's
  // range selector drives the SAME number".
  const [range, setRange] = useState<ChartRange>("All");
  const span = rangeWindow(range);
  const windowFrom = range === "All" ? undefined : (span.from ?? undefined);
  const windowTo = range === "All" ? undefined : span.to;

  const history = useQuery({
    queryKey: ["portfolio-value-history", windowFrom ?? "", windowTo ?? ""],
    queryFn: () => getPortfolioValueHistory({ from: windowFrom, to: windowTo }),
    staleTime: 60_000,
  });

  const headline = readHeadline(positions.data);
  const asOf = positions.data?.day_change_as_of ?? null;
  const mixed = positions.data?.day_change_mixed_baseline === true;
  const stats: PortfolioValueStats | undefined = history.data?.stats;

  const dayLine = useMemo(() => {
    if (headline?.day === null || headline === null || headline.pct === null) return null;
    const day = typeof headline.day === "string" ? Number(headline.day) : headline.day;
    if (day === null || !Number.isFinite(day)) return null;
    return { day, pct: headline.pct };
  }, [headline]);

  if (headline === null) {
    return (
      <Card hover className="p-4" data-testid="portfolio-header">
        <CardTitle>
          Portfolio value
          <HelpHint
            className="ml-1.5"
            text="Market value of everything you hold in investment accounts, plus the cash in them, at the latest cached prices — the same figures the positions table below is built from."
          />
        </CardTitle>
        <SkeletonRows rows={2} />
      </Card>
    );
  }

  const rangeChange =
    stats === undefined || stats.change_cad === null ? null : Number(stats.change_cad);
  const rangePct = stats?.change_pct ?? null;
  const cashNumber = headline.cash === null ? null : Number(headline.cash);

  return (
    <section className="space-y-4" data-testid="portfolio-header">
      <Card hover className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-widest text-muted">
              {headline.holdingsOnly ? "Holdings value" : "Portfolio value"}
              <HelpHint
                className="ml-1.5"
                text={
                  headline.holdingsOnly
                    ? "Market value of everything you hold in investment accounts, at the latest cached prices. Cash could not be converted to CAD, so no total is claimed."
                    : "Market value of everything you hold in investment accounts, plus the cash in them, at the latest cached prices — the same figures the positions table below is built from."
                }
              />
            </p>
            <p
              className="mt-0.5 text-3xl font-semibold tracking-tight tabular text-text"
              data-testid="portfolio-value"
            >
              {formatCad(headline.value)}
              {headline.live && (
                <span className="ml-2 align-middle">
                  <Badge tone="accent">live</Badge>
                </span>
              )}
            </p>

            {dayLine !== null && (
              <p
                className={`mt-1 flex flex-wrap items-baseline gap-x-2 text-sm tabular ${
                  dayLine.day > 0 ? "text-gain" : dayLine.day < 0 ? "text-danger" : "text-muted"
                }`}
                data-testid="portfolio-day-change"
              >
                <span>
                  {deltaArrow(dayLine.day)} {fmtCadSigned(dayLine.day)} (
                  {fmtSignedPct(dayLine.pct)})
                </span>
                {asOf !== null && (
                  <span className="font-normal text-muted">
                    vs {formatCloseDay(asOf)} close
                  </span>
                )}
                {mixed && (
                  <span className="font-normal text-muted">
                    · some holdings last closed earlier
                  </span>
                )}
              </p>
            )}

            <p
              className="mt-1 flex flex-wrap items-baseline gap-x-2 text-sm text-muted"
              data-testid="portfolio-range-stats"
            >
              <span>Over {RANGE_LABEL[range]}:</span>
              <span className="tabular text-text">
                {/* The CAD move and its percent are independent facts: a
                    window that STARTED at zero has a perfectly real change and
                    no denominator, so withholding the whole line would hide a
                    number we do have. */}
                {rangeChange === null
                  ? "—"
                  : fmtCadSigned(rangeChange) +
                    (rangePct === null ? "" : ` (${fmtSignedPct(rangePct)})`)}
              </span>
              {stats?.from_bucket != null && stats.to_bucket != null && (
                <span className="text-xs">
                  {formatCloseDay(stats.from_bucket)} → {formatCloseDay(stats.to_bucket)}
                </span>
              )}
              <HelpHint
                className="ml-0.5"
                text="The chart's first and last point in this range, subtracted server-side from the same series — the sentence and the line can never disagree."
              />
            </p>

            {/* The reconciliation line (docs/09): shown ONLY when it has
                something to explain — a non-zero cash leg (why the headline is
                not the table's Σ) or an unknown one (why no total is claimed).
                Zero cash is the common case and needs no line at all. */}
            {headline.holdingsOnly && (
              <p className="mt-1 text-xs text-muted" data-testid="portfolio-cash">
                Cash in investment accounts —
              </p>
            )}
            {!headline.holdingsOnly && cashNumber !== null && cashNumber !== 0 && (
              <p className="mt-1 text-xs text-muted" data-testid="portfolio-cash">
                Holdings {formatCad(headline.holdings)} · Cash in investment accounts{" "}
                <span className="tabular">{fmtCadSigned(cashNumber)}</span>
              </p>
            )}
          </div>

          <ChartRangeControl
            value={range}
            onChange={setRange}
            ariaLabel="Portfolio value over time range"
            size="md"
            className="shrink-0"
          />
        </div>
      </Card>

      {history.isPending && (
        <Card hover className="p-4">
          <CardTitle>Portfolio value over time</CardTitle>
          <SkeletonRows rows={3} />
        </Card>
      )}
      {!history.isPending && history.isError && (
        <Card hover className="p-4">
          <CardTitle>Portfolio value over time</CardTitle>
          <QueryErrorPanel query={history} />
        </Card>
      )}
      {history.data !== undefined && !history.data.has_data && (
        <Card hover className="p-4">
          <CardTitle>Portfolio value over time</CardTitle>
          <EmptyState
            icon={<TrendingUp />}
            title="No value history yet"
            hint="The value line appears as soon as an investment account records a balance, an import or a trade."
            preview={{
              src: "/help/investments.png",
              alt: "The Investments page with holdings and totals",
            }}
            action={{ label: "Import a statement", to: "/imports" }}
          />
        </Card>
      )}
      {history.data?.has_data === true && (
        <AccountValueChartBody
          history={history.data}
          title="Portfolio value over time"
          // The range control lives in the header above, next to the sentence
          // it drives — passing a node keeps the body's own control off (the
          // M126 cash-view escape hatch) while its badges still render.
          headerExtra={<span className="sr-only">Range selected in the header above</span>}
          selectedEventId={null}
        />
      )}
    </section>
  );
}
