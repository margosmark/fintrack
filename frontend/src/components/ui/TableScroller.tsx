// Horizontal table scroller (M64, docs/09 "Horizontal table scroll").
//
// The transactions tables carry 8 columns of real content (~1110px) inside a
// ~650px box at a 1280px viewport, and the columns that overflow are exactly
// the ones the page exists for (category combo-box, Edit/Delete, tags). A
// bare `overflow-x: auto` fails there twice:
//
//   * a native horizontal scrollbar is invisible in whole classes of
//     environments — overlay scrollbars and the audit's headless harness
//     (`--hide-scrollbars`) paint nothing, so the M61 audit saw "no scrollbar
//     renders" and the right-hand columns became a blind drag; and
//   * the scroll box itself was neither labelled nor focusable, so a keyboard
//     user had no way to scroll it (WCAG 2.1.1).
//
// This wrapper owns the whole affordance: a labelled + focusable `region`
// that scrolls, a custom 10px track/ thumb that mirrors `scrollLeft` (ordinary
// DOM, so it renders everywhere and is >=8px in every environment), and a
// "Scroll for actions ->" button that jumps to the end — or back. The native
// scrollbar is suppressed by `.table-scroll` (index.css) so there is exactly
// one scrollbar per box instead of two stacked ones.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode } from "react";

interface Metrics {
  clientWidth: number;
  scrollWidth: number;
  scrollLeft: number;
}

const ZERO: Metrics = { clientWidth: 0, scrollWidth: 0, scrollLeft: 0 };

/** A11y minimum thumb width (% of the track) so the thumb stays grabbable. */
const MIN_THUMB_PCT = 8;

function reducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export interface TableScrollerProps {
  /** Accessible name of the scrollable region (`role="region"`). */
  label: string;
  /** Layout classes for the card (page-owned: margins, `min-w-0`, …). */
  className?: string;
  children: ReactNode;
}

