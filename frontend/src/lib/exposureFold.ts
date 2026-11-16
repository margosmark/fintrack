// exposureFold — the ONE "top N + Other" rule behind every Exposure card list
// (M167, docs/09 §Exposure visuals).
//
// Mark's complaint was a geography list that scrolls through ~40 countries to
// reach a rounding error. The fix is a fold: the head of the list, an `Other`
// row that owns the tail, and an expander that reveals the full list inline.
// Sector, asset class, companies and geography all render the same table, so
// the rule lives here — four call sites would drift within a release.
//
// Two honesty rules (M147's family):
//   * `Unclassified` is NEVER folded into `Other`. "Other" means "the long
//     tail you chose not to read"; "Unclassified" means "the pipeline has no
//     answer" — merging them would bury the bucket whose whole job is to be
//     explained. It stays visible, ranked among the rows it belongs with;
//   * the fold never invents a number: `Other.market_value_cad` is the exact
//     cent-sum of the rows it folds and `Other.weight_pct` their exact sum, so
//     head + Other === the full list, always (pinned by
//     tests/m167ExposureFold.test.ts).
//
// Expansion is per-session (M139's sessionStorage pattern): a tab remembers
// what you expanded, a new tab starts collapsed again, nothing server-side.

import { useCallback, useSyncExternalStore } from "react";
import type { BreakdownDimension, BreakdownRow } from "../api/portfolio";

/** The bucket the pipeline could not classify (docs/09 §Exposure honesty). */
export const UNCLASSIFIED_KEY = "Unclassified";

/** The folded tail's key. Never a real row's name — see `isOtherBucket`. */
export const OTHER_KEY = "Other";

/**
 * Rows kept before the fold, per dimension. Geography/sector/asset class fold
 * at 8; companies — potentially hundreds of tickers — fold at 10.
 */
export const FOLD_N_BY_DIMENSION: Record<BreakdownDimension, number> = {
  sector: 8,
  geography: 8,
  asset_class: 8,
  company: 10,
};

/** What "the rest" is called in the expander, per dimension. */
export const DIMENSION_PLURAL: Record<BreakdownDimension, string> = {
  sector: "sectors",
  geography: "countries",
  asset_class: "asset classes",
  company: "companies",
};

/** The folded tail, as a row the table and the donut can both render. */
export interface OtherBucket {
  key: typeof OTHER_KEY;
  count: number;
  market_value_cad: string;
  weight_pct: number;
}

export function isOtherBucket(row: BreakdownRow): row is OtherBucket {
  return row.key === OTHER_KEY && "count" in row;
}

export interface Fold<T extends BreakdownRow> {
  /** Rows the COLLAPSED view renders: the head, `Unclassified`, then Other. */
  rows: (T | OtherBucket)[];
  /** The folded tail, or null when everything fits above the fold. */
  other: OtherBucket | null;
  /** Every row in weight order — what the EXPANDED view renders. */
  all: T[];
  /** How many rows the collapsed view hides behind the expander. */
  hiddenCount: number;
}

/** Sum money strings exactly, in cents — floats would drift on a 40-row tail. */
function sumCents(values: readonly string[]): string {
  const cents = values.reduce((total, value) => {
    const n = Number(value);
    return total + (Number.isFinite(n) ? Math.round(n * 100) : 0);
  }, 0);
  return (cents / 100).toFixed(2);
}

/**
 * Apply the fold. `rows` may arrive in any order; the fold sorts by weight
 * descending (ties by name, so the rendering is deterministic) and returns the
 * same order the expanded view uses.
 */
export function foldRows<T extends BreakdownRow>(rows: readonly T[], n: number): Fold<T> {
  const all = [...rows].sort(
    (a, b) => b.weight_pct - a.weight_pct || a.key.localeCompare(b.key),
  );
  const head = all.slice(0, Math.max(0, n));
  const tail = all.slice(Math.max(0, n));
  const hiddenCount = tail.filter((row) => row.key !== UNCLASSIFIED_KEY).length;

  // `Unclassified` keeps its slot outside the fold: it is visible at any rank.
  const unclassified = head.filter((row) => row.key === UNCLASSIFIED_KEY);
  const classifiedHead = head.filter((row) => row.key !== UNCLASSIFIED_KEY);
  const classifiedTail = tail.filter((row) => row.key !== UNCLASSIFIED_KEY);
  const escapedUnclassified = tail.filter((row) => row.key === UNCLASSIFIED_KEY);

  const other: OtherBucket | null =
    classifiedTail.length > 0
      ? {
          key: OTHER_KEY,
          count: classifiedTail.length,
          market_value_cad: sumCents(classifiedTail.map((row) => row.market_value_cad)),
          weight_pct: classifiedTail.reduce((sum, row) => sum + row.weight_pct, 0),
        }
      : null;

  const visible: (T | OtherBucket)[] = [
    ...classifiedHead,
    ...unclassified,
    ...escapedUnclassified,
  ];
  if (other) visible.push(other);

  return { rows: visible, other, all, hiddenCount };
}

// --- Per-session expansion memory -------------------------------------------

const EXPANDED_KEY = "fintrack.exposure-expanded.v1";

function readStore(): ReadonlySet<BreakdownDimension> {
  try {
    const raw = window.sessionStorage.getItem(EXPANDED_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed as BreakdownDimension[]);
  } catch {
    // Storage blocked (private mode): a tab that cannot remember starts
    // collapsed every time — correctness first, never a broken list.
    return new Set();
  }
}

let expanded: ReadonlySet<BreakdownDimension> = readStore();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function persist(): void {
  try {
    window.sessionStorage.setItem(EXPANDED_KEY, JSON.stringify([...expanded]));
  } catch {
    // Best-effort, same as the read path.
  }
}

/** Expand/collapse one dimension for the rest of this session. */
export function setExpandedDimension(dimension: BreakdownDimension, next: boolean): void {
  if (expanded.has(dimension) === next) return;
  const draft = new Set(expanded);
  if (next) draft.add(dimension);
  else draft.delete(dimension);
  expanded = draft;
  persist();
  notify();
}

/** Tests (and the "everything collapsed" state) reset the session memory here. */
export function clearExpandedDimensions(): void {
  expanded = new Set();
  try {
    window.sessionStorage.removeItem(EXPANDED_KEY);
  } catch {
    // Nothing to clear.
  }
  notify();
}

/**
 * Is this dimension's list expanded right now? Subscribing components
 * re-render when any dimension flips, so the table and its expander never
 * disagree with the session record.
 */
export function useExpandedDimension(dimension: BreakdownDimension): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    listeners.add(onChange);
    return () => {
      listeners.delete(onChange);
    };
  }, []);
  const getSnapshot = useCallback(() => expanded.has(dimension), [dimension]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
