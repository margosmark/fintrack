import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { isReadOnlySession } from "../../auth/readOnlySession";
import type { User } from "../../types";
import {
  TOUR_START_EVENT,
  markTourStartedThisSession,
  readTourState,
  tourStartedThisSession,
  writeTourState,
} from "./tourState";

/**
 * M89 G1 — the first-session coach marks.
 *
 * Shape of the contract (docs/09 §Help "Guided first tour"):
 *  - fires once per account on the demo tenant's FIRST dashboard visit;
 *  - four marks over the real nav targets (`data-tour` hooks in AppShell):
 *    the nav list, Imports, Reports, Help;
 *  - never modal: the veils are `pointer-events-none`, so the whole app stays
 *    clickable — the marks highlight, they do not seize the page;
 *  - keyboard: focus lands on the primary control of each mark, Tab cycles
 *    inside the card (one press of Skip or Escape always gets out);
 *  - motion: geometry transitions only, so the global
 *    `prefers-reduced-motion` rule in index.css collapses them to instant and
 *    the static highlight is what remains;
 *  - a target that is not fully on screen (rail collapsed, phone with the
 *    drawer closed, very short viewport) drops the spotlight and shows the
 *    card on its own — the words still teach, and nothing is ever pointed at
 *    a hidden element.
 */

interface TourMark {
  key: string;
  /** Primary selector inside AppShell. */
  target: string;
  /** Used when `target` is absent/hidden — e.g. the phone drawer toggle. */
  fallback?: string;
  title: string;
  body: React.ReactNode;
}

export const TOUR_MARKS: TourMark[] = [
  {
    key: "nav",
    target: '[data-tour="nav"]',
    fallback: '[data-tour="nav-toggle"]',
    title: "Everything lives one tap away",
    body: (
      <>
        Dashboard, transactions, imports, investments and reports sit in the
        top bar; everything else is under <strong>More</strong> — and on a
        phone it is the <strong>menu</strong> at the bottom right.
      </>
    ),
  },
  {
    key: "imports",
    // M185: the row moved into the top bar (desktop) / drawer (phone), so the
    // always-present tab strip is the fallback anchor.
    target: '[data-tour="imports"]',
    fallback: '[data-tour="nav"]',
    title: "How your data gets in",
    body: (
      <>
        Upload a statement and it lands in a <strong>staging batch</strong> you
        review first — nothing is written until you commit; the demo itself is
        read-only, so Help&rsquo;s <strong>first-import walkthrough</strong>{" "}
        shows the whole flow with screenshots.
      </>
    ),
  },
  {
    key: "reports",
    target: '[data-tour="reports"]',
    fallback: '[data-tour="nav"]',
    title: "How you get it back out",
    body: (
      <>
        Spending, net worth, cash flow and taxes are all built from the
        transactions below them, so the charts and the feed
        <strong> always agree</strong>.
      </>
    ),
  },
  {
    key: "help",
    // M185: Help lives in the More panel / drawer, both opened by a control
    // that is on screen at every breakpoint.
    target: '[data-tour="help"]',
    fallback: '[data-tour="more"]',
    title: "Start here when you are ready",
    body: (
      <>
        Help opens with <strong>three quickstart tracks</strong> — try it in two
        minutes, own your data, or drive it from your agent — plus a guide for
        every feature.
      </>
    ),
  },
];

const PAD = 6;
const CARD_WIDTH = "min(340px, calc(100vw - 24px))";

interface Spotlight {
  left: number;
  top: number;
  width: number;
  height: number;
}

