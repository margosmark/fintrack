// cash-flow calendar (M52, docs/36) — one month grid over the three sources
// that already own their own math: bill reminders (docs/25), the M29 dividend
// calendar and the CONFIRMED subscription set (M170, `confirmed_subscriptions`).
// The API composes them; this module only carries the pinned shape.
//
// All money fields are Decimal values serialized as strings and SIGNED
// (dividend positive, bill/subscription negative). `amount` + `currency` are
// an all-or-nothing pair: `amount` is the figure in `currency`, and both are
// null when the source carries no honest figure — a bill with no amount, or
// an accepted group whose rows do not agree on one currency (mixed/unknown).
// M170 follow-up: a native amount is never rendered as CAD (docs/36 §Currency).
//
// M170 (docs/36 §M170): a `subscription` event is CONFIRMED state — a bill or
// a recurring group the user accepted on /transactions — never the detector's
// guess. That is why the payload also carries `confirmed_subscription_count`:
// the month's grid says which confirmed subscriptions fall HERE, and the
// count says how many the user has in total, which is what an honest empty
// state needs ("nothing yet" vs "nothing this month").
import { api } from "./client";

export type CalendarEventKind = "bill" | "dividend" | "subscription";
export type CalendarEventStatus = "past" | "today" | "upcoming";

export interface CalendarEvent {
  kind: CalendarEventKind;
  label: string;
  /** Signed Decimal string (dividend +, bill/subscription −), in `currency`. */
  amount: string | null;
  /** ISO code `amount` is stated in; null together with a null `amount`. */
  currency: string | null;
  /** Per-kind id (docs/36 decision 8): bill id | security id | payee. */
  source_id: string;
  status: CalendarEventStatus;
}

export interface CalendarDay {
  date: string;
  events: CalendarEvent[];
}

export interface CashflowCalendar {
  month: string;
  /** `today_et()` — the single day boundary (docs/29 F1d); never derive this client-side. */
  today: string;
  days: CalendarDay[];
  /**
   * M170: the user's WHOLE confirmed set (active bills + accepted recurring,
   * deduped) — a fact about the user, not about this month. Zero means "no
   * confirmed subscriptions yet", which is a different sentence from "none
   * this month".
   */
  confirmed_subscription_count: number;
}

/**
 * `month` is optional: absent means "the server's current month". An invalid
 * month (`2026-13`) is a 422 `invalid_month` envelope from the API.
 */
export function getCashflowCalendar(month?: string): Promise<CashflowCalendar> {
  const query = month ? `?month=${encodeURIComponent(month)}` : "";
  return api<CashflowCalendar>(`/reports/cashflow-calendar${query}`);
}
