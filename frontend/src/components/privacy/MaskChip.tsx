// MaskChip (M139, docs/09 § Display masking) — the display mask's controls.
//
// DESIGN-LANGUAGE NOTE (D1): this sits visually adjacent to M136's
// VaultLocked status chip but is deliberately NOT that chip and must never
// become it. The vault answers "who can DECRYPT the bytes"; the mask answers
// "who the screen currently shows its numbers to". Two different questions, so
// two different affordances:
//   * VaultLocked is a LOCK glyph on a neutral/accent chip — a custody fact.
//   * MaskChip is an EYE glyph on a WARN chip — a presentation fact, and one
//     the user flips on and off in a hurry while somebody is standing there.
// The warn tone + the eye + the word "Masked" means the state differs by icon,
// by text AND by colour — never colour alone (docs/09 a11y contract).
//
// The chip is rendered at the TOP of the rail (one of the first things in the
// shell, D1) so the state is impossible to miss, and again as an account-menu
// row at the rail's foot. ⌘⇧H toggles it from anywhere and hands focus back to
// the chip, so a keyboard user lands on the thing they just changed.

import { useEffect, useRef } from "react";
import { Eye, EyeOff } from "lucide-react";
import { toggleMasked, useMasked } from "../../lib/mask";
import { RailTip } from "../layout/RailTip";
import type { RailTipTrigger } from "../layout/RailTip";

/** The live chip element, so ⌘⇧H can hand focus back to it after a flip. */
let chipElement: HTMLButtonElement | null = null;

/** Focus the shell's mask chip (no-op when it is not mounted). */
export function focusMaskChip(): void {
  chipElement?.focus();
}

/** The label both surfaces print, so the chip and the row cannot drift. */
export function maskLabel(masked: boolean): string {
  return masked ? "Masked" : "Unmasked";
}

/**
 * The prominent shell chip. A button, not a badge: one tap flips the mask, so
 * the indicator and the switch are the same control (D1 "one-tap toggle").
 *
 * `collapsed` (the mobile appbar) is a FIXED 44×44 icon-only control — and the
 * collapsed branch builds its OWN class list, deliberately sharing no sizing
 * utility with the expanded branch. QA-360-TOUCH-REG (production, candidate
 * 2df3629) proved the cost of composing it by overrides instead: with
 * `w-full px-3` still in the list, `px-0` lost the stylesheet cascade (its
 * 12px padding rode along), and `w-full` — a percentage against the cluster
 * span's auto width — resolved to the span's whole max-content (chip 60px +
 * gap + 44px trigger = 110px), so the chip ballooned to 110px and evicted the
 * trailing `shrink-0` 44px palette trigger past the appbar's edge (x=358..402
 * at 360px; ~2px visible, center tap impossible). A fixed `size-11` gives the
 * chip an intrinsic width independent of its container, so the cluster reads
 * [44px chip][6px gap][44px trigger] at every width and the grid's auto track
 * can never reflow the trigger off-screen.
 */
export function MaskChip({
  className = "",
  collapsed = false,
}: {
  className?: string;
  collapsed?: boolean;
}) {
  const masked = useMasked();
  const ref = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    chipElement = ref.current;
    return () => {
      chipElement = null;
    };
  }, []);

  const tones = masked
    ? "border-warn/50 bg-warn/15 font-medium text-warn"
    : "border-control bg-veil/[0.04] text-muted hover:border-accent/70 hover:text-text";

  const button = (trigger: RailTipTrigger = {}) => (
    <button
      {...trigger}
      ref={ref}
      type="button"
      data-testid="mask-chip"
      aria-pressed={masked}
      aria-label={collapsed ? maskLabel(masked) : undefined}
      // The state is announced as it changes (D1 a11y): a polite live region
      // elsewhere in the shell reads this string out on every flip.
      aria-description="Hides every dollar amount on screen. Transactions, names and dates stay readable."
      onClick={() => toggleMasked()}
      className={
        collapsed
          ? // Fixed 44×44 icon button — see the component note above: no
            // `w-full`, no `px-*`, no overrides to lose a cascade over.
            `flex size-11 shrink-0 items-center justify-center gap-2.5 rounded-lg border outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent ${tones} ${className}`
          : `flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent ${tones} ${className}`
      }
    >
      {masked ? (
        <EyeOff className="size-[18px] shrink-0" aria-hidden />
      ) : (
        <Eye className="size-[18px] shrink-0" aria-hidden />
      )}
      {!collapsed && <span className="flex-1 text-left">{maskLabel(masked)}</span>}
      {/* Icon + text + colour: three channels, so the state never rides on
          colour alone (docs/09 a11y). The dot is the fourth, decorative. */}
      <span
        className={`size-1.5 shrink-0 rounded-full ${masked ? "bg-warn" : "bg-muted/50"}`}
        aria-hidden
      />
    </button>
  );

  if (!collapsed) return button();
  // Collapsed: same control, but the label moves to the rail's hover/focus
  // bubble (M100), exactly like every other icon-only rail row.
  return <RailTip label={maskLabel(masked)}>{button}</RailTip>;
}

/**
 * The account-menu row (D1: the switch lives in the shell's account menu too).
 * Same state, same vocabulary, quieter chrome — it sits beside the theme switch
 * at the rail's foot, where every other session-level control lives.
 */
export function MaskToggleRow({ collapsed = false }: { collapsed?: boolean }) {
  const masked = useMasked();
  const label = masked ? "Unmask amounts" : "Mask amounts";
  const button = (trigger: RailTipTrigger = {}) => (
    <button
      {...trigger}
      type="button"
      data-testid="mask-toggle-row"
      aria-pressed={masked}
      aria-label={collapsed ? label : undefined}
      onClick={() => toggleMasked()}
      className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent ${
        collapsed ? "min-h-11 justify-center px-0" : ""
      }`}
    >
      {masked ? (
        <EyeOff className="size-[18px] shrink-0" aria-hidden />
      ) : (
        <Eye className="size-[18px] shrink-0" aria-hidden />
      )}
      {!collapsed && <span className="flex-1">{label}</span>}
    </button>
  );
  if (!collapsed) return button();
  return <RailTip label={label}>{button}</RailTip>;
}

/**
 * The aria-live announcer (D1 a11y). A blind user flipping ⌘⇧H gets the new
 * state read out; sighted users get the chip. One component owns the sentence
 * so the two channels cannot disagree.
 */
export function MaskLiveRegion() {
  const masked = useMasked();
  return (
    <div aria-live="polite" role="status" className="sr-only" data-testid="mask-live-region">
      {masked
        ? "Amounts masked. Dollar figures are hidden; names, dates and percentages still show."
        : "Amounts visible. Dollar figures are shown."}
    </div>
  );
}
