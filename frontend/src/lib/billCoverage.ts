// billCoverage — "is this candidate ALREADY a bill?" (M165, docs/09 §Recurring
// prompt + §Bills you probably have).
//
// The rule the two suggestion cards now keep: **never offer to create a bill
// for something that already is one.** A candidate whose payee an ENABLED bill
// already tracks is not a creation opportunity — it is a fact to report, so
// the row says "covered by '<bill>' → /bills" and the Create-bill CTA is gone.
//
// The check runs in the browser against the `["bills"]` query every surface
// already shares (`GET /bills` — no new endpoint, no server round-trip per
// candidate), and it deliberately reuses:
//
//   * `lib/payeeKey.ts` — the ONE grouping key, so the comparison means
//     exactly what the server's bill matcher means (a statement's spacing
//     variants are one payee);
//   * `api/bills`' `readTerms`/`readLogic` — the stored rule as the wire
//     carries it (M121 terms/logic, with the pre-M121 `match_term` fallback);
//   * `billFrequencyFor` — the cadence vocabulary the Create-bill hand-off
//     itself maps through.
//
// What it does NOT decide: amount and category scope. Those filter which
// PAYMENTS a bill counts (docs/25 §M116 D2); they do not change the answer to
// "does a bill already exist for this payee", which is the question the card
// is asking.
import {
  billFrequencyFor,
  readLogic,
  readTerms,
  type Bill,
  type BillMatchLogic,
  type BillMatchMode,
} from "../api/bills";
import type { RecurringCadence } from "../api/transactions";
import { normalizePayeeKey } from "./payeeKey";

/**
 * One already-normalized term against the one already-normalized subject —
 * the port of `services/match_terms.py::_term_hits`, direction included
 * (the TERM is what has to sit inside the SUBJECT).
 */
function termHits(subjectKey: string, termKey: string, mode: BillMatchMode): boolean {
  if (subjectKey === "" || termKey === "") return false;
  if (mode === "exact") return subjectKey === termKey;
  if (mode === "starts_with") return subjectKey.startsWith(termKey);
  return subjectKey.includes(termKey);
}

/**
 * The bill's WHOLE rule over one candidate payee: every term normalized the
 * same way as the subject, ANY (widening) or ALL (narrowing) per
 * `match_logic`, and an empty term list matches NOTHING — a rule with no
 * terms is no claim, never "covers everything" (docs/25 §M121).
 */
function termsCover(
  terms: readonly string[],
  logic: BillMatchLogic,
  mode: BillMatchMode,
  subjectKey: string,
): boolean {
  const hits = terms.map((term) => termHits(subjectKey, normalizePayeeKey(term), mode));
  return logic === "all" ? hits.length > 0 && hits.every(Boolean) : hits.some(Boolean);
}

/**
 * Does a bill with this cadence track a stream of this cadence?
 *
 * Tolerance, on purpose: the detector fits `monthly` to a charge that lands on
 * the 26th–32nd with the day-of-month spread of a month, so a bill that
 * records `monthly` covers it however the real gaps fall (28-day February
 * included). A `four_weekly` fit is a 28-day cycle — a month by another name,
 * so a monthly bill covers it too. Weekly and biweekly have no bill cadence
 * at all (`billFrequencyFor` returns null), and a `one_time` bill is not a
 * recurring stream, so neither is claimed as coverage.
 */
export function billCoversCadence(cadence: RecurringCadence, frequency: string): boolean {
  const mapped = billFrequencyFor(cadence);
  if (mapped !== null) return frequency === mapped;
  return cadence === "four_weekly" && frequency === "monthly";
}

/**
 * The bill that already covers this candidate, or null when there is none.
 *
 * `null` is the ONLY answer that may keep the Create-bill CTA: an unloaded or
 * failed bills query yields no evidence, and an absence of evidence is never
 * printed as "covered" — the card falls back to offering the bill it always
 * offered.
 */
export function coveringBill(
  bills: readonly Bill[],
  candidate: { readonly payee: string; readonly cadence: RecurringCadence },
): Bill | null {
  const subjectKey = normalizePayeeKey(candidate.payee);
  if (subjectKey === "") return null;
  for (const bill of bills) {
    if (!bill.enabled) continue;
    if (!billCoversCadence(candidate.cadence, bill.frequency)) continue;
    if (termsCover(readTerms(bill), readLogic(bill), bill.match_mode, subjectKey)) return bill;
  }
  return null;
}

/**
 * The muted line both cards print instead of a Create-bill button:
 * `covered by 'Google *Youtubepremium' → /bills`.
 *
 * Named for what it is — a statement about an existing record, not an action
 * — so it can never be mistaken for the CTA it replaced.
 */
export function coveredByLine(bill: Bill): string {
  return `covered by '${bill.name}' → /bills`;
}
