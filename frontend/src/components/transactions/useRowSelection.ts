// Row multi-select for the ledger tables (docs/09 §Bulk selection toolbar).
//
// Two selection scopes, and the difference is what keeps them honest:
//
//   * PAGE — an explicit set of the ids in front of you (M96). It dies with
//     its page, its filter and its mount: an id that left the view can never
//     reach the API, so there is no "50 rows selected but only 30 of them are
//     here" state to confuse anyone.
//   * MATCHED — "every row the current filter matches" (M125). It is defined
//     by the FILTER, not by the page, so it SURVIVES pagination (that is the
//     whole point of selecting 345 rows at once) and is transmitted as
//     `{filter, exclude_ids}` — never as 345 ids. Deselecting a row out of it
//     adds an exception instead of collapsing the selection.
//
// The counter always reports exactly what a bulk action will touch: the
// server's `total` minus the exceptions while matched, the visible intersection
// while on a page. Never a client-side sum of a server-side set.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type SelectionMode = "page" | "matched";

export interface SelectionSnapshot {
  selected: ReadonlySet<number>;
  anchorId: number | null;
}

/**
 * The ids between the anchor and `id`, in visible order — `null` when the
 * anchor is gone (a stale anchor must never select an arbitrary span).
 *
 * ONE implementation behind both gestures: `extendFromAnchor` ADDS this range
 * in page mode, and the matched-mode toggle REMOVES it (everything in a
 * matched set is already selected, so "add" has nothing left to add).
 */
export function rangeBetween(
  visibleIds: readonly number[],
  anchorId: number | null,
  id: number,
): number[] | null {
  if (anchorId === null) return null;
  const from = visibleIds.indexOf(anchorId);
  const to = visibleIds.indexOf(id);
  if (from < 0 || to < 0 || from === to) return null;
  const [lo, hi] = from < to ? [from, to] : [to, from];
  return visibleIds.slice(lo, hi + 1);
}

/** Single click: add or drop exactly this row, and re-anchor the range here. */
export function toggleOne(
  selected: ReadonlySet<number>,
  id: number,
): Set<number> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * Shift+click: ADD the visible range between the anchor and `id` (the desktop
 * convention — a shift-click never removes). Both ends are resolved against
 * the CURRENT visible order, so a stale anchor outside this view falls back to
 * a plain toggle instead of selecting an arbitrary span.
 */
export function extendFromAnchor(
  selected: ReadonlySet<number>,
  visibleIds: readonly number[],
  anchorId: number | null,
  id: number,
): Set<number> | null {
  const range = rangeBetween(visibleIds, anchorId, id);
  if (range === null) return null;
  const next = new Set(selected);
  for (const rowId of range) next.add(rowId);
  return next;
}

/** Header checkbox: select every visible row, or drop every visible row. */
export function setAllVisible(
  selected: ReadonlySet<number>,
  visibleIds: readonly number[],
  checked: boolean,
): Set<number> {
  if (checked) return new Set(visibleIds);
  const next = new Set(selected);
  for (const id of visibleIds) next.delete(id);
  return next;
}

/**
 * M107 (docs/44 §6.3): the split GROUP header's checkbox — add or drop
 * exactly this group's children in ONE update. Driving it through N calls to
 * `toggle` would lose N-1 of them (every call reads the same render's
 * `selected`), so the group needs its own primitive.
 */
export function toggleMany(
  selected: ReadonlySet<number>,
  ids: readonly number[],
  checked: boolean,
): Set<number> {
  const next = new Set(selected);
  for (const id of ids) {
    if (checked) next.add(id);
    else next.delete(id);
  }
  return next;
}

/** Selection ∩ visible — the ids a PAGE-scoped action may touch. */
export function intersectSelected(
  selected: ReadonlySet<number>,
  visibleIds: readonly number[],
): number[] {
  return visibleIds.filter((id) => selected.has(id));
}

