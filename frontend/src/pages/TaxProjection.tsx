import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Calculator } from "lucide-react";
import { getProjection, updateProjection } from "../api/taxProjection";
import { Card, CardTitle } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { SkeletonRows } from "../components/ui/Skeleton";
import { useToast } from "../components/ui/Toast";
import { apiErrorMessage } from "../components/transactions/format";
import { useMasked } from "../lib/mask";
import { formatCad } from "../lib/format";

const PROVINCES = ["ON", "BC", "AB", "QC", "MB", "SK", "NS", "NB", "PE", "NL", "NT", "NU", "YT"];

/**
 * True when the current input differs from the stored value — `undefined`/
 * `null`/"" all mean "no override" and count as equal so an untouched
 * cleared field is omitted from the save payload.
 */
function moneyChanged(current: string, storedVal: string | null | undefined): boolean {
  const hasCurrent = current.trim() !== "";
  const hasStored = storedVal != null && storedVal !== "";
  if (!hasCurrent && !hasStored) return false;
  if (hasCurrent !== hasStored) return true;
  return parseFloat(current) !== parseFloat(storedVal as string);
}

export function TaxProjection() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const year = 2026;
  const [province, setProvince] = useState("ON");
  const [income, setIncome] = useState("");
  const [deductions, setDeductions] = useState("");
  const [capGains, setCapGains] = useState("");
  const [divIncome, setDivIncome] = useState("");

  const projection = useQuery({
    queryKey: ["tax-projection", year],
    queryFn: () => getProjection(year),
  });

  // Prefill the form from the last saved values (docs/29 F9): the GET
  // response carries the raw stored inputs; cleared overrides fall back to "".
  useEffect(() => {
    const s = projection.data?.stored;
    if (!s) return;
    setProvince(s.province);
    setIncome(s.estimated_income);
    setDeductions(s.deductions);
    setCapGains(s.capital_gains_override ?? "");
    setDivIncome(s.dividend_income_override ?? "");
  }, [projection.data?.stored]);

  const save = useMutation({
    mutationFn: () => {
      const s = projection.data?.stored;
      // Send ONLY edited fields: backend PATCH keeps stored values for
      // omitted params, so untouched inputs never overwrite the row.
      return updateProjection({
        year,
        province,
        estimated_income: moneyChanged(income, s?.estimated_income)
          ? income
          : undefined,
        deductions: moneyChanged(deductions, s?.deductions) ? deductions : undefined,
        capital_gains_override: moneyChanged(capGains, s?.capital_gains_override)
          ? capGains.trim() === "" ? null : capGains
          : undefined,
        dividend_income_override: moneyChanged(divIncome, s?.dividend_income_override)
          ? divIncome.trim() === "" ? null : divIncome
          : undefined,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tax-projection"] });
      toast.success("Projection saved");
    },
    onError: (e: Error) => toast.error("Could not save projection", apiErrorMessage(e)),
  });

  return (
    <section className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Tax Projection {year}</h1>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Inputs */}
        <Card className="p-4">
          <CardTitle>Income & Deductions</CardTitle>
          <div className="grid gap-3 mt-3">
            <div>
              <label className="text-xs text-muted" htmlFor="tp-province">Province</label>
              <select id="tp-province" value={province} onChange={e => setProvince(e.target.value)}
                className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm mt-1">
                {PROVINCES.map(p => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs text-muted" htmlFor="tp-income">Employment Income</label>
              <input id="tp-income" type="number" value={income} onChange={e => setIncome(e.target.value)} placeholder="0"
                className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label className="text-xs text-muted" htmlFor="tp-capgains">Capital Gains</label>
              <input id="tp-capgains" type="number" value={capGains} onChange={e => setCapGains(e.target.value)} placeholder="0"
                className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label className="text-xs text-muted" htmlFor="tp-divincome">Dividend Income</label>
              <input id="tp-divincome" type="number" value={divIncome} onChange={e => setDivIncome(e.target.value)} placeholder="0"
                className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label className="text-xs text-muted" htmlFor="tp-deductions">Deductions</label>
              <input id="tp-deductions" type="number" value={deductions} onChange={e => setDeductions(e.target.value)} placeholder="0"
                className="field-ring w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm mt-1" />
            </div>
            <Button onClick={() => save.mutate()}>Save Projection</Button>
          </div>
        </Card>

        {/* Results */}
        <Card className="p-4">
          <CardTitle>Projection Results</CardTitle>
          {projection.isPending ? <SkeletonRows rows={3} /> : projection.data ? (
            <div className="space-y-3 mt-3">
              <div className="flex justify-between">
                <span className="text-muted">Taxable Income</span>
                <span className="font-medium">{formatCad(projection.data.taxable_income)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted">Federal Tax</span>
                <span className="font-medium">{formatCad(projection.data.federal_tax)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted">Provincial Tax ({projection.data.province})</span>
                <span className="font-medium">{formatCad(projection.data.provincial_tax)}</span>
              </div>
              <div className="flex justify-between border-t border-border pt-2">
                <span className="text-muted">Dividend Tax Credit</span>
                <span className="font-medium text-gain">-{formatCad(projection.data.dividend_tax_credit)}</span>
              </div>
              <div className="flex justify-between border-t border-border pt-2">
                <span className="font-semibold">Estimated Tax</span>
                <span className="text-xl font-bold text-danger">{formatCad(projection.data.total_tax)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted">Average Rate</span>
                <span className="font-medium">{projection.data.average_rate}%</span>
              </div>
            </div>
          ) : (
            <div className="text-center text-muted py-8">
              <Calculator className="size-8 mx-auto mb-2 opacity-50" />
              <p>Enter income details to see your tax projection</p>
            </div>
          )}
        </Card>
      </div>
    </section>
  );
}
