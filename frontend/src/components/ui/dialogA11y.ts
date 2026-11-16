// Dialog focus management (docs/09 "Keyboard contract", WCAG 2.4.3 / 3.2.1).
//
// One implementation, every surface-level dialog:
//   - on open, focus moves INTO the dialog (its container, so Tab then walks
//     the fields in DOM order from the top — not from whatever control
//     happened to be focusable first);
//   - Escape closes it from anywhere in the document, not only while focus
//     happens to sit inside;
//   - on close, focus returns to the element that opened it.
//
// Deliberately NOT a focus trap: `aria-modal="true"` tells assistive tech the
// rest is inert, and the keyboard contract explicitly requires that Tab always
// advances (no keyboard traps). A sighted keyboard user may tab out of the
// dialog, and that is intended.

import { useEffect, useRef } from "react";
import type { RefObject } from "react";

/** Focusable elements in DOM order, skipping hidden ones. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getClientRects().length > 0,
  );
}

/** How long to wait for a portal-mounted dialog node before giving up. */
const FOCUS_MOUNT_TIMEOUT_MS = 2_000;
/** Poll interval while waiting for that node to attach. */
const FOCUS_MOUNT_RETRY_MS = 32;

/**
 * Move focus into `ref`'s element while `open`, and hand it back to the opener
 * when it closes. The dialog container should carry `tabIndex={-1}` so it can
 * receive focus itself.
 *
 * A surface that mounts its portal one commit later than this component —
 * `ModalPortal` renders `null` and attaches `document.body` from its own
 * effect — leaves `ref.current` null on the pass where `open` flips true, and
 * the deps never change again, so a single `node?.focus()` would silently do
 * nothing: the dialog opens with focus still on the page behind it (WCAG
 * 2.4.3). Keep looking for the node until it attaches, bounded so a surface
 * that never mounts one cannot spin a rAF loop forever.
 */
export function useDialogFocus<T extends HTMLElement>(
  ref: RefObject<T | null>,
  open = true,
): void {
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    opener.current = (document.activeElement as HTMLElement | null) ?? null;
    let cancelled = false;
    let timer = 0;
    const deadline = Date.now() + FOCUS_MOUNT_TIMEOUT_MS;

    // Focus the CONTAINER (tabIndex={-1}), not the first control: Tab then
    // walks the dialog from the top in DOM order.
    const focusNode = (): boolean => {
      const node = ref.current;
      if (!node) return false;
      node.focus();
      return true;
    };
    const waitForMount = () => {
      timer = window.setTimeout(() => {
        if (cancelled) return;
        if (!focusNode() && Date.now() < deadline) waitForMount();
      }, FOCUS_MOUNT_RETRY_MS);
    };
    if (!focusNode()) waitForMount();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      const target = opener.current;
      opener.current = null;
      if (target && target.isConnected) target.focus();
    };
  }, [ref, open]);
}

/**
 * Escape anywhere in the document closes the surface.
 *
 * The listener is registered ONCE per `enabled`, never per render: `onClose`
 * is reached through a ref, so a caller may pass a fresh inline arrow on every
 * render without the effect tearing down and re-adding a `window` listener
 * each time. That re-registration was not cosmetic — removing a listener from
 * inside a running keydown dispatch deletes it before the dispatcher reaches
 * it, so an earlier listener's setState (a render landing mid-dispatch) could
 * silently swallow the Escape that should have closed the surface. It is
 * exactly how the M72 nav drawer stopped closing on Escape (QA F1); the
 * listener must survive whatever renders happen while the event is in flight.
 */
export function useEscapeToClose(onClose: () => void, enabled = true): void {
  const closeRef = useRef(onClose);
  // Latest-closure ref, refreshed after every commit — ahead of the
  // registration effect below so a surface mounting enabled reads the current
  // callback, never the one from its first render.
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    if (!enabled) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") closeRef.current();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}
