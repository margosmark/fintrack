// Badge primitive (ui-contract move #7): colored dot + label treatment for
// duplicate/unresolved/stale/status markers instead of raw words.

import type { ReactNode } from "react";

export type BadgeTone = "neutral" | "accent" | "success" | "warn" | "danger";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-border text-muted bg-veil/[0.03]",
  accent: "border-accent/30 text-accent bg-accent/10",
  success: "border-gain/30 text-gain bg-gain/10",
  warn: "border-warn/40 text-warn bg-warn/10",
  danger: "border-danger/40 text-danger bg-danger/10",
};

export function Badge({
  tone = "neutral",
  dot = true,
  pulse = false,
  children,
}: {
  tone?: BadgeTone;
  /** Leading colored dot (default on — the signature treatment). */
  dot?: boolean;
  /** Slow opacity blink on the dot for attention states (stale price). */
  pulse?: boolean;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${TONES[tone]}`}
    >
      {dot && (
        <span
          className={`size-1.5 shrink-0 rounded-full bg-current ${pulse ? "animate-pulse" : ""}`}
          aria-hidden
        />
      )}
      {children}
    </span>
  );
}
