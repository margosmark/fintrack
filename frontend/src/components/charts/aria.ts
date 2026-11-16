// Chart text alternatives (docs/12 "Chart accessibility", WCAG 1.1.1 Level A).
//
// Every chart renders ONE sentence describing what it is and what it says, and
// the wrapper that carries it uses role="img" — ARIA makes that a leaf node
// (children presentational), so assistive tech reads the sentence instead of
// walking the Recharts <svg> below it.

import { formatCad, formatMoney } from "../../lib/format";

export interface ChartSummary {
  /** What the chart is, e.g. "Net worth trend". */
  title: string;
  /** Granularity / shape, e.g. "monthly buckets", "categorical breakdown". */
  kind: string;
  /** Inclusive x-axis span, e.g. "2026-01-31 – 2026-12-31". Omitted for
   *  snapshot charts (donuts) that have no time axis. */
  range?: string;
  /** Rendered data points. */
  points: number;
  /** Most-informative first; joined with "; ". */
  facts?: string[];
}

/**
 * `"<title> — <kind>, <range>, <points> points. <fact>; <fact>."`
 * Empty series append "No data in range." instead of facts.
 */
export function chartAriaLabel({
  title,
  kind,
  range,
  points,
  facts = [],
}: ChartSummary): string {
  const span = range === undefined || range.length === 0 ? "" : `, ${range}`;
  const head = `${title} — ${kind}${span}, ${points} point${points === 1 ? "" : "s"}.`;
  if (points === 0) return `${head} No data in range.`;
  const tail = facts.filter((fact) => fact.trim().length > 0).join("; ");
  return tail.length > 0 ? `${head} ${tail}.` : head;
}

/** docs/12 granularity enum → the words that read correctly in a sentence. */
export function granularityLabel(granularity?: string): string {
  switch (granularity) {
    case "day":
      return "daily buckets";
    case "week":
      return "weekly buckets";
    case "quarter":
      return "quarterly buckets";
    case "year":
      return "yearly buckets";
    case "month":
    default:
      return "monthly buckets";
  }
}

/** Inclusive date span for a series; "no range" when either end is missing. */
export function dateRange(first?: string | null, last?: string | null): string {
  if (!first || !last) return "no range";
  return first === last ? first : `${first} – ${last}`;
}

/** "from $100,000 to $151,800" — the endpoint fact every trend chart carries. */
export function fromToFact(from: number, to: number, label = "value"): string {
  return `${label} ${formatCad(from)} to ${formatCad(to)}`;
}

/** Largest slice of a breakdown: "Groceries 34%". */
export function shareFact(name: string, value: number, total: number): string {
  if (total <= 0) return `${name}`;
  return `${name} ${Math.round((value / total) * 100)}%`;
}

export { formatCad, formatMoney };
