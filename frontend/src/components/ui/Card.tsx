// Glass card primitive (ui-contract move #1): whisper-white surface + blur +
// hairline border; hover lift -2px with border brighten; spring transitions.
// Participates in parent stagger containers via fadeUp variants.

import { forwardRef } from "react";
import type { ReactNode } from "react";
import { motion } from "framer-motion";
import type { HTMLMotionProps, Variants } from "framer-motion";
import { SPRING, fadeUp } from "./motion";

export interface CardProps extends Omit<HTMLMotionProps<"div">, "ref"> {
  /** Enable hover lift + border brighten. */
  hover?: boolean;
  /** Manual entrance delay when not inside a stagger container (seconds). */
  delay?: number;
  /** Override the entrance variant (defaults to fadeUp). */
  variants?: Variants;
  children?: ReactNode;
}

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { hover = false, delay = 0, variants = fadeUp, className = "", children, ...rest },
  ref,
) {
  return (
    <motion.div
      ref={ref}
      variants={variants}
      transition={{ ...SPRING, delay }}
      whileHover={hover ? { y: -2 } : undefined}
      className={`glass glass-hover rounded-xl shadow-card ${className}`}
      {...rest}
    >
      {children}
    </motion.div>
  );
});

/** Section heading inside a card — consistent hierarchy across pages. */
export function CardTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    // flex-wrap: on narrow viewports the `right` slot (stepper, tabs, hints)
    // drops to its own line instead of overflowing the card — its content has a
    // min-content floor that a nowrap row cannot shrink past (M82-F3).
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      {/* overflow-wrap:anywhere — the h2 is a flex item, so an unbreakable
          word sets a floor under it that the row cannot shrink past. At
          Android's 200% font scale (root 32px) "Categorization" alone is
          241.7px of min-content against a 174px card row at 320, so the floor
          held the h2 42.7px past the viewport where main's overflow-x: clip
          cut the title off (M82-F2 200% residual). `anywhere` drops the floor
          to a single character — normal word breaks still win on any line that
          fits, so >=768 layout is byte-identical — and lets the h2 shrink to
          its line instead of standing outside it. */}
      <h2 className="text-sm font-semibold tracking-wide text-text [overflow-wrap:anywhere]">
        {children}
      </h2>
      {right}
    </div>
  );
}
