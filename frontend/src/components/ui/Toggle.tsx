// Toggle switch (ui-contract Settings must-have): spring-knob switch used for
// privacy mode, encrypted backup and rule enablement.

import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { SPRING } from "./motion";

export function Toggle({
  checked,
  onChange,
  disabled = false,
  label,
  ariaLabel,
  ariaDescribedBy,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Optional text rendered after the switch. */
  label?: ReactNode;
  ariaLabel?: string;
  /**
   * `aria-describedby` for the switch itself (docs/47 §4): a locked toggle
   * binds its VISIBLE `DemoReason` on the control, not on a wrapper — the
   * attribute has to name the control a screen reader announces.
   */
  ariaDescribedBy?: string;
}) {
  // flex-wrap: the pill's min-content is switch + gap + label — 197px for
  // `Enabled` at 200% root font (root 32px), wider than the 156px form cell
  // it sits in at 320, so the pill stood 73px outside its cell (M82-F2 200%
  // residual). Wrapping lets the label drop under the switch, so min-content
  // becomes the WIDER of the two instead of their sum; a wrapping flex line
  // sizes exactly like a nowrap one while everything fits, so every layout
  // that used to hold keeps the width it had (verified at 768/1024/1280).
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-describedby={ariaDescribedBy}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`inline-flex cursor-pointer flex-wrap items-center gap-2.5 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-accent ${
        disabled ? "cursor-not-allowed opacity-40" : ""
      }`}
    >
      <span
        className={`track relative inline-flex h-[22px] w-10 shrink-0 items-center rounded-full p-[3px] transition-colors duration-300 ${
          checked ? "bg-accent shadow-glow-sm" : "bg-veil/[0.1] ring-1 ring-inset ring-veil/[0.06]"
        }`}
      >
        <motion.span
          layout
          transition={SPRING}
          className={`inline-block size-4 rounded-full ${
            checked ? "ml-auto bg-accent-ink" : "bg-muted"
          }`}
        />
      </span>
      {label !== undefined && <span className="text-sm">{label}</span>}
    </button>
  );
}
