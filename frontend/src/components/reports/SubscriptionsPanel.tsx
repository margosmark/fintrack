import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { billAutofillParams, listBills } from "../../api/bills";
import {
  getRecurringCandidates,
  suppressRecurringGroup,
  type RecurringCandidate,
} from "../../api/transactions";
import { Button } from "../ui/Button";
import { Card, CardTitle } from "../ui/Card";
import { CoveredByLine } from "../bills/CoveredByLine";
import { EmptyState } from "../ui/EmptyState";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { HelpHint } from "../ui/HelpHint";
import { apiErrorMessage } from "../transactions/format";
import { SkeletonRows } from "../ui/Skeleton";
import { Badge } from "../ui/Badge";
import { useToast } from "../ui/Toast";
import { CalendarPlus, Repeat, X } from "lucide-react";
import { coveringBill } from "../../lib/billCoverage";
import { CADENCE_LABELS, DISMISS_UNDO_MS } from "../../lib/recurring";
import { useMasked } from "../../lib/mask";
import { formatCad } from "../../lib/format";

/**
 * M138 (docs/09 §Bills you probably have).
 *
 * This card used to be fed by the client-side M27 detector in `s3Engine.ts`
 * — "2 or more charges", average-gap bucketing, no amount band at all — which
 * is what put FILM.CA / LOBLAW / TIM HORTONS / SHELL on the dashboard and,
 * because the card had no dismissal of any kind, left no way to remove them.
 * Both of Mark's complaints came from that one substrate (M133's verdict line).
 *
 * It is now fed by `GET /transactions/recurring/candidates?source=subscriptions`
 * — the SAME strict engine the M96 prompt uses (`services/recurring.py`: ≥3
 * occurrences, ±15% same-sign band, every gap must fit one cadence) — and it
 * can dismiss. Three invariants are load-bearing:
 *
 * * **the key is per payee AND per source** (`dismiss_key`, ending
 *   `|subscriptions`), so dropping a suggestion here never answers the M96
 *   prompt for the same merchant, and the reverse;
 * * **nothing is written while the Undo toast is up.** The suppression POST
 *   fires only when the 6-second window closes — Undo needs no unsuppress
 *   route, and `recurring_suppressions` stays write-once (docs/09).
 * * **the card never offers to create what already exists** (M165): a row
 *   whose payee an enabled bill already tracks loses its
 *   `Create bill reminder` button and prints the shared
 *   `covered by '<bill>' → /bills` line instead (docs/09 §Recurring prompt →
 *   Already a bill), while `Not a bill` stays — coverage removes an
 *   affordance, never the row's answer.
 */

/** M133 D-4: the junk module was the tallest thing on the page (29%/31%). */
export const ROW_CAP = 5;

/**
 * The strict rule, verbatim, in the card's own help text — because the rule
 * IS the promise. M133's evidence was a card whose hint read "2 or more
 * charges", i.e. the surface was quoting the bug back at the user.
 */
const STRICT_RULE_HINT =
  "Found automatically from repeated payees that clear the strict rule: at least 3 charges in the last 13 months, the same amount within ±15% (same sign), and a cadence that fits every single gap. There is no sensitivity knob — this rule is the only one.";

