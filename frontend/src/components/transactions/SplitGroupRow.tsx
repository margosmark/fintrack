// SplitGroupRow — the ONE grouping component docs/44 §6.3 pins for every
// surface that renders transaction rows (register, account activity, the
// Dashboard's recent list, the fuzzy search view and the partner feed).
//
// It owns everything that must not drift between those surfaces:
//
//   * grouping over `items` + `split_parents`: header at the group's FIRST
//     occurrence, children ordered `id ASC`, and FAIL OPEN — a child whose
//     header is missing (server bug) renders as an ordinary row, never a crash;
//   * the header: the parent's date / payee / account / `amount_native` total
//     and the literal badge `Split · N parts` IN PLACE of the category chip.
//     The parent's own category, tags and note are never rendered while it is
//     split (docs/44 D5 — they exist only to come back on merge);
//   * the disclosure: ONE focusable button carrying `aria-expanded` +
//     `aria-controls`, ≥44px, whose accessible name includes the badge text;
//   * the children's labelled `role="region"` (M102), which ALWAYS fetches
//     `GET /transactions/{id}/split` so the panel is the complete,
//     checksum-verified group even when a filter hid some children;
//   * the header's action cluster — Expand and Merge back — with Re-split and
//     Merge back repeated in the panel (docs/44 §12). No inline category
//     combo-box, no quick-tag popover, no Edit, no Delete: the affordances are
//     hidden wherever the server refuses them.
//
// Column layout stays with the surface (the two tables have different
// columns); everything else lives here.

import { useEffect, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import type { SplitParentLike, Transaction } from "../../api/transactions";
import { getTransactionSplit } from "../../api/transactions";
import { ApiError } from "../../api/client";
import { formatMoney } from "./format";
import { useMasked } from "../../lib/mask";

/** Columns a surface's table shows — the header renders exactly these cells. */
export type SplitColumn =
  | "select"
  | "review"
  | "date"
  | "payee"
  | "amount"
  | "category"
  | "description"
  | "account"
  | "tags"
  | "actions";

/** /transactions — 10 columns (selection + review come first, M64 order). */
export const REGISTER_SPLIT_LAYOUT: readonly SplitColumn[] = [
  "select",
  "review",
  "date",
  "payee",
  "amount",
  "category",
  "description",
  "account",
  "tags",
  "actions",
];

/** /accounts/:id activity — 8 columns (M125 adds the selection column). */
export const ACCOUNT_SPLIT_LAYOUT: readonly SplitColumn[] = [
  "select",
  "date",
  "payee",
  "amount",
  "category",
  "description",
  "tags",
  "actions",
];

/**
 * The row facts grouping needs. Generic so the partner's projection
 * (`amount_native: string | null`) groups through the SAME component — every
 * ledger row on either side of the seam carries these two fields.
 */
export interface SplitRowLike {
  id: number;
  parent_split_id: number | null;
}

export interface SplitGroup<T extends SplitRowLike = Transaction> {
  parent: SplitParentLike;
  /** Children present in THIS page, `id ASC`. */
  children: T[];
}

export type SplitUnit<T extends SplitRowLike = Transaction> =
  | { kind: "row"; key: string; tx: T }
  | { kind: "group"; key: string; group: SplitGroup<T> };

/**
 * `items` (LEDGER rows) + `split_parents` → render order.
 *
 * The header lands where the group's first child appears in the page; every
 * later child of the same group is folded into it, ordered `id ASC`.
 */
export function groupSplitRows<T extends SplitRowLike>(
  items: readonly T[],
  splitParents: readonly SplitParentLike[] | undefined,
): SplitUnit<T>[] {
  const parents = new Map<number, SplitParentLike>();
  for (const parent of splitParents ?? []) parents.set(parent.id, parent);

  const byParent = new Map<number, T[]>();
  for (const tx of items) {
    const parentId = tx.parent_split_id;
    if (parentId == null) continue;
    const bucket = byParent.get(parentId);
    if (bucket === undefined) byParent.set(parentId, [tx]);
    else bucket.push(tx);
  }
  for (const bucket of byParent.values()) bucket.sort((a, b) => a.id - b.id);

  const headed = new Set<number>();
  const units: SplitUnit<T>[] = [];
  for (const tx of items) {
    const parentId = tx.parent_split_id;
    if (parentId == null) {
      units.push({ kind: "row", key: `tx-${tx.id}`, tx });
      continue;
    }
    const parent = parents.get(parentId);
    // Fail open (docs/44 §6.3): no header in `split_parents` → ordinary row.
    if (parent === undefined) {
      units.push({ kind: "row", key: `tx-${tx.id}`, tx });
      continue;
    }
    // Header at the first occurrence; the rest of the group folds into it.
    if (headed.has(parentId)) continue;
    headed.add(parentId);
    units.push({
      kind: "group",
      key: `split-${parentId}`,
      group: { parent, children: byParent.get(parentId) ?? [] },
    });
  }
  return units;
}

/** The literal badge, in place of the category chip (docs/44 §6.3). */
export function SplitBadge({ childCount }: { childCount: number }): ReactNode {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
      Split · {childCount} parts
    </span>
  );
}

