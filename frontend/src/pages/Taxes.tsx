import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText } from "lucide-react";
import { listAccounts } from "../api/accounts";
import { listAcbPools, rebuildAcb } from "../api/acb";
import {
  downloadCsv,
  getCapitalGains,
  openPrintableHtml,
} from "../api/reports";
import type { CapitalGainsDenial } from "../api/reports";
import { fmtCad, fmtQuantity } from "../components/charts/format";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { HelpHint } from "../components/ui/HelpHint";
import { TaxAlertsPanel } from "../components/reports/TaxAlertsPanel";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import { SkeletonRows } from "../components/ui/Skeleton";
import { useToast } from "../components/ui/Toast";
import { useMasked } from "../lib/mask";

/**
 * Taxes page (docs/09 + docs/06): ACB-per-holding view and the annual
 * capital-gains report with CSV blob download, printable HTML tab,
 * verbatim disclaimer and distinctly-rendered superficial-loss denials.
 * Presentation pass: segmented year switcher, amber-tinted denial rows,
 * toast feedback on exports and pool rebuild.
 */

function currentYear(): number {
  return new Date().getFullYear();
}

const YEARS = Array.from({ length: currentYear() - 2014 + 1 }, (_, i) => currentYear() - i);

export function Taxes() {
  const [year, setYear] = useState(currentYear());

  return (
    <section className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-xl font-semibold tracking-tight">Taxes</h1>
        {/* Year switcher — segmented control (ui-contract Taxes must-have). */}
        <SegmentedControl
          layoutId="tax-year"
          ariaLabel="Tax year"
          className="max-w-full"
          options={YEARS.map((y) => ({ value: y, label: String(y) }))}
          value={year}
          onChange={setYear}
        />
      </div>
      <TaxAlertsPanel />
      <CapitalGainsSection year={year} />
      <AcbSection />
    </section>
  );
}

// --- Annual capital-gains report ---------------------------------------------

