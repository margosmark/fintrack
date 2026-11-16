// ExposureDrillPanel — the inline drill-down under one exposure row (M169,
// docs/09 §Exposure drill, docs/16 §Exposure drill).
//
// Mark's ask on the back of M167's geography map: "neat would be to drill down
// what security or fund is from that place and in the case of fund what
// holding(s) if possible." The engine already attributed every dollar through
// M19's recursive look-through — the panel is the trail the walk always had.
//
// It is an INLINE PANEL, not a page (M126's expand contract): the Exposure
// table stays the surface, the drill opens under the row you clicked, and
// nothing navigates away. One extra query — `?drill=<bucket key>` on the SAME
// `GET /portfolio/breakdown` — so a caller that never opens a drill sends no
// request at all and receives a byte-identical payload.
//
// Three honesty rules carry through from the wire (docs/16 §Exposure drill):
//   * the SERVER's total is what renders. Rows round per-row and can drift a
//     cent from the bucket; the client never re-adds them (M91's pin);
//   * every leaf names its `via` — the funds the money passed through — so a
//     Microsoft reached two funds down is never mistaken for one you hold;
//   * the estimate (M166) and the unattributed residual are ROWS, not a
//     silently-absent remainder. They carry no holding because no holding
//     produced them, and the row that speaks for them is the fund itself.

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useId, useState } from "react";

import { getBreakdown } from "../api/portfolio";
import type { BreakdownDimension, DrillBlock, DrillOther, DrillRow } from "../api/portfolio";
import { fmtCad } from "./charts/format";
import { useMasked } from "../lib/mask";

/** M149 provenance → the tiny badge the drill rows wear (docs/09 §Exposure drill).
 *  `issuer`/`vanguard` are both first-party CSVs from the fund's own issuer;
 *  `yfinance` is the top-holdings scrape, which is thinner and says so. */
export function provenanceLabel(source: string): string {
  if (source === "vanguard" || source === "issuer") return "issuer CSV";
  if (source === "yfinance") return "top holdings";
  if (source === "manual") return "manual";
  return source;
}

/** The one-line "how the money got here" string. Empty chain → no path, so
 *  no arrow — the position IS the destination. */
export function viaLabel(via: readonly string[]): string {
  return via.length > 0 ? `via ${via.join(" → ")}` : "";
}

/**
 * A drill's totals, per level. The server sends its own number; this is only
 * for the assertions a test (or a suspicious reader) wants to make in the
 * browser, and it is EXACT cents — never the float weights.
 */
export function drillTotals(rows: readonly DrillRow[], other: DrillOther | null): number {
  const cents = rows.reduce((sum, row) => sum + Math.round(Number(row.market_value_cad) * 100), 0);
  const tail = other ? Math.round(Number(other.market_value_cad) * 100) : 0;
  return (cents + tail) / 100;
}

function OtherLine({ other, noun }: { other: DrillOther; noun: string }) {
  // M140: re-render on a mask flip — the formatter below reads the ambient
  // mask at call time, so this component only has to re-render (docs/46 §D6).
  useMasked();
  return (
    <li
      data-testid="drill-other"
      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-t border-veil/[0.06] px-3 py-2 text-sm text-muted"
    >
      <span>
        Other — {other.count} more {noun} the list folds away
      </span>
      <span className="tabular">
        {fmtCad(other.market_value_cad)} · {other.weight_pct.toFixed(2)}%
      </span>
    </li>
  );
}

