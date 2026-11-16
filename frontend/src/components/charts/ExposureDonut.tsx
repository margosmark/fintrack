import type { ExposureRow } from "../../api/portfolio";
import { useMasked } from "../../lib/mask";
import { fmtNative, toNumber } from "./format";
import { Donut } from "./Donut";
import { GlassTooltip } from "../ui/charts/ChartTooltip";

/**
 * Currency-exposure donut (docs/05): holdings grouped by listing_currency
 * across all accounts, sized by native market value. Center label carries the
 * holdings count — the only honest cross-currency total (slices are native
 * values, so summing them into one "total value" would mix currencies).
 * Rendered via the shared Donut.
 *
 * F-9 (docs/46 §D2): this component BUILDS money strings — the tooltip value
 * and the text alternative's per-currency fact — so both go through the
 * mask-aware native formatter and the component subscribes with `useMasked()`
 * (D6: a flip must re-render the surface that owns the string; the prop
 * computed here would otherwise freeze the label at its mount state).
 */
export function ExposureDonut({ rows }: { rows: ExposureRow[] }) {
  useMasked();

  const data = rows.map((row) => ({
    name: row.listing_currency,
    value: toNumber(row.total_market_value_native),
    count: row.holdings_count,
  }));
  const totalHoldings = data.reduce((sum, d) => sum + d.count, 0);

  // The text alternative's money fact. NOT the shared default — that sums the
  // native slices across denominations (see the docstring above) and, read
  // through this donut's COUNT formatter, printed that sum as raw money in the
  // aria-label while masked. The honest facts are the per-currency amounts,
  // each masked on its own: `163,260.73 CAD; 47,743.27 USD`, or bullets.
  const totalFact = data.map((d) => fmtNative(String(d.value), d.name)).join("; ");

  return (
    <Donut
      data={data}
      centerValue={totalHoldings}
      centerLabel="holdings"
      centerFormat={(n) => String(Math.round(n))}
      totalFact={totalFact}
      title="Exposure by listing currency"
      tooltip={
        <GlassTooltip formatter={(value, name) => fmtNative(String(value), name)} />
      }
    />
  );
}