function CapitalGainsSection({ year }: { year: number }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const [exportError, setExportError] = useState<string | null>(null);
  const report = useQuery({
    queryKey: ["reports", "capital-gains", year],
    queryFn: () => getCapitalGains(year),
    retry: false,
  });

  const csvPath = `/reports/capital-gains?year=${year}`;

  if (report.isPending) {
    return (
      <Card className="p-4">
        <CardTitle>Capital gains {year}</CardTitle>
        <SkeletonRows rows={5} />
      </Card>
    );
  }

  if (report.isError) {
    return (
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Capital gains {year}</CardTitle>
          <ExportButtons
            csvPath={csvPath}
            filename={`capital-gains-${year}.csv`}
            onError={setExportError}
          />
        </div>
        <ErrorPanel message={`Report unavailable: ${report.error.message}`} />
      </Card>
    );
  }

  const data = report.data;

  return (
    <Card hover className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>Capital gains {data.year}</CardTitle>
        <ExportButtons
          csvPath={csvPath}
          filename={`capital-gains-${data.year}.csv`}
          onError={setExportError}
        />
      </div>
      {exportError !== null && (
        <p className="mb-3 text-sm text-danger">{exportError}</p>
      )}

      {data.items.length === 0 ? (
        <EmptyState
          icon={<FileText />}
          title={`No disposals in ${data.year}.`}
          hint="Sells in this tax year will appear here with their computed gains."
          preview={{
            src: "/help/taxes.png",
            alt: "The Taxes page with computed figures",
          }}
          action={{
            label: "Record a trade",
            to: "/investments",
            ariaLabel: `Record a trade — No disposals in ${data.year}.`,
          }}
        />
      ) : (
        <div role="region" aria-label={`Capital gains ${data.year} table`} tabIndex={0} className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                <th className="px-3 py-2 font-medium">Security</th>
                <th className="px-3 py-2 text-right font-medium">Proceeds (CAD)</th>
                <th className="px-3 py-2 text-right font-medium">ACB (CAD)</th>
                <th className="px-3 py-2 text-right font-medium">Outlays/commissions</th>
                <th className="px-3 py-2 text-right font-medium">Gain/Loss</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr
                  key={item.security_id}
                  className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
                >
                  <td className="px-3 py-2 font-medium">{item.symbol}</td>
                  <td className="px-3 py-2 text-right tabular">{fmtCad(item.proceeds_cad)}</td>
                  <td className="px-3 py-2 text-right text-muted tabular">{fmtCad(item.acb_cad)}</td>
                  <td className="px-3 py-2 text-right tabular">{fmtCad(item.outlays_commissions_cad)}</td>
                  <td
                    className={`px-3 py-2 text-right font-medium tabular ${
                      Number(item.gain_loss_cad) < 0 ? "text-danger" : "text-gain"
                    }`}
                  >
                    {fmtCad(item.gain_loss_cad)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-veil/[0.08] font-medium">
                <td className="px-3 py-2">Totals</td>
                <td className="px-3 py-2 text-right tabular">{fmtCad(data.totals.proceeds_cad)}</td>
                <td className="px-3 py-2 text-right tabular">{fmtCad(data.totals.acb_cad)}</td>
                <td className="px-3 py-2 text-right tabular">
                  {fmtCad(data.totals.outlays_commissions_cad)}
                </td>
                <td
                  className={`px-3 py-2 text-right tabular ${
                    Number(data.totals.gain_loss_cad) < 0 ? "text-danger" : "text-gain"
                  }`}
                >
                  {fmtCad(data.totals.gain_loss_cad)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}

      {data.denials.length > 0 && (
        <div role="region" aria-label="Superficial loss denials table" tabIndex={0} className="mt-4 overflow-x-auto rounded-lg border border-danger/40">
          <div className="bg-danger/10 px-3 py-2 text-sm font-medium text-danger">
            Superficial-loss denials — shown separately so they are never mistaken for realized
            figures.
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                <th className="px-3 py-2 font-medium">Sale date</th>
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 text-right font-medium">Denied loss (CAD)</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {data.denials.map((denial) => (
                <DenialRow key={`${denial.security_id}-${denial.sale_date}`} denial={denial} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Disclaimer displayed verbatim from the API payload (docs/06). */}
      <p className="mt-4 text-xs italic text-muted">{data.disclaimer}</p>
    </Card>
  );
}

function DenialRow({ denial }: { denial: CapitalGainsDenial }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  // Amber treatment (ui-contract): denied losses are warnings about the pool,
  // not realized red numbers.
  const outcome =
    denial.outcome === "deferral"
      ? "Deferred — added to the acquiring pool's ACB"
      : "Denied outright (registered repurchase)";
  return (
    <tr className="border-l-2 border-warn/70 bg-warn/[0.05] text-text">
      <td className="px-3 py-2 tabular">{denial.sale_date}</td>
      <td className="px-3 py-2 font-medium">{denial.symbol}</td>
      <td className="px-3 py-2 text-right tabular text-warn">{fmtCad(denial.denied_loss_cad)}</td>
      <td className="px-3 py-2 text-muted">{outcome}</td>
    </tr>
  );
}

function ExportButtons({
  csvPath,
  filename,
  onError,
}: {
  csvPath: string;
  filename: string;
  onError: (message: string | null) => void;
}) {
  const toast = useToast();
  const download = useMutation({
    mutationFn: () => downloadCsv(csvPath, filename),
    onSuccess: () => {
      onError(null);
      toast.success("Export ready", filename);
    },
    onError: (err) => {
      onError(err.message);
      toast.error("Export failed", err.message);
    },
  });
  const print = useMutation({
    mutationFn: () => openPrintableHtml(csvPath),
    onSuccess: () => onError(null),
    onError: (err) => {
      onError(err.message);
      toast.error("Print view failed", err.message);
    },
  });

  return (
    <div className="flex gap-2 text-sm">
      <Button variant="outline" size="sm" loading={download.isPending} onClick={() => download.mutate()}>
        <Download className="size-3.5" aria-hidden />
        Download CSV
      </Button>
      <Button variant="outline" size="sm" loading={print.isPending} onClick={() => print.mutate()}>
        Printable HTML
      </Button>
    </div>
  );
}

// --- ACB per holding ----------------------------------------------------------

function AcbSection() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const pools = useQuery({
    queryKey: ["acb"],
    queryFn: () => listAcbPools({ limit: 200 }),
  });
  const rebuild = useMutation({
    mutationFn: () => rebuildAcb(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["acb"] });
      toast.success("ACB pools rebuilt", "Recomputed from the full trade history.");
    },
    onError: (err) => toast.error("Rebuild failed", err.message),
  });
  const accounts = useQuery({ queryKey: ["accounts", 200], queryFn: () => listAccounts(200) });
  const accountName = new Map((accounts.data?.items ?? []).map((a) => [a.id, a.name]));

  return (
    <Card hover className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>ACB per holding (non-registered)</CardTitle>
        <HelpHint text="Capital gains use the average-cost (ACB) method required in Canada. Buy transactions raise your cost base; sells realize gains. Do not edit the computed pools by hand." />
        <Button
          variant="accent-soft"
          size="sm"
          loading={rebuild.isPending}
          onClick={() => rebuild.mutate()}
          title="Rebuild all pools from trades"
        >
          Rebuild pools
        </Button>
      </div>

      {pools.isPending && <SkeletonRows rows={4} />}
      {pools.isError && <ErrorPanel message={pools.error.message} />}
      {pools.data !== undefined && pools.data.items.length === 0 && (
        <EmptyState
          icon={<Badge tone="neutral" dot={false}>ACB</Badge>}
          title="No non-registered holdings yet"
          hint="ACB pools appear after the first buy in a taxable account."
          preview={{
            src: "/help/taxes.png",
            alt: "The Taxes page with computed figures",
          }}
          action={{
            label: "Record a trade",
            to: "/investments",
            ariaLabel: "Record a trade — No non-registered holdings yet",
          }}
        />
      )}

      {pools.data !== undefined && pools.data.items.length > 0 && (
        <div role="region" aria-label="ACB per holding (non-registered) table" tabIndex={0} className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
                <th className="px-3 py-2 font-medium">Account</th>
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 text-right font-medium">Quantity</th>
                <th className="px-3 py-2 text-right font-medium">Total ACB (CAD)</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {pools.data.items.map((pool) => (
                <tr
                  key={pool.id}
                  className="border-b border-veil/[0.05] transition-colors last:border-b-0 hover:bg-veil/[0.04]"
                >
                  <td className="px-3 py-2">{accountName.get(pool.account_id) ?? pool.account_id}</td>
                  <td className="px-3 py-2 font-medium">{pool.symbol}</td>
                  <td className="px-3 py-2 text-right tabular">{fmtQuantity(pool.quantity)}</td>
                  <td className="px-3 py-2 text-right tabular">{fmtCad(pool.total_acb_cad)}</td>
                  <td className="px-3 py-2 text-muted tabular">{pool.updated_at.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
