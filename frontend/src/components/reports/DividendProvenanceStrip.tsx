/**
 * The source strip every dividend view renders (docs/07 §M184): one badge per
 * source — label, the day it last answered, its age — pulsing red when the
 * source is stale or missing.
 *
 * It prints dates, counts and labels only, never money, so it needs no display
 * mask handling (docs/46). The per-source sentence (WHY it is stale/missing)
 * rides in the container's `title`, and the state word is always in the badge
 * text itself — the tooltip is supplementary, never the only carrier.
 */
import type { DividendProvenance, DividendProvenanceSource } from "../../api/dividendProvenance";
import { Badge, type BadgeTone } from "../ui/Badge";

function toneFor(source: DividendProvenanceSource): BadgeTone {
  return source.status === "ok" ? "neutral" : "danger";
}

function labelFor(source: DividendProvenanceSource): string {
  const parts = [source.label];
  // The provider rides on the badge when the label does not already carry it
  // ("yfinance dividend history" would repeat itself; "trade-date FX cache"
  // does not say WHO filled the book — boc_valet or a manual gap-fill).
  if (source.provider !== null && !source.label.includes(source.provider)) parts.push(source.provider);
  if (source.as_of !== null) parts.push(source.as_of.slice(0, 10));
  if (source.age_days !== null) parts.push(`${source.age_days}d`);
  if (source.status === "stale") parts.push("(stale)");
  if (source.status === "missing") parts.push("(missing)");
  return parts.join(" · ");
}

export function DividendProvenanceStrip({
  provenance,
  className = "",
}: {
  /** Absent while a payload predates the seam — render nothing, never a stub. */
  provenance?: DividendProvenance | null;
  className?: string;
}) {
  if (!provenance || provenance.sources.length === 0) return null;
  const title = provenance.sources.map((source) => `${source.label} — ${source.detail}`).join("\n");
  return (
    <div
      className={`flex flex-wrap items-center gap-1.5 ${className}`}
      data-testid="dividend-provenance"
      title={title}
    >
      <span className="text-[10px] uppercase tracking-wider text-muted">Source</span>
      {provenance.sources.map((source) => (
        <Badge key={source.key} tone={toneFor(source)} pulse={source.status !== "ok"}>
          {labelFor(source)}
        </Badge>
      ))}
    </div>
  );
}
