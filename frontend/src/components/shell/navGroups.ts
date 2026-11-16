// Navigation destinations shared by the TopBar tabs, the More panel and the
// mobile drawer (M185, docs/47 §1.1–1.2). One literal per group, so the three
// surfaces cannot drift — the rail's old single `NAV_ITEMS` set is split into
// the five artifact tabs plus the More/drawer groups, and every destination
// that used to be one rail click away stays ONE interaction away.
//
// Every destination is a real `<a href>` — zero placeholder hrefs, zero dead
// fragments (docs/09 §Sidebar nav rows' full set must remain reachable;
// pinned by tests/m68Navigation.test.tsx across tabs + panel + drawer).

import {
  ArrowLeftRight,
  Calculator,
  Calendar,
  CalendarDays,
  ChartColumn,
  ChartLine,
  CircleHelp,
  Coins,
  CreditCard,
  FileUp,
  Landmark,
  LayoutDashboard,
  ScrollText,
  PiggyBank,
  Settings as SettingsIcon,
  Sparkles,
  Tag,
  TrendingUp,
  Users,
  Wallet,
  Target,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

/** The five primary tabs the artifact's top bar draws (docs/47 §1.1). */
export const PRIMARY_TABS: readonly NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/transactions", label: "Transactions", icon: ArrowLeftRight },
  { to: "/imports", label: "Imports", icon: FileUp },
  { to: "/investments", label: "Investments", icon: TrendingUp },
  { to: "/reports", label: "Reports", icon: ChartColumn },
];

/** More/drawer group "Primary" (docs/47 §1.1). */
export const PRIMARY_GROUP: readonly NavItem[] = [
  // M109 (docs/45): the what-if surface sits directly after Reports' group.
  { to: "/forecast", label: "Forecast", icon: ChartLine },
  // M188 (docs/57): the plain-English assistant, beside the what-if surface.
  { to: "/assistant", label: "Assistant", icon: Sparkles },
  // M188 (docs/57): the Monte Carlo retirement simulator, next to Forecast.
  { to: "/retirement", label: "Retirement", icon: PiggyBank },
  // M68: Calendar sits between Reports and Taxes (docs/09 §Routes).
  { to: "/calendar", label: "Calendar", icon: CalendarDays },
  { to: "/taxes", label: "Taxes", icon: Landmark },
  { to: "/tax-projection", label: "Tax Projection", icon: Calculator },
  // M90 (docs/43 §13): the partner's read-only page — shown to everyone; a
  // user without a link gets the page's own empty state.
  { to: "/shared", label: "Shared household", icon: Users },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
  { to: "/help", label: "Help", icon: CircleHelp },
];

/** More/drawer group "Tools" (docs/47 §1.1). */
export const TOOLS_GROUP: readonly NavItem[] = [
  { to: "/currencies", label: "Currencies", icon: Coins },
  { to: "/tags", label: "Tags", icon: Tag },
  { to: "/rules", label: "Rules", icon: ScrollText },
  { to: "/budgets", label: "Budgets", icon: Wallet },
  { to: "/goals", label: "Goals", icon: Target },
  { to: "/bills", label: "Bills", icon: Calendar },
  { to: "/debts", label: "Debts", icon: CreditCard },
];

/** M89 tour targets: the surfaces the first-session coach marks point at.
 *  Resolved by route so the groups stay plain literals (docs/09 §Help). */
export const TOUR_TARGETS: Readonly<Record<string, string>> = {
  "/imports": "imports",
  "/reports": "reports",
  "/help": "help",
};
