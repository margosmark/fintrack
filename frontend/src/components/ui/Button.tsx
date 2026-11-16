// Button primitive (ui-contract move #9): primary = accent gradient with glow,
// press scale 0.97, inline spinner swap while loading; ghost variant for
// tables/toolbars; danger for destructive rows.

import { forwardRef } from "react";
import type { ReactNode } from "react";
import { motion } from "framer-motion";
import type { HTMLMotionProps } from "framer-motion";
import { LoaderCircle } from "lucide-react";

type Variant = "primary" | "ghost" | "outline" | "danger" | "accent-soft";
type Size = "sm" | "md" | "lg";

// Exported for the one surface that renders the same visual as a LINK rather
// than a button: `EmptyState`'s deep-link action (M95 P2) reuses these exact
// class strings so a route CTA and a callback CTA are indistinguishable.
export const BASE =
  "relative inline-flex select-none items-center justify-center gap-1.5 rounded-lg font-medium transition-colors duration-200 disabled:pointer-events-none disabled:opacity-40";

export const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-gradient-to-r from-accent to-accent-soft text-accent-ink shadow-glow-sm hover:shadow-glow hover:brightness-110",
  ghost: "text-muted hover:bg-veil/[0.06] hover:text-text",
  outline:
    "border border-control bg-transparent text-text hover:border-accent/70 hover:bg-veil/[0.04] hover:text-accent",
  danger: "border border-danger/30 text-danger hover:bg-danger/10",
  "accent-soft": "bg-accent/15 text-accent hover:bg-accent/25",
};

// [one line, may wrap] — `wrap` swaps `whitespace-nowrap` for
// `whitespace-normal` AND the fixed height for a min-height of the same value,
// because a wrapped label is two lines tall and a fixed `h-7` would print it
// straight out of the button. Without `wrap` the emitted class list is exactly
// the one every existing call site gets today.
export const SIZES: Record<Size, [single: string, wrapped: string]> = {
  sm: ["h-7 px-2.5 text-xs", "min-h-7 px-2.5 text-xs"],
  md: ["h-9 px-3.5 text-sm", "min-h-9 px-3.5 text-sm"],
  lg: ["h-11 px-5 text-sm", "min-h-11 px-5 text-sm"],
};

export interface ButtonProps extends Omit<HTMLMotionProps<"button">, "ref"> {
  variant?: Variant;
  size?: Size;
  /** Swaps content for an inline spinner (ui-contract). */
  loading?: boolean;
  /** Let the label wrap instead of forcing one line — drops the min-content
   *  floor (label + padding) so the button can live in a narrow cell. */
  wrap?: boolean;
  children?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "outline",
    size = "md",
    loading = false,
    wrap = false,
    className = "",
    children,
    disabled,
    ...rest
  },
  ref,
) {
  return (
    <motion.button
      ref={ref}
      whileTap={disabled || loading ? undefined : { scale: 0.97 }}
      className={`${BASE} ${wrap ? "whitespace-normal" : "whitespace-nowrap"} ${
        VARIANTS[variant]
      } ${SIZES[size][wrap ? 1 : 0]} ${className}`}
      disabled={disabled ?? loading}
      {...rest}
    >
      {/* Keep layout stable: dim the label, spin the loader on top. */}
      <span className={`inline-flex items-center gap-1.5 ${loading ? "opacity-0" : ""}`}>
        {children}
      </span>
      {loading && (
        <span className="absolute inset-0 flex items-center justify-center">
          <LoaderCircle className="size-4 animate-spin" aria-hidden />
        </span>
      )}
    </motion.button>
  );
});
