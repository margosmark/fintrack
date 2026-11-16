// RuleDryRun (docs/31): dry-run a saved rule plus targeted apply.
// Preview shows what the rule would match (count + sample) without writing;
// Apply backfills with an explicit scope (all history / date range /
// forward-only) and an overwrite toggle.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  applyRule,
  previewRule,
  type Rule,
  type RuleApplyMode,
  type RuleApplyResult,
  type RulePreview,
} from "../../api/rules";
import { apiErrorMessage } from "../transactions/format";
import { Button } from "../ui/Button";
import { Card, CardTitle } from "../ui/Card";
import { EmptyState, ErrorPanel } from "../ui/EmptyState";
import { HelpHint } from "../ui/HelpHint";
import { useToast } from "../ui/Toast";

const FIELD =
  "field-ring rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors hover:border-accent/70";

export function RuleDryRun({ rule, onClose }: { rule: Rule; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [mode, setMode] = useState<RuleApplyMode>("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [onlyUnassigned, setOnlyUnassigned] = useState(true);
  const [overwrite, setOverwrite] = useState(false);

  const preview = useMutation({
    mutationFn: () =>
      previewRule({
        match_type: rule.match_type,
        pattern: rule.pattern,
        from: mode === "range" && from !== "" ? from : undefined,
        to: mode === "range" && to !== "" ? to : undefined,
        only_unassigned: onlyUnassigned,
      }),
    onError: (e: Error) => toast.error("Preview failed", apiErrorMessage(e)),
  });

  const apply = useMutation({
    mutationFn: () =>
      applyRule(rule.id, {
        mode,
        from: mode === "range" && from !== "" ? from : undefined,
        to: mode === "range" && to !== "" ? to : undefined,
        overwrite,
      }),
    onSuccess: (result: RuleApplyResult) => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
      toast.success(
        "Rule applied",
        result.note ?? `${result.applied_count} of ${result.matched_count} matched updated.`,
      );
    },
    onError: (e: Error) => toast.error("Apply failed", apiErrorMessage(e)),
  });

  const rangeReady = mode !== "range" || (from !== "" && to !== "");

  return (
    <Card className="mt-4 p-4">
      <div className="flex items-center justify-between gap-2">
        <CardTitle>
          Test run: <span className="font-mono">{rule.pattern}</span>
        </CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-x-4 gap-y-3">
        <fieldset className="flex items-center gap-3">
          <span className="text-xs text-muted">Scope</span>
          <HelpHint text="Forward files only new transactions from now on. All re-files every match. A date range re-files only matches inside the range. Only-unassigned skips transactions that already have a category." />
          {(["all", "range", "forward"] as const).map((m) => (
            <label key={m} className="flex items-center gap-1.5 text-xs">
              <input
                type="radio"
                name={`dryrun-scope-${rule.id}`}
                value={m}
                checked={mode === m}
                onChange={() => setMode(m)}
                className="accent-accent"
              />
              {m === "all" ? "All history" : m === "range" ? "Date range" : "Forward only"}
            </label>
          ))}
        </fieldset>
        {mode === "range" && (
          <>
            <label className="text-xs text-muted">
              From{" "}
              <input
                type="date"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
                className={FIELD}
              />
            </label>
            <label className="text-xs text-muted">
              To{" "}
              <input
                type="date"
                value={to}
                onChange={(e) => setTo(e.target.value)}
                className={FIELD}
              />
            </label>
          </>
        )}
        <label className="flex items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={onlyUnassigned}
            onChange={(e) => setOnlyUnassigned(e.target.checked)}
            className="accent-accent"
          />
          Only unassigned
        </label>
        <label className="flex items-center gap-1.5 text-xs" title="Also re-categorize already assigned rows on apply">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
            className="accent-accent"
          />
          Overwrite on apply
        </label>
      </div>

      <div className="mt-3 flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!rangeReady || preview.isPending}
          loading={preview.isPending}
          onClick={() => preview.mutate()}
        >
          Run preview
        </Button>
        <Button
          variant="primary"
          size="sm"
          disabled={!rangeReady || apply.isPending}
          loading={apply.isPending}
          onClick={() => apply.mutate()}
        >
          Apply {mode === "all" ? "to all" : mode === "range" ? "to range" : "forward only"}
        </Button>
      </div>

      {preview.isError && (
        <div className="mt-3">
          <ErrorPanel message={apiErrorMessage(preview.error)} />
        </div>
      )}
      {preview.data !== undefined && (
        <PreviewResult preview={preview.data} onClose={onClose} />
      )}
      {apply.data !== undefined && (
        <p className="mt-3 text-sm text-muted">
          Applied {apply.data.applied_count} of {apply.data.matched_count} matched.{" "}
          {apply.data.note ??
            (apply.data.applied_count < apply.data.matched_count
              ? "The rest already have a category — tick Overwrite on apply to re-categorize them."
              : "")}
        </p>
      )}
    </Card>
  );
}

function PreviewResult({ preview, onClose }: { preview: RulePreview; onClose: () => void }) {
  if (preview.matched_count === 0) {
    return (
      <div className="mt-3">
        <EmptyState
          icon={<span aria-hidden>∅</span>}
          title="No matches in scope"
          hint={
            preview.assigned_skipped > 0
              ? `${preview.assigned_skipped} matching row${preview.assigned_skipped === 1 ? " is" : "s are"} already assigned — uncheck Only unassigned to include ${preview.assigned_skipped === 1 ? "it" : "them"}.`
              : "Widen the scope or adjust the pattern."
          }
          preview={{ src: "/help/rules.png", alt: "The Rules page listing automation rules" }}
          action={{
            label: "Back to the rules list",
            onClick: onClose,
            ariaLabel: "Back to the rules list — No matches in scope",
          }}
        />
      </div>
    );
  }
  return (
    <div className="mt-3">
      <p className="text-sm">
        <span className="font-semibold">{preview.matched_count}</span>{" "}
        <span className="text-muted">transaction{preview.matched_count === 1 ? "" : "s"} would match</span>
        {preview.assigned_skipped > 0 && (
          <span className="text-muted">
            {" "}({preview.assigned_skipped} already assigned, hidden by filter)
          </span>
        )}
      </p>
      <div role="region" aria-label="Test run matches table" tabIndex={0} className="mt-2 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-veil/[0.06] text-left text-[11px] uppercase tracking-widest text-muted">
              <th className="py-1.5 pr-3 font-medium">Date</th>
              <th className="py-1.5 pr-3 font-medium">Payee</th>
              <th className="py-1.5 pr-3 font-medium">Amount</th>
              <th className="py-1.5 font-medium">Current category</th>
            </tr>
          </thead>
          <tbody>
            {preview.items.map((item) => (
              <tr key={item.id} className="border-b border-veil/[0.04]">
                <td className="py-1.5 pr-3 font-mono text-xs">{item.date}</td>
                <td className="py-1.5 pr-3">{item.payee}</td>
                <td className="py-1.5 pr-3 font-mono text-xs">{item.amount_native}</td>
                <td className="py-1.5 text-xs text-muted">{item.category_name ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
