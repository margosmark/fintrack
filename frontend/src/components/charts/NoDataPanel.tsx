/**
 * "No data in range" panel (docs/09 interaction notes, docs/12): empty or
 * zero-filled-only ranges render this instead of an empty chart — never a
 * crash. Upgraded with the shared EmptyState personality (icon + one-liner).
 */
import { ChartNoAxesColumn } from "lucide-react";
import { EmptyState } from "../ui/EmptyState";

export function NoDataPanel({ hint }: { hint?: string }) {
  return (
    <div className="glass rounded-xl">
      <EmptyState
        icon={<ChartNoAxesColumn />}
        title="No data in range"
        hint={hint ?? "Adjust the date range or import statements to populate this report."}
        preview={{ src: "/help/reports.png", alt: "The Reports page with charts for the selected range" }}
        action={{ label: "Import a statement", to: "/imports" }}
      />
    </div>
  );
}
