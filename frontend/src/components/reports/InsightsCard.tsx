import { useQuery } from "@tanstack/react-query";
import { getSpendingInsights } from "../../api/insights";
import { Card, CardTitle } from "../ui/Card";
import { SkeletonRows } from "../ui/Skeleton";
import { HelpHint } from "../ui/HelpHint";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { useMasked } from "../../lib/mask";
import { formatCad } from "../../lib/format";

/** Whole-dollar velocity/burn figures (docs/09 money displays). */
const WHOLE = { fractionDigits: 0 } as const;

export function InsightsCard() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { data, isLoading } = useQuery({
    queryKey: ["insights"],
    queryFn: getSpendingInsights,
    staleTime: 60_000,
  });

  if (isLoading) return <SkeletonRows rows={2} />;
  if (!data) return null;

  const momPct = data.month_over_month_pct;
  const TrendIcon = momPct > 5 ? TrendingUp : momPct < -5 ? TrendingDown : Minus;
  const trendColor = momPct > 5 ? "text-danger" : momPct < -5 ? "text-gain" : "text-muted";

  return (
    <Card className="p-4">
      <CardTitle>Spending insights</CardTitle>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mt-3">
        <div>
          <div className="text-xs text-muted uppercase tracking-wider">
            This month
            <HelpHint text="Signed CAD spending from the 1st of this month through today — refunds subtract, income never counts, and a row with no cached exchange rate is excluded, never fetched." />
          </div>
          <div className="text-lg font-semibold text-text">{formatCad(data.current_month_spending, undefined, WHOLE)}</div>
        </div>
        <div>
          <div className="text-xs text-muted uppercase tracking-wider">
            vs last month
            <HelpHint text="This month's spending so far against ALL of last month — a part-month measured against a full month, so the two converge as the month runs. 0% when last month spent nothing." />
          </div>
          <div className={`text-lg font-semibold flex items-center gap-1 ${trendColor}`}>
            <TrendIcon className="size-4" />
            {momPct > 0 ? "+" : ""}{momPct}%
          </div>
        </div>
        <div>
          <div className="text-xs text-muted uppercase tracking-wider">
            Daily burn rate
            <HelpHint text="This month's spending so far ÷ the days elapsed so far." />
          </div>
          <div className="text-lg font-semibold text-text">{formatCad(data.daily_burn_rate, undefined, WHOLE)}/day</div>
        </div>
        <div>
          <div className="text-xs text-muted uppercase tracking-wider">
            Projected month-end
            <HelpHint text="Daily burn rate × the days in this month — a straight-line projection of this pace, not a forecast." />
          </div>
          <div className="text-lg font-semibold text-text">{formatCad(data.projected_month_end, undefined, WHOLE)}</div>
        </div>
      </div>
      {data.category_deltas.length > 0 && (
        <div className="mt-3 pt-3 border-t border-border">
          <div className="text-xs text-muted uppercase tracking-wider mb-2">Biggest changes</div>
          <div className="flex flex-wrap gap-2">
            {data.category_deltas.slice(0, 5).map((d) => (
              <span key={d.category_id} className="text-xs px-2 py-1 rounded-full bg-surface-raised">
                {d.category_name}: {d.change_pct > 0 ? "+" : ""}{d.change_pct}%
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
