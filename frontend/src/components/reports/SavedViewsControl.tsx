import { useState } from "react";
import type { ReportTab } from "../../lib/reportTabs";
import type { SavedReportView, SavedReportViewState } from "../../types";
import { Button } from "../ui/Button";
import { SaveViewPanel, SavedViewListPanel, newViewId } from "../ui/SavedViewPanels";

/**
 * M108 P2 (docs/09 §Saved report views) — the Reports header's two controls:
 * "Save this view" and the "Saved views" list.
 *
 * The panels themselves live in `components/ui/SavedViewPanels.tsx` so the
 * M123 "Create from search" menu reuses the same save form and the same
 * client-side list edit instead of growing a parallel copy. Storage is the
 * whole-list PATCH (docs/09): rename and delete are list edits on the client,
 * so there is one write shape and no per-preset CRUD to keep in sync with the
 * server's `saved_report_views` column.
 */

const PANEL_ID = "saved-report-views-panel";

interface SavedViewsControlProps {
  tab: ReportTab;
  tabLabel: string;
  /** Every saved preset; the list below filters to THIS tab. */
  views: SavedReportView[];
  /** The current tab's filter/grain/variant, captured verbatim on save. */
  state: SavedReportViewState;
  /** False while the tab's range is invalid — there is nothing to save. */
  canSave: boolean;
  /** Persist the whole list; rejects → the caller surfaces the error. */
  onPersist: (next: SavedReportView[]) => Promise<void>;
  /** Apply a preset to the page (tab + filter + grain). */
  onRestore: (view: SavedReportView) => void;
}

export function SavedViewsControl({
  tab,
  tabLabel,
  views,
  state,
  canSave,
  onPersist,
  onRestore,
}: SavedViewsControlProps) {
  const [panel, setPanel] = useState<null | "save" | "list">(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);

  const forTab = views.filter((view) => view.tab === tab);
  const toggle = (next: "save" | "list") => {
    setPanel((current) => (current === next ? null : next));
    if (next === "save") setTitle(`${tabLabel} ${state.from ?? ""} – ${state.to ?? ""}`.trim());
  };

  async function persist(next: SavedReportView[]): Promise<boolean> {
    setBusy(true);
    try {
      await onPersist(next);
      return true;
    } catch {
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={!canSave || busy}
        onClick={() => toggle("save")}
        aria-expanded={panel === "save"}
        aria-controls={PANEL_ID}
      >
        Save this view
      </Button>
      <Button
        variant="outline"
        size="sm"
        onClick={() => toggle("list")}
        aria-expanded={panel === "list"}
        aria-controls={PANEL_ID}
      >
        Saved views{forTab.length > 0 ? ` (${forTab.length})` : ""}
      </Button>

      {panel === "save" && (
        <SaveViewPanel
          id={PANEL_ID}
          title={title}
          onTitleChange={setTitle}
          placeholder={`e.g. ${tabLabel} ${state.from ?? ""}–${state.to ?? ""}`}
          hint={
            "Keeps this tab, its date range, granularity and chart variant. Saved to your profile, not to an account."
          }
          busy={busy}
          onSave={(trimmed) => {
            void persist([...views, { id: newViewId(), title: trimmed, tab, state }]).then((ok) => {
              if (ok) {
                setPanel(null);
                setTitle("");
              }
            });
          }}
          onCancel={() => setPanel(null)}
        />
      )}

      {panel === "list" && (
        <SavedViewListPanel
          id={PANEL_ID}
          views={forTab}
          describe={(view) =>
            [
              view.state.from && view.state.to ? `${view.state.from} – ${view.state.to}` : null,
              view.state.granularity,
              view.state.view,
              view.state.year != null ? String(view.state.year) : null,
            ]
              .filter((part): part is string => part != null && part !== "")
              .join(" · ") || "Tab only"
          }
          emptyHint="No saved views on this tab yet — set a range and press “Save this view”."
          busy={busy}
          onRestore={(view) => {
            onRestore(view);
            setPanel(null);
          }}
          onRename={(view, next) =>
            persist(
              views.map((candidate) =>
                candidate.id === view.id ? { ...candidate, title: next } : candidate,
              ),
            )
          }
          onDelete={(view) => void persist(views.filter((candidate) => candidate.id !== view.id))}
        />
      )}
    </div>
  );
}
