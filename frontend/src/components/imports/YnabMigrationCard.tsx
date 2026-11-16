// M76 / docs/38 — YNAB budget migration card on the Imports page.
// Preview-first contract: picking a file only READS the export and shows the
// full mapping (accounts, categories, targets, every row); nothing is written
// until "Import" is pressed, and rows that cannot be imported are listed with
// their row number and reason instead of being dropped.

import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, Sparkles } from "lucide-react";
import { ApiError } from "../../api/client";
import { listAccounts } from "../../api/accounts";
import {
  YnabCommitResult,
  YnabMigrationPlan,
  commitYnab,
  previewYnab,
} from "../../api/ynab";
import type { YnabIssue } from "../../api/ynab";
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

const ROW_LIMIT = 200;

function IssueList({ issues, tone }: { issues: YnabIssue[]; tone: "danger" | "warn" }) {
  const shown = issues.slice(0, ROW_LIMIT);
  const color = tone === "danger" ? "text-danger" : "text-warn";
  return (
    <ul className="space-y-1.5 text-xs" data-testid={`ynab-${tone}-list`}>
      {shown.map((issue) => (
        <li key={`${issue.code}-${issue.index ?? "file"}`} className={color}>
          <span className="font-medium">
            {issue.index === null ? "File" : `Row ${issue.index}`}:
          </span>{" "}
          {issue.reason}
        </li>
      ))}
      {issues.length > shown.length && (
        <li className="text-muted">
          +{issues.length - shown.length} more rows need a decision before they can be imported.
        </li>
      )}
    </ul>
  );
}

