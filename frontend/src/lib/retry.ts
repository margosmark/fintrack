// retry (F-RETRY, docs/47 §3 "Error" row) — the ONE place that decides whether
// a failed surface may offer `Try again`, and the one place that runs it.
//
// The taxonomy's Error row requires `ErrorPanel` + a `Try again` bound to the
// query's own `refetch()`. What it must NOT do is offer that button for a
// failure no second press can change: a demo write refusal, a privacy-mode
// refusal, an E2EE write guard, a validation error. A retry button on those is
// a dead control dressed as a way back — the exact class the gate removed —
// so retryability is decided here from the envelope's `code`/status (docs/08:
// branch on `code`, NEVER on `detail`), not by each call site.
//
// Two rules, in order:
//
//   1. POLICY/CAPABILITY codes never retry. The server already answered
//      definitively; the honest affordance is the one that state owns
//      (`demo-refusal` + toast, `VaultLocked`, a disabled control with a
//      visible reason) — never a second identical request.
//   2. otherwise the failure retries only when it is PLATELY TRANSIENT: no
//      envelope at all (DNS/CORS/proxy/network — nothing deterministic about
//      it), a 5xx, or 408/429. Every other 4xx is a property of the request
//      itself (404 not_found, 409 duplicate_*, 422 validation_error, 401
//      invalid_token — the refresh-and-continue path owns that one) and would
//      fail the same way twice.

import { useCallback, useEffect, useRef } from "react";

import { ApiError } from "../api/client";

/**
 * Envelope codes whose failure is a policy or capability refusal — the server
 * answered definitively and no repeat request changes the answer. Named, not
 * inferred: `encryption_required` is the E2EE write guard (`docs/33`; the
 * surfaces that can hit it render `VaultLocked` instead), the first two are
 * `docs/47 §4`'s demo/privacy boundary, and `validation_error` is the
 * request's own shape (`backend/app/core/errors.py`).
 */
export const NON_RETRYABLE_CODES: readonly string[] = [
  "demo_account_restricted",
  "demo_disabled",
  "privacy_mode_blocks_external_api",
  "encryption_required",
  "invalid_cipher",
  "validation_error",
];

/** Is a repeat of THIS request plausibly able to produce a different answer? */
export function isRetryableError(err: unknown): boolean {
  if (err instanceof ApiError) {
    if (err.code !== null && NON_RETRYABLE_CODES.includes(err.code)) return false;
    // 5xx (incl. the upstream-refusal envelopes `llm_upstream_error`,
    // `market_data_unavailable`) and the timeout/rate-limit pair are the
    // transient classes; every other 4xx is deterministic.
    return err.status >= 500 || err.status === 408 || err.status === 429;
  }
  // Not an envelope at all: a network failure, an aborted fetch, a proxy's
  // HTML body. Nothing deterministic about it — retry is honest.
  return err !== null && err !== undefined;
}

export interface RetryAction {
  /**
   * Bound handler for `ErrorPanel`, or `undefined` when no honest re-run
   * exists (policy refusal, or a surface that kept no copy of its inputs) —
   * the panel then renders exactly what it rendered before this wave.
   */
  onRetry?: () => void;
  /** True while the retry is in flight: the control disables itself (no
   *  duplicate request) and the panel announces the pending state. */
  retrying: boolean;
}

/**
 * The retry half of the ErrorPanel contract.
 *
 * `retry` is the surface's OWN re-run — a query's `refetch()`, or a closure
 * that re-posts the very same payload. It is `null` when no honest one exists,
 * which is how a policy refusal stays without a button.
 *
 * `pending` is the request's own in-flight flag (`query.isFetching`,
 * `mutation.isPending`). The ref guard closes the window a double-click opens
 * between the press and the state update that disables the button, so one
 * press is one request.
 */
export function useRetryAction({
  retry,
  pending,
}: {
  retry: (() => void) | null;
  pending: boolean;
}): RetryAction {
  const inFlight = useRef(false);
  useEffect(() => {
    if (!pending) inFlight.current = false;
  }, [pending]);

  const run = useCallback(() => {
    if (retry === null || pending || inFlight.current) return;
    inFlight.current = true;
    retry();
  }, [retry, pending]);

  return { onRetry: retry === null ? undefined : run, retrying: pending };
}
