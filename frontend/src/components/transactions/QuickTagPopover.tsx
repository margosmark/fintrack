// QuickTagPopover — the row-level "add tags" surface (M104, docs/09 §Tag UX).
//
// The whole point is interaction count: adding one tag to one row used to be
// open the editor → find the Tags row → focus the picker → type → Enter
// (4–5 interactions). Here it is click the row's `+` button → type → Enter.
//
// Shape: a non-modal popover (NOT `role="dialog"` — the dialog contract is
// for surface-level modals; this is the inline-surface family, so the nesting
// rule applies instead: Escape closes the picker's list first and the popover
// on the second press). Position is `fixed` off the trigger's rect, clamped
// horizontally so it can never leave a 390px viewport, and flipped above the
// trigger when there is not enough room below. Any scroll or resize outside
// the popover closes it, so an anchor that has moved can never go stale.

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { Tag } from "../../api/tags";
import { TagPicker } from "./TagPicker";
import { DemoReason } from "../ui/DemoLock";
import { useDialogFocus, useEscapeToClose } from "../ui/dialogA11y";

/** w-64 list + room to breathe; the horizontal clamp uses the same number. */
const POPOVER_WIDTH = 272;
/** Below this much room under the trigger the popover opens upward instead. */
const FLIP_THRESHOLD = 240;
const EDGE = 8;

/**
 * `aria-describedby` target of the picker input's padlock while the popover
 * is open (docs/47 §4). Its own id: the page's row reason is a separate
 * element and two nodes may not share one.
 */
const QUICK_TAG_REASON_ID = "quick-tag-write-demo-reason";

export interface QuickTagAnchor {
  left: number;
  top: number;
  bottom: number;
}

interface QuickTagPopoverProps {
  anchor: QuickTagAnchor;
  attached: Tag[];
  allTags: Tag[];
  creating?: boolean;
  /** Unique-per-row naming for the input (docs/09 M92 — names stay unique). */
  label?: string;
  onAttach: (tagId: number) => void;
  onCreate: (name: string) => void;
  onClose: () => void;
  /**
   * docs/47 §4 — the row surface's §4 verdict. Attaching (an option click or
   * Enter) IS the blocked write, so the picker input wears the padlock and
   * this popover renders the VISIBLE reason it binds to. The row's `+`
   * trigger itself dispatches nothing and stays unlocked (§13 ex.2).
   */
  locked?: boolean;
}

export function QuickTagPopover({
  anchor,
  attached,
  allTags,
  creating = false,
  label = "Add or create tag",
  onAttach,
  onCreate,
  onClose,
  locked = false,
}: QuickTagPopoverProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  // Focus moves into the popover on open and returns to the row's `+` button
  // when it closes — the shared helpers, so Escape/focus behave like every
  // other surface (docs/09 keyboard contract).
  useDialogFocus(ref);
  useEscapeToClose(onClose);

  // After the container takes focus, hand it to the input: opening a picker
  // with focus already in the field is what makes "click, type, Enter" two
  // interactions. Registered AFTER useDialogFocus, so it wins the race.
  useEffect(() => {
    ref.current?.querySelector<HTMLInputElement>("input")?.focus();
  }, []);

  // Close when the page moves under the anchor (scroll in ANY container —
  // capture phase catches the table's own scroller — or a resize). The
  // listener registers ONCE and reaches the handler through a ref, the same
  // shape `useEscapeToClose` uses: an inline `onClose` is a new identity on
  // every render, and re-registering inside a running dispatch is exactly how
  // the M72 drawer stopped closing on Escape (docs/09 §Escape routing).
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    function handle(event: Event): void {
      const node = ref.current;
      if (event.target instanceof Node && node?.contains(event.target)) return;
      closeRef.current();
    }
    window.addEventListener("scroll", handle, true);
    window.addEventListener("resize", handle);
    return () => {
      window.removeEventListener("scroll", handle, true);
      window.removeEventListener("resize", handle);
    };
  }, []);

  const left = Math.max(EDGE, Math.min(anchor.left, window.innerWidth - POPOVER_WIDTH - EDGE));
  const flip = window.innerHeight - anchor.bottom < FLIP_THRESHOLD;
  const style: { position: "fixed"; left: number; top?: number; bottom?: number; width: number } =
    flip
      ? { position: "fixed", left, bottom: window.innerHeight - anchor.top + EDGE, width: POPOVER_WIDTH }
      : { position: "fixed", left, top: anchor.bottom + EDGE, width: POPOVER_WIDTH };

  // Portalled to `document.body`, i.e. OUTSIDE every landmark — axe's
  // `region` rule then reports the popover's own content as unlandmarked. A
  // named `region` is the honest fix: a labelled area of the page, not a
  // dialog (no aria-modal; Escape still nests list -> popover).
  return createPortal(
    <div
      ref={ref}
      tabIndex={-1}
      role="region"
      aria-label="Tag picker"
      style={style}
      className="z-40 outline-none"
    >
      <div className="rounded-lg border border-control bg-surface p-2 shadow-xl">
        <TagPicker
          attached={attached}
          allTags={allTags}
          creating={creating}
          label={label}
          listClassName="w-full"
          inputClassName="w-full"
          locked={locked}
          hintId={locked ? QUICK_TAG_REASON_ID : undefined}
          onAttach={onAttach}
          onCreate={onCreate}
        />
        {/* docs/47 §4 (F-4): the VISIBLE reason the padlock above binds to —
            same popover, non-zero box, never sr-only. */}
        {locked && (
          <DemoReason id={QUICK_TAG_REASON_ID}>
            The demo account is read-only — tags are not saved.
          </DemoReason>
        )}
      </div>
    </div>,
    document.body,
  );
}
