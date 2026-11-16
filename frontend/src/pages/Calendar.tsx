import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { CalendarDays, ChevronLeft, ChevronRight, X } from "lucide-react";
import {
  getCashflowCalendar,
  type CalendarEvent,
  type CalendarEventKind,
} from "../api/cashflowCalendar";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { ErrorPanel } from "../components/ui/EmptyState";
import { Skeleton } from "../components/ui/Skeleton";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { apiErrorMessage } from "../components/transactions/format";
import { formatMoney } from "../lib/format";
import { useMasked } from "../lib/mask";

/**
 * Cash-flow calendar (M52, docs/36): a month grid over bills (docs/25), the
 * M29 dividend calendar and the CONFIRMED subscription set (M170) — composed
 * by `GET /reports/cashflow-calendar`, never recomputed here.
 *
 * Conventions pinned by the spec: weeks start Monday (docs/12), "today" comes
 * from the response (`today_et()`, docs/29 F1d) and never from the browser
 * clock, ISO dates are split rather than parsed with `new Date("YYYY-MM-DD")`
 * (UTC midnight shifts the weekday), and only semantic tokens are used so both
 * themes keep the documented contrast pairs.
 *
 * M170 (docs/36 §M170): a `subscription` dot is CONFIRMED state — a bill or a
 * recurring group the user accepted — never the detector's guess, so the
 * month this series is empty is reported honestly rather than silently: the
 * legend carries one line, worded from the response's
 * `confirmed_subscription_count` ("nothing confirmed yet" is a different
 * sentence from "nothing this month").
 *
 * M170 follow-up (docs/36 §Currency): each event's figure is rendered in ITS
 * OWN currency — `amount` + `currency` arrive as a pair, and a mixed or
 * unknown group carries neither, so a native amount is never printed as CAD.
 */

/** Kind → dot color (semantic tokens only — no literal colors). */
const KIND_DOT: Record<CalendarEventKind, string> = {
  bill: "bg-warn",
  dividend: "bg-gain",
  subscription: "bg-accent",
};

const KIND_NAME: Record<CalendarEventKind, string> = {
  bill: "Bill",
  dividend: "Dividend",
  subscription: "Subscription",
};

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const MIN_MONTH = "1900-01";
const MAX_MONTH = "2200-12";
const PANEL_WIDTH = 320;
const PANEL_HEIGHT = 300;

function toParts(iso: string): { year: number; month: number; day: number } {
  const [year, month, day] = iso.split("-").map(Number);
  return { year, month, day };
}

/** Weekday index with Monday = 0 (docs/12 week bucketing). */
function mondayIndex(iso: string): number {
  const { year, month, day } = toParts(iso);
  return (new Date(year, month - 1, day).getDay() + 6) % 7;
}

