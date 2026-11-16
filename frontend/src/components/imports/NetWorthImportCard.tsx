// docs/58 §B — net-worth file card on the Imports page.
// Preview-first contract like every import here: picking a file only READS
// it (ProjectionLab JSON export or an Account/Balance spreadsheet) and shows
// the full plan — accounts to create, accounts reused by name, everything
// skipped with a reason; nothing is written until "Import" is pressed, and a
// reused account's balance is never overwritten (manual data wins).

import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileUp, Sparkles } from "lucide-react";
import { ApiError } from "../../api/client";
import {
  NetWorthCommitResult,
  NetWorthPlan,
  commitNetWorth,
  previewNetWorth,
} from "../../api/networth";
import { apiErrorMessage } from "../transactions/format";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Card, CardTitle } from "../ui/Card";
import { HelpHint } from "../ui/HelpHint";
import { EmptyState, ErrorPanel } from "../ui/EmptyState";
import { useRetryAction } from "../../lib/retry";
import { ServerSideBlocked, useServerSideWriteBlocked } from "../e2ee/ServerSideBlocked";
import { useToast } from "../ui/Toast";

const INPUT_CLASS =
  "field-ring rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70";

const ACTION_TONE = {
  create: "success",
  reuse: "neutral",
  unmapped: "warn",
} as const;

