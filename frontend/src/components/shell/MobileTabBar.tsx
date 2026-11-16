// MobileTabBar (M185, docs/47 §1.2) — the five-tab bottom bar that replaces
// MobileNav's six. THE USER-VISIBLE CONTRACT CHANGE of this wave: six tabs
// (Home · Txns · Import · Invest · Reports · Settings) become five, with `Add`
// promoted to the centre, because adding a transaction is the dominant mobile
// job and Import/Invest/Reports/Settings stay one tap away in the drawer.
// The M82 F7 pixel-constant rules survive unchanged: the touch floor is a
// physical constant (44px, it does not scale with type), and the label is
// capped at the size that still fits a 1/5 column at 360px and ellipsized,
// so every tab stays inside the viewport and hit-testable at ANY root font
// size (pinned by tests/m82FontScale.test.tsx).
//
// `Add` is a REAL control (docs/47 §1.2): it hands the caller a callback the
// shell wires to the transaction editor — never a demo stub, never a no-op.

import { NavLink } from "react-router-dom";
import {
  ArrowLeftRight,
  LayoutDashboard,
  Menu,
  Plus,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

interface Tab {
  to: string;
  label: string;
  icon: LucideIcon;
}

const FIRST: Tab = { to: "/dashboard", label: "Home", icon: LayoutDashboard };
const SECOND: Tab = {
  to: "/transactions",
  label: "Activity",
  icon: ArrowLeftRight,
};
const FOURTH: Tab = {
  to: "/investments",
  label: "Portfolio",
  icon: TrendingUp,
};

/** One route tab: icon + capped label, 44px floor, active state by colour. */
function RouteTab({ tab }: { tab: Tab }) {
  const Icon = tab.icon;
  return (
    <NavLink
      to={tab.to}
      className={({ isActive }) =>
        `flex min-h-[44px] min-w-[44px] flex-1 flex-col items-center justify-center gap-0.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          isActive ? "text-accent" : "text-muted"
        }`
      }
    >
      <Icon className="size-5 shrink-0" aria-hidden />
      <span className="block max-w-full truncate text-[length:min(0.75rem,14px)] leading-tight">
        {tab.label}
      </span>
    </NavLink>
  );
}

export function MobileTabBar({
  onAdd,
  onMore,
  moreOpen,
}: {
  /** Opens the transaction editor (shell-wired; never a no-op). */
  onAdd: () => void;
  /** Opens the navigation drawer — the same one the appbar toggle opens. */
  onMore: () => void;
  moreOpen: boolean;
}) {
  return (
    <nav
      aria-label="Mobile navigation"
      data-testid="mobile-tab-bar"
      className="mobile-bottom-nav fixed bottom-0 left-0 right-0 z-40 flex h-14 items-stretch box-content border-t border-border bg-surface pb-[env(safe-area-inset-bottom)] md:hidden"
    >
      <RouteTab tab={FIRST} />
      <RouteTab tab={SECOND} />
      {/* Centre `Add` — a real control over the CURRENT route (docs/47 §1.2). */}
      <button
        type="button"
        data-testid="tab-add"
        aria-label="Add transaction"
        onClick={onAdd}
        className="flex min-h-[44px] min-w-[44px] flex-1 flex-col items-center justify-center gap-0.5 text-xs text-accent outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <span
          className="flex size-5 shrink-0 items-center justify-center rounded-md bg-accent text-accent-ink"
          aria-hidden
        >
          <Plus className="size-4" strokeWidth={2.5} />
        </span>
        <span className="block max-w-full truncate text-[length:min(0.75rem,14px)] leading-tight">
          Add
        </span>
      </button>
      <RouteTab tab={FOURTH} />
      <button
        type="button"
        data-testid="tab-more"
        data-tour="more"
        aria-expanded={moreOpen}
        aria-controls="app-nav"
        onClick={onMore}
        className="flex min-h-[44px] min-w-[44px] flex-1 flex-col items-center justify-center gap-0.5 text-xs text-muted outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Menu className="size-5 shrink-0" aria-hidden />
        <span className="block max-w-full truncate text-[length:min(0.75rem,14px)] leading-tight">
          More
        </span>
      </button>
    </nav>
  );
}
