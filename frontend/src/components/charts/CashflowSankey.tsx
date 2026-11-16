import { useChartPalette } from "./palette";
import { ChartFrame } from "./ChartFrame";
import { chartAriaLabel, dateRange, formatCad, shareFact } from "./aria";
import type { SankeyNode, SankeyReport } from "../../lib/sankeyEngine";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Sankey cash-flow — three columns: sources → one `Money in` hub →
 * destinations. A hand-rolled SVG layout (the locked stack has no d3-sankey,
 * and a new dependency would land in package.json, which this slice does not
 * own); every number comes from the client engine, which is golden-diffed
 * against the server's own JSON.
 *
 * Accessibility follows docs/12 exactly: `ChartFrame` carries `role="img"` +
 * one `chartAriaLabel` sentence, and the `<svg>` tree is `aria-hidden`, so a
 * screen reader hears the totals instead of fifty `<path>` nodes. No entrance
 * animation runs, which is what the `prefers-reduced-motion` contract wants.
 */

const W = 1000;
const H = 400;
const PAD_TOP = 16;
const PAD_BOTTOM = 16;
const GAP = 10;

const COL = { srcX: 196, hubX: 494, dstX: 792, bar: 12 };
const LABEL_SRC_X = COL.srcX - 8;
const LABEL_DST_X = COL.dstX + COL.bar + 8;

function amountOf(node: { amount_cad: string }): number {
  const n = Number(node.amount_cad);
  return Number.isFinite(n) ? n : 0;
}

/** Push labels apart so thin nodes never print on top of each other. */
function labelPositions(centers: number[], minGap: number): number[] {
  if (centers.length === 0) return [];
  const out = [...centers];
  for (let i = 1; i < out.length; i += 1) {
    if (out[i] - out[i - 1] < minGap) out[i] = out[i - 1] + minGap;
  }
  const overflow = out[out.length - 1] - (H - PAD_BOTTOM);
  if (overflow > 0) {
    for (let i = out.length - 1; i >= 0; i -= 1) {
      out[i] -= overflow;
      if (i > 0 && out[i] - out[i - 1] >= minGap) break;
      if (i > 0) out[i - 1] = out[i] - minGap;
    }
  }
  return out;
}

function nodeColor(
  node: SankeyNode,
  palette: ReturnType<typeof useChartPalette>,
): string {
  if (node.column === "total") return palette.ACCENT;
  if (node.group === "spent") return palette.LOSS;
  if (node.group === "saved") return palette.GAIN;
  if (node.group === "drawn") return palette.WARN;
  if (node.group === "invested") return palette.ACCENT_SOFT;
  return palette.GAIN; // income sources
}

/** Ribbon path from (x0, y0) to (x1, y1), each side `thickness` tall. */
function ribbon(x0: number, y0: number, x1: number, y1: number, thickness: number): string {
  const mid = (x0 + x1) / 2;
  return [
    `M ${x0} ${y0}`,
    `C ${mid} ${y0}, ${mid} ${y1}, ${x1} ${y1}`,
    `L ${x1} ${y1 + thickness}`,
    `C ${mid} ${y1 + thickness}, ${mid} ${y0 + thickness}, ${x0} ${y0 + thickness}`,
    "Z",
  ].join(" ");
}

