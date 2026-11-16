// Empty/error states (ui-contract move #8, M95 P2 "empty-state choreography"):
// every surface that waits for data renders the SAME three-part contract —
//
//   (1) what will live here        → `title` (+ optional `hint` body copy)
//   (2) what it looks like when    → `preview`: a REAL product capture, never
//       real                         an illustration (the guides' figure style)
//   (3) the one action that fills  → `action`: a deep link or an in-page
//       it                            callback that actually creates the data
//
// All three are REQUIRED by the type: a call site with no preview must say why
// (`preview={{ none: "…" }}` — a documented exception in docs/09 §Empty
// states) and an action must be either `to` (a route) or `onClick` (the page's
// own create control). Errors render separately in a red-tinted glass panel
// showing the API {detail}.

import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { Link, useInRouterContext } from "react-router-dom";
import { CircleAlert } from "lucide-react";
import { fadeUp } from "./motion";
import { Button, BASE, VARIANTS, SIZES } from "./Button";

/** (2) A real capture of the surface with data in it. */
export interface EmptyPreviewShot {
  src: string;
  alt: string;
}

/**
 * (2) Either a real capture, or an explicit, documented reason why no
 * populated capture can exist for this surface — the `none` branch is the ONLY
 * way to omit the image, and docs/09 §Empty states lists every one of them.
 */
export type EmptyPreview = EmptyPreviewShot | { none: string };

export type EmptyAction =
  | {
      label: string;
      to: string;
      onClick?: undefined;
      ariaLabel?: string;
      /** M141: render the control DISABLED and say why (docs/09 §Empty states). */
      disabledReason?: string;
    }
  | {
      label: string;
      onClick: () => void;
      to?: undefined;
      ariaLabel?: string;
      disabledReason?: string;
    };

function isShot(preview: EmptyPreview): preview is EmptyPreviewShot {
  return "src" in preview;
}

/**
 * M141 disabled-with-reason affordance (docs/09 §Empty states, the M140 G2
 * pattern): a control that cannot deliver is DISABLED and names the reason
 * next to it, rather than sitting there enabled and lying. `aria-describedby`
 * binds the two so a screen reader hears the reason with the button.
 */
