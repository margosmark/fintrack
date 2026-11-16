// Bulk operations over /transactions (docs/09 §Bulk selection toolbar, docs/19).
//
// v1 drives the EXISTING single-row routes SEQUENTIALLY — one PATCH or DELETE
// per selected id, in order, with a progress callback between rows (docs/08
// keeps byte-identical contracts for `PATCH /transactions/{id}` and
// `DELETE /transactions/{id}`; no bulk schema is invented here).
//
// The point of the wrapper is the failure shape: a row the server refuses
// (409 `transaction_in_pending_batch`, 404 `not_found`, a transport error)
// becomes a RECORD in `conflicts` and the loop keeps going. Partial success is
// therefore always observable — the caller reports "7 of 9" plus one line per
// rejected row instead of a batch that silently lost or silently skipped rows.
import { ApiError } from "../../api/client";

// M106 (docs/09 §Bulk selection toolbar): review flips are bulk actions too.
// They drive the SAME single-row route as the row toggle, so a bulk run is N
// sequential requests and leaves N receipts — one per row, exactly like N
// individual clicks would.
//
// M125 adds `tag`, and with it the SECOND execution path: while the MATCHED
// set is marked the toolbar makes ONE request to `/transactions/bulk/*`
// instead of N. Both paths answer into the SAME `BulkOutcome`, so the toast,
// the conflict notice and the pagination fix never learn which one ran.
export type BulkAction =
  | "recategorize"
  | "delete"
  | "rename"
  | "review"
  | "unreview"
  | "tag";

/** One row the server refused (or that failed before an envelope came back). */
export interface BulkConflict {
  id: number;
  /** HTTP status when the `{detail, code}` envelope carried one. */
  status: number | null;
  /** Error-envelope `code` (docs/08), null for a non-envelope failure. */
  code: string | null;
  detail: string;
  /**
   * M125: the row's own payee, when the server named it. A matched set can
   * skip a row on page 7 that the browser never loaded, and `#4711` is not a
   * name a person can act on.
   */
  label?: string;
}

export interface BulkOutcome {
  /**
   * The ids a page-scoped run accepted. Empty on a matched run by design —
   * the whole point of that path is that the ids never leave the server.
   */
  okIds: number[];
  /** Rows the run accepted (the page path's `okIds.length`). */
  okCount: number;
  /**
   * Rows the run ATTEMPTED: `okCount + conflicts.length` on a page run, the
   * server's `matched` on a matched run (which can differ when a refusal
   * stopped a row the count still included).
   */
  attempted: number;
  conflicts: BulkConflict[];
}

export type BulkProgress = (done: number, total: number) => void;

function toConflict(id: number, err: unknown): BulkConflict {
  if (err instanceof ApiError) {
    return { id, status: err.status, code: err.code, detail: err.message };
  }
  return {
    id,
    status: null,
    code: null,
    detail: err instanceof Error ? err.message : String(err),
  };
}

/**
 * Run `call` once per id, in order, never aborting the batch on a rejection.
 *
 * Sequential on purpose (docs/09): N rows are N authenticated requests, and a
 * parallel burst would trade a progress indicator for rate-limit 429s on the
 * one surface import-heavy users hammer hardest.
 */
export async function runSequential(
  ids: readonly number[],
  call: (id: number) => Promise<unknown>,
  onProgress?: BulkProgress,
): Promise<BulkOutcome> {
  const okIds: number[] = [];
  const conflicts: BulkConflict[] = [];
  const total = ids.length;
  let done = 0;
  for (const id of ids) {
    try {
      await call(id);
      okIds.push(id);
    } catch (err) {
      conflicts.push(toConflict(id, err));
    }
    done += 1;
    onProgress?.(done, total);
  }
  return { okIds, okCount: okIds.length, attempted: total, conflicts };
}

/**
 * M125: the server's `BulkResult` in the same shape a page run produces.
 *
 * The count the confirmation printed is `matched`; the rows that landed are
 * `updated`; every refusal arrives already named. Nothing here is inferred by
 * the client, so the toast cannot disagree with what the server did.
 */
export function outcomeFromResult(result: {
  matched: number;
  updated: number;
  skipped: Array<{ id: number; label: string; code: string | null; detail: string }>;
}): BulkOutcome {
  return {
    okIds: [],
    okCount: result.updated,
    attempted: result.matched,
    conflicts: result.skipped.map((row) => ({
      id: row.id,
      status: null,
      code: row.code,
      detail: row.detail,
      label: row.label,
    })),
  };
}

/** "Deleted 7 of 9 rows; 2 were skipped." — the one-line partial report. */
export function summarizeOutcome(action: BulkAction, outcome: BulkOutcome): string {
  const total = outcome.attempted;
  const verb =
    action === "delete"
      ? "Deleted"
      : action === "recategorize"
        ? "Recategorized"
        : action === "review"
          ? "Reviewed"
          : action === "unreview"
            ? "Unreviewed"
            : action === "tag"
              ? "Tagged"
              : "Renamed";
  if (outcome.conflicts.length === 0) {
    return `${verb} ${outcome.okCount} of ${total}`;
  }
  return `${verb} ${outcome.okCount} of ${total} — ${outcome.conflicts.length} skipped`;
}
