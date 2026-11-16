// The app-wide honest-percent DISPLAY rule (M105, docs/09 §Percent display),
// lifted out of the component that first shipped it so every surface that
// prints a percentage applies the same three moves.
//
//   1. The engine/client value arrives as `[-]digits.<exactly 8 decimals>` —
//      the wire contract, never re-parsed or re-rounded on the way in.
//   2. What the eye sees is a TRIM: `String.slice` down to 2 decimals, so no
//      digit on screen is invented by a second rounding.
//   3. Everything the trim hides stays reachable — the raw string feeds the
//      element's `aria-label` (and any tooltip), so assistive tech reads the
//      real number rather than display sugar.
//
// Fail-open: anything that is not exactly `[-]digits.8dp` comes back untouched
// and renders as it always did, so an unexpected shape is shown honestly
// instead of being mis-parsed.

/** `[-]digits.<exactly 8 decimals>` — every percentage the app emits. */
const WIRE_PCT = /^(-?)(\d+)\.(\d{8})$/;

/** Display-only 2-decimal slice of a wire percentage (M105). */
export function trimPct(value: string): string {
  const match = WIRE_PCT.exec(value);
  if (match === null) return value;
  return `${match[1]}${match[2]}.${match[3].slice(0, 2)}`;
}

/**
 * Tone always comes from the RAW wire string (M105 ruling): a muted zero means
 * the engine's exact `"0.00000000"`, never a value that merely TRIMS to zero —
 * `0.00000123%` still reads as a gain while displaying `+0.00%`, so a nonzero
 * value never hides behind a muted zero.
 */
export function toneOf(value: string): string {
  if (value === "0.00000000") return "text-muted";
  return value.startsWith("-") ? "text-danger" : "text-gain";
}

/** `+21.38%` / `-0.07%` / unsigned muted `0.00%`; `null` stays "No data". */
export function formatPct(value: string | null): string {
  if (value === null) return "No data";
  const shown = trimPct(value);
  return value === "0.00000000"
    ? `${shown}%`
    : `${value.startsWith("-") ? "" : "+"}${shown}%`;
}

/**
 * A share of a whole as the app's 8-decimal wire shape.
 *
 * `sharePct` is computed here rather than read off the wire (a share is a
 * client-side ratio, there is no server string for it), but it is produced in
 * the SAME `[-]digits.8dp` shape on purpose so {@link trimPct} — and therefore
 * the whole trim/aria contract above — applies to it unchanged. Dividing by a
 * zero total is undefined, not 0%: that returns `null` and callers render
 * "No data" instead of a confident `0.00%` (M65 honesty rule).
 */
export function sharePct(part: number, total: number): string | null {
  if (!(total > 0) || !Number.isFinite(part)) return null;
  return ((part / total) * 100).toFixed(8);
}
