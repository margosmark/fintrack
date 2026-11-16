import { useQuery } from "@tanstack/react-query";
import { Coins } from "lucide-react";
import { Card } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Sparkline } from "../components/ui/charts/Sparkline";
import { FxStalenessChip } from "../components/ui/FxStalenessChip";
import { getFxBoard } from "../api/fx";
import { apiErrorMessage } from "../components/transactions/format";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

// Display-only metadata for the curated board currencies (backend returns raw
// base codes). Names/symbols are a UI concern and live here, not in the API.
const CURRENCY_META: Record<string, { name: string; symbol: string }> = {
  USD: { name: "US Dollar", symbol: "US$" },
  EUR: { name: "Euro", symbol: "€" },
  GBP: { name: "British Pound", symbol: "£" },
  JPY: { name: "Japanese Yen", symbol: "¥" },
  CHF: { name: "Swiss Franc", symbol: "CHF" },
  CNY: { name: "Chinese Yuan", symbol: "¥" },
  AUD: { name: "Australian Dollar", symbol: "A$" },
  NZD: { name: "New Zealand Dollar", symbol: "NZ$" },
  HKD: { name: "Hong Kong Dollar", symbol: "HK$" },
  SGD: { name: "Singapore Dollar", symbol: "S$" },
  INR: { name: "Indian Rupee", symbol: "₹" },
  MXN: { name: "Mexican Peso", symbol: "MX$" },
  BRL: { name: "Brazilian Real", symbol: "R$" },
  ZAR: { name: "South African Rand", symbol: "R" },
  KRW: { name: "South Korean Won", symbol: "₩" },
  NOK: { name: "Norwegian Krone", symbol: "kr" },
  SEK: { name: "Swedish Krona", symbol: "kr" },
  // M151: no DKK entry — the board never returns one (Bank of Canada
  // publishes no live FXDKKCAD series, docs/07 §Curated board membership),
  // and `meta?.name ?? item.base` degrades safely if that ever changes.
};

/**
 * Currency Tracker (docs/15): read-only board of CAD-per-1-unit rates for a
 * curated set of major currencies, sourced from the shared BoC `fx_rates`
 * cache via GET /api/v1/fx/board. No persistence or per-user config.
 */
export function Currencies() {
  const { user } = useAuth();
  // Hook order must be unconditional (docs/29 F9): gate via `enabled`, not an
  // early return before useQuery.
  const enabled = user?.currency_tracker_enabled === true;
  const board = useQuery({
    queryKey: ["fx", "board"],
    queryFn: () => getFxBoard(90),
    staleTime: 60_000,
    enabled,
  });

  if (!enabled) {
    return <Navigate to="/dashboard" replace />;
  }

  return (
    <section className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Currencies</h1>
          <p className="mt-1 text-sm text-muted">
            Bank of Canada mid-market rates — CAD per 1 unit of the foreign currency.
          </p>
        </div>
        {/* M150: the board below is cache-only, so this says how old that
            cache actually is — the thing a silent gap never says. */}
        <FxStalenessChip />
      </div>

      {board.isPending && <SkeletonRows rows={6} />}
      {board.isError && (
        <p className="text-danger">{apiErrorMessage(board.error)}</p>
      )}
      {board.data && board.data.currencies.length === 0 && (
        <EmptyState
          icon={<Coins />}
          title="No rates available"
          preview={{
            src: "/help/currencies.png",
            alt: "The Currencies page with tracked rates",
          }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      )}
      {board.data && board.data.currencies.length > 0 && (
        <div className="space-y-3">
          {board.data.currencies.map((item) => {
            const meta = CURRENCY_META[item.base];
            return (
              <Card
                key={item.base}
                className="flex items-center justify-between gap-4 p-4"
              >
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wide text-muted">
                    {item.base}
                  </p>
                  <p className="truncate text-sm text-text">{meta?.name ?? item.base}</p>
                  <p className="mt-1 text-lg font-semibold text-text">
                    {item.latest_rate !== null
                      ? `1 ${item.base} = ${item.latest_rate} CAD`
                      : `1 ${item.base} = —`}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">{item.as_of ?? "no data"}</p>
                </div>
                <Sparkline values={item.series.map((s) => Number(s.rate))} />
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}
