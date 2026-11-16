// RecurringPromptCard — the dismissible "these look recurring" card (M96 B2,
// docs/09 §Recurring prompt).
//
// One session, one scan: the candidate groups are fetched at most once per
// browser session (`lib/recurringSession`, sessionStorage) and re-entering the
// page reads the cache instead of recomputing. Every answer the user gives is
// written back to that cache, so an accepted or declined group cannot come
// back — and a decline writes `recurring_suppressions` server-side, which is
// the durable half of "no nagging".
//
// M165 (docs/09 §Recurring prompt → Already a bill): before a row may offer
// **Create bill**, it asks the shared `["bills"]` cache whether an enabled
// bill already tracks its payee. If one does, the button is GONE and the row
// says `covered by '<bill>' → /bills` instead — never offer to create what
// already exists. Tag / Not-recurring are untouched: coverage removes an
// affordance, never the row's answer.
//
// The privacy line is not decoration: docs/09 pins the exact sentence this
// card must carry, and `tests/recurringPrompt.test.tsx` asserts it renders
// verbatim next to the groups it is talking about.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import {
  acceptRecurringGroup,
  getRecurringCandidates,
  suppressRecurringGroup,
  type RecurringCandidate,
} from "../../api/transactions";
import { billFrequencyFor, listBills } from "../../api/bills";
import { coveringBill } from "../../lib/billCoverage";
import { CADENCE_LABELS, DISMISS_UNDO_MS } from "../../lib/recurring";
import { readCandidates, writeCandidates } from "../../lib/recurringSession";
import { apiErrorMessage, formatMoney } from "../transactions/format";
import { CoveredByLine } from "../bills/CoveredByLine";
import { Button } from "../ui/Button";
import { useToast } from "../ui/Toast";
import { useMasked } from "../../lib/mask";

/**
 * The privacy sentence docs/09 §Recurring prompt pins for this card, quoted
 * verbatim — the test asserts the SAME string is in the rendered copy.
 */
export const RECURRING_PRIVACY_COPY =
  "this detection runs entirely in your browser / on your server; nothing leaves";

const round2 = (value: number): number => Math.round(value * 100) / 100;

function sumMonthly(groups: readonly RecurringCandidate[]): number {
  return round2(groups.reduce((total, group) => total + Number(group.monthly_total), 0));
}

interface CardState {
  groups: RecurringCandidate[];
}

