import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getSpendingComparison } from "../../api/spendingComparison";
import { Card } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { TrendingUp, TrendingDown, BarChart3 } from "lucide-react";
import { useMasked } from "../../lib/mask";
import { formatCad } from "../../lib/format";
import { etToday } from "../../lib/budgets";

/**
 * Start of the comparison window: one calendar month before the ET date, from
 * the ET date STRING rather than a `Date` object. `new Date()` carries the
 * browser's zone, so east of UTC `toISOString()` already reads tomorrow in the
 * evening, and `setMonth` would compound it; the day-of-month clamp (Mar 31 →
 * Feb 28) is what the old `setMonth` did, so the window length is unchanged.
 */
function monthAgo(): string {
  const [year, month, day] = etToday().split("-").map(Number);
  const prev = new Date(Date.UTC(year, month - 2, 1));
  const lastDay = new Date(Date.UTC(prev.getUTCFullYear(), prev.getUTCMonth() + 1, 0)).getUTCDate();
  const clamped = Math.min(day, lastDay);
  return `${prev.getUTCFullYear()}-${String(prev.getUTCMonth() + 1).padStart(2, "0")}-${String(clamped).padStart(2, "0")}`;
}

export function ComparisonPanel() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const [curStart] = useState(monthAgo);
  const [curEnd] = useState(etToday);

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["spending-comparison", curStart, curEnd],
    queryFn: () => getSpendingComparison(curStart, curEnd),
  });

  if (isLoading) return <SkeletonRows rows={3} />;
  if (error) return <QueryErrorPanel query={{ error, isFetching, refetch }} />;
  if (!data || data.deltas.length === 0) {
    return (
      <EmptyState
        icon={<BarChart3 />}
        title="No comparison data"
        hint="Need transactions in both periods."
        preview={{ src: "/help/reports.png", alt: "The Reports page with charts for the selected range" }}
        action={{ label: "Import a statement", to: "/imports" }}
      />
    );
  }

  const totalPct = data.total_change_pct;

  return (
    <div className="space-y-3">
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <div>
            <span className="text-muted">Previous: </span>
            <span className="font-medium">{formatCad(data.previous_period.total)}</span>
          </div>
          <div>
            <span className="text-muted">Current: </span>
            <span className="font-medium">{formatCad(data.current_period.total)}</span>
          </div>
          <div className={`flex items-center gap-1 font-medium ${totalPct > 0 ? "text-danger" : "text-gain"}`}>
            {totalPct > 0 ? <TrendingUp className="size-4" /> : <TrendingDown className="size-4" />}
            {totalPct > 0 ? "+" : ""}{totalPct}%
          </div>
        </div>
      </Card>
      <div role="region" aria-label="Category comparison table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted uppercase text-xs tracking-wider">
              <th className="pb-2 pr-4">Category</th>
              <th className="pb-2 pr-4 text-right">Previous</th>
              <th className="pb-2 pr-4 text-right">Current</th>
              <th className="pb-2 pr-4 text-right">Change</th>
              <th className="pb-2 text-right">%</th>
            </tr>
          </thead>
          <tbody>
            {data.deltas.map((d) => (
              <tr key={d.category_id} className="border-b border-border/50">
                <td className="py-2 pr-4 font-medium">{d.category_name}</td>
                <td className="py-2 pr-4 text-right text-muted">{formatCad(d.previous)}</td>
                <td className="py-2 pr-4 text-right">{formatCad(d.current)}</td>
                <td className={`py-2 pr-4 text-right font-medium ${parseFloat(d.change) > 0 ? "text-danger" : "text-gain"}`}>
                  {parseFloat(d.change) > 0 ? "+" : ""}{formatCad(d.change)}
                </td>
                <td className={`py-2 text-right ${d.change_pct > 0 ? "text-danger" : "text-gain"}`}>
                  {d.change_pct > 0 ? "+" : ""}{d.change_pct}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
