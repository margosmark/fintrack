// ToastProvider arms one AUTO_DISMISS_MS (4200) timer per plain toast and one
// commit timer per undo snackbar. A `setTimeout` outlives the React render that
// scheduled it, so the provider has to cancel its own on unmount — it did not,
// and that was a gate-killer with no failing test: a suite file that pushed a
// toast and finished <4.2 s later left the timer armed, the file's jsdom
// environment was torn down, the callback fired into a dead `window`, react-dom
// threw `ReferenceError: window is not defined` out of `getCurrentEventPriority`
// (react-dom.development.js), and vitest reported an unhandled error and
// EXITED 1 with every test passing (~1 full-suite run in 9 under load; caught
// by the t_cc2b952e date-lock sweep).
//
// Both cases are pinned here through the window timer seams, filtered by the
// two toast deadlines, so nothing else on the page (framer-motion, React's
// scheduler) can be mistaken for a toast timer. Fake timers are installed in
// `beforeEach` and wrapped afterwards, so the wrapper delegates to whatever
// clock is live and no test waits out a real 4.2 s / 5 s deadline.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { AUTO_DISMISS_MS, ToastProvider, UNDO_WINDOW_MS, useToast } from "../src/components/ui/Toast";

// jsdom has no matchMedia; framer-motion asks for it (reduced motion).
beforeAll(() => {
  if (window.matchMedia === undefined) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }),
    });
  }
});

const noOp = (): void => {};

/** Pushes one plain toast and one undo snackbar, i.e. both timer families. */
function Harness() {
  const toast = useToast();
  return (
    <div>
      <button type="button" onClick={() => toast.success("Batch committed")}>
        plain toast
      </button>
      <button
        type="button"
        onClick={() => toast.undo({ title: "Deleted transaction", onCommit: noOp, onUndo: noOp })}
      >
        undo toast
      </button>
    </div>
  );
}

// Under fake timers the handles are objects, not numbers (vitest's clock hands
// back a Timeout record), so the seams are typed loosely and round-trip
// whatever the clock produced — identity is all the assertions need.
let armed: unknown[] = [];
let cancelled = new Set<unknown>();

const isToastDeadline = (delay?: number): boolean =>
  delay === AUTO_DISMISS_MS || delay === UNDO_WINDOW_MS;

beforeEach(() => {
  vi.useFakeTimers();
  armed = [];
  cancelled = new Set();
  const liveSetTimeout = window.setTimeout.bind(window);
  const liveClearTimeout = window.clearTimeout.bind(window);
  window.setTimeout = ((handler: TimerHandler, delay?: number) => {
    const handle: unknown = liveSetTimeout(handler, delay);
    if (isToastDeadline(delay)) armed.push(handle);
    return handle as number;
  }) as typeof window.setTimeout;
  window.clearTimeout = ((handle?: number) => {
    if (handle !== undefined) cancelled.add(handle);
    liveClearTimeout(handle);
  }) as typeof window.clearTimeout;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ToastProvider timer ownership", () => {
  it("cancels every toast timer it armed when the provider unmounts", () => {
    const view = render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByText("plain toast"));
    fireEvent.click(screen.getByText("undo toast"));

    // Both families armed: the 4200 ms auto-dismiss and the 5 s commit window.
    expect(armed).toHaveLength(2);
    expect(cancelled.size).toBe(0);

    view.unmount();

    for (const handle of armed) {
      expect(cancelled).toContain(handle);
    }
  });

  it("keeps no spent timer in the unmount sweep (the tally stays bounded)", () => {
    const view = render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByText("plain toast"));
    const [handle] = armed;
    expect(handle).toBeDefined();

    act(() => {
      vi.advanceTimersByTime(AUTO_DISMISS_MS + 100);
    });

    view.unmount();

    // The timer already fired and removed its own entry, so the sweep had
    // nothing left to cancel — a list that only ever grows would clear it here.
    expect(cancelled.has(handle)).toBe(false);
  });
});
