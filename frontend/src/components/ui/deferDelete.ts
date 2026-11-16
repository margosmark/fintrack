// M95 P3 — the two-stage destructive action (docs/09 §Destructive actions).
//
// Stage 1 (immediate): the caller hides the row optimistically and a snackbar
// offers Undo for 5 seconds. NOTHING has been sent at that point.
// Stage 2 (after the window, or on Dismiss): the real request fires.
//   - success  → the caller refetches so server truth replaces the optimistic
//                state;
//   - failure  → the row is restored locally and the server's message is
//                surfaced VERBATIM (the 409 `account_in_use` detail included);
// Undo during the window → the timer is cancelled and the row is restored;
// nothing was ever dispatched, so there is nothing else to compensate for.

import { apiErrorMessage } from "../transactions/format";
import type { ToastApi } from "./Toast";

export interface DeferredDeleteOptions {
  /** What the snackbar announces: `Deleted transaction`, `Deleted RBC …`. */
  title: string;
  /** Optional second line: the payee, the batch file name, … */
  detail?: string;
  /** Stage 1 — take it out of the UI now. */
  hide: () => void;
  /** Undo (and commit failure) — put it back exactly. */
  restore: () => void;
  /** Stage 2 — the REAL request, fired only when the window closes. */
  commit: () => Promise<unknown>;
  /** Commit succeeded — refetch/invalidate so the server's truth wins. */
  onCommitted?: () => void;
  /**
   * Commit failed — restore already happened; surface `message` verbatim.
   *
   * `error` is the raw rejection (added for docs/47 §4): the formatted
   * `message` alone cannot be told apart from any other failure, and a demo
   * 403 must reach the caller's refusal path instead of an error toast — an
   * error toast renders `role="alert"`.
   */
  onError: (message: string, error?: unknown) => void;
}

export function deferDelete(toast: ToastApi, options: DeferredDeleteOptions): void {
  options.hide();
  toast.undo({
    title: options.title,
    description: options.detail,
    onCommit: () => {
      void (async () => {
        try {
          await options.commit();
          options.onCommitted?.();
        } catch (err) {
          options.restore();
          options.onError(apiErrorMessage(err), err);
        }
      })();
    },
    onUndo: () => options.restore(),
  });
}
