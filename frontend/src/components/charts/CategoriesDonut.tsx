import { toNumber } from "./format";
import { Donut } from "./Donut";

/**
 * docs/12 §Chart mapping — Categories: donut over the slices, descending.
 * Uncategorized (or Untagged, under M124's tag dimension) is its own slice.
 * Center label carries the range total (ui-contract donut center-label move).
 *
 * M124 (docs/12 §1): `items` is STRUCTURAL — `{id, name, cad_total}` — not a
 * category-shaped interface, so the same component renders the category
 * roll-up and the tag breakdown without learning what either one is. That is
 * what "the charts accept a dimension label" means: the dimension lives in
 * the caller's mapping and the title string, never in here.
 */
export function CategoriesDonut({
  items,
  total,
  centerLabel = "spent",
  title = "Category breakdown",
  onSelect,
}: {
  items: ReadonlyArray<{ id?: number | null; name: string; cad_total: string }>;
  /** Range total; defaults to the sum of slices. */
  total?: string;
  centerLabel?: string;
  /** Accessible summary title (docs/12) — "Spending by tag" under M124. */
  title?: string;
  /**
   * M124 R2 (docs/12 §2): a slice was activated. `id` is null for the
   * uncategorized/untagged bucket, which has no drill target of its own.
   * Passing this also swaps the donut's legend for focusable buttons.
   */
  onSelect?: (id: number | null, name: string) => void;
}) {
  const data = items.map((item) => ({
    name: item.name,
    value: toNumber(item.cad_total),
  }));
  const totalValue =
    total !== undefined ? toNumber(total) : data.reduce((sum, d) => sum + d.value, 0);

  return (
    <Donut
      data={data}
      centerValue={totalValue}
      centerLabel={centerLabel}
      title={title}
      onSelect={
        onSelect
          ? (index) => {
              const item = items[index];
              if (item) onSelect(item.id ?? null, item.name);
            }
          : undefined
      }
    />
  );
}
