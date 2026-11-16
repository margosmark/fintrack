import { Fragment, lazy, Suspense, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { listAccounts, useInvestmentAccounts } from "../api/accounts";
import type { ValueHistoryEvent } from "../api/accounts";
import { ApiError } from "../api/client";
import {
  getBreakdown,
  getExposure,
  getPositions,
  refreshPrices,
} from "../api/portfolio";
import type { BreakdownDimension, Position, PositionsResponse } from "../api/portfolio";
import {
  CASH_ONLY_ACTIONS,
  COMMISSION_ACTIONS,
  TRADE_ACTIONS,
  createTrade,
  getDividendSuggestions,
  listTrades,
  recordDividend,
} from "../api/trades";
import type {
  DividendSuggestion,
  Trade,
  TradeAction,
} from "../api/trades";
import {
  candidateListing,
  checkQuote,
  createSecurity,
  listSecurities,
  listingLabel,
  lookupSecurity,
  resolvedListing,
} from "../api/securities";
import type {
  SecurityInput,
  SecurityLookupCandidate,
  SecurityLookupResult,
} from "../api/securities";
import type { QuoteCheck } from "../api/securities";
import {
  addWatchlistEntry,
  getWatchlist,
  removeWatchlistEntry,
} from "../api/watchlist";
import type { WatchlistItem } from "../api/watchlist";
import { ExposureDonut } from "../components/charts/ExposureDonut";
import {
  DIMENSION_PLURAL,
  FOLD_N_BY_DIMENSION,
  OTHER_KEY,
  foldRows,
  isOtherBucket,
  setExpandedDimension,
  useExpandedDimension,
} from "../lib/exposureFold";
import { PriceHistoryPanel } from "../components/charts/PriceHistoryPanel";
import { BenchmarkPanel } from "../components/investments/BenchmarkPanel";
import { PortfolioHeaderCard } from "../components/investments/PortfolioHeaderCard";
import { TotalReturnMetrics } from "../components/investments/TotalReturnMetrics";
import { CategoriesDonut } from "../components/charts/CategoriesDonut";
import { SecurityPicker } from "../components/investments/SecurityPicker";
import { CandidatePicker, SuggestionChip } from "../components/investments/SymbolSuggestion";
import { fmtCad, fmtNative, fmtQuantity, formatFetchAge, sourceLabel } from "../components/charts/format";
import { ExposureDrillPanel } from "../components/ExposureDrillPanel";
import { provenanceFromHistory } from "../api/dividendProvenance";
import { DividendProvenanceStrip } from "../components/reports/DividendProvenanceStrip";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Sparkline } from "../components/ui/charts/Sparkline";
import { HelpHint } from "../components/ui/HelpHint";
import { useToast } from "../components/ui/Toast";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import type { SegmentedOption } from "../components/ui/SegmentedControl";
import { usePriceStream } from "../hooks/usePriceStream";
import { fmtCadSigned, fmtSignedPct, fmtSignedPctBookCost } from "../lib/impact";
import { groupPositionsBySecurity } from "../lib/positionGroups";
import { etToday } from "../lib/budgets";
import type { PositionGroup } from "../lib/positionGroups";
import { useAuth } from "../auth/AuthContext";
import { useOptionalUser } from "../auth/authUser";
import { scopedKey } from "../lib/tenantStorage";
import { SSEStatusIndicator } from "../components/ui/SSEStatusIndicator";
import {
  ServerSideBlocked,
  useServerSideWriteBlocked,
} from "../components/e2ee/ServerSideBlocked";
import { PriceStalenessStrip } from "../components/investments/PriceStalenessStrip";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  useDemoRefusal,
  useDemoWriteLock,
} from "../components/ui/DemoLock";
import { MASK_TEXT, isMasked, useMasked } from "../lib/mask";
import { usePublishPageMeta } from "../lib/pageMeta";
/**
 * Investments page (docs/09): positions table with staleness badges and a
 * trend column (sparkline slot), currency exposure view with a center-labeled
 * donut, searchable security picker with an inline new-security fallback,
 * investment-account gating, price refresh, a watchlist for securities not
 * owned, signed-quantity trade entry and a paginated trades log.
 */

const PAGE_SIZE = 25;

// The window both the benchmark comparison and the M77 period-return metrics
// describe — one pair of constants so the two cards can never drift apart
// (docs/05 §Total return: "on par with the benchmark comparison").
const BENCHMARK_FROM = "2026-01-01";
const BENCHMARK_TO = "2026-09-01";

/** M95 P2 — the empty state's "Record a trade" must land ON the form that
 *  fills it. The trade form is rendered on THIS page (below the empty panel),
 *  so a `to: "/investments"` link would be a route no-op: scroll to the form
 *  and hand it focus instead. Instant scroll — no smooth behaviour, so the
 *  reduced-motion contract has nothing to collapse. */
function focusTradeForm(): void {
  const form = document.getElementById("trade-form");
  form?.scrollIntoView({ block: "start" });
  form?.querySelector<HTMLElement>("input, select")?.focus();
}

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors disabled:cursor-not-allowed disabled:opacity-40 hover:border-accent/70";


export function Investments({
  accountId,
  showHeader = true,
  focusTrade = null,
}: {
  accountId?: number;
  showHeader?: boolean;
  /** M118: a value-chart marker that came from a TRADE row lands in the log. */
  focusTrade?: ValueHistoryEvent | null;
}) {
  const { user } = useAuth();
  const watchlist = useQuery({
    queryKey: ["watchlist"],
    queryFn: getWatchlist,
    refetchInterval: 60000,
  });
  const positions = useQuery({
    queryKey: ["positions", accountId],
    queryFn: () => getPositions(accountId),
    refetchInterval: 60000,
  });

  const securityIds = useMemo(
    () => [...new Set([
      ...(positions.data?.items ?? []).map((p) => p.security_id),
      ...(watchlist.data?.items ?? []).map((w) => w.security_id),
    ])],
    [positions.data, watchlist.data],
  );

  const { status: streamStatus, lastEventAt } = usePriceStream(securityIds, {
    privacyMode: user?.privacy_mode === true,
  });

  // m61 #7 / M65: any row priced before today (price_age_days > 0 — the
  // day-granular proxy for age > 24h) must not sit next to a neutral green
  // "Stream connected".
  const pricesStale = [...(positions.data?.items ?? []), ...(watchlist.data?.items ?? [])].some(
    (row) => (row.price_age_days ?? 0) > 0,
  );

  // M185 (docs/47 §1.3): the truth bar's prices segment is the NEWEST
  // price_date this payload carries, with the payload's own source — never a
  // clock reading, never a vendor name outside sourceLabel.
  const priceRows = positions.data?.items ?? [];
  const newestPriceDate = priceRows.reduce<string | null>(
    (newest, row) =>
      row.price_date !== null && (newest === null || row.price_date > newest)
        ? row.price_date
        : newest,
    null,
  );
  const newestPriceSource =
    priceRows.find((row) => row.price_date === newestPriceDate && row.price_source !== null)
      ?.price_source ?? null;
  usePublishPageMeta({
    prices:
      newestPriceDate === null
        ? { price_date: null, price_source: null }
        : { price_date: newestPriceDate, price_source: newestPriceSource },
  });

  // M185 G10: ONE refresh mutation for the header button AND the staleness
  // strip — one click, one `{queued, skipped_dead}` truth (a second instance
  // would make the strip report a click it never saw).
  const refreshPrices = useRefreshPrices();

  return (
    <section className="space-y-8">
      {/* docs/47 §4 + docs/09 §Read-only sessions #3: ONE refusal element per
          page, fed by the page's ONE refresh mutation (M185 G10). It lives at
          the page root — not inside the header button — so it renders on EVERY
          route this component mounts on, including /accounts/{id} where
          `showHeader={false}` removes the button entirely (F-3). */}
      {refreshPrices.demo.refusal !== null && (
        <DemoRefusal id={refreshPrices.demo.refusalId} action={refreshPrices.demo.refusal} />
      )}
      {showHeader && (
        <div className="flex flex-wrap items-center justify-between gap-4">
          <h1 className="text-xl font-semibold tracking-tight">Investments</h1>
          <div className="flex items-center gap-3">
            <SSEStatusIndicator status={streamStatus} lastEventAt={lastEventAt} pricesStale={pricesStale} />
            <RefreshPricesButton refresh={refreshPrices} />
          </div>
        </div>
      )}
      {/* M130 (docs/09 §Portfolio header): the whole-portfolio headline comes
          FIRST — value, day change and the overall chart — and it only exists
          on the portfolio view; an account-scoped page has its own timeline. */}
      {accountId === undefined && <PortfolioHeaderCard positions={positions} />}
      <PositionsSection positions={positions} showAccount={!accountId} refresh={refreshPrices} />
      <ExposureSection accountId={accountId} />
      <TotalReturnMetrics from={BENCHMARK_FROM} to={BENCHMARK_TO} />
      <BenchmarkPanel startDate={BENCHMARK_FROM} endDate={BENCHMARK_TO} />
      <DividendSuggestionsSection accountId={accountId} />
      <div className="grid items-start gap-6 xl:grid-cols-2">
        <WatchlistSection />
        <TradeForm accountId={accountId} />
      </div>
      <TradesLog accountId={accountId} focus={focusTrade} />
    </section>
  );
}

