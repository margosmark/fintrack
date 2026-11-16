import { useEffect, useRef, useState, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { restoreSession } from "../api/client";
import { createPriceStream, type PriceUpdate, type SecurityOverview } from "../api/securities";
import type { PositionsResponse } from "../api/portfolio";

export type StreamStatus = "connecting" | "connected" | "error" | "disconnected";

/** Backoff before rebuilding the stream after an error (docs/29 F5b). */
const RECONNECT_BACKOFF_MS = 3000;

/**
 * Hook to subscribe to cached price updates over SSE (docs/18 §Frontend).
 *
 * Updates the React Query cache for security details, the watchlist AND the
 * positions rows when cached price events arrive:
 *
 *   - the QUOTE fields of a positions row (`price`, `price_date`,
 *     `price_source`, `price_age_days`, `price_stale`) are patched client-side
 *     so a tick is visible immediately — that is L1 of M126;
 *   - the DERIVED valuation (`market_value_native`, `market_value_cad`,
 *     `book_cost_cad`) stays server-owned and only ever changes on a server
 *     re-read, which the same changed-payload gate below already triggers.
 *
 * M129 adds the security page's own tick: the `["security-overview", id]`
 * cache gets the same QUOTE patch, explicitly carrying the day-Δ baseline
 * (`previous_close` & friends) forward, plus a client-only `livePatchedAt`
 * marker. While that marker is present the page re-evaluates its price-derived
 * figures (day Δ, your-position impact) from server primitives and labels them
 * "live" (docs/09 §Day change); the changed-payload gate also invalidates
 * `["security-overview"]`, so the next server payload — baseline included —
 * replaces the patch and drops the marker.
 *
 * The patch also stamps `livePatchedAt` on the cached positions payload, which
 * is how a table can say "totals update on next refresh" honestly until the
 * server's own cadence replaces that object (docs/09 §Live prices).
 *
 * Returns connection status and the last message receipt time; message receipt
 * time is not quote freshness.
 */
export function usePriceStream(
  securityIds: number[],
  options: { privacyMode?: boolean } = {},
) {
  const queryClient = useQueryClient();
  const privacyMode = options.privacyMode === true;
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const queryClientRef = useRef(queryClient);
  const [status, setStatus] = useState<StreamStatus>("disconnected");
  const [lastEventAt, setLastEventAt] = useState<Date | null>(null);
  // Signature of the last payload seen, so a *changed* quote can trigger a
  // server re-read instead of an invalidation on every 5s tick.
  const lastPayloadRef = useRef<string>("");
  // Bumped to tear down and rebuild the EventSource after an error — the
  // rebuilt URL re-reads the current access token at creation time.
  const [reconnectKey, setReconnectKey] = useState(0);

  // Keep queryClient ref up to date without triggering re-renders
  queryClientRef.current = queryClient;

  // Serialize securityIds to prevent unnecessary effect re-runs. Copy before
  // sorting: Array.prototype.sort() mutates in place and would corrupt the
  // caller's prop array.
  const securityIdsKey = useMemo(() => [...securityIds].sort().join(","), [securityIds]);
  useEffect(() => {
    let disposed = false;
    lastPayloadRef.current = "";
    if (!securityIdsKey || privacyMode) {
      setStatus("disconnected");
      return () => {
        disposed = true;
      };
    }

    // Close existing connection if any
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    setStatus("connecting");
    const ids = securityIdsKey.split(",").map(Number);
    const eventSource = createPriceStream(ids);
    eventSourceRef.current = eventSource;

    eventSource.onopen = () => {
      if (!disposed && eventSourceRef.current === eventSource) setStatus("connected");
    };

    eventSource.onmessage = (event) => {
      try {
        if (disposed) return;
        // An EventSource callback cannot be synchronously interrupted by React
        // cleanup, but the latest effect owns this EventSource. Ignore a stale
        // connection's late event instead of mutating the current cache/status.
        if (eventSourceRef.current !== eventSource) return;
        const updates: PriceUpdate[] = JSON.parse(event.data);
        setLastEventAt(new Date());

        // The stream is a 5s cache poll that repeats an unchanged payload, so
        // an invalidation per tick would turn a read-only transport into a
        // positions refetch every 5s. Compare against the previous payload
        // instead: only a quote that actually moved (a background refresh
        // landed a new close) makes the server's positions/exposure stale.
        const signature = updates.map((u) => `${u.security_id}:${u.date}:${u.price}`).join("|");
        const previous = lastPayloadRef.current;
        lastPayloadRef.current = signature;

        // Update React Query cache for each security.
        updates.forEach((update) => {
          queryClientRef.current.setQueriesData<import("../api/watchlist").WatchlistResponse>(
            { queryKey: ["watchlist"] },
            (oldData) => {
              if (!oldData) return oldData;
              return {
                ...oldData,
                items: oldData.items.map((item) =>
                  item.security_id === update.security_id
                    ? {
                        ...item,
                        latest_price: update.price,
                        price_source: update.source,
                        price_age_days: update.age_days,
                        price_stale: update.stale,
                      }
                    : item,
                ),
              };
            },
          );
          // L1 (M126): patch the QUOTE on every cached positions row for this
          // security so the tick shows without waiting for the 60s cadence.
          // `setQueriesData` covers ["positions"] with any suffix (the
          // Investments page keys it ["positions", accountId]) — one patch
          // covers every surface rendering positions (docs/18).
          // Market value / book cost are deliberately NOT touched: those are
          // Decimal valuation the server owns (docs/09 §Live prices).
          queryClientRef.current.setQueriesData<PositionsResponse>(
            { queryKey: ["positions"] },
            (oldData) => {
              if (!oldData) return oldData;
              let changed = false;
              const items = oldData.items.map((item) => {
                if (item.security_id !== update.security_id) return item;
                if (
                  item.price === update.price &&
                  item.price_date === update.date &&
                  item.price_source === update.source &&
                  item.price_age_days === update.age_days &&
                  item.price_stale === update.stale
                ) {
                  return item;
                }
                changed = true;
                return {
                  ...item,
                  price: update.price,
                  price_date: update.date,
                  price_source: update.source,
                  price_age_days: update.age_days,
                  price_stale: update.stale,
                };
              });
              // Returning the SAME object when nothing moved keeps React Query
              // (and React) from re-rendering every positions table on a tick
              // that did not actually change a held row. When it DID move, the
              // marker rides the payload: the next server refetch replaces this
              // object outright and the marker goes with it — no timer, no
              // second clock, and the note cannot outlive its own truth.
              return changed ? { ...oldData, items, livePatchedAt: Date.now() } : oldData;
            },
          );

          queryClientRef.current.setQueryData(
            ["security-overview", update.security_id],
            (oldData: SecurityOverview | undefined) => {
              if (!oldData) return oldData;
              // M129: only a quote that actually moved stamps the marker (and
              // re-renders); an unchanged 5s repeat returns the same object.
              if (
                oldData.latest !== null &&
                oldData.latest.price === update.price &&
                oldData.latest.date === update.date &&
                oldData.latest.source === update.source &&
                oldData.latest.age_days === update.age_days &&
                oldData.latest.stale === update.stale
              ) {
                return oldData;
              }
              return {
                ...oldData,
                latest: {
                  // The M129 delta primitives ride the patch explicitly: a
                  // tick changes the quote, never the baseline it moves from
                  // (the server's next payload refreshes that too).
                  previous_close: oldData.latest?.previous_close ?? null,
                  day_change_native: oldData.latest?.day_change_native ?? null,
                  day_change_pct: oldData.latest?.day_change_pct ?? null,
                  price: update.price,
                  date: update.date,
                  source: update.source,
                  age_days: update.age_days,
                  stale: update.stale,
                },
                livePatchedAt: Date.now(),
              };
            }
          );
        });

        // Invalidation comes LAST and only on a real move: a cache WRITE is a
        // `success` state transition in React Query, and it clears
        // `isInvalidated` — patching after the invalidation would silently
        // cancel the very refetch this gate exists to schedule. The patch is
        // the fast path (the quote now), the invalidation is the slow path
        // (valuation, exposure, totals — recomputed server-side, docs/18).
        if (previous && previous !== signature) {
          void queryClientRef.current.invalidateQueries({ queryKey: ["positions"] });
          void queryClientRef.current.invalidateQueries({ queryKey: ["exposure"] });
          // M129 slow path: the moved quote also invalidates the security
          // page payload, so the server re-read refreshes the day-Δ baseline
          // (`previous_close` may itself have advanced to a new session) and
          // replaces every patched number — including the `livePatchedAt`
          // marker, whose lifetime is exactly this gap.
          void queryClientRef.current.invalidateQueries({ queryKey: ["security-overview"] });
        }
      } catch (error) {
        console.error("Failed to parse SSE message:", error);
      }
    };

    eventSource.onerror = (error) => {
      if (disposed || eventSourceRef.current !== eventSource) return;
      console.error("SSE connection error:", error);
      setStatus("error");
      // The browser's auto-reconnect replays the URL captured at creation —
      // including an access token that has since expired (docs/29 F5b).
      // Close instead, kick the client's refresh machinery (re-issues
      // POST /auth/refresh and stores a fresh access token when the cookie
      // is still valid), and rebuild from the current token after backoff.
      eventSource.close();
      eventSourceRef.current = null;
      // Rebuild only after token restoration settles; a fixed delay could
      // otherwise recreate the EventSource while the refresh is still using
      // the expired token. The backoff starts once refresh work completes.
      void restoreSession().then((session) => {
        if (disposed) return;
        if (!session || session.user.privacy_mode) {
          setStatus("disconnected");
          return;
        }
        reconnectTimerRef.current = window.setTimeout(
          () => setReconnectKey((key) => key + 1),
          RECONNECT_BACKOFF_MS,
        );
      });
    };

    return () => {
      disposed = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      eventSource.close();
      eventSourceRef.current = null;
      setStatus("disconnected");
    };
  }, [privacyMode, securityIdsKey, reconnectKey]);

  return { status, lastEventAt };
}
