// spendingFold (M185 G2/G5, docs/47 §10) — the TWO documented pure client
// folds of the wave, over `GET /reports/spending/categories` payloads.
//
// docs/47 §9.1 pins the rule: no total, ratio or sum is re-derived in the
// browser EXCEPT the two documented pure folds — M146's `positionGroups.ts`
// and this file. Both are pure functions over payload Decimal strings: no
// clock, no network, no rounding rule of their own beyond quantising to
// cents (the payload's own scale).
//
// G5 — donut fold: everything after the top 6 slices folds into one
//   `Other (n)` legend line carrying the EXACT folded remainder, and the
//   `category_id: null` slice (Uncategorized) is NEVER folded — it is the
//   action (G4), not a tail. Mirrors M167's fold discipline.
//
// G2 — category deltas: issue the SAME endpoint twice (current period + a
//   prior period of equal length) and diff client-side. The floor is an
//   ABSOLUTE $50.00 (PM ruling on PROPOSAL Q3: a relative floor makes the
//   chip set unstable across tenants), at most 3 chips, and a category with
//   no dollar delta renders nothing.

import { dabs, dadd, dcmp, dneg, dstr, dec, dsub, quantizeCents } from "./dec";

/** One category slice, exactly as `CategoryBreakdown.items` flattens it. */
export interface CategorySlice {
  /** `null` is the Uncategorized bucket (docs/12: `category_id: null`). */
  id: number | null;
  name: string;
  /** Decimal serialized as string. */
  cad_total: string;
}

/** The folded tail the legend prints as one `Other (n)` line. */
export interface FoldedTail {
  name: string;
  total_cad: string;
  items: CategorySlice[];
}

export interface FoldedDonut {
  /** Head slices, still one slice per category, in payload order. */
  slices: CategorySlice[];
  /** The exact folded remainder, or null when nothing folded. */
  other: FoldedTail | null;
}

/** Head size: the top N slices render individually (docs/47 G5). */
export const DONUT_KEEP = 6;

/**
 * Fold a spending donut's tail. Pure: sorts a copy, never mutates the payload
 * and never re-quantises a kept slice (the head's cents are the payload's).
 */
export function foldCategoryDonut(
  items: readonly CategorySlice[],
): FoldedDonut {
  const sorted = [...items].sort((a, b) =>
    dcmp(dec(b.cad_total), dec(a.cad_total)),
  );
  const head = sorted.slice(0, DONUT_KEEP);
  const tail = sorted.slice(DONUT_KEEP);
  // Uncategorized never folds, wherever it lands (G4/G5).
  const neverFolded = tail.filter((slice) => slice.id === null);
  const foldable = tail.filter((slice) => slice.id !== null);
  const other: FoldedTail | null =
    foldable.length === 0
      ? null
      : {
          name: `Other (${foldable.length})`,
          total_cad: dstr(
            quantizeCents(
              foldable.reduce(
                (acc, slice) => dadd(acc, dec(slice.cad_total)),
                dec("0"),
              ),
            ),
          ),
          items: foldable,
        };
  return { slices: [...head, ...neverFolded], other };
}

/** A signed dollar change of one category between two equal-length periods. */
export interface CategoryDelta {
  id: number | null;
  name: string;
  /** Decimal serialized as string, signed (current − prior), cents-quantised. */
  delta_cad: string;
  current_cad: string;
  prior_cad: string;
}

/** The G2 floor: absolute dollars, so the chip set is stable and testable. */
export const CATEGORY_DIFF_FLOOR_CAD = "50.00";
/** At most three chips (docs/47 G2). */
export const CATEGORY_DIFF_MAX = 3;

/**
 * Diff two equal-length periods of `GET /reports/spending/categories` and
 * return the top movers: |delta| ≥ the $50 floor, biggest first, at most 3.
 *
 * A category that VANISHED this period is a real −100% change and is diffed
 * too (prior value → 0); a category that APPEARED diffs against 0. Pure: the
 * clock, the range and the endpoint call are the CALLER's decisions (docs/47
 * G2 pins issuing the same endpoint twice).
 */
export function diffCategories(
  current: readonly CategorySlice[],
  prior: readonly CategorySlice[],
): CategoryDelta[] {
  const priorById = new Map(prior.map((slice) => [slice.id, slice]));
  const currentIds = new Set(current.map((slice) => slice.id));

  const deltas: CategoryDelta[] = current.map((slice) => {
    const before = priorById.get(slice.id);
    const delta = dsub(
      dec(slice.cad_total),
      before ? dec(before.cad_total) : dec("0"),
    );
    return {
      id: slice.id,
      name: slice.name,
      delta_cad: dstr(quantizeCents(delta)),
      current_cad: slice.cad_total,
      prior_cad: before?.cad_total ?? "0",
    };
  });
  for (const slice of prior) {
    if (currentIds.has(slice.id)) continue;
    deltas.push({
      id: slice.id,
      name: slice.name,
      delta_cad: dstr(quantizeCents(dneg(dec(slice.cad_total)))),
      current_cad: "0",
      prior_cad: slice.cad_total,
    });
  }

  const floor = dec(CATEGORY_DIFF_FLOOR_CAD);
  return deltas
    .filter((delta) => dcmp(dabs(dec(delta.delta_cad)), floor) >= 0)
    .sort((a, b) => dcmp(dabs(dec(b.delta_cad)), dabs(dec(a.delta_cad))))
    .slice(0, CATEGORY_DIFF_MAX);
}
