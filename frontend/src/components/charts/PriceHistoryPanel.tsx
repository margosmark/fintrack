// PriceHistoryPanel — the loading / error / empty contract around
// PriceHistoryChart (M126, docs/09 §Empty states, docs/08 §Price history).
//
// One component owns the range state for BOTH places the price chart renders
// (the security page and a positions row's expansion), so "what range am I
// looking at" cannot drift between them and the header — title + range
// control — is identical in every state: a control that vanishes while its
// request is in flight leaves the user unable to see or change what they
// just asked for.
//
// Data comes from `usePriceHistory` (GET /prices/history): ONE request per
// (security, range), cached, and never a side-effecting refresh — so this
// panel is safe under privacy mode and costs nothing to expand and collapse.

import { useState, type ReactNode } from "react";
import { Inbox } from "lucide-react";

import { usePriceHistory } from "../../hooks/usePriceHistory";
import type { ChartRange } from "../../lib/chartRange";
import { PriceHistoryChart, PriceHistoryHeader } from "./PriceHistoryChart";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";

export function PriceHistoryPanel({
  securityId,
  symbol,
  currency,
  defaultRange = "1M",
  onRefresh,
  watchBackfill = false,
  emptyTitle = "No price history yet",
  emptyHint,
  compactEmpty = false,
}: {
  securityId: number;
  symbol?: string;
  currency?: string;
  /** Held securities open on 1M (docs/12 §Chart ranges). */
  defaultRange?: ChartRange;
  /** Shown as the empty state's action when the caller can refresh prices. */
  onRefresh?: () => void;
  /** Keep re-reading every 5s while this series is still empty (a security
   *  whose first backfill has not landed yet) — 3 minutes, same as the
   *  overview's own watch. */
  watchBackfill?: boolean;
  /** Copy for the honest "no data" state — never a fabricated line. */
  emptyTitle?: string;
  emptyHint?: string;
  /** Compact variant for a table cell (no preview image, no action). */
  compactEmpty?: boolean;
}) {
  const [range, setRange] = useState<ChartRange>(defaultRange);
  // Deadlined once per mount: the watch exists for a NEW security, so it
  // cannot outlive the panel it belongs to.
  const [pollUntil] = useState(() => (watchBackfill ? Date.now() + 3 * 60_000 : 0));
  const history = usePriceHistory(securityId, range, { pollUntil });
  const title = symbol ? `${symbol} price history` : "Price history";
  const header = <PriceHistoryHeader title={title} range={range} onChange={setRange} />;

  if (history.isPending) {
    return (
      <div className="space-y-2">
        {header}
        <SkeletonRows rows={3} />
      </div>
    );
  }

  if (history.isError) {
    return (
      <div className="space-y-2">
        {header}
        <QueryErrorPanel query={history} />
      </div>
    );
  }

  if (history.data.count === 0) {
    return (
      <div className="space-y-2">
        {header}
        <HistoryEmptyMessage
          compact={compactEmpty}
          title={emptyTitle}
          hint={
            emptyHint ??
            "This security has no cached closes yet — Refresh prices backfills up to a year of daily closes."
          }
          onRefresh={onRefresh}
          actionLabel={symbol ? `Refresh prices for ${symbol}` : "Refresh prices"}
        />
      </div>
    );
  }

  return (
    <PriceHistoryChart
      history={history.data.items}
      symbol={symbol}
      currency={currency}
      range={range}
      onRangeChange={setRange}
      granularity={history.data.granularity}
    />
  );
}

/** The house three-part empty state on a full card; inside a table cell the
 *  preview image and the action would be noise, so a sentence says the same
 *  thing honestly (docs/09 §Empty states). */
function HistoryEmptyMessage({
  compact,
  title,
  hint,
  onRefresh,
  actionLabel,
}: {
  compact: boolean;
  title: string;
  hint: string;
  onRefresh?: () => void;
  actionLabel: string;
}): ReactNode {
  // EmptyState's contract is icon + title + preview + ONE action — an empty
  // chart with no way forward is exactly the half-built state docs/09 bans.
  // Without a refresh hook there is no honest action, so say it in a sentence.
  if (!compact && onRefresh) {
    return (
      <EmptyState
        icon={<Inbox />}
        title={title}
        hint={hint}
        preview={{
          src: "/help/security-detail.png",
          alt: "A security page with price history and holdings",
        }}
        action={{
          label: actionLabel,
          onClick: onRefresh,
          ariaLabel: `${actionLabel} — ${title}`,
        }}
      />
    );
  }
  return (
    <p className="py-2 text-sm text-muted">
      {title} — {hint}
    </p>
  );
}
