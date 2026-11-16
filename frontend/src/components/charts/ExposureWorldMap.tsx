// ExposureWorldMap — the Geography view's world choropleth (M167,
// docs/09 §Exposure visuals).
//
// Hand-rolled SVG on the ChartFrame family's terms: a bare <svg> wrapped in
// ONE labelled node, exactly like the Recharts wrappers, and zero new chart
// libraries — the path set is generated once from Natural Earth's public-
// domain 1:110m layer (components/charts/worldMapData.ts, regenerate with
// frontend/scripts/gen_world_map.py).
//
// The map is a PICTURE OF THE LIST, never a second source of truth:
//   * every shaded country's number is also a table row, and the readout
//     under the map prints the same name/weight/value in text;
//   * a bucket the geometry cannot place (a REGION name like "North America"
//     from the holdings column, or a country with no polygon) is listed under
//     the map rather than silently dropped — weight the map cannot draw must
//     still be weight the user can read;
//   * "no exposure" and "not in the payload" draw the same neutral land, so
//     the map never invents a zero (M147's honesty family).
//
// Interaction: activating a shaded country tells the parent, which scrolls to
// and highlights that country's row — the table stays the authoritative
// surface. Hover/focus drives a text readout (name, weight, masked-aware
// value) that doubles as the screen-reader path: every shaded country is a
// focusable, labelled control.

import { useId, useMemo, useState } from "react";
import type { KeyboardEvent, MouseEvent } from "react";

import type { BreakdownRow } from "../../api/portfolio";
import { formatCad } from "../../lib/format";
import { UNCLASSIFIED_KEY } from "../../lib/exposureFold";
import { useMasked } from "../../lib/mask";
import { useTheme } from "../../theme/ThemeProvider";
import { WEIGHT_BUCKET_LABELS, choroplethColors, weightBucket } from "./choropleth";
import { useChartPalette } from "./palette";
import { chartAriaLabel, shareFact } from "./aria";
import { WORLD_COUNTRIES, WORLD_COUNTRY_BY_ISO3, WORLD_ISO3_BY_NAME, WORLD_VIEWBOX } from "./worldMapData";

/** `United States` / `united states of america` / `U.S.` → one lookup key. */
export function normalizeCountryName(name: string): string {
  let out = "";
  let prevSpace = false;
  for (const ch of name.toLowerCase()) {
    if (ch >= "a" && ch <= "z") {
      out += ch;
      prevSpace = false;
    } else if (ch >= "0" && ch <= "9") {
      out += ch;
      prevSpace = false;
    } else if (!prevSpace) {
      out += " ";
      prevSpace = true;
    }
  }
  return out.trim();
}

/** A breakdown bucket → the ISO3 that draws it, or null when none does. */
function iso3ForRow(row: BreakdownRow): string | null {
  return WORLD_ISO3_BY_NAME[normalizeCountryName(row.key)] ?? null;
}

export interface ExposureWorldMapProps {
  /** The FULL breakdown list — the fold lives in the table, not here. */
  rows: readonly BreakdownRow[];
  /** Activating a shaded country: the parent scrolls to + highlights the row. */
  onSelect: (rowKey: string) => void;
  /** The row the table has highlighted, if any. */
  selectedKey?: string | null;
  /** Accessible summary title (docs/12 §Chart accessibility). */
  title?: string;
  /** Mobile: the map collapses behind a disclosure button (docs/09 §M82). */
  collapsibleOnMobile?: boolean;
}

