/**
 * Partner-view budget progress — the audited client-only compute (M160,
 * docs/33 §Server-first rule 5 / rule 8, R2 decision on t_4fea992a).
 *
 * RULING (M160-R1): `budgetsProgress` / `etToday` / `RateRow` SURVIVE. This
 * is NOT mirror code: there is no server half to retire. `GET /partner/budgets`
 * deliberately returns budget DEFINITIONS only (backend/app/api/partner.py —
 * D3 / docs/43 §9: the server must not read the owner's hidden accounts into
 * the partner's answer), so `api/sharing.ts::loadSharedView` computes
 * `spent` / `carried_in` / `remaining` / `pct_used` over the PARTNER-visible
 * rows it already holds. No parity pass is producible for it (M135 §4.1 is
 * unsatisfiable by design, not by defect) and docs/33 rules 4/7 forbid the
 * silent-empty regression — hence an explicit Row-8-class disposition row in
 * the docs/33 inventory, with lock behavior stated there.
 *
 * Carved verbatim out of the retired `lib/s3Engine.ts` (M160). The private
 * fixed-point block below is deliberately KEPT self-contained: the s3 port
 * rounds with ROUND_HALF_EVEN (`dquantize`) and 28-significant-digit
 * division (`ddivSig`) — Python's default `decimal` context — while
 * `lib/dec.ts`'s shared cluster rounds HALF_UP. Merging the two would move
 * `pct_used`'s 10.353333333333333 golden. The golden itself
 * (`s3-vectors.json::budgets.progress`) is still pinned, lock-invariance
 * included, by `tests/lockedVaultDegradation.test.ts` → “A13”.
 */

import type { Budget } from "../api/budgets";
import type { Category } from "../api/categories";
import type { Transaction } from "../api/transactions";
import { EngineError, type Dec } from "./dec";

// ---------------------------------------------------------------------------
// fixed-point Decimal (Python `decimal.Decimal`, default context) — private
// to this module on purpose (see the header).
// ---------------------------------------------------------------------------

function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

