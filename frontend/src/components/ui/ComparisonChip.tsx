// ComparisonChip (M185, docs/47 §9.3) — a comparison NEVER appears without
// its basis: value + signed delta + basis text in one pill.
//
// The basis is derived from PAYLOAD DATES ONLY — `vs <previous bucket label>`
// for dashboard KPIs (the two cashflow/net-worth buckets already on screen)
// or `vs {previous_close_date} close` for the portfolio header (server field,
// docs/09 §Portfolio header). Never "yesterday", never a wall-clock
// derivation (G1).
//
// A11y (docs/47 §6.5): the chip's accessible text is the FULL SENTENCE
// ("Net worth up $2,140.55 versus September 13"), not "▲" — the visible
// group is decorative to assistive tech and the sentence carries every word
// of it, so state never rides on the arrow glyph or on colour alone.

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

export type ComparisonTone = "gain" | "loss" | "flat";

const TONES: Record<ComparisonTone, string> = {
  gain: "border-gain/30 bg-gain/10 text-gain",
  loss: "border-danger/30 bg-danger/10 text-danger",
  flat: "border-rule bg-veil/[0.04] text-muted",
};

const ICONS = {
  gain: ArrowUpRight,
  loss: ArrowDownRight,
  flat: Minus,
} as const;

export interface ComparisonChipProps {
  /** The signed delta as already-formatted money (mask-aware at the call
   *  site). Omit for a value with no comparison — a fabricated delta is
   *  worse than none (docs/47 §9.2). */
  delta?: string;
  tone?: ComparisonTone;
  /** The payload-derived comparison window, e.g. "vs Sep 13" / "vs Oct 12 close". */
  basis: string;
  /**
   * The full sentence for assistive tech. Required when `delta` is present —
   * the visible delta + basis are aria-hidden behind it.
   */
  ariaLabel?: string;
  /** A non-money annotation chip (e.g. "ledger balance · CAD + USD"). */
  plain?: boolean;
}

export function ComparisonChip({
  delta,
  tone = "flat",
  basis,
  ariaLabel,
  plain = false,
}: ComparisonChipProps) {
  const Icon = ICONS[tone];
  const body = (
    <>
      {!plain && delta !== undefined && (
        <span className="inline-flex items-center gap-1">
          <Icon className="size-3.5" aria-hidden />
          <span className="tabular">{delta}</span>
        </span>
      )}
      {plain && <span>{delta ?? basis}</span>}
      {!plain && <span className="font-medium opacity-90">{basis}</span>}
    </>
  );
  return (
    <span
      className={`inline-flex h-6 max-w-full items-center gap-1.5 truncate rounded-full border px-2.5 text-xs font-semibold tabular ${TONES[plain ? "flat" : tone]}`}
    >
      {ariaLabel !== undefined ? (
        <>
          {/* The wrapper MUST stay a flex row with the chip's own gap: as a
              plain inline span it drops its children out of the outer chip's
              flex layout, the `gap-1.5` stops applying to them, and the
              amount runs straight into the basis — "$2Kvs Nov 1". */}
          <span aria-hidden className="inline-flex items-center gap-1.5">
            {body}
          </span>
          <span className="sr-only">{ariaLabel}</span>
        </>
      ) : (
        body
      )}
    </span>
  );
}
