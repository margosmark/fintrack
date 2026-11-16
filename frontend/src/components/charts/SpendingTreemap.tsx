import { useEffect, useRef, useState } from "react";
import type { SpendSlice } from "../../api/reports";
import { sharePct, trimPct } from "../../lib/percent";
import { squarify } from "../../lib/treemap";
import { chartAriaLabel, dateRange, shareFact } from "./aria";
import { ChartFrame } from "./ChartFrame";
import { fmtCad, toNumber } from "./format";
import { NoDataPanel } from "./NoDataPanel";
import { useChartPalette } from "./palette";
import { useMasked } from "../../lib/mask";

/**
 * M108 P1 (docs/12 §Chart mapping, §Chart accessibility) — the Spending tab's
 * second chart variant: one block per category, area proportional to what was
 * spent in the range.
 *
 * Same data, same colors, same drill-in as the stacked bars:
 *   - the block set is `spendingReport.items` re-aggregated over buckets — NO
 *     second query surface, so the two variants can never disagree on totals;
 *   - a category's color is its index in the SAME ordering
 *     `SpendingStackedBarChart` derives (by `category_id`, uncategorized
 *     last), so a series is one color on both variants and the legend below
 *     matches the bars;
 *   - M124 (docs/12 §1) flattened the item into a `SpendSlice`, so this
 *     component no longer knows what a category IS: `id` is a category id or
 *     a tag id depending on which dimension the page asked for, and the
 *     caller supplies the drill target for it.
 *
 * Hover reads the exact amount plus a share TRIMMED to 2 decimals, never
 * re-rounded (M105's rule, `lib/percent.ts`); the digits the trim hides stay
 * on the legend button's `aria-label`, so assistive tech reads the raw share.
 *
 * Blocks carry NO inline text: every series fill in both palettes fails 4.5:1
 * against a single fixed text colour (white on dark-theme `#79a9ff` is 2.4:1,
 * black on light-theme `#65a30d` is 4.2:1), and the app's own donut solves the
 * same problem the same way — swatches in a legend, not labels on the paint.
 * The `<svg>` is `aria-hidden` under `ChartFrame`'s single `role="img"`
 * sentence, so the legend buttons below are the keyboard path (a focusable
 * element inside an `aria-hidden` subtree is an axe `aria-hidden-focus`
 * violation, which is exactly why the drill-in lives in the legend).
 */

const HEIGHT = 320;

interface Block {
  id: number | null;
  name: string;
  amount: number;
  color: string;
}

/** Series → color index, byte-for-byte the ordering the bar chart uses. */
function seriesOrder(items: readonly SpendSlice[]): Map<string, number> {
  const series = [
    ...new Map(items.map((item) => [item.name, item.id] as const)).entries(),
  ].sort((a, b) => (a[1] ?? -1) - (b[1] ?? -1));
  return new Map(series.map(([name], index) => [name, index]));
}

function buildBlocks(items: readonly SpendSlice[], colors: readonly string[]): Block[] {
  const order = seriesOrder(items);
  const byName = new Map<string, Block>();
  for (const item of items) {
    const amount = toNumber(item.total_cad);
    const existing = byName.get(item.name);
    if (existing !== undefined) {
      existing.amount += amount;
      continue;
    }
    byName.set(item.name, {
      id: item.id,
      name: item.name,
      amount,
      color: colors[(order.get(item.name) ?? 0) % colors.length],
    });
  }
  // Biggest first: layout order IS the visual reading order, and a zero block
  // (bucket zero-fill, docs/12) is not something that was spent.
  return [...byName.values()]
    .filter((block) => block.amount > 0)
    .sort((a, b) => b.amount - a.amount);
}

