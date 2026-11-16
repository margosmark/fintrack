import { Download } from "lucide-react";
import { downloadCsv } from "../../api/reports";
import type { CategoryTagMatrix, ReportScope } from "../../api/reports";
import { fmtCad } from "../charts/format";
import { NoDataPanel } from "../charts/NoDataPanel";
import { ApiError } from "../../api/client";
import { useToast } from "../ui/Toast";
import { useMasked } from "../../lib/mask";

/**
 * M124 R3 (docs/12 §1) — the Category × Tag matrix: which tags each
 * category's spending carries, for the question "how much did I spend on
 * Groceries tagged vacation?".
 *
 * Rows are RAW categories, not the donut's root roll-up — Groceries is a
 * child of Food, and rolling up would erase the row the user came for. That
 * is the one deliberate difference from the chart above, and the caption
 * says so rather than leaving the reader to notice (docs/09 §honesty).
 *
 * Cells count a transaction ONCE PER TAG, so a row's cells can add up to more
 * than the row's own spend. "Other tags" absorbs everything past the top-8
 * cut so no spend is silently dropped, and "Untagged" is always the last
 * column — never truncated away (docs/09 §Hidden data).
 *
 * Accessibility: a real `<table>` with `<th scope>` on both axes and a
 * caption, inside a FOCUSABLE scroll region — at 390px the grid overflows
 * horizontally and docs/09 §Scroll regions requires every overflow box to be
 * keyboard-reachable, not a dead zone for a screen-reader or a keyboard user.
 */
export function CategoryTagMatrixPanel({
  matrix,
  from,
  to,
  scope = "own",
}: {
  matrix: CategoryTagMatrix;
  from: string;
  to: string;
  /** M179 — which population the grid sums. `household` renders the caption
   *  that names it and carries the scope into the CSV download, so the export
   *  can never describe a different population than the table (docs/08's
   *  CSV-parity obligation). */
  scope?: ReportScope;
}) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const toast = useToast();
  const columns = matrix.columns;
  // Long form -> grid. A missing (row, column) pair is a zero cell, which is
  // the truth: nothing was spent under that tag in that category.
  const cellAt = new Map<string, string>();
  for (const cell of matrix.items) {
    cellAt.set(`${cell.category_id ?? "null"}|${cell.tag_id ?? "null"}|${cell.tag_name}`, cell.cad_total);
  }
  const max = matrix.items.reduce(
    (peak, cell) => Math.max(peak, Number(cell.cad_total)),
    0,
  );

  async function handleDownload() {
    try {
      const params: Record<string, string> = { from, to, format: "csv" };
      // M159/M179: `own` stays off the wire, so a narrow export is
      // byte-identical to the pre-scope URL.
      if (scope !== "own") params.scope = scope;
      await downloadCsv(
        `/reports/category-tag-matrix?${new URLSearchParams(params)}`,
        `category-tag-matrix_${from}_${to}.csv`,
      );
      toast.success("Export ready", `category-tag-matrix_${from}_${to}.csv`);
    } catch (err) {
      toast.error("Export failed", err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <section className="mt-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Category × Tag</h2>
        <button
          type="button"
          onClick={() => void handleDownload()}
          className="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-control px-3 text-sm text-muted outline-none transition-colors hover:border-accent/70 hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Download className="size-3.5" aria-hidden />
          CSV
        </button>
      </div>

      <p className="mt-1 text-sm text-muted">
        Rows are the category on each transaction, not rolled up like the donut above — this is what
        answers &ldquo;Groceries tagged vacation?&rdquo;. Cells count a transaction once per tag, so
        a row&rsquo;s cells can add up to more than the{" "}
        <span className="font-medium tabular">{fmtCad(matrix.total_cad)}</span> spent in the range;
        only the {Math.max(0, columns.length - 2)} largest tags are columns, and anything smaller is
        in <em>Other tags</em>.
        {/* M179 — the grid above the fold can be a different population than the
            one the reader assumes, so the Owner lens says which one it summed
            (docs/33 rule 4: a degraded/differing result explains itself in
            place; never a silent weaker number). */}
        {scope === "household" ? (
          <>
            {" "}
            <span className="font-medium text-text">
              Own rows + the owner&rsquo;s allowlisted accounts (M93 scope)
            </span>{" "}
            — the same household population as the donut above, with category and tag names
            resolved from both tenants&rsquo; trees.
          </>
        ) : null}
      </p>

      {matrix.rows.length === 0 ? (
        <NoDataPanel />
      ) : (
        <div
          className="mt-3 overflow-x-auto rounded-lg border border-control"
          role="region"
          tabIndex={0}
          aria-label="Category by tag spending matrix, scrollable"
        >
          <table className="w-full min-w-[36rem] text-left text-sm">
            <caption className="sr-only">
              Spending by category and tag from {from} to {to}. Cells count a transaction once per
              tag; totals count each transaction once.
            </caption>
            <thead>
              <tr className="border-b border-control text-xs text-muted">
                <th scope="col" className="px-3 py-2 font-medium">
                  Category
                </th>
                {columns.map((column) => (
                  <th key={column.tag_name} scope="col" className="px-3 py-2 text-right font-medium">
                    {column.tag_name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.rows.map((row) => (
                <tr key={`${row.category_id ?? "null"}-${row.category_name}`} className="border-b border-veil/[0.05] last:border-b-0">
                  <th scope="row" className="px-3 py-2 font-normal text-text">
                    {row.category_name}
                  </th>
                  {columns.map((column) => {
                    const key = `${row.category_id ?? "null"}|${column.tag_id ?? "null"}|${column.tag_name}`;
                    const raw = cellAt.get(key);
                    const value = raw === undefined ? 0 : Number(raw);
                    // Heatmap tint, capped so `text-text` keeps its contrast
                    // against the accent wash (docs/09 §Colour never carries
                    // meaning alone — the number is always printed too).
                    const intensity = max > 0 && value > 0 ? 0.04 + (value / max) * 0.2 : 0;
                    return (
                      <td
                        key={key}
                        className={`px-3 py-2 text-right tabular ${value > 0 ? "text-text" : "text-muted"}`}
                        style={
                          intensity > 0
                            ? { backgroundColor: `rgb(var(--color-accent) / ${intensity.toFixed(3)})` }
                            : undefined
                        }
                      >
                        {value > 0 ? fmtCad(String(value)) : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
