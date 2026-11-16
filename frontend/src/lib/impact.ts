// M129 — the security page's day-delta + own-position impact arithmetic
// (docs/09 §Day change & own-position impact).
//
// Every input is a SERVER-provided primitive: a cached close, a quantity, and
// the server's own `fx_rate_to_cad`. These helpers only RE-EVALUATE that
// arithmetic at a price the SSE stream just delivered, so the page can show
// the tick instantly and label it "live"; the next server payload replaces
// every figure with its own (docs/09 §Live prices). The positions TABLE
// never uses this module — its valuation stays server-owned end to end.

import { MASK_TEXT, isMasked } from "./mask";

/** Explicit `masked` wins; otherwise the session mask (fail-closed). */
function masked_(masked?: boolean): boolean {
  return masked ?? isMasked();
}

/** One security's day move, native. `pct` is null on a zero previous close. */
export interface DayDelta {
  change: number;
  pct: number | null;
}

/**
 * The move from `price` to `previousClose`. Null when either side is unknown
 * (no close yet, or a first-day listing with no prior session) — callers
 * withold the delta line entirely rather than printing a fabricated zero.
 */
export function dayDelta(
  price: string | null | undefined,
  previousClose: string | null | undefined,
): DayDelta | null {
  if (price == null || previousClose == null) return null;
  const p = Number(price);
  const prev = Number(previousClose);
  if (!Number.isFinite(p) || !Number.isFinite(prev)) return null;
  return {
    change: p - prev,
    pct: prev === 0 ? null : Math.round(((p - prev) / prev) * 10000) / 100,
  };
}

/** Half-up to cents for the provisional live figures (the server rounds
 *  Decimal HALF_EVEN; the ≤0.5-cent gap is exactly why they are labelled
 *  "live" and replaced on the next payload). */
export function toCents(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

/** The per-row inputs `liveImpact` needs — a subset of the positions rows
 *  the overview payload already carries for this security. */
export interface ImpactRow {
  quantity: string;
  book_cost_cad: string | null;
  fx_rate_to_cad: string | null;
}

export interface LiveImpact {
  /** Σ cents(qty × price × rate). */
  marketValueCad: number | null;
  /** marketValueCad − Σ book over rows whose book cost is known. */
  unrealizedCad: number | null;
  /** unrealizedCad ÷ that book sum, percent; null when book is 0/unknown. */
  unrealizedPct: number | null;
  /** Σ cents(qty × Δ × rate) — the day's move on your shares. */
  dayCad: number | null;
}

const EMPTY_IMPACT: LiveImpact = {
  marketValueCad: null,
  unrealizedCad: null,
  unrealizedPct: null,
  dayCad: null,
};

/**
 * Re-evaluate the impact block at a LIVE price (M129 tick). Rows with an
 * unknown rate or quantity contribute nothing; the results are null unless
 * at least one row could be valued — same "no data, not zero" contract the
 * server fields follow.
 */
export function liveImpact(
  rows: ImpactRow[],
  livePrice: string | null | undefined,
  previousClose: string | null | undefined,
): LiveImpact {
  const price = Number(livePrice);
  if (livePrice == null || !Number.isFinite(price)) return EMPTY_IMPACT;
  const prev = previousClose == null ? null : Number(previousClose);
  const hasPrev = prev !== null && Number.isFinite(prev);

  let marketValue = 0;
  let valueRows = 0;
  let gain = 0;
  let book = 0;
  let gainRows = 0;
  let day = 0;
  let dayRows = 0;

  for (const row of rows) {
    const qty = Number(row.quantity);
    const rate = row.fx_rate_to_cad == null ? null : Number(row.fx_rate_to_cad);
    if (!Number.isFinite(qty) || rate === null || !Number.isFinite(rate)) continue;
    const value = toCents(qty * price * rate);
    marketValue += value;
    valueRows += 1;
    if (row.book_cost_cad != null) {
      const bookRow = Number(row.book_cost_cad);
      if (Number.isFinite(bookRow)) {
        gain += value - bookRow;
        book += bookRow;
        gainRows += 1;
      }
    }
    if (hasPrev) {
      day += toCents(qty * (price - (prev as number)) * rate);
      dayRows += 1;
    }
  }

  if (valueRows === 0) return EMPTY_IMPACT;
  return {
    marketValueCad: toCents(marketValue),
    unrealizedCad: gainRows > 0 ? toCents(gain) : null,
    unrealizedPct: gainRows > 0 && book !== 0 ? Math.round((gain / book) * 10000) / 100 : null,
    dayCad: dayRows > 0 ? toCents(day) : null,
  };
}

// --- M130 — the portfolio header's live tick (docs/09 §Portfolio header) -----

/** The per-row inputs the portfolio headline re-evaluates from: the server's
 *  own quantity / price / baseline / rate, nothing the client invented. */
export interface PortfolioRow {
  quantity: string;
  /** The CURRENT price — the SSE patch may have replaced the server's. */
  price: string | null;
  previous_close_native: string | null;
  fx_rate_to_cad: string | null;
}

export interface LivePortfolio {
  /** Σ cents(qty × price × rate) over rows that could be valued. */
  holdingsCad: number | null;
  /** holdingsCad + the server's cash — `null` when the cash leg is unknown,
   *  exactly as the payload's `portfolio_value_cad` is. */
  valueCad: number | null;
  /** Σ cents(qty × (price − prior close) × rate). */
  dayCad: number | null;
}

/**
 * Re-evaluate the header's now-value and day Δ at the prices currently on
 * screen (M130's live-tick rule, docs/09 §Live prices): every multiplier is a
 * SERVER-provided primitive and the baseline is the server's own pinned prior
 * close, so a tick moves the figure the stream actually moved and nothing else
 * — cash never moves with a quote. The result is provisional by construction
 * (half-up vs the server's HALF_EVEN, ≤0.5¢ per row), which is exactly why the
 * caller labels it "live" and the next server payload replaces it.
 */
export function livePortfolio(rows: PortfolioRow[], cash: string | null): LivePortfolio {
  let holdings = 0;
  let valueRows = 0;
  let day = 0;
  let dayRows = 0;

  for (const row of rows) {
    const qty = Number(row.quantity);
    const price = row.price == null ? null : Number(row.price);
    const rate = row.fx_rate_to_cad == null ? null : Number(row.fx_rate_to_cad);
    if (!Number.isFinite(qty) || price === null || !Number.isFinite(price)) continue;
    if (rate === null || !Number.isFinite(rate)) continue;
    holdings += toCents(qty * price * rate);
    valueRows += 1;
    const prev = row.previous_close_native == null ? null : Number(row.previous_close_native);
    if (prev !== null && Number.isFinite(prev)) {
      day += toCents(qty * (price - prev) * rate);
      dayRows += 1;
    }
  }

  if (valueRows === 0) return { holdingsCad: null, valueCad: null, dayCad: null };
  const holdingsCad = toCents(holdings);
  const cashNumber = cash == null ? null : Number(cash);
  return {
    holdingsCad,
    valueCad:
      cashNumber === null || !Number.isFinite(cashNumber)
        ? null
        : toCents(holdingsCad + cashNumber),
    dayCad: dayRows > 0 ? toCents(day) : null,
  };
}

/** "+$807.33" / "−$50.00" — CAD with an explicit sign, for gain cells.
 *  M139: absolute CAD, so it masks with the session by default. */
export function fmtCadSigned(value: number | null | undefined, masked?: boolean): string {
  if (masked_(masked)) return MASK_TEXT;
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return CAD_SIGNED.format(value);
}

const CAD_SIGNED = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "CAD",
  signDisplay: "exceptZero",
});

