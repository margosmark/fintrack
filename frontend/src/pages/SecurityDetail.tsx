import { useState, useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Inbox, RefreshCw, Trash2 } from "lucide-react";
import {
  getSecurityOverview,
  importHoldings,
  putHoldings,
  refreshSecurity,
  updateFundamentals,
} from "../api/securities";
import type { EtfHolding, SecurityOverview } from "../api/securities";
import { addWatchlistEntry, removeWatchlistEntry } from "../api/watchlist";
import { useInvestmentAccounts } from "../api/accounts";
import { recordDividend } from "../api/trades";
import { apiErrorMessage } from "../components/transactions/format";
import { PriceHistoryPanel } from "../components/charts/PriceHistoryPanel";
import { Button } from "../components/ui/Button";
import { fmtCad, fmtNative, fmtQuantity, sourceLabel } from "../components/charts/format";
import {
  dayDelta,
  deltaArrow,
  fmtCadSigned,
  fmtSigned,
  fmtSignedPct,
  fmtSignedPctBookCost,
  liveImpact,
  type DayDelta,
} from "../lib/impact";
import { Badge } from "../components/ui/Badge";
import type { BadgeTone } from "../components/ui/Badge";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { Card, CardTitle } from "../components/ui/Card";
import { SkeletonCard } from "../components/ui/Skeleton";
import { Toggle } from "../components/ui/Toggle";
import { useToast } from "../components/ui/Toast";
import { usePriceStream, type StreamStatus } from "../hooks/usePriceStream";
import { useAuth } from "../auth/AuthContext";
import { SSEStatusIndicator } from "../components/ui/SSEStatusIndicator";
import {
  ServerSideBlocked,
  useServerSideWriteBlocked,
} from "../components/e2ee/ServerSideBlocked";
import { MASK_TEXT, isMasked, useMasked } from "../lib/mask";import { etToday } from "../lib/budgets";

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors disabled:cursor-not-allowed disabled:opacity-40 hover:border-accent/70";

/**
 * Security dashboard page (docs/09): everything the backend's one-payload
 * overview returns — identity + watchlist toggle, latest price with source and
 * staleness, 52-week stats, cached-close history chart, per-account holdings
 * with totals, and recent trades. Unknown/foreign ids surface the API error.
 */
export function SecurityDetail() {
  const { user } = useAuth();
  const { securityId } = useParams();
  const id = Number(securityId);
  const valid = Number.isInteger(id) && id > 0;
  
  // Memoize securityIds array to prevent unnecessary SSE reconnections
  const securityIds = useMemo(() => (valid ? [id] : []), [id, valid]);
  
  // Subscribe to cached price updates via the SSE transport.
  const { status: streamStatus, lastEventAt } = usePriceStream(securityIds, {
    privacyMode: user?.privacy_mode === true,
  });
  
  // Backfill watch: a just-added security has no cached closes until the
  // background price fetch lands. Poll every 5 s while empty, for at most
  // 3 minutes from mount — symbols with no market data stop polling too.
  const [pollUntil] = useState(() => Date.now() + 3 * 60_000);
  const overview = useQuery({
    queryKey: ["security-overview", id],
    queryFn: () => getSecurityOverview(id),
    enabled: valid,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data === undefined || data.stats.cached_days > 0) return false;
      // M141: a dead symbol's backfill is never going to land — polling it
      // would just re-read the same "no prices" payload every 5 s for three
      // minutes (docs/07 §Dead symbols).
      if (data.security.dead_symbol) return false;
      return Date.now() < pollUntil ? 5000 : false;
    },
  });

  // M95 P2: ONE refresh for the whole page. The header button and the two
  // price empty states ("No prices yet", "No cached closes yet") all trigger
  // the same mutation, so an empty state's action is the real control, not a
  // link to another page.
  const queryClient = useQueryClient();
  const toast = useToast();
  const refresh = useMutation({
    mutationFn: () => refreshSecurity(id),
    onSuccess: () => {
      toast.success("Market data refreshed");
      void queryClient.invalidateQueries({ queryKey: ["security-overview", id] });
      // The chart reads GET /prices/history, not the overview payload — the
      // same button has to invalidate BOTH or the chart sits on stale closes.
      void queryClient.invalidateQueries({ queryKey: ["price-history"] });
    },
    onError: (err) => toast.error("Refresh failed", apiErrorMessage(err)),
  });
  const onRefresh = () => refresh.mutate();

  if (!valid || overview.isError) {
    return (
      <section className="space-y-6">
        <h1 className="text-xl font-semibold tracking-tight">Security</h1>
        <ErrorPanel
          message={overview.isError ? apiErrorMessage(overview.error) : "Unknown security."}
        />
        {/* M68 recovery affordance: a dead/foreign symbol id must not be a
            dead end — the holdings table it came from is one click away. */}
        <Link
          to="/investments"
          className="inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-control bg-transparent px-3.5 text-sm font-medium text-text transition-colors duration-200 hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Back to Investments
        </Link>
      </section>
    );
  }
  if (overview.isPending || overview.data === undefined) {
    return (
      <section className="space-y-6">
        <SkeletonCard className="h-28" />
        <div className="grid gap-6 lg:grid-cols-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      </section>
    );
  }

  const data = overview.data;

  return (
    <section className="space-y-6">
      <HeaderCard
        data={data}
        streamStatus={streamStatus}
        lastEventAt={lastEventAt}
        onRefresh={onRefresh}
        refreshing={refresh.isPending}
      />
      <div className="grid items-start gap-6 lg:grid-cols-2">
        <div className="space-y-6">
          <PriceCard
            data={data}
            streamStatus={streamStatus}
            lastEventAt={lastEventAt}
            onRefresh={onRefresh}
            refreshing={refresh.isPending}
          />
          {/* M129: suppressed entirely when nothing is held — a "You hold"
              row for zero shares would be a fiction (docs/09). */}
          {data.positions.length > 0 && <ImpactCard data={data} />}
        </div>
        <StatsStrip data={data} />
      </div>
      <ChartCard data={data} onRefresh={onRefresh} />
      <FundamentalsCard data={data} />
      {(data.security.kind === "etf" || data.security.kind === "mutual_fund") && (
        <CompositionCard data={data} />
      )}
      <HoldingsCard data={data} />
      <RecordDividendCard securityId={data.security.id} symbol={data.security.symbol} />
      <TradesCard data={data} />
    </section>
  );
}

