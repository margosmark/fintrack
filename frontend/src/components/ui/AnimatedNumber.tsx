// AnimatedNumber (ui-contract move #2): money values tween on mount and when
// data changes; tabular-nums everywhere; respects prefers-reduced-motion by
// jumping straight to the target.

import { useEffect, useRef, useState } from "react";
import { animate, useReducedMotion } from "framer-motion";
import { EASE } from "./motion";
import { formatCad } from "../../lib/format";
import { MASK_TEXT, useMasked } from "../../lib/mask";

export interface AnimatedNumberProps {
  value: number;
  /** Formats the tweened value; defaults to compact-less CAD currency. */
  format?: (n: number) => string;
  duration?: number;
  className?: string;
  /**
   * M139 (docs/09 § Display masking): force the mask on or off for this value.
   * Omitted, it follows the session mask. Masked, BOTH spans render bullets —
   * the sr-only twin included, so a screen reader never reads the real figure
   * the visual one is hiding.
   */
  mask?: boolean;
}

export function AnimatedNumber({
  value,
  format = (n) => formatCad(n),
  duration = 0.9,
  className = "",
  mask,
}: AnimatedNumberProps) {
  const reduced = useReducedMotion();
  const sessionMasked = useMasked();
  const masked = mask ?? sessionMasked;
  const [display, setDisplay] = useState(reduced ? value : 0);
  const previous = useRef(0);

  useEffect(() => {
    // While masked there is nothing to tween — the target the caller would
    // animate toward is exactly the figure we must not show. Render bullets
    // and park the tween so an un-mask starts from a sane baseline.
    if (masked) {
      previous.current = value;
      setDisplay(value);
      return;
    }
    if (reduced) {
      previous.current = value;
      setDisplay(value);
      return;
    }
    const controls = animate(previous.current, value, {
      duration,
      ease: EASE,
      onUpdate: (v) => setDisplay(v),
    });
    previous.current = value;
    return () => controls.stop();
  }, [value, duration, reduced, masked]);

  return (
    <span className={`tabular ${className}`}>
      {/* The tween is visual only — aria-hidden keeps screen readers from
          reading $0 → $151.8K. The exact figure ships as visually-hidden
          text: aria-label is PROHIBITED on a generic <span> with no role
          (axe `aria-prohibited-attr`), which is what this used to do. */}
      <span aria-hidden>{masked ? MASK_TEXT : format(display)}</span>
      <span className="sr-only">{masked ? MASK_TEXT : format(value)}</span>
    </span>
  );
}

/** Compact CAD for tight spots ($1.2k / $345). Re-exported from the shared
 *  `../../lib/format` so every surface formats identically. */
export { formatCadCompact as fmtCadCompact } from "../../lib/format";
