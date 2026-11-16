// F-9 (P2, Ledger security re-verification t_f5b927bd) — the currency-exposure
// donut used to print RAW money on a masked session, on two surfaces:
//
//   * the chart's TEXT ALTERNATIVE: `… CAD 77%; total 211004.` — the shared
//     Donut fact is `total ${centerFormat(total)}`, and this donut's
//     `centerFormat` is its holdings-COUNT formatter, so the mixed-currency
//     slice sum rode into the aria-label as a bare integer. The aria-label IS
//     the content for assistive tech, so a screen-reader user got the figure
//     on every load, mask or not;
//   * the hover TOOLTIP: `47743.27 USD` — a hand-rolled
//     `` `${value.toFixed(2)} ${name}` `` formatter that never asked the mask.
//
// Both are money built INSIDE a formatter callback, which is exactly the class
// `scripts/sweep_money.mjs` cannot see (it tracks calls to the shared helpers;
// F-7 was the same shape) — hence this behavioural pin.
//
// The fix under test (docs/46 §D2): `ExposureDonut` routes both through
// `fmtNative` (the mask-aware native-currency helper the table beside it
// already uses) and subscribes with `useMasked()`, and the shared `Donut` grew
// a `totalFact` prop so a donut whose centre is a COUNT owns its money fact
// instead of inheriting a cross-currency sum.
//
// Every assertion here is OBSERVABLE: the strings assistive tech and a
// pointer actually receive, on one mount, flipping both directions.

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ExposureDonut } from "../src/components/charts/ExposureDonut";
import type { ExposureRow } from "../src/api/portfolio";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { MASK_TEXT, initMaskForSession, setMasked } from "../src/lib/mask";

// jsdom has no matchMedia (ThemeProvider reads prefers-color-scheme).
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
// the chart a fixed box, and force the sectors to their final state so they
// render as <path>s a pointer can actually hit (tests/donut.test.tsx shape).
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
    Pie: (props: React.ComponentProps<typeof mod.Pie>) =>
      React.createElement(mod.Pie, { ...props, isAnimationActive: false }),
  };
});

// The live-measured pair from the finding: CAD 163,260.73 and USD 47,743.27 —
// a cross-currency sum of 211,004, which is the number that leaked as `total`.
const ROWS: ExposureRow[] = [
  { listing_currency: "CAD", total_market_value_native: "163260.73", holdings_count: 4 },
  { listing_currency: "USD", total_market_value_native: "47743.27", holdings_count: 2 },
];

/** The money shape the finding asks us to prove is ABSENT while masked. */
const MONEY = /\d{1,3}(,\d{3})*\.\d{2}/;

function renderDonut() {
  return render(
    <ThemeProvider>
      <ExposureDonut rows={ROWS} />
    </ThemeProvider>,
  );
}

function ariaLabel(): string {
  return screen.getByRole("img", { name: /Exposure by listing currency/ }).getAttribute(
    "aria-label",
  ) ?? "";
}

