import { useQuery } from "@tanstack/react-query";
import { getDividendCalendar, type DividendPeriod } from "../../api/dividendCalendar";
import { Card } from "../ui/Card";
import { DividendProvenanceStrip } from "./DividendProvenanceStrip";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { Calendar } from "lucide-react";
import { useMasked } from "../../lib/mask";
import { formatMoney } from "../../lib/format";

const periods: { key: DividendPeriod; label: string }[] = [
  { key: "this_month", label: "This month" },
  { key: "this_quarter", label: "This quarter" },
  { key: "this_year", label: "This year" },
];

export function DividendCalendarPanel() {
  // Subscribe so a privacy-mask flip re-renders immediately; formatMoney reads
  // the ambient mask and no query/refetch is needed to hide or reveal amounts.
  useMasked();

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["dividend-calendar"],
    queryFn: getDividendCalendar,
  });

  if (isLoading) return <SkeletonRows rows={3} />;
  if (error) return <QueryErrorPanel query={{ error, isFetching, refetch }} />;
  if (!data || data.upcoming.length === 0) {
    // docs/07 §M184: even the empty answer carries its provenance — "no
    // dividend history" and "no source ever answered" are different facts and
    // only the strip can tell them apart.
    return (
      <div className="space-y-3">
        {data && <DividendProvenanceStrip provenance={data.provenance} />}
        <EmptyState
          icon={<Calendar />}
          title="No dividend history"
          hint="Record dividend trades to see upcoming expected income."
          preview={{ src: "/help/reports.png", alt: "The Reports page with charts for the selected range" }}
          action={{ label: "Record a dividend", to: "/investments" }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* docs/07 §M184: these projections are computed from recorded payments —
          the strip says so, with the day of the newest one, instead of letting
          a projection read as market data. */}
      <DividendProvenanceStrip provenance={data.provenance} />
      <Card className="space-y-3 p-4">
        <p className="text-sm font-medium text-text">Estimated income by currency — no FX conversion</p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3 text-sm">
          {periods.map(({ key, label }) => {
            const count = data.summary.unresolved_counts[key];
            return (
              <div key={key}>
                <div className="text-xs text-muted uppercase">{label}</div>
                {data.summary.by_currency.length === 0 ? (
                  <div className="mt-1 text-text">—</div>
                ) : (
                  <ul className="mt-1 space-y-1">
                    {data.summary.by_currency.map((subtotal) => (
                      <li key={subtotal.currency} className="flex justify-between gap-2">
                        <span className="text-muted">{subtotal.currency}</span>
                        <span className="font-semibold text-text">
                          {formatMoney(subtotal[key], subtotal.currency)} <span>{subtotal.currency}</span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                {count > 0 && (
                  <p className="mt-1 text-xs text-muted">
                    Excludes {count} projections with mixed or unknown currency
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </Card>
      <div role="region" aria-label="Dividend calendar table" tabIndex={0} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted uppercase text-xs tracking-wider">
              <th className="pb-2 pr-4">Security</th>
              <th className="pb-2 pr-4">Frequency</th>
              <th className="pb-2 pr-4 text-right">Avg Amount</th>
              <th className="pb-2 pr-4 text-right">Annual</th>
              <th className="pb-2 pr-4">Last Paid</th>
              <th className="pb-2">Next Expected</th>
            </tr>
          </thead>
          <tbody>
            {data.upcoming.map((entry) => (
              <tr key={entry.security_id} className="border-b border-border/50">
                <td className="py-2 pr-4 font-medium">{entry.symbol}</td>
                <td className="py-2 pr-4 text-muted">{entry.frequency}</td>
                <td className="py-2 pr-4 text-right">
                  {entry.currency ? (
                    <>{formatMoney(entry.avg_amount, entry.currency)} <span>{entry.currency}</span></>
                  ) : (
                    <span title="Mixed or unknown currency">— <span className="sr-only">Mixed or unknown currency</span></span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right">
                  {entry.currency ? (
                    <>{formatMoney(entry.annual_projection, entry.currency)} <span>{entry.currency}</span></>
                  ) : (
                    <span title="Mixed or unknown currency">— <span className="sr-only">Mixed or unknown currency</span></span>
                  )}
                </td>
                <td className="py-2 pr-4 text-muted">{entry.last_payment_date}</td>
                <td className="py-2 text-gain font-medium">{entry.next_expected_date}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.upcoming.some((entry) => entry.currency === null) && (
          <p className="mt-2 text-xs text-muted">Mixed or unknown currency</p>
        )}
      </div>
    </div>
  );
}
