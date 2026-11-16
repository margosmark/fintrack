// M138 (6086a58) — `tickMargin={8}` on the net-worth X-axis: the label row
// clears the bottom-most Y tick (docs/46 §D7).
//
// The bug was sub-pixel geometry: `-$3,500` and the first X label printed into
// each other by a pixel at the axis origin. Recharts anchors tick text to the
// AXIS LINE, so the axis' `height` prop alone does not move it — `tickMargin`
// is the only prop that does (measured, in the commit). jsdom has no layout,
// so the rendered overlap itself can only be proven by the live probe
// (m138_dashboard_probe.py, geometry JSON); what THIS pins is the prop contract
// the geometry depends on: the chart must pass tickMargin={8} to its XAxis.
// Pre-fix the prop is absent → red.
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NetWorthAreaChart } from "../src/components/charts/NetWorthAreaChart";
import { ThemeProvider } from "../src/theme/ThemeProvider";

// jsdom has no matchMedia; the theme/mask hooks call it.
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

// Capture the props each <XAxis> actually received, then render the REAL
// axis (same capture pattern as reportMoneyAxes.test.tsx's YAxis).
const captured = vi.hoisted(() => ({
  xAxes: [] as Array<{ tickMargin?: number; height?: number }>,
}));

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
    XAxis: (props: React.ComponentProps<typeof mod.XAxis>) => {
      captured.xAxes.push(props as { tickMargin?: number; height?: number });
      return ReactMod.createElement(mod.XAxis, props);
    },
  };
});

const BUCKETS = [
  { bucket_end: "2026-01-31", cash_cad: "400.00", positions_cad: "600.00", net_cad: "-1000.00" },
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

beforeEach(() => {
  captured.xAxes.length = 0;
});

describe("M138 — the net-worth axis label row", () => {
  it("passes tickMargin={8} to the XAxis (the prop that clears the label row)", () => {
    render(tree());
    expect(captured.xAxes.length).toBeGreaterThan(0);
    // The axis origin overlap this fixes: Recharts anchors tick text to the
    // axis line, so height alone never moved it — tickMargin is the contract.
    expect(captured.xAxes.some((axis) => axis.tickMargin === 8)).toBe(true);
  });
});
