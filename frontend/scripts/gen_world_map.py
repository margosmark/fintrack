#!/usr/bin/env python3
"""Generate ``src/components/charts/worldMapData.ts`` — the bundled world
choropleth path set (M167, docs/09 §Exposure visuals).

WHY a generator and not a runtime dependency: the Exposure card's map must
ship with ZERO new chart libraries (M126's rule) and ZERO network calls at
runtime. Natural Earth's public-domain 1:110m Admin 0 Countries GeoJSON is
projected to SVG path data HERE, once, and the result is committed as a
TypeScript module that the component imports — the browser never parses
GeoJSON and never fetches anything.

Source (public domain, no attribution requirement beyond the notice below):
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson
  Natural Earth 1:110m Admin 0 Countries, version 5.x (public domain).

Regenerate:
  curl -sL -o /tmp/ne_110m.geojson <the URL above>
  python3 frontend/scripts/gen_world_map.py --geojson /tmp/ne_110m.geojson

Projection: equirectangular (lon → x, lat → y, both linear), latitude
clamped to [-60, 84] so Antarctica's sprawl and the empty polar bands do
not eat the frame. Every country is either a simplified ``<path>`` or, when
the 110m polygon is smaller than a couple of pixels (Malta, Liechtenstein,
Bahrain, …), a 2-unit dot — a country that exists must always be
representable, because a shaded-but-invisible country is a wrong number.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Latitude band of the frame (degrees). Antarctica (ADM0_A3 "ATA") and the
# French/South Antarctic lands are dropped outright: they can hold no equity
# exposure and each costs a full-width band of pixels.
LAT_MAX = 84.0
LAT_MIN = -60.0
WIDTH = 640.0

# Douglas-Peucker tolerance in viewBox units, and the two visibility floors:
# rings thinner than MIN_RING vanish into the coastline, a country whose
# largest dimension is under DOT_MAX px becomes a dot instead.
RDP_EPSILON = 0.9
MIN_RING = 0.9
DOT_MAX = 2.0
DOT_RADIUS = 1.6

# Natural Earth's own NAME is what the rest of the world spells the country;
# these are the handful where NE's spelling and the yfinance spelling that
# reaches the exposure pie differ (docs/09 §Exposure visuals).
NAME_OVERRIDES = {
    "USA": "United States",
    "GBR": "United Kingdom",
    "KOR": "South Korea",
    "CZE": "Czechia",
    "ARE": "United Arab Emirates",
}

# Extra spellings that resolve to a country without being an NE name. Keys
# are the yfinance / provider spellings seen in `fundamentals.country`,
# `holding.region` fallbacks and the static company→country map (backend
# app/services/company_country_map.py).
ALIASES = {
    "USA": ["United States of America", "US", "U.S.", "USA"],
    "GBR": ["UK", "Great Britain", "Britain"],
    "KOR": ["Korea, Republic of", "Korea", "Republic of Korea"],
    "CZE": ["Czech Republic"],
    "TUR": ["Turkiye", "Republic of Turkey"],
    "NLD": ["The Netherlands", "Holland"],
    "ARE": ["UAE", "Emirates"],
    "HKG": ["Hong Kong SAR", "Hong Kong, China"],
    "TWN": ["Taiwan, Province of China", "Chinese Taipei"],
    "SVK": ["Slovak Republic"],
    "GRC": ["Hellenic Republic"],
    "SAU": ["Kingdom of Saudi Arabia"],
    "DEU": ["Federal Republic of Germany"],
    "FRA": ["French Republic"],
    "ITA": ["Italian Republic"],
    "ESP": ["Kingdom of Spain"],
    "CHE": ["Swiss Confederation"],
    "DNK": ["Kingdom of Denmark"],
    "SWE": ["Kingdom of Sweden"],
    "NOR": ["Kingdom of Norway"],
    "JPN": ["Japan Nippon"],
    "HKG": ["Hong Kong SAR China"],
    "PRI": ["Puerto Rico (US)"],
}

# Countries whose NE polygon is smaller than the 110m resolution can show
# are forced to a dot even if a sliver polygon exists — a one-pixel sliver
# cannot be hit with a finger at 390px.
ALWAYS_DOT = {"MLT", "LIE", "BHR", "MNE"}

# Entities Natural Earth's 1:110m layer does NOT carry at all (too small to
# polygon at that scale, or absorbed into a neighbour). The exposure pie emits
# them by name — Hong Kong and Singapore are common fund holdings — so they
# are placed as hand-placed dots at their capital's coordinates rather than
# silently left unshadeable. Coordinates are plain geographic facts
# (lon, lat), not geometry: (iso3, display name, lon, lat, extra spellings).
EXTRA_POINTS = {
    "HKG": ("Hong Kong", 114.17, 22.32, ["Hong Kong SAR China"]),
    "SGP": ("Singapore", 103.82, 1.35, []),
    "MLT": ("Malta", 14.44, 35.91, []),
    "BHR": ("Bahrain", 50.55, 26.07, []),
    "LIE": ("Liechtenstein", 9.55, 47.16, []),
    "FRO": ("Faroe Islands", -6.91, 62.0, []),
}

DROP = {"ATA", "ATF"}


def normalize(name: str) -> str:
    """The one normalization both the generator and the component use."""
    out = []
    prev_space = False
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def project(lon: float, lat: float, width: float, height: float) -> tuple[float, float]:
    lat = max(LAT_MIN, min(LAT_MAX, lat))
    x = (lon + 180.0) / 360.0 * width
    y = (LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * height
    return x, y


def perpendicular_distance(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker (recursion blows the stack on 110m rings)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        a, b = points[start], points[end]
        farthest, best = -1, 0.0
        for i in range(start + 1, end):
            d = perpendicular_distance(points[i], a, b)
            if d > best:
                farthest, best = i, d
        if best > epsilon and farthest != -1:
            keep[farthest] = True
            stack.append((start, farthest))
            stack.append((farthest, end))
    return [p for p, k in zip(points, keep) if k]


def rings_of(geometry: dict) -> list[list[list[float]]]:
    """Every ring of a (Multi)Polygon, outer and holes alike."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [coords] if coords else []
    if gtype == "MultiPolygon":
        return [poly for poly in coords if poly]
    return []


