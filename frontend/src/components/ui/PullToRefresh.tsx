import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Reduced-motion flag for this surface.
 *
 * framer-motion's `useReducedMotion()` is also correct here, but it lazily
 * initialises ONE module-level listener per document (`motion-dom`) and then
 * never re-reads — this hook reads the media query on mount and subscribes to
 * it, so the flag is live for the component's whole lifetime (and testable).
 * The global `prefers-reduced-motion` rule in index.css remains the backstop:
 * it collapses every CSS transition, including the indicator's.
 */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    try {
      return (
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches
      );
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(media.matches);
    media.addEventListener?.("change", onChange);
    return () => media.removeEventListener?.("change", onChange);
  }, []);

  return reduced;
}

/**
 * Pull-to-refresh for the list/dashboard surfaces (M72, docs/09).
 *
 * Contract:
 *   - touch-only, and only from a page scrolled to the very top: a gesture
 *     that starts mid-list belongs to the list, and a horizontally dominant
 *     drag belongs to `TableScroller` (M64) — both stay untouched;
 *   - the pull is resisted (0.5x) and capped, so a hard flick never throws
 *     the page off-screen;
 *   - releasing past `PULL_THRESHOLD` runs `onRefresh` (in this app: a full
 *     React Query invalidation — data is always re-read from the server, the
 *     pull never fabricates local data);
 *   - `prefers-reduced-motion` is honoured two ways: no transition on the
 *     indicator (the global reduce rule in index.css already kills CSS
 *     animation) and no spinning glyph — the indicator is static text, which
 *     is exactly what the reduced-motion contract in docs/09 requires.
 */

export type PullPhase = "idle" | "pulling" | "ready" | "refreshing";

export interface PullState {
  /** Pixels the content is currently translated down by. */
  distance: number;
  phase: PullPhase;
}

/** Release distance that commits to a refresh (px of finger travel). */
export const PULL_THRESHOLD = 72;
/** Finger travel before the gesture is claimed at all — kills scroll jitter. */
const PULL_START = 8;
/** Hard cap on content translation. */
const PULL_MAX = 140;
/** Resistance applied to finger travel (rubber band). */
const PULL_RESISTANCE = 0.5;
/** How far the indicator is parked while the refresh itself runs. */
const REFRESH_HOLD = 28;

export function usePullToRefresh(
  onRefresh: () => Promise<void> | void,
  options?: { disabled?: boolean },
): PullState {
  const [state, setState] = useState<PullState>({ distance: 0, phase: "idle" });
  const disabled = options?.disabled ?? false;

  const start = useRef<{ x: number; y: number } | null>(null);
  const engaged = useRef(false);
  const distance = useRef(0);
  const running = useRef(false);
  const refreshFn = useRef(onRefresh);
  refreshFn.current = onRefresh;

  const settle = useCallback(() => {
    start.current = null;
    engaged.current = false;
    distance.current = 0;
    setState({ distance: 0, phase: "idle" });
  }, []);

  const runRefresh = useCallback(async () => {
    running.current = true;
    distance.current = REFRESH_HOLD;
    setState({ distance: REFRESH_HOLD, phase: "refreshing" });
    try {
      await refreshFn.current();
    } finally {
      running.current = false;
      settle();
    }
  }, [settle]);

  useEffect(() => {
    if (disabled) return;

    function onTouchStart(event: TouchEvent): void {
      if (running.current || event.touches.length !== 1) return;
      if (window.scrollY > 0) return;
      start.current = {
        x: event.touches[0].clientX,
        y: event.touches[0].clientY,
      };
      engaged.current = false;
      distance.current = 0;
    }

    function onTouchMove(event: TouchEvent): void {
      const origin = start.current;
      if (!origin || running.current || event.touches.length !== 1) return;
      const touch = event.touches[0];
      const dx = touch.clientX - origin.x;
      const dy = touch.clientY - origin.y;
      if (!engaged.current) {
        const vertical = dy > PULL_START && Math.abs(dy) > Math.abs(dx) * 1.5;
        // The page may have scrolled since touchstart — a pull never fights
        // an in-flight scroll.
        if (!vertical || window.scrollY > 0) return;
        engaged.current = true;
      }
      if (event.cancelable) event.preventDefault();
      const next = Math.min(PULL_MAX, dy * PULL_RESISTANCE);
      distance.current = next;
      setState({
        distance: next,
        phase: next >= PULL_THRESHOLD ? "ready" : "pulling",
      });
    }

    function onTouchEnd(): void {
      if (!start.current) return;
      const committed = engaged.current && distance.current >= PULL_THRESHOLD;
      if (committed) {
        start.current = null;
        engaged.current = false;
        void runRefresh();
      } else {
        settle();
      }
    }

    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("touchcancel", onTouchEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("touchcancel", onTouchEnd);
    };
  }, [disabled, runRefresh, settle]);

  return state;
}

/**
 * The floating indicator. Rendered only while a gesture or a refresh is in
 * flight, `role="status"` so assistive tech hears "Release to refresh" and
 * "Refreshing…" instead of only seeing them.
 */
export function PullToRefreshIndicator({
  distance,
  phase,
}: Pick<PullState, "distance" | "phase">): JSX.Element | null {
  const reduced = usePrefersReducedMotion();
  if (phase === "idle") return null;

  const label =
    phase === "refreshing"
      ? "Refreshing…"
      : phase === "ready"
        ? "Release to refresh"
        : "Pull to refresh";

  return (
    <div
      data-testid="pull-to-refresh"
      data-phase={phase}
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed left-1/2 top-[calc(env(safe-area-inset-top)_+_4.5rem)] z-[60] flex items-center gap-2 rounded-full border border-control bg-surface-raised/95 px-3 py-1.5 text-xs font-medium text-text shadow-glow-sm backdrop-blur-sm"
      style={{
        transform: `translate(-50%, ${Math.round(Math.min(distance, 56))}px)`,
        transition: reduced
          ? undefined
          : "transform 180ms cubic-bezier(0.22, 1, 0.36, 1)",
      }}
    >
      <span aria-hidden className={phase === "refreshing" && !reduced ? "m72-spin" : ""}>
        ↻
      </span>
      <span>{label}</span>
    </div>
  );
}
