// DemoLock (M185, docs/47 §4) — the demo-tenant write affordance. The single
// most load-bearing decision of the wave: the artifact's 90 padlocks are a
// DEMO-TENANT fact, and shipping them as static decoration would (a) tell a
// live user their product is read-only and (b) create the dead-control class
// the UX gate spent four revisions removing.
//
// A padlock means exactly ONE thing in this product — "the demo tenant cannot
// save this" — and its copy says so. It renders iff (docs/47 §4):
//   1. isReadOnlySession(user); AND
//   2. the control's action is a mutating request on a non-exempt path
//      (lib/demoWrite.ts mirrors backend/app/api/deps.py).
// Reads — including every GET download (W1) — never wear it. The single named
// exception W2 ("Test connection") is disabled BEFORE dispatch and never uses
// this component (no 403 fires, so there is no refusal to explain).
//
// BEHAVIOUR (docs/47 §4): a locked control stays ENABLED — never disabled,
// never aria-disabled — because an inert control cannot explain itself. The
// write path is unchanged: the request really goes out, the server really
// refuses 403 `demo_account_restricted`, and `useDemoRefusal` turns that real
// refusal into ONE toast.info + ONE local `role="status"` element per page.
// No ErrorPanel, no navigation, no second failure.

import { useCallback, useId, useState } from "react";
import type { ReactNode } from "react";
import { Lock } from "lucide-react";
import { isReadOnlySession } from "../../auth/readOnlySession";
import { useAuth } from "../../auth/AuthContext";
import { demoWouldRefuse, isDemoRefusalError } from "../../lib/demoWrite";
import { useToast } from "./Toast";

/**
 * The padlock glyph (decorative — the reason text is the accessible story,
 * bound by `aria-describedby`). Both padlock parts are lucide's `Lock`
 * (closed body + U shackle), so a download arrow or chevron can never pass
 * for a lock (the artifact gate's own instrument rejected 15 such icons).
 */
export function DemoLock({ className = "" }: { className?: string }) {
  return (
    <span
      data-testid="demo-lock"
      className={`inline-flex shrink-0 items-center ${className}`}
      aria-hidden
    >
      <Lock className="size-3.5" strokeWidth={2} />
    </span>
  );
}

/** Would the server refuse THIS request as a demo write? (docs/47 §4.) */
export function useDemoWriteLock(method: string, path: string): boolean {
  const { user } = useAuth();
  if (!isReadOnlySession(user)) return false;
  return demoWouldRefuse(method, path);
}

/**
 * The SAME-PANEL reason a locked control's `aria-describedby` points at.
 *
 * VISIBLE (docs/47 §4 "local, visible explanation"): it must never be
 * `sr-only`/clipped — a sighted demo user sees the padlock glyph and has to
 * be able to read WHY next to it without hover or assistive tech (the
 * `sr-only` version measured 1×1 px in the M185 audit, F-4). `block` on a
 * `span` keeps the box non-zero and lets it nest legally inside the inline
 * wrappers the locked controls live in. `data-testid="demo-reason"` is the
 * pin's handle.
 */
export function DemoReason({
  id,
  children,
}: {
  id: string;
  children: ReactNode;
}) {
  return (
    <span
      id={id}
      data-testid="demo-reason"
      className="block text-xs text-muted"
    >
      {children}
    </span>
  );
}

/** The refusal element — `role="status"`, never `role="alert"`: a refused
 *  demo write is not an error (docs/47 §6.4). One per page (docs/09
 *  §Read-only sessions #3). */
export function DemoRefusal({ id, action }: { id: string; action: string }) {
  return (
    <div
      id={id}
      role="status"
      data-testid="demo-refusal"
      className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
    >
      {action} — the demo account is read-only; changes are not saved.
    </div>
  );
}

/**
 * One refusal per page, fed by the REAL 403. `onError` is a mutation's error
 * handler: it swallows exactly the demo refusal (converting it to the local
 * explanation) and returns true, so the caller can skip its generic
 * ErrorPanel path for that one code and nothing else.
 */
export function useDemoRefusal() {
  const toast = useToast();
  const refusalId = useId();
  const [refusal, setRefusal] = useState<string | null>(null);

  const onError = useCallback(
    (action: string, error: unknown): boolean => {
      if (!isDemoRefusalError(error)) return false;
      setRefusal(action);
      // ONE rendering of the same state as the banner + the reason element
      // (docs/47 §4) — an info, never an error.
      toast.info(`${action} — the demo account is read-only`);
      return true;
    },
    [toast],
  );

  return { refusal, refusalId, onError };
}

/**
 * The NON-hook verdict for controls a hook cannot reach — every row-scoped
 * affordance in a `rows.map(...)` (docs/47 §4 condition 1+2 still apply; only
 * the call site changes: `useDemoWriteLock` is `useAuth()` plus this
 * function, and rules-of-hooks forbid calling it inside a loop).
 *
 * `user` is the same object `useAuth()` hands out, so the two verdicts can
 * never disagree.
 */
export function demoLocks(
  user: { email: string } | null | undefined,
  method: string,
  path: string,
): boolean {
  if (!isReadOnlySession(user)) return false;
  return demoWouldRefuse(method, path);
}
