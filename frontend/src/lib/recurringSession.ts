// recurringSession — the prompt card's once-per-session cache (docs/09
// §Recurring prompt: "compute once per session on the Transactions entry,
// cache the result in session state").
//
// `sessionStorage`, not react-query memory: the point is that re-entering or
// reloading the tab does NOT re-run the scan, and that the cache dies with the
// tab (a new tab is a new session). Every accept/dismiss rewrites the cached
// list, so what the card shows is always what it already asked about.
//
// TENANCY (B2, QA blocker on t_b29beeaa): sessionStorage is scoped to the TAB,
// not to the USER, so this cache outlived the identity that filled it — sign
// out of the demo tenant, sign in as somebody else in the same tab, and
// /transactions rendered the previous tenant's payees, amounts and
// transaction ids (it survived the reload too, because the tab's storage
// does). Two rules close it, both enforced HERE, in the only module that
// touches the key:
//
//   1. every entry is STAMPED with the identity the auth seam bound to this
//      tab (`scopeCandidatesTo` — called from AuthContext on boot/restore, on
//      every explicit identity change including logout, and on a failed boot);
//   2. a read whose stamp does not match the live identity DROPS the entry —
//      deleted, not merely hidden, so no prior tenant's rows stay in storage
//      for anyone to find afterwards.
//
// A pre-fix entry (a bare array under this key) is unowned by construction and
// is deleted on sight for the same reason: it could have been written by
// anybody.
//
// `owner === null` means "no session is bound" (logged out / boot failed). The
// card can only mount behind `RequireAuth`, which renders nothing until the
// probe has resolved an identity (docs/09 §App shell & auth flow), so a null
// stamp never carries a tenant's rows in the app — and in a harness that never
// mounts the auth seam, null == null is a consistent scope rather than a
// permissive one: rows written without an identity can only be read without
// one.
import type { RecurringCandidate } from "../api/transactions";

const KEY = "fintrack.recurring-candidates.v1";

/**
 * The shape an identity can arrive in.
 *
 * `users.id` is an `int` on the wire, so the auth seam hands over a NUMBER in
 * the running app. Every
 * stamp and every comparison therefore goes through `String(...)`: a numeric
 * `1` written by one call must never compare unequal to the `1` the next call
 * binds, or a same-user reload would drop its own cache (measured: the
 * un-normalized stamp made the card re-scan on every entry).
 */
export type CacheOwner = string | number | null;

/** What is actually in storage: the rows AND the identity they belong to. */
interface ScopedCandidates {
  owner: string | null;
  groups: RecurringCandidate[];
}

/** Normalize an incoming identity to the one canonical stamp form. */
function stampOf(nextOwner: CacheOwner): string | null {
  if (nextOwner === null || nextOwner === undefined) return null;
  return String(nextOwner);
}

/**
 * The identity this tab's cache is bound to. `null` = no session bound.
 *
 * Module state on purpose (the `lib/mask.ts` shape): it is written by exactly
 * one caller — the auth seam — and read synchronously by this module, so a
 * storage read never has to reach through React to learn who is signed in.
 */
let owner: string | null = null;

/** Remove the key; storage may be blocked (private mode, quota). */
function drop(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear.
  }
}

/**
 * Parse the raw value into a stamped entry. Returns `undefined` when the key
 * is absent and `null` when it holds something that is NOT a stamped entry —
 * a pre-fix bare array, a hand-edited value, another app's junk. Both callers
 * treat `null` as "delete it": an entry nobody can attribute is an entry that
 * must not be served.
 */
function parse(raw: string | null): ScopedCandidates | undefined | null {
  if (raw === null) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const candidate = parsed as { owner?: unknown; groups?: unknown };
  if (!("owner" in candidate) || !Array.isArray(candidate.groups)) return null;
  if (
    candidate.owner !== null &&
    typeof candidate.owner !== "string" &&
    typeof candidate.owner !== "number"
  ) {
    return null;
  }
  return {
    owner: candidate.owner === null ? null : String(candidate.owner),
    groups: candidate.groups as RecurringCandidate[],
  };
}

/**
 * Bind this tab's cache to `nextOwner` and drop anything that is not theirs.
 *
 * The identity transition itself, called from AuthContext — the SAME seams the
 * display-mask boot contract runs on (docs/46 §D1):
 *   * a restore/boots seeds the identity the probe resolved: a same-identity
 *     entry survives, which is what keeps the documented "reloading the tab
 *     does not re-run the scan" behaviour for the tenant who owns it;
 *   * an explicit identity change (login / demo login / logout) re-binds and
 *     therefore purges the previous tenant's rows before anything can render;
 *   * binding `null` (logout, failed boot) clears outright — with no session
 *     there is nothing a session-scoped cache may keep.
 */
export function scopeCandidatesTo(nextOwner: CacheOwner): void {
  const bound = stampOf(nextOwner);
  owner = bound;
  if (bound === null) {
    drop();
    return;
  }
  const stored = parse(safeGet());
  // Absent, unattributable or somebody else's: all three are "not theirs".
  if (stored?.owner !== bound) drop();
}

function safeGet(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** Read this session's cached candidate groups; null when never computed. */
export function readCandidates(): RecurringCandidate[] | null {
  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(KEY);
  } catch {
    // Storage blocked (private mode, disabled cookies): fall back to
    // recomputing each entry — correctness over caching, never a broken card.
    return null;
  }
  const stored = parse(raw);
  if (stored === null) {
    // Unattributable → gone, not served (B2).
    drop();
    return null;
  }
  if (stored === undefined) return null;
  if (stored.owner !== owner) {
    // Another tenant's rows (or an entry from before a logout): delete and
    // recompute under the identity that is actually signed in.
    drop();
    return null;
  }
  return stored.groups;
}

/** Persist the groups this session has already decided about. */
export function writeCandidates(groups: RecurringCandidate[]): void {
  try {
    const entry: ScopedCandidates = { owner, groups };
    window.sessionStorage.setItem(KEY, JSON.stringify(entry));
  } catch {
    // Same degradation: caching is best-effort.
  }
}

/** Drop the cache (tests, and the "everything answered" state). */
export function clearCandidates(): void {
  drop();
}

/** The identity the cache is currently bound to (tests / diagnostics). */
export function candidatesOwner(): string | null {
  return owner;
}