export function NetWorthImportCard() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [accountName, setAccountName] = useState("");
  const [plan, setPlan] = useState<NetWorthPlan | null>(null);
  const [committed, setCommitted] = useState<NetWorthCommitResult | null>(null);

  // M181 / docs/33 rule 7d: the commit runs SERVER-side over a file that
  // carries no ciphertext, so an encryption-enrolled tenant is refused by
  // ENROLMENT ALONE — an unlocked vault would not change the answer.
  const serverWriteBlocked = useServerSideWriteBlocked();

  const options = { accountName: accountName.trim() || undefined };

  const preview = useMutation({
    mutationFn: (upload: File) => previewNetWorth(upload, options),
    onSuccess: (result) => setPlan(result),
    onError: (err: Error) => toast.error("Could not read that file", apiErrorMessage(err)),
  });

  const commit = useMutation({
    mutationFn: (upload: File) => commitNetWorth(upload, options),
    onSuccess: (result: NetWorthCommitResult) => {
      toast.success(
        result.created > 0
          ? `Created ${result.created} account${result.created === 1 ? "" : "s"}`
          : "Nothing new — every account in that file is already here",
      );
      setCommitted(result);
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["networth"] });
    },
    onError: (err: Error) => toast.error("Import stopped before anything was written", apiErrorMessage(err)),
  });

  const commitRefusedEncryption =
    commit.error instanceof ApiError && commit.error.code === "encryption_required";

  // Re-previewing posts the SAME file and writes nothing, so a transient
  // failure has an honest re-run; the commit deliberately has none (a second
  // POST could double a partially-landed import).
  const previewRetry = useRetryAction({
    retry: preview.isError && file !== null ? () => preview.mutate(file) : null,
    pending: preview.isPending,
  });

  function runPreview(event: FormEvent) {
    event.preventDefault();
    if (file) preview.mutate(file);
  }

  function reset() {
    setFile(null);
    setAccountName("");
    setPlan(null);
    setCommitted(null);
    preview.reset();
    commit.reset();
  }

  const counts = plan?.counts;
  const pending = commit.isPending || preview.isPending;

  return (
    <Card className="p-4">
      <CardTitle
        right={
          <HelpHint text="ProjectionLab: Export → Data (JSON). Otherwise any spreadsheet with Account and Balance columns — an optional Date column imports history, latest balance wins." />
        }
      >
        Import net-worth balances
      </CardTitle>

      {!plan && (
        <form onSubmit={runPreview} className="space-y-3">
          <p className="text-sm text-muted">
            Bring over what you own and owe: accounts are created with the file's balance as
            their starting balance. Existing accounts with the same name are kept as they are —
            the preview shows the file's balance next to the one you already have, and nothing
            changes until you press Import.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-0 flex-col gap-1 text-xs text-muted">
              ProjectionLab export or balance spreadsheet
              <input
                type="file"
                accept=".json,.csv,application/json,text/csv"
                onChange={(event) => {
                  setFile(event.target.files?.[0] ?? null);
                  setPlan(null);
                }}
                className={`${INPUT_CLASS} min-w-0 w-full max-w-80`}
                data-testid="networth-file"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              Account name (only if the file has no Account column)
              <input
                type="text"
                value={accountName}
                onChange={(event) => setAccountName(event.target.value)}
                placeholder="e.g. My Portfolio"
                className={`${INPUT_CLASS} w-56`}
                data-testid="networth-account-name"
              />
            </label>
            <Button type="submit" disabled={!file || pending} loading={preview.isPending}>
              <FileUp className="mr-1 inline h-4 w-4" aria-hidden /> Preview
            </Button>
          </div>
          {preview.isError && (
            <ErrorPanel
              message={apiErrorMessage(preview.error)}
              onRetry={previewRetry.onRetry}
              retrying={previewRetry.retrying}
            />
          )}
        </form>
      )}

      {plan && counts && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="accent">
              {plan.source_format === "projectionlab_json" ? "ProjectionLab JSON" : "Balance CSV"}
            </Badge>
            <Badge tone="neutral">{plan.file_name}</Badge>
            <Badge tone={counts.accounts_create > 0 ? "success" : "neutral"}>
              {counts.accounts_create} to create
            </Badge>
            {counts.accounts_reuse > 0 && (
              <Badge tone="neutral">{counts.accounts_reuse} already here</Badge>
            )}
            {counts.balance_rows_collapsed > 0 && (
              <Badge tone="neutral">{counts.balance_rows_collapsed} older balances collapsed</Badge>
            )}
            <Badge tone={plan.skipped.length > 0 ? "warn" : "success"}>
              {plan.skipped.length} not imported
            </Badge>
          </div>

          <div
            role="region"
            aria-label="Accounts this file would import"
            tabIndex={0}
            className="overflow-x-auto"
          >
            <table className="w-full text-left text-xs" data-testid="networth-accounts">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 pr-3 font-medium">Account</th>
                  <th className="py-1 pr-3 font-medium">Action</th>
                  <th className="py-1 pr-3 font-medium">Type</th>
                  <th className="py-1 pr-3 text-right font-medium">In file</th>
                  <th className="py-1 pr-3 text-right font-medium">Yours now</th>
                </tr>
              </thead>
              <tbody>
                {plan.accounts.map((account) => (
                  <tr key={account.name} className="border-t border-control">
                    <td className="py-1.5 pr-3">
                      <span className="font-medium">{account.name}</span>
                      {account.notes.length > 0 && (
                        <ul className="mt-0.5 space-y-0.5 text-muted">
                          {account.notes.map((note) => (
                            <li key={note}>{note}</li>
                          ))}
                        </ul>
                      )}
                      {account.reason && <p className="mt-0.5 text-warn">{account.reason}</p>}
                    </td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={ACTION_TONE[account.action]}>{account.action}</Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-muted">{account.mapped_type ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {account.file_balance} {account.currency}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-muted">
                      {account.current_balance ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {plan.skipped.length > 0 && (
            <section aria-label="Not imported">
              <h3 className="mb-1 text-xs font-semibold text-warn">
                Not imported ({plan.skipped.length})
              </h3>
              <ul className="space-y-1.5 text-xs" data-testid="networth-skipped">
                {plan.skipped.map((item) => (
                  <li key={`${item.kind}-${item.name}`}>
                    <span className="font-medium">
                      {item.kind === "account" ? "Row" : item.kind}: {item.name}
                    </span>{" "}
                    <span className="text-muted">— {item.reason}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {plan.warnings.length > 0 && (
            <ul className="space-y-1 text-xs text-warn" data-testid="networth-warnings">
              {plan.warnings.map((warning) => (
                <li key={warning.code}>{warning.detail}</li>
              ))}
            </ul>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {serverWriteBlocked ? (
              <ServerSideBlocked capability="import:networth" id="networth-import-blocked" />
            ) : (
              <Button
                variant="primary"
                disabled={!plan.can_commit || commit.isPending || committed !== null}
                loading={commit.isPending}
                onClick={() => file && commit.mutate(file)}
                data-testid="networth-import"
              >
                <Sparkles className="mr-1 inline h-4 w-4" aria-hidden />
                Import {counts.accounts_create} new account
                {counts.accounts_create === 1 ? "" : "s"}
              </Button>
            )}
            <Button variant="ghost" onClick={reset} disabled={commit.isPending}>
              Start over
            </Button>
            {committed && (
              <span className="text-xs text-gain" data-testid="networth-done">
                Created {committed.created}, kept {committed.reused} existing — balances you
                already had were left untouched.
              </span>
            )}
          </div>

          {commit.isError && commitRefusedEncryption && (
            <ServerSideBlocked capability="import:networth" id="networth-import-refused" />
          )}
          {commit.isError && !commitRefusedEncryption && (
            <ErrorPanel message={apiErrorMessage(commit.error)} />
          )}
        </div>
      )}

      {plan && !counts && (
        <EmptyState
          icon={<FileUp aria-hidden />}
          title="Nothing to import"
          hint="This file has no account balances."
          preview={{ src: "/help/imports.png", alt: "The Imports page with a committed batch in the history list" }}
          action={{ label: "Start over", onClick: reset }}
        />
      )}
    </Card>
  );
}
