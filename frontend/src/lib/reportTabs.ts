// M95 P1 — the report tab strip's ids and labels, in docs/09 order.
//
// Lives OUTSIDE `src/pages/Reports.tsx` on purpose: the ⌘K command palette
// (components/layout/CommandPalette.tsx) is mounted by the shell and must list
// every report tab as a jump target without importing a lazy page module into
// the entry/palette chunk. `Reports.tsx` imports this same array, so the strip
// and the palette can never drift apart.

export const REPORT_TABS = [
  { id: "spending", label: "Spending" },
  { id: "categories", label: "Categories" },
  { id: "cashflow", label: "Cash flow" },
  { id: "networth", label: "Net worth" },
  { id: "performance", label: "Performance" },
  { id: "dividends", label: "Dividends" },
  { id: "projections", label: "Projections" },
] as const;

export type ReportTab = (typeof REPORT_TABS)[number]["id"];

export function isReportTab(value: string | null | undefined): value is ReportTab {
  return value != null && REPORT_TABS.some((tab) => tab.id === value);
}
