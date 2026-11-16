// Display mask (M139, docs/09 § Display masking) — the session-scoped "somebody
// is looking over my shoulder" switch.
//
// WHAT it is: a RENDER-time transformation. Flipping it re-renders text; the
// API payload is byte-identical either way, the mask never touches the wire and
// never reaches `users` (docs/11 step 6 keeps privacy_mode's EGRESS contract
// untouched — this layer sits BELOW it, on the client only).
//
// WHY a module store and not only React state: the formatters (`formatCad`,
// `formatMoney`, …) are plain functions called from ~200 sites, so they read
// the ambient mask synchronously by default. Components re-render on a flip by
// subscribing with `useMasked()` (useSyncExternalStore) — no prop threading,
// no API round-trip, so the flip is instant (the D5 acceptance criterion).
//
// SESSION CONTRACT (PM ruling, docs/09 § Display masking):
//   * per-tab: the key lives in sessionStorage, never localStorage, never a
//     cookie, never the DB — the point is THIS screen, not this account;
//   * restore-on-load OFF: a boot never resurrects a stored mask — the user
//     re-masks deliberately (the demo tenant is the one default-ON exception);
//   * logout kills the key (AuthContext.logout → applyUser(null) → the mask
//     init below, which clears it);
//   * the key is gone the moment the tab dies, because sessionStorage is.

import { useSyncExternalStore } from "react";

/** What a masked amount renders as. Five bullets, per the PM spec. */
export const MASK_TEXT = "•••••";

/** The sessionStorage key. Session-scoped by construction — never localStorage. */
export const MASK_SESSION_KEY = "fintrack.mask";

let masked = false;
/**
 * Has the USER made an explicit choice this session? The demo tenant boots
 * masked (docs/09 § Display masking), but that default must not stomp a
 * deliberate un-mask — it is a default, not a lock.
 */
let decided = false;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function persist(): void {
  if (typeof sessionStorage === "undefined") return;
  try {
    if (masked) sessionStorage.setItem(MASK_SESSION_KEY, "1");
    else sessionStorage.removeItem(MASK_SESSION_KEY);
  } catch {
    // Storage can be blocked (private mode, quota). The in-memory state is the
    // source of truth; the key is only the session-scoped record.
  }
}

/** Current mask state. Read by the formatters — synchronous, no re-render. */
export function isMasked(): boolean {
  return masked;
}

/** Flip the mask. Instant: notifies subscribers, no I/O. */
export function setMasked(next: boolean): void {
  decided = true;
  if (masked === next) return;
  masked = next;
  persist();
  notify();
}

/** ⌘⇧H / chip / account-menu entry all land here. */
export function toggleMasked(): void {
  setMasked(!masked);
}

/**
 * Boot / logout contract (D1 + D5). A load NEVER resurrects a stored mask —
 * restore-on-load is OFF by PM ruling, so this clears any stale key and
 * applies the caller's default. `demoDefault` masks the demo tenant at boot
 * (it is a public storefront) unless the user already chose this session.
 */
export function initMaskForSession(opts: { demoDefault?: boolean } = {}): void {
  decided = false;
  const next = opts.demoDefault === true;
  if (typeof sessionStorage !== "undefined") {
    try {
      sessionStorage.removeItem(MASK_SESSION_KEY);
    } catch {
      // See persist() — memory is authoritative.
    }
  }
  masked = next;
  persist();
  notify();
}

/**
 * The RESTORE half of the boot contract (t_2d8ffedb P1).
 *
 * A cookie restore resolves while the tab is already live, so unlike a boot on
 * an identity CHANGE it can land after the user has acted. The session default
 * is a default, not a lock — the rule `applyDemoMaskDefault` already applies one
 * level down — so a choice already made this session wins and nothing is reset.
 * Otherwise this IS the boot contract: clear any stale key, take the default.
 *
 * In the app the guard is invisible (App.tsx renders no control until `ready`,
 * so nothing can have been decided before the probe answers). It exists so a
 * late restore cannot stomp a live surface — measured: without it,
 * `m167WorldMap.test.tsx`'s explicit `setMasked(true)` was reset to unmasked by
 * the harness's own restore and the readout printed dollars.
 */
export function initMaskForRestoredSession(opts: { demoDefault?: boolean } = {}): void {
  if (decided) return;
  initMaskForSession(opts);
}

/**
 * The demo-tenant boot default (D5). Applied when auth RESOLVES, not at module
 * load — the tenant is only known once the session probe answers. A user who
 * already un-masked this session keeps their choice: the default is a default,
 * not a lock (one click either way, docs/09 § Display masking).
 */
export function applyDemoMaskDefault(): void {
  if (decided) return;
  masked = true;
  persist();
  notify();
}

/**
 * Subscribe to mask changes. Every component that renders money calls this so a
 * flip re-renders it; the formatters read the ambient state themselves.
 */
export function useMasked(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      listeners.add(onChange);
      return () => {
        listeners.delete(onChange);
      };
    },
    () => masked,
    () => false,
  );
}

/**
 * The year carve-out (D2): "mask everything >= the current year". Amounts in
 * year-scoped analytics for YEARS-OLD data stay readable — nobody is
 * shoulder-surfing 2019 — but the current year and anything forward masks, so
 * today's numbers never leak.
 */
export function maskForYear(maskedFlag: boolean, year: number): boolean {
  return maskedFlag && year >= currentYear();
}

/** The clock-derived current year, in one place (docs/09 § Display masking). */
export function currentYear(): number {
  return new Date().getFullYear();
}

/**
 * Mask the money inside a free-text string (notification bodies, LLM test
 * output). Titles and dates stay readable; percentages stay readable (the
 * operator ruling: proportional values are not sensitive); absolute amounts
 * become •••••.
 *
 * The three real notification body shapes (docs/35) are the pinned cases:
 *   "Spent 123.45 of 500.00 CAD (88% of limit; threshold 75%)"
 *   "Due 2026-10-15 — 45.00 CAD"
 *   "Unrealized loss 432.10 CAD in TFSA (estimated tax benefit 88.00 CAD)"
 */
export function maskAmountsInText(text: string): string {
  if (!masked) return text;
  const stash: string[] = [];
  const park = (value: string): string => {
    stash.push(value);
    return `\u0000${stash.length - 1}\u0000`;
  };
  // Park first what must survive: ISO dates, then percentages (the operator
  // ruling — proportional values are not sensitive).
  let out = text.replace(/\d{4}-\d{2}-\d{2}/g, park);
  out = out.replace(/\d+(?:\.\d+)?\s?%/g, park);
  // Now mask the three money shapes, in order of specificity:
  //   1. `$`-prefixed (with or without cents)  — "$1,234" / "$12.50"
  //   2. a number glued to a CAD/USD code      — "45.00 CAD"
  //   3. any remaining number WITH cents       — "123.45" (bare, in prose)
  // Rule 3 deliberately skips bare integers ("3 of 9 rows", "limit 5"): only a
  // value carrying a decimal fraction reads as an amount in prose, and that is
  // the only bare shape the notification bodies emit.
  out = out.replace(/[$]\s?-?\d[\d,]*(?:\.\d+)?/g, MASK_TEXT);
  out = out.replace(/-?\d[\d,]*(?:\.\d+)?(?=\s?(?:CAD|USD)\b)/g, MASK_TEXT);
  out = out.replace(/(?<![\w.,\u0000])-?\d[\d,]*\.\d{2}(?![\d%])/g, MASK_TEXT);
  return out.replace(/\u0000(\d+)\u0000/g, (_m, index: string) => stash[Number(index)] ?? "");
}