export function TableScroller({ label, className = "", children }: TableScrollerProps) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ startX: number; startScroll: number } | null>(null);
  const regionId = useId();
  const [metrics, setMetrics] = useState<Metrics>(ZERO);

  const measure = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    setMetrics({
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
      scrollLeft: el.scrollLeft,
    });
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    measure();
    // ResizeObserver catches content-driven width changes (more rows, longer
    // payees, a category that wraps). jsdom has none — tests drive `measure`
    // through the scroll listener instead.
    const observer =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => measure()) : null;
    observer?.observe(el);
    const child = el.firstElementChild;
    if (observer && child) observer.observe(child);
    el.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      el.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  const maxScroll = Math.max(0, metrics.scrollWidth - metrics.clientWidth);
  // Sub-pixel layout noise must not paint a scrollbar nobody can use.
  const overflowing = maxScroll > 0.5;
  const thumbPct = Math.max(
    MIN_THUMB_PCT,
    metrics.scrollWidth > 0 ? (metrics.clientWidth / metrics.scrollWidth) * 100 : 100,
  );
  const thumbLeftPct = maxScroll > 0 ? (metrics.scrollLeft / maxScroll) * (100 - thumbPct) : 0;
  const valueNow = maxScroll > 0 ? Math.round((metrics.scrollLeft / maxScroll) * 100) : 0;
  const atEnd = overflowing && maxScroll - metrics.scrollLeft < 2;

  const scrollTo = useCallback((left: number) => {
    const el = scrollerRef.current;
    if (!el) return;
    const target = Math.min(Math.max(0, left), Math.max(0, el.scrollWidth - el.clientWidth));
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ left: target, behavior: reducedMotion() ? "auto" : "smooth" });
    } else {
      // jsdom implements neither `scrollTo` nor scrolling itself — write the
      // position directly so tests (and any browser without `scrollTo`) still
      // see the scroll happen.
      el.scrollLeft = target;
    }
    measure();
  }, [measure]);

  /** Track pointer-down: grab the thumb, or jump + drag from the track. */
  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    const el = scrollerRef.current;
    const track = trackRef.current;
    if (!el || !track || maxScroll <= 0) return;
    const rect = track.getBoundingClientRect();
    const thumbWidth = (thumbPct / 100) * rect.width;
    const pointerX = event.clientX - rect.left;
    const thumbX = (thumbLeftPct / 100) * rect.width;
    if (pointerX < thumbX || pointerX > thumbX + thumbWidth) {
      // Jump-to-position (centered on the click) like a native track.
      const fraction = rect.width - thumbWidth > 0
        ? (pointerX - thumbWidth / 2) / (rect.width - thumbWidth)
        : 0;
      el.scrollLeft = Math.min(Math.max(0, fraction), 1) * maxScroll;
      measure();
    }
    dragRef.current = { startX: event.clientX, startScroll: el.scrollLeft };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    const el = scrollerRef.current;
    const track = trackRef.current;
    if (!drag || !el || !track) return;
    const rect = track.getBoundingClientRect();
    const usable = rect.width * (1 - thumbPct / 100);
    if (usable <= 0) return;
    const delta = ((event.clientX - drag.startX) / usable) * maxScroll;
    el.scrollLeft = drag.startScroll + delta;
    measure();
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    if (!dragRef.current) return;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  /** ARIA scrollbar keyboard support (WCAG 2.1.1 — arrows, page, home/end). */
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const el = scrollerRef.current;
    if (!el || maxScroll <= 0) return;
    const page = metrics.clientWidth * 0.9;
    const step = 48;
    const map: Record<string, number> = {
      ArrowLeft: el.scrollLeft - step,
      ArrowRight: el.scrollLeft + step,
      PageUp: el.scrollLeft - page,
      PageDown: el.scrollLeft + page,
      Home: 0,
      End: maxScroll,
    };
    const next = map[event.key];
    if (next === undefined) return;
    event.preventDefault();
    scrollTo(next);
  }

  return (
    <div className={`relative ${className}`}>
      <div className="glass overflow-hidden rounded-xl">
        <div
          ref={scrollerRef}
          id={regionId}
          role="region"
          aria-label={label}
          tabIndex={0}
          className="table-scroll overflow-x-auto"
        >
          {children}
        </div>
      </div>
      {overflowing && (
        // Sticky, not static: a 50-row table is thousands of pixels tall, so a
        // bar parked under the card would sit a couple of screenfuls below the
        // fold — exactly where the M61 audit never found it. Riding the
        // viewport bottom keeps the scrollbar + hint on screen for as long as
        // the table is, then it settles back under the card.
        //
        // Below `md` the fixed `nav.mobile-bottom-nav` owns the viewport
        // bottom (z-40, opaque): a bar parked at `bottom-4` rests INSIDE the
        // nav band, so the nav paints over it and `elementFromPoint` at the
        // hint's centre returns a nav link (M82 F5). Clear the nav instead —
        // 57px = its `h-14` + 1px `border-t` (56px content-box + border) plus
        // its `pb-[env(safe-area-inset-bottom)]`, then 1rem of air; `md:` is
        // exactly the `md:hidden` at which the nav disappears, so the desktop
        // offset stays `bottom-4`.
        <div className="sticky bottom-[calc(57px_+_env(safe-area-inset-bottom)_+_1rem)] z-30 mt-2 flex items-center gap-3 rounded-xl border border-control bg-surface/95 px-3 py-1.5 shadow-glow-sm backdrop-blur-sm md:bottom-4">
          <button
            type="button"
            onClick={() => (atEnd ? scrollTo(0) : scrollTo(maxScroll))}
            className="shrink-0 rounded px-1 py-0.5 text-[11px] uppercase tracking-widest text-accent transition-colors hover:text-text"
          >
            {atEnd ? "← Scroll back" : "Scroll for actions →"}
          </button>
          <div
            ref={trackRef}
            role="scrollbar"
            aria-orientation="horizontal"
            aria-controls={regionId}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={valueNow}
            aria-valuetext={`${valueNow}% scrolled`}
            aria-label={`${label} scrollbar`}
            tabIndex={0}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onKeyDown={onKeyDown}
            className="relative h-2.5 min-w-24 flex-1 cursor-pointer touch-none rounded-full bg-border"
          >
            <div
              className="absolute top-0 h-full rounded-full bg-muted transition-[left] duration-75"
              style={{ left: `${thumbLeftPct}%`, width: `${thumbPct}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