export interface RowSelection {
  mode: SelectionMode;
  /** Page-scoped ids. Ignored while `mode === "matched"`. */
  selected: ReadonlySet<number>;
  /** Matched-set exceptions — the ids the user unchecked out of the filter. */
  excluded: ReadonlySet<number>;
  /** What the counter must print, and what a bulk action will act on. */
  count: number;
  /** The server's `total` behind `count` in matched mode, else null. */
  matchedTotal: number | null;
  anchorId: number | null;
  isSelected: (id: number) => boolean;
  /** `shift` extends from the anchor; otherwise this row is toggled. */
  toggle: (id: number, shift?: boolean) => void;
  setAllVisible: (checked: boolean) => void;
  /** Toggle an explicit id set (a split group's children on this page). */
  toggleMany: (ids: readonly number[], checked: boolean) => void;
  clear: () => void;
  /** Header checkbox — the three-state cycle (docs/09 §Select-all-matched). */
  headerChecked: boolean;
  headerIndeterminate: boolean;
  headerLabel: string;
  headerToggle: () => void;
}

interface UseRowSelectionArgs {
  /** Filters + search mode — changing it wipes BOTH scopes. */
  filterKey: string;
  /** Offset within the view — changing it wipes PAGE-scoped selection only. */
  pageKey?: string;
  /** Ids currently rendered, in table order. */
  visibleIds: readonly number[];
  /**
   * The server's `total` for this filter, or `null` when the view is not
   * server-described. The fuzzy search scores in the browser (docs/33 S5), so
   * its matched set has no server count and no `select-all-matched` — the
   * header checkbox keeps M96's page behaviour there.
   */
  matchedTotal?: number | null;
}