function measure(selector: string): Spotlight | null {
  const el = document.querySelector<HTMLElement>(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const visible =
    r.width >= 8 &&
    r.height >= 8 &&
    r.left >= 0 &&
    r.top >= 0 &&
    r.right <= window.innerWidth &&
    r.bottom <= window.innerHeight;
  return visible
    ? { left: r.left, top: r.top, width: r.width, height: r.height }
    : null;
}

function veilGeom(spot: Spotlight) {
  const { left, top, width, height } = spot;
  const right = left + width;
  const bottom = top + height;
  return {
    top: { left: 0, top: 0, width: "100%", height: Math.max(0, top - PAD) },
    bottom: {
      left: 0,
      top: bottom + PAD,
      width: "100%",
      height: Math.max(0, window.innerHeight - bottom - PAD),
    },
    leftSide: {
      left: 0,
      top: Math.max(0, top - PAD),
      width: Math.max(0, left - PAD),
      height: height + PAD * 2,
    },
    rightSide: {
      left: right + PAD,
      top: Math.max(0, top - PAD),
      width: Math.max(0, window.innerWidth - right - PAD),
      height: height + PAD * 2,
    },
  };
}

export function Tour({ user }: { user: User | null }) {
  const location = useLocation();
  const [step, setStep] = useState<number | null>(null);
  const [spot, setSpot] = useState<Spotlight | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  const start = useCallback(() => {
    const active = document.activeElement;
    restoreRef.current = active instanceof HTMLElement ? active : null;
    setStep(0);
  }, []);

  const finish = useCallback(
    (outcome: "completed" | "skipped") => {
      writeTourState(user, outcome);
      setStep(null);
      setSpot(null);
      const restore = restoreRef.current;
      restoreRef.current = null;
      if (restore && document.contains(restore)) restore.focus();
    },
    [user],
  );

  // Auto-start: demo tenant, first dashboard visit, once per account, once per
  // tab-session. Every condition is re-checked on each render of the shell.
  useEffect(() => {
    if (step !== null) return;
    if (!isReadOnlySession(user)) return;
    if (location.pathname !== "/dashboard") return;
    if (readTourState(user) !== null) return;
    if (tourStartedThisSession()) return;
    markTourStartedThisSession();
    start();
  }, [user, location.pathname, step, start]);

  // Resurface path — Help's "Take the tour" button ignores the stored outcome.
  useEffect(() => {
    const onRequest = () => start();
    window.addEventListener(TOUR_START_EVENT, onRequest);
    return () => window.removeEventListener(TOUR_START_EVENT, onRequest);
  }, [start]);

  // Spotlight geometry for the current mark; re-measured on scroll/resize so
  // the ring cannot drift off the element it claims to highlight.
  useLayoutEffect(() => {
    if (step === null) {
      setSpot(null);
      return;
    }
    const mark = TOUR_MARKS[step];
    const selectors = [mark.target, ...(mark.fallback ? [mark.fallback] : [])];
    const apply = () => {
      let next: Spotlight | null = null;
      for (const selector of selectors) {
        next = measure(selector);
        if (next) break;
      }
      setSpot(next);
    };
    apply();
    window.addEventListener("scroll", apply, true);
    window.addEventListener("resize", apply);
    return () => {
      window.removeEventListener("scroll", apply, true);
      window.removeEventListener("resize", apply);
    };
  }, [step]);

  // Escape dismisses from anywhere (WCAG 2.1.1 — one press out).
  useEffect(() => {
    if (step === null) return;
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") finish("skipped");
    };
    window.addEventListener("keydown", onKeydown);
    return () => window.removeEventListener("keydown", onKeydown);
  }, [step, finish]);

  // Focus the mark's primary control whenever the step changes.
  useEffect(() => {
    if (step === null) return;
    const id = window.setTimeout(() => primaryRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [step]);

  if (step === null) return null;
  const mark = TOUR_MARKS[step];
  const last = step === TOUR_MARKS.length - 1;
  const veils = spot ? veilGeom(spot) : null;
  const cardLeft = spot
    ? Math.min(Math.max(spot.left, 12), window.innerWidth - 352)
    : 12;
  // Park the card under the highlighted region when there is room for it,
  // otherwise above it (anchored by its bottom edge, so its own height does
  // not matter). No spotlight → docked to the bottom-left of the viewport.
  const cardStyle: React.CSSProperties = spot
    ? spot.top + spot.height + 12 + 190 < window.innerHeight
      ? { top: spot.top + spot.height + 12, left: cardLeft }
      : { top: spot.top - 12, left: cardLeft, transform: "translateY(-100%)" }
    : { left: 12, bottom: 12 };

  const trapTab = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusables = Array.from(
      cardRef.current?.querySelectorAll<HTMLElement>("button") ?? [],
    );
    if (focusables.length === 0) return;
    const first = focusables[0];
    const lastEl = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === null)) {
      event.preventDefault();
      lastEl.focus();
    } else if (!event.shiftKey && active === lastEl) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      {spot && veils && (
        <>
          <div
            aria-hidden
            className="pointer-events-none fixed z-[60] transition-all duration-200"
            style={{
              background: "var(--scrim)",
              left: veils.top.left,
              top: veils.top.top,
              width: veils.top.width,
              height: veils.top.height,
            }}
          />
          <div
            aria-hidden
            className="pointer-events-none fixed z-[60] transition-all duration-200"
            style={{
              background: "var(--scrim)",
              left: veils.bottom.left,
              top: veils.bottom.top,
              width: veils.bottom.width,
              height: veils.bottom.height,
            }}
          />
          <div
            aria-hidden
            className="pointer-events-none fixed z-[60] transition-all duration-200"
            style={{
              background: "var(--scrim)",
              left: veils.leftSide.left,
              top: veils.leftSide.top,
              width: veils.leftSide.width,
              height: veils.leftSide.height,
            }}
          />
          <div
            aria-hidden
            className="pointer-events-none fixed z-[60] transition-all duration-200"
            style={{
              background: "var(--scrim)",
              left: veils.rightSide.left,
              top: veils.rightSide.top,
              width: veils.rightSide.width,
              height: veils.rightSide.height,
            }}
          />
          <div
            aria-hidden
            data-testid="tour-spotlight"
            className="pointer-events-none fixed z-[61] rounded-xl border-2 border-accent transition-all duration-200"
            style={{
              left: spot.left - PAD,
              top: spot.top - PAD,
              width: spot.width + PAD * 2,
              height: spot.height + PAD * 2,
              boxShadow: "0 0 24px -4px rgb(34 211 238 / 0.55)",
            }}
          />
        </>
      )}

      <div
        ref={cardRef}
        role="dialog"
        aria-labelledby="tour-mark-title"
        aria-describedby="tour-mark-body"
        data-testid="tour"
        onKeyDown={trapTab}
        className="fixed z-[62] rounded-xl border border-control bg-surface-raised p-4 shadow-glow"
        style={{ ...cardStyle, width: CARD_WIDTH }}
      >
        <p className="text-xs font-semibold uppercase tracking-widest text-muted">
          Tour &middot; {step + 1} of {TOUR_MARKS.length}
        </p>
        <h2
          id="tour-mark-title"
          className="mt-1 text-base font-semibold text-text"
        >
          {mark.title}
        </h2>
        <p id="tour-mark-body" className="mt-1.5 text-sm text-muted">
          {mark.body}
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            type="button"
            data-testid="tour-skip"
            onClick={() => finish("skipped")}
            className="min-h-11 rounded-lg border border-control px-4 text-sm font-medium text-text transition-colors hover:bg-veil/10"
          >
            Skip
          </button>
          <button
            ref={primaryRef}
            type="button"
            data-testid="tour-next"
            onClick={() => {
              if (last) finish("completed");
              else setStep(step + 1);
            }}
            className="min-h-11 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink transition-opacity hover:opacity-90"
          >
            {last ? "Done" : "Next"}
          </button>
        </div>
      </div>
    </>
  );
}
