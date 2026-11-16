import { useQuery } from "@tanstack/react-query";
import { getTaxLossAlerts } from "../../api/taxAlerts";
import { Card } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { Badge } from "../ui/Badge";
import { AlertTriangle } from "lucide-react";
import { formatCad } from "../../lib/format";
import { useMasked } from "../../lib/mask";

export function TaxAlertsPanel() {
  // M140: re-render on a mask flip — the shared `formatCad` below reads the
  // ambient mask at call time, so this component only has to re-render
  // (docs/46 §D6). Money renders through `lib/format::formatCad`, the ONE CAD
  // helper (no page-local shim): an unparseable Decimal-string renders the
  // honest "—", never a fabricated "$0".
  useMasked();

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["tax-alerts"],
    queryFn: getTaxLossAlerts,
  });

  if (isLoading) return <SkeletonRows rows={2} />;
  if (error) return <QueryErrorPanel query={{ error, isFetching, refetch }} />;
  if (!data || data.alerts.length === 0) {
    return (
      <EmptyState
        icon={<AlertTriangle />}
        title="No tax-loss harvesting opportunities"
        hint="All taxable positions are at or above their adjusted cost basis."
        preview={{ src: "/help/investments.png", alt: "The Investments page with holdings and totals" }}
        action={{ label: "Record a trade", to: "/investments" }}
      />
    );
  }

  return (
    <div className="space-y-3">
      <Card className="p-4">
        <div className="flex items-center gap-4 text-sm">
          <span className="text-muted">
            {data.summary.count} position{data.summary.count !== 1 ? "s" : ""} at loss
          </span>
          <span className="font-medium text-danger">
            Total loss: {formatCad(data.summary.total_unrealized_loss_cad)}
          </span>
          <span className="font-medium text-gain">
            Est. tax benefit: {formatCad(data.summary.total_estimated_tax_benefit_cad)}
          </span>
        </div>
      </Card>
      <div role="region" aria-label="Tax loss alerts table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted uppercase text-xs tracking-wider">
              <th className="pb-2 pr-4">Security</th>
              <th className="pb-2 pr-4">Account</th>
              <th className="pb-2 pr-4 text-right">Qty</th>
              <th className="pb-2 pr-4 text-right">ACB (CAD)</th>
              <th className="pb-2 pr-4 text-right">Market (CAD)</th>
              <th className="pb-2 pr-4 text-right">Loss</th>
              <th className="pb-2 pr-4 text-right">Tax Benefit</th>
              <th className="pb-2">Risk</th>
            </tr>
          </thead>
          <tbody>
            {data.alerts.map((a) => (
              <tr key={`${a.account_id}-${a.security_id}`} className="border-b border-border/50">
                <td className="py-2 pr-4 font-medium">{a.symbol}</td>
                <td className="py-2 pr-4 text-muted">{a.account_name}</td>
                <td className="py-2 pr-4 text-right">{a.quantity}</td>
                <td className="py-2 pr-4 text-right">{formatCad(a.book_cost_cad)}</td>
                <td className="py-2 pr-4 text-right">{formatCad(a.market_value_cad)}</td>
                <td className="py-2 pr-4 text-right font-medium text-danger">
                  -{formatCad(a.unrealized_loss_cad)}
                </td>
                <td className="py-2 pr-4 text-right text-gain">
                  {formatCad(a.estimated_tax_benefit_cad)}
                </td>
                <td className="py-2">
                  {a.superficial_loss_risk ? (
                    <Badge tone="warn">Superficial risk</Badge>
                  ) : (
                    <Badge tone="success">Clear</Badge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
