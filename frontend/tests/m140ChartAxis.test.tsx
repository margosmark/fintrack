// M140 — chart AXIS TICKS flip with the mask, in place, without a remount
// (docs/46 §D3/D6).
//
// This is the case a per-component subscription has to get right and a
// parent-walk cannot: Recharts memoizes `XAxis`/`YAxis` with
// `axisPropsAreEqual`, and `chrome.moneyAxis` is the STABLE module function
// `formatAxisMoney`. So if the chart component does not re-render, the memo
// sees identical props, bails out, and the axis keeps drawing real dollars
// under a mask — a masked UI that still prints `$16K` on a gridline.
//
// The fix is the D6 rule (the chart subscribes itself), and this test pins the
// MECHANISM rather than the format: it reads the tick strings back out of the
// SVG the axis actually drew, before and after a flip, on ONE mount.
//
// jsdom has no layout, so ResponsiveContainer measures 0x0 — the house stub
// (tests/reportMoneyAxes.test.tsx) hands the chart a fixed box.
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NetWorthAreaChart } from "../src/components/charts/NetWorthAreaChart";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";
import { ThemeProvider } from "../src/theme/ThemeProvider";

vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, { width: 480, height: 320 });
      }
      return child;
    },
  };
});

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** Net worth climbing $1,000 → $1,200 → $1,400, so the axis draws real ticks. */
const BUCKETS = [
  { bucket_end: "2026-01-31", cash_cad: "400.00", positions_cad: "600.00", net_cad: "1000.00" },
  { bucket_end: "2026-02-28", cash_cad: "400.00", positions_cad: "800.00", net_cad: "1200.00" },
  { bucket_end: "2026-03-31", cash_cad: "400.00", positions_cad: "1000.00", net_cad: "1400.00" },
] as never;

function tree() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <NetWorthAreaChart buckets={BUCKETS} granularity="month" />
      </ThemeProvider>
    </QueryClientProvider>
  );
}

/** Money ticks the Y-axis actually drew, read back out of the DOM. */
function moneyTicks(container: HTMLElement): string[] {
  return [
    ...container.querySelectorAll<SVGTextElement>(".recharts-yAxis-tick-labels text"),
  ].map((t) => t.textContent ?? "");
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

describe("M140 — masked chart axes", () => {
  it("redraws every money tick as bullets on a flip, without a remount", async () => {
    const { container, rerender } = render(tree());
    await waitFor(() => expect(moneyTicks(container).length).toBeGreaterThan(0));

    const before = moneyTicks(container);
    // The fixture tops out at $1,400, so the axis draws grouped dollars.
    expect(before.some((t) => /^\$[\d,.]+$/.test(t))).toBe(true);
    expect(before.every((t) => t !== MASK_TEXT)).toBe(true);
    const ticksNode = container.querySelector(".recharts-yAxis-tick-labels");

    setMasked(true);
    rerender(tree());

    await waitFor(() => expect(moneyTicks(container).every((t) => t === MASK_TEXT)).toBe(true));
    // Same axis element — the ticks REDREW, the chart did not remount.
    expect(container.querySelector(".recharts-yAxis-tick-labels")).toBe(ticksNode);
    expect(moneyTicks(container).some((t) => /^\$[\d,.]+$/.test(t))).toBe(false);

    setMasked(false);
    rerender(tree());
    await waitFor(() => expect(moneyTicks(container)).toEqual(before));
  });

  it("keeps the shape of the chart: the x-axis DATE labels are never masked", async () => {
    const { container, rerender } = render(tree());
    await waitFor(() => expect(moneyTicks(container).length).toBeGreaterThan(0));

    const datesBefore = [
      ...container.querySelectorAll<SVGTextElement>(".recharts-xAxis-tick-labels text"),
    ].map((t) => t.textContent ?? "");
    expect(datesBefore.length).toBeGreaterThan(0);

    setMasked(true);
    rerender(tree());
    await waitFor(() => expect(moneyTicks(container).every((t) => t === MASK_TEXT)).toBe(true));

    // D2: the mask hides the SCALE, not the trend. A masked chart still says
    // when its points are — dates and percentages are ruled visible.
    const datesAfter = [
      ...container.querySelectorAll<SVGTextElement>(".recharts-xAxis-tick-labels text"),
    ].map((t) => t.textContent ?? "");
    expect(datesAfter).toEqual(datesBefore);
    expect(datesAfter.every((t) => t !== MASK_TEXT)).toBe(true);
  });
});
