// M108 P1 — the treemap's layout and its honest percentage display.
//
// Two independent contracts live here:
//   1. `squarify` partitions the canvas: one rect per positive value, every
//      rect inside the box, each area proportional to its value (that is the
//      whole claim a treemap makes — a block twice as big means twice the
//      spend), and degenerate input returns [] rather than NaN geometry.
//   2. `sharePct` + `trimPct` produce the M105 display rule for a share: an
//      8-decimal wire-shaped value, TRIMMED (never re-rounded) to 2 for the
//      eye, with the raw digits still reachable for the aria-label.

import { describe, expect, it } from "vitest";

import { sharePct, trimPct } from "../src/lib/percent";
import { squarify } from "../src/lib/treemap";

const W = 1000;
const H = 320;

function area(rect: { w: number; h: number }): number {
  return rect.w * rect.h;
}

describe("squarify (docs/12 §Chart mapping — M108 P1)", () => {
  it("returns one rect per positive value, indexed back to the input", () => {
    const values = [50, 30, 20, 0, -5];
    const rects = squarify(values, W, H);
    // The zero and the negative are dropped: a zero-area block has no
    // position to honestly occupy (bucket zero-fill, docs/12).
    expect(rects).toHaveLength(3);
    expect(new Set(rects.map((r) => r.index))).toEqual(new Set([0, 1, 2]));
    expect(rects.map((r) => r.index)).not.toContain(3);
  });

  it("tiles the canvas: areas sum to width × height and every rect is inside it", () => {
    const values = [420, 180, 140, 90, 60, 40, 30, 25, 15];
    const rects = squarify(values, W, H);
    const totalArea = rects.reduce((sum, rect) => sum + area(rect), 0);
    expect(totalArea).toBeCloseTo(W * H, 4);

    for (const rect of rects) {
      expect(rect.x).toBeGreaterThanOrEqual(-1e-9);
      expect(rect.y).toBeGreaterThanOrEqual(-1e-9);
      expect(rect.x + rect.w).toBeLessThanOrEqual(W + 1e-6);
      expect(rect.y + rect.h).toBeLessThanOrEqual(H + 1e-6);
      expect(rect.w).toBeGreaterThan(0);
      expect(rect.h).toBeGreaterThan(0);
    }
  });

  it("keeps every block's area proportional to its value (the treemap claim)", () => {
    const values = [640, 160, 100, 60, 40];
    const total = values.reduce((sum, value) => sum + value, 0);
    const rects = squarify(values, W, H);
    const canvas = W * H;
    for (const rect of rects) {
      const expected = (values[rect.index] / total) * canvas;
      expect(area(rect)).toBeCloseTo(expected, 3);
    }
  });

  it("does not produce slivers — the worst block keeps a usable aspect ratio", () => {
    // A skewed distribution is where a naive slice-and-dice degenerates into
    // 1×N strips; squarify's whole point is that it does not.
    const rects = squarify([900, 40, 30, 20, 10], W, H);
    for (const rect of rects) {
      const ratio = Math.max(rect.w / rect.h, rect.h / rect.w);
      expect(ratio).toBeLessThan(12);
    }
  });

  it("is a pure function of its inputs (no module state between calls)", () => {
    const first = squarify([10, 20, 30], W, H);
    const second = squarify([10, 20, 30], W, H);
    expect(second).toEqual(first);
  });

  it("degrades to an empty layout instead of NaN geometry", () => {
    expect(squarify([], W, H)).toEqual([]);
    expect(squarify([0, 0, 0], W, H)).toEqual([]);
    expect(squarify([10, 20], 0, H)).toEqual([]);
    expect(squarify([10, 20], W, -1)).toEqual([]);
    expect(squarify([Number.NaN, 10], W, H).every((r) => Number.isFinite(r.x))).toBe(true);
  });
});

describe("sharePct + trimPct — M105's honest share display", () => {
  it("emits the wire shape and trims it to 2 decimals without re-rounding", () => {
    expect(sharePct(1, 3)).toBe("33.33333333");
    expect(trimPct(sharePct(1, 3) as string)).toBe("33.33");

    // 1/8 = 12.5% exactly: the trim must not turn it into "12.50" via a
    // different rounding path, it must SLICE the same digits.
    expect(trimPct(sharePct(1, 8) as string)).toBe("12.50");

    // 2/3 = 66.6666...% — toFixed(8) then slice gives 66.66, the digits the
    // eye loses are exactly the ones the aria-label still carries.
    const raw = sharePct(2, 3) as string;
    expect(raw).toBe("66.66666667");
    expect(trimPct(raw)).toBe("66.66");
  });

  it("a share of a zero total is undefined, not a confident 0.00%", () => {
    // M65 honesty: there is no denominator, so there is no share to show.
    expect(sharePct(0, 0)).toBeNull();
    expect(sharePct(10, 0)).toBeNull();
  });

  it("keeps the whole share when it is the whole (100%) — trim is lossless there", () => {
    expect(trimPct(sharePct(50, 50) as string)).toBe("100.00");
  });

  it("fails open on a shape that is not the wire value", () => {
    expect(trimPct("n/a")).toBe("n/a");
    expect(trimPct("12.3")).toBe("12.3");
  });
});