function PositionsSection({
  positions,
  showAccount = true,
  refresh,
}: {
  positions: UseQueryResult<PositionsResponse>;
  showAccount?: boolean;
  /** The page's ONE refresh mutation (M185 G10) — shared with the header. */
  refresh: ReturnType<typeof useRefreshPrices>;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // M146 (docs/09 §Positions by security): the lens at the top of the card.
  // "By account" is the payload's own rows, one per account+security pair;
  // "By security" folds the SAME rows — a pure client-side roll-up over data
  // the page already holds, so switching views never refetches. localStorage
  // ONLY: this is a lens on one table, not a stored preference (unlike the
  // dashboard preset, which the server keeps) — and it is keyed per tenant
  // (F-6): two tenants sharing a browser must not inherit each other's lens.
  const viewKey = scopedKey(POSITIONS_VIEW_KEY, useOptionalUser());
  const [view, setView] = useState<PositionsView>(() => _loadView(viewKey));
  // One account panel open at a time — the split-rows rule (docs/44 §6.3).
  const [expandedSecurity, setExpandedSecurity] = useState<number | null>(null);
  const changeView = (next: PositionsView) => {
    _saveView(viewKey, next);
    setView(next);
    setExpandedSecurity(null);
  };
  // The grouped view is a PORTFOLIO view only: an account-scoped page holds
  // one account's rows, where every group would be a single row (the same
  // `showAccount` gate the account column itself uses).
  const grouped = showAccount && view === "security";

  if (positions.isPending) {
    return (
      <Card className="p-4">
        <CardTitle>Positions</CardTitle>
        <SkeletonRows rows={5} />
      </Card>
    );
  }
  if (positions.isError) {
    return (
      <Card className="p-4">
        <CardTitle>Positions</CardTitle>
        <p className="text-sm text-danger">{positions.error.message}</p>
      </Card>
    );
  }

  const items = positions.data.items;
  if (items.length === 0) {
    return (
      <Card className="p-4">
        <CardTitle>Positions</CardTitle>
        <EmptyState
          icon={<span className="font-mono text-xs">0x</span>}
          title="No open positions yet"
          hint="Record your first trade below and it will appear here."
          preview={{
            src: "/help/investments.png",
            alt: "The Investments page with holdings and totals",
          }}
          action={{
            label: "Record a trade",
            onClick: focusTradeForm,
            ariaLabel: "Record a trade — No open positions yet",
          }}
        />
      </Card>
    );
  }

  // M126 L1 (docs/09 §Live prices): the stream patches the QUOTE instantly,
  // the server owns every derived figure. `livePatchedAt` is stamped on the
  // cached payload by that patch and is dropped by the next SERVER payload,
  // so "the quote is ahead of the totals" is a fact about the cache — not a
  // timer, and not something to remember or clear.
  const totalsBehindLive = positions.data.livePatchedAt !== undefined;

  // M146: the fold itself — pure, over the rows this payload already carries
  // (docs/09 §Positions by security). `groups` is EMPTY in the account view,
  // so nothing about the flat path changes shape.
  const groups = grouped ? groupPositionsBySecurity(items) : [];

  return (
    <Card hover className="p-4">
      <CardTitle
        right={
          showAccount ? (
            <SegmentedControl
              options={POSITION_VIEWS}
              value={view}
              onChange={changeView}
              layoutId="positions-view"
              size="sm"
              ariaLabel="Positions view"
            />
          ) : undefined
        }
      >
        Positions
        <HelpHint
          className="ml-1.5"
          text="Each price, source and age ticks in live from the price stream. Market value, day change, gains and the page's totals are computed server-side from those prices and refresh on the normal cadence — within 60s, or straight away on Refresh."
        />
      </CardTitle>
      {/* M185 G9/G10 (docs/47): stale count + as-of list + what the last
          refresh actually revalued — the rows this table draws are the ones
          the strip is talking about. */}
      <StalenessStrip positions={positions} refresh={refresh} />
      <div role="region" aria-label="Positions table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              {showAccount && <th className="px-3 py-2 font-medium">Account</th>}
              <th className="px-3 py-2 font-medium">Symbol</th>
              <th className="px-3 py-2 text-right font-medium">Quantity</th>
              <th className="px-3 py-2 text-right font-medium">Book cost (CAD)</th>
              <th className="px-3 py-2 text-right font-medium">Price</th>
              <th className="px-3 py-2 font-medium">Trend</th>
              {/* M129: the delta columns are the user-facing priority — below
                  sm they take the room the source/age diagnostics give up
                  (docs/09 §Day change; the diagnostics fold into the gain
                  cell as a stacked percent). */}
              <th className="px-3 py-2 text-right font-medium">Day Δ</th>
              <th className="hidden px-3 py-2 font-medium sm:table-cell">Source · age</th>
              <th className="px-3 py-2 text-right font-medium">Market value</th>
              <th className="px-3 py-2 text-right font-medium">Market value (CAD)</th>
              <th className="px-3 py-2 text-right font-medium">Your gain (CAD)</th>
              <th className="hidden px-3 py-2 text-right font-medium sm:table-cell">
                Your gain %
              </th>
            </tr>
          </thead>
          {/* M146: the two views share the same columns, the same Σ row and
              the same rows — only the FOLD differs. The grouped branch emits
              `<tr>` + a second `<tbody>` per group (both legal table
              children), so the per-account sub-rows sit on the SAME columns
              as the group above them instead of re-flowing inside a nested
              table. */}
          {grouped ? (
            groups.map((group) =>
              group.account_count === 1 ? (
                // Single-account security: FLAT, no chevron — there is nothing
                // to reveal (docs/09 §Positions by security).
                <PositionRow
                  key={`sec-${group.security_id}`}
                  position={group.rows[0]}
                  showAccount
                />
              ) : (
                <Fragment key={`sec-${group.security_id}`}>
                  <PositionGroupRow
                    group={group}
                    expanded={expandedSecurity === group.security_id}
                    onToggle={() =>
                      setExpandedSecurity((open) =>
                        open === group.security_id ? null : group.security_id,
                      )
                    }
                  />
                  <PositionGroupAccounts
                    group={group}
                    expanded={expandedSecurity === group.security_id}
                  />
                </Fragment>
              ),
            )
          ) : (
            <tbody>
              {items.map((position) => (
                <PositionRow
                  key={`${position.account_id}-${position.security_id}`}
                  position={position}
                  showAccount={showAccount}
                />
              ))}
            </tbody>
          )}
          {/* M129/M130: the Σ row — the SERVER's own totals, so the footer can
              never disagree with the rows above it. `total_unrealized_cad` is
              the sum of the per-row gains (unknown-book rows excluded),
              `total_value_cad` is the sum of this column, and M130 adds the
              portfolio day Δ — the same Σ the header card at the top of the
              page renders, which is why the two can be asserted equal. The
              native "Market value" and quantity totals stay empty on purpose:
              the payload computes no cross-currency total and no share count,
              and inventing one client-side is exactly what the
              anti-fabrication rule forbids. */}
          <tfoot>
            <tr
              className="border-t border-veil/[0.08] font-medium"
              data-testid="positions-total-row"
            >
              {showAccount && <td className="px-3 py-2">Total</td>}
              <td className="px-3 py-2">{showAccount ? null : "Total"}</td>
              <td className="px-3 py-2" />
              <td className="px-3 py-2" />
              <td className="px-3 py-2" />
              <td className="px-3 py-2" />
              {/* M130: the portfolio day Δ — the SAME Σ the header card at
                  the top of the page shows, as the percent this column is in. */}
              <td
                className={`px-3 py-2 text-right tabular ${
                  positions.data.total_day_change_cad === null ||
                  positions.data.total_day_change_cad === undefined
                    ? "text-muted"
                    : Number(positions.data.total_day_change_cad) < 0
                      ? "text-danger"
                      : "text-gain"
                }`}
              >
                <span className="sr-only">Total day change: </span>
                {positions.data.total_day_change_pct === null ||
                positions.data.total_day_change_pct === undefined
                  ? "—"
                  : fmtSignedPct(positions.data.total_day_change_pct)}
              </td>
              <td className="hidden px-3 py-2 sm:table-cell" />
              <td className="px-3 py-2" />
              {/* The CAD column's own Σ — the header card's holdings basis,
                  verbatim from the payload (docs/08 §Portfolio summary). */}
              <td className="px-3 py-2 text-right tabular">
                <span className="sr-only">Total market value (CAD): </span>
                {fmtCad(positions.data.total_value_cad)}
              </td>
              <td
                className={`px-3 py-2 text-right tabular ${
                  positions.data.total_unrealized_cad === null
                    ? "text-muted"
                    : Number(positions.data.total_unrealized_cad) < 0
                      ? "text-danger"
                      : "text-gain"
                }`}
              >
                <span className="sr-only">Total unrealized (CAD): </span>
                {positions.data.total_unrealized_cad === null
                  ? "—"
                  : fmtCadSigned(Number(positions.data.total_unrealized_cad))}
              </td>
              <td className="hidden px-3 py-2 text-right tabular text-muted sm:table-cell">—</td>
            </tr>
          </tfoot>
        </table>
      </div>
      {totalsBehindLive && (
        <p className="mt-2 text-[11px] leading-snug text-muted" data-testid="positions-live-note">
          Prices above are live from the stream; market value, day change, gains and totals
          update on next refresh (≤60s or on Refresh).
        </p>
      )}
    </Card>
  );
}

/**
 * One holding: the row itself, plus — when expanded — the same
 * PriceHistoryChart the security page draws, inline (M126 L2, docs/09
 * §Expanded rows). No navigation, no extra request until it is opened.
 *
 * The row is interactive (click anywhere toggles) and the chevron is the
 * keyboard path: `aria-expanded` + `aria-controls` on a real button, so a
 * screen-reader user hears the state and Tab reaches it.
 */
function PositionRow({
  position,
  showAccount = true,
  accountLink = false,
  subrow = false,
}: {
  position: Position;
  showAccount?: boolean;
  /** M146: an expanded group's per-account rows link the account NAME to its
   *  detail page — the one place a position row offers navigation by
   *  account. Flat rows keep the plain text they have always rendered. */
  accountLink?: boolean;
  /** M146: this row is a GROUP's sub-row — same columns, a veil tint and a
   *  left indent so it reads as belonging to the group above it. */
  subrow?: boolean;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const stale = position.price_stale;
  // Two labels, two truths (Option A, t_bd5ec95f): market-date day-only,
  // fetch age ONLY from real provider-fetch evidence (null → no segment).
  const fetchAge = formatFetchAge(position.price_fetched_at);
  const age =
    position.price_date === null ? null : (
      <Badge tone={stale === true ? "danger" : "neutral"} pulse={stale === true}>
        {position.price_source ?? "?"} · {position.price_age_days === 0 ? "today" : position.price_date}
        {fetchAge !== null ? ` · fetched ${fetchAge} ago` : ""}
        {stale ? " (stale)" : ""}
      </Badge>
    );

  const [expanded, setExpanded] = useState(false);
  const [ticked, setTicked] = useState(false);
  // The quote that was on screen last. Comparing against it (rather than
  // "did the stream fire") is what keeps a tick on ONE row: a payload that
  // only moved a different security must not flash this cell.
  const lastPrice = useRef(position.price);

  useEffect(() => {
    if (lastPrice.current === position.price) return;
    lastPrice.current = position.price;
    setTicked(true);
    const id = window.setTimeout(() => setTicked(false), 1500);
    return () => window.clearTimeout(id);
  }, [position.price]);

  // 12 columns at >=sm (11 below it, where the diagnostics pair is hidden):
  // the colSpan only ever needs the CEILING — a span wider than the visible
  // cells still covers the row.
  const cellCount = (showAccount ? 1 : 0) + 11;
  const panelId = `position-history-${position.account_id}-${position.security_id}`;
  const toggle = () => setExpanded((open) => !open);

  return (
    <>
      {/* Interactive row: click anywhere expands it. The symbol link and the
          chevron stop propagation so navigating/still toggling stay distinct. */}
      <tr
        onClick={toggle}
        className={`cursor-pointer border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04] ${
          expanded ? "bg-veil/[0.04]" : ""
        } ${subrow ? "bg-veil/[0.02]" : ""}`}
      >
        {showAccount && (
          <td className={`px-3 py-2 ${subrow ? "pl-7" : ""}`}>
            {accountLink ? (
              <Link
                to={`/accounts/${position.account_id}`}
                className="text-accent hover:underline"
                onClick={(event) => event.stopPropagation()}
              >
                {position.account_name}
              </Link>
            ) : (
              position.account_name
            )}
          </td>
        )}
        <td className="px-3 py-2 font-medium">
          <Link
            to={`/securities/${position.security_id}`}
            className="text-accent hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {position.symbol}
          </Link>
        </td>
        <td className="px-3 py-2 text-right tabular">{fmtQuantity(position.quantity)}</td>
        {/* w12 pin 4: book cost between quantity and price; em dash when null. */}
        <td className="px-3 py-2 text-right tabular">{fmtCad(position.book_cost_cad)}</td>
        {/* M126 L1: the patched cell carries the pulse; it is a background
            sweep only, so it never competes with the SSE chip, which stays the
            single source of truth about CONNECTION state. */}
        <td
          className={`px-3 py-2 text-right tabular ${ticked ? "price-live-tick" : ""}`}
          data-live-tick={ticked ? "true" : undefined}
        >
          {fmtNative(position.price, position.listing_currency)}
        </td>
        {/* w12 pin 5: trend sparkline from the security's last <=30 closes —
            it rides the positions payload, so rendering costs NO fetch. */}
        <td className="px-3 py-2">
          <div className="flex items-center gap-1">
            {position.spark.length >= 2 ? (
              <Sparkline values={position.spark.map(Number)} width={60} height={24} />
            ) : (
              <span className="text-muted">—</span>
            )}
            <button
              type="button"
              aria-expanded={expanded}
              /* Only reference the panel while it exists: an `aria-controls`
                 idref with no element behind it is an invalid ARIA value to
                 assistive tech (and to axe's `aria-valid-attr-value`), and a
                 collapsed row has nothing to control yet. */
              aria-controls={expanded ? panelId : undefined}
              aria-label={
                expanded
                  ? `Hide ${position.symbol} price history`
                  : `Show ${position.symbol} price history`
              }
              onClick={(event) => {
                event.stopPropagation();
                toggle();
              }}
              className="cursor-pointer rounded p-1 text-muted outline-none transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
            >
              {expanded ? (
                <ChevronDown className="size-3.5" aria-hidden />
              ) : (
                <ChevronRight className="size-3.5" aria-hidden />
              )}
            </button>
          </div>
        </td>
        {/* M129: the day's move vs the previous close — server-computed like
            every derived figure, so it rides the server cadence (the footnote
            states the gap while a tick is ahead of it). */}
        <td
          className={`px-3 py-2 text-right tabular ${
            position.day_change_pct === null
              ? "text-muted"
              : position.day_change_pct < 0
                ? "text-danger"
                : position.day_change_pct > 0
                  ? "text-gain"
                  : ""
          }`}
        >
          {position.day_change_pct === null ? "—" : fmtSignedPct(position.day_change_pct)}
        </td>
        <td className="hidden px-3 py-2 sm:table-cell">
          {age ?? <span className="text-xs text-muted">no price — enter one manually</span>}
        </td>
        <td className="px-3 py-2 text-right tabular">
          {fmtNative(position.market_value_native, position.listing_currency)}
        </td>
        {/* Server-owned valuation (docs/09): the live stream patches the
            price above and never this cell — it changes on a server re-read. */}
        <td className="px-3 py-2 text-right tabular">{fmtCad(position.market_value_cad)}</td>
        {/* M129 own-impact columns: the SERVER's unrealized figures — the row
            renders the wire, never arithmetic of its own. Below sm the hidden
            percent column folds in under the amount. */}
        <td
          className={`px-3 py-2 text-right tabular ${
            position.unrealized_gain_cad === null
              ? "text-muted"
              : Number(position.unrealized_gain_cad) < 0
                ? "text-danger"
                : "text-gain"
          }`}
        >
          {position.unrealized_gain_cad === null ? (
            "—"
          ) : (
            <>
              {fmtCadSigned(Number(position.unrealized_gain_cad))}
              {position.unrealized_gain_pct !== null && (
                <span className="block text-xs text-muted sm:hidden">
                  {/* M139 carve-out: position-level "Your gain %" is derived
                      from book cost, so unlike every other percentage it
                      FOLLOWS the mask — %.4.63 beside a masked market value
                      hands a shoulder-surfer the scale. */}
                  {fmtSignedPctBookCost(position.unrealized_gain_pct)}
                </span>
              )}
            </>
          )}
        </td>
        <td
          className={`hidden px-3 py-2 text-right tabular sm:table-cell ${
            position.unrealized_gain_pct === null
              ? "text-muted"
              : position.unrealized_gain_pct < 0
                ? "text-danger"
                : "text-gain"
          }`}
        >
          {position.unrealized_gain_pct === null
            ? "—"
            : fmtSignedPctBookCost(position.unrealized_gain_pct)}
        </td>
      </tr>
      {expanded && (
        <tr data-testid="position-history-row">
          {/* The cell spans the whole (horizontally SCROLLABLE) table, so at
              390/412 its own width is ~1400px and a right-aligned control
              inside it would sit off-screen — the user would have to scroll
              the table sideways to reach a range picker, which is exactly
              what docs/09 §Range control forbids. Cap the panel to the
              viewport: the header then lays out (title left, control right)
              inside what is actually visible, while the table columns keep
              their own scroll region.
              Measured on the deployed stack at 390/412: the scroll region
              starts 37px in, the cell padding adds 12px, and 37px remain on
              the right — 86px of chrome total. 6rem = 96px leaves ~10px of
              slack at both widths, and at >=768 it stops binding as soon as
              the table itself fits (the cell is then narrower than the cap). */}
          <td id={panelId} colSpan={cellCount} className="px-3 py-3">
            <div className="max-w-[calc(100vw-6rem)]">
            <PriceHistoryPanel
              securityId={position.security_id}
              symbol={position.symbol}
              currency={position.listing_currency}
              compactEmpty
              emptyHint="No cached closes for this holding yet — Refresh prices backfills up to a year of daily closes."
            />
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// --- M146 — the by-security roll-up (docs/09 §Positions by security) ----------

/** The lens at the top of the Positions card. */
type PositionsView = "account" | "security";

const POSITIONS_VIEW_KEY = "fintrack:positions_view";

const POSITION_VIEWS: readonly SegmentedOption<PositionsView>[] = [
  { value: "account", label: "By account" },
  { value: "security", label: "By security" },
];

/** localStorage ONLY (docs/09 §Positions by security): the lens is a view
 *  over ONE table, so it never becomes a server-stored preference the way the
 *  dashboard preset does. The key is tenant-scoped (F-6) — see
 *  `lib/tenantStorage.ts`. A missing or corrupt value falls back to the
 *  account view — the payload's own order, which is what the page has always
 *  shown. */
function _loadView(key: string): PositionsView {
  try {
    return localStorage.getItem(key) === "security" ? "security" : "account";
  } catch {
    return "account";
  }
}

function _saveView(key: string, view: PositionsView): void {
  try {
    localStorage.setItem(key, view);
  } catch {
    /* private mode / quota: the lens simply will not persist */
  }
}

/** The duplicated-holding signal on a group row — the split rows' badge shape
 *  (docs/44 §6.3), so the two disclosures look like the same idea. */
function AccountsChip({ count }: { count: number }): ReactNode {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
      {count} accounts
    </span>
  );
}

/**
 * One SECURITY's roll-up row (M146): the same 12 columns as a position row,
 * filled from the group's aggregates. The chevron discloses the per-account
 * rows that live in the sibling `<tbody>` — the split-rows disclosure
 * (docs/44 §6.3): one focusable button, `aria-expanded` + `aria-controls`,
 * and the count in its accessible name. A single-account security never gets
 * this component: it renders as an ordinary {@link PositionRow}.
 *
 * The group's percents are the value-weighted fold in `lib/positionGroups`
 * (docs/09) — never an average of the rows' percents — and every amount is a
 * sum of the SERVER's own per-row figures, so no valuation happens here.
 */
function PositionGroupRow({
  group,
  expanded,
  onToggle,
}: {
  group: PositionGroup;
  expanded: boolean;
  onToggle: () => void;
}) {
  // M140: re-render on a mask flip (docs/46 §D6).
  useMasked();

  const quote = group.quote;
  // Two labels, two truths (Option A, t_bd5ec95f): the fetch segment exists
  // only with real provider-fetch evidence.
  const quoteFetchAge = formatFetchAge(quote.price_fetched_at);
  const age =
    quote.price_age_days === null ? null : (
      <Badge tone={quote.price_stale === true ? "danger" : "neutral"} pulse={quote.price_stale === true}>
        {quote.price_source ?? "?"} · {quote.price_age_days}d
        {quoteFetchAge !== null ? ` · fetched ${quoteFetchAge} ago` : ""}
        {quote.price_stale ? " (stale)" : ""}
      </Badge>
    );
  const panelId = `position-group-accounts-${group.security_id}`;
  const headerId = `position-group-header-${group.security_id}`;

  return (
    <tr
      onClick={onToggle}
      data-testid="position-group-row"
      className={`cursor-pointer border-b border-veil/[0.05] bg-veil/[0.03] transition-colors hover:bg-veil/[0.05] ${
        expanded ? "bg-veil/[0.05]" : ""
      }`}
    >
      <td className="px-3 py-2">
        <AccountsChip count={group.account_count} />
      </td>
      <td className="px-3 py-2 font-medium">
        <Link
          to={`/securities/${group.security_id}`}
          className="text-accent hover:underline"
          onClick={(event) => event.stopPropagation()}
        >
          {group.symbol}
        </Link>
      </td>
      <td className="px-3 py-2 text-right tabular">{fmtQuantity(group.quantity)}</td>
      {/* Display-only book cost — the sum of each account's OWN cost basis,
          never a pooled ACB (docs/05 §ACB; grouping feeds no tax math). */}
      <td className="px-3 py-2 text-right tabular">{fmtCad(group.book_cost_cad)}</td>
      {/* The security's single cached quote: no live-tick sweep here (a
          sub-row carries it) — this cell is a label for the group, and the
          stream still moves it because it reads the same payload field. */}
      <td className="px-3 py-2 text-right tabular">
        {fmtNative(quote.price, group.listing_currency)}
      </td>
      {/* ONE sparkline per group: the per-account sparks are the same closes
          drawn N times (docs/09). */}
      <td className="px-3 py-2">
        <div className="flex items-center gap-1">
          {group.spark.length >= 2 ? (
            <Sparkline values={group.spark.map(Number)} width={60} height={24} />
          ) : (
            <span className="text-muted">—</span>
          )}
          <button
            type="button"
            id={headerId}
            aria-expanded={expanded}
            /* Only reference the panel while it exists — the same ARIA-value
               rule the flat row's chevron follows (an idref with nothing
               behind it is invalid to assistive tech). */
            aria-controls={expanded ? panelId : undefined}
            aria-label={
              expanded
                ? `Hide ${group.symbol} per-account rows`
                : `Show ${group.symbol} per-account rows`
            }
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
            className="cursor-pointer rounded p-1 text-muted outline-none transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
          >
            {/* The chip's count rides in the accessible name, so a screen
                reader hears the duplicated-holding signal with the control. */}
            <span className="sr-only">{`, ${group.account_count} accounts`}</span>
            {expanded ? (
              <ChevronDown className="size-3.5" aria-hidden />
            ) : (
              <ChevronRight className="size-3.5" aria-hidden />
            )}
          </button>
        </div>
      </td>
      {/* Value-weighted day % (docs/09): Σ day Δ ÷ Σ (value − day Δ). */}
      <td
        className={`px-3 py-2 text-right tabular ${
          group.day_change_pct === null
            ? "text-muted"
            : group.day_change_pct < 0
              ? "text-danger"
              : group.day_change_pct > 0
                ? "text-gain"
                : ""
        }`}
      >
        {group.day_change_pct === null ? "—" : fmtSignedPct(group.day_change_pct)}
      </td>
      <td className="hidden px-3 py-2 sm:table-cell">
        {age ?? <span className="text-xs text-muted">no price — enter one manually</span>}
      </td>
      {/* Every row of a security shares a listing currency, so this native
          sum is a single-currency sum (unlike the portfolio Σ the payload
          refuses to compute). */}
      <td className="px-3 py-2 text-right tabular">
        {fmtNative(group.market_value_native, group.listing_currency)}
      </td>
      <td className="px-3 py-2 text-right tabular" data-testid="position-group-value-cad">
        {fmtCad(group.market_value_cad)}
      </td>
      <td
        className={`px-3 py-2 text-right tabular ${
          group.unrealized_gain_cad === null
            ? "text-muted"
            : Number(group.unrealized_gain_cad) < 0
              ? "text-danger"
              : "text-gain"
        }`}
      >
        {group.unrealized_gain_cad === null ? (
          "—"
        ) : (
          <>
            {fmtCadSigned(Number(group.unrealized_gain_cad))}
            {group.unrealized_gain_pct !== null && (
              <span className="block text-xs text-muted sm:hidden">
                {fmtSignedPctBookCost(group.unrealized_gain_pct)}
              </span>
            )}
          </>
        )}
      </td>
      <td
        className={`hidden px-3 py-2 text-right tabular sm:table-cell ${
          group.unrealized_gain_pct === null
            ? "text-muted"
            : group.unrealized_gain_pct < 0
              ? "text-danger"
              : "text-gain"
        }`}
      >
        {group.unrealized_gain_pct === null
          ? "—"
          : fmtSignedPctBookCost(group.unrealized_gain_pct)}
      </td>
    </tr>
  );
}

/**
 * A group's per-account rows — a SIBLING `<tbody>`, not a nested table: the
 * sub-rows then sit on the very same columns as the group above them (a
 * nested table would re-flow them into their own widths). It stays in the DOM
 * collapsed (`hidden`), so the chevron's `aria-controls` always resolves, and
 * `aria-labelledby` ties the rowgroup back to its group header.
 */
function PositionGroupAccounts({
  group,
  expanded,
}: {
  group: PositionGroup;
  expanded: boolean;
}) {
  return (
    <tbody
      hidden={!expanded}
      aria-labelledby={`position-group-header-${group.security_id}`}
      data-testid="position-group-accounts"
    >
      {group.rows.map((row) => (
        <PositionRow
          key={`${row.account_id}-${row.security_id}`}
          position={row}
          showAccount
          accountLink
          subrow
        />
      ))}
    </tbody>
  );
}

// --- Exposure (currency + M18 look-through dimensions) -----------------------

/**
 * M167: the choropleth's generated path set is ~50 kB raw (~21 kB gzipped) and
 * only the GEOGRAPHY tab needs it, so it rides its own chunk behind `lazy` —
 * /investments keeps its pre-M167 payload for everyone who never opens that
 * tab (docs/09 §Exposure visuals).
 */
const ExposureWorldMap = lazy(async () => ({
  default: (await import("../components/charts/ExposureWorldMap")).ExposureWorldMap,
}));

type ExposureTab = "currency" | BreakdownDimension;

const EXPOSURE_TABS: readonly SegmentedOption<ExposureTab>[] = [
  { value: "currency", label: "Currency" },
  { value: "sector", label: "Sector" },
  { value: "geography", label: "Geography" },
  { value: "asset_class", label: "Asset class" },
  { value: "company", label: "Companies" },
];

const EXPOSURE_LABELS: Record<BreakdownDimension, string> = {
  sector: "Sector",
  geography: "Geography",
  asset_class: "Asset class",
  company: "Companies",
};

/**
 * M169: bucket keys are arbitrary strings ("United States", "Unclassified",
 * anything a provider published), so they cannot be pasted into an HTML id
 * without escaping every one of them. The drill panel's id is a ROW INDEX
 * under the table's own `useId()` prefix instead — scoped to this table
 * instance, so two Exposure cards never collide, and safe for any key.
 */
function makeDrillIdFactory(): (key: string) => string {
  const issued = new Map<string, string>();
  return (key: string) => {
    let id = issued.get(key);
    if (id === undefined) {
      id = String(issued.size);
      issued.set(key, id);
    }
    return id;
  };
}

/**
 * Exposure card (docs/09): the currency view plus the M18 look-through
 * dimensions — sector / geography / asset class, each a donut over
 * GET /portfolio/breakdown with a legend table. Currency keeps its native-
 * value table (slices are not CAD-comparable); look-through dimensions are
 * all-CAD so the donut center can honestly carry the total.
 */
function ExposureSection({ accountId }: { accountId?: number }) {
  const [dimension, setDimension] = useState<ExposureTab>("currency");
  return (
    <Card hover className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <CardTitle>Exposure</CardTitle>
        <SegmentedControl
          options={EXPOSURE_TABS}
          value={dimension}
          onChange={setDimension}
          layoutId="exposure-dimension"
          size="sm"
          ariaLabel="Exposure dimension"
        />
      </div>
      <div className="mt-4">
        {dimension === "currency" ? (
          <CurrencyExposureBody accountId={accountId} />
        ) : (
          <BreakdownBody dimension={dimension} accountId={accountId} />
        )}
      </div>
    </Card>
  );
}

function CurrencyExposureBody({ accountId }: { accountId?: number }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const exposure = useQuery({
    queryKey: ["exposure", accountId],
    queryFn: () => getExposure(accountId),
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });

  if (exposure.isPending) {
    return <SkeletonRows rows={3} />;
  }
  if (exposure.isError) {
    return <p className="text-sm text-danger">{exposure.error.message}</p>;
  }

  const rows = exposure.data.items;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<span className="font-mono text-xs">$</span>}
        title="No holdings to aggregate yet"
        hint="Currency mix appears once positions exist."
        preview={{
          src: "/help/investments.png",
          alt: "The Investments page with holdings and totals",
        }}
        action={{
          label: "Record a trade",
          onClick: focusTradeForm,
          ariaLabel: "Record a trade — No holdings to aggregate yet",
        }}
      />
    );
  }

  return (
    <div className="grid items-center gap-6 lg:grid-cols-[1fr_320px]">
      <div role="region" aria-label="Holdings by listing currency table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="px-3 py-2 font-medium">Listing currency</th>
              <th className="px-3 py-2 text-right font-medium">Holdings</th>
              <th className="px-3 py-2 text-right font-medium">Market value (native)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.listing_currency}
                className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
              >
                <td className="px-3 py-2 font-medium">{row.listing_currency}</td>
                <td className="px-3 py-2 text-right tabular">{row.holdings_count}</td>
                <td className="px-3 py-2 text-right tabular">
                  {fmtNative(row.total_market_value_native, row.listing_currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ExposureDonut rows={rows} />
    </div>
  );
}

function BreakdownBody({ dimension, accountId }: { dimension: BreakdownDimension; accountId?: number }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const breakdown = useQuery({
    queryKey: ["breakdown", dimension, accountId],
    queryFn: () => getBreakdown(dimension, accountId),
  });

  // M167 (docs/09 §Exposure visuals): every dimension's list folds to its
  // head + an `Other` bucket, and the expansion is remembered for the rest of
  // this session (M139's sessionStorage pattern — no server persistence).
  const expanded = useExpandedDimension(dimension);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // M169: the one bucket whose drill is open (docs/09 §Exposure drill). One at
  // a time — a table with five open drills is not a table.
  const [drilledKey, setDrilledKey] = useState<string | null>(null);
  const rowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});
  const tbodyId = useId();
  // M169: stable per-bucket panel ids, scoped to this table (see the note on
  // drillPanelId above).
  const drillIds = useMemo(() => makeDrillIdFactory(), []);

  const rows = breakdown.data?.items ?? [];
  // The fold sorts, so the table and the donut agree on order as well as on
  // the head/Other split.
  const fold = useMemo(() => foldRows(rows, FOLD_N_BY_DIMENSION[dimension]), [rows, dimension]);
  const visibleRows = expanded ? fold.all : fold.rows;
  const foldN = FOLD_N_BY_DIMENSION[dimension];

  // Selecting a row (from the map, or the row itself): highlight it, scroll it
  // into view, and unfold the list first when the fold is what hides it.
  useEffect(() => {
    if (!selectedKey) return;
    rowRefs.current[selectedKey]?.scrollIntoView?.({ block: "nearest" });
  }, [selectedKey, visibleRows]);

  if (breakdown.isPending) {
    return <SkeletonRows rows={3} />;
  }
  if (breakdown.isError) {
    return <p className="text-sm text-danger">{breakdown.error.message}</p>;
  }

  const unclassifiedFunds = breakdown.data.unclassified_funds ?? [];
  // M166: the slice allocated from each fund's own published sector mix.
  const estimatedCents = parseFloat(breakdown.data.estimated_cad ?? "0");
  const estimatedPct = parseFloat(breakdown.data.total_cad) > 0
    ? (estimatedCents / parseFloat(breakdown.data.total_cad)) * 100
    : 0;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<span className="font-mono text-xs">%</span>}
        title="No holdings to aggregate yet"
        hint="The breakdown appears once positions exist."
        preview={{
          src: "/help/investments.png",
          alt: "The Investments page with holdings and totals",
        }}
        action={{
          label: "Record a trade",
          onClick: focusTradeForm,
          ariaLabel: "Record a trade — No holdings to aggregate yet",
        }}
      />
    );
  }

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[1fr_320px]">
      <div>
      {/* M167: the geography view gets a world choropleth above the list — a
          picture OF these rows, never a second source of truth. */}
      {dimension === "geography" ? (
        <Suspense fallback={null}>
          <ExposureWorldMap
            rows={fold.all}
            onSelect={(rowKey) => {
              setSelectedKey(rowKey);
              if (!visibleRows.some((row) => row.key === rowKey)) {
                setExpandedDimension(dimension, true);
              }
            }}
            selectedKey={selectedKey}
          />
        </Suspense>
      ) : null}
      <div role="region" aria-label="Exposure breakdown table" tabIndex={0} className="mt-4 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="px-3 py-2 font-medium">{EXPOSURE_LABELS[dimension]}</th>
              <th className="px-3 py-2 text-right font-medium">Market value</th>
              <th className="px-3 py-2 text-right font-medium">Weight</th>
            </tr>
          </thead>
          <tbody id={tbodyId}>
            {visibleRows.map((row) => {
              if (isOtherBucket(row)) {
                return (
                  <tr
                    key={OTHER_KEY}
                    data-exposure-row={OTHER_KEY}
                    className="border-b border-veil/[0.05] text-muted transition-colors last:border-b-0"
                  >
                    <td className="px-3 py-2 font-medium">Other</td>
                    <td className="px-3 py-2 text-right tabular">{fmtCad(row.market_value_cad)}</td>
                    <td className="px-3 py-2 text-right tabular">
                      {Number(row.weight_pct).toFixed(2)}%
                    </td>
                  </tr>
                );
              }
              // M169: every bucket row is a drill target (docs/09 §Exposure
              // drill). The row click opens it, the chevron is the keyboard
              // path — M126's expand contract, one level down from the fold.
              const drilled = drilledKey === row.key;
              const drillPanelId = drillIds(row.key);
              return (
              <Fragment key={row.key}>
              <tr
                ref={(element) => {
                  rowRefs.current[row.key] = element;
                }}
                data-exposure-row={row.key}
                onClick={() => setDrilledKey(drilled ? null : row.key)}
                className={`cursor-pointer border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04] ${
                  row.key === selectedKey ? "bg-accent/[0.08] ring-1 ring-inset ring-accent" : ""
                } ${drilled ? "bg-veil/[0.04]" : ""} ${row.key === "Unclassified" ? "text-muted" : ""}`}
              >
                <td className="px-3 py-2 font-medium">
                  {row.key}
                  {/* M166: a bucket can be part sourced, part estimated —
                      mark it and let the tooltip say how much of it. */}
                  {row.estimated ? (
                    <span
                      className="ml-1.5 align-middle text-[10px] uppercase tracking-widest text-muted"
                      title={`${fmtCad(row.estimated_cad ?? "0")} of this slice is allocated from the fund's own published sector mix.`}
                    >
                      est
                    </span>
                  ) : null}
                </td>
                <td className="px-3 py-2 text-right tabular">{fmtCad(row.market_value_cad)}</td>
                <td className="px-3 py-2 text-right tabular">
                  <span className="inline-flex items-center gap-1.5">
                    <span>{row.weight_pct != null ? Number(row.weight_pct).toFixed(2) : "0.00"}%</span>
                    {/* M126's contract: a real button with aria-expanded, so a
                        screen reader hears the state and Tab reaches it. The
                        name carries the bucket and the drill, per docs/09. */}
                    <button
                      type="button"
                      aria-expanded={drilled}
                      /* Only reference the panel while it exists — an
                         `aria-controls` idref with no element behind it is an
                         invalid ARIA value (axe's `aria-valid-attr-value`), and
                         a collapsed row has nothing to control yet. */
                      aria-controls={drilled ? drillPanelId : undefined}
                      aria-label={
                        drilled ? `Hide ${row.key} drill` : `Drill into ${row.key}`
                      }
                      onClick={(event) => {
                        event.stopPropagation();
                        setDrilledKey(drilled ? null : row.key);
                      }}
                      className="cursor-pointer rounded p-1 text-muted outline-none transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      {drilled ? (
                        <ChevronDown className="size-3.5" aria-hidden />
                      ) : (
                        <ChevronRight className="size-3.5" aria-hidden />
                      )}
                    </button>
                  </span>
                </td>
              </tr>
              {/* M169: the drill opens INLINE, in the table — the Exposure
                  card stays the surface and nothing navigates away. */}
              {drilled ? (
                <tr data-exposure-drill-row={row.key}>
                  <td colSpan={3} className="px-0 py-0">
                    <div id={drillPanelId}>
                      <ExposureDrillPanel
                        bucketKey={row.key}
                        dimension={dimension}
                        accountId={accountId}
                      />
                    </div>
                  </td>
                </tr>
              ) : null}
              </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {/* M167: the fold's expander — the head stays readable, the tail is one
          click and one session-memory away (docs/09 §Exposure visuals). */}
      {fold.other ? (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={tbodyId}
          onClick={() => setExpandedDimension(dimension, !expanded)}
          className="mt-2 self-start rounded px-2 py-1 text-sm text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          {expanded
            ? `Show the top ${foldN} ${DIMENSION_PLURAL[dimension]} only`
            : `Show remaining ${fold.other.count} ${DIMENSION_PLURAL[dimension]} (${Number(
                fold.other.weight_pct,
              ).toFixed(2)}%)`}
        </button>
      ) : null}
      {/* M147 honesty copy (docs/09): every fund the Unclassified slice
          is made of names itself here — not-yet-imported (one Import
          click) and top-slice-only (a fuller provider source) get
          different, truthful explanations. */}
      {unclassifiedFunds.length > 0 && (
        <div
          role="note"
          className="mt-3 rounded-lg border border-veil/[0.06] p-3 text-sm text-muted"
        >
          <p className="font-medium">
            Unclassified because these funds&apos; holdings haven&apos;t been imported yet:
          </p>
          <ul className="mt-1 list-disc pl-5">
            {unclassifiedFunds.map((fund) => (
              <li key={fund.security_id}>
                {fund.reason === "partial" ? (
                  <>
                    {fund.symbol} — {fund.name} ({fmtCad(fund.market_value_cad)}): only{" "}
                    {Number(fund.imported_weight_pct ?? 0).toFixed(1)}% of this fund is
                    imported — providers publish just the top holdings; the rest of the slice
                    waits on a fuller source.
                  </>
                ) : (
                  <>
                    {fund.symbol} — {fund.name} ({fmtCad(fund.market_value_cad)}): open the
                    fund and use Import holdings to fill this slice.
                  </>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {/* M166 honesty copy (docs/09 §Exposure honesty): a slice allocated
          from a fund's PUBLISHED sector mix is an estimate, and the pie
          says so in the same voice as the Unclassified note above. */}
      {estimatedCents > 0 && (
        <div
          role="note"
          className="mt-3 rounded-lg border border-veil/[0.06] p-3 text-sm text-muted"
        >
          <p>
            ≈{estimatedPct.toFixed(1)}% of this pie ({fmtCad(breakdown.data.estimated_cad ?? "0")})
            is allocated from each fund&apos;s own published sector mix, not from a
            per-holding figure — the rows it feeds are marked{" "}
            <span className="text-[10px] uppercase tracking-widest">est</span>. Company,
            country and asset class stay exact.
          </p>
        </div>
      )}
      </div>
      {/* CategoriesDonut is shape-generic (name + cad_total slices): feed it
          the fold's COLLAPSED list — head + Other — so the pie and the table
          fold by exactly one rule (M167, docs/09 §Exposure visuals). The
          donut stays folded while the table is expanded: 40 slices is not a
          chart, and the table is where a full list belongs. */}
      <CategoriesDonut
        items={fold.rows.map((row) => ({
          category_id: null,
          name: isOtherBucket(row) ? `Other (${row.count})` : row.key,
          cad_total: row.market_value_cad,
        }))}
        total={breakdown.data.total_cad}
        centerLabel="exposure"
        title="Exposure breakdown"
      />
    </div>
  );
}

// --- Security creation (yfinance autofill + manual fallback, docs/05) --------
// --- Dividend suggestions (yfinance history vs recorded trades) ------------

const DISMISSED_KEY = "fintrack:dismissed_dividends";

// F-6: the dismissed set holds `{account_id, security_id, date}` triples —
// row IDENTIFIERS — so it is keyed per tenant (`lib/tenantStorage.ts`); two
// tenants sharing a browser never inherit each other's dismissals.
function _loadDismissed(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function _saveDismissed(key: string, s: Set<string>) {
  localStorage.setItem(key, JSON.stringify([...s]));
}

function _suggestionKey(s: { account_id: number; security_id: number; date: string }) {
  return `${s.account_id}-${s.security_id}-${s.date}`;
}

function DividendSuggestionsSection({ accountId, showAccount = true }: { accountId?: number; showAccount?: boolean }) {
  const [, rerender] = useState(0);
  // F-6: the dismissed set is tenant-scoped (identifiers, not UI state) —
  // resolved once per render from the session's own user.
  const dismissedKey = scopedKey(DISMISSED_KEY, useOptionalUser());
  const { accounts, investmentAccounts } = useInvestmentAccounts();
  const accountNameMap = useMemo(
    () => new Map((accounts.data?.items ?? []).map((a) => [a.id, a.name])),
    [accounts.data],
  );
  const suggestions = useQuery({
    queryKey: ["dividend-suggestions", accountId],
    queryFn: () => getDividendSuggestions(accountId),
    enabled: investmentAccounts.length > 0,
    // TanStack's default structural sharing copies array indices only; keep the
    // additive history property attached to the list-shaped result.
    structuralSharing: false,
    refetchInterval: (query) => (query.state.data?.history?.pending ? 5_000 : false),
  });
  const showAccountCol = showAccount && accountId === undefined;
  // M95 P2 (docs/09 §Empty states): the query above is gated on the tenant
  // HAVING investment accounts, so a tenant without any (a truly-empty one)
  // would sit on this skeleton forever — a state that can never resolve to
  // data. While the accounts are still loading the skeleton is honest; once
  // they are loaded and none is an investment account, the card has nothing to
  // suggest and hides exactly the way it already does when suggestions exist
  // but none is visible.
  if (accounts.isPending) {
    return (
      <Card className="p-4">
        <CardTitle>Dividend Suggestions</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }
  if (investmentAccounts.length === 0) return null;
  if (suggestions.isPending) {
    return (
      <Card className="p-4">
        <CardTitle>Dividend Suggestions</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }
  if (suggestions.isError) {
    return (
      <Card className="p-4">
        <CardTitle>Dividend Suggestions</CardTitle>
        <p className="text-sm text-danger">{suggestions.error.message}</p>
      </Card>
    );
  }
  const dismissed = _loadDismissed(dismissedKey);
  const visible = suggestions.data.filter((s) => !dismissed.has(_suggestionKey(s)));
  const history = suggestions.data.history ?? {
    pending: 0,
    unavailable: 0,
    empty: 0,
    stale: 0,
    quote_miss: 0,
    missing: 0,
    source: null,
    age_days: null,
    as_of: null,
  };
  // quote_miss (M186) is a subset of unavailable — sources whose provider said
  // the SYMBOL itself has no quote. Report them separately so the copy never
  // implies a retry will help; the generic sentence covers only the rest.
  const quoteMiss = history.quote_miss ?? 0;
  const transientUnavailable = Math.max(0, history.unavailable - quoteMiss);
  const hasHistoryNotice =
    history.pending > 0 ||
    history.unavailable > 0 ||
    history.empty > 0 ||
    history.stale > 0 ||
    (history.missing ?? 0) > 0;
  if (visible.length === 0 && !hasHistoryNotice) return null;
  const dismiss = (s: { account_id: number; security_id: number; date: string }) => {
    const d = _loadDismissed(dismissedKey);
    d.add(_suggestionKey(s));
    _saveDismissed(dismissedKey, d);
    rerender((n) => n + 1);
  };
  const dismissAll = () => {
    const d = _loadDismissed(dismissedKey);
    for (const s of visible) d.add(_suggestionKey(s));
    _saveDismissed(dismissedKey, d);
    rerender((n) => n + 1);
  };
  return (
    <Card hover className="p-4">
      <div className="mb-3 flex items-center justify-between">
        <CardTitle>Dividend Suggestions</CardTitle>
        {visible.length > 0 && <Button variant="ghost" size="sm" onClick={dismissAll}>Dismiss all</Button>}
      </div>
      {/* docs/07 §M184: which feed these suggestions came from, when it last
          answered, and whether it is stale or missing — the same three words
          the two report panels' strips use. */}
      <DividendProvenanceStrip provenance={provenanceFromHistory(history)} className="mb-3" />
      {hasHistoryNotice && (
        <p role="status" className="mb-3 text-xs text-muted">
          {history.pending > 0 && `${history.pending} dividend history refresh${history.pending === 1 ? " is" : "es are"} pending. `}
          {transientUnavailable > 0 && `${transientUnavailable} dividend history source${transientUnavailable === 1 ? " is" : "s are"} unavailable; last-good history, if any, is retained. `}
          {quoteMiss > 0 && `${quoteMiss} dividend history source${quoteMiss === 1 ? " has" : "s have"} no public quote for the symbol (possibly delisted); last-good history is kept and automatic retries are paused. `}
          {history.empty > 0 && `${history.empty} source${history.empty === 1 ? " returned" : "s returned"} no dividend events${history.as_of ? ` (as of ${history.as_of})` : ""}. `}
          {history.stale > 0 && `${history.stale} dividend history source${history.stale === 1 ? " is" : "s are"} stale${history.as_of ? ` (last success ${history.as_of})` : " (no successful refresh yet)"}.`}
        </p>
      )}
      {visible.length > 0 && <>
        <p className="mb-3 text-xs text-muted">
          Pending dividends from market data, scoped per holding. Click Record to log.
        </p>
        <div role="region" aria-label="Dividend suggestions table" tabIndex={0} className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                {showAccountCol && <th className="px-3 py-2 font-medium">Account</th>}
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 font-medium">Date</th>
                <th className="px-3 py-2 text-right font-medium">Per share</th>
                <th className="px-3 py-2 text-right font-medium">Qty</th>
                <th className="px-3 py-2 text-right font-medium">Gross</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((s) => (
                <DividendSuggestionRow
                  key={_suggestionKey(s)}
                  suggestion={s}
                  accountName={accountNameMap.get(s.account_id) ?? `#${s.account_id}`}
                  showAccount={showAccountCol}
                  onDismiss={() => dismiss(s)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </>}
    </Card>
  );
}

function DividendSuggestionRow({
  suggestion,
  accountName,
  onDismiss,
  showAccount = true,
}: {
  suggestion: DividendSuggestion;
  accountName: string;
  onDismiss: () => void;
  showAccount?: boolean;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  // M181 (docs/33 rule 6): `POST /trades/record-dividend` creates a
  // mandatory-plaintext row server-side, so it fails closed for an opted-in
  // tenant. The gate is ENROLMENT ALONE — unlocking the vault cannot enable a
  // server-side write (rule 7d) — and the Record control plus the DRIP options
  // that only feed it are replaced by the reason. Dismiss is a removal path and
  // stays available.
  const serverWriteBlocked = useServerSideWriteBlocked();
  const [drip, setDrip] = useState(false);
  const [dripPrice, setDripPrice] = useState("");
  const [dripCommission, setDripCommission] = useState("");
  const [fractional, setFractional] = useState(true);
  const grossAmount = Number(suggestion.gross_amount);
  const dripShares =
    drip && dripPrice !== "" && Number(dripPrice) > 0
      ? fractional
        ? ((grossAmount - Number(dripCommission || "0")) / Number(dripPrice)).toFixed(4)
        : Math.floor((grossAmount - Number(dripCommission || "0")) / Number(dripPrice)).toString()
      : null;
  const record = useMutation({
    mutationFn: () =>
      recordDividend({
        account_id: suggestion.account_id,
        security_id: suggestion.security_id,
        date: suggestion.date,
        gross_amount: suggestion.gross_amount,
        action: "dividend",
        drip,
        drip_price: drip ? dripPrice : undefined,
        drip_commission: drip && dripCommission !== "" ? dripCommission : undefined,
        fractional: drip ? fractional : undefined,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["dividend-suggestions"] });
      void queryClient.invalidateQueries({ queryKey: ["trades"] });
      void queryClient.invalidateQueries({ queryKey: ["positions"] });
      void queryClient.invalidateQueries({ queryKey: ["exposure"] });
      toast.success("Dividend recorded", `${suggestion.symbol} ${suggestion.date}`);
    },
    onError: (err) => toast.error("Record failed", err.message),
  });
  return (
    <tr className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]">
      {showAccount && <td className="px-3 py-2 text-xs text-muted">{accountName}</td>}
      <td className="px-3 py-2 font-medium">{suggestion.symbol}</td>
      <td className="px-3 py-2 tabular">{suggestion.date}</td>
      <td className="px-3 py-2 text-right tabular">
        {fmtNative(suggestion.amount, suggestion.currency)}
      </td>
      <td className="px-3 py-2 text-right tabular">{fmtQuantity(suggestion.quantity)}</td>
      <td className="px-3 py-2 text-right tabular">
        {fmtNative(suggestion.gross_amount, suggestion.currency)}
      </td>
      <td className="px-3 py-2 text-right">
        <div className="flex flex-col items-end gap-1">
          <div className="flex items-center gap-1">
            {serverWriteBlocked ? (
              <ServerSideBlocked capability="write:dividend" className="max-w-80 justify-end" />
            ) : (
              <>
                <label className="flex items-center gap-0.5 text-xs text-muted">
                  <input type="checkbox" checked={drip} onChange={(e) => setDrip(e.target.checked)} className="h-3 w-3" />
                  DRIP
                </label>
                <Button variant="primary" size="sm" loading={record.isPending} onClick={() => record.mutate()}>
                  Record
                </Button>
              </>
            )}
            <button type="button" onClick={onDismiss} className="ml-1 text-[10px] text-muted transition-colors hover:text-danger" title="Dismiss" aria-label="Dismiss dividend suggestion">
              ✕
            </button>
          </div>
          {drip && (
            <>
              <div className="flex items-center gap-1 text-[11px]">
                <label className="flex items-center gap-0.5 text-muted whitespace-nowrap">
                  <input type="checkbox" checked={fractional} onChange={(e) => setFractional(e.target.checked)} className="h-2.5 w-2.5" />
                  frac
                </label>
                <input value={dripPrice} onChange={(e) => setDripPrice(e.target.value)} placeholder="$" inputMode="decimal"
                  className="field-ring w-14 rounded border border-control bg-surface px-1 py-0.5 text-[11px] outline-none" />
                <input value={dripCommission} onChange={(e) => setDripCommission(e.target.value)} placeholder="fee" inputMode="decimal"
                  className="field-ring w-12 rounded border border-control bg-surface px-1 py-0.5 text-[11px] outline-none" />
              </div>
              {dripShares !== null && (
                <span className="text-[10px] text-muted whitespace-nowrap">≈ {dripShares} sh</span>
              )}
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

const EMPTY_SECURITY: SecurityInput = {
  symbol: "",
  name: "",
  kind: "stock",
  // M143 (docs/07 §Unresolved symbols): NO guessed default. `listing_currency`
  // is filled by the lookup for every listing that resolves; an empty value
  // means "we could not identify this instrument", and the API stores the row
  // as UNRESOLVED (dead_symbol, no backfill) instead of stamping a currency
  // no quote ever supplied. The old "CAD" default made an unresolved row look
  // exactly like a resolved one — indistinguishable until a price never came.
  listing_currency: "",
  exchange: "",
};

function SecurityForm() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState<SecurityInput>(EMPTY_SECURITY);
  const [notice, setNotice] = useState<string | null>(null);
  const [lookupWarningSymbol, setLookupWarningSymbol] = useState<string | null>(null);
  // M141 (docs/09 §No public quote): the add form's non-blocking warning. It
  // holds the symbol we could NOT confirm a quote for, so the banner persists
  // until dismissed — and the "Add anyway?" path is an explicit second click
  // rather than a hard block (a never-fetched symbol is legitimate to watch).
  const [quoteWarning, setQuoteWarning] = useState<{ symbol: string; reason: string } | null>(
    null,
  );
  // The symbol the user already confirmed ("Add anyway") — a second submit for
  // the SAME symbol must not ask again.
  const [quoteConfirmed, setQuoteConfirmed] = useState<string | null>(null);
  // M142 (docs/09 §New security suggestion chip): what the probe is offering.
  // `suggestion` is a resolved listing whose SYMBOL differs from the typed
  // string; `candidates` is the pick-one chooser when several listings
  // matched. They never appear together, and neither ever stores anything —
  // the user's click does.
  const [suggestion, setSuggestion] = useState<SecurityLookupResult | null>(null);
  const [candidates, setCandidates] = useState<SecurityLookupCandidate[] | null>(null);
  // The string whose probe already ran (one settled value = one request) and
  // where the request came from: the debounce is SILENT on a miss, the
  // explicit "Look up" button keeps W11 pin 7's loud, persistent warning.
  const probedRef = useRef<string | null>(null);

  /** Adopt a resolved listing into the form — the chip's accept and the
   *  picker's pick both land here, so the fields can never half-apply. */
  function applyListing(listing: SecurityInput) {
    setForm((prev) => ({ ...prev, ...listing }));
    setNotice(`Autofilled from market data for ${listing.symbol}.`);
    setSuggestion(null);
    setCandidates(null);
    setLookupWarningSymbol(null);
    // M141: the accepted listing IS a confirmed quote for the new symbol, so
    // the "Add anyway?" gate must not re-ask for the one the user just chose.
    setQuoteWarning(null);
    setQuoteConfirmed(listing.symbol.toUpperCase());
    probedRef.current = listing.symbol.toUpperCase();
  }

  const lookup = useMutation({
    mutationFn: (vars: { query: string; mode: "probe" | "button" }) =>
      lookupSecurity(vars.query),
    onSuccess: (result, vars) => {
      const listing = resolvedListing(result);
      if (listing === null) {
        // Several listings matched — either a typed NAME (APTX.TO + APTX.NE)
        // or, since M143, a bare-vs-`.TO` COLLISION (VGRO + VGRO.TO). The
        // chooser IS the answer: a background guess must never pick a market
        // for the user, and picking applies the chosen candidate directly
        // (docs/07 §Candidate resolution) instead of re-asking the resolver
        // the question it just failed.
        setCandidates(result.candidates);
        setSuggestion(null);
        return;
      }
      setCandidates(null);
      if (listing.symbol.toUpperCase() !== vars.query) {
        // A NAME resolved to one listing: offer it, don't take over the field.
        setSuggestion(result);
        setNotice(null);
        return;
      }
      setSuggestion(null);
      if (vars.mode === "button") {
        applyListing(listing); // the explicit Look up still autofills
      }
    },
    onError: (error, vars) => {
      // docs/07: lookup failures degrade to manual entry, never block creation.
      // W11 pin 7 (the explicit button): an unresolvable symbol says so
      // plainly and persists until dismissed — silent degradation hid the
      // fact nothing will be tracked. The DEBOUNCED probe stays quiet: a
      // string that matches nothing is not an error while it is being typed,
      // and M141's dead-symbol guard owns the "no public quote" copy.
      if (vars.mode === "probe") return;
      if (error instanceof ApiError && error.status === 404 && error.code === "lookup_unavailable") {
        const symbol = vars.query.toUpperCase();
        setLookupWarningSymbol(symbol);
        toast.error(
          "Lookup failed",
          `${symbol} not found — it won't be tracked automatically. You can still add it manually and enter prices by hand.`,
        );
      } else {
        setNotice(
          `Lookup unavailable (${error.message}) — fill the fields manually and create.`,
        );
      }
    },
  });

  // M142: the autofill resolves DURING typing — one debounced probe per
  // settled value, and only the FIRST time that value settles, so a slow
  // network can never turn keystrokes into a request storm. Any edit cancels
  // the timer and clears the offer, so a chip cannot outlive the string it
  // was about, and accepting one pins the accepted symbol as probed (the
  // field it wrote must not re-trigger the probe it came from).
  const typed = form.symbol.trim().toUpperCase();
  const suggestionListing = suggestion === null ? null : resolvedListing(suggestion);
  useEffect(() => {
    setSuggestion(null);
    setCandidates(null);
    if (typed === "" || probedRef.current === typed) return;
    const timer = window.setTimeout(() => {
      probedRef.current = typed;
      lookup.mutate({ query: typed, mode: "probe" });
    }, 600);
    return () => window.clearTimeout(timer);
  }, [typed]);

  const create = useMutation({
    mutationFn: () =>
      createSecurity({
        ...form,
        symbol: form.symbol.trim().toUpperCase(),
        name: form.name.trim(),
        listing_currency: form.listing_currency.trim().toUpperCase(),
        exchange: form.exchange.trim(),
      }),
    onSuccess: (security) => {
      void queryClient.invalidateQueries({ queryKey: ["securities"] });
      setForm(EMPTY_SECURITY);
      setNotice(null);
      setLookupWarningSymbol(null);
      setQuoteWarning(null);
      setQuoteConfirmed(null);
      toast.success("Security created", `${security.symbol} now appears in the selects below.`);
    },
    onError: (error) => {
      setNotice(error.message);
      toast.error("Security creation failed", error.message);
    },
  });

  // M141 (docs/07 §Symbol check): one yfinance probe before the add. NOT a
  // hard block — a symbol with no listing yet is legitimate to watch — so a
  // miss raises the warning banner and waits for an explicit "Add anyway".
  // `quote_found: null` (we could not ask: network, rate-limit, privacy mode)
  // proceeds straight to create: an answer we do not have must not be
  // reported as "no quote".
  const quoteCheck = useMutation({
    mutationFn: () => checkQuote(form.symbol.trim().toUpperCase()),
    onSuccess: (result: QuoteCheck) => {
      if (result.quote_found === false) {
        setQuoteWarning({
          symbol: result.symbol,
          reason:
            result.reason ??
            `No public quote found for ${result.symbol} — the symbol may be delisted or private.`,
        });
        return;
      }
      create.mutate();
    },
    // A failed probe is not evidence about the symbol: add it (docs/07).
    onError: () => create.mutate(),
  });

  const canCreate = form.symbol.trim() !== "" && form.name.trim() !== "";

  /** Submit: probe first unless this exact symbol was already confirmed. */
  const submit = () => {
    const symbol = form.symbol.trim().toUpperCase();
    if (quoteConfirmed === symbol) {
      create.mutate();
      return;
    }
    quoteCheck.mutate();
  };

  return (
    <Card hover className="h-fit p-4">
      <CardTitle>New security</CardTitle>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label className="flex items-center gap-2 text-sm">
          <span className="w-24 shrink-0 text-muted">Symbol</span>
          <input
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value })}
            placeholder="XEQT"
            className={FIELD}
          />
          <Button
            type="button"
            variant="accent-soft"
            size="sm"
            className="shrink-0"
            loading={lookup.isPending}
            disabled={form.symbol.trim() === ""}
            onClick={() =>
              lookup.mutate({ query: form.symbol.trim().toUpperCase(), mode: "button" })
            }
          >
            Look up
          </Button>
        </label>
        {/* M142 (docs/09): the probe resolved while typing — offer, never take
            over. Accepting writes the canonical symbol + every autofilled
            field; continuing to type clears the offer instead of fighting it. */}
        {suggestion !== null && suggestionListing !== null && (
          <SuggestionChip
            typed={typed}
            symbol={suggestionListing.symbol}
            label={listingLabel(suggestion, suggestionListing.listing_currency)}
            onAccept={() => applyListing(suggestionListing)}
            onDismiss={() => setSuggestion(null)}
          />
        )}
        {candidates !== null && candidates.length > 0 && (
          <CandidatePicker
            typed={typed}
            candidates={candidates}
            pending={false}
            onPick={(candidate) => applyListing(candidateListing(candidate))}
            onDismiss={() => setCandidates(null)}
          />
        )}
        <Field label="Name">
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            className={FIELD}
          />
        </Field>
        <div className="grid grid-cols-3 gap-2">
          <Field label="Kind">
            <select
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value as SecurityInput["kind"] })}
              className={FIELD}
            >
              <option value="stock">stock</option>
              <option value="etf">etf</option>
              <option value="mutual_fund">mutual_fund</option>
            </select>
          </Field>
          <Field label="Currency">
            <input
              value={form.listing_currency}
              onChange={(e) => setForm({ ...form, listing_currency: e.target.value })}
              maxLength={3}
              className={FIELD}
            />
          </Field>
          <Field label="Exchange">
            <input
              value={form.exchange}
              onChange={(e) => setForm({ ...form, exchange: e.target.value })}
              className={FIELD}
            />
          </Field>
        </div>
        {notice !== null && <p className="text-xs text-muted">{notice}</p>}
        {lookupWarningSymbol !== null && (
          <div
            role="alert"
            data-testid="lookup-warning"
            className="flex items-start justify-between gap-2 rounded-lg border border-warn/40 bg-warn/[0.06] px-3 py-2 text-xs text-warn"
          >
            <p>
              <code className="font-mono">{lookupWarningSymbol}</code> not found — it won't be
              tracked automatically. You can still add it manually and enter prices by hand.
            </p>
            <button
              type="button"
              aria-label="Dismiss lookup warning"
              data-testid="dismiss-lookup-warning"
              onClick={() => setLookupWarningSymbol(null)}
              className="shrink-0 leading-none text-warn transition-colors hover:text-warn"
            >
              ✕
            </button>
          </div>
        )}
        {quoteWarning !== null && (
          <div
            role="alert"
            data-testid="quote-warning"
            className="rounded-lg border border-warn/40 bg-warn/[0.06] px-3 py-2 text-xs text-warn"
          >
            <p>
              No public quote found for{" "}
              <code className="font-mono">{quoteWarning.symbol}</code> — {quoteWarning.reason}{" "}
              Add anyway?
            </p>
            <div className="mt-2 flex items-center gap-2">
              {/* M141: the warnings' cancel path — an explicit second click,
                  not a hard block. A never-fetched symbol is legitimate to
                  watch (a future listing, a cash equivalent). */}
              <Button
                type="button"
                variant="accent-soft"
                size="sm"
                data-testid="add-anyway"
                onClick={() => {
                  setQuoteConfirmed(quoteWarning.symbol);
                  setQuoteWarning(null);
                  create.mutate();
                }}
              >
                Add anyway
              </Button>
              <button
                type="button"
                aria-label="Cancel adding this symbol"
                data-testid="dismiss-quote-warning"
                onClick={() => setQuoteWarning(null)}
                className="text-warn transition-colors hover:underline"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending || quoteCheck.isPending}
          disabled={!canCreate}
        >
          Create security
        </Button>
      </form>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-muted">{label}</span>
      {children}
    </label>
  );
}

// --- Price refresh + watchlist (W11 pins 3/8) --------------------------------

/**
 * Re-read the server at 2s/6s/12s after a refresh is queued (docs/18).
 *
 * `POST /portfolio/refresh` answers `202` immediately and does the work in a
 * FastAPI BackgroundTask chain that runs *after* the response — one yfinance
 * call per held/watched security, sequential. A single 2s invalidate reads the
 * DB long before the last row lands, which is why the button used to look
 * inert. The SSE stream also invalidates when its payload changes; this ladder
 * covers the stream being unavailable (privacy mode, hidden tab) and its last
 * tick at 12s keeps the rows inside the 15s budget.
 */
const REFRESH_INVALIDATE_BACKOFF_MS = [2000, 6000, 12000] as const;

/**
 * M185 G10 (docs/47): ONE mutation instance serves both the header button and
 * the staleness strip, so the strip reports the SAME `{queued, skipped_dead}`
 * answer the toast just announced — never two diverging histories of one
 * click.
 *
 * The demo write-lock verdict lives HERE for the same reason (F-3): one
 * `POST /portfolio/refresh`, one verdict, one wording — both affordances read
 * `refreshLock`, so they can never disagree about whether the padlock shows
 * or what the reason says.
 */
const REFRESH_DEMO_REASON =
  "The demo account is read-only — refreshing prices would write cached quotes.";

function useRefreshPrices() {
  const queryClient = useQueryClient();
  const toast = useToast();
  // M185 §4: a demo refusal is ONE info toast + the local refusal element —
  // never the generic error path (docs/47 §4).
  const demo = useDemoRefusal();
  const locked = useDemoWriteLock("POST", "/portfolio/refresh");
  const timersRef = useRef<number[]>([]);

  useEffect(
    () => () => {
      timersRef.current.forEach((timer) => clearTimeout(timer));
    },
    [],
  );

  const mutation = useMutation({
    mutationFn: refreshPrices,
    onSuccess: ({ queued, skipped_dead }) => {
      // M141: name what was SKIPPED rather than imply every row was refreshed
      // (docs/07 §Dead symbols). A dead row is only reachable through the
      // per-security "Try refresh" on its own page.
      toast.info(
        queued === 1 ? "Refreshing 1 security…" : `Refreshing ${queued} securities…`,
        skipped_dead > 0
          ? `${skipped_dead} ${skipped_dead === 1 ? "symbol has" : "symbols have"} no public quote and ${skipped_dead === 1 ? "was" : "were"} skipped — open the security page to try again.`
          : "Prices are refreshed in the background; tables update shortly.",
      );
      // The backend queues BackgroundTasks — retry with backoff until the
      // fan-out has had time to reach every security.
      timersRef.current.forEach((timer) => clearTimeout(timer));
      timersRef.current = REFRESH_INVALIDATE_BACKOFF_MS.map((delay) =>
        window.setTimeout(() => {
          void queryClient.invalidateQueries({ queryKey: ["positions"] });
          void queryClient.invalidateQueries({ queryKey: ["exposure"] });
          void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
        }, delay),
      );
    },
    onError: (err) => {
      if (demo.onError("Refresh prices", err)) return;
      toast.error("Refresh failed", err.message);
    },
  });
  return { ...mutation, demo, refreshLock: { locked, reason: REFRESH_DEMO_REASON } };
}

function RefreshPricesButton({ refresh }: { refresh: ReturnType<typeof useRefreshPrices> }) {
  // M185 §4 (docs/47): `POST /portfolio/refresh` is a genuinely blocked demo
  // write (not in the deps.py exemption set), so on the demo tenant the button
  // wears the padlock, STAYS ENABLED, and the real 403 becomes one local
  // refusal + one info toast — never an ErrorPanel, never a dead control.
  // The verdict comes from the page's ONE refresh mutation (F-3) and the
  // reason is VISIBLE, never sr-only (F-4).
  const locked = refresh.refreshLock.locked;
  const reasonId = "refresh-prices-demo-reason";
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        loading={refresh.isPending}
        data-testid="refresh-prices"
        aria-describedby={locked ? reasonId : undefined}
        onClick={() => refresh.mutate()}
      >
        {locked && <DemoLock />}
        Refresh prices
      </Button>
      {locked && <DemoReason id={reasonId}>{refresh.refreshLock.reason}</DemoReason>}
    </span>
  );
}

/**
 * The strip + the button share one mutation (M185 G9/G10): the header keeps
 * its `refresh-prices` control, and the strip reports the counts under the
 * positions table — where the stale rows it is talking about actually are.
 */
function StalenessStrip({
  positions,
  refresh,
}: {
  positions: UseQueryResult<PositionsResponse>;
  refresh: ReturnType<typeof useRefreshPrices>;
}) {
  const rows = (positions.data?.items ?? []).map((item) => ({
    label: item.symbol,
    price_date: item.price_date,
    price_source: item.price_source,
    price_age_days: item.price_age_days,
    price_fetched_at: item.price_fetched_at,
    price_stale: item.price_stale,
  }));
  return (
    <PriceStalenessStrip
      rows={rows}
      refreshOutcome={refresh.data ?? null}
      onRefresh={() => refresh.mutate()}
      refreshing={refresh.isPending}
      // F-3: the strip's affordance wears the SAME verdict + wording as the
      // header button — one mutation, one lock, both surfaces.
      lock={refresh.refreshLock}
    />
  );
}

function WatchlistSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const watchlist = useQuery({
    queryKey: ["watchlist"],
    queryFn: getWatchlist,
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });
  const securities = useQuery({
    queryKey: ["securities", 200],
    queryFn: () => listSecurities(200),
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });
  const [selected, setSelected] = useState("");

  const add = useMutation({
    mutationFn: () => addWatchlistEntry(Number(selected)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      const symbol = (securities.data?.items ?? []).find(
        (s) => String(s.id) === selected,
      )?.symbol;
      setSelected("");
      toast.success("Watchlist updated", `${symbol ?? "Security"} added.`);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "already_watched") {
        toast.error("Already watched", err.message);
      } else {
        toast.error("Watchlist update failed", err.message);
      }
    },
  });

  const remove = useMutation({
    mutationFn: (securityId: number) => removeWatchlistEntry(securityId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.info("Removed from watchlist");
    },
    onError: (err) => toast.error("Watchlist update failed", err.message),
  });

  // M186 (docs/09 §Watchlist ticker search): a market answer is PICKED, not
  // selected — that row does not exist locally yet, so the pick itself writes:
  // reuse an exact-symbol stored row or `POST /securities`, then `POST
  // /watchlist`, then both lists refetch so the new row is visible everywhere.
  const addFromMarket = useMutation({
    mutationFn: async (listing: SecurityInput) => {
      const existing = (securities.data?.items ?? []).find(
        (s) => s.symbol.toUpperCase() === listing.symbol.toUpperCase(),
      );
      const security = existing ?? (await createSecurity(listing));
      await addWatchlistEntry(security.id);
      return security;
    },
    onSuccess: (security) => {
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      void queryClient.invalidateQueries({ queryKey: ["securities"] });
      toast.success("Watchlist updated", `${security.symbol} added.`);
    },
    onError: (err, listing) => {
      if (err instanceof ApiError && err.code === "already_watched") {
        toast.error("Already watched", err.message);
      } else if (err instanceof ApiError && err.code === "duplicate_security") {
        // Another surface created the row while this list was stale — the
        // stored row IS the answer now, so refresh it and say how to finish.
        void queryClient.invalidateQueries({ queryKey: ["securities"] });
        toast.error(
          "Already in your list",
          `${listing.symbol.toUpperCase()} was added elsewhere — pick it from the list and press Add.`,
        );
      } else {
        toast.error("Watchlist update failed", err.message);
      }
    },
  });

  return (
    <Card hover className="h-fit p-4">
      <CardTitle>Watchlist</CardTitle>
      <form
        className="mb-2 flex items-start gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (selected !== "") add.mutate();
        }}
      >
        <div className="flex-1">
          <SecurityPicker
            securities={securities.data?.items ?? []}
            value={selected}
            onChange={setSelected}
            placeholder="Add a symbol to follow…"
            // M186: the market fallback arms only once the STORED list is
            // known (docs/09 §Watchlist ticker search) — a half-loaded list
            // must never hide a row it already has, nor race a duplicate create.
            onSearchMarket={securities.isSuccess ? lookupSecurity : undefined}
            onPickMarket={(listing) => addFromMarket.mutate(listing)}
          />
        </div>
        <Button
          type="submit"
          variant="accent-soft"
          loading={add.isPending}
          disabled={selected === ""}
          data-testid="watch-add"
        >
          Add
        </Button>
      </form>

      {watchlist.isPending && <SkeletonRows rows={3} />}
      {watchlist.isError && <p className="text-sm text-danger">{watchlist.error.message}</p>}
      {watchlist.data !== undefined && watchlist.data.items.length === 0 && (
        <EmptyState
          icon={<span className="font-mono text-xs">★</span>}
          title="Nothing watched — add a symbol to follow it"
          hint="Watched securities are price-tracked without needing a position."
          preview={{
            src: "/help/investments.png",
            alt: "The Investments page with holdings and totals",
          }}
          action={{
            label: "Add a symbol",
            onClick: () => {
              const input = document.querySelector<HTMLInputElement>(
                'input[placeholder="Add a symbol to follow…"]',
              );
              input?.focus();
            },
            ariaLabel: "Add a symbol — Nothing watched",
          }}
        />
      )}
      {watchlist.data !== undefined && watchlist.data.items.length > 0 && (
        <ul className="divide-y divide-veil/[0.05]">
          {watchlist.data.items.map((item) => (
            <WatchRow
              key={item.security_id}
              item={item}
              onRemove={() => remove.mutate(item.security_id)}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

export function WatchRow({
  item,
  onRemove,
}: {
  item: WatchlistItem;
  onRemove: () => void;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // Same staleness treatment as positions (docs/09): pulsing danger badge for
  // stale prices, source·age badge otherwise, muted note when unpriced.
  //
  // M141: a dead symbol says "no public quote" — "no price" reads as waiting,
  // which is exactly the false promise this card exists to remove.
  //
  // Two labels, two truths (Option A, t_bd5ec95f): the market-date segment is
  // day-only; the fetch age renders ONLY with real provider-fetch evidence —
  // null/absent omits it entirely (t_a16b7663: never "0s", never "just now",
  // never "fetch time unavailable").
  const watchFetchAge = formatFetchAge(item.price_fetched_at);
  const badge = item.dead_symbol ? (
    // Badge takes no DOM props, so the reason rides a wrapping span's title
    // (the same convention as SecurityDetail's day-delta "live" test hook).
    <span title={item.dead_symbol_reason ?? undefined}>
      <Badge tone="danger">no public quote</Badge>
    </span>
  ) : item.price_stale === true ? (
    <Badge tone="danger" pulse>
      stale
    </Badge>
  ) : item.price_source !== null ? (
    <Badge tone="neutral">
      {sourceLabel(item.price_source)} ·{" "}
      {item.price_age_days === 0
        ? "today"
        : (item.price_date ?? "no price date")}
      {watchFetchAge !== null ? ` · fetched ${watchFetchAge} ago` : ""}
    </Badge>
  ) : (
    <span className="text-xs text-muted">no price</span>
  );

  return (
    <li data-testid={`watch-row-${item.symbol}`} className="flex items-center gap-2 py-2 text-sm">
      <span className="w-16 shrink-0 truncate font-medium">
        <Link to={`/securities/${item.security_id}`} className="text-accent hover:underline">
          {item.symbol}
        </Link>
      </span>
      <span className="min-w-0 flex-1 truncate text-muted">{item.name}</span>
      <span className="whitespace-nowrap tabular">{fmtNative(item.latest_price, item.listing_currency)}</span>
      {badge}
      <Button
        variant="ghost"
        size="sm"
        data-testid={`watch-remove-${item.symbol}`}
        onClick={onRemove}
      >
        Remove
      </Button>
    </li>
  );
}

// --- Trade entry -------------------------------------------------------------

/** The trade's default booking date is ET today (`today_et()`'s twin), not
 *  the UTC date — `toISOString()` reads TOMORROW east of UTC in the evening,
 *  which would silently book a trade a day ahead. */
const today = () => etToday();

interface TradeFormState {
  action: TradeAction;
  account_id: string;
  security_id: string;
  date: string;
  quantity: string;
  price_native: string;
  commission: string;
  fx_rate_used: string;
}

function TradeForm({ accountId }: { accountId?: number }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { investmentAccounts } = useInvestmentAccounts();
  const securities = useQuery({
    queryKey: ["securities", 200],
    queryFn: () => listSecurities(200),
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });
  const [error, setError] = useState<string | null>(null);
  const [showNewSecurity, setShowNewSecurity] = useState(false);
  // M181 (docs/33 rule 6): `POST /trades` fails closed for an opted-in tenant —
  // `trades` has no cipher columns at all, so the SERVER cannot write this row
  // encrypted whatever the browser holds. The control is therefore replaced by
  // its reason BEFORE submit, and the gate is ENROLMENT ALONE: unlocking the
  // vault cannot enable a server-side trade write (rule 7d).
  const serverWriteBlocked = useServerSideWriteBlocked();
  const [form, setForm] = useState<TradeFormState>({
    action: "buy",
    account_id: accountId !== undefined ? String(accountId) : "",
    security_id: "",
    date: today(),
    quantity: "",
    price_native: "",
    commission: "",
    fx_rate_used: "",
  });

  const create = useMutation({
    mutationFn: () => {
      const cashOnly = CASH_ONLY_ACTIONS.includes(form.action);
      const takesCommission = COMMISSION_ACTIONS.includes(form.action);
      return createTrade({
        account_id: Number(form.account_id),
        security_id: Number(form.security_id),
        action: form.action,
        date: form.date,
        // Signed-quantity convention (docs/02): sells are entered positive and
        // stored negative server-side; cash actions always send quantity 0.
        quantity: cashOnly ? "0" : form.quantity,
        price_native: form.price_native,
        commission: takesCommission && form.commission !== "" ? form.commission : undefined,
        fx_rate_used: form.fx_rate_used !== "" ? form.fx_rate_used : null,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["trades"] });
      void queryClient.invalidateQueries({ queryKey: ["positions"] });
      void queryClient.invalidateQueries({ queryKey: ["exposure"] });
      void queryClient.invalidateQueries({ queryKey: ["acb"] });
      setError(null);
      const symbol = (securities.data?.items ?? []).find(
        (s) => String(s.id) === form.security_id,
      )?.symbol;
      toast.success("Trade recorded", `${form.action.toUpperCase()}${symbol ? ` ${symbol}` : ""}`);
      setForm((prev) => ({ ...prev, quantity: "", price_native: "", commission: "", fx_rate_used: "" }));
    },
    onError: (err) => {
      setError(err.message);
      toast.error("Trade failed", err.message);
    },
  });

  const cashOnly = CASH_ONLY_ACTIONS.includes(form.action);
  const takesCommission = COMMISSION_ACTIONS.includes(form.action);
  const ready = form.account_id !== "" && form.security_id !== "" && form.date !== "" && form.price_native !== "";

  return (
    <Card hover id="trade-form" className="h-fit p-4">
      <CardTitle>Record trade</CardTitle>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          // Rule 6 defense-in-depth: the submit control is replaced below, but
          // Enter in a field would still post — refuse here too.
          if (serverWriteBlocked) return;
          create.mutate();
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <Field label="Account">
            <select
              value={form.account_id}
              onChange={(e) => setForm({ ...form, account_id: e.target.value })}
              className={FIELD}
              disabled={investmentAccounts.length === 0 || accountId !== undefined}
            >
              <option value="">Select…</option>
              {investmentAccounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
            {investmentAccounts.length === 0 && accountId === undefined && (
              <span
                data-testid="no-investment-accounts"
                className="mt-1 block text-xs text-warn"
              >
                No investment accounts — create one in Settings → Accounts
              </span>
            )}
          </Field>
          <Field label="Security">
            <SecurityPicker
              securities={securities.data?.items ?? []}
              value={form.security_id}
              onChange={(security_id) => setForm({ ...form, security_id })}
              placeholder="Search symbol or name…"
            />
            <button
              type="button"
              data-testid="new-security-toggle"
              onClick={() => setShowNewSecurity((v) => !v)}
              className="mt-1 block text-left text-xs text-accent transition-colors hover:underline"
            >
              {showNewSecurity ? "Hide new-security form" : "+ New security"}
            </button>
          </Field>
          <Field label="Action">
            <select
              value={form.action}
              onChange={(e) => setForm({ ...form, action: e.target.value as TradeAction })}
              className={FIELD}
            >
              {TRADE_ACTIONS.map((action) => (
                <option key={action} value={action}>
                  {action}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Date">
            <input
              type="date"
              value={form.date}
              onChange={(e) => setForm({ ...form, date: e.target.value })}
              className={FIELD}
            />
          </Field>
          <Field label={cashOnly ? "Gross amount" : "Price"}>
            <input
              value={form.price_native}
              onChange={(e) => setForm({ ...form, price_native: e.target.value })}
              inputMode="decimal"
              placeholder={cashOnly ? "cash amount (native)" : "per-share price"}
              className={FIELD}
            />
          </Field>
          <Field label="Quantity">
            <input
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: e.target.value })}
              inputMode="decimal"
              disabled={cashOnly}
              placeholder={cashOnly ? "—" : "shares (sells positive)"}
              className={FIELD}
            />
          </Field>
          <Field label="Commission">
            <input
              value={form.commission}
              onChange={(e) => setForm({ ...form, commission: e.target.value })}
              inputMode="decimal"
              disabled={!takesCommission}
              placeholder={takesCommission ? "optional" : "—"}
              className={FIELD}
            />
          </Field>
          <Field label="FX rate used">
            <input
              value={form.fx_rate_used}
              onChange={(e) => setForm({ ...form, fx_rate_used: e.target.value })}
              inputMode="decimal"
              placeholder="display copy only (optional)"
              className={FIELD}
            />
          </Field>
        </div>
        <p className="text-xs leading-relaxed text-muted">
          Sells: enter the share count as a positive number — it is stored negative automatically.
          Cash actions carry the gross amount in “Gross amount” with quantity 0; pool math
          derives its own FX rates, so “FX rate used” is a display copy only.
        </p>
        {error !== null && <p className="text-sm text-danger">{error}</p>}
        {/* Rule 6: the control that cannot run is REPLACED by its reason,
            before anything is typed — never a post-hoc 422 on a typed trade. */}
        {serverWriteBlocked ? (
          <ServerSideBlocked capability="write:trade" id="trade-form-blocked" />
        ) : (
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!ready}>
            Record trade
          </Button>
        )}
      </form>
      {/* M142: the new-security panel is a SIBLING of the trade form, not a
          <form> inside it. A nested <form> is invalid HTML: a real browser
          does not deliver the inner submit to React's handler, so clicking
          "Create security" performed a NATIVE submission of the OUTER form —
          the page reloaded and nothing was ever created. jsdom has no
          navigation, which is why the unit test could not see it; the live
          probe did (scripts/a11y/m142_symbol_suggestion_probe.py). */}
      {showNewSecurity && (
        <div data-testid="new-security-form" className="rounded-lg border border-border bg-veil/[0.03] p-3">
          <SecurityForm />
        </div>
      )}
    </Card>
  );
}

// --- Investment CSV Import (docs/05) ------------------------------------------


// --- Trades log --------------------------------------------------------------

function TradesLog({
  accountId,
  focus = null,
}: {
  accountId?: number;
  focus?: ValueHistoryEvent | null;
}) {
  // F-10 (docs/46 §D2): both money cells below run through `fmtNative`, so
  // this component is the subscriber that repaints them on a flip.
  useMasked();
  const [accountFilter, setAccountFilter] = useState("");
  const [securityId, setSecurityId] = useState("");
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);

  const accounts = useQuery({ queryKey: ["accounts", 200], queryFn: () => listAccounts(200) });
  const securities = useQuery({
    queryKey: ["securities", 200],
    queryFn: () => listSecurities(200),
    refetchInterval: 60000, // Auto-refresh every 60 seconds
  });

  const effectiveAccountId = accountId ?? accountFilter;
  const filters = {
    limit: PAGE_SIZE,
    offset,
    account_id: effectiveAccountId === "" ? null : Number(effectiveAccountId),
    security_id: securityId === "" ? null : Number(securityId),
    action: action === "" ? null : (action as TradeAction),
  };
  const trades = useQuery({
    queryKey: ["trades", filters],
    queryFn: () => listTrades(filters),
  });

  /** The trade a value-chart marker pointed at — ringed until another wins. */
  const [highlightId, setHighlightId] = useState<number | null>(null);

  // M118 (docs/09 §Marker → row): narrow to the flow's OWN action + security
  // so the target lands on page 1 — a contribution/transfer set is a handful
  // of rows, where the unfiltered log would put an old row pages away.
  useEffect(() => {
    if (focus === null || focus.trade_id === null) return;
    setAction(focus.action ?? "");
    setSecurityId(focus.security_id === null ? "" : String(focus.security_id));
    setOffset(0);
    setHighlightId(focus.trade_id);
  }, [focus]);

  // Runs again once the narrowed query resolves — the row only exists then.
  useEffect(() => {
    if (highlightId === null) return;
    document
      .querySelector(`[data-trade-id="${highlightId}"]`)
      ?.scrollIntoView?.({ block: "center" });
  }, [highlightId, trades.data]);

  const accountName = new Map((accounts.data?.items ?? []).map((a) => [a.id, a.name]));
  const accountCurrency = new Map((accounts.data?.items ?? []).map((a) => [a.id, a.currency]));
  const securitySymbol = new Map((securities.data?.items ?? []).map((s) => [s.id, s.symbol]));
  const total = trades.data?.total ?? 0;

  /**
   * The trades log's money cell (F-10): `price_native` / `commission` are
   * STATED in the row's own currency, a pre-M132 NULL reading in its
   * ACCOUNT's currency — the exact read the server does (docs/02 §trades,
   * M132). Both cells route through `fmtNative`, the mask-aware helper the
   * positions table beside this one already uses, so a masked session prints
   * ••••• (docs/46 §D2: prices are money) and `useMasked()` above repaints
   * them on a flip. `.trimEnd()` covers the only unnameable case — an account
   * outside the 200-row lookup, where `fmtNative` has no ISO code to append —
   * so that row prints the bare figure rather than a fabricated one.
   */
  const money = (value: string, trade: Trade): string =>
    fmtNative(value, trade.currency ?? accountCurrency.get(trade.account_id) ?? "").trimEnd();

  return (
    <Card hover className="p-4">
      <CardTitle>Trades log</CardTitle>
      <div className="mb-3 flex flex-wrap gap-2 text-sm">
        {accountId === undefined && (
          <select aria-label="Filter trades by account"
            value={accountFilter}
            onChange={(e) => {
              setAccountFilter(e.target.value);
              setOffset(0);
            }}
            className="field-ring rounded-lg border border-control bg-surface-raised px-2 py-1.5 text-sm outline-none transition-colors hover:border-accent/70"
          >
            <option value="">All accounts</option>
            {(accounts.data?.items ?? []).map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
        )}
        <select aria-label="Filter trades by security"
          value={securityId}
          onChange={(e) => {
            setSecurityId(e.target.value);
            setOffset(0);
          }}
          className="field-ring rounded-lg border border-control bg-surface-raised px-2 py-1.5 text-sm outline-none transition-colors hover:border-accent/70"
        >
          <option value="">All securities</option>
          {(securities.data?.items ?? []).map((security) => (
            <option key={security.id} value={security.id}>
              {security.symbol}
            </option>
          ))}
        </select>
        <select aria-label="Filter trades by action"
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
            setOffset(0);
          }}
          className="field-ring rounded-lg border border-control bg-surface-raised px-2 py-1.5 text-sm outline-none transition-colors hover:border-accent/70"
        >
          <option value="">All actions</option>
          {TRADE_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>

      {trades.isPending && <SkeletonRows rows={5} />}
      {trades.isError && <p className="text-sm text-danger">{trades.error.message}</p>}
      {trades.data !== undefined && trades.data.items.length === 0 && (
        <EmptyState
          icon={<span className="font-mono text-xs">↔</span>}
          title="No trades match these filters."
          hint="Recorded trades appear here with their full audit detail."
          preview={{
            src: "/help/investments.png",
            alt: "The Investments page with holdings and totals",
          }}
          action={{
            label: "Clear all filters",
            onClick: () => {
              setAccountFilter("");
              setSecurityId("");
              setAction("");
              setOffset(0);
            },
            ariaLabel: "Clear all filters — No trades match these filters.",
          }}
        />
      )}

      {trades.data !== undefined && trades.data.items.length > 0 && (
        <>
          <div role="region" aria-label="Trades log table" tabIndex={0} className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Account</th>
                  <th className="px-3 py-2 font-medium">Security</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 text-right font-medium">Quantity</th>
                  <th className="px-3 py-2 text-right font-medium">Price / amount</th>
                  <th className="px-3 py-2 text-right font-medium">Commission</th>
                </tr>
              </thead>
              <tbody>
                {trades.data.items.map((trade) => (
                  <tr
                    key={trade.id}
                    data-trade-id={trade.id}
                    aria-current={highlightId === Number(trade.id) ? "true" : undefined}
                    className={`border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]${
                      highlightId === Number(trade.id) ? " bg-accent/10 ring-1 ring-inset ring-accent" : ""
                    }`}
                  >
                    <td className="whitespace-nowrap px-3 py-2 tabular">{trade.date}</td>
                    <td className="px-3 py-2">{accountName.get(trade.account_id) ?? trade.account_id}</td>
                    <td className="px-3 py-2 font-medium">
                      {securitySymbol.get(trade.security_id) ?? trade.security_id}
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={trade.quantity.startsWith("-") ? "danger" : "success"} dot={false}>
                        {trade.action}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-right tabular">{fmtQuantity(trade.quantity)}</td>
                    <td className="px-3 py-2 text-right tabular">{money(trade.price_native, trade)}</td>
                    <td className="px-3 py-2 text-right tabular">
                      {/* A zero commission is an em dash unmasked — a display
                          convention, not a figure the mask may skip: masked,
                          it is bullets like every other amount here. */}
                      {trade.commission === "0"
                        ? isMasked()
                          ? MASK_TEXT
                          : "—"
                        : money(trade.commission, trade)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex items-center justify-end gap-3 text-sm text-muted">
            <span className="tabular">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Prev
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}