import { useCallback, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** One named landmark at the end of `<body>` that every hint bubbles into.
 *
 *  axe's `region` rule counts EVERY rendered node outside a landmark — and the
 *  closed bubble is real content (it is the trigger's `aria-describedby`), so
 *  portalling straight to `document.body` put 12 of them on `/dashboard` and
 *  broke the zero-violation audit docs/09 pins (M97 scan). A named `region` is
 *  a landmark, the layer is created once, and because it carries no transform
 *  or overflow the bubble inside it still positions against the viewport. */
const LAYER_ID = "help-hint-layer";

function hintLayer(): HTMLElement {
  const existing = document.getElementById(LAYER_ID);
  if (existing !== null) return existing;
  const layer = document.createElement("div");
  layer.id = LAYER_ID;
  layer.setAttribute("role", "region");
  layer.setAttribute("aria-label", "Definitions");
  document.body.appendChild(layer);
  return layer;
}

/** `?` hint — the app's ONE tooltip pattern (M97, docs/09 §Tooltips & in-app
 *  help).
 *
 *  Before M97 this was a native `title=` attribute: hover-only, invisible to
 *  keyboard focus, undismissable, and unwrapped from any container that
 *  clips. It is now a real tooltip built on the SAME primitive (no dependency
 *  added — YAGNI still holds for a library):
 *
 *  - **trigger** stays a `<button>` (focusable, Enter/Space activate it like
 *    every other control) and stays keyboard-operable;
 *  - **shows on focus** — every focus, which is a superset of `:focus-visible`,
 *    so a keyboard user and a screen-reader user both get the content the
 *    moment they reach the control. Nothing anywhere is hover-only;
 *  - **Escape dismisses** (the tooltip's own keydown on the focused trigger,
 *    stopped so a dialog behind it does not close too);
 *  - **tap works** — a tap opens/toggles it, so phones and 390px layouts get
 *    the same content a mouse hover gets. Hover is tracked through POINTER
 *    events and ignored when `pointerType === "touch"`, because browsers fire
 *    pointerenter on touch too: without that guard a tap would open the
 *    tooltip on enter and immediately close it on leave;
 *  - **`aria-describedby` is wired to the tooltip element at all times**, and
 *    the element is ALWAYS in the tree — `sr-only` while closed (so assistive
 *    tech can read the explanation from the focused trigger with no hover
 *    needed), floating while open. The trigger's own accessible name is the
 *    short "What does this mean?" rather than the explanation, so the text is
 *    announced once as the description instead of twice (name + description).
 *
 *  The bubble is portalled into a named landmark layer at the end of
 *  `<body>` (`#help-hint-layer`): several hints live inside
 *  `TableScroller`/`Card` overflow contexts where an absolutely-positioned
 *  child would be clipped, and inside `framer-motion` wrappers whose
 *  `transform` would make `position: fixed` resolve against the wrapper.
 *  Placement is measured from the trigger rect (flip below, clamp to the
 *  viewport, re-place on scroll/resize) so the copy never leaves the screen at
 *  390px either.
 *
 *  The edge is written as the ARBITRARY value `border-[rgb(var(--color-
 *  border-control))]`, not the `border-control` utility: that colour is
 *  registered under `theme.extend.colors` (tailwind.config.js), so the class
 *  Tailwind generates for it is `border-border-control` — `border-control`
 *  matches no theme key, emits no rule, and every `border border-control` in
 *  the tree silently falls back to the default hairline (verified against the
 *  built CSS and a live computed style). The bubble needs the real token: it
 *  is what clears 3:1 against the page in BOTH themes (3.58:1 light /
 *  3.29:1 dark against its own fill, 3.28 / 3.49 against the surface).
 *
 *  `className` REPLACES the default `ml-1` gap (the rest of the classes are
 *  fixed): a hint that sits in a cell narrower than its own box has to give
 *  up some of that gap — the /rules Priority label passes `ml-0.5 lg:ml-1`,
 *  which keeps a visible space at 100/115/130% and still fits the 36.7px
 *  12-col track the Priority cell gets at 768/200% root font. */

type Reason = "hover" | "focus" | "tap";

/** Edge inset for the bubble: 8px off the trigger and 8px off any viewport
 *  edge, the same clearance the phone drawer/popovers use. */
const EDGE = 8;

export function HelpHint({ text, className = "ml-1" }: { text: string; className?: string }) {
  const id = useId();
  const [reasons, setReasons] = useState<readonly Reason[]>([]);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const open = reasons.length > 0;

  const add = useCallback((reason: Reason) => {
    setReasons((prev) => (prev.includes(reason) ? prev : [...prev, reason]));
  }, []);
  const remove = useCallback((reason: Reason) => {
    setReasons((prev) => prev.filter((r) => r !== reason));
  }, []);

  // Measure only while open: bubble above the trigger, flipped below when it
  // would clip off the top (or sit under the fixed nav), clamped inside the
  // viewport. jsdom reports all-zero rects, which still lands in-bounds — the
  // tests assert content, never geometry.
  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    const place = () => {
      const trigger = triggerRef.current;
      const bubble = bubbleRef.current;
      if (!trigger || !bubble) return;
      const rect = trigger.getBoundingClientRect();
      const width = bubble.offsetWidth;
      const height = bubble.offsetHeight;
      let top = rect.top - height - EDGE;
      if (top < EDGE) top = Math.min(rect.bottom + EDGE, window.innerHeight - height - EDGE);
      const left = Math.max(
        EDGE,
        Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - EDGE),
      );
      setPos({ top, left: Math.max(EDGE, left) });
    };
    place();
    // capture=true: the trigger's ancestors scroll first (that is where the
    // bubble's positioning context moves), so a bubbling listener would place
    // it one frame behind.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        aria-label="What does this mean?"
        aria-describedby={id}
        className={`${className} inline-flex size-4 cursor-help items-center justify-center rounded-full text-xs text-muted ring-1 ring-inset ring-veil/[0.12] hover:text-accent`}
        onFocus={() => add("focus")}
        onBlur={() => remove("focus")}
        onKeyDown={(event) => {
          if (event.key !== "Escape" || !open) return;
          // Close the bubble only: a tooltip inside a dialog must not take the
          // dialog down with it.
          event.stopPropagation();
          setReasons([]);
        }}
        onPointerEnter={(event) => {
          if (event.pointerType !== "touch") add("hover");
        }}
        onPointerLeave={(event) => {
          if (event.pointerType !== "touch") remove("hover");
        }}
        onClick={() => {
          // Tap contract: a click with no hover reason is a tap (touch, or a
          // click that arrived after the pointer left), and it toggles. A
          // mouse click while hovering leaves hover in charge so the bubble
          // does not close under the pointer.
          if (reasons.includes("hover")) return;
          setReasons((prev) => (prev.includes("tap") ? prev.filter((r) => r !== "tap") : [...prev, "tap"]));
        }}
      >
        ?
      </button>
      {createPortal(
        <div
          id={id}
          ref={bubbleRef}
          role="tooltip"
          className={
            open
              ? "fixed z-[70] max-w-[min(20rem,calc(100vw-1.5rem))] whitespace-normal rounded-lg border border-[rgb(var(--color-border-control))] bg-surface-raised px-2.5 py-1.5 text-xs leading-snug text-text shadow-glow-sm"
              : "sr-only"
          }
          style={pos ? { top: pos.top, left: pos.left } : undefined}
        >
          {text}
        </div>,
        hintLayer(),
      )}
    </>
  );
}