/** Hover a slice and read back whatever GlassTooltip painted. */
function hoverFirstSlice(container: HTMLElement): HTMLElement {
  // No `instanceof SVGPathElement`: jsdom does not define that global.
  const slice = container.querySelector("svg path");
  if (!(slice instanceof Element)) throw new Error("no sector path rendered");
  // Wrapped: recharts answers a hover through its own async store
  // (EventSynchronizer / SetPiePayloadLegend), so the tooltip's state lands
  // outside React's batch.
  act(() => fireEvent.mouseEnter(slice));
  const tip = document.querySelector(".glass-strong");
  if (!(tip instanceof HTMLElement)) throw new Error("tooltip never activated on hover");
  return tip;
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

afterEach(() => {
  // Wrapped: vitest runs afterEach LIFO, so this fires BEFORE setup.ts's
  // RTL cleanup — a tree from the previous test is still mounted, and
  // `initMaskForSession()` notifies its mask subscribers.
  sessionStorage.clear();
  act(() => initMaskForSession());
});

describe("F-9 — the currency-exposure donut never prints raw amounts while masked", () => {
  it("CONTROL (unmasked): the same surfaces DO carry the real figures, so a masked pass is not vacuous", async () => {
    const { container } = renderDonut();

    // Text alternative: per-currency native amounts, no cross-currency sum.
    const label = ariaLabel();
    expect(label).toContain("163,260.73 CAD");
    expect(label).toContain("47,743.27 USD");
    // The share is a proportion — ruled VISIBLE by docs/46 §D2.
    expect(label).toContain("CAD 77%");
    // …and the dishonest mixed-currency "total 211004" is gone for good.
    expect(label).not.toMatch(/total 211004/);
    expect(label).not.toMatch(/total \d/);

    // Tooltip: the same native string the table beside it prints.
    expect(hoverFirstSlice(container).textContent).toContain("163,260.73 CAD");
  });

  it("masked: the aria-label carries no money at all — bullets, and the percent survives", async () => {
    act(() => setMasked(true));
    const { container } = renderDonut();

    const label = ariaLabel();
    expect(label).not.toMatch(MONEY);
    expect(label).toContain(MASK_TEXT);
    // D2's ruling: a percentage is proportional, so it stays readable.
    expect(label).toContain("CAD 77%");
    // One bullet run per currency — the shape (two amounts) survives, the
    // scale does not.
    expect(label.match(/•••••/g)).toHaveLength(2);

    // Nothing anywhere in the chart region prints a raw amount.
    const chart = screen.getByRole("img", { name: /Exposure by listing currency/ });
    expect(chart.textContent ?? "").not.toMatch(MONEY);
    expect(container.querySelector("svg")?.textContent ?? "").not.toMatch(MONEY);
    expect(hoverFirstSlice(container).textContent).not.toMatch(MONEY);
  });

  it("masked: the hover tooltip reads bullets, not 47743.27 USD", async () => {
    act(() => setMasked(true));
    const { container } = renderDonut();

    expect(hoverFirstSlice(container).textContent).toContain(MASK_TEXT);
    expect(document.body.textContent).not.toMatch(MONEY);
  });

  it("flips BOTH directions on ONE mount — same node, no remount (docs/46 §D3)", async () => {
    const { container } = renderDonut();
    const chart = screen.getByRole("img", { name: /Exposure by listing currency/ });
    expect(ariaLabel()).toContain("163,260.73 CAD");
    expect(hoverFirstSlice(container).textContent).toContain("163,260.73 CAD");

    act(() => setMasked(true));
    await waitFor(() => expect(ariaLabel()).not.toMatch(MONEY));
    // Same wrapper node: the label REPAINTED, the chart did not remount.
    expect(screen.getByRole("img", { name: /Exposure by listing currency/ })).toBe(chart);
    expect(ariaLabel()).toContain(MASK_TEXT);
    // The tooltip too — its formatter is a CLOSURE built in ExposureDonut, so
    // this half only flips because that component re-renders (D6). A remount
    // would have satisfied the aria-label assertion and failed here.
    expect(hoverFirstSlice(container).textContent).toContain(MASK_TEXT);

    act(() => setMasked(false));
    await waitFor(() => expect(ariaLabel()).toContain("163,260.73 CAD"));
    expect(screen.getByRole("img", { name: /Exposure by listing currency/ })).toBe(chart);
    expect(ariaLabel()).not.toMatch(MASK_TEXT);
  });

  it("the centre is a COUNT, so it never renders a currency figure in either state", () => {
    // Unmasked: 4 + 2 holdings through the count formatter — a bare integer,
    // no `$`, no decimals. (AnimatedNumber ships the exact figure as
    // visually-hidden text; the tween starts from 0 under jsdom.)
    const { unmount } = renderDonut();
    expect(document.querySelector(".sr-only")?.textContent).toBe("6");
    unmount();

    // Masked: AnimatedNumber masks BOTH spans (M139) — pre-existing, and the
    // fail-closed direction, so a count the mask hides is not this finding's
    // leak. What matters is that it is never a raw money string.
    act(() => setMasked(true));
    renderDonut();
    const centre = document.querySelector(".sr-only")?.textContent ?? "";
    expect(centre).not.toMatch(MONEY);
    expect(centre === MASK_TEXT || centre === "6").toBe(true);
  });
});