const SIGNED_2DP = new Intl.NumberFormat("en-CA", {
  signDisplay: "exceptZero",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** "+1.43" / "−1.43" / "0.00" — the plain signed number for a delta cell.
 *  M139: a signed delta is an absolute amount (a day move, a native gain), so
 *  it masks with the session by default. */
export function fmtSigned(
  value: number | null | undefined,
  digits = 2,
  masked?: boolean,
): string {
  if (masked_(masked)) return MASK_TEXT;
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (digits === 2) return SIGNED_2DP.format(value);
  const rounded = Number(value.toFixed(digits));
  return `${rounded > 0 ? "+" : ""}${rounded.toFixed(digits)}`;
}

/** "+2.05%" — signed percent with the house's two decimals.
 *  M139 INVERSION: percentages are PROPORTIONAL, not absolute, so they stay
 *  VISIBLE by default (operator ruling) — this function takes no mask flag at
 *  all. Budget % remaining, donut slice %, spending-change %, growth/period
 *  delta % and TWR/MWR all render through it, masked session or not.
 *
 *  Use `fmtSignedPctBookCost` for the one carve-out (see below). The two are
 *  separate functions rather than one with a flag because the difference is
 *  semantic — "is this ratio derived from a masked absolute?" — and a boolean
 *  at the call site says nothing about which it is. */
export function fmtSignedPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${SIGNED_2DP.format(value)}%`;
}

/** "+4.63%" — the M139 carve-out: a percent DERIVED FROM BOOK COST.
 *  Unlike every other percentage, this FOLLOWS the session mask exactly like an
 *  amount does (`isMasked()`, not an unconditional bullet): showing `+4.63`
 *  beside a masked market value hands a determined shoulder-surfer back the
 *  very scale the mask removed. Call sites: Investments' position-level
 *  "Your gain %" column and SecurityDetail's book-vs-market gain.
 *
 *  The null/NaN check runs BEFORE the mask check on purpose (M65 honesty):
 *  a missing value is not a hidden one. Rendering `•••••` where the data is
 *  simply absent would claim a real figure is being withheld, so absence
 *  stays an em dash whether or not the mask is on. */
export function fmtSignedPctBookCost(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (isMasked()) return MASK_TEXT;
  return `${SIGNED_2DP.format(value)}%`;
}

/** The up/down arrow that pairs with a delta (colour is the caller's job —
 *  this is shape, not tone, so it never carries meaning alone). */
export function deltaArrow(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return "•";
  return value > 0 ? "▲" : "▼";
}