function DisabledAction({ label, ariaLabel, reason }: { label: string; ariaLabel?: string; reason: string }) {
  const reasonId = `empty-action-reason-${label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
  return (
    <div className="mt-3 flex flex-col items-center gap-1.5" data-testid="empty-state-action">
      <Button variant="primary" size="md" disabled aria-label={ariaLabel} aria-describedby={reasonId}>
        {label}
      </Button>
      <p id={reasonId} className="max-w-sm text-xs text-muted" data-testid="empty-state-action-reason">
        {reason}
      </p>
    </div>
  );
}

/** The shared button classes, so a `to` link renders exactly like the button. */
const ACTION_CLASSES = `${BASE} ${VARIANTS.primary} ${SIZES.md[0]}`;

export function EmptyState({
  icon,
  title,
  hint,
  preview,
  action,
}: {
  icon: ReactNode;
  /** (1) heading-size lead — the guides' heading weight at body scale. */
  title: string;
  hint?: string;
  /** (2) a real capture, or the documented `none` reason. */
  preview: EmptyPreview;
  /** (3) the one control that fills this surface. */
  action: EmptyAction;
}) {
  // Inside the app there is always a router, so the deep link is a real SPA
  // <Link>. Tests (and any static render) may mount a page WITHOUT one — the
  // honest fallback is a plain anchor, which still navigates, instead of
  // throwing `useContext(...)` at a page that only wanted to draw a CTA.
  const inRouter = useInRouterContext();
  return (
    <motion.div
      variants={fadeUp}
      data-testid="empty-state"
      className="flex min-h-48 flex-col items-center justify-center gap-2 p-8 text-center"
    >
      <div className="mb-1 flex size-11 items-center justify-center rounded-xl bg-veil/[0.05] text-muted [&_svg]:size-5">
        {icon}
      </div>
      <p className="text-base font-semibold" data-testid="empty-state-title">
        {title}
      </p>
      {hint !== undefined && (
        <p className="max-w-sm text-sm text-muted" data-testid="empty-state-hint">
          {hint}
        </p>
      )}
      {/* (2) Real capture in the guides' figure style (docs/09 §Guide
          screenshots): bordered, rounded, uncropped, so the reader recognises
          the whole page they are about to fill. */}
      {isShot(preview) && (
        <figure className="mt-2 w-full max-w-sm" data-testid="empty-state-preview">
          <img
            src={preview.src}
            alt={preview.alt}
            loading="lazy"
            decoding="async"
            className="w-full rounded-lg border border-veil/[0.08]"
          />
        </figure>
      )}
      {action.disabledReason !== undefined ? (
        <DisabledAction
          label={action.label}
          ariaLabel={action.ariaLabel}
          reason={action.disabledReason}
        />
      ) : action.to !== undefined ? (
        inRouter ? (
          <Link
            to={action.to}
            aria-label={action.ariaLabel}
            data-testid="empty-state-action"
            className={`mt-3 whitespace-nowrap ${ACTION_CLASSES}`}
          >
            <span>{action.label}</span>
          </Link>
        ) : (
          <a
            href={action.to}
            aria-label={action.ariaLabel}
            data-testid="empty-state-action"
            className={`mt-3 whitespace-nowrap ${ACTION_CLASSES}`}
          >
            <span>{action.label}</span>
          </a>
        )
      ) : (
        <div className="mt-3" data-testid="empty-state-action">
          <Button variant="primary" size="md" onClick={action.onClick} aria-label={action.ariaLabel}>
            {action.label}
          </Button>
        </div>
      )}
    </motion.div>
  );
}

/** Red-tinted glass panel for failed loads — surfaces the API detail.
 *
 * M185 (docs/47 §3): gains `data-testid="error-panel"` (the state taxonomy's
 * pinned selector) and an OPTIONAL `Try again` bound to the query's own
 * `refetch()` — additive: a panel without `onRetry` renders exactly what it
 * rendered before. The retry is the difference between "here is the failure"
 * and "here is the way back" (acceptance example 15).
 *
 * F-RETRY: the button now tells the truth about the round trip. While the
 * retry is in flight it is DISABLED and reads `Retrying…` (`aria-busy`, plus a
 * polite `role="status"` live region, so the pending state is announced and
 * not merely painted) — one press is one request, and a second press during
 * the flight cannot stack a duplicate. The envelope `detail` underneath never
 * changes while the retry runs, so the failure a retry is answering stays on
 * screen until the answer arrives. Sites bind it through
 * `components/ui/QueryErrorPanel` / `lib/retry`, which decide WHEN a retry is
 * honest; nothing renders a retry for a policy refusal. */
export function ErrorPanel({
  message,
  onRetry,
  retrying = false,
}: {
  message: string;
  /** The query's own refetch — never a page reload, never a new query key. */
  onRetry?: () => void;
  /** The retry is in flight: the control disables itself and says so. */
  retrying?: boolean;
}) {
  return (
    <motion.div
      variants={fadeUp}
      /* Live region: a failed load or rejected submit must be SPOKEN, not just
         painted (docs/09 Naming & semantics contract). */
      role="alert"
      data-testid="error-panel"
      className="flex min-h-36 items-start gap-2.5 rounded-xl border border-danger/30 bg-danger/[0.06] p-4 text-sm"
    >
      <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-danger">Something went wrong</p>
        <p className="mt-0.5 break-words text-xs text-muted">{message}</p>
        {onRetry !== undefined && (
          <div className="mt-3 flex items-center gap-2.5">
            <button
              type="button"
              data-testid="error-panel-retry"
              onClick={onRetry}
              disabled={retrying}
              aria-busy={retrying || undefined}
              className="inline-flex h-9 items-center justify-center whitespace-nowrap rounded-lg border border-control px-3.5 text-sm font-medium transition-colors duration-200 hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent disabled:cursor-progress disabled:opacity-70"
            >
              {retrying ? "Retrying…" : "Try again"}
            </button>
            {/* The pending state is ANNOUNCED, not just painted: a live region
                that exists before the text changes (an aria-live element added
                with its content is not reliably spoken). Empty at rest, so it
                adds nothing to the panel's reading order. */}
            <span role="status" aria-live="polite" className="sr-only">
              {retrying ? "Retrying…" : ""}
            </span>
          </div>
        )}
      </div>
    </motion.div>
  );
}