/** Parse a decimal string exactly (incl. `1e-7` style exponents). */
function dec(text: string): Dec {
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

function dzero(scale = 0): Dec {
  return { v: 0n, s: scale };
}

function dadd(a: Dec, b: Dec): Dec {
  if (a.s === b.s) return { v: a.v + b.v, s: a.s };
  const s = Math.max(a.s, b.s);
  return { v: a.v * pow10(s - a.s) + b.v * pow10(s - b.s), s };
}

function dsub(a: Dec, b: Dec): Dec {
  return dadd(a, { v: -b.v, s: b.s });
}

function dmul(a: Dec, b: Dec): Dec {
  return { v: a.v * b.v, s: a.s + b.s };
}

/** Dec × plain integer (scale unchanged — Decimal * int is exact). */
function dmulInt(a: Dec, n: number | bigint): Dec {
  return { v: a.v * BigInt(n), s: a.s };
}

function dcmp(a: Dec, b: Dec): number {
  const s = Math.max(a.s, b.s);
  const av = a.v * pow10(s - a.s);
  const bv = b.v * pow10(s - b.s);
  return av < bv ? -1 : av > bv ? 1 : 0;
}

function disZero(a: Dec): boolean {
  return a.v === 0n;
}

/** Round to `scale` with ROUND_HALF_EVEN (Python's default context). */
function dquantize(a: Dec, scale: number): Dec {
  if (a.s <= scale) return { v: a.v * pow10(scale - a.s), s: scale };
  const p = pow10(a.s - scale);
  const negative = a.v < 0n;
  const magnitude = negative ? -a.v : a.v;
  let q = magnitude / p;
  const r = magnitude % p;
  const twice = r * 2n;
  if (twice > p) q += 1n;
  else if (twice === p && q % 2n === 1n) q += 1n; // half -> even
  return { v: negative ? -q : q, s: scale };
}

/**
 * `Decimal.__truediv__` under the default context: the quotient rounded
 * half-even to `sig` SIGNIFICANT digits (Python uses precision=28).
 *
 * Only the exponent needs care — a value below 1 gets a deeper scale than
 * `sig - 1`, e.g. 62.12/600 = 0.1035333… with 28 significant digits means
 * scale 28, not 27.
 */
function ddivSig(a: Dec, b: Dec, sig = 28): Dec {
  if (b.v === 0n) throw new EngineError(500, "decimal division by zero", "internal_error");
  // (a.v / 10^a.s) / (b.v / 10^b.s) = a.v·10^b.s / (b.v·10^a.s)
  const N = a.v * pow10(b.s);
  const D = b.v * pow10(a.s);
  const negative = (N < 0n) !== (D < 0n);
  const an = N < 0n ? -N : N;
  const ad = D < 0n ? -D : D;
  if (an === 0n) return dzero(Math.max(0, sig - 1));

  // m such that 10^m <= |N/D| < 10^(m+1)
  let m: number;
  if (an >= ad) {
    m = (an / ad).toString().length - 1;
  } else {
    let t = 1;
    while (an * pow10(t) < ad) t += 1;
    m = -t;
  }
  const scale = sig - 1 - m;
  if (scale < 0) throw new EngineError(500, "decimal quotient exceeds precision", "internal_error");
  const scaled = an * pow10(scale);
  let q = scaled / ad;
  const r = scaled % ad;
  const twice = r * 2n;
  if (twice > ad) q += 1n;
  else if (twice === ad && q % 2n === 1n) q += 1n;
  return { v: negative ? -q : q, s: scale };
}

/** `str(Decimal)` — fixed notation, scale preserved, no exponent. */
function dstr(a: Dec): string {
  const negative = a.v < 0n;
  const digits = (negative ? -a.v : a.v).toString();
  if (a.s === 0) return negative ? `-${digits}` : digits;
  const padded = digits.padStart(a.s + 1, "0");
  const cut = padded.length - a.s;
  const body = `${padded.slice(0, cut)}.${padded.slice(cut)}`;
  return negative ? `-${body}` : body;
}

/** Python's `float(Decimal)` — nearest double of the exact decimal value. */
function dfloat(a: Dec): number {
  return Number(dstr(a));
}

// ---------------------------------------------------------------------------
// Budgets (M25/M37; GET /budgets kept as the definition read path)
// ---------------------------------------------------------------------------

export interface BudgetProgress extends Budget {
  category_name: string;
  spent: string;
  /** M110: the frozen carry INTO this period — `null` when nothing carried. */
  carried_in: string | null;
  /** `amount + carried_in − spent` (docs/09 §Budget carry). */
  remaining: string;
  /** `spent ÷ (amount + carried_in) × 100`, unclamped — `null` if no pool. */
  pct_used: number | null;
}

/**
 * A definition as `GET /budgets` and `GET /partner/budgets` publish it: the
 * M110 `carried_in` rides alongside (the definitions-only POST/PATCH shapes
 * do not carry it, so it is optional on the input and always answered out).
 */
export type BudgetDefinition = Budget & { readonly carried_in?: string | null };

export interface RateRow {
  date: string;
  rate: string;
}

/** `FxCache.rate` — newest cached base->quote rate on or before `day`. */
function rateOn(rows: ReadonlyArray<RateRow> | undefined, day: string): Dec | null {
  if (rows === undefined || rows.length === 0) return null;
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].date <= day) lo = mid + 1;
    else hi = mid;
  }
  return lo === 0 ? null : dec(rows[lo - 1].rate);
}

export interface BudgetProgressInputs {
  /** Definitions from `GET /budgets` (the server's progress fields are discarded). */
  budgets: readonly BudgetDefinition[];
  categories: readonly Category[];
  transactions: readonly Transaction[];
  /** Cache-only fx rows per base, ascending by date (`GET /fx?fetch=false`). */
  rates: ReadonlyMap<string, ReadonlyArray<RateRow>>;
  /** INJECTED `today_et()` (America/Toronto). */
  today: string;
  /** `get_settings().reporting_currency` — pinned, no endpoint exposes it. */
  reporting?: string;
}

