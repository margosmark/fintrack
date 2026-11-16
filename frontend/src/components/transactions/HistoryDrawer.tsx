// HistoryDrawer — the per-transaction activity log (M106, docs/09 §History drawer).
//
// "Edits never silently vanish": every mutation the server receipted for this
// row is one line here, newest first, rendered from the STORED payload at read
// time — so the wording can change without a data migration, and the payload
// stays the single source the sentence is drawn from (docs/02
// §transaction_events). The receipt's hash rides along in the `title`: the
// line is what happened, the hash is the proof it was written then.
//
// Dialog contract: the SHARED helpers from `dialogA11y` — focus moves in on
// open, Escape closes, focus returns to the opener, Tab keeps advancing (no
// focus trap, docs/09 §Keyboard contract).

import { useRef } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { listTransactionEvents } from "../../api/transactions";
import { QueryErrorPanel } from "../ui/QueryErrorPanel";
import { SkeletonRows } from "../ui/Skeleton";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";

/** "Oct 5" — the date the drawer lines are read against (docs/09). */
function stamp(iso: string | null): string {
  if (iso === null) return "";
  return new Date(iso).toLocaleDateString("en-CA", { month: "short", day: "numeric" });
}

/** docs/04 §Learning loop: a receipt carrying the `learned` marker is a
 * correction that taught the payee -> category rule, and the drawer says so —
 * the payload is the source, so the wording is ours to change (docs/02
 * §transaction_events). */
function learnedFromYou(payload: Record<string, unknown> | null | undefined): boolean {
  // The drawer already reads `event.payload && …`, i.e. the field is not
  // guaranteed at runtime — a receipt without a payload must render as "no
  // badge", never as a crash inside the drawer.
  if (typeof payload !== "object" || payload === null) return false;
  const marker = payload.learned;
  return typeof marker === "object" && marker !== null;
}

interface HistoryDrawerProps {
  transactionId: number;
  /** Shown in the heading — the row's payee, so two open drawers never read alike. */
  title: string;
  onClose: () => void;
}

export function HistoryDrawer({ transactionId, title, onClose }: HistoryDrawerProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(dialogRef);
  useEscapeToClose(onClose);

  const events = useQuery({
    queryKey: ["transaction-events", transactionId],
    queryFn: () => listTransactionEvents(transactionId),
  });

  const items = events.data?.items ?? [];

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex justify-end bg-overlay/60"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="history-drawer-title"
        tabIndex={-1}
        className="glass flex h-full w-full max-w-md flex-col p-5 shadow-2xl outline-none"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="history-drawer-title" className="text-lg font-semibold">
              History
            </h2>
            <p className="mt-1 truncate text-xs text-muted">{title}</p>
          </div>
          <button
            type="button"
            aria-label="Close history"
            onClick={onClose}
            className="rounded-lg p-1 text-muted outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
          >
            ×
          </button>
        </div>

        {events.isPending && (
          <div className="mt-4">
            <SkeletonRows rows={5} />
          </div>
        )}
        {events.isError && (
          <div className="mt-4">
            <QueryErrorPanel query={events} />
          </div>
        )}

        {/* No EmptyState here on purpose: the three-part contract's preview
            exceptions are a pinned set (docs/09 §Empty states), and a dialog
            that lists an empty collection only needs to say so. */}
        {!events.isPending && !events.isError && items.length === 0 && (
          <p className="mt-4 text-sm text-muted">
            Nothing recorded yet — every change you make from now on lands here.
          </p>
        )}

        {!events.isPending && !events.isError && items.length > 0 && (
          <ol className="mt-4 flex-1 space-y-3 overflow-y-auto pr-1">
            {items.map((event) => (
              <li key={event.id} className="flex gap-3 border-b border-veil/[0.05] pb-3">
                <span className="w-14 shrink-0 text-xs text-muted tabular">
                  {stamp(event.at)}
                </span>
                <span className="min-w-0 flex-1 text-sm text-text">
                  {event.summary}
                  {learnedFromYou(event.payload) && (
                    <span className="ml-1.5 inline-flex items-center rounded-full border border-accent/40 bg-accent/10 px-1.5 py-px align-middle text-[10px] font-medium text-accent">
                      learned from you
                    </span>
                  )}
                  <span
                    className="mt-0.5 block truncate text-[10px] text-muted"
                    title={`receipt ${event.payload_hash}`}
                  >
                    {event.event_kind} · {event.payload_hash.slice(0, 12)}…
                  </span>
                </span>
              </li>
            ))}
          </ol>
        )}

        {!events.isPending && !events.isError && items.length > 0 && (
          <p className="mt-3 border-t border-veil/[0.05] pt-3 text-[11px] text-muted">
            {items.length} receipt{items.length === 1 ? "" : "s"} · newest first · nothing here
            can be edited or removed
          </p>
        )}
      </div>
    </div>,
    document.body,
  );
}
