// Toast system (ui-contract move #6): top-right stack, slide+fade via
// AnimatePresence, auto-dismiss. Every mutation in the app reports through
// `toast.success / error / info`.
//
// M95 P3 adds the one asynchronous flavour: `toast.undo(...)` — the
// destructive-action snackbar (docs/09 §Destructive actions). NOTHING has been
// sent when it appears; the real request fires only when the 5-second window
// closes (or the user presses Dismiss, which acknowledges and commits now).
// Undo during the window cancels the timer and restores locally — nothing was
// ever dispatched, so there is nothing to compensate for.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { CircleCheck, CircleX, Info, Undo2 } from "lucide-react";
import { EASE } from "./motion";

type Tone = "success" | "error" | "info";

/** The M95 P3 undo window: 5 seconds (docs/09 §Destructive actions). */
export const UNDO_WINDOW_MS = 5000;

export interface UndoToastRequest {
  /** "Deleted transaction", "Deleted RBC Main Chequing", … */
  title: string;
  description?: string;
  /** Defaults to UNDO_WINDOW_MS (5000). */
  durationMs?: number;
  /** Stage 2: the REAL request. Runs when the window closes or Dismiss is pressed. */
  onCommit: () => void;
  /** Undo: cancel the timer and restore locally — nothing was ever sent. */
  onUndo: () => void;
}

interface UndoRuntime {
  expiresAt: number;
  /** Dismiss acknowledges the snackbar: close it and commit immediately. */
  dismiss: () => void;
  undoNow: () => void;
}

interface ToastItem {
  id: number;
  tone: Tone;
  title: string;
  description?: string;
  undo?: UndoRuntime;
}