// --- Header -------------------------------------------------------------------

function HeaderCard({
  data,
  streamStatus,
  lastEventAt,
  onRefresh,
  refreshing,
}: {
  data: SecurityOverview;
  streamStatus: StreamStatus;
  lastEventAt: Date | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const toggleWatch = useMutation({
    mutationFn: async (next: boolean) => {
      if (next) await addWatchlistEntry(data.security.id);
      else await removeWatchlistEntry(data.security.id);
    },
    onSuccess: (_result, next) => {
      toast.info(
        next
          ? `${data.security.symbol} added to watchlist`
          : `${data.security.symbol} removed from watchlist`,
      );
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      void queryClient.invalidateQueries({ queryKey: ["security-overview", data.security.id] });
    },
    onError: (err) => toast.error("Watchlist update failed", err.message),
  });

  return (
    <Card hover className="flex flex-wrap items-center gap-4 p-4">
      {/* min-w-40 (not min-w-0): below ~420px the Refresh/Watching controls
          would otherwise squeeze the issuer-name column to <50px and wrap the
          name to 4 lines — the min-width makes the controls wrap onto their
          own row instead (flex-wrap on the Card). */}
      <div className="min-w-40 flex-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{data.security.symbol}</h1>
          <Badge tone="accent">{data.security.kind}</Badge>
          <SSEStatusIndicator status={streamStatus} lastEventAt={lastEventAt} />
        </div>
        {/* No truncate: long issuer names wrap instead of losing characters. */}
        <p className="mt-1 text-sm text-muted">{data.security.name}</p>
        <p className="mt-0.5 text-xs text-muted">
          {/* M143 (docs/07 §Unresolved symbols): an empty listing currency is
              the honest "we never identified this instrument" state — never a
              guessed venue/currency. The row stays manual-entry only until a
              refresh proves a listing exists. */}
          {data.security.listing_currency ? (
            <>
              {data.security.exchange} · {data.security.listing_currency}
            </>
          ) : (
            <span data-testid="unresolved-symbol" className="text-warn">
              Unresolved symbol — no market listing found, enter prices by hand
            </span>
          )}
        </p>
      </div>
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label="Refresh market data"
        >
          <RefreshCw className={`size-4 ${refreshing ? "animate-spin" : ""}`} />
          <span className="ml-2">Refresh</span>
        </Button>
        <Toggle
          checked={data.watchlisted}
          disabled={toggleWatch.isPending}
          onChange={(next) => toggleWatch.mutate(next)}
          label="Watching"
          ariaLabel={`Watch ${data.security.symbol}`}
        />
      </div>
    </Card>
  );
}

// --- Latest price -------------------------------------------------------------

function PriceCard({
  data,
  streamStatus,
  lastEventAt,
  onRefresh,
  refreshing,
}: {
  data: SecurityOverview;
  streamStatus: StreamStatus;
  lastEventAt: Date | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { latest } = data;
  const symbol = data.security.symbol;
  return (
    <Card className="p-4">
      <CardTitle>Latest price</CardTitle>
      {latest === null && data.security.dead_symbol ? (
        /* M141 (docs/09 §No public quote): the symbol has been declared
           quote-less by Yahoo itself, so the state says so instead of the
           forever-"yet" promise. "Refresh prices" is DISABLED WITH THE REASON
           (the M140 G2 pattern) because it cannot deliver; "Try refresh" stays
           as the one explicit path that can still probe the row and revive it
           if a listing ever appears. */
        <div className="space-y-4">
          <EmptyState
            icon={<Inbox />}
            title={`No public quote for ${symbol}`}
            hint={`${data.security.dead_symbol_reason ?? "Yahoo returned no quote for this symbol."} It may be delisted or private — this symbol will not track unless a public listing appears.`}
            preview={{
              src: "/help/security-detail.png",
              alt: "A security page with price history and holdings",
            }}
            action={{
              label: "Refresh prices",
              onClick: onRefresh,
              disabledReason:
                "Yahoo has no public quote for this symbol, so refreshing cannot produce a price.",
            }}
          />
          <div className="flex justify-center">
            <Button
              variant="outline"
              size="sm"
              onClick={onRefresh}
              loading={refreshing}
              data-testid="try-refresh"
            >
              Try refresh
            </Button>
          </div>
        </div>
      ) : latest === null ? (
        <EmptyState
          icon={<Inbox />}
          title="No prices yet"
          hint="Prices appear here after a market-data refresh — nothing has been fetched for this symbol yet."
          preview={{
            src: "/help/security-detail.png",
            alt: "A security page with price history and holdings",
          }}
          action={{ label: "Refresh prices", onClick: onRefresh }}
        />
      ) : (
        <div className="space-y-2">
          <p className="text-3xl font-semibold tabular">
            {fmtNative(latest.price, data.security.listing_currency)}
          </p>
          <DayDeltaLine data={data} />
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="neutral">{sourceLabel(latest.source)}</Badge>
            {latest.stale && (
              <Badge tone="danger" pulse>
                stale · {latest.age_days}d old
              </Badge>
            )}
            <span className="text-xs text-muted">as of {latest.date}</span>
            {streamStatus === "connected" && lastEventAt && (
              <span className="text-xs text-muted">
                · stream message {lastEventAt.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

/**
 * The daily delta line (M129, docs/09 §Day change): ▲ +1.43 (+0.27%) vs the
 * previous trading day's close, house-coloured, tabular. At rest the numbers
 * are the server's own (`day_change_native` / `day_change_pct`); while the
 * stream's `livePatchedAt` marker rides the payload they are re-evaluated
 * from the patched price and the SAME server baseline, and the line says
 * "live" so the provisional state is stated, not implied.
 */
function DayDeltaLine({ data }: { data: SecurityOverview }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { latest } = data;
  if (latest === null) return null;
  const live = data.livePatchedAt !== undefined;
  const delta: DayDelta | null = live
    ? dayDelta(latest.price, latest.previous_close)
    : latest.day_change_native === null
      ? null
      : { change: Number(latest.day_change_native), pct: latest.day_change_pct };
  if (delta === null) return null; // first-day listing: no prior close, no line
  return (
    <p className="flex items-center gap-2 text-sm tabular" data-testid="day-delta">
      <span
        className={
          delta.change < 0 ? "text-danger" : delta.change > 0 ? "text-gain" : "text-muted"
        }
      >
        {deltaArrow(delta.change)} {fmtSigned(delta.change)}
        {delta.pct !== null && ` (${fmtSignedPct(delta.pct)})`}
      </span>
      <span className="text-xs text-muted">vs previous close</span>
      {/* The "live" word is the test hook — Badge takes no DOM props. */}
      {live && <Badge tone="accent">live</Badge>}
    </p>
  );
}

/**
 * "Your position" impact card (M129, docs/09 §Own-position impact): what the
 * day's move and the whole holding are worth to THIS user, aggregated across
 * every account that holds the security (the Holdings table below carries
 * the per-account split). At rest every figure is the server's; while the
 * stream's marker rides the payload the price-derived figures re-evaluate
 * live from qty × price × the server's own rate and say so. Book cost is
 * never re-evaluated — it is history, not a quote.
 */
function ImpactCard({ data }: { data: SecurityOverview }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const live = data.livePatchedAt !== undefined;
  const latest = data.latest;
  const impact = live
    ? liveImpact(data.positions, latest?.price ?? null, latest?.previous_close ?? null)
    : null;

  const book = fmtCad(data.totals.book_cost_cad);
  const market = impact
    ? fmtCad(impact.marketValueCad === null ? null : String(impact.marketValueCad))
    : fmtCad(data.totals.market_value_cad);
  const gainCad = impact
    ? impact.unrealizedCad
    : data.totals.unrealized_gain_cad === null
      ? null
      : Number(data.totals.unrealized_gain_cad);
  const gainPct = impact ? impact.unrealizedPct : data.totals.unrealized_gain_pct;
  const dayCad = impact ? impact.dayCad : data.totals.day_change_cad === null ? null : Number(data.totals.day_change_cad);
  const gainNative = data.totals.unrealized_gain_native;
  const delta = latest === null ? null : dayDelta(latest.price, latest.previous_close);

  return (
    <Card hover className="p-4" data-testid="impact-card">
      <div className="flex items-center gap-2">
        <CardTitle>Your position</CardTitle>
        {live && <Badge tone="accent">live</Badge>}
      </div>
      <p className="mt-2 text-sm" data-testid="impact-headline">
        You hold <span className="font-medium tabular">{fmtQuantity(data.totals.quantity)}</span>{" "}
        {data.security.symbol} · book <span className="tabular">{book}</span> · market{" "}
        <span className="tabular">{market}</span> ·{" "}
        <span
          className={`tabular font-medium ${gainCad !== null && gainCad < 0 ? "text-danger" : "text-gain"}`}
          data-testid="impact-gain"
        >
          {fmtCadSigned(gainCad)}
          {/* M139 carve-out — book-cost derived, same class as Investments'
              position gain %: this percentage follows the session mask. */}
          {gainPct !== null && ` (${fmtSignedPctBookCost(gainPct)})`}
        </span>{" "}
        since first buy
        {!live && gainNative !== null && (
          <span className="text-muted">
            {" "}
            · {fmtSigned(Number(gainNative))} {data.security.listing_currency}
          </span>
        )}
      </p>
      {dayCad !== null && delta !== null && (
        <p className="mt-1 text-xs text-muted" data-testid="impact-day">
          Today:{" "}
          <span className={`tabular ${dayCad < 0 ? "text-danger" : "text-gain"}`}>
            {fmtCadSigned(dayCad)}
          </span>{" "}
          — {fmtQuantity(data.totals.quantity)} × {fmtSigned(delta.change)}{" "}
          {data.security.listing_currency} per share
        </p>
      )}
      <p className="mt-2 text-[11px] leading-snug text-muted">
        {live
          ? "Live from the price stream: market value and today's impact re-evaluate as quotes arrive; book cost and the server's own figures replace them on the next refresh."
          : "Book cost is your average cost basis (CAD); market value and gains are computed server-side from cached closes."}
      </p>
    </Card>
  );
}

// --- 365-day stats ------------------------------------------------------------

function StatsStrip({ data }: { data: SecurityOverview }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const changePct = data.stats.change_pct;
  const change =
    changePct === null ? (
      <span className="text-muted">—</span>
    ) : (
      <span className={changePct < 0 ? "text-danger" : changePct > 0 ? "text-gain" : ""}>
        {changePct > 0 ? "+" : ""}
        {changePct.toFixed(2)}%
      </span>
    );

  const cells = [
    { label: "52w high", value: fmtNative(data.stats.high_52w, data.security.listing_currency) },
    { label: "52w low", value: fmtNative(data.stats.low_52w, data.security.listing_currency) },
    { label: "365d change", value: change },
    { label: "Cached closes", value: String(data.stats.cached_days) },
  ];

  return (
    <Card className="p-4">
      <CardTitle>365-day stats</CardTitle>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
        {cells.map((cell) => (
          <div key={cell.label}>
            <dt className="text-[11px] uppercase tracking-widest text-muted">{cell.label}</dt>
            <dd className="mt-0.5 text-sm font-medium tabular">{cell.value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

// --- History chart ------------------------------------------------------------

function ChartCard({ data, onRefresh }: { data: SecurityOverview; onRefresh: () => void }) {
  const dead = data.security.dead_symbol;
  return (
    <Card className="p-4">
      {/* M126 (docs/12 §Chart ranges): the window is now the CONTROL's to
          choose, so the title can no longer promise "last 365 days". The
          empty copy is unchanged — it is the documented action path for a
          security whose first backfill has not landed (docs/09).

          M141: a dead symbol gets the honest title and NO enabled refresh —
          the same "backfills up to a year of daily closes" promise would be
          just as false here as on the price card above. */}
      <PriceHistoryPanel
        securityId={data.security.id}
        symbol={data.security.symbol}
        currency={data.security.listing_currency}
        onRefresh={dead ? undefined : onRefresh}
        watchBackfill={!dead}
        emptyTitle={dead ? `No public quote for ${data.security.symbol}` : "No cached closes yet"}
        emptyHint={
          dead
            ? `${data.security.dead_symbol_reason ?? "Yahoo returned no quote for this symbol."} It may be delisted or private, so there are no closes to chart.`
            : "A market-data refresh caches up to a year of daily closes for this chart."
        }
      />
    </Card>
  );
}

// --- Fundamentals --------------------------------------------------------------

const FUNDAMENTALS_INPUT =
  "field-ring w-full rounded-lg border border-control bg-surface-raised px-2 py-1 text-sm tabular outline-none transition-colors hover:border-accent/70";

/**
 * Compact display for research numbers: strips Decimal serialization noise
 * ("11.33000000" → "11.33") and abbreviates caps ("287785156608.00" →
 * "287.79B"). Unmasked, the trimmed/abbreviated string is what reaches both the
 * Pair value and its tooltip.
 *
 * F-12 (docs/46 §D2): these ARE money — a per-share figure, a market cap, the
 * 52-week range — so a masked session gets `•••••` here exactly as it does on
 * the stats strip above (`fmtNative`). Before F-12 this helper never asked the
 * mask, so the card printed `EPS 15.79 / MARKET CAP 386.27B` (and the same
 * strings again in six `title=` tooltips) directly beneath the stats card
 * hiding the identical figure class — the sibling-contradiction shape behind
 * F-9/F-10/F-11, and invisible to `scripts/sweep_money.mjs` because the
 * interpolation never named a tracked helper.
 *
 * `masked` is optional exactly like every other formatter's (docs/46 §D6):
 * omitted means "consult the ambient mask", so a new call site fails closed.
 * The check runs FIRST — while masked nothing this helper holds is rendered,
 * not even the string a non-numeric value would have passed through — and the
 * abbreviation below is unchanged for an unmasked session.
 */
function fmtResearch(raw: string, masked?: boolean): string {
  if (masked ?? isMasked()) return MASK_TEXT;
  const n = Number(raw);
  if (!Number.isFinite(n)) return raw;
  const abs = Math.abs(n);
  if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  return String(Number(n.toFixed(4)));
}

/** Editable fundamentals fields; empty input clears the stored value to null. */
const FUNDAMENTAL_FIELDS = [
  { key: "trailing_eps", label: "EPS" },
  { key: "pe_ratio", label: "P/E" },
  { key: "market_cap", label: "Market cap" },
  { key: "dividend_yield_pct", label: "Div yield %" },
  { key: "beta", label: "Beta" },
  { key: "sector", label: "Sector" },
  { key: "industry", label: "Industry" },
  { key: "country", label: "Country" },
  { key: "fifty_two_week_low", label: "52w low" },
  { key: "fifty_two_week_high", label: "52w high" },
] as const;

type FundamentalFieldKey = (typeof FUNDAMENTAL_FIELDS)[number]["key"];

function FundamentalsCard({ data }: { data: SecurityOverview }) {
  // M140 / F-12: re-render on a mask flip — the research figures below run
  // through `fmtResearch`, which reads the ambient mask at call time, so this
  // component only has to re-render (docs/46 §D3/§D6). Before F-12 it never
  // subscribed, which is why the card kept printing raw figures under a masked
  // stats strip.
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Partial<Record<FundamentalFieldKey, string>>>({});
  const f = data.fundamentals;

  const startEdit = () => {
    const initial: Partial<Record<FundamentalFieldKey, string>> = {};
    for (const { key } of FUNDAMENTAL_FIELDS) {
      initial[key] = f?.[key] ?? "";
    }
    setDraft(initial);
    setEditing(true);
  };

  const save = useMutation({
    mutationFn: () => {
      const patch: Partial<Record<FundamentalFieldKey, string | null>> = {};
      for (const { key } of FUNDAMENTAL_FIELDS) {
        const raw = draft[key]?.trim() ?? "";
        patch[key] = raw === "" ? null : raw;
      }
      return updateFundamentals(data.security.id, patch);
    },
    onSuccess: () => {
      toast.success("Fundamentals saved");
      setEditing(false);
      void queryClient.invalidateQueries({
        queryKey: ["security-overview", data.security.id],
      });
    },
    onError: (err) => toast.error("Fundamentals save failed", apiErrorMessage(err)),
  });

  const range =
    f?.fifty_two_week_low != null && f?.fifty_two_week_high != null
      ? `${fmtResearch(f.fifty_two_week_low)} – ${fmtResearch(f.fifty_two_week_high)}`
      : null;

  return (
    <Card hover className="p-4">
      <div className="flex items-center justify-between gap-3">
        <CardTitle>Fundamentals</CardTitle>
        {editing ? (
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={save.isPending}
              onClick={() => save.mutate()}
            >
              Save
            </Button>
          </div>
        ) : (
          <Button variant="ghost" size="sm" onClick={startEdit}>
            {f === null ? "Enter manually" : "Edit"}
          </Button>
        )}
      </div>

      {editing ? (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
          {FUNDAMENTAL_FIELDS.map(({ key, label }) => (
            <div key={key}>
              <dt className="text-[11px] uppercase tracking-widest text-muted">{label}</dt>
              <dd className="mt-1">
                <input
                  className={FUNDAMENTALS_INPUT}
                  value={draft[key] ?? ""}
                  placeholder="—"
                  onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
                />
              </dd>
            </div>
          ))}
        </dl>
      ) : f === null ? (
        <EmptyState
          icon={<Inbox />}
          title="No fundamentals available"
          hint="Fetch one via a portfolio refresh, or enter values manually."
          preview={{
            src: "/help/security-detail.png",
            alt: "A security page with price history and holdings",
          }}
          action={{
            label: "Enter manually",
            onClick: startEdit,
            ariaLabel: "Enter manually — No fundamentals available",
          }}
        />
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
          <Pair label="EPS" value={f.trailing_eps == null ? null : fmtResearch(f.trailing_eps)} />
          <Pair label="P/E" value={f.pe_ratio == null ? null : fmtResearch(f.pe_ratio)} />
          <Pair label="Market cap" value={f.market_cap == null ? null : fmtResearch(f.market_cap)} />
          <Pair
            label="Div yield %"
            value={f.dividend_yield_pct == null ? null : fmtResearch(f.dividend_yield_pct)}
          />
          <Pair label="Beta" value={f.beta == null ? null : fmtResearch(f.beta)} />
          <Pair label="Sector" value={f.sector} />
          <Pair label="Industry" value={f.industry} />
          <Pair label="Country" value={f.country} />
          <Pair label="52w range" value={range} />
          <div>
            <dt className="text-[11px] uppercase tracking-widest text-muted">Source</dt>
            <dd className="mt-0.5">
              <Badge tone={f.source === "manual" ? "accent" : "neutral"}>{sourceLabel(f.source)}</Badge>
            </dd>
          </div>
        </dl>
      )}
    </Card>
  );
}

/** One label/value pair; em dash for missing data. */
function Pair({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-widest text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-sm font-medium tabular" title={value ?? undefined}>
        {value ?? "—"}
      </dd>
    </div>
  );
}

// --- Fund composition (M17) ----------------------------------------------------

const ASSET_CLASSES = ["stock", "bond", "cash", "commodity", "crypto", "other"] as const;
type AssetClass = (typeof ASSET_CLASSES)[number];

const ASSET_CLASS_TONE: Record<AssetClass, BadgeTone> = {
  stock: "accent",
  bond: "success",
  cash: "neutral",
  commodity: "warn",
  crypto: "danger",
  other: "neutral",
};

/** One editable composition row; empty strings clear optional fields. */
interface DraftRow {
  holding_symbol: string;
  holding_name: string;
  asset_class: AssetClass;
  region: string;
  country: string;
  sector: string;
  weight_pct: string;
}

function rowToDraft(row: EtfHolding): DraftRow {
  return {
    holding_symbol: row.holding_symbol ?? "",
    holding_name: row.holding_name,
    asset_class: row.asset_class,
    region: row.region ?? "",
    country: row.country ?? "",
    sector: row.sector ?? "",
    weight_pct: String(Number(row.weight_pct)),
  };
}

/**
 * Fund composition (docs/05 M17): cached top holdings with asset class and
 * weights, an optional market-data import, and a minimal manual editor.
 * Weights under 100% are legal — the residual is uncategorized exposure.
 */
function CompositionCard({ data }: { data: SecurityOverview }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<DraftRow[]>([]);
  const [recursive, setRecursive] = useState(false);
  const block = data.holdings;

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["security-overview", data.security.id] });

  const startEdit = () => {
    setDraft((block?.items ?? []).map(rowToDraft));
    setEditing(true);
  };

  const save = useMutation({
    mutationFn: () =>
      putHoldings(
        data.security.id,
        draft
          .filter((row) => row.holding_name.trim() !== "")
          .map((row) => ({
            holding_symbol: row.holding_symbol.trim() || null,
            holding_name: row.holding_name.trim(),
            asset_class: row.asset_class,
            region: row.region.trim() || null,
            country: row.country.trim() || null,
            sector: row.sector.trim() || null,
            weight_pct: row.weight_pct,
          })),
      ),
    onSuccess: () => {
      toast.success("Holdings saved");
      setEditing(false);
      void invalidate();
    },
    onError: (err) => toast.error("Holdings save failed", apiErrorMessage(err)),
  });

  const importRows = useMutation({
    mutationFn: () => importHoldings(data.security.id),
    onSuccess: () => {
      toast.success("Imported from market data");
      void invalidate();
    },
    onError: (err) => toast.error("Import failed", apiErrorMessage(err)),
  });

  const total = block ? Number(block.total_weight_pct) : 0;
  const unclassified = Math.max(0, 100 - total);
  const maxWeight = Math.max(...(block?.items.map((r) => Number(r.weight_pct)) ?? [0]), 0.01);

  return (
    <Card hover className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <CardTitle>Holdings composition</CardTitle>
          {block?.source && (
            <Badge tone={block.source === "manual" ? "accent" : "neutral"}>
              {sourceLabel(block.source)}
            </Badge>
          )}
        </div>
        {editing ? (
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={save.isPending}
              onClick={() => save.mutate()}
            >
              Save
            </Button>
          </div>
        ) : (
          <div className="flex gap-2">
            <Button
              variant={recursive ? "primary" : "ghost"}
              size="sm"
              onClick={() => setRecursive(!recursive)}
            >
              {recursive ? "Recursive: On" : "Recursive: Off"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              loading={importRows.isPending}
              onClick={() => importRows.mutate()}
            >
              Import from market data
            </Button>
            <Button variant="ghost" size="sm" onClick={startEdit}>
              Edit manually
            </Button>
          </div>
        )}
      </div>

      {editing ? (
        <div className="mt-3 space-y-2">
          {draft.map((row, index) => (
            <div
              key={index}
              className="grid grid-cols-[1fr_2fr_1fr_1fr_1fr_auto] items-center gap-2"
            >
              <input
                className={FUNDAMENTALS_INPUT}
                value={row.holding_symbol}
                placeholder="Symbol"
                aria-label="Holding symbol"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) => (i === index ? { ...r, holding_symbol: e.target.value } : r)),
                  )
                }
              />
              <input
                className={FUNDAMENTALS_INPUT}
                value={row.holding_name}
                placeholder="Name"
                aria-label="Holding name"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) => (i === index ? { ...r, holding_name: e.target.value } : r)),
                  )
                }
              />
              <select
                className={FUNDAMENTALS_INPUT}
                value={row.asset_class}
                aria-label="Asset class"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) =>
                      i === index ? { ...r, asset_class: e.target.value as AssetClass } : r,
                    ),
                  )
                }
              >
                {ASSET_CLASSES.map((ac) => (
                  <option key={ac} value={ac}>
                    {ac}
                  </option>
                ))}
              </select>
              <input
                className={FUNDAMENTALS_INPUT}
                value={row.sector}
                placeholder="Sector"
                aria-label="Sector"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) => (i === index ? { ...r, sector: e.target.value } : r)),
                  )
                }
              />
              <input
                className={FUNDAMENTALS_INPUT}
                value={row.region}
                placeholder="Region"
                aria-label="Region"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) => (i === index ? { ...r, region: e.target.value } : r)),
                  )
                }
              />
              <input
                className={`${FUNDAMENTALS_INPUT} w-20`}
                type="number"
                min="0"
                max="100"
                step="0.0001"
                value={row.weight_pct}
                placeholder="Weight %"
                aria-label="Weight percent"
                onChange={(e) =>
                  setDraft((rows) =>
                    rows.map((r, i) => (i === index ? { ...r, weight_pct: e.target.value } : r)),
                  )
                }
              />
              <button
                type="button"
                className="text-muted transition-colors hover:text-danger"
                aria-label={`Remove row ${index + 1}`}
                onClick={() => setDraft((rows) => rows.filter((_, i) => i !== index))}
              >
                <Trash2 className="size-4" />
              </button>
            </div>
          ))}
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              setDraft((rows) => [
                ...rows,
                {
                  holding_symbol: "",
                  holding_name: "",
                  asset_class: "other",
                  region: "",
                  country: "",
                  sector: "",
                  weight_pct: "",
                },
              ])
            }
          >
            + Add holding
          </Button>
        </div>
      ) : !block || block.items.length === 0 ? (
        <EmptyState
          icon={<Inbox />}
          title="No composition data"
          hint="Import from market data or add holdings manually."
          preview={{
            src: "/help/security-detail.png",
            alt: "A security page with price history and holdings",
          }}
          action={{
            label: "Edit manually",
            onClick: startEdit,
            ariaLabel: "Edit manually — No composition data",
          }}
        />
      ) : (
        <>
          <div role="region" aria-label="Holdings composition table" tabIndex={0} className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">Asset class</th>
                  <th className="px-3 py-2 font-medium">Region</th>
                  <th className="px-3 py-2 font-medium">Sector</th>
                  <th className="px-3 py-2 text-right font-medium">Weight</th>
                  {recursive && <th className="px-3 py-2 font-medium">Breakdown</th>}
                </tr>
              </thead>
              <tbody>
                {block.items.map((item, index) => (
                  <tr
                    key={`${item.holding_symbol ?? "-"}-${index}`}
                    className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
                  >
                    <td className="px-3 py-2 font-medium">
                      {item.holding_security_id ? (
                        <Link
                          to={`/securities/${item.holding_security_id}`}
                          className="text-accent hover:underline"
                        >
                          {item.holding_symbol ?? "—"}
                        </Link>
                      ) : (
                        item.holding_symbol ?? "—"
                      )}
                    </td>
                    <td className="max-w-56 truncate px-3 py-2" title={item.holding_name}>
                      {item.holding_name}
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={ASSET_CLASS_TONE[item.asset_class]}>{item.asset_class}</Badge>
                    </td>
                    <td className="px-3 py-2 text-muted">{item.region ?? "—"}</td>
                    <td className="px-3 py-2 text-muted">{item.sector ?? "—"}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-2">
                        <div className="h-1 w-24 overflow-hidden rounded bg-veil/[0.08]">
                          <div
                            className="h-full rounded bg-accent"
                            style={{ width: `${Math.min(100, (Number(item.weight_pct) / maxWeight) * 100)}%` }}
                          />
                        </div>
                        <span className="tabular">{Number(item.weight_pct).toFixed(2)}%</span>
                      </div>
                    </td>
                    {recursive && (
                      <td className="px-3 py-2 text-xs text-muted">
                        {item.asset_class_breakdown ? (
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(item.asset_class_breakdown).map(([ac, pct]) => (
                              <span key={ac} className="rounded bg-veil/[0.08] px-1.5 py-0.5">
                                {ac}: {pct}%
                              </span>
                            ))}
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 px-3 text-xs text-muted tabular">
            Total known weight: {total.toFixed(2)}% · unclassified: {unclassified.toFixed(2)}%
          </p>
        </>
      )}
    </Card>
  );
}
// --- Holdings -----------------------------------------------------------------

function HoldingsCard({ data }: { data: SecurityOverview }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  if (data.positions.length === 0) {
    return (
      <Card className="p-4">
        <CardTitle>Holdings</CardTitle>
        <EmptyState
          icon={<Inbox />}
          title="Not held — watchlist only"
          hint="Positions appear once you record trades in this security."
          preview={{
            src: "/help/security-detail.png",
            alt: "A security page with price history and holdings",
          }}
          action={{ label: "Record a trade", to: "/investments" }}
        />
      </Card>
    );
  }

  return (
    <Card hover className="p-4">
      <CardTitle>Holdings</CardTitle>
      <div role="region" aria-label="Holdings table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="px-3 py-2 font-medium">Account</th>
              <th className="px-3 py-2 text-right font-medium">Quantity</th>
              <th className="px-3 py-2 text-right font-medium">Book cost (CAD)</th>
              <th className="px-3 py-2 text-right font-medium">Market value (CAD)</th>
              <th className="px-3 py-2 text-right font-medium">Gain / loss</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((position) => (
              <tr
                key={position.account_id}
                className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
              >
                <td className="px-3 py-2">{position.account_name}</td>
                <td className="px-3 py-2 text-right tabular">{fmtQuantity(position.quantity)}</td>
                <td className="px-3 py-2 text-right tabular">{fmtCad(position.book_cost_cad)}</td>
                <td className="px-3 py-2 text-right tabular">
                  {fmtCad(position.market_value_cad)}
                </td>
                <GainCell
                  gain={position.unrealized_gain_cad}
                  pct={position.unrealized_gain_pct}
                />
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-veil/[0.08] font-medium">
              <td className="px-3 py-2">Total</td>
              <td className="px-3 py-2 text-right tabular">{fmtQuantity(data.totals.quantity)}</td>
              <td className="px-3 py-2 text-right tabular">{fmtCad(data.totals.book_cost_cad)}</td>
              <td className="px-3 py-2 text-right tabular">{fmtCad(data.totals.market_value_cad)}</td>
              <GainCell
                gain={data.totals.unrealized_gain_cad}
                pct={data.totals.unrealized_gain_pct}
              />
            </tr>
          </tfoot>
        </table>
      </div>
    </Card>
  );
}

/** mv − bc as the SERVER computed it (M129: anti-fabrication — the table
 *  renders the wire fields, never arithmetic of its own); em dash when the
 *  book cost is unknown, with the percent share beside the amount. */
function GainCell({ gain, pct }: { gain: string | null; pct: number | null }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  if (gain === null) {
    return <td className="px-3 py-2 text-right tabular text-muted">—</td>;
  }
  const value = Number(gain);
  return (
    <td
      className={`px-3 py-2 text-right tabular ${value < 0 ? "text-danger" : "text-gain"}`}
    >
      {fmtCadSigned(value)}
      {/* M139 carve-out — book-cost-derived position gain, same class as
          the Investments "Your gain %" column: follows the session mask. */}
      {pct !== null && (
        <span className="ml-1 text-xs opacity-80">({fmtSignedPctBookCost(pct)})</span>
      )}
    </td>
  );
}

// --- Recent trades ------------------------------------------------------------
// --- Record dividend ---------------------------------------------------------

function RecordDividendCard({
  securityId,
  symbol,
}: {
  securityId: number;
  symbol: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { investmentAccounts } = useInvestmentAccounts();
  // M181 (docs/33 rule 6): `POST /trades/record-dividend` is refused server-side
  // for an opted-in tenant (it writes a mandatory-plaintext row), so the submit
  // control is replaced by its reason before anything is typed. ENROLMENT
  // ALONE — unlocking cannot enable it (rule 7d).
  const serverWriteBlocked = useServerSideWriteBlocked();

  const [accountId, setAccountId] = useState<string>("");
  // ET today (`today_et()`'s twin), not the UTC date: `toISOString()` reads
  // TOMORROW east of UTC in the evening, booking the dividend a day ahead.
  const [date, setDate] = useState(() => etToday());
  const [grossAmount, setGrossAmount] = useState("");
  const [drip, setDrip] = useState(false);
  const [dripPrice, setDripPrice] = useState("");
  const [dripCommission, setDripCommission] = useState("");
  const [fractional, setFractional] = useState(true);

  const dripShares =
    drip && dripPrice !== "" && Number(dripPrice) > 0 && grossAmount !== ""
      ? fractional
        ? ((Number(grossAmount) - Number(dripCommission || "0")) / Number(dripPrice)).toFixed(6)
        : Math.floor((Number(grossAmount) - Number(dripCommission || "0")) / Number(dripPrice)).toString()
      : null;

  const record = useMutation({
    mutationFn: () =>
      recordDividend({
        account_id: Number(accountId),
        security_id: securityId,
        date,
        gross_amount: grossAmount,
        action: "dividend",
        drip,
        drip_price: drip ? dripPrice : undefined,
        drip_commission: drip && dripCommission !== "" ? dripCommission : undefined,
        fractional: drip ? fractional : undefined,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["security-overview", securityId] });
      void queryClient.invalidateQueries({ queryKey: ["trades"] });
      void queryClient.invalidateQueries({ queryKey: ["positions"] });
      toast.success("Dividend recorded", `${symbol} ${date}`);
      setGrossAmount("");
      setDrip(false);
      setDripPrice("");
      setDripCommission("");
    },
    onError: (err) => toast.error("Record failed", err.message),
  });

  const canRecord = accountId !== "" && grossAmount !== "" && Number(grossAmount) > 0;

  return (
    <Card hover className="p-4">
      <CardTitle>Record Dividend — {symbol}</CardTitle>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          // Rule 6 defense-in-depth: Enter in a field would still post.
          if (serverWriteBlocked) return;
          if (canRecord) record.mutate();
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <label className="block text-sm">
            <span className="mb-1 block text-muted">Account</span>
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className={FIELD}
              disabled={investmentAccounts.length === 0}
            >
              <option value="">Select…</option>
              {investmentAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-muted">Date</span>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className={FIELD}
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-muted">Gross amount (native)</span>
            <input
              value={grossAmount}
              onChange={(e) => setGrossAmount(e.target.value)}
              inputMode="decimal"
              placeholder="total dividend"
              className={FIELD}
            />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={drip}
            onChange={(e) => setDrip(e.target.checked)}
            className="h-4 w-4"
          />
          <span>DRIP (reinvest as shares)</span>
        </label>
        {drip && (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={fractional}
              onChange={(e) => setFractional(e.target.checked)}
              className="h-4 w-4"
            />
            <span>Fractional shares</span>
          </label>
        )}
        {drip && (
          <div className="grid grid-cols-2 gap-2">
            <label className="block text-sm">
              <span className="mb-1 block text-muted">DRIP price per share</span>
              <input
                value={dripPrice}
                onChange={(e) => setDripPrice(e.target.value)}
                inputMode="decimal"
                placeholder="reinvestment price"
                className={FIELD}
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-muted">Commission</span>
              <input
                value={dripCommission}
                onChange={(e) => setDripCommission(e.target.value)}
                inputMode="decimal"
                placeholder="0"
                className={FIELD}
              />
            </label>
          </div>
        )}
        {dripShares !== null && (
          <p className="text-xs text-muted">≈ {dripShares} shares will be purchased</p>
        )}
        {record.isError && <p className="text-sm text-danger">{record.error.message}</p>}
        {/* Rule 6: the control that cannot run is REPLACED by its reason. */}
        {serverWriteBlocked ? (
          <ServerSideBlocked capability="write:dividend" id="record-dividend-blocked" />
        ) : (
          <Button type="submit" variant="primary" loading={record.isPending} disabled={!canRecord}>
            Record dividend
          </Button>
        )}
      </form>
    </Card>
  );
}

function TradesCard({ data }: { data: SecurityOverview }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // The overview payload carries account ids only — resolve names from the
  // holdings rows (same user's accounts), falling back to #id.
  const accountNames: Record<number, string> = {};
  for (const position of data.positions) {
    accountNames[position.account_id] = position.account_name;
  }

  if (data.recent_trades.length === 0) return null;

  return (
    <Card hover className="p-4">
      <CardTitle>Recent trades</CardTitle>
      <div role="region" aria-label="Recent trades table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="px-3 py-2 font-medium">Date</th>
              <th className="px-3 py-2 font-medium">Account</th>
              <th className="px-3 py-2 font-medium">Action</th>
              <th className="px-3 py-2 text-right font-medium">Quantity</th>
              <th className="px-3 py-2 text-right font-medium">Price</th>
            </tr>
          </thead>
          <tbody>
            {data.recent_trades.map((trade) => (
              <tr key={trade.id} className="border-b border-veil/[0.05] last:border-b-0">
                <td className="whitespace-nowrap px-3 py-2 tabular">{trade.date}</td>
                <td className="px-3 py-2">{accountNames[trade.account_id] ?? `#${trade.account_id}`}</td>
                <td className="px-3 py-2 capitalize">{trade.action.replace("_", " ")}</td>
                <td className="px-3 py-2 text-right tabular">{fmtQuantity(trade.quantity)}</td>
                <td className="px-3 py-2 text-right tabular">
                  {fmtNative(trade.price_native, data.security.listing_currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
