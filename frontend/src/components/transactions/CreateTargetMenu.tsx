import { useEffect, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, RefObject } from "react";
import type { LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";

/**
 * The ONE create-target menu (M123, docs/09 §Create from search; reused by
 * M174 §Create from a selection).
 *
 * Both surfaces offer the same four-target family — Saved view | Rule | Bill |
 * Budget from a search, Rule | Bill from a selection — so they share this
 * component rather than two look-alike menus that drift on keyboard behaviour.
 * What differs between them is only the CRITERIA SOURCE and which targets are
 * offered; the contract below is identical on both:
 *
 *   * a real `menu` (`role="menu"` + `aria-haspopup`/`aria-expanded`/
 *     `aria-controls` on the trigger, owned by the caller), with a roving focus
 *     (arrows / Home / End / Escape / Tab-out);
 *   * opening focuses the first AVAILABLE item, never a disabled one;
 *   * `blocked` renders a focusable-but-`aria-disabled` item that prints WHY,
 *     so a keyboard or screen-reader user meets the explanation instead of a
 *     gap in the menu — and it is never a link, because a dead click is not an
 *     explanation.
 */
export interface CreateTargetItem {
  /** Stable key for the list. */
  id: string;
  label: string;
  hint: string;
  icon: LucideIcon;
  /** Present → the item deep-links to this href (the target's editor). */
  href?: string;
  /** Present → the item runs this in place (the one editor that edits inline). */
  onSelect?: () => void;
  /** Non-null → the target is unavailable, and this is the reason printed. */
  blocked?: string;
}

interface CreateTargetMenuProps {
  /** The `aria-controls` target the trigger points at. */
  id: string;
  /** The menu's accessible name — each surface names its own criteria source. */
  label: string;
  items: readonly CreateTargetItem[];
  /**
   * Bumped to re-focus the first available item while the menu is ALREADY open
   * (the ⌘K hand-off): a mount-only effect would swallow the second request.
   */
  focusNonce?: number;
  /** Where Escape/Tab-out returns focus — the trigger that opened the menu. */
  triggerRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
}

export function CreateTargetMenu({
  id,
  label,
  items,
  focusNonce = 0,
  triggerRef,
  onClose,
}: CreateTargetMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);

  // Open with focus on the first available target (menu behaviour).
  useEffect(() => {
    menuRef.current
      ?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')
      ?.focus();
  }, [focusNonce]);

  function targets(): HTMLElement[] {
    return Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>): void {
    const entries = targets();
    const current = entries.indexOf(document.activeElement as HTMLElement);
    const step = (delta: number) => {
      if (entries.length === 0) return;
      const next = entries[(Math.max(current, 0) + delta + entries.length) % entries.length];
      next?.focus();
      event.preventDefault();
    };
    switch (event.key) {
      case "ArrowDown":
        step(1);
        break;
      case "ArrowUp":
        step(-1);
        break;
      case "Home":
        entries[0]?.focus();
        event.preventDefault();
        break;
      case "End":
        entries[entries.length - 1]?.focus();
        event.preventDefault();
        break;
      case "Escape":
        onClose();
        triggerRef?.current?.focus();
        event.preventDefault();
        event.stopPropagation();
        break;
      case "Tab":
        // Let Tab leave the menu rather than trap inside it.
        onClose();
        break;
      default:
        break;
    }
  }

  const itemClass =
    "flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent";

  return (
    <div
      ref={menuRef}
      id={id}
      role="menu"
      aria-label={label}
      className="space-y-0.5"
      onKeyDown={onKeyDown}
    >
      {items.map((target) => {
        const Icon = target.icon;
        const blocked = target.blocked;
        const body = (
          <>
            <Icon className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden="true" />
            <span className="min-w-0">
              <span className="block text-sm text-text">{target.label}</span>
              <span className="block text-xs text-muted">{blocked ?? target.hint}</span>
            </span>
          </>
        );

        if (blocked !== undefined) {
          return (
            <span
              key={target.id}
              role="menuitem"
              aria-disabled="true"
              tabIndex={-1}
              className={`${itemClass} cursor-not-allowed opacity-70`}
            >
              {body}
            </span>
          );
        }
        if (target.href !== undefined) {
          return (
            <Link
              key={target.id}
              to={target.href}
              role="menuitem"
              tabIndex={-1}
              className={itemClass}
              onClick={() => onClose()}
            >
              {body}
            </Link>
          );
        }
        return (
          <button
            key={target.id}
            type="button"
            role="menuitem"
            tabIndex={-1}
            className={itemClass}
            onClick={() => target.onSelect?.()}
          >
            {body}
          </button>
        );
      })}
    </div>
  );
}
