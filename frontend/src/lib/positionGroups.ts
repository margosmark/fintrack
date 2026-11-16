// M146 — the Positions table's BY SECURITY fold (docs/09 §Positions by
// security). A PURE client-side roll-up over the `/portfolio/positions`
// payload the page already holds: no request, no new server field, and
// nothing recomputed that the server already computed. Same shape as the
// other S3-style folds (e.g. the retired `s3Engine::subscriptionsReport`,
// removed by M160): a pure
// function the tests can drive without a browser.
//
// Three rules the whole module exists to keep:
//
//   1. GROUP KEY IS THE `security_id`, NEVER THE SYMBOL. Two listings can
//      carry the same bare ticker across exchanges (M143), so folding on the
//      string would merge two different securities into one row.
//   2. PERCENTS ARE VALUE-WEIGHTED, NEVER AN AVERAGE OF PERCENTS. A 2×
//      holding must tilt the group's % toward itself; the mean of the rows'
//      percentages would let a $50 stake outweigh a $50,000 one.
//   3. NULLS STAY NULL. `market_value_cad` / `book_cost_cad` /
//      `day_change_cad` are each `string | null` on the wire (no price /
//      missing pool / no prior close). A column with NO known value folds to
//      `null` — the caller renders the "no price" / em-dash state. A zero
//      here would be an invented figure, which docs/09 forbids everywhere.
//
// OUT OF SCOPE (docs/05 §ACB): this fold feeds NO tax math. `book_cost_cad`
// is summed only because the group row DISPLAYS it, and each row's book cost
// stays whatever its own account's rule produced (persisted pool ACB for
// non-registered, average-cost replay for registered). The sum is NOT a
// pooled ACB and nothing downstream may treat it as one.
//
// The portfolio-level Σ row is deliberately ABSENT here: it stays the
// payload's own `total_value_cad` / `total_unrealized_cad` /
// `total_day_change_*`, rendered by the shared `<tfoot>` in both views, so
// the two views cannot show different totals.

import type { Position } from "../api/portfolio";

/** One security's roll-up across every account that holds it. */
export interface PositionGroup {
  /** The GROUP KEY — the security's id. Never the symbol (see rule 1). */
  security_id: number;
  symbol: string;
  /** Every row of a security shares the listing currency, so a native sum
   *  inside a group is a single-currency sum (unlike the portfolio total the
   *  payload refuses to compute). */
  listing_currency: string;
  /** Per-account rows, in the payload's own order. */
  rows: Position[];
  /** How many accounts hold it — the "N accounts" chip (the duplicated-
   *  holding signal). 1 → the caller renders the row FLAT, no chevron. */
  account_count: number;
  /** Σ quantity, up to 6 dp (docs/02), as a Decimal string. */
  quantity: string;
  /** Σ `book_cost_cad` over rows whose cost is known; null when none is.
   *  Display only — see the ACB note above. */
  book_cost_cad: string | null;
  /** Σ `market_value_cad` over rows that could be valued; null when none. */
  market_value_cad: string | null;
  /** Σ `market_value_native` — a single currency, so this sum is honest. */
  market_value_native: string | null;
  /** Σ `day_change_cad` over rows with a prior close; null when none. */
  day_change_cad: string | null;
  /** Σ day Δ ÷ Σ (value − day Δ), percent — VALUE-WEIGHTED (rule 2). */
  day_change_pct: number | null;
  /** Σ `unrealized_gain_cad` over rows with a known book cost; null when none. */
  unrealized_gain_cad: string | null;
  /** Σ gain ÷ Σ book cost, percent — the same value-weighted shape as the
   *  day %. */
  unrealized_gain_pct: number | null;
  /** ONE sparkline: the first non-empty row's closes. Every row of a security
   *  is the same close series, so per-account sparks would draw it N times. */
  spark: string[];
  /** The security's cached quote. Price / source / age are facts about the
   *  SECURITY, so any row carries the group's answer; rows of one security
   *  agree by construction. */
  quote: Position;
}

/** Half-away-from-zero — the display rounding for the client-computed group
 *  percents (the server quantizes HALF_EVEN; an exact 0.005 tie can differ,
 *  which is why these figures are sums of the SERVER's own cent-rounded
 *  fields rather than a second valuation). */
function roundHalfAway(n: number): number {
  return n < 0 ? -Math.round(-n) : Math.round(n);
}

/** 2 dp, half away from zero. */
function round2(n: number): number {
  return roundHalfAway(n * 100) / 100;
}

/** A Decimal-string money field → integer cents, or null when absent. */
function cents(value: string | null | undefined): number | null {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? roundHalfAway(n * 100) : null;
}

/** Integer cents → the Decimal string `fmtCad` expects. */
function fromCents(value: number): string {
  return (value / 100).toFixed(2);
}

/** Σ of a cent-quantized money column over the rows where it is known.
 *  `null` when NO row carries one — never a fabricated 0. */
function sumMoney(
  rows: readonly Position[],
  field: "market_value_cad" | "market_value_native" | "book_cost_cad" | "day_change_cad" | "unrealized_gain_cad",
): number | null {
  let total = 0;
  let seen = false;
  for (const row of rows) {
    const value = cents(row[field]);
    if (value === null) continue;
    total += value;
    seen = true;
  }
  return seen ? total : null;
}

