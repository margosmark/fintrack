// Segmented control (ui-contract per-page must-haves): pill that morphs
// between segments via a shared layoutId; used by Reports granularity,
// Taxes year switcher, and anywhere a select would feel heavy.

import { motion } from "framer-motion";
import { SPRING } from "./motion";

export interface SegmentedOption<T extends string | number> {
  value: T;
  label: string;
  title?: string;
}

export function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
  layoutId,
  size = "md",
  disabled = false,
  className = "",
  ariaLabel,
  ariaDescribedBy,
}: {
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
  /** Unique id so the morphing pill stays local to this control. */
  layoutId: string;
  size?: "sm" | "md";
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
  /**
   * docs/47 §4 — `aria-describedby` for EVERY segment: each button's action
   * is the caller's blocked demo write, so each one is bound to the caller's
   * single VISIBLE `DemoReason` (the padlock glyph itself is rendered by the
   * caller, beside this control).
   */
  ariaDescribedBy?: string;
}) {
  const pad =
    size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-xs sm:text-sm";
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      // The strip itself scrolls (`overflow-x-auto`): focus it so a keyboard
      // user can scroll a row of segments that does not fit (WCAG 2.1.1) —
      // docs/09 §Scroll regions. The tabs inside stay individually focusable.
      tabIndex={0}
      className={`glass inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-lg p-0.5 ${
        disabled ? "opacity-40" : ""
      } ${className}`}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={String(option.value)}
            type="button"
            role="tab"
            aria-selected={active}
            aria-describedby={ariaDescribedBy}
            title={option.title}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={`relative shrink-0 cursor-pointer rounded-md outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${pad} ${
              active ? "font-medium text-accent" : "text-muted hover:text-text"
            }`}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                transition={SPRING}
                className="absolute inset-0 rounded-md bg-veil/[0.08] ring-1 ring-inset ring-veil/[0.06]"
                aria-hidden
              />
            )}
            <span className="relative z-10">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}
