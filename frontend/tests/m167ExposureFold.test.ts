// M167 — the Exposure card's shared "top N + Other" fold (docs/09
// §Exposure visuals).
//
// The fold is money math over a user's real list, so it is pinned as exact
// arithmetic, not as a shape: the head's cents plus Other's cents must equal
// the cents of every row (M130's anti-fabrication pin, applied to the slice
// list), `Unclassified` must survive the fold at any rank, and the session
// memory must round-trip.

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import type { BreakdownRow } from "../src/api/portfolio";
import {
  DIMENSION_PLURAL,
  FOLD_N_BY_DIMENSION,
  OTHER_KEY,
  UNCLASSIFIED_KEY,
  clearExpandedDimensions,
  foldRows,
  isOtherBucket,
  setExpandedDimension,
  useExpandedDimension,
} from "../src/lib/exposureFold";

const row = (key: string, weight_pct: number, market_value_cad: string): BreakdownRow => ({
  key,
  market_value_cad,
  weight_pct,
});

/** Mark's real shape: one big country, a fat middle, a long tail. */
const TWELVE = [
  row("United States", 44.7, "645646.42"),
  row("Canada", 20.49, "295961.03"),
  row("Japan", 7.53, "108762.3"),
  row("United Kingdom", 5.1, "73686.4"),
  row("Germany", 4.2, "60685.48"),
  row("France", 3.3, "47683.35"),
  row("Australia", 2.5, "36124.25"),
  row("Switzerland", 1.9, "27456.03"),
  row("India", 1.29, "18633.41"),
  row("Netherlands", 1.08, "15607.7"),
  row("Denmark", 0.9, "12999.9"),
  row("Spain", 0.71, "10261.47"),
];

const cents = (value: string) => Math.round(Number(value) * 100);

beforeEach(() => {
  clearExpandedDimensions();
});

describe("M167 — foldRows", () => {
  it("keeps the top 8 and folds the tail into an exact Other bucket", () => {
    const fold = foldRows(TWELVE, FOLD_N_BY_DIMENSION.geography);

    expect(fold.rows.filter((r) => !isOtherBucket(r))).toHaveLength(8);
    expect(fold.other).not.toBeNull();
    expect(fold.other?.key).toBe(OTHER_KEY);
    expect(fold.other?.count).toBe(4);
    expect(fold.hiddenCount).toBe(4);
  });

  it("never fabricates: head + Other is exactly the full list, in cents and in weight", () => {
    const fold = foldRows(TWELVE, FOLD_N_BY_DIMENSION.geography);
    const head = fold.rows.filter((r): r is BreakdownRow => !isOtherBucket(r));

    const headCents = head.reduce((sum, r) => sum + cents(r.market_value_cad), 0);
    const otherCents = fold.other ? cents(fold.other.market_value_cad) : 0;
    const allCents = TWELVE.reduce((sum, r) => sum + cents(r.market_value_cad), 0);
    expect(headCents + otherCents).toBe(allCents);

    const headWeight = head.reduce((sum, r) => sum + r.weight_pct, 0);
    expect(headWeight + (fold.other?.weight_pct ?? 0)).toBeCloseTo(
      TWELVE.reduce((sum, r) => sum + r.weight_pct, 0),
      9,
    );

    // And the expanded view is the same population, untouched.
    expect(fold.all).toHaveLength(TWELVE.length);
    expect(fold.all.reduce((sum, r) => sum + cents(r.market_value_cad), 0)).toBe(allCents);
  });

  it("sorts by weight descending whatever order the payload arrives in", () => {
    const shuffled = [...TWELVE].reverse();
    const fold = foldRows(shuffled, 3);
    const head = fold.rows.filter((r): r is BreakdownRow => !isOtherBucket(r));

    expect(head.map((r) => r.key)).toEqual(["United States", "Canada", "Japan"]);
    expect(fold.all.map((r) => r.key)).toEqual(TWELVE.map((r) => r.key));
  });

  it("folds nothing when the list fits above the line", () => {
    const fold = foldRows(TWELVE.slice(0, 8), FOLD_N_BY_DIMENSION.geography);

    expect(fold.other).toBeNull();
    expect(fold.rows).toHaveLength(8);
    expect(fold.hiddenCount).toBe(0);
  });

  it("never folds Unclassified into Other — the honesty bucket outranks the fold", () => {
    // Unclassified is the SMALLEST row here, so it is exactly the row a naive
    // fold would bury: 9 classified + Unclassified, folded at 8.
    const rows = [...TWELVE.slice(0, 9), row(UNCLASSIFIED_KEY, 0.4, "5780.12")];
    const fold = foldRows(rows, FOLD_N_BY_DIMENSION.geography);
    const keys = fold.rows.map((r) => r.key);

    expect(keys).toContain(UNCLASSIFIED_KEY);
    expect(keys.filter((k) => k === UNCLASSIFIED_KEY)).toHaveLength(1);
    // The one row left to fold is the 9th CLASSIFIED country, not Unclassified.
    expect(fold.other?.count).toBe(1);
    expect(keys.indexOf(OTHER_KEY)).toBe(keys.length - 1);
  });

  it("folds companies at 10 and every other dimension at 8", () => {
    expect(FOLD_N_BY_DIMENSION.company).toBe(10);
    expect(FOLD_N_BY_DIMENSION.geography).toBe(8);
    expect(FOLD_N_BY_DIMENSION.sector).toBe(8);
    expect(FOLD_N_BY_DIMENSION.asset_class).toBe(8);

    const fold = foldRows(TWELVE, FOLD_N_BY_DIMENSION.company);
    expect(fold.other?.count).toBe(2);
    expect(fold.rows.filter((r) => !isOtherBucket(r))).toHaveLength(10);
    expect(DIMENSION_PLURAL.geography).toBe("countries");
    expect(DIMENSION_PLURAL.company).toBe("companies");
  });

  it("sums the tail in CENTS, so 40 float weights cannot drift a dollar", () => {
    const tail = Array.from({ length: 40 }, (_, i) =>
      row(`Country ${i}`, 0.1, "1000.01"),
    );
    const fold = foldRows(tail, 8);
    expect(fold.other?.count).toBe(32);
    // 32 × 1000.01 = 32000.32 — a float reduce would print …31.9999999999996.
    expect(fold.other?.market_value_cad).toBe("32000.32");
  });
});

describe("M167 — per-session expansion memory (M139's pattern)", () => {
  it("remembers a flip for the session and clears it back to collapsed", () => {
    const { result } = renderHook(() => useExpandedDimension("geography"));

    expect(result.current).toBe(false);

    act(() => setExpandedDimension("geography", true));
    expect(result.current).toBe(true);
    expect(window.sessionStorage.getItem("fintrack.exposure-expanded.v1")).toBe(
      '["geography"]',
    );

    // Other dimensions keep their own state — one button does not expand all.
    const sector = renderHook(() => useExpandedDimension("sector"));
    expect(sector.result.current).toBe(false);

    act(() => clearExpandedDimensions());
    expect(result.current).toBe(false);
    expect(window.sessionStorage.getItem("fintrack.exposure-expanded.v1")).toBeNull();
  });

  it("survives a remount within the session", () => {
    renderHook(() => useExpandedDimension("company"));
    act(() => setExpandedDimension("company", true));

    // A fresh mount (tab re-entry, navigation) reads the session record.
    const second = renderHook(() => useExpandedDimension("company"));
    expect(second.result.current).toBe(true);

    act(() => clearExpandedDimensions());
  });
});
