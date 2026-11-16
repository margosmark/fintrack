/**
 * Shared schedule math for the two client-side runners — M56 scheduled
 * backup and M75 scheduled export (docs/11 § "Schedule model" / § "Due
 * semantics").
 *
 * Extracted from `scheduledBackup.ts` when M75 needed the identical rules:
 * the weekday-encoding bridge below shipped a one-day-off schedule once, and
 * copying the bug into a second file is how that class of defect survives a
 * fix. `scheduledBackup.ts` re-exports these three functions, so its public
 * API — and `frontend/tests/scheduledBackup.test.ts` — are unchanged.
 *
 * Everything is pure and clock-injected, so the rules are testable without a
 * server, a vault or a timer.
 */

/** The five schedule columns, as both `GET /backup/schedule` and
 * `GET /export/schedule` return them. */
export type Cadence = "off" | "daily" | "weekly" | "monthly";

export interface CadenceSchedule {
  frequency: Cadence;
  /** 0–23, local time of the machine running the tab. */
  hour: number;
  /** 0–6, **0 = Monday** (weekly only) — the column's encoding, not JS's. */
  weekday: number;
  /** 1–28 (monthly only). */
  day_of_month: number;
  /** 1–20 retention (keep-last-N). */
  keep_last_n: number;
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * ISO-8601 week + ISO week-year (docs/11 weekly label `YYYY-Www`).
 * The week-year differs from the calendar year near New Year — the Thursday
 * of the week decides it — which is exactly the boundary the tests pin.
 */
function isoWeek(at: Date): { year: number; week: number } {
  const day = new Date(at.getFullYear(), at.getMonth(), at.getDate());
  const weekday = (day.getDay() + 6) % 7; // 0 = Monday
  day.setDate(day.getDate() - weekday + 3); // Thursday of this week
  const year = day.getFullYear();
  const firstThursday = new Date(year, 0, 4);
  const firstWeekday = (firstThursday.getDay() + 6) % 7;
  firstThursday.setDate(firstThursday.getDate() - firstWeekday + 3);
  const week = 1 + Math.round((day.getTime() - firstThursday.getTime()) / (7 * 24 * 3600 * 1000));
  return { year, week };
}

/**
 * The idempotency key for one run (docs/11): daily `2026-09-28`, weekly ISO
 * week `2026-W39`, monthly `2026-09` — all in the runner's LOCAL time. Two
 * tabs racing for the same period converge on one stored record.
 */
export function periodFor(frequency: Exclude<Cadence, "off">, at: Date): string {
  switch (frequency) {
    case "daily":
      return `${at.getFullYear()}-${pad2(at.getMonth() + 1)}-${pad2(at.getDate())}`;
    case "weekly": {
      const { year, week } = isoWeek(at);
      return `${year}-W${pad2(week)}`;
    }
    case "monthly":
      return `${at.getFullYear()}-${pad2(at.getMonth() + 1)}`;
  }
}

/**
 * First LOCAL instant strictly after `after` matching the frequency at
 * `hour:00`: daily → every day, weekly → next date whose weekday matches the
 * schedule, monthly → next date whose `getDate()` is `day_of_month`. Returns
 * `null` when the schedule is off. Instants are built from local components,
 * so a DST jump the hour skips or repeats is accepted as-is (documented, not
 * handled specially — docs/11).
 *
 * Weekday encoding is pinned here because the two conventions differ: the
 * schedule column and the picker use **0 = Monday … 6 = Sunday** (docs/11),
 * while `Date.getDay()` is 0 = Sunday — the two are bridged here, so
 * `weekday: 0` really does run on a Monday.
 */
export function nextDue(schedule: CadenceSchedule, after: Date): Date | null {
  if (schedule.frequency === "off") return null;
  const jsWeekday = (schedule.weekday + 1) % 7; // 0 = Monday … 6 = Sunday → JS getDay()
  let candidate = new Date(
    after.getFullYear(),
    after.getMonth(),
    after.getDate(),
    schedule.hour,
    0,
    0,
    0,
  );
  if (candidate.getTime() <= after.getTime()) {
    candidate = new Date(
      candidate.getFullYear(),
      candidate.getMonth(),
      candidate.getDate() + 1,
      schedule.hour,
      0,
      0,
      0,
    );
  }
  // 800 days comfortably covers weekly (≤7) and monthly day-of-month (≤28).
  for (let guard = 0; guard < 800; guard++) {
    const matches =
      schedule.frequency === "daily" ||
      (schedule.frequency === "weekly" && candidate.getDay() === jsWeekday) ||
      (schedule.frequency === "monthly" && candidate.getDate() === schedule.day_of_month);
    if (matches) return candidate;
    candidate = new Date(
      candidate.getFullYear(),
      candidate.getMonth(),
      candidate.getDate() + 1,
      schedule.hour,
      0,
      0,
      0,
    );
  }
  return null;
}

/**
 * Run iff the schedule is on AND (`latest === null` or
 * `now >= nextDue(latest.created_at)`): no record yet ⇒ due immediately, so
 * the first run happens as soon as the schedule is on (docs/11 due semantics).
 */
export function isDue(
  schedule: CadenceSchedule,
  latestCreatedAt: string | null,
  now: Date,
): boolean {
  if (schedule.frequency === "off") return false;
  if (latestCreatedAt === null) return true;
  const due = nextDue(schedule, new Date(latestCreatedAt));
  return due !== null && now.getTime() >= due.getTime();
}
