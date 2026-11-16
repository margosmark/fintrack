// M126 — chart ranges: window math, the documented range → granularity
// formula, and axis tick thinning (docs/12 §Range → granularity, §Axis
// thinning).
//
// The formula is pinned on BOTH sides (`app/services/price_series.py` in
// `backend/tests/test_prices_history.py`) because a chart that buckets daily
// on the client and monthly on the server would disagree with its own axis.
// This file is the client half: same table, same boundary values.
import { describe, expect, it } from "vitest";

import {
  CHART_RANGES,
  MAX_SERIES_POINTS,
  granularityForDays,
  granularityForRange,
  granularityForWindow,
  rangeWindow,
} from "../src/lib/chartRange";
import { MIN_TICKS, axisTickInterval, viewportWidth } from "../src/components/charts/thinning";

const TODAY = new Date(2026, 9, 9); // 2026-10-09, local calendar

describe("rangeWindow (the `from` the charts send)", () => {
  it("1M/3M/6M/1Y/5Y land on the same day of the month, one/fewer years back", () => {
    expect(rangeWindow("1M", TODAY)).toEqual({
      from: "2026-09-09",
      to: "2026-10-09",
      days: 30,
    });
    expect(rangeWindow("3M", TODAY).from).toBe("2026-07-09");
    expect(rangeWindow("6M", TODAY).from).toBe("2026-04-09");
    expect(rangeWindow("1Y", TODAY).from).toBe("2025-10-09");
    expect(rangeWindow("5Y", TODAY).from).toBe("2021-10-09");
  });

  it("YTD is Jan 1 → today, measured against the real clock", () => {
    const ytd = rangeWindow("YTD", TODAY);
    expect(ytd.from).toBe("2026-01-01");
    expect(ytd.to).toBe("2026-10-09");
    expect(ytd.days).toBe(Math.round((TODAY.getTime() - Date.parse("2026-01-01T00:00:00Z")) / 86_400_000));

    // The bound is relative to TODAY, not hard-coded: on a January day YTD is
    // a few days long, not a negative span.
    const january = rangeWindow("YTD", new Date(2026, 0, 15));
    expect(january.from).toBe("2026-01-01");
    expect(january.to).toBe("2026-01-15");
    expect(january.days).toBe(14);
  });

  it("All has no floor and an unknown span — the server applies the formula", () => {
    expect(rangeWindow("All", TODAY)).toEqual({ from: null, to: "2026-10-09", days: null });
    expect(granularityForRange("All", TODAY)).toBe("auto");
  });

  it("offers exactly the seven documented presets, in order", () => {
    expect([...CHART_RANGES]).toEqual(["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"]);
  });

  it("never lands in the future, whatever day it is called on", () => {
    const today = new Date();
    for (const pick of CHART_RANGES) {
      const { from, to } = rangeWindow(pick, today);
      const now = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
      expect(to <= now).toBe(true);
      if (from !== null) expect(from <= to).toBe(true);
    }
  });
});

describe("granularityForDays — docs/12's table", () => {
  it("matches the published formula (finest fit under the 150-point budget)", () => {
    expect(granularityForDays(0)).toBe("day");
    expect(granularityForDays(31)).toBe("day"); // 1M
    expect(granularityForDays(92)).toBe("day"); // 3M
    expect(granularityForDays(150)).toBe("day"); // budget boundary
    expect(granularityForDays(151)).toBe("week");
    expect(granularityForDays(182)).toBe("week"); // 6M
    expect(granularityForDays(365)).toBe("week"); // 1Y
    expect(granularityForDays(1826)).toBe("month"); // 5Y
    expect(granularityForDays(3650)).toBe("month"); // 10Y "All"
    expect(granularityForDays(365 * 40)).toBe("year");
  });

  it("keeps every pick inside the documented budget", () => {
    const units: Record<string, number> = { day: 1, week: 7, month: 30.44, year: 365.25 };
    for (const days of [0, 7, 45, 200, 400, 900, 1826, 5000, 20000]) {
      const pick = granularityForDays(days);
      expect(Math.ceil(days / units[pick])).toBeLessThanOrEqual(MAX_SERIES_POINTS);
    }
  });

  it("granularityForWindow reads an explicit [from, to] pair", () => {
    // YTD on the day of this run — the formula, not a hard-coded month.
    expect(granularityForWindow("2026-01-01", "2026-10-09")).toBe("week");
    expect(granularityForWindow("2026-09-09", "2026-10-09")).toBe("day");
    // 10Y still fits the month budget (120 buckets); you need ~16 years of
    // span before the formula reaches `year`.
    expect(granularityForWindow("2016-10-09", "2026-10-09")).toBe("month");
    expect(granularityForWindow("1990-10-09", "2026-10-09")).toBe("year");
    // Garbage in → the smallest window, never a NaN-driven crash.
    expect(granularityForWindow("nope", "2026-10-09")).toBe("day");
  });
});

describe("axisTickInterval — thinned X axis at every responsive width", () => {
  const POINTS = 365;

  it("thins harder as the viewport narrows (390 / 412 / 768 / 1280)", () => {
    const widths = [390, 412, 768, 1280];
    const intervals = widths.map((w) => axisTickInterval(POINTS, w));
    expect(intervals).toEqual([91, 91, 45, 26]);
    // Never denser on a narrow screen than on a wide one.
    expect(intervals).toEqual([...intervals].sort((a, b) => b - a));
  });

  it("renders at least MIN_TICKS and no more than the pixel budget allows", () => {
    for (const w of [390, 412, 768, 1280]) {
      const interval = axisTickInterval(POINTS, w);
      const rendered = Math.floor(POINTS / (interval + 1)) + 1;
      expect(rendered).toBeGreaterThanOrEqual(MIN_TICKS);
      expect(rendered).toBeLessThanOrEqual(Math.max(MIN_TICKS, Math.floor(w / 90)));
    }
  });

  it("labels every point when they already fit, and never returns a negative step", () => {
    expect(axisTickInterval(4, 1280)).toBe(0);
    expect(axisTickInterval(1, 390)).toBe(0);
    expect(axisTickInterval(0, 390)).toBe(0);
    expect(axisTickInterval(Number.NaN, 390)).toBe(0);
    expect(axisTickInterval(10_000, 0)).toBeGreaterThanOrEqual(0);
  });

  it("viewportWidth reads the real window, falling back when it has none", () => {
    // jsdom reports 1024, so the fallback must NOT win here…
    expect(viewportWidth(777)).toBe(window.innerWidth || 777);
    // …and the value is always a usable budget, never 0/NaN.
    expect(viewportWidth()).toBeGreaterThan(0);
  });
});
