// TopBar (M185, docs/47 §1.1) — the desktop chrome that retires the left
// rail. Mark approved the artifact's surface model: brand → the five primary
// tabs → `More` → right cluster (search, ⌘K, mask switch, notifications bell,
// account menu). Routes are unchanged; only the chrome that reaches them is.
//
// Layout contract: sticky under the viewport top at z-40 (content cards never
// outrank it), ONE `role="navigation"` landmark for the primary tabs, and the
// More panel mounted in this bar's own stacking context so its popover cannot
// be occluded by page cards (the M68 S2b lesson: a panel inside a filtered
// container is trapped by that container's stacking context — this bar
// carries the filter, so the panel wins).
//
// Below 768px this bar is NOT rendered: the appbar + bottom tab bar + drawer
// are the mobile shell (docs/47 §1.2).

import { useRef } from "react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { ChevronDown, Zap } from "lucide-react";
import type { Account } from "../../api/accounts";
import { MorePanel } from "./MorePanel";
import { PRIMARY_TABS, TOUR_TARGETS } from "./navGroups";

export function TopBar({
  moreOpen,
  onMoreOpenChange,
  accounts,
  toolsEnabled,
  right,
}: {
  moreOpen: boolean;
  onMoreOpenChange: (open: boolean) => void;
  accounts: readonly Account[];
  toolsEnabled: boolean;
  /** The right cluster: search, mask switch, bell, account menu (shell state). */
  right: ReactNode;
}) {
  const moreButtonRef = useRef<HTMLButtonElement | null>(null);

  return (
    <header
      data-testid="app-topbar"
      className="sticky top-0 z-40 border-b border-rule bg-surface-raised"
    >
      <div className="mx-auto flex min-h-[60px] max-w-[1344px] flex-wrap items-center gap-x-5 gap-y-1 px-6 py-2">
        <NavLink
          to="/dashboard"
          aria-label="fin_tracker — Dashboard"
          className="flex shrink-0 items-center gap-2.5 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span className="flex size-[26px] shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-soft text-accent-ink">
            <Zap className="size-4" aria-hidden />
          </span>
          <span className="hidden text-base font-semibold tracking-tight sm:inline">
            fin_tracker
          </span>
        </NavLink>

        <nav
          aria-label="Primary"
          data-testid="topbar-tabs"
          data-tour="nav"
          className="flex min-w-0 flex-1 flex-wrap items-center gap-1"
        >
          {/* NavLink paints `aria-current="page"` on the active tab itself —
              a nested span would announce "current page" twice. */}
          {PRIMARY_TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              data-tour={TOUR_TARGETS[tab.to]}
              className={({ isActive }) =>
                `relative flex h-9 shrink-0 items-center rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                  isActive
                    ? "font-medium text-accent after:absolute after:inset-x-3 after:-bottom-[9px] after:h-0.5 after:rounded-full after:bg-accent"
                    : "text-muted hover:text-text"
                }`
              }
            >
              {tab.label}
            </NavLink>
          ))}
          <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-rule" />
          <div className="relative shrink-0">
            <button
              ref={moreButtonRef}
              type="button"
              data-testid="more-toggle"
              data-tour="more"
              aria-expanded={moreOpen}
              aria-controls="more-panel"
              onClick={() => onMoreOpenChange(!moreOpen)}
              className={`flex h-9 items-center gap-1 rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                moreOpen ? "text-accent" : "text-muted hover:text-text"
              }`}
            >
              More
              <ChevronDown className="size-3.5" aria-hidden />
            </button>
            <MorePanel
              open={moreOpen}
              onClose={() => onMoreOpenChange(false)}
              accounts={accounts}
              toolsEnabled={toolsEnabled}
              anchorRef={moreButtonRef}
            />
          </div>
        </nav>

        <div className="flex shrink-0 items-center gap-2">{right}</div>
      </div>
    </header>
  );
}