export function SubscriptionsPanel() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["recurring", "candidates", "subscriptions"],
    queryFn: () => getRecurringCandidates("subscriptions"),
  });
  // M165 — the same `["bills"]` cache entry the M96 prompt card and the Bills
  // page read: a suggestion whose payee an ENABLED bill already tracks is a
  // fact to report, not a create-bill opportunity (docs/09 §Bills you probably
  // have). Undefined data = unknown, and unknown never claims coverage.
  const bills = useQuery({ queryKey: ["bills"], queryFn: () => listBills() });
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();

  /** Rows hidden LOCALLY while their Undo window is still running. */
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());
  /** How many suggestions this session has dismissed (drives the empty state). */
  const [dismissedCount, setDismissedCount] = useState(0);
  const [showAll, setShowAll] = useState(false);

  const groups = useMemo(
    () => (data?.groups ?? []).filter((group) => !dismissed.has(group.dismiss_key)),
    [data, dismissed],
  );

  /**
   * The card's total is money going OUT. A credit (a payroll deposit clears
   * the strict rule as happily as a bill does) still earns its row — M133's
   * fixture keeps the strict six — but it must never inflate a figure under a
   * headline that says "Bills" (M133 D-11's sign complaint, pre-empted here).
   */
  const billsMonthly = useMemo(
    () =>
      groups.reduce((total, group) => {
        const value = Number(group.monthly_total);
        return value < 0 ? total + value : total;
      }, 0),
    [groups],
  );

  function restore(keys: readonly string[]): void {
    setDismissed((current) => {
      const next = new Set(current);
      for (const key of keys) next.delete(key);
      return next;
    });
    setDismissedCount((count) => Math.max(0, count - keys.length));
  }

  async function commit(keys: readonly string[]): Promise<void> {
    try {
      for (const key of keys) await suppressRecurringGroup(key);
    } catch (err) {
      // Nothing persisted — put the rows back rather than leave a dismissal
      // the server never heard about.
      restore(keys);
      toast.error("Could not dismiss these suggestions", apiErrorMessage(err));
      return;
    }
    void queryClient.invalidateQueries({ queryKey: ["recurring", "candidates"] });
  }

  /**
   * One dismissal, one vocabulary: hide locally, offer a 6-second Undo, and
   * only write the permanent suppression when the window closes. No modal —
   * a confirmation dialog for "not a bill" costs more attention than the
   * action itself (M133 `dismissal_design`).
   */
  function dismiss(candidates: readonly RecurringCandidate[]): void {
    const keys = candidates.map((candidate) => candidate.dismiss_key);
    if (keys.length === 0) return;
    setDismissed((current) => {
      const next = new Set(current);
      for (const key of keys) next.add(key);
      return next;
    });
    setDismissedCount((count) => count + keys.length);
    toast.undo({
      title:
        candidates.length === 1
          ? `Dismissed ${candidates[0].payee}`
          : `Dismissed ${candidates.length} suggestions`,
      description: "They will not be suggested again. Undo keeps them.",
      durationMs: DISMISS_UNDO_MS,
      onCommit: () => void commit(keys),
      onUndo: () => restore(keys),
    });
  }

  /**
   * M55 (docs/25): hand a detected candidate to the /bills draft form.
   * Nothing is created here — the user still reviews and submits the form.
   * Cadences with no bill equivalent (`weekly`/`biweekly`/`four_weekly`)
   * never navigate.
   */
  function seedBillDraft(group: RecurringCandidate) {
    const params = billAutofillParams({
      payee: group.payee,
      frequency: group.cadence,
      avg_amount: group.monthly_total,
      next_expected: group.next_expected_date,
    });
    if (params === null) {
      toast.error(
        "Can't create a bill reminder",
        `${CADENCE_LABELS[group.cadence]} charges can't become a bill reminder from here (monthly only) — create it on /bills if you still want it.`,
      );
      return;
    }
    navigate(`/bills?${params.toString()}`);
  }

  if (isLoading) return <SkeletonRows rows={3} />;
  if (error) return <QueryErrorPanel query={{ error, isFetching, refetch }} />;

  if (!data || groups.length === 0) {
    // NEVER "No subscriptions detected" after a dismissal — that claim would
    // be false (the user dismissed them) and it is off-vocabulary now that
    // the card is about bills (M133 `dismissal_design`).
    if (dismissedCount > 0) {
      return (
        <EmptyState
          icon={<Repeat />}
          title={`Dismissed ${dismissedCount} suggestion${dismissedCount === 1 ? "" : "s"}`}
          hint="Undo was offered for 6 seconds. These will not come back — if you were wrong, add the bill by hand on the Bills page."
          preview={{
            src: "/help/dashboard.png",
            alt: "The Dashboard with a spending donut, a net-worth trend and recent transactions",
          }}
          action={{ label: "Go to Bills", to: "/bills" }}
        />
      );
    }
    // A user who already defined bills has taken this notice's advice —
    // showing it again is noise (t_cc3b35f1). Absence of the notice is the
    // only statement made, and it is made by rendering nothing: the panel
    // never claims anything about the user's bills. While the shared
    // ["bills"] query is still pending, show the same skeleton as the
    // candidates-loading branch so the notice never flashes then vanishes;
    // an ERRORED bills query falls through to the notice — unknown must
    // never be read as "has bills".
    if (bills.isLoading) return <SkeletonRows rows={3} />;
    if ((bills.data?.length ?? 0) > 0) return null;
    return (
      <EmptyState
        icon={<Repeat />}
        title="No bill suggestions right now"
        hint="Nothing in the last 13 months cleared the strict recurring rule (3+ charges, the same amount, a cadence that fits every gap). Import more transactions, or add a bill by hand."
        preview={{
          src: "/help/dashboard.png",
          alt: "The Dashboard with a spending donut, a net-worth trend and recent transactions",
        }}
        action={{ label: "Import a statement", to: "/imports" }}
      />
    );
  }

  const shown = showAll ? groups : groups.slice(0, ROW_CAP);

  return (
    <div className="space-y-3">
      <Card className="p-4">
        {/* M133: "Bills you probably have" — honesty about the inference, and
            the header carries the bulk dismissal next to it. */}
        <CardTitle
          right={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => dismiss(groups)}
              aria-label={`Dismiss all ${groups.length} suggestions`}
            >
              <X className="size-4" aria-hidden />
              Dismiss all ({groups.length})
            </Button>
          }
        >
          Bills you probably have
        </CardTitle>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="text-muted">
            {groups.length} suggestion{groups.length === 1 ? "" : "s"}
            <HelpHint
              text={`${STRICT_RULE_HINT} The total counts money going out; a credit still gets a row but never inflates the figure.`}
            />
          </span>
          <span className="font-medium text-text">
            {formatCad(String(Math.abs(billsMonthly)))}/mo
          </span>
          <span className="text-muted">({formatCad(String(Math.abs(billsMonthly) * 12))}/yr)</span>
        </p>
      </Card>
      <div className="space-y-2">
        {shown.map((group) => {
          // M165 — never offer to create what already exists (docs/09).
          const covered = coveringBill(bills.data ?? [], group);
          return (
            <Card
              key={group.group_key}
              className="flex flex-col items-start gap-3 p-4 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex min-w-0 flex-col gap-1">
                <div className="flex min-w-0 flex-wrap items-center gap-3">
                  <span className="min-w-0 break-words font-medium text-text">{group.payee}</span>
                  <Badge tone="neutral">{CADENCE_LABELS[group.cadence]}</Badge>
                </div>
                {covered !== null && <CoveredByLine bill={covered} />}
              </div>
              <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-sm">
                <span className="text-text font-medium tabular">
                  {formatCad(group.monthly_total)}
                </span>
                <span className="text-muted">{group.occurrence_count}x</span>
                <span className="text-muted">Next: {group.next_expected_date}</span>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                {covered === null && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => seedBillDraft(group)}
                    aria-label={`Create bill reminder from ${group.payee}`}
                  >
                    <CalendarPlus className="size-4" aria-hidden />
                    Create bill reminder
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => dismiss([group])}
                  aria-label={`Not a bill — ${group.payee}`}
                >
                  <X className="size-4" aria-hidden />
                  Not a bill
                </Button>
              </div>
            </Card>
          );
        })}
        {groups.length > ROW_CAP && (
          <div className="flex justify-center">
            <Button variant="ghost" size="sm" onClick={() => setShowAll((open) => !open)}>
              {showAll ? "Show fewer" : `Show all (${groups.length})`}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
