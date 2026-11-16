import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Donut } from "../src/components/charts/Donut";
import type { DonutDatum } from "../src/components/charts/Donut";
import { ThemeProvider } from "../src/theme/ThemeProvider";

// jsdom lacks matchMedia; ThemeProvider/useTheme needs it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}

// Recharts' ResponsiveContainer measures the DOM, which jsdom cannot do; give
// the chart a fixed box so the Pie sectors actually render as <path>s.
vi.mock("recharts", async () => {
  const mod = await import("recharts");
  return {
    ...mod,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => {
      const child = Array.isArray(children) ? children[0] : children;
      if (child && typeof child === "object" && "type" in (child as object)) {
        return React.cloneElement(child as React.ReactElement, { width: 400, height: 320 });
      }
      return child;
    },
    // jsdom never advances recharts' rAF tween, so sectors stay at zero angle
    // and emit no <path>; force the final state so slices are assertable.
    Pie: (props: React.ComponentProps<typeof mod.Pie>) =>
      React.createElement(mod.Pie, { ...props, isAnimationActive: false }),
  };
});

const data: DonutDatum[] = [
  { name: "A", value: 10 },
  { name: "B", value: 20 },
  { name: "C", value: 30 },
];

function renderDonut(props: Partial<React.ComponentProps<typeof Donut>>) {
  return render(
    React.createElement(
      ThemeProvider,
      null,
      React.createElement(Donut, {
        data,
        centerValue: 60,
        centerLabel: "spent",
        ...props,
      }),
    ),
  );
}

describe("Donut", () => {
  it("renders the center caption and the exact center value (en-CA CAD)", () => {
    const { container } = renderDonut();
    expect(screen.getByText("spent")).toBeTruthy();
    // AnimatedNumber ships the exact figure as visually-hidden text so it
    // reads regardless of the in-flight tween (which starts from 0 under
    // jsdom). aria-label is PROHIBITED on a generic <span> (axe
    // `aria-prohibited-attr`, docs/09 "Naming & semantics contract"), so the
    // tween stays aria-hidden and the exact value rides alongside it.
    expect(container.querySelector(".sr-only")?.textContent).toBe("$60.00");
    expect(container.querySelector(".tabular > [aria-hidden]")).not.toBeNull();
  });

  it("renders one slice path per datum inside the chart svg", () => {
    const { container } = renderDonut();
    expect(container.querySelector("svg")).not.toBeNull();
    const paths = container.querySelectorAll("path");
    expect(paths.length).toBeGreaterThanOrEqual(data.length);
  });

  it("honors a custom center formatter", () => {
    const { container } = renderDonut({
      centerValue: 7,
      centerLabel: "holdings",
      centerFormat: (n) => String(Math.round(n)),
    });
    expect(screen.getByText("holdings")).toBeTruthy();
    expect(container.querySelector(".sr-only")?.textContent).toBe("7");
  });
});
