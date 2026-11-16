/**
 * Shared fixed-point + date primitives (M160, docs/33 §Server-first rule 5).
 *
 * R1 decision (M160-R1, t_4fea992a): these are the BigInt fixed-point Decimal
 * primitives, the UTC date/bucket helpers and the two grouping helpers that
 * every client report port shared while they lived inside
 * `lib/reportEngine.ts`. M160 retired that engine (the report computes are
 * the server's), but the primitives themselves are not mirrors — they are
 * exact ports of Python's `decimal.Decimal` / naive-date arithmetic that the
 * SURVIVING client code still runs on:
 *
 *   - `components/transactions/SplitEditor.tsx` — the split-transactions
 *     money math (M107, docs/44), a live editing surface;
 *   - `lib/sankeyEngine.ts` — the M77 sankey compute. M160-R2 (t_85311c34)
 *     re-pointed `SankeyPanel` at `GET /reports/cashflow-sankey`, so no
 *     production caller runs it in the browser anymore; it stays as the
 *     reference implementation the route was written against (M160-R1 ruling
 *     3) and still owns the `SankeyReport` wire shape the chart renders;
 *   - `lib/budgets.ts` — the audited client-only Row-8 budget compute
 *     (docs/43 §9: no server twin by design).
 *
 * Byte-for-byte the code that shipped inside reportEngine.ts — no rounding
 * rule moved (note that `lib/budgets.ts` deliberately keeps its OWN private
 * copies of the arithmetic it needs: the s3 port used ROUND_HALF_EVEN
 * `dquantize`/`ddivSig`, while this cluster rounds HALF_UP — the two
 * conventions must never be merged).
 *
 * Lock-vacuous by construction: nothing here reads a row, so a locked vault
 * cannot move a number (docs/33 rule-8 inventory rows for these computes
 * live under this module since M160).
 */

import type { Granularity } from "../api/reports";

// --- money -------------------------------------------------------------------

/**
 * Mirror of the server's `{detail, code}` envelope — loaders rethrow it as
 * ApiError so errorHint keeps rendering server messages verbatim.
 */
export class EngineError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, message: string, code: string) {
    super(message);
    this.name = "EngineError";
    this.status = status;
    this.code = code;
  }
}

export interface Dec {
  readonly v: bigint; // unscaled integer
  readonly s: number; // decimal places
}

export function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

/** Parse a decimal string exactly (incl. `1e-7` style exponents). */
export function dec(text: string): Dec {
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text.trim());
  if (!m || (m[2] === "" && (m[3] ?? "") === "")) {
    throw new EngineError(500, `not a decimal: ${text}`, "internal_error");
  }
  const frac = m[3] ?? "";
  let v = BigInt((m[2] || "0") + frac);
  if (m[1] === "-") v = -v;
  let s = frac.length;
  if (m[4] !== undefined) {
    const exp = Number(m[4]);
    if (exp > 0) v *= pow10(exp);
    else s += -exp;
  }
  return { v, s };
}

export function dzero(scale = 0): Dec {
  return { v: 0n, s: scale };
}

export function dadd(a: Dec, b: Dec): Dec {
  if (a.s === b.s) return { v: a.v + b.v, s: a.s };
  const s = Math.max(a.s, b.s);
  return { v: a.v * pow10(s - a.s) + b.v * pow10(s - b.s), s };
}

export function dsub(a: Dec, b: Dec): Dec {
  return dadd(a, { v: -b.v, s: b.s });
}

export function dmul(a: Dec, b: Dec): Dec {
  return { v: a.v * b.v, s: a.s + b.s };
}

export function dabs(a: Dec): Dec {
  return { v: a.v < 0n ? -a.v : a.v, s: a.s };
}

export function dneg(a: Dec): Dec {
  return { v: -a.v, s: a.s };
}

export function dcmp(a: Dec, b: Dec): number {
  const s = Math.max(a.s, b.s);
  const av = a.v * pow10(s - a.s);
  const bv = b.v * pow10(s - b.s);
  return av < bv ? -1 : av > bv ? 1 : 0;
}

export function disZero(a: Dec): boolean {
  return a.v === 0n;
}

/** Round half away from zero to `scale` (Python Decimal ROUND_HALF_UP). */
export function dround(a: Dec, scale: number): Dec {
  if (a.s <= scale) return { v: a.v * pow10(scale - a.s), s: scale };
  const p = pow10(a.s - scale);
  const negative = a.v < 0n;
  const magnitude = negative ? -a.v : a.v;
  let q = magnitude / p;
  if ((magnitude % p) * 2n >= p) q += 1n;
  return { v: negative ? -q : q, s: scale };
}

/** `str(Decimal)` — fixed notation, scale preserved, no exponent. */
export function dstr(a: Dec): string {
  const negative = a.v < 0n;
  const digits = (negative ? -a.v : a.v).toString();
  if (a.s === 0) return negative ? `-${digits}` : digits;
  const padded = digits.padStart(a.s + 1, "0");
  const cut = padded.length - a.s;
  const body = `${padded.slice(0, cut)}.${padded.slice(cut)}`;
  return negative ? `-${body}` : body;
}