export function CashflowSankey({ report }: { report: SankeyReport }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const palette = useChartPalette();

  const sources = report.nodes.filter((n) => n.column === "source");
  const destinations = report.nodes.filter((n) => n.column === "destination");
  const hub = report.nodes.find((n) => n.column === "total");
  const moneyIn = hub ? amountOf(hub) : 0;

  const gapTotal = GAP * (Math.max(sources.length, destinations.length, 1) - 1);
  const usable = H - PAD_TOP - PAD_BOTTOM - gapTotal;
  const scale = moneyIn > 0 ? usable / moneyIn : 0;

  /** Running y for a stacked column, vertically centred. */
  const stackTop = (count: number): number => {
    const total = moneyIn * scale + GAP * Math.max(count - 1, 0);
    return PAD_TOP + (H - PAD_TOP - PAD_BOTTOM - total) / 2;
  };

  const srcTop = stackTop(sources.length);
  const dstTop = stackTop(destinations.length);
  const hubHeight = moneyIn * scale;
  const hubTop = PAD_TOP + (H - PAD_TOP - PAD_BOTTOM - hubHeight) / 2;

  let y = srcTop;
  const srcLayout = sources.map((node) => {
    const h = amountOf(node) * scale;
    const entry = { node, y, h };
    y += h + GAP;
    return entry;
  });
  y = dstTop;
  const dstLayout = destinations.map((node) => {
    const h = amountOf(node) * scale;
    const entry = { node, y, h };
    y += h + GAP;
    return entry;
  });

  // Hub-side offsets: incoming links stack down the hub's left edge in the
  // order the engine emitted them; outgoing links do the same on its right.
  let hubIn = hubTop;
  const incoming = srcLayout.map((entry) => {
    const h = amountOf(entry.node) * scale;
    const ribbonY = hubIn;
    hubIn += h;
    return { ...entry, ribbonY };
  });
  let hubOut = hubTop;
  const outgoing = dstLayout.map((entry) => {
    const h = amountOf(entry.node) * scale;
    const ribbonY = hubOut;
    hubOut += h;
    return { ...entry, ribbonY };
  });

  const srcCenters = labelPositions(
    srcLayout.map((e) => e.y + e.h / 2),
    14,
  );
  const dstCenters = labelPositions(
    dstLayout.map((e) => e.y + e.h / 2),
    14,
  );

  const topSource = sources[0];
  const topDestination = destinations[0];
  const label = chartAriaLabel({
    title: "Cash flow Sankey",
    kind: "three-column money flow",
    range: dateRange(report.from, report.to),
    points: report.nodes.length,
    facts: [
      `money in ${formatCad(moneyIn)}`,
      topSource ? `largest source ${shareFact(topSource.label, amountOf(topSource), moneyIn)}` : "",
      topDestination
        ? `largest destination ${shareFact(topDestination.label, amountOf(topDestination), moneyIn)}`
        : "",
      `invested ${formatCad(Number(report.totals.invested_cad))}`,
      `saved ${formatCad(Number(report.totals.saved_cad))}`,
    ].filter((fact) => fact.length > 0),
  });

  return (
    <ChartFrame label={label}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        className="h-[400px] w-full"
        aria-hidden="true"
      >
        <g>
          {incoming.map((link) => (
            <path
              key={`in-${link.node.id}`}
              d={ribbon(COL.srcX + COL.bar, link.y, COL.hubX, link.ribbonY, link.h)}
              fill={nodeColor(link.node, palette)}
              opacity={0.42}
            />
          ))}
          {outgoing.map((link) => (
            <path
              key={`out-${link.node.id}`}
              d={ribbon(COL.hubX + COL.bar, link.ribbonY, COL.dstX, link.y, link.h)}
              fill={nodeColor(link.node, palette)}
              opacity={0.42}
            />
          ))}
        </g>

        {srcLayout.map((entry, i) => (
          <g key={entry.node.id}>
            <rect
              x={COL.srcX}
              y={entry.y}
              width={COL.bar}
              height={Math.max(entry.h, 1)}
              rx={3}
              fill={nodeColor(entry.node, palette)}
            />
            <text
              x={LABEL_SRC_X}
              y={srcCenters[i]}
              textAnchor="end"
              dominantBaseline="middle"
              fontSize={11}
              fill={palette.LEGEND_COLOR}
            >
              {entry.node.label} {formatCad(amountOf(entry.node))}
            </text>
          </g>
        ))}

        {hub && (
          <g>
            <rect
              x={COL.hubX}
              y={hubTop}
              width={COL.bar}
              height={Math.max(hubHeight, 1)}
              rx={3}
              fill={palette.ACCENT}
            />
            <text
              x={COL.hubX + COL.bar / 2}
              y={hubTop - 6}
              textAnchor="middle"
              fontSize={11}
              fill={palette.LEGEND_COLOR}
            >
              {hub.label} {formatCad(moneyIn)}
            </text>
          </g>
        )}

        {dstLayout.map((entry, i) => (
          <g key={entry.node.id}>
            <rect
              x={COL.dstX}
              y={entry.y}
              width={COL.bar}
              height={Math.max(entry.h, 1)}
              rx={3}
              fill={nodeColor(entry.node, palette)}
            />
            <text
              x={LABEL_DST_X}
              y={dstCenters[i]}
              textAnchor="start"
              dominantBaseline="middle"
              fontSize={11}
              fill={palette.LEGEND_COLOR}
            >
              {entry.node.label} {formatCad(amountOf(entry.node))}
            </text>
          </g>
        ))}
      </svg>
    </ChartFrame>
  );
}
