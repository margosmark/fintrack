import { useQuery } from "@tanstack/react-query";
import { getCashflowSankey } from "../../api/reports";
import { CashflowSankey } from "../charts/CashflowSankey";
import { NoDataPanel } from "../charts/NoDataPanel";
import { StateRow } from "../ui/StateRow";
import { fmtCad } from "../charts/format";
import { useMasked } from "../../lib/mask";

/**
 * docs/12 §Sankey cash-flow — the Cash-flow column of the Reports page.
 * Owns its query, loading and error states (the same pattern as
 * `ProjectionsPanel`), so the composed chart above it never has to branch on a
 * second report.
 *
 * JSON-only: there is no `format=csv` twin, because a graph has no flat table
 * to export (the totals line below is the human-readable summary).
 */
export function SankeyPanel({ from, to }: { from: string; to: string }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const sankey = useQuery({
    queryKey: ["reports", "sankey", from, to],
    queryFn: () => getCashflowSankey(from, to),
    enabled: from <= to,
    retry: false,
  });

  if (sankey.isPending) return <StateRow variant="loading" title="Loading" />;
  if (sankey.isError || sankey.data.nodes.length === 0) {
    return <NoDataPanel hint={sankey.isError ? "No data in range" : undefined} />;
  }

  const { totals } = sankey.data;
  return (
    <section className="mt-8 space-y-3" aria-label="Cash-flow Sankey">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold tracking-tight">Where the money went</h2>
          <p className="text-sm text-muted">
            Sources → money in → destinations, {from} to {to}. Categories roll up to their
            top-level ancestor; trade principal lands on Investments.
          </p>
        </div>
        <p className="text-sm text-muted">
          Money in <span className="font-medium tabular text-text">{fmtCad(totals.money_in_cad)}</span>
          {" · "}Spent <span className="font-medium tabular text-text">{fmtCad(totals.expenses_cad)}</span>
          {" · "}Invested <span className="font-medium tabular text-text">{fmtCad(totals.invested_cad)}</span>
          {" · "}Saved <span className="font-medium tabular text-text">{fmtCad(totals.saved_cad)}</span>
        </p>
      </div>
      <div className="glass rounded-xl p-4">
        <CashflowSankey report={sankey.data} />
      </div>
    </section>
  );
}
