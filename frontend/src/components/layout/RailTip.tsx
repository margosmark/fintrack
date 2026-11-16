// RailTip — the collapsed rail's icon-label bubble (M100, docs/09
// §Tooltips & in-app help "rail label bubble").
//
// WHY THIS IS NOT `HelpHint`: HelpHint is the app's one INLINE EXPLANATION
// pattern — a `?` button whose accessible name is "What does this mean?" and
// whose content rides the description. The collapsed rail has the opposite
// problem: the icon has NO visible text at all, so what the bubble shows is
// the control's NAME ("Transactions"), not an explanation of it. Wiring that
// to `aria-describedby` would announce the label twice (name + identical
// description), which is exactly what HelpHint's own doc says to avoid. So:
//
//   * the trigger carries `aria-label = destination` (== the expanded row's
//     visible label — pinned by `tests/m100AccountsNav.test.tsx`);
//   * the bubble is the VISUAL twin of that name and is deliberately NOT
//     `aria-describedby`-linked — nothing is conveyed by hover that the
//     accessibility tree does not already carry. It renders only while open,
//     so no hidden copy of the label ever sits in the tree.
//
// The rest of HelpHint's rules are inherited verbatim: shows on **focus** as
// well as hover (never hover-only), Escape dismisses from the focused trigger
// with `stopPropagation` (the shell's Escape router must not also fire), touch
// never hover-opens (browsers fire pointerenter on tap), and the bubble is
// PORTALLED — the rail's middle column is `overflow-y-auto`, so an in-tree
// absolutely-positioned bubble would be clipped at the rail's own edge. Same
// tokens as HelpHint: opaque `bg-surface-raised` + `text-text` (≥4.5:1 both
// themes) behind `border-[rgb(var(--color-border-control))]` (≥3:1).
//
// `enabled={false}` (the expanded rail) makes this component a pass-through:
// no handlers, no portal — the expanded DOM stays byte-identical.

import { useLayoutEffect, useRef, useState } from "react";
import type {
  FocusEvent as ReactFocusEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** One named landmark at the end of `<body>` that every rail bubble bubbles
 *  into — the same axe `region` reasoning as `HelpHint`'s layer: an open
 *  bubble is real content, and content outside any landmark is an axe
 *  violation. A `region` with a name is a landmark, created once. */
const LAYER_ID = "rail-tip-layer";

function tipLayer(): HTMLElement {
  const existing = document.getElementById(LAYER_ID);
  if (existing !== null) return existing;
  const layer = document.createElement("div");
  layer.id = LAYER_ID;
  layer.setAttribute("role", "region");
  layer.setAttribute("aria-label", "Navigation destinations");
  document.body.appendChild(layer);
  return layer;
}

/** Clearance: 8px off the trigger, 8px off any viewport edge (the phone
 *  drawer / popover clearance, reused so the bubble never touches an edge). */
const EDGE = 8;

type Reason = "hover" | "focus";

/** Handlers the control receives. Spread them onto the icon-only control. */
export interface RailTipTrigger {
  onFocus?: (event: ReactFocusEvent) => void;
  onBlur?: () => void;
  onPointerEnter?: (event: ReactPointerEvent) => void;
  onPointerLeave?: () => void;
  onKeyDown?: (event: ReactKeyboardEvent) => void;
}

export function RailTip({
  label,
  enabled = true,
  children,
}: {
  /** The destination name — identical to the control's accessible name. */
  label: string;
  /** Expanded rail (visible text) opts out: no handlers, no bubble. */
  enabled?: boolean;
  /** Render prop, so no wrapper element is inserted into the rail. */
  children: (trigger: RailTipTrigger) => ReactNode;
}) {
  const [reasons, setReasons] = useState<readonly Reason[]>([]);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const open = enabled && reasons.length > 0;

  const add = (reason: Reason, target: Element) => {
    setRect(target.getBoundingClientRect());
    setReasons((prev) => (prev.includes(reason) ? prev : [...prev, reason]));
  };
  const remove = (reason: Reason) =>
    setReasons((prev) => prev.filter((r) => r !== reason));

  // Measure only while open: bubble to the RIGHT of the 76px rail, vertically
  // centred on the trigger, flipped to the left if it would clip and clamped
  // to the viewport. jsdom reports all-zero rects, which still lands
  // in-bounds — the tests assert content and position flags, never geometry.
  useLayoutEffect(() => {
    if (!open || rect === null) {
      setPos(null);
      return;
    }
    const place = () => {
      const bubble = bubbleRef.current;
      if (!bubble) return;
      const width = bubble.offsetWidth;
      const height = bubble.offsetHeight;
      let left = rect.right + EDGE;
      if (left + width > window.innerWidth - EDGE) {
        left = Math.max(EDGE, rect.left - width - EDGE);
      }
      const top = Math.max(
        EDGE,
        Math.min(
          rect.top + rect.height / 2 - height / 2,
          window.innerHeight - height - EDGE,
        ),
      );
      setPos({ top, left: Math.max(EDGE, left) });
    };
    place();
    // capture=true: the rail's own scroll column moves the trigger first, so
    // a bubbling listener would place the bubble one frame behind.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, rect]);

  const trigger: RailTipTrigger = enabled
    ? {
        onFocus: (event) => add("focus", event.currentTarget),
        onBlur: () => remove("focus"),
        onPointerEnter: (event) => {
          if (event.pointerType !== "touch") add("hover", event.currentTarget);
        },
        onPointerLeave: () => remove("hover"),
        onKeyDown: (event) => {
          if (event.key !== "Escape" || !open) return;
          // Close the bubble only — the shell's single Escape router (drawer,
          // search, palette) must not also fire for a tooltip.
          event.stopPropagation();
          setReasons([]);
        },
      }
    : {};

  return (
    <>
      {children(trigger)}
      {open &&
        createPortal(
          <div
            ref={bubbleRef}
            data-testid="rail-tip"
            role="tooltip"
            className="pointer-events-none fixed z-[70] whitespace-nowrap rounded-lg border border-[rgb(var(--color-border-control))] bg-surface-raised px-2 py-1 text-xs font-medium leading-snug text-text shadow-glow-sm"
            // First paint happens before `useLayoutEffect` measures, so park it
            // off-screen rather than flashing at the viewport corner.
            style={pos ?? { top: -9999, left: -9999 }}
          >
            {label}
          </div>,
          tipLayer(),
        )}
    </>
  );
}
