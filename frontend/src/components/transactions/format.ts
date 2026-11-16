// Display helpers shared by this slice's pages (Dashboard / Transactions /
// Imports / suggestion panel). Backend Decimals arrive as JSON strings.
import { ApiError } from "../../api/client";
import { formatMoney } from "../../lib/format";

/** Native-currency formatter (en-CA); re-exported from the shared lib. */
export { formatMoney };

/**
 * Native-amount cell text: native currency always, plus a muted CAD
 * approximation when the row carries one (docs/09 money displays).
 * M139: both halves are absolute amounts, so the pair masks as one unit.
 */
export function formatWithCad(
  value: string | number,
  currency: string,
  cadAmount: string | null,
  masked?: boolean,
): string {
  const native = formatMoney(value, currency, masked);
  if (cadAmount === null || currency === "CAD") return native;
  return `${native} (≈ ${formatMoney(cadAmount, "CAD", masked)})`;
}

export function apiErrorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong";
}
