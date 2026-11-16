import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, Upload } from "lucide-react";
import { getReconciliation, uploadStatement } from "../api/reconciliation";
import type { StatementEntry } from "../api/reconciliation";
import { listAccounts } from "../api/accounts";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Badge } from "../components/ui/Badge";
import { useToast } from "../components/ui/Toast";
import { formatCad } from "../lib/format";

// M139 D4: reconciliation is the import flow's matching surface — the user is
// actively comparing statement lines to ledger rows here, so amounts stay
// readable by design (same exemption as the import wizard itself). The calls
// pass `masked: false` explicitly, which is also what docs/46 §D6 pins this
// surface as exempt from the subscription requirement.

export function Reconciliation() {
  const { accountId } = useParams<{ accountId: string }>();
  const acctId = parseInt(accountId || "0");
  const queryClient = useQueryClient();
  const toast = useToast();
  const [csvText, setCsvText] = useState("");

  const recon = useQuery({
    queryKey: ["reconciliation", acctId],
    queryFn: () => getReconciliation(acctId),
    enabled: acctId > 0,
  });

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => listAccounts(200) });
  const account = accounts.data?.items?.find(a => a.id === acctId);

  const upload = useMutation({
    mutationFn: (items: StatementEntry[]) =>
      uploadStatement(acctId, items),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reconciliation", acctId] });
      toast.success("Statement uploaded", "Statement items loaded for matching.");
      setCsvText("");
    },
    onError: (e: Error) => toast.error("Upload failed", e.message),
  });

  function parseCsv(text: string): StatementEntry[] {
    const lines = text.trim().split("\n").filter(l => l.trim());
    return lines.flatMap(line => {
      const parts = line.split(",").map(s => s.trim().replace(/^["']|["']$/g, ""));
      const amountStr = parts[1] ?? "";
      const n = parseFloat(amountStr);
      if (parts[0] === "" || !Number.isFinite(n)) return [];
      // Backend stores Decimals; send the raw text so the server parses it.
      return [{ date: parts[0], amount: amountStr, description: parts.slice(2).join(", ") }];
    });
  }

  function handleUpload() {
    const items = parseCsv(csvText);
    if (items.length === 0) { toast.error("No data", "Could not parse any rows."); return; }
    upload.mutate(items);
  }

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">
          Reconciliation {account ? `- ${account.name}` : ""}
        </h1>
      </div>

      {!accountId ? (
        <EmptyState
          icon={<CheckCircle />}
          title="Select an account"
          hint="Navigate to an account detail page to reconcile."
          preview={{
            none: "a populated reconciliation needs an uploaded statement, which the demo tenant cannot create",
          }}
          action={{ label: "Choose an account", to: "/settings" }}
        />
      ) : recon.isPending ? <SkeletonRows rows={5} /> : (
        <>
          <div className="grid gap-3 sm:grid-cols-4">
            <Card className="p-4">
              <div className="text-xs text-muted uppercase">Total</div>
              <div className="text-lg font-semibold">{recon.data?.summary.total || 0}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-muted uppercase">Matched</div>
              <div className="text-lg font-semibold text-gain">{recon.data?.summary.matched || 0}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-muted uppercase">Unmatched</div>
              <div className="text-lg font-semibold text-danger">{recon.data?.summary.unmatched || 0}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-muted uppercase">Difference</div>
              <div className="text-lg font-semibold">
                {formatCad(recon.data?.summary.unmatched_sum || "0", false)}
              </div>
            </Card>
          </div>

          <Card className="p-4">
            <CardTitle>Upload statement</CardTitle>
            <p className="text-sm text-muted mb-2">Paste CSV: date, amount, description (one per line)</p>
            <textarea aria-label="Statement rows (CSV)"
              value={csvText}
              onChange={e => setCsvText(e.target.value)}
              placeholder={"2026-09-01,-85.50,WALMART\n2026-09-03,-12.00,STARBUCKS"}
              className="field-ring w-full h-24 rounded-lg border border-control bg-surface px-3 py-2 text-sm font-mono"
            />
            <Button onClick={handleUpload} disabled={!csvText.trim()} className="mt-2">
              <Upload className="size-4 mr-1" /> Upload
            </Button>
          </Card>

          {recon.data && recon.data.items.length > 0 && (
            <Card className="p-4">
              <CardTitle>Statement items</CardTitle>
              <div role="region" aria-label="Statement items table" tabIndex={0} className="overflow-x-auto mt-2">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-muted uppercase text-xs">
                      <th className="pb-2 pr-4">Date</th>
                      <th className="pb-2 pr-4">Description</th>
                      <th className="pb-2 pr-4 text-right">Amount</th>
                      <th className="pb-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recon.data.items.map(item => (
                      <tr key={item.id} className="border-b border-border/50">
                        <td className="py-2 pr-4">{item.statement_date}</td>
                        <td className="py-2 pr-4 text-muted">{item.statement_description}</td>
                        <td className="py-2 pr-4 text-right font-medium">{formatCad(item.statement_amount, false)}</td>
                        <td className="py-2">
                          {item.status === "matched" ? (
                            <Badge tone="success">Matched</Badge>
                          ) : (
                            <Badge tone="warn">Unmatched</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {recon.data && recon.data.items.length === 0 && (
            <EmptyState
              icon={<CheckCircle />}
              title="No statement items"
              hint="Upload a bank statement above to start reconciling."
              preview={{
                none: "a populated reconciliation needs an uploaded statement, which the demo tenant cannot create",
              }}
              action={{ label: "Import a statement", to: "/imports" }}
            />
          )}
        </>
      )}
    </section>
  );
}