/**
 * M95's two-stage destructive action (docs/09 §Destructive actions) for
 * merge: stage 1 arms the button and sends NOTHING, stage 2 fires the DELETE.
 * The sr-only context keeps two groups' buttons distinguishable by name.
 */
export function MergeBackButton({
  context,
  pending = false,
  onMerge,
}: {
  /** Disambiguates this button from every other "Merge back" on the page. */
  context: string;
  pending?: boolean;
  onMerge: () => void;
}): ReactNode {
  const [armed, setArmed] = useArmed(5000);
  return (
    <button
      type="button"
      disabled={pending}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onMerge();
        } else {
          setArmed(true);
        }
      }}
      className="inline-flex min-h-11 items-center rounded-lg border border-danger/40 px-2.5 text-xs text-danger outline-none transition-colors hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-50"
    >
      {armed ? "Confirm merge?" : "Merge back"}
      {/* A comma, not a leading space: the accessible-name algorithm trims
          each node's outer whitespace, which would glue the two halves. */}
      <span className="sr-only">{`, ${context}`}</span>
    </button>
  );
}

/** Arms once, self-disarms after `ms` — nothing is dispatched while armed. */
function useArmed(ms: number): [boolean, (next: boolean) => void] {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return undefined;
    const timer = window.setTimeout(() => setArmed(false), ms);
    return () => window.clearTimeout(timer);
  }, [armed, ms]);
  return [armed, setArmed];
}

export interface SplitGroupSelection {
  isSelected: (id: number) => boolean;
  /** The header checkbox: toggle every child of this group present in the page. */
  onToggleGroup: (ids: number[]) => void;
}

export interface SplitGroupRowProps<T extends SplitRowLike = Transaction> {
  group: SplitGroup<T>;
  /** The surface's column order — TABLE variant only, one cell per entry. */
  layout?: readonly SplitColumn[];
  /** `table` = <tr> rows; `list` = <li>; `div` = <div> (partner feed). */
  variant: "table" | "list" | "div";
  expanded: boolean;
  onToggleExpand: () => void;
  /** The surface's ordinary row markup — children are rendered with it. */
  renderRow: (tx: T, ctx: { child: boolean }) => ReactNode;
  /** `local` never calls the API (the partner has no split route, docs/43). */
  expandMode?: "fetch" | "local";
  selection?: SplitGroupSelection;
  merge?: { pending: boolean; onMerge: () => void };
  resplit?: { onResplit: (children: T[]) => void };
  accountName?: (accountId: number) => string;
  /** Vault resolution for the parent (it is not in `items`, so not automatic). */
  resolved?: { payee?: string; locked?: boolean };
}

