// StateRow (M185, docs/47 §3) — ONE compact row per in-panel state. The
// artifact's `state-row` does NOT replace `EmptyState`: the product's
// three-part empty-state contract (title + real capture + the one action
// that fills it, type-enforced at 47 call sites) is the STRONGER user-visible
// contract, so the compact row is additive — it is the in-panel loading
// skeleton, the stale/refresh strip, and the info line. Errors keep
// `ErrorPanel` (role=alert, envelope detail verbatim) plus its new Try again.
//
// Never a wall of shimmer, never a spinner: loading is exactly one compact
// skeleton row (`state-row-loading`). The action never shrinks below its
// label (flex-none), and below 560px the action pair leaves the text line
// and stacks right-aligned instead of being crushed — the artifact's own
// 360px CTA rule (gate M1: client width == scroll width, never clipped).

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Skeleton } from "./Skeleton";
import { DemoLock } from "./DemoLock";

export interface StateRowAction {
  label: string;
  /** A real route — renders as the button-shaped link (EmptyState's classes). */
  to?: string;
  /** An in-page callback that really does the thing. */
  onClick?: () => void;
  testId?: string;
  /**
   * docs/47 §4 (F-3): the action's request is a blocked demo write — the
   * button then renders the `DemoLock` padlock and binds itself to the
   * caller's VISIBLE reason element with `aria-describedby`. Button actions
   * only: a `to` action is a navigation, never a write, so it never locks.
   */
  demoLock?: { reasonId: string };
}

export interface StateRowProps {
  /** Exactly one compact row per state — never stacked variants. */
  variant?: "loading" | "neutral" | "warn";
  icon?: ReactNode;
  title: string;
  detail?: string;
  action?: StateRowAction;
  testId?: string;
}

const TONES = {
  // Hairline + panel surface (M185): no glass, no glow on a state row.
  loading: "border-rule bg-surface-raised",
  neutral: "border-rule bg-surface-raised",
  warn: "border-warn/30 bg-warn/10",
} as const;

export function StateRow({
  variant = "neutral",
  icon,
  title,
  detail,
  action,
  testId,
}: StateRowProps) {
  return (
    <div
      data-testid={
        testId ?? (variant === "loading" ? "state-row-loading" : "state-row")
      }
      role={variant === "loading" ? "status" : undefined}
      aria-label={variant === "loading" ? "Loading" : undefined}
      className={`flex flex-wrap items-center gap-3.5 rounded-[10px] border p-4 ${TONES[variant]}`}
    >
      {variant === "loading" ? (
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-veil/[0.05]">
          <Skeleton className="size-4" />
        </span>
      ) : (
        icon !== undefined && (
          <span
            className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${
              variant === "warn"
                ? "bg-warn/15 text-warn"
                : "bg-veil/[0.05] text-muted"
            }`}
            aria-hidden
          >
            {icon}
          </span>
        )
      )}
      <div className="min-w-0 flex-1">
        {variant === "loading" ? (
          <>
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="mt-2 h-3 w-24" />
          </>
        ) : (
          <>
            <p className="text-sm font-semibold">{title}</p>
            {detail !== undefined && (
              <p
                className={
                  variant === "warn"
                    ? "mt-0.5 text-xs text-warn"
                    : "mt-0.5 text-xs text-muted"
                }
              >
                {detail}
              </p>
            )}
          </>
        )}
      </div>
      {action !== undefined && variant !== "loading" && (
        <div className="ml-auto flex flex-none items-center gap-3">
          {action.to !== undefined ? (
            <Link
              to={action.to}
              data-testid={action.testId}
              className="inline-flex h-9 items-center justify-center whitespace-nowrap rounded-lg border border-control px-3.5 text-sm font-medium transition-colors duration-200 hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent"
            >
              {action.label}
            </Link>
          ) : (
            <button
              type="button"
              data-testid={action.testId}
              onClick={action.onClick}
              aria-describedby={action.demoLock?.reasonId}
              className="inline-flex h-9 items-center justify-center whitespace-nowrap rounded-lg border border-control px-3.5 text-sm font-medium transition-colors duration-200 hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent"
            >
              {action.demoLock !== undefined && <DemoLock />}
              {action.label}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