export function RecurringPromptCard() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const queryClient = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  // M165: the bills this user ALREADY keeps — the SAME `["bills"]` key the
  // Bills page reads, so the card borrows one cache entry instead of growing a
  // second source of truth. Every candidate is checked against it before the
  // card may offer to create a bill: "never offer to create something that
  // already is one" (docs/09 §Recurring prompt).
  //
  // An in-flight or FAILED read answers `data === undefined` → no coverage
  // claim either way, and the row keeps offering the hand-off it always did.
  // That is not a hole in the rule: the button never writes anything (M55 d6 —
  // it opens a DRAFT on /bills and only a submit creates the row), so an
  // unknown answer can only cost a review step, whereas a FALSE "covered" the
  // server would deny is a claim about the user's own data. Absence of
  // evidence is never printed as "covered".
  const bills = useQuery({ queryKey: ["bills"], queryFn: () => listBills() });
  // null = this session has not decided yet (fetch in flight).
  const [state, setState] = useState<CardState | null>(() => {
    const cached = readCandidates();
    return cached === null ? null : { groups: cached };
  });

  useEffect(() => {
    if (state !== null) return;
    let cancelled = false;
    void getRecurringCandidates()
      .then((page) => {
        if (cancelled) return;
        setState({ groups: page.groups });
        writeCandidates(page.groups);
      })
      .catch(() => {
        // A prompt is optional: a failed scan hides the card instead of
        // nagging about a suggestion the user never saw. Nothing is cached,
        // so the next entry retries.
        if (!cancelled) setState({ groups: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  /** One group answered: drop it from the card AND from this session cache. */
  function settle(rest: RecurringCandidate[]): void {
    setState({ groups: rest });
    writeCandidates(rest);
  }

  async function accept(group: RecurringCandidate): Promise<void> {
    try {
      await acceptRecurringGroup({
        group_key: group.group_key,
        transaction_ids: group.transaction_ids,
        cadence: group.cadence,
      });
      settle(state?.groups.filter((candidate) => candidate.group_key !== group.group_key) ?? []);
      // The badge reads `transactions`; Budgets/Reports read the rollups.
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["budgets"] });
      void queryClient.invalidateQueries({ queryKey: ["reports"] });
      toast.success(
        `Tagged ${group.payee} as recurring`,
        `${CADENCE_LABELS[group.cadence]} · next expected ${group.next_expected_date}`,
      );
    } catch (error) {
      toast.error("Could not tag these rows", apiErrorMessage(error));
    }
  }

  async function decline(group: RecurringCandidate): Promise<void> {
    try {
      await suppressRecurringGroup(group.group_key);
      settle(state?.groups.filter((candidate) => candidate.group_key !== group.group_key) ?? []);
    } catch (error) {
      toast.error("Could not dismiss this suggestion", apiErrorMessage(error));
    }
  }

  /**
   * M116 B1 — one-click adoption: hand this group to the Bills create form as
   * a PREFILLED draft (name + match term + cadence + next due). Nothing is
   * created here; the same rule as M55 decision 6 — the form opens, the user
   * submits, and only then is a `bill_reminders` row written.
   *
   * The recurring detector's cadence set (`weekly | biweekly | four_weekly |
   * monthly`) only overlaps the bill cadences on `monthly`, so anything else
   * says so instead of silently becoming a `one_time` bill.
   */
  function offerBill(group: RecurringCandidate): void {
    const frequency = billFrequencyFor(group.cadence);
    if (frequency === null) {
      toast.info(
        "Only monthly charges can become a bill reminder from here",
        `${group.payee} is ${CADENCE_LABELS[group.cadence].toLowerCase()} — create it on /bills if you still want it.`,
      );
      return;
    }
    const params = new URLSearchParams({
      autofill: group.payee,
      frequency,
      due: group.next_expected_date,
      // M116: the group's merchant IS the match term — that is the whole
      // "create reminder with match_term X" hand-off.
      match: group.payee,
    });
    // A monthly group's `monthly_total` IS its per-occurrence amount (the
    // detector divides by `AVERAGE_MONTH_DAYS / period`, and the period of a
    // monthly cadence is that same constant). It is SIGNED — a charge is
    // negative — and a bill's `amount` is what the bill COSTS, so the draft
    // carries the magnitude. A signed amount would make the amount band
    // (|transaction| vs bill amount) match nothing, ever.
    params.set("amount", String(Math.abs(Number(group.monthly_total))));
    navigate(`/bills?${params.toString()}`);
  }

  /**
   * Card-level dismiss (M138): decline everything still listed, once, for
   * good — but only after the 6-second Undo window closes, exactly like the
   * dashboard's subscriptions card. One vocabulary, both surfaces (docs/09
   * §Bills you probably have): nothing is POSTed while the toast is up, so
   * Undo is a local restore and `recurring_suppressions` stays write-once.
   */
  function dismissAll(): void {
    const remaining = state?.groups ?? [];
    if (remaining.length === 0) return;
    settle([]);
    toast.undo({
      title: `Dismissed ${remaining.length} suggestion${remaining.length === 1 ? "" : "s"}`,
      description: "They will not be suggested again. Undo keeps them.",
      durationMs: DISMISS_UNDO_MS,
      onCommit: () => void commitDismissal(remaining),
      onUndo: () => settle(remaining),
    });
  }

  async function commitDismissal(groups: RecurringCandidate[]): Promise<void> {
    try {
      for (const group of groups) {
        await suppressRecurringGroup(group.group_key);
      }
    } catch (error) {
      // Nothing persisted — put the session cache back, and the candidates
      // come back with it.
      settle(groups);
      toast.error("Could not dismiss these suggestions", apiErrorMessage(error));
      return;
    }
  }

  if (state === null || state.groups.length === 0) return null;

  const groups = state.groups;
  const combined = sumMonthly(groups);

  return (
    <section
      aria-labelledby="recurring-prompt-title"
      data-testid="recurring-prompt"
      className="glass mt-4 rounded-xl p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="recurring-prompt-title" className="text-sm font-semibold text-text">
            These look recurring — tag them?
          </h2>
          <p className="mt-0.5 text-xs text-muted">
            {groups.length} candidate{groups.length === 1 ? "" : "s"} ·{" "}
            <span className="tabular text-text">{formatMoney(combined, "CAD")}</span> combined per
            month. Tagging keeps the badge and the cadence on each row.
          </p>
        </div>
        {/* M138: the same header affordance the dashboard's subscriptions card
            carries — a NAMED bulk dismissal instead of a bare "×", because the
            count is the only thing that says how much is about to be lost for
            good (docs/09 §Bills you probably have). */}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          aria-label={`Dismiss all ${groups.length} suggestions for good`}
          title="Dismiss all — you will not be asked again about these"
          onClick={dismissAll}
        >
          <X className="size-3.5" aria-hidden />
          Dismiss all ({groups.length})
        </Button>
      </div>

      <ul className="mt-3 grid gap-2">
        {groups.map((group) => {
          // M165 — one lookup per row, and the answer decides whether the
          // Create-bill affordance may exist at all.
          const covered = coveringBill(bills.data ?? [], group);
          return (
            <li
              key={group.group_key}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-control px-3 py-2"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-text">{group.payee}</p>
                <p className="text-xs text-muted">
                  {CADENCE_LABELS[group.cadence]} · {group.occurrence_count} since{" "}
                  {group.first_date} · next {group.next_expected_date} ·{" "}
                  <span className="tabular text-text">
                    {formatMoney(group.monthly_total, "CAD")}
                  </span>
                  /mo
                </p>
                {/* M165: a bill already tracks this payee, so the row says so
                    instead of offering to make a second one — a statement
                    about an existing record, never a CTA (docs/09). */}
                {covered !== null && <CoveredByLine bill={covered} />}
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                <Button size="sm" variant="primary" onClick={() => void accept(group)}>
                  Tag as recurring
                </Button>
                {covered === null && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => offerBill(group)}
                    aria-label={`Create bill reminder for ${group.payee}`}
                  >
                    Create bill
                  </Button>
                )}
                <Button size="sm" variant="ghost" onClick={() => void decline(group)}>
                  Not recurring
                </Button>
              </div>
            </li>
          );
        })}
      </ul>

      <p className="mt-3 text-[11px] leading-relaxed text-muted">
        <span className="font-medium text-text">Privacy:</span> {RECURRING_PRIVACY_COPY} — one
        request to your own server, zero third-party calls, no LLM and no new dependency.
      </p>
    </section>
  );
}
