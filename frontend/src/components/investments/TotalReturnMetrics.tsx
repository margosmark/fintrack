import { useQuery } from "@tanstack/react-query";
import { Percent } from "lucide-react";
import { getTotalReturn } from "../../api/reports";
import { Card, CardTitle } from "../ui/Card";
import { SkeletonRows } from "../ui/Skeleton";
import { EmptyState } from "../ui/EmptyState";
import { HelpHint } from "../ui/HelpHint";
import { fmtCad } from "../charts/format";

/**
 * Metrics grid (M77, docs/05 §Total return): time-weighted and money-weighted
 * PERIOD returns over the window the benchmark comparison above it uses, so
 * the two sit side by side for the same dates.
 *
 * The engines send FIXED 8-decimal strings and that shape is the wire
 * contract — it is never re-parsed or re-rounded on the way in. What the eye
 * sees is a separate, display-only concern (M105): `formatPct` SLICES the
 * string down to a 2-decimal reading (a trim, never a re-round, so no digit
 * on screen is invented), `toneOf` still reads the RAW string, and the digits
 * the trim hides stay recoverable through the value's own `HelpHint`
 * (`21.38091492% precise`) and its `aria-label`. `null` renders "No data"
 * instead of a confident 0.00% (M65 honesty rule).
 */

// M105's trim/tone/format rule moved to `lib/percent.ts` (M108) so every
// percentage surface — this card, the treemap's share readout — applies one
// implementation. Imported AND re-exported here so this module's public shape
// (and the tests that import from it) is unchanged.
import { formatPct, toneOf, trimPct } from "../../lib/percent";
import { useMasked } from "../../lib/mask";

export { formatPct, toneOf, trimPct };

/** The wire value when — and only when — the display actually hides digits
 *  from it, which is exactly when the precision affordance earns its space
 *  (M105): an untrimmed value already shows itself in full. */
function preciseOf(value: string | null): string | undefined {
  if (value === null) return undefined;
  return trimPct(value) === value ? undefined : value;
}

function Metric({
  label,
  value,
  tone = "",
  hint,
  precise,
}: {
  label: string;
  value: string;
  tone?: string;
  /** M97 honest tooltip — the derivation is NOT on this card for these four,
   *  so each label carries it (docs/09 §Tooltips & in-app help). */
  hint?: string;
  /** FULL-precision wire value behind a TRIMMED display (M105). It is the
   *  span's `aria-label`, so assistive tech reads the real number rather than
   *  display sugar, and it feeds the value's own hint — the trim is
   *  recoverable, not lossy. */
  precise?: string;
}) {
  return (
    <div>
      <div className="text-xs text-muted uppercase">
        {label}
        {hint !== undefined && <HelpHint text={hint} />}
      </div>
      <div className={`text-lg font-semibold tabular ${tone}`}>
        <span aria-label={precise !== undefined ? `${precise}%` : undefined}>{value}</span>
        {precise !== undefined && <HelpHint text={`${precise}% precise`} />}
      </div>
    </div>
  );
}

export function TotalReturnMetrics({ from, to }: { from: string; to: string }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const totalReturn = useQuery({
    queryKey: ["investments", "total-return", from, to],
    queryFn: () => getTotalReturn(from, to),
    enabled: from <= to,
    retry: false,
  });

  if (totalReturn.isPending) {
    return (
      <Card className="p-4">
        <CardTitle>Period return</CardTitle>
        <SkeletonRows rows={2} />
      </Card>
    );
  }
  if (totalReturn.isError || !totalReturn.data) {
    return (
      <Card className="p-4">
        <CardTitle>Period return</CardTitle>
        <EmptyState
          icon={<Percent />}
          title="No data"
          hint="Total return could not be computed for this window."
          preview={{ src: "/help/investments.png", alt: "The Investments page with holdings and totals" }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      </Card>
    );
  }

  const data = totalReturn.data;
  if (data.status === "no_data") {
    return (
      <Card className="p-4">
        <CardTitle>Period return</CardTitle>
        <EmptyState
          icon={<Percent />}
          title="No data"
          hint="No investment activity in this window — a return would be meaningless."
          preview={{ src: "/help/investments.png", alt: "The Investments page with holdings and totals" }}
          action={{ label: "Import a statement", to: "/imports" }}
        />
      </Card>
    );
  }

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-3">
        <CardTitle>Period return</CardTitle>
        <span className="text-xs text-muted">
          {from} — {to}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 text-center sm:grid-cols-4">
        <Metric
          label="Time-weighted (TWR)"
          value={formatPct(data.twr_pct)}
          tone={toneOf(data.twr_pct ?? "0.00000000")}
          precise={preciseOf(data.twr_pct)}
          hint="Time-weighted return: each day's move chain-linked, with money you moved in or out taken out of the calculation — deposits cannot inflate it. Shown here beside MWR for the same window: TWR is the one you compare against a benchmark, MWR the one that answers 'what did I actually earn'."
        />
        <Metric
          label="Money-weighted (MWR)"
          value={formatPct(data.mwr_pct)}
          tone={toneOf(data.mwr_pct ?? "0.00000000")}
          precise={preciseOf(data.mwr_pct)}
          hint="Money-weighted return (Modified Dietz): every deposit and withdrawal weighted by the days it stayed in the window — your own result including WHEN you moved money. The app keeps it next to TWR because neither replaces the other: MWR is yours, TWR is comparable. Neither is annualized."
        />
        <Metric
          label="Investment income"
          value={fmtCad(data.income_cad)}
          hint="Gross dividends, distributions and return of capital received in the window, converted at trade-date rates — income only; price moves are not counted here."
        />
        <Metric
          label="Net flows"
          value={fmtCad(data.net_flow_cad)}
          tone={Number(data.net_flow_cad) >= 0 ? "" : "text-danger"}
          hint="External money crossing the portfolio boundary — contributions, withdrawals and transfers, plus every committed transaction on an investment account. Buys, sells, dividends and fees stay inside the return instead."
        />
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>
          Start <span className="tabular text-text">{fmtCad(data.start_value_cad)}</span> → End{" "}
          <span className="tabular text-text">{fmtCad(data.end_value_cad)}</span>
        </span>
        <span>
          {data.valuation_days} days · {data.active_days} valued · {data.flow_days} with flows ·{" "}
          {data.account_count} investment account{data.account_count === 1 ? "" : "s"}
        </span>
      </div>
      <p className="mt-2 text-xs text-muted">
        Period returns, not annualized. TWR chain-links daily and excludes external money in/out;
        MWR weights each flow by the days it stayed in the window.
      </p>
    </Card>
  );
}