export interface ToastApi {
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
  info: (title: string, description?: string) => void;
  undo: (request: UndoToastRequest) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/** Usage: const toast = useToast(); toast.success("Batch committed"). */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (ctx === null) {
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return ctx;
}

/** The plain auto-dismiss window for success/error/info toasts (docs/09). */
export const AUTO_DISMISS_MS = 4200;

const TONE_STYLE: Record<Tone, { icon: typeof CircleCheck; ring: string; glow: string }> = {
  success: { icon: CircleCheck, ring: "text-gain", glow: "" },
  error: { icon: CircleX, ring: "text-danger", glow: "shadow-glow-danger" },
  info: { icon: Info, ring: "text-accent", glow: "" },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const reduced = useReducedMotion();
  // One ticker for every visible undo window (started only while one exists),
  // so the countdown label is honest about how much time is left.
  const [now, setNow] = useState(() => Date.now());
  const hasUndo = items.some((item) => item.undo !== undefined);
  useEffect(() => {
    if (!hasUndo) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasUndo]);

  // Every dismiss/commit timer still armed right now. A `setTimeout` outlives
  // the React render that scheduled it, so the provider cancels its own on
  // unmount: in the SPA the provider outlives every toast (harmless), but in
  // vitest a test that pushes a toast and ends <4.2 s later leaves the timer
  // armed, the file's jsdom environment is torn down, and the callback fires
  // into a dead `window` — react-dom throws `ReferenceError: window is not
  // defined` from `getCurrentEventPriority`, vitest reports an unhandled error
  // and exits 1 with ZERO failing tests. A Set, not a list, so the tally stays
  // bounded by the visible stack: each timer drops its own entry when it fires
  // (or when `finish` settles the undo window early), and the unmount sweep
  // below clears whatever is genuinely still armed.
  const armedTimers = useRef<Set<number>>(new Set());
  useEffect(() => {
    const armed = armedTimers.current;
    return () => {
      for (const timer of armed) window.clearTimeout(timer);
      armed.clear();
    };
  }, []);

  const push = useCallback((tone: Tone, title: string, description?: string) => {
    const id = nextId.current++;
    setItems((prev) => [...prev.slice(-4), { id, tone, title, description }]);
    const timer = window.setTimeout(() => {
      armedTimers.current.delete(timer);
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, AUTO_DISMISS_MS);
    armedTimers.current.add(timer);
  }, []);

  const pushUndo = useCallback((request: UndoToastRequest) => {
    const id = nextId.current++;
    const durationMs = request.durationMs ?? UNDO_WINDOW_MS;
    const expiresAt = Date.now() + durationMs;
    // Exactly one of commit/undo may run: the flag survives the button that
    // removes the item and the timer that would otherwise fire behind it.
    let settled = false;
    let timer = 0;
    const finish = (outcome: "commit" | "undo"): void => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      armedTimers.current.delete(timer);
      setItems((prev) => prev.filter((t) => t.id !== id));
      if (outcome === "commit") request.onCommit();
      else request.onUndo();
    };
    timer = window.setTimeout(() => finish("commit"), durationMs);
    armedTimers.current.add(timer);
    // Re-stamp `now`: the 1s ticker only runs while an item is visible, so a
    // stale value here would make the first label overshoot ("6s").
    setNow(Date.now());
    setItems((prev) => [
      ...prev.slice(-4),
      {
        id,
        tone: "info",
        title: request.title,
        description: request.description,
        undo: {
          expiresAt,
          dismiss: () => finish("commit"),
          undoNow: () => finish("undo"),
        },
      },
    ]);
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      success: (title, description) => push("success", title, description),
      error: (title, description) => push("error", title, description),
      info: (title, description) => push("info", title, description),
      undo: pushUndo,
    }),
    [push, pushUndo],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* Top-right viewport-fixed stack; pointer-events none on the wrapper. */}
      <div
        className="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
        role="region"
        aria-label="Notifications"
      >
        <AnimatePresence>
          {items.map(({ id, tone, title, description, undo }) => {
            const { icon: Icon, ring, glow } = TONE_STYLE[tone];
            const secondsLeft =
              undo === undefined
                ? 0
                : Math.max(0, Math.ceil((undo.expiresAt - now) / 1000));
            return (
              <motion.div
                key={id}
                layout
                role={tone === "error" ? "alert" : "status"}
                aria-live={undo !== undefined ? "polite" : undefined}
                data-testid={undo !== undefined ? "undo-snackbar" : undefined}
                initial={reduced ? { opacity: 0 } : { opacity: 0, x: 32 }}
                animate={reduced ? { opacity: 1 } : { opacity: 1, x: 0 }}
                exit={reduced ? { opacity: 0 } : { opacity: 0, x: 32, transition: { duration: 0.15 } }}
                transition={{ duration: 0.3, ease: EASE }}
                className={`glass-strong pointer-events-auto flex items-start gap-2.5 rounded-xl border-l-2 p-3 shadow-card ${
                  tone === "error" ? "border-l-danger" : tone === "success" ? "border-l-gain" : "border-l-accent"
                } ${glow}`}
              >
                {undo !== undefined ? (
                  <Undo2 className={`mt-0.5 size-4 shrink-0 ${ring}`} aria-hidden />
                ) : (
                  <Icon className={`mt-0.5 size-4 shrink-0 ${ring}`} aria-hidden />
                )}
                <div className="min-w-0 text-sm">
                  <p className="font-medium leading-5">{title}</p>
                  {description !== undefined && (
                    <p className="mt-0.5 break-words text-xs leading-4 text-muted">{description}</p>
                  )}
                  {undo !== undefined && (
                    /* Touch floor: the stack sits outside `main`, so the <768
                       44px rule never reaches it — the buttons carry it
                       themselves (docs/09 §Touch-target floor). */
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        data-testid="undo-snackbar-undo"
                        onClick={undo.undoNow}
                        className="min-h-11 rounded-md border border-control bg-surface px-3 text-xs font-medium text-text outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent md:min-h-7 md:px-2.5"
                      >
                        Undo ({secondsLeft}s)
                      </button>
                      <button
                        type="button"
                        data-testid="undo-snackbar-dismiss"
                        onClick={undo.dismiss}
                        className="min-h-11 rounded-md px-3 text-xs text-muted outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent md:min-h-7 md:px-2.5"
                      >
                        Dismiss
                      </button>
                    </div>
                  )}
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}
