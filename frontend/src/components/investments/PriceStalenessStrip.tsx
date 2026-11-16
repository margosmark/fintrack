// PriceStalenessStrip (M185, docs/47 G9/G10) — the Investments strip that
// states WHICH prices are old and WHAT a refresh actually did.
//
// G9 (per-holding as-of + source): every row already carries its own
// `price_date` / `price_source` / `price_age_days` / `price_stale`; this strip
// counts the stale ones and opens the per-row as-of list behind one control,
// so the number stays readable without a wall of badges.
//
// G10 (what a refresh revalued): `POST /portfolio/refresh` answers
// `{queued, skipped_dead}` — the strip reports BOTH counts ("N prices
// refreshed", "M symbols have no listing and were skipped"). No per-symbol
// list: it is not in the payload, and inventing one is forbidden (docs/47 §9).
//
// The real number always stays on the row; a stale price is a date problem,
// not a zero (docs/47 §9.2).
//
// Two labels, two truths (Option A, t_bd5ec95f): the as-of list states the
// SOURCE + MARKET DATE ("Market data · today" / "Market data · Oct 25" — from
// `price_date`, day-only) AND, when the row carries real provider-fetch
// evidence, the fetch age (" · fetched 12s ago" — from `price_fetched_at`
// only, formatted by `formatFetchAge`). `price_fetched_at` null/absent means
// NO evidence and the fetch segment is omitted entirely — never "0s", never
// "just now". Formatting `price_date` into hours/minutes would be
// fabrication; this component never does it.

import { useState } from "react";
import { Clock } from "lucide-react";
import { formatFetchAge, sourceLabel } from "../charts/format";
import { StateRow } from "../ui/StateRow";
import { DemoReason } from "../ui/DemoLock";

export interface StalenessRow {
  /** Display label for the as-of list — the row's own symbol/name. */
  label: string;
  price_date: string | null;
  price_source: string | null;
  price_age_days: number | null;
  /** REAL provider-fetch evidence (Option A); null/absent = no segment. */
  price_fetched_at?: string | null;
  price_stale: boolean | null;
}

export interface RefreshOutcome {
  queued: number;
  skipped_dead: number;
}

/**
 * The page's ONE demo write-lock verdict for `POST /portfolio/refresh`
 * (docs/47 §4, F-3): computed by the shared refresh mutation, never inside
 * this component, so the strip and the header button can only ever agree —
 * the same truth flows to both affordances and the same wording renders as
 * each one's visible reason.
 */
export interface RefreshDemoLock {
  locked: boolean;
  reason: string;
}

/** The strip's reason element id — one strip per page, so a literal is unique. */
export const STALE_STRIP_REASON_ID = "stale-strip-refresh-reason";

export function PriceStalenessStrip({
  rows,
  refreshOutcome,
  onRefresh,
  refreshing = false,
  lock,
}: {
  rows: readonly StalenessRow[];
  /** The last `POST /portfolio/refresh` answer this page received, if any. */
  refreshOutcome?: RefreshOutcome | null;
  /** Wired to the page's own refresh mutation (never a stub). */
  onRefresh?: () => void;
  refreshing?: boolean;
  /** The shared lock verdict; absent means the caller has no demo guard. */
  lock?: RefreshDemoLock;
}) {
  const [showAsOf, setShowAsOf] = useState(false);
  const staleRows = rows.filter((row) => row.price_stale === true);
  const hasOutcome = refreshOutcome !== null && refreshOutcome !== undefined;
  // Nothing stale and nothing to report → the strip does not render at all
  // (an empty box would lie about there being something to say).
  if (staleRows.length === 0 && !hasOutcome) {
    return null;
  }

  const detailParts: string[] = [];
  if (staleRows.length > 0) {
    detailParts.push(
      `${staleRows.length} holding${staleRows.length === 1 ? "" : "s"} last priced more than a week ago — the real number stays, with its as-of date`,
    );
  }
  if (refreshOutcome !== null && refreshOutcome !== undefined) {
    detailParts.push(
      `${refreshOutcome.queued} price${refreshOutcome.queued === 1 ? "" : "s"} refreshed` +
        (refreshOutcome.skipped_dead > 0
          ? ` · ${refreshOutcome.skipped_dead} symbol${refreshOutcome.skipped_dead === 1 ? "" : "s"} have no listing and were skipped`
          : ""),
    );
  }

  const locked = lock?.locked === true;
  return (
    <div className="space-y-2" data-testid="stale-strip">
      {staleRows.length > 0 && (
        <>
          <StateRow
            variant="warn"
            icon={<Clock className="size-4" aria-hidden />}
            title="Some prices are not fresh"
            detail={detailParts.join(" · ")}
            testId="stale-strip-warn"
            action={
              onRefresh !== undefined
                ? {
                    label: refreshing ? "Refreshing…" : "Refresh prices",
                    onClick: onRefresh,
                    testId: "stale-strip-refresh",
                    demoLock: locked ? { reasonId: STALE_STRIP_REASON_ID } : undefined,
                  }
                : undefined
            }
          />
          {/* docs/47 §4: the VISIBLE reason this affordance's padlock binds
              to — same panel, non-zero box, never sr-only (F-4). */}
          {locked && <DemoReason id={STALE_STRIP_REASON_ID}>{lock?.reason}</DemoReason>}
        </>
      )}
      {staleRows.length === 0 &&
        refreshOutcome !== null &&
        refreshOutcome !== undefined && (
          <StateRow
            variant="neutral"
            title="Refresh complete"
            detail={detailParts.join(" · ")}
            testId="stale-strip-outcome"
          />
        )}
      {staleRows.length > 0 && (
        <div>
          <button
            type="button"
            data-testid="stale-strip-asof-toggle"
            aria-expanded={showAsOf}
            aria-controls="stale-strip-asof"
            onClick={() => setShowAsOf((open) => !open)}
            className="text-xs text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
          >
            {showAsOf ? "Hide as-of times" : "Show as-of times"}
          </button>
          {showAsOf && (
            <ul
              id="stale-strip-asof"
              className="mt-2 space-y-1 rounded-lg border border-rule bg-surface-raised p-3 text-xs text-muted"
            >
              {staleRows.map((row) => {
                // Two labels, two truths (Option A, t_bd5ec95f): the market
                // date is day-only truth; the fetch age exists ONLY when the
                // row carries real provider-fetch evidence.
                const fetchAge = formatFetchAge(row.price_fetched_at);
                return (
                  <li
                    key={row.label}
                    className="flex items-baseline justify-between gap-3"
                  >
                    <span className="min-w-0 truncate text-text">
                      {row.label}
                    </span>
                    <span className="shrink-0 tabular">
                      {row.price_source !== null && row.price_source !== ""
                        ? sourceLabel(row.price_source)
                        : "Price"}
                      {row.price_date === null
                        ? " · no price date"
                        : row.price_age_days === 0
                          ? " · today"
                          : ` · ${new Date(`${row.price_date}T00:00:00`).toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" })}`}
                      {fetchAge !== null ? ` · fetched ${fetchAge} ago` : ""}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
