// X-axis tick thinning (M126, docs/12 §Axis thinning).
//
// A 5Y range at day granularity would ask an 800px axis to label 1,800 dates.
// Recharts' `interval` prop is the knob: `interval={n}` renders every
// (n+1)-th tick, so one formula over (point count, viewport width) gives the
// same density at 390px and 1280px — and the same range renders FEWER ticks
// on a phone without any per-chart special case.
//
import { useEffect, useState } from "react";
import type { RefObject } from "react";

// The widths are viewports because that is what the responsive contract
// actually varies (docs/09 §Mobile): a chart lives in a card, and the card is
// a fraction of the viewport — so this is a conservative (denser-than-needed
// at worst) estimate, never an unreadable axis.

/** Roughly one tick label per this many pixels before it starts colliding. */
export const TICK_BUDGET_PX = 90;

/** Never render fewer than this many ticks — an axis with one label is a lie
 *  about having a scale at all. */
export const MIN_TICKS = 2;

/**
 * Recharts `interval` value: how many points to SKIP between labels.
 * `0` renders every point, `2` renders every 3rd, and so on.
 *
 * `points <= budget` → 0 (every point is labelable); otherwise the step that
 * brings the rendered tick count down to the pixel budget.
 */
export function axisTickInterval(points: number, widthPx: number): number {
  if (!Number.isFinite(points) || points <= 1) return 0;
  const budget = Math.max(MIN_TICKS, Math.floor(Math.max(0, widthPx) / TICK_BUDGET_PX));
  if (points <= budget) return 0;
  return Math.ceil(points / budget) - 1;
}

/**
 * Current viewport width, re-rendering on resize. `window` is undefined in
 * SSR/tests, and jsdom never resizes — the fallback keeps charts rendering
 * with a sane desktop budget instead of crashing.
 */
export function viewportWidth(fallback = 1280): number {
  if (typeof window === "undefined" || !Number.isFinite(window.innerWidth)) return fallback;
  return window.innerWidth || fallback;
}

/** Viewport width as component state — re-renders on resize so an axis thins
 *  when the window (or a device rotation) changes width. */
export function useViewportWidth(fallback = 1280): number {
  const [width, setWidth] = useState(() => viewportWidth(fallback));
  useEffect(() => {
    const onResize = () => setWidth(viewportWidth(fallback));
    globalThis.addEventListener("resize", onResize);
    return () => globalThis.removeEventListener("resize", onResize);
  }, [fallback]);
  return width;
}

/**
 * M138 D-7 — the chart's OWN width, not the viewport's.
 *
 * `useViewportWidth` is an honest description of the responsive contract but
 * the wrong number for tick spacing: on a 1440 viewport the net-worth card is
 * ~576px wide, so a budget derived from 1440 asked for 16 labels in 576px —
 * 36px apart for ~45px text, which is exactly the 5 axis-label pairs M133
 * counted overlapping at 1440 (and 6 more at the axis origin).
 *
 * Falls back to `useViewportWidth` when the box has no measurable width
 * (jsdom, `display:none`, before the first layout), so tests and off-screen
 * charts keep the number they had.
 */
export function useChartWidth<T extends HTMLElement>(
  ref: RefObject<T | null>,
  fallback = 1280,
): number {
  const [measured, setMeasured] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (element === null) return;
    const update = () => setMeasured(element.getBoundingClientRect().width);
    update();
    if (typeof globalThis.ResizeObserver === "undefined") {
      globalThis.addEventListener("resize", update);
      return () => globalThis.removeEventListener("resize", update);
    }
    const observer = new globalThis.ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  const viewport = useViewportWidth(fallback);
  return measured > 0 ? measured : viewport;
}
