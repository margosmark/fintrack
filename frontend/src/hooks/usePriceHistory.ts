import { useQuery } from "@tanstack/react-query";

import { getPriceHistory } from "../api/prices";
import { granularityForRange, rangeWindow, type ChartRange } from "../lib/chartRange";

/**
 * Closes for one security over a chart range (M126, docs/08 §Price history).
 *
 * ONE fetch per (security, range) — the sparkline on a positions row comes
 * from the positions payload itself, so rendering the table costs nothing
 * extra; only an explicit expansion (or a range change) lands here, and the
 * result is cached by that key so collapsing and re-expanding is free.
 *
 * `granularity` rides the key because the SERVER buckets the series: asking
 * for a different granularity is a different payload, not a client-side view.
 *
 * `pollUntil` is the security page's backfill watch, mirroring the overview's
 * own `cached_days === 0` poll: while THIS series is still empty and the
 * deadline has not passed, re-read every 5s, so a just-added security's chart
 * fills in on its own instead of sitting on "No price history yet". It stops
 * the moment there is data, and it never runs when the caller does not ask
 * for it (a positions row expands once and tells the truth).
 */
export function usePriceHistory(
  securityId: number,
  range: ChartRange,
  options: { enabled?: boolean; pollUntil?: number } = {},
) {
  const span = rangeWindow(range);
  const granularity = granularityForRange(range);
  const enabled = options.enabled !== false;
  const pollUntil = options.pollUntil ?? 0;
  return useQuery({
    queryKey: ["price-history", securityId, span.from ?? "", span.to, granularity],
    queryFn: () =>
      getPriceHistory({
        securityId,
        from: span.from,
        to: span.to,
        granularity,
      }),
    enabled: enabled && Number.isFinite(securityId),
    staleTime: 60_000,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data !== undefined && data.count > 0) return false;
      return Date.now() < pollUntil ? 5000 : false;
    },
  });
}
