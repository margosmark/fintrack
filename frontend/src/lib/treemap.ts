// Squarified treemap layout (M108 P1, docs/12 §Chart mapping).
//
// The locked stack (docs/01) has no d3-hierarchy, and this slice adds no
// dependency — so the layout is a hand-rolled port of Bruls, Huizing & van
// Wijk's "squarified treemap" heuristic: peel rows of blocks off the SHORTEST
// side of the remaining rectangle, adding a value to the row while doing so
// LOWERS the row's worst aspect ratio, then freeze the row and rotate.
//
// Everything here works in ABSOLUTE AREAS (value × px²), so the caller scales
// once up front and the math below never sees a raw money number. Zero and
// non-finite values are dropped by {@link squarify} — a zero-area block has no
// aspect ratio and no honest position, and a spending report's zero rows come
// from bucket zero-fill, not from real spend.

export interface TreemapRect {
  /** Index of the value this rectangle belongs to (caller's array order). */
  index: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Worst aspect ratio a row of `areas` would have against a `side`-long edge. */
function worstRatio(areas: readonly number[], side: number): number {
  if (areas.length === 0 || side <= 0) return Number.POSITIVE_INFINITY;
  let sum = 0;
  let max = Number.NEGATIVE_INFINITY;
  let min = Number.POSITIVE_INFINITY;
  for (const area of areas) {
    sum += area;
    if (area > max) max = area;
    if (area < min) min = area;
  }
  if (sum <= 0) return Number.POSITIVE_INFINITY;
  const s2 = sum * sum;
  const l2 = side * side;
  return Math.max((l2 * max) / s2, s2 / (l2 * min));
}

/**
 * Partition `values` into `width × height`, biggest first, each rectangle's
 * area proportional to its value. Returns one rect per NON-ZERO value, in
 * caller order of the input array; values ≤ 0 or non-finite get no rect.
 *
 * Degenerate canvases (empty input, non-positive width/height, an all-zero
 * total) return `[]` rather than dividing by zero — the caller renders its
 * "No data" state instead of a wall of NaN coordinates.
 */
export function squarify(
  values: readonly number[],
  width: number,
  height: number,
): TreemapRect[] {
  const rects: TreemapRect[] = [];
  if (!(width > 0) || !(height > 0) || values.length === 0) return rects;

  const live = values
    .map((value, index) => ({ value, index }))
    .filter((entry) => Number.isFinite(entry.value) && entry.value > 0);
  if (live.length === 0) return rects;
  live.sort((a, b) => b.value - a.value || a.index - b.index);

  const total = live.reduce((sum, entry) => sum + entry.value, 0);
  const scale = (width * height) / total;

  let x = 0;
  let y = 0;
  let w = width;
  let h = height;
  let cursor = 0;
  let row: Array<{ index: number; area: number }> = [];

  const flushRow = (): void => {
    if (row.length === 0) return;
    const rowArea = row.reduce((sum, entry) => sum + entry.area, 0);
    // The floor is float-drain protection, not a fudge: the last flush of a
    // full-canvas partition leaves `w*h` at ~1e-15, and dividing by it would
    // hand out Infinity-sized blocks instead of the row that belongs there.
    if (w <= h) {
      // Strip spans the full remaining WIDTH; items run left → right.
      const stripH = rowArea / Math.max(w, 1e-9);
      let cx = x;
      for (const entry of row) {
        const ew = entry.area / stripH;
        rects.push({ index: entry.index, x: cx, y, w: ew, h: stripH });
        cx += ew;
      }
      y += stripH;
      h = Math.max(0, h - stripH);
    } else {
      // Strip spans the full remaining HEIGHT; items run top → bottom.
      const stripW = rowArea / Math.max(h, 1e-9);
      let cy = y;
      for (const entry of row) {
        const eh = entry.area / stripW;
        rects.push({ index: entry.index, x, y: cy, w: stripW, h: eh });
        cy += eh;
      }
      x += stripW;
      w = Math.max(0, w - stripW);
    }
    row = [];
  };

  while (cursor < live.length) {
    const entry = { index: live[cursor].index, area: live[cursor].value * scale };
    const side = Math.min(w, h);
    if (row.length === 0) {
      row.push(entry);
      cursor += 1;
      continue;
    }
    const current = row.map((r) => r.area);
    const grown = [...current, entry.area];
    if (worstRatio(grown, side) <= worstRatio(current, side)) {
      row.push(entry);
      cursor += 1;
    } else {
      flushRow();
    }
  }
  flushRow();

  return rects;
}
