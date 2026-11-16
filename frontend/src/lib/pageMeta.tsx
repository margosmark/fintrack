// PageMeta (M185, docs/47 §1.3 + G18) — how a page tells the shell's TruthBar
// what it is actually showing, so the bar can print a segment ONLY when a real
// payload field backs it.
//
// The truth bar is the direction's trust claim, so it is bounded by the
// payload: a segment with no field does not render. Pages own their
// from/to state and their newest `price_date`, so they PUBLISH those here;
// the bar never derives a range from the wall clock and never invents a
// vendor name. Below 768px the bar renders one truncated line (range +
// privacy + demo) with the full text in a Details popover.
//
// Context, not a module store: the provider lives inside AppShell and wraps
// BOTH the bar and <Outlet/>, and a page's cleanup clears its own segment the
// moment it unmounts — a stale range can never survive a route change.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

/** The active date range a money page is showing (payload-driven state). */
export interface PageRange {
  /** ISO date, inclusive. */
  from: string;
  /** ISO date, inclusive. */
  to: string;
}

/** The newest priced row a page's payload carries — prices segment (G9). */
export interface PagePrices {
  /** ISO date of the newest `price_date` across the rows on screen, or null. */
  price_date: string | null;
  /** The payload's own `price_source` (vendor token) — the bar renders it
   *  through `sourceLabel`, never the raw enum (docs/09 §Naming). */
  price_source: string | null;
}

export interface PageMeta {
  range: PageRange | null;
  prices: PagePrices | null;
}

const EMPTY: PageMeta = { range: null, prices: null };

const MetaContext = createContext<{
  meta: PageMeta;
  publish: (patch: Partial<PageMeta>) => void;
}>({ meta: EMPTY, publish: () => {} });

export function PageMetaProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<PageMeta>(EMPTY);
  const publish = useCallback((patch: Partial<PageMeta>) => {
    setMeta((prev) => {
      const next = { ...prev, ...patch };
      // Structural equality so a re-publish of the same values never loops.
      if (
        (next.range?.from ?? null) === (prev.range?.from ?? null) &&
        (next.range?.to ?? null) === (prev.range?.to ?? null) &&
        (next.prices?.price_date ?? null) ===
          (prev.prices?.price_date ?? null) &&
        (next.prices?.price_source ?? null) ===
          (prev.prices?.price_source ?? null)
      ) {
        return prev;
      }
      return next;
    });
  }, []);
  const value = useMemo(() => ({ meta, publish }), [meta, publish]);
  return <MetaContext.Provider value={value}>{children}</MetaContext.Provider>;
}

export function usePageMeta(): PageMeta {
  return useContext(MetaContext).meta;
}

/**
 * Publish this page's segments for as long as it is mounted. Values must come
 * from the page's own payload — a page that publishes a wall-clock-derived
 * range or a vendor name it did not read off a row violates docs/47 §1.3.
 */
export function usePublishPageMeta(patch: Partial<PageMeta>): void {
  const { publish } = useContext(MetaContext);
  const serialized = JSON.stringify(patch);
  useEffect(() => {
    publish(JSON.parse(serialized) as Partial<PageMeta>);
    // Clear the page's own segments on unmount so the next route starts from
    // the empty bar instead of inheriting a range it does not own.
    return () => publish({ range: null, prices: null });
    // `serialized` is the stable identity of the patch (same object identity
    // is not guaranteed across renders of a page that rebuilds its props).
  }, [serialized, publish]);
}