/** `_cents(x)` → `str(Decimal.quantize(0.01))`, sign kept ("-0.00"). */
export function dcents(a: Dec): string {
  if (a.v < 0n) {
    const r = dround(a, 2);
    if (r.v === 0n) return "-0.00"; // Python quantize preserves the sign of zero
    return dstr(r);
  }
  return dstr(dround(a, 2));
}

/** Quantize to cents, keeping the Dec (Python `_cents` where a sum follows). */
export function quantizeCents(a: Dec): Dec {
  return dround(a, 2);
}

/**
 * ROUND_HALF_UP to 2dp, always 2 decimal places — the port of
 * `_cents` + `_fmt(cents=True)`. The 1e-6 epsilon absorbs binary-float
 * noise without moving a genuine half-cent boundary.
 */
export function cents(value: number): string {
  if (!Number.isFinite(value)) return "0.00";
  const sign = value < 0 ? -1 : 1;
  const rounded = Math.round(Math.abs(value) * 100 + 1e-6);
  return (sign * rounded) / 100 === 0
    ? (0).toFixed(2)
    : (sign * rounded / 100).toFixed(2);
}

// --- time bucketing (docs/12) ------------------------------------------------
// All math in UTC milliseconds over YYYY-MM-DD strings — no local-Timezone
// Date parsing anywhere (the Python side works on naive dates).

export const DAY = 86_400_000;

export const REPORTING = "CAD"; // get_settings().reporting_currency (pinned by S2a parity)

export function utc(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

export function iso(day: number): string {
  const d = new Date(day);
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return `${d.getUTCFullYear()}-${mm}-${dd}`;
}

function floorBucket(day: number, granularity: Granularity): number {
  const d = new Date(day);
  if (granularity === "day") return day;
  if (granularity === "week") return day - ((d.getUTCDay() + 6) % 7) * DAY;
  if (granularity === "month") return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
  return Date.UTC(d.getUTCFullYear(), 0, 1);
}

function advanceBucket(day: number, granularity: Granularity): number {
  const d = new Date(day);
  switch (granularity) {
    case "day":
      return day + DAY;
    case "week":
      return day + 7 * DAY;
    case "month":
      return Date.UTC(
        d.getUTCMonth() === 11 ? d.getUTCFullYear() + 1 : d.getUTCFullYear(),
        (d.getUTCMonth() + 1) % 12,
        1,
      );
    case "year":
      return Date.UTC(d.getUTCFullYear() + 1, 0, 1);
  }
}

/** First date of every bucket covering [from, to] (weeks start Monday). */
export function bucketStarts(from: string, to: string, granularity: Granularity): number[] {
  const end = utc(to);
  let cur = floorBucket(utc(from), granularity);
  const starts: number[] = [];
  while (cur <= end) {
    starts.push(cur);
    cur = advanceBucket(cur, granularity);
  }
  return starts;
}

/** `bucket_end` of the bucket starting at `start` (Python `bucket_end`). */
export function bucketEnd(start: number, granularity: Granularity): number {
  const d = new Date(start);
  if (granularity === "day") return start;
  if (granularity === "week") return start + 6 * DAY;
  if (granularity === "month") return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) - DAY;
  return Date.UTC(d.getUTCFullYear(), 11, 31);
}

/** ISO date `days` from `iso` (negative = before), UTC calendar arithmetic. */
export function isoAfterDays(iso: string, days: number): string {
  return new Date(Date.UTC(
    Number(iso.slice(0, 4)),
    Number(iso.slice(5, 7)) - 1,
    Number(iso.slice(8, 10)) + days,
  )).toISOString().slice(0, 10);
}

/** UTC calendar date — the server container runs UTC, so `dt.date.today()`
 *  and this must agree or performance valuation picks different price rows. */
export function utcToday(): string {
  return new Date().toISOString().slice(0, 10);
}

// --- shared input-shape helpers ----------------------------------------------

/** Newest row on-or-before `day` — count of rows with date ≤ day (bisect_right). */
export function countOnOrBefore(rows: ReadonlyArray<{ date: string }>, day: string): number {
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].date <= day) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Regroup a flat /prices page (date DESC globally) into per-security
 *  ascending history — the client twin of PriceCache's (security, date) load. */
export function groupPrices(
  rows: ReadonlyArray<{ security_id: number; date: string; price: string }>,
): Map<number, Array<{ date: string; price: string }>> {
  const grouped = new Map<number, Array<{ date: string; price: string }>>();
  for (const row of rows) {
    const list = grouped.get(row.security_id);
    const entry = { date: row.date, price: row.price };
    if (list) list.push(entry);
    else grouped.set(row.security_id, [entry]);
  }
  for (const list of grouped.values()) {
    list.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  }
  return grouped;
}

/** Ascending-by-date copy of an fx range (GET /fx guarantees ascending, but
 *  the bisect above requires it — sort defensively like loadRates does). */
export function ratesAsc<T extends { date: string }>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}