function longDate(iso: string): string {
  const { year, month, day } = toParts(iso);
  return new Date(year, month - 1, day).toLocaleDateString("en-CA", {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function monthLabel(month: string): string {
  if (!/^\d{4}-\d{2}$/.test(month)) return "";
  const { year, month: m } = toParts(`${month}-01`);
  return new Date(year, m - 1, 1).toLocaleDateString("en-CA", {
    month: "long",
    year: "numeric",
  });
}

function shiftMonth(month: string, delta: number): string {
  const { year, month: m } = toParts(`${month}-01`);
  const index = year * 12 + (m - 1) + delta;
  const nextYear = Math.floor(index / 12);
  const nextMonth = (((index % 12) + 12) % 12) + 1;
  return `${String(nextYear).padStart(4, "0")}-${String(nextMonth).padStart(2, "0")}`;
}

/** Where an event's `source_id` sends the user (docs/36 decision 8). */
function sourceHref(event: CalendarEvent, dayDate: string): string {
  if (event.kind === "bill") return "/bills";
  if (event.kind === "dividend") return `/securities/${event.source_id}`;
  // Subscription: the M55 draft hand-off (docs/25). Only the params we have
  // are written; `readBillAutofill` degrades the rest (frequency → monthly).
  const params = new URLSearchParams({ autofill: event.source_id });
  // A bill is CAD by schema (docs/25), so a native amount in any other
  // currency is NOT a CAD figure and is not handed over — the draft opens with
  // an empty amount field rather than a mislabelled one (docs/36 §Currency).
  if (event.amount !== null && event.currency === "CAD") {
    params.set("amount", event.amount.replace(/^-/, ""));
  }
  params.set("due", dayDate);
  return `/bills?${params.toString()}`;
}

/**
 * The event's figure in ITS OWN currency (M170 follow-up, docs/36 §Currency).
 * `amount` and `currency` arrive together or not at all, so a missing pair is
 * the app's "no honest figure" dash — never a `$0.00` and never the native
 * number relabelled CAD (`formatMoney` renders a USD figure as `US$…`).
 */
function formatAmount(amount: string | null, currency: string | null): string {
  if (amount === null || currency === null) return "—";
  return formatMoney(amount, currency);
}

interface SelectedDay {
  date: string;
  left: number;
  top: number;
}

export function Calendar() {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const [month, setMonth] = useState<string | undefined>(undefined);
  const [selected, setSelected] = useState<SelectedDay | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  // One key = one queryFn (docs/09): the default-month fetch and an explicit
  // month are different cache entries ("current" vs "2026-09").
  const calendar = useQuery({
    queryKey: ["cashflow-calendar", month ?? "current"],
    queryFn: () => getCashflowCalendar(month),
  });

  const data = calendar.data;
  const activeMonth = month ?? data?.month ?? "";
  const today = data?.today ?? "";

  useEffect(() => {
    setSelected(null);
  }, [activeMonth]);

  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  const days = useMemo(() => data?.days ?? [], [data]);
  const leading = days.length > 0 ? mondayIndex(days[0].date) : 0;
  const totalEvents = useMemo(
    () => days.reduce((sum, day) => sum + day.events.length, 0),
    [days],
  );
  /** M170: how many CONFIRMED subscriptions this month actually shows. */
  const subscriptionEvents = useMemo(
    () =>
      days.reduce(
        (sum, day) => sum + day.events.filter((event) => event.kind === "subscription").length,
        0,
      ),
    [days],
  );

  const canGoPrev = activeMonth > MIN_MONTH;
  const canGoNext = activeMonth < MAX_MONTH;

  function openDay(date: string, element: HTMLElement) {
    const rect = element.getBoundingClientRect();
    const left = Math.min(
      Math.max(rect.left, 12),
      Math.max(12, window.innerWidth - PANEL_WIDTH - 12),
    );
    const below = rect.bottom + 8;
    const top =
      below + PANEL_HEIGHT > window.innerHeight
        ? Math.max(12, rect.top - PANEL_HEIGHT - 8)
        : below;
    setSelected({ date, left, top });
  }

  const selectedDay = selected ? days.find((day) => day.date === selected.date) : undefined;

  return (
    <motion.div
      variants={staggerContainer(0.05)}
      initial="hidden"
      animate="show"
      className="space-y-6"
    >
      <motion.div variants={fadeUp} className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <CalendarDays className="size-5 text-accent" aria-hidden />
          <h1 className="text-xl font-semibold tracking-tight">Calendar</h1>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            aria-label="Previous month"
            disabled={!activeMonth || !canGoPrev}
            onClick={() => setMonth(shiftMonth(activeMonth, -1))}
          >
            <ChevronLeft className="size-4" aria-hidden />
          </Button>
          <span
            className="min-w-40 text-center text-sm font-medium text-text"
            data-testid="calendar-month"
          >
            {monthLabel(activeMonth) || "\u00A0"}
          </span>
          <Button
            variant="ghost"
            size="sm"
            aria-label="Next month"
            disabled={!activeMonth || !canGoNext}
            onClick={() => setMonth(shiftMonth(activeMonth, 1))}
          >
            <ChevronRight className="size-4" aria-hidden />
          </Button>
          <Button variant="outline" size="sm" disabled={!today} onClick={() => setMonth(undefined)}>
            Today
          </Button>
        </div>
      </motion.div>

      <motion.div variants={fadeUp} className="flex flex-wrap items-center gap-3">
        {(Object.keys(KIND_NAME) as CalendarEventKind[]).map((kind) => (
          <span key={kind} className="inline-flex items-center gap-1.5 text-xs text-muted">
            <span className={`size-2 rounded-full ${KIND_DOT[kind]}`} aria-hidden />
            {KIND_NAME[kind]}s
          </span>
        ))}
        <span className="text-xs text-muted">Signed amounts — outflow negative</span>
      </motion.div>

      {/* M170 (docs/36 §M170): an empty subscription series is reported, not
          hidden. The two sentences are different facts and the response knows
          which one is true: `confirmed_subscription_count === 0` is "nothing
          confirmed yet"; anything else means the user HAS confirmed
          subscriptions and this month simply holds none of them. */}
      {!calendar.isPending && !calendar.isError && data && subscriptionEvents === 0 && (
        <motion.p
          variants={fadeUp}
          className="text-xs text-muted"
          data-testid="calendar-subscriptions-empty"
        >
          {data.confirmed_subscription_count === 0
            ? "No confirmed subscriptions yet — recurring candidates ask on /transactions before they land here."
            : `No subscription payments in ${monthLabel(activeMonth)} — a subscription appears here once it is confirmed: a bill, or a candidate you accepted on /transactions.`}
        </motion.p>
      )}

      <motion.div variants={fadeUp}>
        <Card className="p-4">
          {calendar.isError ? (
            <ErrorPanel message={apiErrorMessage(calendar.error)} />
          ) : (
            <>
              <div className="grid grid-cols-7 gap-1.5" role="presentation">
                {WEEKDAYS.map((weekday) => (
                  <div
                    key={weekday}
                    className="pb-1 text-center text-[11px] font-medium uppercase tracking-widest text-muted"
                  >
                    {weekday}
                  </div>
                ))}
                {calendar.isPending &&
                  Array.from({ length: 35 }, (_, index) => (
                    <Skeleton key={`skeleton-${index}`} className="h-24 rounded-lg" />
                  ))}
                {!calendar.isPending &&
                  Array.from({ length: leading }, (_, index) => (
                    <div key={`blank-${index}`} aria-hidden />
                  ))}
                {!calendar.isPending &&
                  days.map((day) => {
                    const count = day.events.length;
                    const dots = day.events.slice(0, 3);
                    const overflow = count - dots.length;
                    const isSelected = selected?.date === day.date;
                    return (
                      <button
                        key={day.date}
                        type="button"
                        data-testid={`calendar-day-${day.date}`}
                        aria-label={`${longDate(day.date)} — ${count} ${
                          count === 1 ? "event" : "events"
                        }`}
                        aria-current={day.date === today ? "date" : undefined}
                        aria-expanded={isSelected}
                        onClick={(event) => openDay(day.date, event.currentTarget)}
                        className={`min-h-24 rounded-lg border p-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                          isSelected
                            ? "border-accent bg-veil/[0.06]"
                            : "border-border bg-surface hover:border-accent/70 hover:bg-veil/[0.04]"
                        } ${day.date === today ? "ring-1 ring-accent" : ""}`}
                      >
                        <span
                          className={`text-xs font-medium ${
                            day.date === today ? "text-accent" : "text-text"
                          }`}
                        >
                          {toParts(day.date).day}
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-1">
                          {dots.map((event, index) => (
                            <span
                              key={`${event.kind}-${event.source_id}-${index}`}
                              className={`size-1.5 rounded-full ${KIND_DOT[event.kind]}`}
                              data-kind={event.kind}
                              aria-hidden
                            />
                          ))}
                          {overflow > 0 && (
                            <span className="text-[10px] text-muted">+{overflow}</span>
                          )}
                        </span>
                      </button>
                    );
                  })}
              </div>
              {!calendar.isPending && !calendar.isError && totalEvents === 0 && (
                <p className="pt-3 text-xs text-muted">
                  No cash-flow events in {monthLabel(activeMonth)}.
                </p>
              )}
            </>
          )}
        </Card>
      </motion.div>

      {selected &&
        createPortal(
          <>
            <div
              className="fixed inset-0 z-50 bg-overlay/60"
              onMouseDown={() => setSelected(null)}
              aria-hidden
            />
            <div
              ref={panelRef}
              role="dialog"
              aria-label={longDate(selected.date)}
              data-testid="calendar-popover"
              className="glass fixed z-[60] rounded-xl border border-border p-3 shadow-glow-sm"
              style={{
                left: selected.left,
                top: selected.top,
                width: PANEL_WIDTH,
                maxWidth: "calc(100vw - 1.5rem)",
              }}
            >
              <div className="flex items-start justify-between gap-2 pb-2">
                <div>
                  <p className="text-sm font-semibold text-text">{longDate(selected.date)}</p>
                  <p className="text-[11px] text-muted">
                    {(selectedDay?.events.length ?? 0) === 0
                      ? "No cash-flow events"
                      : `${selectedDay?.events.length} event${
                          selectedDay?.events.length === 1 ? "" : "s"
                        }`}
                  </p>
                </div>
                <button
                  type="button"
                  aria-label="Close"
                  onClick={() => setSelected(null)}
                  className="rounded-md p-1 text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <X className="size-4" aria-hidden />
                </button>
              </div>

              {selectedDay && selectedDay.events.length > 0 && (
                <ul className="flex flex-col gap-1">
                  {selectedDay.events.map((event, index) => (
                    <li key={`${event.kind}-${event.source_id}-${index}`}>
                      <Link
                        to={sourceHref(event, selectedDay.date)}
                        className="flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-veil/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        <span
                          className={`size-2 shrink-0 rounded-full ${KIND_DOT[event.kind]}`}
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1 truncate text-xs text-text">
                          {event.label}
                        </span>
                        <span className="text-xs font-medium text-text">
                          {formatAmount(event.amount, event.currency)}
                        </span>
                        <span
                          className={`w-16 shrink-0 text-right text-[10px] ${
                            event.status === "today" ? "text-accent" : "text-muted"
                          }`}
                        >
                          {event.status}
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>,
          document.body,
        )}
    </motion.div>
  );
}
