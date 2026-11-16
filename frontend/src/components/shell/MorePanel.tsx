// MorePanel (M185, docs/47 §1.1) — the three-group destination panel behind
// the top bar's `More` button. NOT a `role="dialog"`: it is a popover whose
// destinations are plain links, so it never traps Tab and never claims
// modality. The focus contract:
//
//   * `aria-expanded` + `aria-controls="more-panel"` live on the BUTTON;
//   * focus moves IN on open (`useDialogFocus`) and returns to the button on
//     close — the hook owns both halves;
//   * Escape closes through the SHELL's existing single keydown listener (the
//     panel is its fifth consumer: palette → inbox → search → More → drawer,
//     innermost first) — never a second listener (docs/09 §Escape routing);
//   * Tab advances out of the panel without trapping; an outside pointer-down
//     closes it, like every other shell surface.
//
// Groups: Primary · Tools (gated on the currency tracker, exactly as the rail
// did) · Accounts (`All accounts` → /accounts, then one row per account with
// name · type label · own-currency balance → /accounts/{id}).

import { useEffect, useRef } from "react";
import { NavLink } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import type { Account } from "../../api/accounts";
import { TYPE_GROUPS } from "../../lib/accountGroups";
import { formatMoney } from "../../lib/format";
import { useDialogFocus } from "../ui/dialogA11y";
// M140 (docs/46): this panel prints every account's `total_value` through the
// mask-aware formatter, so it subscribes to the ambient mask itself — a ⌘⇧H
// flip re-renders these rows with bullets and no network request.
import { useMasked } from "../../lib/mask";
import { PRIMARY_GROUP, TOOLS_GROUP } from "./navGroups";
import type { NavItem } from "./navGroups";

function GroupLabel({ children }: { children: string }) {
  return (
    <p className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-widest text-muted">
      {children}
    </p>
  );
}

function Row({ item }: { item: NavItem }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      className={({ isActive }) =>
        `flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm outline-none transition-colors duration-200 hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent ${
          isActive ? "font-medium text-accent" : "text-muted"
        }`
      }
    >
      <Icon className="size-[18px] shrink-0" aria-hidden />
      <span className="truncate">{item.label}</span>
    </NavLink>
  );
}

export function MorePanel({
  open,
  onClose,
  accounts,
  toolsEnabled,
  anchorRef,
}: {
  open: boolean;
  onClose: () => void;
  /** The caller's accounts page-200 list (same query the drawer reads). */
  accounts: readonly Account[];
  /** `user.currency_tracker_enabled` — the rail's Tools gate, unchanged. */
  toolsEnabled: boolean;
  /** The `More` button, so focus can return to it on close. */
  anchorRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Focus in on open / back to the anchor on close — one hook, both halves.
  useDialogFocus(panelRef, open);
  // The rows below print money → subscribe (M140 sweep: every money surface).
  useMasked();

  // Outside pointer-down closes (Escape belongs to the shell's router).
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node | null;
      if (
        panelRef.current?.contains(target ?? null) ||
        anchorRef.current?.contains(target ?? null)
      ) {
        return;
      }
      onClose();
    }
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open, onClose, anchorRef]);

  if (!open) return null;

  // Grouped by account type (same TYPE_GROUPS the index page sections by), so
  // the panel and /accounts cannot disagree about what a "Cash" row is.
  const accountGroups = TYPE_GROUPS.map((group) => ({
    label: group.label,
    items: accounts.filter((account) => group.types.includes(account.type)),
  })).filter((group) => group.items.length > 0);

  return (
    <div
      ref={panelRef}
      id="more-panel"
      data-testid="more-panel"
      tabIndex={-1}
      className="absolute left-0 top-full z-50 mt-2 max-h-[calc(100dvh-6rem)] w-[19rem] overflow-y-auto rounded-xl border border-rule bg-surface-raised p-2 shadow-glow-sm"
    >
      <GroupLabel>Primary</GroupLabel>
      {PRIMARY_GROUP.map((item) => (
        <Row key={item.to} item={item} />
      ))}
      {toolsEnabled && (
        <>
          <GroupLabel>Tools</GroupLabel>
          {TOOLS_GROUP.map((item) => (
            <Row key={item.to} item={item} />
          ))}
        </>
      )}
      <GroupLabel>Accounts</GroupLabel>
      <NavLink
        to="/accounts"
        end
        className={({ isActive }) =>
          `flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm font-medium outline-none transition-colors duration-200 hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent ${
            isActive ? "text-accent" : "text-text"
          }`
        }
      >
        <span className="flex-1">All accounts</span>
        <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
      </NavLink>
      {accountGroups.map((group) => (
        <div key={group.label} className="mt-1">
          <p className="px-3 pb-0.5 text-[10px] font-medium uppercase tracking-wider text-muted">
            {group.label}
          </p>
          {group.items.map((account) => (
            <NavLink
              key={account.id}
              to={`/accounts/${account.id}`}
              className={({ isActive }) =>
                `flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm outline-none transition-colors duration-200 hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent ${
                  isActive ? "font-medium text-accent" : "text-muted"
                }`
              }
            >
              <span className="min-w-0 flex-1 truncate">{account.name}</span>
              <span className="shrink-0 text-[11px] text-muted">
                {formatMoney(account.total_value, account.currency)}
              </span>
            </NavLink>
          ))}
        </div>
      ))}
    </div>
  );
}