/** One leaf of a drill: the name, the path it took, and its slice. */
function HoldingRow({ row }: { row: DrillRow }) {
  useMasked();
  return (
    <li
      className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-3 py-1.5 pl-6 sm:pl-8"
      data-testid={`drill-holding-${row.symbol ?? row.name ?? "row"}`}
    >
      <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        {row.symbol ? (
          row.security_id !== null ? (
            <a
              href={`/securities/${row.security_id}`}
              className="font-medium text-accent hover:underline"
            >
              {row.symbol}
            </a>
          ) : (
            <span className="font-medium">{row.symbol}</span>
          )
        ) : null}
        <span className="min-w-0 truncate text-muted">{row.name}</span>
        {row.via.length > 0 ? (
          <span className="whitespace-nowrap text-[11px] text-muted" data-testid="drill-via">
            {viaLabel(row.via)}
          </span>
        ) : null}
        {row.provenance_source ? (
          <span
            className="whitespace-nowrap text-[10px] uppercase tracking-widest text-muted"
            title={
              row.provenance_source === "yfinance"
                ? "Weights from a provider's published top-holdings list — a slice of the fund, not all of it."
                : "Weights from a first-party holdings file published by the fund's own issuer."
            }
          >
            {provenanceLabel(row.provenance_source)}
          </span>
        ) : null}
        {row.estimated ? (
          <span
            className="whitespace-nowrap text-[10px] uppercase tracking-widest text-muted"
            title="Allocated from this fund's own published sector mix, not a per-holding figure."
            data-testid="drill-estimated"
          >
            est
          </span>
        ) : null}
        {row.unattributed ? (
          <span
            className="whitespace-nowrap text-[10px] uppercase tracking-widest text-muted"
            title="The look-through could not place this slice in a named holding."
            data-testid="drill-unattributed"
          >
            not placed
          </span>
        ) : null}
      </span>
      <span className="flex shrink-0 items-baseline gap-3 tabular">
        {/* M139: values mask, percentages stay — the ambient formatter does it. */}
        <span>{fmtCad(row.market_value_cad)}</span>
        <span className="text-muted">{row.weight_pct.toFixed(2)}%</span>
      </span>
    </li>
  );
}