export function ExposureWorldMap({
  rows,
  onSelect,
  selectedKey,
  title = "Exposure by country",
  collapsibleOnMobile = true,
}: ExposureWorldMapProps) {
  // M139: the readout prints money through the ambient formatters, so it must
  // re-render on a mask flip like every other money surface.
  useMasked();
  const palette = useChartPalette();
  const { theme } = useTheme();
  const colors = choroplethColors(palette, theme);
  const regionId = useId();
  const [open, setOpen] = useState(true);
  const [activeIso3, setActiveIso3] = useState<string | null>(null);

  /** iso3 → the bucket row that shades it. Unclassified is not a country. */
  const { byIso3, unplaceable } = useMemo(() => {
    const map = new Map<string, BreakdownRow>();
    const leftOut: BreakdownRow[] = [];
    for (const row of rows) {
      if (row.key === UNCLASSIFIED_KEY) continue;
      const iso3 = iso3ForRow(row);
      if (iso3 && WORLD_COUNTRY_BY_ISO3[iso3]) map.set(iso3, row);
      else leftOut.push(row);
    }
    return { byIso3: map, unplaceable: leftOut };
  }, [rows]);

  const selectedIso3 = useMemo(() => {
    if (!selectedKey) return null;
    const row = rows.find((candidate) => candidate.key === selectedKey);
    return row ? iso3ForRow(row) : null;
  }, [rows, selectedKey]);

  const shownIso3 = activeIso3 ?? selectedIso3;
  const shownRow = shownIso3 ? (byIso3.get(shownIso3) ?? null) : null;

  const shadedRows = [...byIso3.values()];
  const top = shadedRows.reduce<BreakdownRow | null>(
    (best, row) => (best === null || row.weight_pct > best.weight_pct ? row : best),
    null,
  );
  const shadedWeight = shadedRows.reduce((sum, row) => sum + row.weight_pct, 0);
  const summary = chartAriaLabel({
    title,
    kind: "world choropleth",
    points: byIso3.size,
    facts: [
      top ? shareFact(top.key, top.weight_pct, shadedWeight) : "",
      unplaceable.length > 0
        ? `${unplaceable.length} bucket${unplaceable.length === 1 ? "" : "s"} outside the map, listed below`
        : "",
    ],
  });

  const activate =
    (rowKey: string) =>
    (event: MouseEvent<SVGPathElement | SVGCircleElement> | KeyboardEvent<SVGPathElement | SVGCircleElement>) => {
      event.preventDefault();
      onSelect(rowKey);
    };
  const keyActivate =
    (rowKey: string) =>
    (event: KeyboardEvent<SVGPathElement | SVGCircleElement>) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelect(rowKey);
      }
    };

  const handlers = (iso3: string, rowKey: string) => ({
    onClick: activate(rowKey),
    onKeyDown: keyActivate(rowKey),
    onFocus: () => setActiveIso3(iso3),
    onBlur: () => setActiveIso3(null),
    onMouseEnter: () => setActiveIso3(iso3),
    onMouseLeave: () => setActiveIso3(null),
  });

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] uppercase tracking-widest text-muted">Where the money is</p>
        {collapsibleOnMobile ? (
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            aria-controls={regionId}
            className="rounded px-2 py-1 text-xs text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-accent lg:hidden"
          >
            {open ? "Hide map" : "Show map"}
          </button>
        ) : null}
      </div>
      {/* The collapse is a MOBILE affordance: below lg it hides, at lg and up
          the map is always there (a resize never eats the visual). */}
      <div id={regionId} className={open ? "block" : "hidden lg:block"} data-open={open}>
        <div role="group" aria-label={summary} className="mt-2">
          <svg
            viewBox={`0 0 ${WORLD_VIEWBOX.width} ${WORLD_VIEWBOX.height}`}
            className="block h-auto w-full"
          >
            {WORLD_COUNTRIES.map((country) => {
              const row = byIso3.get(country.iso3);
              const bucket = row ? weightBucket(row.weight_pct) : null;
              const interactive = row !== undefined && bucket !== null;
              const highlighted =
                interactive && (country.iso3 === activeIso3 || row.key === selectedKey);
              const fill = bucket === null ? colors.noData : colors.fills[bucket];
              const stroke = highlighted ? palette.ACCENT : colors.stroke;
              const accessibleName = row
                ? `${row.key}, ${Number(row.weight_pct).toFixed(2)}% of exposure`
                : undefined;
              const shape = country.d !== null ? { d: country.d } : country.dot;

              if (!shape) return null;

              if (country.d !== null) {
                return (
                  <path
                    key={country.iso3}
                    d={country.d}
                    data-country={country.iso3}
                    fill={fill}
                    stroke={stroke}
                    strokeWidth={highlighted ? 1.4 : 0.4}
                    cursor={interactive ? "pointer" : "default"}
                    role={interactive ? "button" : undefined}
                    tabIndex={interactive ? 0 : undefined}
                    aria-label={interactive ? accessibleName : undefined}
                    aria-hidden={interactive ? undefined : true}
                    {...(interactive ? handlers(country.iso3, row.key) : {})}
                  />
                );
              }

              const dot = country.dot;
              if (!dot) return null;
              // A dot is ~1.6 viewBox units across — smaller than a fingertip
              // at any real width. The invisible stroke halo IS the hit area
              // (a transparent stroke still takes pointer events), so the
              // target grows without drawing anything.
              return (
                <circle
                  key={country.iso3}
                  cx={dot.cx}
                  cy={dot.cy}
                  r={dot.r}
                  data-country={country.iso3}
                  fill={fill}
                  stroke={interactive ? (highlighted ? palette.ACCENT : "transparent") : stroke}
                  strokeWidth={interactive ? (highlighted ? 2 : 7) : 0.4}
                  role={interactive ? "button" : undefined}
                  tabIndex={interactive ? 0 : undefined}
                  aria-label={interactive ? accessibleName : undefined}
                  aria-hidden={interactive ? undefined : true}
                  {...(interactive ? handlers(country.iso3, row.key) : {})}
                />
              );
            })}
          </svg>
        </div>
        <ul className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
          {colors.fills.map((fill, index) => (
            <li key={WEIGHT_BUCKET_LABELS[index]} className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="inline-block size-2.5 rounded-sm"
                style={{ backgroundColor: fill }}
              />
              <span>{WEIGHT_BUCKET_LABELS[index]}</span>
            </li>
          ))}
          <li className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block size-2.5 rounded-sm ring-1 ring-inset ring-veil/40"
              style={{ backgroundColor: colors.noData }}
            />
            <span>No exposure listed</span>
          </li>
        </ul>
        {/* The map's text twin: name + weight + value, announced as it moves. */}
        <p
          role="status"
          aria-live="polite"
          className="mt-2 min-h-5 text-sm"
          data-testid="exposure-map-readout"
        >
          {shownRow ? (
            <>
              <span className="font-medium">{shownRow.key}</span>
              <span className="tabular"> — {Number(shownRow.weight_pct).toFixed(2)}%</span>
              <span className="tabular text-muted"> · {formatCad(shownRow.market_value_cad)}</span>
            </>
          ) : (
            <span className="text-muted">
              Hover, focus or tap a shaded country to read its weight — every one of them is also a
              row in the table below.
            </span>
          )}
        </p>
        {unplaceable.length > 0 ? (
          <p className="mt-1 text-xs text-muted" data-testid="exposure-map-unplaceable">
            Not on the map:{" "}
            {unplaceable
              .map((row) => `${row.key} (${Number(row.weight_pct).toFixed(2)}%)`)
              .join(", ")}{" "}
            — this bucket has no country shape.
          </p>
        ) : null}
      </div>
    </div>
  );
}
