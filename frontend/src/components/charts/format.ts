// Formatting helpers shared by the wealth/reporting pages (docs/09:
// money displays respect the row's native currency with CAD where relevant).

import { MASK_TEXT, isMasked } from "../../lib/mask";

/** Formats a Decimal-as-string CAD amount; null/invalid renders as "—".
 *  Thin re-export of the shared {@link formatCad} in `../../lib/format`. */
export { formatCad as fmtCad } from "../../lib/format";

/** Native-currency amounts have no ISO code attached per row beyond `currency`.
 *  M139: a native-currency amount is still an absolute amount, so it masks. */
export function fmtNative(
  value: string | null | undefined,
  currency: string,
  masked?: boolean,
): string {
  if (masked ?? isMasked()) return MASK_TEXT;
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  return `${new Intl.NumberFormat("en-CA", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n)} ${currency}`;
}

/** Quantities carry up to 6 decimals (docs/02); trim trailing zeros. */
export function fmtQuantity(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  return String(Number(n.toFixed(6)));
}

/** Plain decimal string -> number for chart axes. */
export function toNumber(value: string | null | undefined): number {
  const n = Number(value ?? NaN);
  return Number.isNaN(n) ? 0 : n;
}

/**
 * Display label for a data-provenance badge (M68, docs/09 "Naming"): the
 * vendor name behind a cached price is an implementation detail — it reaches
 * the UI as "Market data", never `yfinance`. Unknown sources pass through so a
 * new server-side value still shows something.
 */
export function sourceLabel(source: string): string {
  if (source === "yfinance") return "Market data";
  if (source === "manual") return "Manual";
  return source;
}

/**
 * Sub-day fetch age from `price_fetched_at` ONLY (Option A, t_bd5ec95f).
 *
 * The server stamps `price_fetched_at` when a provider response actually
 * arrived (`prices.fetched_at`, docs/07 §Fetch provenance); this formats that
 * instant's age as "12s" / "5m" / "2h" / "3d". Passing `price_date` (a
 * MARKET date, day-resolution) here would fabricate sub-day precision — the
 * one thing this provenance column exists to prevent.
 *
 * Returns null for missing/unparseable input — the caller then OMITS the
 * fetch-age segment entirely (never "0s", never "just now", never "fetch
 * time unavailable"; t_a16b7663). A stamp ahead of `now` (clock skew) is
 * clamped to "0s": the age is never rendered negative.
 */
export function formatFetchAge(
  fetchedAt: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!fetchedAt) return null;
  const fetched = Date.parse(fetchedAt);
  if (Number.isNaN(fetched)) return null;
  const elapsed = Math.max(0, now - fetched);
  const seconds = Math.floor(elapsed / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** Bucket keys are the bucket's FIRST date (ISO) — display compactly. */
export function fmtBucket(bucket: string, granularity?: string): string {
  const d = new Date(`${bucket}T00:00:00`);
  if (Number.isNaN(d.getTime())) return bucket;
  switch (granularity) {
    case "day":
      return d.toLocaleDateString("en-CA", { month: "short", day: "numeric" });
    case "week":
      return d.toLocaleDateString("en-CA", { month: "short", day: "numeric" });
    case "quarter":
      // M116: a quarterly bill buckets on the calendar quarter, and "Jul 2026"
      // would read as a MONTH. The key is the quarter's first date, so the
      // quarter is derived from it — no second key format to invent.
      return `Q${Math.floor(d.getMonth() / 3) + 1} ${d.getFullYear()}`;
    case "year":
      return d.toLocaleDateString("en-CA", { year: "numeric" });
    default:
      return d.toLocaleDateString("en-CA", { year: "numeric", month: "short" });
  }
}