export function SpendingTreemap({
  items,
  title = "Spending by category",
  onDrill,
}: {
  /** Slices with the dimension already flattened (M124, docs/12 §1). */
  items: SpendSlice[];
  /** Accessible summary title (docs/12) — "Spending by owner" under M108 P3,
   *  "Spending by tag" under M124. */
  title?: string;
  /**
   * Drill-in. OMITTED when the grouping has no target to open — the Owner
   * dimension (M108 P3) groups rows by household member, and there is no
   * `/transactions?category=` target for "Partner", so inventing one would be
   * a link that filters on the wrong thing. Under M124 the caller decides per
   * dimension, which is why this takes the whole slice id and not a category.
   * Without it the legend is static text and the blocks are not clickable.
   */
  onDrill?: (id: number | null) => void;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);
  const [hover, setHover] = useState<Block | null>(null);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });

  // Real pixel width, so the squarify aspect ratios describe the box the user
  // actually sees rather than a fixed viewBox stretched to fit. 1000 is the
  // pre-measure fallback (and what jsdom renders at — ResizeObserver is absent
  // there, so the guard keeps the first paint non-empty instead of an empty svg).
  useEffect(() => {
    const node = wrapRef.current;
    if (node === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0;
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const blocks = buildBlocks(items, palette.SERIES_COLORS);
  const total = blocks.reduce((sum, block) => sum + block.amount, 0);
  const buckets = items.map((item) => item.bucket).sort();
  const top = blocks[0];

  const label = chartAriaLabel({
    title,
    kind: "treemap",
    range: dateRange(buckets[0], buckets[buckets.length - 1]),
    points: blocks.length,
    facts:
      blocks.length > 0 && total > 0 && top !== undefined
        ? [shareFact(top.name, top.amount, total), `total ${fmtCad(total)}`]
        : [],
  });

  if (blocks.length === 0 || !(width > 0)) return <NoDataPanel />;

  const rects = squarify(
    blocks.map((block) => block.amount),
    width,
    HEIGHT,
  );
  const rectOf = new Map(rects.map((rect) => [rect.index, rect]));

  return (
    <div ref={wrapRef} className="relative w-full">
      <ChartFrame label={label}>
        <svg
          width={width}
          height={HEIGHT}
          viewBox={`0 0 ${width} ${HEIGHT}`}
          aria-hidden
          className="block"
        >
          {blocks.map((block, index) => {
            const rect = rectOf.get(index);
            if (rect === undefined) return null;
            const share = sharePct(block.amount, total);
            const shareShown = share === null ? "—" : trimPct(share);
            const gap = rects.length > 1 ? 2 : 0;
            return (
              <rect
                key={block.name}
                x={rect.x + gap / 2}
                y={rect.y + gap / 2}
                width={Math.max(0, rect.w - gap)}
                height={Math.max(0, rect.h - gap)}
                rx={4}
                fill={block.color}
                className={
                  onDrill === undefined
                    ? "transition-opacity hover:opacity-90"
                    : "cursor-pointer transition-opacity hover:opacity-80"
                }
                onClick={onDrill === undefined ? undefined : () => onDrill(block.id)}
                onMouseEnter={(event) => {
                  const host = wrapRef.current?.getBoundingClientRect();
                  setPointer({
                    x: event.clientX - (host?.left ?? 0),
                    y: event.clientY - (host?.top ?? 0),
                  });
                  setHover(block);
                }}
                onMouseMove={(event) => {
                  const host = wrapRef.current?.getBoundingClientRect();
                  setPointer({
                    x: event.clientX - (host?.left ?? 0),
                    y: event.clientY - (host?.top ?? 0),
                  });
                }}
                onMouseLeave={() => setHover(null)}
              >
                <title>
                  {`${block.name} — ${fmtCad(String(block.amount))} (${shareShown}%)`}
                </title>
              </rect>
            );
          })}
        </svg>
      </ChartFrame>

      {/* Hover readout: the exact amount plus the TRIMMED share. Purely visual —
          aria-hidden, because the same numbers (with the RAW share) are on the
          legend button each block owns, and a role="status" here would announce
          every mouse move. Clamped inside the chart box so it cannot overflow
          the card at 390px. */}
      {hover !== null && (
        <div
          aria-hidden
          className="glass-strong pointer-events-none absolute z-10 max-w-56 rounded-lg px-3 py-2 text-xs shadow-card"
          style={{
            left: Math.min(Math.max(pointer.x + 12, 4), Math.max(4, width - 232)),
            top: Math.min(pointer.y + 12, HEIGHT - 64),
          }}
        >
          <p className="font-medium text-text">{hover.name}</p>
          <p className="mt-0.5 tabular text-muted">
            {fmtCad(String(hover.amount))}
            {(() => {
              const share = sharePct(hover.amount, total);
              return share === null ? "" : ` · ${trimPct(share)}%`;
            })()}
          </p>
        </div>
      )}

      {/* Legend = the keyboard path AND the mobile reading order.
          WITH a drill target each row is a ≥44px button whose aria-label
          carries the full-precision share; WITHOUT one (M108 P3's Owner
          grouping — there is no category to open) it is plain list text, so
          the raw share rides in an sr-only span instead of an aria-label that
          nothing would ever be focused to read. */}
      <ul className="mt-3 flex flex-wrap gap-2">
        {blocks.map((block) => {
          const share = sharePct(block.amount, total);
          const shown = share === null ? "—" : trimPct(share);
          const swatch = (
            <span
              className="size-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: block.color }}
              aria-hidden
            />
          );
          const figures = (
            <>
              <span className="whitespace-nowrap text-text">{block.name}</span>
              <span className="tabular text-muted">{fmtCad(String(block.amount))}</span>
              <span className="tabular text-muted">{shown}%</span>
            </>
          );
          const precise =
            share === null ? null : <span className="sr-only"> ({share}% precise)</span>;
          return (
            <li key={block.name}>
              {onDrill === undefined ? (
                <span className="inline-flex min-h-11 min-w-11 items-center gap-2 rounded-lg border border-control bg-veil/[0.04] px-3 py-2 text-sm">
                  {swatch}
                  {figures}
                  {precise}
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => onDrill(block.id)}
                  aria-label={
                    share === null
                      ? `${block.name} — share of range total unavailable. Show these transactions.`
                      : `${block.name} — ${fmtCad(String(block.amount))}, ${share}% of the range total (shown as ${shown}%). Show these transactions.`
                  }
                  className="inline-flex min-h-11 min-w-11 items-center gap-2 rounded-lg border border-control bg-veil/[0.04] px-3 py-2 text-sm outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {swatch}
                  {figures}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