export function SplitGroupRow<T extends SplitRowLike>({
  group,
  layout,
  variant,
  expanded,
  onToggleExpand,
  renderRow,
  expandMode = "fetch",
  selection,
  merge,
  resplit,
  accountName,
  resolved,
}: SplitGroupRowProps<T>) {
  // M140: re-render on a mask flip — the formatters below read the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();

  const parent = group.parent;
  const childCount = parent.child_count;
  const uid = useId().replace(/:/g, "");
  const regionId = `split-parts-${uid}`;
  const headerId = `split-header-${uid}`;
  const presentIds = useMemo(() => group.children.map((child) => child.id), [group.children]);

  // The drilldown is ALWAYS fetched on expand (docs/44 §6.3): the panel must
  // be complete and checksum-verified even when a filter hid some children.
  const detailQuery = useQuery({
    queryKey: ["transactions", "split", parent.id],
    queryFn: () => getTransactionSplit(parent.id),
    enabled: expandMode === "fetch" && expanded,
    retry: false,
    staleTime: 0,
  });
  const detail = detailQuery.data;

  const panelChildren = useMemo(() => {
    // `GET /transactions/{id}/split` is the OWNER route — `expandMode="local"`
    // (the partner feed) never runs it, so drilldown children are `T` here.
    const source: T[] =
      detail === undefined ? group.children : (detail.children as unknown as T[]);
    return [...source].sort((a, b) => a.id - b.id);
  }, [detail, group.children]);

  const payee = resolved?.payee ?? parent.normalized_payee;
  const amountText =
    parent.amount_native === null
      ? "🔒" // partner privacy mode (docs/43 §9) — the header never leaks.
      : resolved?.locked === true
        ? "🔒"
        : formatMoney(parent.amount_native, parent.currency);

  const disclosure = (
    <button
      type="button"
      id={headerId}
      aria-expanded={expanded}
      aria-controls={regionId}
      onClick={onToggleExpand}
      className="inline-flex min-h-11 w-full items-center gap-1.5 rounded-lg px-1 text-left font-medium outline-none transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
    >
      <ChevronRight
        aria-hidden
        className={`size-4 shrink-0 transition-transform motion-reduce:transition-none ${
          expanded ? "rotate-90" : ""
        }`}
      />
      <span className="min-w-0 truncate">{payee}</span>
      {/* The badge (and the statement facts) ride in the accessible name. */}
      <span className="sr-only">
        {`, ${parent.date}, ${amountText}, Split · ${childCount} parts`}
      </span>
    </button>
  );

  const checkbox =
    selection === undefined ? null : (
      <span className="flex items-center justify-center min-h-11 min-w-11 md:min-h-0 md:min-w-0">
        <input
          type="checkbox"
          className="size-5 cursor-pointer accent-accent"
          aria-label={`Select the split parts of ${payee}`}
          checked={
            presentIds.length > 0 && presentIds.every((id) => selection.isSelected(id))
          }
          ref={(element) => {
            if (element !== null) {
              element.indeterminate =
                !presentIds.every((id) => selection.isSelected(id)) &&
                presentIds.some((id) => selection.isSelected(id));
            }
          }}
          onChange={() => selection.onToggleGroup(presentIds)}
        />
      </span>
    );

  const mergeControl =
    merge === undefined ? null : (
      <MergeBackButton context={payee} pending={merge.pending} onMerge={merge.onMerge} />
    );

  // The region's own tag: an <li> on a list surface so every list item stays
  // inside a list (axe `listitem`), a <div> inside a table cell.
  const RegionTag = variant === "list" ? "li" : "div";
  const region = (
    <RegionTag
      id={regionId}
      role="region"
      aria-labelledby={headerId}
      className="bg-veil/[0.02]"
    >
      {expanded && (
        <>
          {variant === "table" ? (
            <table className="w-full">
              <tbody>
                {panelChildren.map((child) => renderRow(child, { child: true }))}
              </tbody>
            </table>
          ) : (
            panelChildren.map((child) => renderRow(child, { child: true }))
          )}
          {(resplit !== undefined || merge !== undefined) && (
            <div className="flex flex-wrap items-center gap-2 border-t border-veil/[0.06] px-3 py-2">
              {resplit !== undefined && (
                <button
                  type="button"
                  onClick={() => resplit.onResplit(panelChildren)}
                  className="inline-flex min-h-11 items-center rounded-lg border border-control bg-veil/[0.04] px-2.5 text-xs text-accent outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent"
                >
                  Re-split
                  <span className="sr-only">{`, ${payee}`}</span>
                </button>
              )}
              {merge !== undefined && (
                <MergeBackButton
                  context={`${payee}, from the parts panel`}
                  pending={merge.pending}
                  onMerge={merge.onMerge}
                />
              )}
              <span className="ml-auto text-xs text-muted tabular">
                {detail === undefined
                  ? `Split · ${childCount} parts`
                  : detail.sum_ok
                    ? `Parts add to ${detail.split_checksum}`
                    : `Parts do not add to ${detail.split_checksum}`}
              </span>
            </div>
          )}
          {detailQuery.isError && (
            <p role="alert" className="px-3 py-2 text-xs text-danger">
              {/* A server envelope prints `detail` verbatim plus its code —
                  the page keeps its own sentence only for a transport error. */}
              {detailQuery.error instanceof ApiError
                ? `${detailQuery.error.message} (${detailQuery.error.code ?? "error"})`
                : "Could not load the complete split."}
            </p>
          )}
        </>
      )}
    </RegionTag>
  );

  if (variant === "table") {
    const cells = layout ?? [];
    return (
      <>
        <tr className="border-b border-veil/[0.05] bg-veil/[0.03] transition-colors">
          {cells.map((column) => {
            switch (column) {
              case "select":
                return (
                  <td key={column} className="px-1 py-2">
                    {checkbox}
                  </td>
                );
              case "review":
                return <td key={column} className="px-1 py-2" />;
              case "date":
                return (
                  <td key={column} className="whitespace-nowrap px-3 py-2 tabular">
                    {parent.date}
                  </td>
                );
              case "payee":
                return (
                  <td key={column} className="px-3 py-2 font-medium">
                    {disclosure}
                  </td>
                );
              case "amount":
                return (
                  <td
                    key={column}
                    className="whitespace-nowrap px-3 py-2 text-right font-medium"
                  >
                    <span
                      className={`tabular ${
                        amountText.startsWith("-") ? "text-danger" : ""
                      }`}
                    >
                      {amountText}
                    </span>
                  </td>
                );
              case "category":
                return (
                  <td key={column} className="px-3 py-2">
                    <SplitBadge childCount={childCount} />
                  </td>
                );
              case "description":
                return (
                  <td key={column} className="max-w-64 truncate px-3 py-2 text-muted">
                    {parent.raw_description}
                  </td>
                );
              case "account":
                return (
                  <td key={column} className="px-3 py-2 text-muted">
                    {accountName?.(parent.account_id) ?? `#${parent.account_id}`}
                  </td>
                );
              case "tags":
                // The parent's tags are inert while split (docs/44 D5).
                return <td key={column} className="px-3 py-2" />;
              case "actions":
                return (
                  <td key={column} className="whitespace-nowrap px-3 py-2 text-right">
                    {mergeControl}
                  </td>
                );
            }
          })}
        </tr>
        {/* The panel row stays in the DOM so `aria-controls` always resolves;
            collapsed it carries no children at all (never a hidden duplicate). */}
        <tr hidden={!expanded}>
          <td colSpan={cells.length} className="p-0">
            {region}
          </td>
        </tr>
      </>
    );
  }

  // List surfaces: the header and the region are TWO sibling blocks — on a
  // list that keeps every direct child an <li> (axe `list`), and the rows
  // nest inside the region <li> exactly like a real sub-list.
  const Tag = variant === "list" ? "li" : "div";
  return (
    <>
      <Tag className="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm transition-colors hover:bg-veil/[0.03]">
        {checkbox}
        {disclosure}
        <span className="text-xs text-muted tabular">{parent.date}</span>
        <SplitBadge childCount={childCount} />
        <span className={`ml-auto tabular ${amountText.startsWith("-") ? "text-danger" : ""}`}>
          {amountText}
        </span>
        {mergeControl}
      </Tag>
      {region}
    </>
  );
}
