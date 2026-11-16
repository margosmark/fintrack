import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { MASK_TEXT, useMasked } from "../../lib/mask";
import type { Proposal } from "../../api/assistant";

// docs/57 §6 — the Proposed Change card: target + summary, a Now vs With
// change table pairing `before`/`after` keys, then Apply (primary) / Dismiss.
// Presentational only: the page owns the mutations, so this component stays
// a pure render of the delta the server validated.

const FIELD_LABELS: Record<string, string> = {
  name: "Name",
  starting_balance: "Starting balance",
  llm_autopilot: "LLM autopilot",
  category_id: "Category",
  note: "Note",
  events: "Plan events",
};

const KIND_LABELS: Record<Proposal["kind"], string> = {
  "account.update": "Account",
  "transaction.update": "Transaction",
  "forecast_scenario.update": "Plan scenario",
};

function displayValue(
  field: string,
  value: unknown,
  opts: { masked: boolean; categoryName: (id: number) => string | undefined },
): string {
  if (value === null || value === undefined || value === "") return "—";
  if (field === "category_id") {
    const id = Number(value);
    return opts.categoryName(id) ?? `#${id}`;
  }
  if (Array.isArray(value)) {
    const n = value.length;
    return `${n} event${n === 1 ? "" : "s"}`;
  }
  if (typeof value === "boolean") return value ? "On" : "Off";
  // Money fields mask on a masked session (docs/46 §D2) — the server's
  // canonical decimal string otherwise, never re-derived.
  if (field === "starting_balance" && opts.masked) return MASK_TEXT;
  return String(value);
}

export interface ProposedChangeCardProps {
  proposal: Proposal;
  /** Resolve a category_id to its name (from the categories cache). */
  categoryName: (id: number) => string | undefined;
  onApply?: () => void;
  onDismiss?: () => void;
  /** True while the Apply or Dismiss request is in flight. */
  busy?: boolean;
}

export function ProposedChangeCard({
  proposal,
  categoryName,
  onApply,
  onDismiss,
  busy = false,
}: ProposedChangeCardProps) {
  const masked = useMasked();
  // `before` and `after` carry the SAME keys (the changed fields only) —
  // the table pairs them row by row (docs/57 §6).
  const fields = Object.keys(proposal.after);
  const opts = { masked, categoryName };

  return (
    <Card data-testid="assistant-proposal" className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            Proposed change · {KIND_LABELS[proposal.kind]}
          </p>
          <p className="mt-1 font-semibold">{proposal.target_label}</p>
          <p className="text-sm text-muted">{proposal.summary}</p>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-veil/[0.04] text-left text-xs uppercase tracking-wide text-muted">
              <th scope="col" className="px-3 py-2 font-medium">
                Field
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Now
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                With change
              </th>
            </tr>
          </thead>
          <tbody>
            {fields.map((field) => (
              <tr
                key={field}
                className="border-b border-border last:border-0"
                data-testid="assistant-delta-row"
              >
                <td className="px-3 py-2 text-muted">{FIELD_LABELS[field] ?? field}</td>
                <td className="px-3 py-2" data-testid="assistant-delta-now">
                  {displayValue(field, proposal.before[field], opts)}
                </td>
                <td className="px-3 py-2 font-medium" data-testid="assistant-delta-with">
                  {displayValue(field, proposal.after[field], opts)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          onClick={onApply}
          loading={busy}
          data-testid="assistant-apply"
        >
          Apply
        </Button>
        <Button variant="ghost" onClick={onDismiss} disabled={busy} data-testid="assistant-dismiss">
          Dismiss
        </Button>
      </div>
    </Card>
  );
}
