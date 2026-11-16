// Chart range presets + the range → granularity formula (M126, docs/12
// §Range → granularity).
//
// Two surfaces offer a date range in this app and they are deliberately
// different:
//
//   * `components/ui/RangePicker` — the PAGE-level control (Dashboard,
//     Reports, Transactions, Account detail): presets 1W…1Y plus free-form
//     From/To inputs.
//   * `components/ui/ChartRangeControl` — the CHART-header control added by
//     M126: presets only, so it stays one short row in a card header.
//
// The granularity formula below is the one docs/12 pins. The backend carries
// the same table for `GET /prices/history` (`app/services/price_series.py`);
// `tests/chartRange.test.ts` and `tests/test_prices_history.py` pin the SAME
// table on each side, because a chart that buckets day-granular on the client
// and month-granular on the server would disagree about its own x-axis.

/** Budget the auto-pick fits inside — one point per ~1 trading day per month
 *  of history is already more than an axis can label. Mirrors the backend's
 *  `MAX_SERIES_POINTS`. */
export const MAX_SERIES_POINTS = 150;

/** The chart-header presets, in the order they render (docs/12). */
export const CHART_RANGES = ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"] as const;
export type ChartRange = (typeof CHART_RANGES)[number];

export type SeriesGranularity = "day" | "week" | "month" | "year";

/** Average days per bucket — month/year are averages on purpose: this is a
 *  budget decision, not a calendar (same constants as the backend). */
const UNIT_DAYS: Record<SeriesGranularity, number> = {
  day: 1,
  week: 7,
  month: 30.44,
  year: 365.25,
};

/** The window a preset asks for. `from === null` means "no floor" (All) and
 *  `days === null` means the span is unknown until the server answers. */
export interface RangeWindow {
  from: string | null;
  to: string;
  days: number | null;
}

/** ISO date in the BROWSER's local calendar (the same rule `rangeFor` uses —
 *  a chart's "today" and a report's "today" must never disagree). */
function iso(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function daysBetween(from: Date, to: Date): number {
  return Math.max(0, Math.round((to.getTime() - from.getTime()) / 86_400_000));
}

export function rangeWindow(pick: ChartRange, today: Date = new Date()): RangeWindow {
  const to = iso(today);
  if (pick === "All") return { from: null, to, days: null };
  if (pick === "YTD") {
    const yearStart = new Date(today.getFullYear(), 0, 1);
    return { from: iso(yearStart), to, days: daysBetween(yearStart, today) };
  }
  const months = pick === "1M" ? 1 : pick === "3M" ? 3 : pick === "6M" ? 6 : pick === "1Y" ? 12 : 60;
  const start = new Date(today.getFullYear(), today.getMonth() - months, today.getDate());
  return { from: iso(start), to, days: daysBetween(start, today) };
}

/**
 * The documented formula (docs/12 §Range → granularity): the FINEST
 * granularity whose estimated bucket count fits `MAX_SERIES_POINTS`.
 *
 *   ceil(days / unitDays) <= 150, evaluated day-first
 *
 * 1M (31d) → day, 3M (92d) → day, 6M (182d) → week, 1Y (365d) → week,
 * 5Y (1826d) → month, 10Y → month.
 */
export function granularityForDays(days: number): SeriesGranularity {
  const span = Math.max(0, days);
  for (const granularity of ["day", "week", "month", "year"] as const) {
    if (Math.ceil(span / UNIT_DAYS[granularity]) <= MAX_SERIES_POINTS) return granularity;
  }
  return "year";
}

/**
 * Granularity for an explicit `[from, to]` window — the form the report
 * endpoints take (they need a bucket size, not a preset). Same formula, so
 * the Dashboard's net-worth/cash-flow charts bucket by the range the page
 * header is showing instead of a hard-coded `month` that collapses a 1W range
 * to a single bar.
 */
export function granularityForWindow(from: string, to: string): SeriesGranularity {
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  const days = Number.isFinite(start) && Number.isFinite(end) ? (end - start) / 86_400_000 : 0;
  return granularityForDays(Math.round(days));
}

/**
 * What a chart should ask the server for. `All` has no known span, so the
 * client asks for `auto` and lets the server apply the same formula to the
 * data it actually has.
 */
export function granularityForRange(
  pick: ChartRange,
  today: Date = new Date(),
): SeriesGranularity | "auto" {
  const { days } = rangeWindow(pick, today);
  return days === null ? "auto" : granularityForDays(days);
}