def fmt(value: float) -> str:
    """Compact number: 12.0 → '12', 12.5 → '12.5'."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def build_path(rings: list[list[tuple[float, float]]]) -> str:
    parts = []
    for ring in rings:
        coords = [f"{fmt(x)},{fmt(y)}" for x, y in ring]
        parts.append("M" + "L".join(coords) + "Z")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", required=True, help="Path to ne_110m_admin_0_countries.geojson")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "src/components/charts/worldMapData.ts"),
        help="TypeScript module to write",
    )
    args = parser.parse_args()

    data = json.loads(Path(args.geojson).read_text())
    height = WIDTH * (LAT_MAX - LAT_MIN) / 360.0

    countries = []
    for feature in data["features"]:
        props = feature.get("properties") or {}
        iso3 = props.get("ADM0_A3") or props.get("ISO_A3_EH") or ""
        if iso3 in DROP or iso3 == "-99":
            continue
        geometry = feature.get("geometry") or {}
        rings: list[list[tuple[float, float]]] = []
        for polygon in rings_of(geometry):
            for ring in polygon:
                projected = [project(lon, lat, WIDTH, height) for lon, lat in ring]
                simplified = rdp(projected, RDP_EPSILON)
                if len(simplified) < 3:
                    continue
                xs = [p[0] for p in simplified]
                ys = [p[1] for p in simplified]
                if max(max(xs) - min(xs), max(ys) - min(ys)) < MIN_RING:
                    continue
                rings.append(simplified)

        bbox = None
        if rings:
            xs = [p[0] for ring in rings for p in ring]
            ys = [p[1] for ring in rings for p in ring]
            bbox = (max(xs) - min(xs), max(ys) - min(ys))

        name = NAME_OVERRIDES.get(iso3) or props.get("NAME") or props.get("ADMIN") or iso3
        dot = None
        if iso3 in ALWAYS_DOT or bbox is None or max(bbox) < DOT_MAX:
            # The label point Natural Earth already computed for this country;
            # a country with neither a label point nor a ring cannot exist.
            label_lon = float(props.get("LABEL_X") or 0.0)
            label_lat = float(props.get("LABEL_Y") or 0.0)
            cx, cy = project(label_lon, label_lat, WIDTH, height)
            dot = {"cx": round(cx, 1), "cy": round(cy, 1), "r": DOT_RADIUS}
            rings = []

        spellings = {
            str(props[key])
            for key in ("NAME", "NAME_LONG", "ADMIN", "NAME_SORT", "NAME_ALT")
            if props.get(key)
        }
        spellings.update(ALIASES.get(iso3, []))
        spellings.add(name)

        countries.append(
            {
                "iso3": iso3,
                "name": name,
                "d": build_path(rings) if rings else None,
                "dot": dot,
                "names": sorted(spellings),
            }
        )

    countries.sort(key=lambda c: (-len(c["d"] or ""), c["iso3"]))

    # Hand-placed dots (EXTRA_POINTS): merge their spellings into an existing
    # feature, or add the country outright when 110m carries no polygon for it.
    by_iso = {country["iso3"]: country for country in countries}
    for iso3, (extra_name, lon, lat, extra_aliases) in EXTRA_POINTS.items():
        spellings = {extra_name, *extra_aliases}
        existing = by_iso.get(iso3)
        if existing is not None:
            existing["names"] = sorted(set(existing["names"]) | spellings)
            if existing["d"] is None:
                existing["name"] = extra_name
            continue
        cx, cy = project(lon, lat, WIDTH, height)
        country = {
            "iso3": iso3,
            "name": extra_name,
            "d": None,
            "dot": {"cx": round(cx, 1), "cy": round(cy, 1), "r": DOT_RADIUS},
            "names": sorted(spellings),
        }
        countries.append(country)
        by_iso[iso3] = country

    aliases: dict[str, str] = {}
    for country in countries:
        for alias in country["names"]:
            key = normalize(alias)
            if key and key not in aliases:
                aliases[key] = country["iso3"]

    lines = []
    lines.append("// GENERATED FILE — do not edit by hand.")
    lines.append("//")
    lines.append("// World choropleth path set for the Exposure card's Geography view")
    lines.append("// (M167, docs/09 §Exposure visuals). Source: Natural Earth 1:110m Admin 0")
    lines.append("// Countries (public domain), projected to an equirectangular frame by")
    lines.append("// frontend/scripts/gen_world_map.py — regenerate with that script; never")
    lines.append("// patch the coordinates here.")
    lines.append("//")
    lines.append("// Coordinates are viewBox units (see WORLD_VIEWBOX). A country too small")
    lines.append("// for a 110m polygon carries `dot` instead of `d`, so it stays shadeable")
    lines.append("// and hittable rather than silently disappearing.")
    lines.append("")
    lines.append("export const WORLD_VIEWBOX = {")
    lines.append(f"  width: {fmt(WIDTH)},")
    lines.append(f"  height: {fmt(height)},")
    lines.append("} as const;")
    lines.append("")
    lines.append("export interface WorldCountryShape {")
    lines.append("  /** ISO 3166-1 alpha-3 (Natural Earth ADM0_A3). */")
    lines.append("  iso3: string;")
    lines.append("  /** Display name — the spelling the exposure pie uses. */")
    lines.append("  name: string;")
    lines.append("  /** SVG path `d` in WORLD_VIEWBOX units; null when this is a dot. */")
    lines.append("  d: string | null;")
    lines.append("  /** Dot fallback for a country below the 110m polygon floor. */")
    lines.append("  dot?: { cx: number; cy: number; r: number };")
    lines.append("}")
    lines.append("")
    lines.append("export const WORLD_COUNTRIES: readonly WorldCountryShape[] = [")
    for country in countries:
        entry = {"iso3": country["iso3"], "name": country["name"], "d": country["d"]}
        if country["dot"]:
            entry["dot"] = country["dot"]
        lines.append("  " + json.dumps(entry, separators=(",", ":")) + ",")
    lines.append("];")
    lines.append("")
    lines.append("export const WORLD_COUNTRY_BY_ISO3: Readonly<Record<string, WorldCountryShape>> =")
    lines.append("  Object.freeze(Object.fromEntries(WORLD_COUNTRIES.map((c) => [c.iso3, c])));")
    lines.append("")
    lines.append("/**")
    lines.append(" * Normalized country name (and provider spelling variant) → ISO3.")
    lines.append(" * Keys use the component's normalization: lowercase, letters and digits")
    lines.append(" * only, single spaces. Covers every spelling `fundamentals.country`, the")
    lines.append(" * holdings `region` column and the static company→country map emit.")
    lines.append(" */")
    lines.append("export const WORLD_ISO3_BY_NAME: Readonly<Record<string, string>> = Object.freeze({")
    for key in sorted(aliases):
        lines.append(f"  {json.dumps(key)}: {json.dumps(aliases[key])},")
    lines.append("});")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))

    dots = sum(1 for c in countries if c["dot"])
    size = out.stat().st_size
    print(
        f"{out}: {len(countries)} countries ({dots} dots), "
        f"{len(aliases)} name aliases, {size / 1024:.1f} KiB",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