/** Σ quantity in 1e-6 units (quantities carry up to 6 dp, docs/02), returned
 *  as the trimmed Decimal string `fmtQuantity` renders. */
function sumQuantity(rows: readonly Position[]): string {
  let total = 0;
  for (const row of rows) {
    const n = Number(row.quantity);
    if (Number.isFinite(n)) total += roundHalfAway(n * 1_000_000);
  }
  return String(Number((total / 1_000_000).toFixed(6)));
}

/** Σ numerator ÷ Σ denominator as a percent — the value-weighted rule.
 *  `null` on an empty or zero denominator (no invented 0, no Infinity). */
function weightedPct(numerator: number | null, denominator: number | null): number | null {
  if (numerator === null || denominator === null || denominator === 0) return null;
  return round2((numerator / denominator) * 100);
}

/**
 * Fold the payload's rows into one group per security, sorted by Σ market
 * value CAD descending — the own-position reading order. A group with no
 * value anywhere sorts LAST, never as a zero, and ties break on the security
 * id so the order is stable across renders.
 */
export function groupPositionsBySecurity(items: readonly Position[]): PositionGroup[] {
  const bySecurity = new Map<number, Position[]>();
  for (const row of items) {
    const bucket = bySecurity.get(row.security_id);
    if (bucket === undefined) bySecurity.set(row.security_id, [row]);
    else bucket.push(row);
  }

  const groups: PositionGroup[] = [];
  for (const [securityId, rows] of bySecurity) {
    groups.push(buildGroup(securityId, rows));
  }
  return groups.sort(compareByValue);
}

/** One security's rows → its roll-up. */
function buildGroup(securityId: number, rows: Position[]): PositionGroup {
  const quote = rows[0];

  // Day %: Σ day Δ ÷ Σ (value − day Δ), where "value − day Δ" is the value
  // BEFORE the move. Numerator and denominator are counted over the SAME
  // rows — a ratio whose two halves came from different row sets would
  // describe a portfolio that does not exist.
  let dayNumerator: number | null = null;
  let dayDenominator: number | null = null;
  let dayN = 0;
  let dayD = 0;
  let dayRows = 0;
  for (const row of rows) {
    const change = cents(row.day_change_cad);
    const value = cents(row.market_value_cad);
    if (change === null || value === null) continue;
    dayN += change;
    dayD += value - change;
    dayRows += 1;
  }
  if (dayRows > 0) {
    dayNumerator = dayN;
    dayDenominator = dayD;
  }

  // Gain %: Σ gain ÷ Σ book cost, over the rows where BOTH are known — the
  // mirror of the day %, and the same reason the two halves must be counted
  // over one row set (a row with a gain but no book cost cannot exist, but
  // the guard keeps the ratio honest if the wire ever changes).
  let gainNumerator: number | null = null;
  let gainDenominator: number | null = null;
  let gainN = 0;
  let gainD = 0;
  let gainRows = 0;
  for (const row of rows) {
    const gain = cents(row.unrealized_gain_cad);
    const book = cents(row.book_cost_cad);
    if (gain === null || book === null) continue;
    gainN += gain;
    gainD += book;
    gainRows += 1;
  }
  if (gainRows > 0) {
    gainNumerator = gainN;
    gainDenominator = gainD;
  }

  const marketValue = sumMoney(rows, "market_value_cad");
  const marketValueNative = sumMoney(rows, "market_value_native");
  const bookCost = sumMoney(rows, "book_cost_cad");
  const dayChange = sumMoney(rows, "day_change_cad");
  const unrealized = sumMoney(rows, "unrealized_gain_cad");

  return {
    security_id: securityId,
    symbol: quote.symbol,
    listing_currency: quote.listing_currency,
    rows,
    account_count: rows.length,
    quantity: sumQuantity(rows),
    book_cost_cad: bookCost === null ? null : fromCents(bookCost),
    market_value_cad: marketValue === null ? null : fromCents(marketValue),
    market_value_native: marketValueNative === null ? null : fromCents(marketValueNative),
    day_change_cad: dayChange === null ? null : fromCents(dayChange),
    day_change_pct: weightedPct(dayNumerator, dayDenominator),
    unrealized_gain_cad: unrealized === null ? null : fromCents(unrealized),
    unrealized_gain_pct: weightedPct(gainNumerator, gainDenominator),
    spark: rows.find((row) => row.spark.length >= 2)?.spark ?? [],
    quote,
  };
}

/** Σ market value CAD desc; an unvalued group sorts last (never as 0). */
function compareByValue(a: PositionGroup, b: PositionGroup): number {
  const left = a.market_value_cad === null ? null : Number(a.market_value_cad);
  const right = b.market_value_cad === null ? null : Number(b.market_value_cad);
  if (left === null && right === null) return a.security_id - b.security_id;
  if (left === null) return 1;
  if (right === null) return -1;
  if (left === right) return a.security_id - b.security_id;
  return right - left;
}
