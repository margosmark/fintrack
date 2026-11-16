// RuleTestPanel — M122 T2: what the DRAFT rule would do, before it is saved.
//
// This is to rule editing what the import wizard's dry-run is to imports: the
// editor's "Test" action runs the draft over the committed ledger through
// `POST /bills/find-past` and renders the effect INLINE. The panel is
// deliberately loud about being a preview — "Draft preview — not saved" — so
// a test run can never be mistaken for the saved rule's current state.
//
// Every figure comes from the SERVER's `stats` block, computed over the whole
// matched set (the previewed rows truncate at 500; the summary never does —
// the M91 anti-fabrication rule applied to a dry-run).

import type { BillFindPast } from "../../api/bills";
import { formatMoney } from "../transactions/format";
import { Badge } from "../ui/Badge";
import { useMasked } from "../../lib/mask";

function money(value: string | null | undefined): string {
  if (!value) return "—";
  return formatMoney(String(value), "CAD");
}

/** The dry-run's sample: the first 8 matched rows, newest period first. */
const SAMPLE_ROWS = 8;

export function RuleTestPanel({ preview }: { preview: BillFindPast }) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const stats = preview.stats;
  const sample = preview.groups.flatMap((group) => group.items).slice(0, SAMPLE_ROWS);
  const accounts = stats.account_counts;

  return (
    <div
      className="mt-3 rounded-lg border border-dashed border-accent/40 bg-accent/[0.04] p-3"
      aria-label="Draft preview — not saved"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">Draft preview — not saved</Badge>
        <span className="text-xs text-muted tabular">
          {preview.total_count === 0
            ? "Nothing in your committed ledger matches this draft yet"
            : `${preview.total_count} matching transaction${preview.total_count === 1 ? "" : "s"}`}
          {preview.truncated ? " (row sample truncated)" : ""}
        </span>
      </div>

      {preview.total_count > 0 && (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs text-muted">First → last payment</dt>
              <dd className="tabular text-text">
                {stats.first_date} → {stats.last_date}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Last 12 periods</dt>
              <dd className="tabular text-text">{money(stats.last_12_total)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">By account</dt>
              <dd className="tabular text-text">
                {accounts.length === 0
                  ? "—"
                  : accounts
                      .map((account) => `${account.account_name || `#${account.account_id}`} ×${account.count}`)
                      .join(" · ")}
              </dd>
            </div>
          </dl>

          {sample.length > 0 && (
            <table className="mt-3 w-full text-left text-xs">
              <caption className="sr-only">
                First {sample.length} transactions the draft rule would match
              </caption>
              <thead>
                <tr className="border-b border-control text-muted">
                  <th scope="col" className="px-3 py-1.5 font-medium">Date</th>
                  <th scope="col" className="px-3 py-1.5 font-medium">Payee</th>
                  <th scope="col" className="px-3 py-1.5 font-medium">Account</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">Amount</th>
                </tr>
              </thead>
              <tbody>
                {sample.map((row) => (
                  <tr key={row.id} className="border-b border-veil/[0.05] last:border-b-0">
                    <td className="whitespace-nowrap px-3 py-1.5 tabular">{row.date}</td>
                    <td className="px-3 py-1.5 text-text">{row.normalized_payee}</td>
                    <td className="px-3 py-1.5 text-muted">{row.account_name}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular">
                      <span className={row.amount_native.startsWith("-") ? "text-danger" : ""}>
                        {money(row.amount_native)} {row.currency}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {preview.groups.length > 0 && (
            <div className="mt-3 max-h-56 overflow-y-auto rounded-lg border border-control">
              <table className="w-full text-left text-xs">
                <caption className="sr-only">
                  Past payments matching the draft rule, grouped by period
                </caption>
                <thead>
                  <tr className="border-b border-control text-muted">
                    <th scope="col" className="px-3 py-1.5 font-medium">Period</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-medium">Payments</th>
                    <th scope="col" className="px-3 py-1.5 text-right font-medium">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.groups.map((group) => (
                    <tr key={group.period} className="border-b border-veil/[0.05] last:border-b-0">
                      <th scope="row" className="px-3 py-1.5 font-normal text-text">
                        {group.label}
                      </th>
                      <td className="px-3 py-1.5 text-right tabular text-muted">{group.count}</td>
                      <td className="px-3 py-1.5 text-right tabular text-text">
                        {money(group.total)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