/**
 * `_budget_spent` + `list_budgets` ported together.
 *
 * Signed reporting-currency net spend floored at 0, `cad_amount` mirror first
 * then the newest cached rate on or before the row's date, unresolved rows
 * EXCLUDED (never fetched — docs/12). Period start comes from the injected
 * `today`; `pct_used` is `float(spent / spendable * 100)` — the TRUE ratio,
 * never clamped (M61 #3 / M65: a 202% overspend read as "100% used") — where
 * `spendable = amount + carried_in` (M110: `carried_in` is its own layer, and
 * `null` when nothing carried leaves the pre-M110 math untouched). The division
 * goes through 28 significant Decimal digits before becoming a double (plain
 * `62.12/600*100` is 10.353333333333332 in IEEE-754, while Python's Decimal
 * path yields 10.353333333333333 — the golden pins it). Only the BAR caps its
 * width at 100%; the label does not (Budgets.tsx / BudgetCards.tsx).
 */
export function budgetsProgress(inputs: BudgetProgressInputs): BudgetProgress[] {
  const reporting = inputs.reporting ?? "CAD";
  const catNames = new Map(inputs.categories.map((c) => [c.id, c.name]));
  const year = inputs.today.slice(0, 4);
  const month = inputs.today.slice(5, 7);

  const result = inputs.budgets.map((budget) => {
    const periodStart = budget.period === "yearly" ? `${year}-01-01` : `${year}-${month}-01`;
    let net = dzero();
    for (const t of inputs.transactions) {
      if (t.category_id !== budget.category_id || t.date < periodStart) continue;
      if (t.cad_amount !== null && t.cad_amount !== undefined) {
        net = dadd(net, dec(t.cad_amount));
        continue;
      }
      if (t.currency === reporting) {
        net = dadd(net, dec(t.amount_native));
        continue;
      }
      const rate = rateOn(inputs.rates.get(t.currency), t.date);
      if (rate !== null) net = dadd(net, dmul(dec(t.amount_native), rate));
      // unresolved -> excluded, never fetched and never summed at face value
    }
    const negated = { v: -net.v, s: net.s };
    const spent = dquantize(dcmp(negated, dzero()) < 0 ? dzero() : negated, 2);
    const amount = dec(budget.amount);
    // M110 (docs/09 §Budget carry) — the one layering both implementations
    // share: `carried_in` is its OWN layer, never merged into `amount`.
    // Absent / null (carry off, nothing carried) runs today's math untouched.
    const carriedText = budget.carried_in ?? null;
    const carried = carriedText === null ? null : dec(carriedText);
    const pool = carried === null ? amount : dadd(amount, carried);
    const remaining = dsub(pool, spent);
    let pct: number | null;
    if (carried === null) {
      pct = disZero(amount) ? 0 : dfloat(dmulInt(ddivSig(spent, amount), 100));
    } else if (dcmp(pool, dzero()) <= 0) {
      // No positive denominator: the honest answer is "no ratio exists"
      // (`null`) — never a fabricated 0% and never a clamped 100%.
      pct = null;
    } else {
      pct = dfloat(dmulInt(ddivSig(spent, pool), 100));
    }
    return {
      id: budget.id,
      user_id: budget.user_id,
      category_id: budget.category_id,
      amount: budget.amount,
      period: budget.period,
      carry: budget.carry,
      category_name: catNames.get(budget.category_id) ?? "",
      spent: dstr(spent),
      carried_in: carriedText,
      remaining: dstr(remaining),
      pct_used: pct,
    };
  });
  // Server order is `ORDER BY Category.name` under the DB's `en_US.utf8`
  // collation; like the old reportEngine this port compares UTF-16 code
  // units. Both agree on the captured goldens (all-lowercase ASCII names)
  // and can diverge only when the deciding character differs by case/accent
  // (`Zulu` vs `alpha`) — display order, not a numeric contract (docs/08).
  result.sort((a, b) => (a.category_name < b.category_name ? -1 : a.category_name > b.category_name ? 1 : 0));
  return result;
}

/** `today_et()` (America/Toronto) — the single "today" for this path. */
export function etToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Toronto",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
