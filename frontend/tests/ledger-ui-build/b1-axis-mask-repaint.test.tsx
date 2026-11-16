// B1 (QA t_b29beeaa blocker) — the /investments account-value chart's money
// axis must LIVE-REPAINT on a display-mask flip, both directions, on ONE mount.
//
// Why this file exists: `AccountValueChartBody` already subscribes to the mask
// (`useMasked()`), so the component re-renders — but Recharts memoizes
// `YAxis` with `axisPropsAreEqual` and `chrome.moneyAxis` is the STABLE module
// function `formatAxisMoney`, so the memo saw identical props, bailed, and the
// axis kept painting mount-state dollars (masked) / bullets (unmasked). The
// Dashboard's NetWorthAreaChart escaped this by accident (its `data` array is
// rebuilt every render, which churns the chart store); the investments body
// memoizes its `points`, so nothing else invalidated the tick layer.
//
// The fix under test is the REACTIVE CONTRACT the task pins: Recharts receives
// a tickFormatter whose identity tracks the mask, so the stale axis layer is
// invalidated by a prop change — not by a formatter sniffing global state at
// call time, and not by a remount/reload/refetch.
//
// jsdom has no layout, so ResponsiveContainer measures 0x0 — the house stub
// (tests/m140ChartAxis.test.tsx) hands the chart a fixed box.
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccountValueChartBody } from "../../src/components/charts/AccountValueChart";
import type { ValueChartPayload } from "../../src/components/charts/AccountValueChart";
import { MASK_TEXT, initMaskForSession, setMasked } from "../../src/lib/mask";
import { ThemeProvider } from "../../src/theme/ThemeProvider";

vi.mock("recharts", async () => {
  const mod = await import("recharts");
  const ReactMod = await import("react");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return ReactMod.cloneElement(child as React.ReactElement, { width: 480, height: 280 });
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

/** Three monthly buckets with real dollars, so the y-axis draws real ticks. */
const PAYLOAD: ValueChartPayload = {
  granularity: "month",
  requested_granularity: "month",
  currency: "CAD",
  native_currency: "CAD",
  has_data: true,
  buckets: [
    {
      bucket_end: "2026-01-31",
      cash_cad: "4000.00",
      positions_cad: "8000.00",
      value_cad: "12000.00",
      estimated: false,
      valued: true,
      gap_reason: null,
    },
    {
      bucket_end: "2026-02-28",
      cash_cad: "4000.00",
      positions_cad: "10000.00",
      value_cad: "14000.00",
      estimated: false,
      valued: true,
      gap_reason: null,
    },
    {
      bucket_end: "2026-03-31",
      cash_cad: "4000.00",
      positions_cad: "12000.00",
      value_cad: "16000.00",
      estimated: false,
      valued: true,
      gap_reason: null,
    },
  ],
  events: [],
};

function mount() {
  return render(
    <ThemeProvider>
      <AccountValueChartBody history={PAYLOAD} title="Portfolio value over time" selectedEventId={null} />
    </ThemeProvider>,
  );
}

/** Money ticks the y-axis actually drew, read back out of the DOM. */
function moneyTicks(container: HTMLElement): string[] {
  return [
    ...container.querySelectorAll<SVGTextElement>(".recharts-yAxis-tick-labels text"),
  ].map((t) => t.textContent ?? "");
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
  vi.restoreAllMocks();
});

describe("B1 — the investments money axis repaints on a mask flip", () => {
  it("masked → unmasked → masked on ONE mount, same axis node, zero fetches", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { container } = mount();
    await waitFor(() => expect(moneyTicks(container).length).toBeGreaterThan(0));

    const ticksNode = container.querySelector(".recharts-yAxis-tick-labels");
    const dollars = moneyTicks(container);
    expect(dollars.some((t) => /^\$[\d,.]+K?$/.test(t))).toBe(true);

    // masked → unmasked is only a live concern if we start masked; flip both
    // ways so each direction is exercised from a known state.
    act(() => setMasked(true));
    await waitFor(() => expect(moneyTicks(container).every((t) => t === MASK_TEXT)).toBe(true));

    act(() => setMasked(false));
    await waitFor(() => expect(moneyTicks(container)).toEqual(dollars));

    act(() => setMasked(true));
    await waitFor(() => expect(moneyTicks(container).every((t) => t === MASK_TEXT)).toBe(true));

    // Same axis element every time — the ticks REDREW, the chart never remounted.
    expect(container.querySelector(".recharts-yAxis-tick-labels")).toBe(ticksNode);
    // A mask flip is a render-time transform: nothing may go over the wire.
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
