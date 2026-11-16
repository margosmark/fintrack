// M72 — pull-to-refresh behaviour (docs/09 §Pull-to-refresh).
//
// The gesture is touch-only, top-of-page only and vertically dominant, so it
// can never steal a scroll from a list or a horizontal drag from TableScroller
// (M64). Releasing past the threshold runs the refresh callback — which in the
// app is a full React Query invalidation, i.e. data always comes back from the
// server — and `prefers-reduced-motion` swaps the animated indicator for a
// static one (no transition, no spinning glyph).
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  PULL_THRESHOLD,
  PullToRefreshIndicator,
  usePullToRefresh,
} from "../src/components/ui/PullToRefresh";

/** jsdom has no matchMedia; framer's useReducedMotion calls it. */
let reduceMotion = false;
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /prefers-reduced-motion/.test(query) ? reduceMotion : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

function touch(type: string, x: number, y: number): void {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "touches", {
    value: type === "touchend" ? [] : [{ clientX: x, clientY: y }],
  });
  window.dispatchEvent(event);
}

function Harness({ onRefresh, disabled }: { onRefresh: () => void; disabled?: boolean }) {
  const { distance, phase } = usePullToRefresh(onRefresh, { disabled });
  return (
    <div>
      <PullToRefreshIndicator distance={distance} phase={phase} />
      <output data-testid="distance">{distance}</output>
      <output data-testid="phase">{phase}</output>
    </div>
  );
}

const phase = () => (screen.getByTestId("phase") as HTMLOutputElement).value;
const distance = () => Number((screen.getByTestId("distance") as HTMLOutputElement).value);

function pullTo(y: number): void {
  act(() => {
    touch("touchstart", 100, 100);
    touch("touchmove", 100, y);
  });
}

beforeEach(() => {
  reduceMotion = false;
  Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
});

describe("usePullToRefresh", () => {
  it("reports pull progress and flips to ready past the threshold", () => {
    render(<Harness onRefresh={vi.fn()} />);
    expect(phase()).toBe("idle");

    pullTo(120); // 20px of travel -> 10px of resisted distance
    expect(phase()).toBe("pulling");
    expect(distance()).toBeGreaterThan(0);
    expect(distance()).toBeLessThan(PULL_THRESHOLD);

    pullTo(400); // 300px -> 150px, capped
    expect(phase()).toBe("ready");
    expect(distance()).toBeLessThanOrEqual(140);
  });

  it("refreshes on release past the threshold, then settles", async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    render(<Harness onRefresh={onRefresh} />);

    pullTo(400);
    act(() => {
      touch("touchend", 100, 400);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(phase()).toBe("refreshing");

    await waitFor(() => expect(phase()).toBe("idle"));
    expect(distance()).toBe(0);
  });

  it("does not refresh when released short of the threshold", () => {
    const onRefresh = vi.fn();
    render(<Harness onRefresh={onRefresh} />);
    pullTo(120);
    act(() => {
      touch("touchend", 100, 120);
    });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(phase()).toBe("idle");
    expect(distance()).toBe(0);
  });

  it("ignores horizontally dominant drags (TableScroller territory)", () => {
    const onRefresh = vi.fn();
    render(<Harness onRefresh={onRefresh} />);
    act(() => {
      touch("touchstart", 100, 100);
      touch("touchmove", 300, 110); // dx 200, dy 10
      touch("touchmove", 500, 130);
      touch("touchend", 500, 130);
    });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(phase()).toBe("idle");
    expect(distance()).toBe(0);
  });

  it("never engages when the page is not scrolled to the top", () => {
    const onRefresh = vi.fn();
    Object.defineProperty(window, "scrollY", { configurable: true, value: 240 });
    render(<Harness onRefresh={onRefresh} />);
    pullTo(500);
    act(() => {
      touch("touchend", 100, 500);
    });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(distance()).toBe(0);
  });

  it("is inert while disabled (the drawer is open)", () => {
    const onRefresh = vi.fn();
    render(<Harness onRefresh={onRefresh} disabled />);
    pullTo(500);
    act(() => {
      touch("touchend", 100, 500);
    });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(phase()).toBe("idle");
  });
});

describe("PullToRefreshIndicator", () => {
  it("announces the state and animates only when motion is allowed", () => {
    reduceMotion = false;
    const { rerender } = render(
      <PullToRefreshIndicator distance={40} phase="pulling" />,
    );
    const indicator = screen.getByTestId("pull-to-refresh");
    expect(indicator.getAttribute("role")).toBe("status");
    expect(indicator.textContent).toContain("Pull to refresh");
    expect(indicator.style.transition).not.toBe("");

    rerender(<PullToRefreshIndicator distance={40} phase="ready" />);
    expect(screen.getByTestId("pull-to-refresh").textContent).toContain(
      "Release to refresh",
    );

    rerender(<PullToRefreshIndicator distance={28} phase="refreshing" />);
    expect(screen.getByTestId("pull-to-refresh").querySelector(".m72-spin")).not.toBeNull();
  });

  it("renders static text with no transition under prefers-reduced-motion", () => {
    reduceMotion = true;
    render(<PullToRefreshIndicator distance={28} phase="refreshing" />);
    const indicator = screen.getByTestId("pull-to-refresh");
    expect(indicator.textContent).toContain("Refreshing…");
    expect(indicator.style.transition).toBe("");
    expect(indicator.querySelector(".m72-spin")).toBeNull();
    reduceMotion = false;
  });

  it("renders nothing while idle", () => {
    render(<PullToRefreshIndicator distance={0} phase="idle" />);
    expect(screen.queryByTestId("pull-to-refresh")).toBeNull();
  });
});
