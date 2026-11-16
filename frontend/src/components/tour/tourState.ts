// M89 G1 — first-session coach-mark state.
//
// Two flags, deliberately stored on the client (docs/09 — the tour teaches the
// UI, it is not user data, so it never becomes a server field):
//
//   * "has this account already been taught?" — localStorage, keyed per
//     account, because a dismissal must survive a page reload AND a new
//     browser session (acceptance: a second fresh session shows no tour).
//   * "has the tour already fired in THIS tab-session?" — sessionStorage, so
//     navigating away from the dashboard mid-tour and back cannot restart it
//     while the tab is open.
//
// Both storages can throw (Safari private mode, storage disabled), so every
// access is guarded: an unavailable storage degrades to "show the tour" rather
// than to a crash.

export const TOUR_STORAGE_PREFIX = "fin_tracker.tour.v1.";
const SESSION_STARTED_KEY = "fin_tracker.tour.session-started";

/** The event `Help`'s "Take the tour" button fires; `Tour` listens for it. */
export const TOUR_START_EVENT = "fin_tracker:tour:start";

export type TourOutcome = "completed" | "skipped";

/** Minimal account identity the tour needs (User from docs/08 satisfies it). */
export interface TourAccount {
  id?: string | number | null;
  email?: string | null;
}

export function tourStorageKey(account: TourAccount | null | undefined): string {
  const id = account?.id ?? account?.email ?? "anonymous";
  return `${TOUR_STORAGE_PREFIX}${id}`;
}

export function readTourState(
  account: TourAccount | null | undefined,
): TourOutcome | null {
  try {
    const raw = window.localStorage.getItem(tourStorageKey(account));
    return raw === "completed" || raw === "skipped" ? raw : null;
  } catch {
    return null;
  }
}

export function writeTourState(
  account: TourAccount | null | undefined,
  outcome: TourOutcome,
): void {
  try {
    window.localStorage.setItem(tourStorageKey(account), outcome);
  } catch {
    /* storage unavailable — the tour simply offers itself again next time */
  }
}

/** True once the tour has been opened in this tab-session. */
export function tourStartedThisSession(): boolean {
  try {
    return window.sessionStorage.getItem(SESSION_STARTED_KEY) === "1";
  } catch {
    return false;
  }
}

export function markTourStartedThisSession(): void {
  try {
    window.sessionStorage.setItem(SESSION_STARTED_KEY, "1");
  } catch {
    /* ignore */
  }
}

/** Test/reset helper — clears both flags for an account (never called by UI). */
export function clearTourState(account: TourAccount | null | undefined): void {
  try {
    window.localStorage.removeItem(tourStorageKey(account));
    window.sessionStorage.removeItem(SESSION_STARTED_KEY);
  } catch {
    /* ignore */
  }
}

/** Ask the mounted tour to open (resurface path — ignores the stored outcome). */
export function requestTour(): void {
  window.dispatchEvent(new CustomEvent(TOUR_START_EVENT));
}