export function YnabMigrationCard() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<YnabMigrationPlan | null>(null);
  const [targetAccountId, setTargetAccountId] = useState<string>("");
  const [showWarnings, setShowWarnings] = useState(false);
  const [committed, setCommitted] = useState<YnabCommitResult | null>(null);

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => listAccounts(200) });
  const options = { accountId: targetAccountId ? Number(targetAccountId) : null };

  // G3 (M134 §5.1 / M136 P0-3) — "locked vault destroys work" — re-keyed as an
  // M181 server-side gate (docs/33 §Server-first computing rule 7d).
  //
  // `POST /imports/ynab/commit` refuses an encryption-enrolled tenant outright
  // (`backend/app/api/ynab.py`): the importer runs SERVER-side and produces no
  // cipher fields, so unlocking the vault cannot enable it. The old
  // `useEncryptionRequired() && !vaultUnlocked` key was wrong twice over: it
  // left an enrolled + UNLOCKED tenant looking at a live Import control the
  // API would refuse anyway, and the refusal it then rendered (`VaultLocked
  // blocked`) offered "unlock the vault" — a remedy that cannot work for a
  // server-side upload (the false-remedy rule 7d bans). ENROLMENT ALONE now,
  // exactly like the rest of `ServerSideBlocked`'s surfaces; the 422 branch
  // below stays as defense-in-depth and tells the same story as this gate.
  const serverWriteBlocked = useServerSideWriteBlocked();

  const preview = useMutation({
    mutationFn: (upload: File) => previewYnab(upload, options),
    onSuccess: (result) => {
      setPlan(result);
      setShowWarnings(false);
    },
    onError: (err: Error) => toast.error("Could not read that export", apiErrorMessage(err)),
  });

  const commit = useMutation({
    mutationFn: (upload: File) => commitYnab(upload, options),
    onSuccess: (result: YnabCommitResult) => {
      toast.success(
        result.net_new > 0
          ? `Imported ${result.net_new} transactions across ${result.batches.length} accounts`
          : "Nothing new to import — every row was already in fin_tracker",
      );
      // Keep the plan on screen: the summary below tells the user what landed.
      setCommitted(result);
      void queryClient.invalidateQueries({ queryKey: ["imports"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["categories"] });
    },
    onError: (err: Error) => toast.error("Import stopped before anything was written", apiErrorMessage(err)),
  });
  // Branch on the envelope's `code`, never on its `detail` (docs/08 §Error
  // envelope): "encryption required" is a different story from a bad file.
  const commitRefusedEncryption =
    commit.error instanceof ApiError && commit.error.code === "encryption_required";

  // F-RETRY: re-previewing posts the SAME export file that is still in state
  // and writes nothing (the import happens at commit), so a transient failure
  // has an honest re-run. The commit below deliberately has none — see its
  // panel.
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
    setPlan(null);
    setCommitted(null);
    preview.reset();
    commit.reset();
  }

  const counts = plan?.counts;
  const rows = plan?.transactions;
  const pending = commit.isPending || preview.isPending;

  return (
    <Card className="p-4">
      <CardTitle
        right={
          <HelpHint text="Whole budget: save the JSON from YNAB's API (Settings → Developer, GET /plans/last-used). No token: export one account's register CSV." />
        }
      >
        Import a YNAB budget
      </CardTitle>

      {!plan && (
        <form onSubmit={runPreview} className="space-y-3">
          <p className="text-sm text-muted">
            Bring over accounts, categories, group tags, targets, and every transaction. The
            preview below shows exactly what would change — the import itself happens when you
            press Import, and only after you have seen this plan.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            {/* min-w-0 + w-full: a file input's intrinsic width (~325px) is
                wider than the row at <=360 and, as the label's min-content
                floor, pushed this field past the card edge (M82-F3). */}
            <label className="flex min-w-0 flex-col gap-1 text-xs text-muted">
              YNAB export (JSON or register CSV)
              <input
                type="file"
                accept=".json,.csv,application/json,text/csv"
                onChange={(event) => {
                  setFile(event.target.files?.[0] ?? null);
                  setPlan(null);
                }}
                className={`${INPUT_CLASS} min-w-0 w-full max-w-80`}
                data-testid="ynab-file"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              Rows with no account go to
              <select
                value={targetAccountId}
                onChange={(event) => setTargetAccountId(event.target.value)}
                className={INPUT_CLASS}
                data-testid="ynab-target-account"
              >
                <option value="">Ask me each time</option>
                {(accounts.data?.items ?? []).map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.name}
                  </option>
                ))}
              </select>
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

      {plan && counts && rows && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="accent">{plan.budget_name}</Badge>
            <Badge tone="neutral">{plan.source_format === "json" ? "Budget JSON" : "Register CSV"}</Badge>
            <Badge tone={rows.importable > 0 ? "success" : "neutral"}>
              {rows.importable} to import
            </Badge>
            {rows.duplicates + rows.duplicates_pending > 0 && (
              <Badge tone="warn">
                {rows.duplicates + rows.duplicates_pending} already here
              </Badge>
            )}
            {rows.transfer_pairs > 0 && <Badge tone="neutral">{rows.transfer_pairs} transfers</Badge>}
            <Badge tone={rows.unmappable.length > 0 ? "danger" : "success"}>
              {rows.unmappable.length} rows cannot be imported
            </Badge>
          </div>

          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
            <div>
              <dt className="text-muted">Accounts</dt>
              <dd>
                {counts.accounts_create} new, {counts.accounts_reuse} kept
                {counts.accounts_unmapped > 0 ? `, ${counts.accounts_unmapped} skipped` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-muted">Categories</dt>
              <dd>
                {counts.categories_create} new, {counts.categories_reuse} kept (from{" "}
                {counts.groups_create + counts.groups_reuse} groups)
              </dd>
            </div>
            <div>
              <dt className="text-muted">Tags &amp; targets</dt>
              <dd>
                {counts.tags_create} new tags, {counts.budgets_create + counts.goals_create} targets
                {counts.targets_unsupported > 0 ? `, ${counts.targets_unsupported} not supported` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-muted">Duplicate check</dt>
              <dd>
                {rows.duplicates} matched now, {rows.duplicates_pending} checked when you press
                Import
              </dd>
            </div>
          </dl>

          {plan.accounts.filter((account) => account.action === "unmapped").length > 0 && (
            <ul className="space-y-1 text-xs text-warn" data-testid="ynab-unmapped-accounts">
              {plan.accounts
                .filter((account) => account.action === "unmapped")
                .map((account) => (
                  <li key={account.ynab_id}>
                    <span className="font-medium">{account.name}</span>: {account.reason}
                  </li>
                ))}
            </ul>
          )}

          {rows.unmappable.length > 0 && (
            <section aria-label="Rows that cannot be imported">
              <h3 className="mb-1 text-xs font-semibold text-danger">
                Rows that cannot be imported ({rows.unmappable.length})
              </h3>
              <IssueList issues={rows.unmappable} tone="danger" />
            </section>
          )}

          {rows.warnings.length > 0 && (
            <section aria-label="Rows imported with a caveat">
              <button
                type="button"
                onClick={() => setShowWarnings((value) => !value)}
                className="text-xs font-semibold text-warn underline-offset-2 hover:underline"
              >
                {rows.warnings.length} rows import with a caveat {showWarnings ? "▾" : "▸"}
              </button>
              {showWarnings && <IssueList issues={rows.warnings} tone="warn" />}
            </section>
          )}

          {plan.notes.length > 0 && (
            <ul className="space-y-1 text-xs text-muted" data-testid="ynab-notes">
              {plan.notes.map((note) => (
                <li key={note.code}>{note.reason}</li>
              ))}
            </ul>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {/* G3 / M181: pre-emptive gate — the commit control is REPLACED
                by the reason it cannot run, before anything is typed or
                uploaded. Enrolment alone: an unlocked vault does not bring
                this control back, because the SERVER cannot write ciphers. */}
            {serverWriteBlocked ? (
              <ServerSideBlocked capability="import:ynab" id="ynab-import-blocked" />
            ) : (
              <Button
                variant="primary"
                disabled={rows.importable === 0 || commit.isPending || committed !== null}
                loading={commit.isPending}
                onClick={() => file && commit.mutate(file)}
                data-testid="ynab-import"
              >
                <Sparkles className="mr-1 inline h-4 w-4" aria-hidden />
                Import {rows.importable} transactions
              </Button>
            )}
            <Button variant="ghost" onClick={reset} disabled={commit.isPending}>
              Start over
            </Button>
            {rows.importable === 0 && (
              <span className="text-xs text-muted">Everything in this file is already here.</span>
            )}
            {committed && (
              <span className="text-xs text-gain" data-testid="ynab-done">
                {committed.net_new} new transactions staged for review below.
              </span>
            )}
          </div>
          {/* The one case that deserves its own copy instead of the raw
              envelope: the server said "encryption required", which means the
              TENANT is enrolled — not that the vault was locked. Same
              capability notice as the gate above (defense-in-depth: this
              branch is reachable only if a future change re-enables the
              button), so the two can never tell two stories. */}
          {commit.isError && commitRefusedEncryption && (
            <ServerSideBlocked capability="import:ynab" id="ynab-import-refused" />
          )}
          {/* F-RETRY: NO retry on a failed commit, by design. The import may
              have partially landed (a second POST would double it) and the
              encryption refusal above is a POLICY state the server repeats
              until the tenant's enrolment changes — unlocking the vault would
              NOT help it — so neither is a failure a repeat can fix. */}
          {commit.isError && !commitRefusedEncryption && (
            <ErrorPanel message={apiErrorMessage(commit.error)} />
          )}
        </div>
      )}

      {plan && !rows && (
        <EmptyState
          icon={<FileUp aria-hidden />}
          title="Nothing to import"
          hint="This export has no transactions."
          preview={{ src: "/help/imports.png", alt: "The Imports page with a committed batch in the history list" }}
          action={{ label: "Start over", onClick: reset }}
        />
      )}
    </Card>
  );
}