/** One top-level contributor: the position, and — for a fund — its leaves. */
function ContributionRow({ row, noun }: { row: DrillRow; noun: string }) {
  useMasked();
  const [open, setOpen] = useState(false);
  const holdingsId = useId();
  const holdings = row.holdings ?? [];
  const hasHoldings = holdings.length > 0 || Boolean(row.holdings_other);

  return (
    <li
      className="border-b border-veil/[0.05] last:border-b-0"
      data-testid={`drill-contribution-${row.symbol ?? row.name ?? "row"}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-3 py-2">
        <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          {row.symbol ? (
            row.security_id !== null ? (
              <a
                href={`/securities/${row.security_id}`}
                className="font-medium text-accent hover:underline"
              >
                {row.symbol}
              </a>
            ) : (
              <span className="font-medium">{row.symbol}</span>
            )
          ) : null}
          <span className="min-w-0 truncate text-muted">{row.name}</span>
          {/* A fund with no rows has nothing to drill into — say so rather
              than leaving a chevron that opens a hole. */}
          {row.position_kind === "fund" && !hasHoldings ? (
            <span
              className="whitespace-nowrap text-[11px] text-muted"
              title="This fund has no imported holdings, so there is nothing under it to name yet."
            >
              no holdings imported
            </span>
          ) : null}
          {row.account_count > 1 ? (
            <span className="whitespace-nowrap text-[11px] text-muted">
              {row.account_count} accounts
            </span>
          ) : null}
          {row.estimated ? (
            <span
              className="whitespace-nowrap text-[10px] uppercase tracking-widest text-muted"
              title={`${fmtCad(row.estimated_cad ?? "0")} of this position is allocated from its own published sector mix.`}
              data-testid="drill-estimated"
            >
              est
            </span>
          ) : null}
          {row.unattributed ? (
            <span
              className="whitespace-nowrap text-[10px] uppercase tracking-widest text-muted"
              title={`${fmtCad(row.unattributed_cad ?? "0")} of this position could not be placed in a named holding.`}
              data-testid="drill-unattributed"
            >
              not placed
            </span>
          ) : null}
        </span>
        <span className="flex shrink-0 items-baseline gap-3 tabular">
          <span>{fmtCad(row.market_value_cad)}</span>
          <span className="text-muted">{row.weight_pct.toFixed(2)}%</span>
          {hasHoldings ? (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              /* Only reference the panel while it exists — an `aria-controls`
                 idref with no element behind it is invalid ARIA (axe's
                 `aria-valid-attr-value`), and a collapsed row controls nothing. */
              aria-controls={open ? holdingsId : undefined}
              aria-label={
                open
                  ? `Hide ${row.symbol ?? row.name ?? noun} holdings`
                  : `Show ${row.symbol ?? row.name ?? noun} holdings`
              }
              className="cursor-pointer rounded p-1 text-muted outline-none transition-colors hover:text-accent focus-visible:ring-2 focus-visible:ring-accent"
            >
              {open ? (
                <ChevronDown className="size-3.5" aria-hidden />
              ) : (
                <ChevronRight className="size-3.5" aria-hidden />
              )}
            </button>
          ) : null}
        </span>
      </div>
      {open && hasHoldings ? (
        <ul id={holdingsId} className="border-t border-veil/[0.05] bg-veil/[0.02]">
          {holdings.map((holding, index) => (
            <HoldingRow key={`${holding.symbol ?? ""}-${index}`} row={holding} />
          ))}
          {row.holdings_other ? <OtherLine other={row.holdings_other} noun="holdings" /> : null}
        </ul>
      ) : null}
    </li>
  );
}

export interface ExposureDrillPanelProps {
  /** The bucket's exact `key` — what `?drill=` carries. */
  bucketKey: string;
  dimension: BreakdownDimension;
  accountId?: number;
}

/**
 * Fetches and renders one bucket's attribution. Mount it INSIDE the exposure
 * table under the row it describes; it is a panel, not a sibling surface.
 */
export function ExposureDrillPanel({ bucketKey, dimension, accountId }: ExposureDrillPanelProps) {
  useMasked();
  const noun = dimension === "company" ? "companies" : "holdings";

  const drill = useQuery({
    queryKey: ["breakdown", dimension, accountId, "drill", bucketKey],
    queryFn: () => getBreakdown(dimension, accountId, bucketKey),
    staleTime: 60_000,
  });

  if (drill.isPending) {
    return (
      <p className="px-3 py-3 text-sm text-muted" role="status">
        Tracing what {bucketKey} is made of…
      </p>
    );
  }
  if (drill.isError) {
    return (
      <p className="px-3 py-3 text-sm text-danger" role="alert">
        {drill.error.message}
      </p>
    );
  }

  const block: DrillBlock | undefined = drill.data.drill;
  if (!block) {
    return (
      <p className="px-3 py-3 text-sm text-muted" role="status">
        No attribution trail for {bucketKey}.
      </p>
    );
  }

  return (
    <div className="border-t border-veil/[0.06] bg-veil/[0.02] py-1" data-testid="exposure-drill">
      <p className="px-3 pt-1 text-[11px] uppercase tracking-widest text-muted">
        What {bucketKey} is made of — {fmtCad(block.contributions_cad)} of exposure
      </p>
      {block.contributions.length === 0 ? (
        <p className="px-3 py-2 text-sm text-muted">
          Nothing in this bucket could be traced to a position.
        </p>
      ) : (
        <ul>
          {block.contributions.map((row, index) => (
            <ContributionRow
              key={`${row.symbol ?? ""}-${index}`}
              row={row}
              noun={noun}
            />
          ))}
          {block.other ? <OtherLine other={block.other} noun={noun} /> : null}
        </ul>
      )}
      {/* The walk's own stop, disclosed — not a client-side cap. */}
      <p className="px-3 py-2 text-xs text-muted">
        Weights follow the look-through as far as {block.depth_cap} levels of funds; anything
        deeper stays inside its fund and is not split further.
      </p>
    </div>
  );
}