export function useRowSelection({
  filterKey,
  pageKey = "",
  visibleIds,
  matchedTotal = null,
}: UseRowSelectionArgs): RowSelection {
  const [mode, setMode] = useState<SelectionMode>("page");
  const [selected, setSelected] = useState<ReadonlySet<number>>(() => new Set<number>());
  const [excluded, setExcluded] = useState<ReadonlySet<number>>(() => new Set<number>());
  const [anchorId, setAnchorId] = useState<number | null>(null);

  // Latest inputs for the next click (effects run before any user event).
  const visibleRef = useRef<readonly number[]>(visibleIds);
  const matchedTotalRef = useRef<number | null>(matchedTotal);
  const modeRef = useRef<SelectionMode>(mode);
  useEffect(() => {
    visibleRef.current = visibleIds;
    matchedTotalRef.current = matchedTotal;
    modeRef.current = mode;
  });

  const clear = useCallback(() => {
    setMode("page");
    setSelected(new Set<number>());
    setExcluded(new Set<number>());
    setAnchorId(null);
  }, []);

  // A different filter or search mode is a different selection universe.
  useEffect(() => {
    clear();
  }, [filterKey, clear]);

  // A different PAGE is a different set of visible ids. A page-scoped
  // selection dies with its page (M96's rule, and the reason no id can leave
  // the view); the matched set survives it, because paging changes nothing
  // about what the filter matches.
  const lastPageKey = useRef(pageKey);
  useEffect(() => {
    if (lastPageKey.current === pageKey) return;
    lastPageKey.current = pageKey;
    if (modeRef.current !== "page") return;
    setSelected(new Set<number>());
    setAnchorId(null);
  }, [pageKey]);

  const isSelected = useCallback(
    (id: number) => (mode === "matched" ? !excluded.has(id) : selected.has(id)),
    [mode, excluded, selected],
  );

  const count = useMemo(() => {
    if (mode === "matched") {
      return Math.max(0, (matchedTotal ?? 0) - excluded.size);
    }
    // Derived from the VISIBLE intersection, never from `selected.size`: a
    // page change renders before its effect clears, and that one frame must
    // not claim the previous page's rows are still selected.
    let visible = 0;
    for (const id of visibleIds) if (selected.has(id)) visible += 1;
    return visible;
  }, [mode, matchedTotal, excluded, visibleIds, selected]);

  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => isSelected(id));

  const headerChecked = mode === "matched" ? excluded.size === 0 : allVisibleSelected;
  const headerIndeterminate =
    mode === "matched"
      ? excluded.size > 0
      : !allVisibleSelected && visibleIds.some((id) => isSelected(id));
  // "Select all N matched" is offered only while the matched set is genuinely
  // bigger than the page — a second tick that could only re-select what tick
  // one already selected is a lie of a control.
  const moreMatched =
    matchedTotal !== null && matchedTotal > visibleIds.length;
  const headerLabel =
    mode === "matched"
      ? excluded.size > 0
        ? `Select all ${matchedTotal ?? 0} matched transactions`
        : `Clear selection — ${count} matched transactions selected`
      : allVisibleSelected
        ? moreMatched
          ? `Select all ${matchedTotal} matched transactions`
          : "Clear selection"
        : "Select every transaction on this page";

  const headerToggle = useCallback(() => {
    const visible = visibleRef.current;
    const total = matchedTotalRef.current;
    if (visible.length === 0) {
      clear();
      return;
    }
    if (mode === "matched") {
      // Indeterminate → checked: the exceptions come off and the whole matched
      // set is selected again. Checked → unchecked: everything clears.
      if (excluded.size > 0) {
        setExcluded(new Set<number>());
        return;
      }
      clear();
      return;
    }
    const allVisible = visible.every((id) => selected.has(id));
    if (!allVisible) {
      // Unchecked or partial → tick 1: the visible page.
      setMode("page");
      setSelected(new Set(visible));
      setExcluded(new Set<number>());
      setAnchorId(null);
      return;
    }
    if (total !== null && total > visible.length) {
      // Tick 2: EVERY row the filter matches — server-side (select-all-matched).
      setMode("matched");
      setSelected(new Set<number>());
      setExcluded(new Set<number>());
      setAnchorId(null);
      return;
    }
    clear();
  }, [mode, excluded, selected, clear]);

  const toggle = useCallback(
    (id: number, shift = false) => {
      const visible = visibleRef.current;
      if (mode === "matched") {
        // Everything in the matched set is already selected, so the range
        // gesture marks rows to EXCLUDE — the mirror of the page-mode gesture,
        // which adds them. A stale anchor still falls back to a plain toggle.
        if (shift) {
          const range = rangeBetween(visible, anchorId, id);
          if (range !== null) {
            setExcluded((prev) => {
              const next = new Set(prev);
              for (const rowId of range) next.add(rowId);
              return next;
            });
            setAnchorId(id);
            return;
          }
        }
        setExcluded((prev) => toggleOne(prev, id));
        setAnchorId(id);
        return;
      }
      if (shift) {
        const extended = extendFromAnchor(selected, visible, anchorId, id);
        if (extended !== null) {
          setSelected(extended);
          return;
        }
      }
      setMode("page");
      setSelected(toggleOne(selected, id));
      setAnchorId(id);
    },
    [mode, selected, anchorId],
  );

  const setAll = useCallback(
    (checked: boolean) => {
      setMode("page");
      setSelected((prev) => setAllVisible(prev, visibleRef.current, checked));
      setExcluded(new Set<number>());
      setAnchorId(null);
    },
    [],
  );

  const many = useCallback(
    (ids: readonly number[], checked: boolean) => {
      if (mode === "matched") {
        setExcluded((prev) => {
          const next = new Set(prev);
          for (const id of ids) {
            if (checked) next.delete(id);
            else next.add(id);
          }
          return next;
        });
        return;
      }
      setMode("page");
      setSelected((prev) => toggleMany(prev, ids, checked));
      setAnchorId(null);
    },
    [mode],
  );

  return {
    mode,
    selected,
    excluded,
    count,
    matchedTotal: mode === "matched" ? matchedTotal : null,
    anchorId,
    isSelected,
    toggle,
    setAllVisible: setAll,
    toggleMany: many,
    clear,
    headerChecked,
    headerIndeterminate,
    headerLabel,
    headerToggle,
  };
}
