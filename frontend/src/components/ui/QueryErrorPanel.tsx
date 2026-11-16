// QueryErrorPanel (F-RETRY, docs/47 §3 "Error" row) — the ErrorPanel every
// QUERY surface renders, with the `Try again` already bound to that query's
// own `refetch()`.
//
// The panel is deliberately structural rather than typed on
// `UseQueryResult<TData, TError>`: any `{ error, isFetching, refetch }` fits,
// so a call site may hand it the whole query object or the three fields it
// destructured. What it never does is invent a request: the retry is the SAME
// query's refetch — never a page reload, never a new query key.
//
// THE FAILURE RECORD. TanStack v5 clears `query.error` (and drops `isError`)
// the instant a refetch starts, so a surface whose gate keeps this panel
// mounted through its own retry — Settings (`isError || user === undefined`),
// ScheduledBackupPanel (`isError || draft === null`) — would otherwise lose
// BOTH the envelope detail and the button for the whole flight, and a screen
// reader would hear a bare "Something went wrong". The panel therefore keeps
// the last live failure as its own record and keeps showing it until the next
// answer replaces it: the detail stays verbatim, the retry stays visible
// (disabled, `Retrying…`) and re-arms on the next failure. A surface that
// hands off to its loading row instead (every `if (isError)` panel) never
// sees this state — there is nothing to hold.
//
// Retryability is NOT decided here (see `lib/retry.ts`): a policy refusal
// (`demo_account_restricted`, `privacy_mode_blocks_external_api`,
// `encryption_required`), a validation error, or any other deterministic 4xx
// renders the panel WITHOUT the button, and a panel whose `error` is null and
// has no record (a surface that also renders it for a non-error condition,
// e.g. a missing `user`) never offers one either.

import { useState } from "react";

import { ErrorPanel } from "./EmptyState";
import { apiErrorMessage } from "../transactions/format";
import { isRetryableError, useRetryAction } from "../../lib/retry";

/** The half of TanStack's query result this panel actually reads. */
export interface RetryableQuery {
  error: unknown;
  isFetching: boolean;
  refetch: () => unknown;
}

interface FailureRecord {
  /** The envelope `detail` exactly as the surface rendered it. */
  detail: string;
  /** Whether a repeat of that request could change the answer. */
  retryable: boolean;
}

export function QueryErrorPanel({
  query,
  message,
}: {
  query: RetryableQuery;
  /**
   * Override the rendered copy. Defaults to the envelope `detail` verbatim
   * (docs/47 §3) via `apiErrorMessage`; an override never drops the detail —
   * callers pass it only to ADD context, never to replace it.
   */
  message?: string;
}) {
  const live = query.error !== null && query.error !== undefined;
  const liveDetail = live ? (message ?? apiErrorMessage(query.error)) : undefined;
  const liveRetryable = live && isRetryableError(query.error);

  // Render-phase state adjustment (React's documented "adjust state when a
  // prop changes" pattern): the record updates in the same paint as the new
  // failure, so the detail is never a frame stale and an effect never races
  // the retry it is meant to describe.
  const [record, setRecord] = useState<FailureRecord | null>(
    live ? { detail: liveDetail ?? "", retryable: liveRetryable } : null,
  );
  if (live && (record?.detail !== liveDetail || record?.retryable !== liveRetryable)) {
    setRecord({ detail: liveDetail ?? "", retryable: liveRetryable });
  }

  const detail = live ? (liveDetail ?? "") : (record?.detail ?? message ?? "");
  const retryable = live ? liveRetryable : (record?.retryable ?? false);

  const { onRetry, retrying } = useRetryAction({
    retry: retryable ? () => void query.refetch() : null,
    pending: query.isFetching,
  });

  return <ErrorPanel message={detail} onRetry={onRetry} retrying={retrying} />;
}
