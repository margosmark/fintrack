// Single source of money/number formatting (scout #7 / #4). All currency
// formatting is en-CA so every surface agrees (docs/09 money displays); the
// two CAD helpers render a dash ("—") for null/invalid Decimals instead of
// letting NaN bleed into the UI.
//
// M139 (docs/09 § Display masking): every helper takes an OPTIONAL `masked`
// flag. When it is omitted the helper consults the session mask, so the ~200
// existing call sites mask themselves with no edit at all — fail-closed, which
// is the right direction for a privacy feature. A surface that must NOT mask
// (D4: the import wizard, Settings, receipts, an export CONFIRM preview) passes
// `masked: false` explicitly. Percentages are the one inversion: they stay
// visible by default (the operator ruling — proportional values are not
// sensitive) and opt IN to masking at the one derived-from-cost surface.

import { MASK_TEXT, isMasked } from "./mask";

const CAD = new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD" });
const CAD_WHOLE = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "CAD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** Resolve the flag: explicit wins, otherwise the ambient session mask. */
function resolveMask(masked?: boolean): boolean {
  return masked ?? isMasked();
}

/**
 * Rounding knob shared by `formatCad`. `0` is the whole-dollar display the
 * budget/goal/insight cards use (`$1,235` beside a progress bar); the default
 * is the two-decimal display every other money surface uses. Deliberately a
 * two-state union, not a free `number`: these are the only two precisions the
 * app renders, and an arbitrary one would be a new display nobody specified.
 */
export interface CadFormatOptions {
  fractionDigits?: 0 | 2;
}

/**
 * Formats a Decimal-as-string CAD amount; null/blank/non-numeric renders as
 * "—" — never a fabricated `$0.00` (`Number("") === 0` would invent a figure
 * the wire never sent, the same M145 reasoning that `formatMoney` documents).
 * Ten page-local `$`-shims were collapsed into this one helper; they disagreed
 * on exactly that fallback (nine printed `$0`, Debts printed `—`), so the
 * shared helper settles it on the honest one.
 */
export function formatCad(
  value: string | number | null | undefined,
  masked?: boolean,
  opts: CadFormatOptions = {},
): string {
  if (resolveMask(masked)) return MASK_TEXT;
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" && value.trim() === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(n)) return "—";
  return opts.fractionDigits === 0 ? CAD_WHOLE.format(n) : CAD.format(n);
}

/** Compact CAD for tight spots ($1.2k / $345). */
export function formatCadCompact(n: number, masked?: boolean): string {
  if (resolveMask(masked)) return MASK_TEXT;
  return new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency: "CAD",
    notation: Math.abs(n) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: Math.abs(n) >= 10_000 ? 1 : 2,
  }).format(n);
}

/**
 * Formats a numeric string/number in its native currency, e.g. "$1,234.56".
 * M145 (docs/08 §cash_balance): a `null` money field means NO HONEST FIGURE
 * (an investment account's foreign cash leg past the M115 fx window — the
 * same gap the chart draws), so it renders as "—" like the CAD helpers —
 * `Number(null) === 0` would fabricate a "$0.00" the data never claimed.
 */
export function formatMoney(
  value: string | number | null | undefined,
  currency: string,
  masked?: boolean,
): string {
  if (resolveMask(masked)) return MASK_TEXT;
  if (value === null || value === undefined) return "—";
  const amount = typeof value === "string" ? Number(value) : value;
  try {
    return new Intl.NumberFormat("en-CA", { style: "currency", currency }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}
