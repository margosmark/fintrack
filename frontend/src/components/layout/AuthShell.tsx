// Auth hero shell (ui-contract move #10): centered glass card over a
// slow-drifting ambient background — pure CSS radial gradients, transform-only
// animation, disabled entirely under prefers-reduced-motion.

import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { Zap } from "lucide-react";
import { fadeUp } from "../ui/motion";

export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="relative grid min-h-dvh place-items-center overflow-hidden bg-surface px-4">
      {/* Ambient field: three drifting radials, very low alpha. */}
      <div className="pointer-events-none absolute inset-0" aria-hidden>
        <div className="ambient-blob blob-a animate-drift-a left-[-10%] top-[-15%] size-[34rem] opacity-[0.16]" />
        <div className="ambient-blob blob-b animate-drift-b bottom-[-20%] right-[-10%] size-[38rem] opacity-[0.1]" />
        {/* Third blob drifts on the b timeline for phase separation. */}
        <div
          className="ambient-blob blob-c animate-drift-a bottom-[10%] left-[30%] size-[26rem] opacity-[0.07]"
          data-drift-b=""
        />
      </div>

      <motion.div variants={fadeUp} initial="hidden" animate="show" className="relative z-10 w-full max-w-sm">
        {/* Brand mark */}
        <div className="mb-6 flex flex-col items-center gap-2">
          <span className="flex size-11 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-soft text-accent-ink shadow-glow">
            <Zap className="size-5" aria-hidden />
          </span>
          <span className="text-lg font-semibold tracking-tight">fin_tracker</span>
          <span className="text-xs text-muted">Personal finance, kept private</span>
        </div>
        <div className="glass rounded-2xl p-7 shadow-card">{children}</div>
      </motion.div>
    </div>
  );
}
