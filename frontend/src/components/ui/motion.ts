// Shared framer-motion vocabulary (ui-contract move #4): one entrance
// language across the app — fade + 12px rise, staggered cards, spring hovers.
// All animations are transform/opacity only (compositor-friendly).

import type { Transition, Variants } from "framer-motion";

/** Signature ease-out-quart-ish curve; never bounce/elastic. */
export const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

export const SPRING: Transition = { type: "spring", stiffness: 380, damping: 30 };

/** Container that staggers its children (Cards) in. */
export const staggerContainer = (stagger = 0.05, delayChildren = 0): Variants => ({
  hidden: {},
  show: { transition: { staggerChildren: stagger, delayChildren } },
});

/** Child of a staggerContainer — fade + rise 12px. */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE } },
};

/** Standalone entrance (no container needed). */
export const enterVariant = (delay = 0): Variants => ({
  hidden: { opacity: 0, y: 12 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE, delay } },
});

/** Route/tab swap transition — quick, subtle, no layout thrash. */
export const routeFade = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
  transition: { duration: 0.22, ease: EASE },
};

/** Branded session-probe splash: gentle scale breath, loops until ready. */
export const splashPulse = {
  animate: { scale: [1, 1.08, 1], opacity: [0.85, 1, 0.85] },
  transition: { duration: 1.6, repeat: Infinity, ease: EASE },
};
