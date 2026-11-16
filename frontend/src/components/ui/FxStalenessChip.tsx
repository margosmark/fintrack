// M150 (docs/09 §FX staleness) — the chip that makes a carried-forward rate
// visible instead of silent.
//
// Every exact-date conversion resolves in two steps: the date's row, else the
// newest published row within 7 days (docs/07). That window exists for BoC's
// publication calendar — weekends and holidays have no observation — but it
// also hides a book that stopped being fetched: Wednesday's USD cash leg reads
// Tuesday's rate, nothing errors and nothing logs. The server now refreshes the
// book on a schedule; this reports whether that is actually working, for the
// currencies that price THIS tenant's money.
//
// Three states, all from `GET /fx/status` (cache-only server-side):
//   - current  → "FX · 2026-10-09 · 1d" (neutral)
//   - stale    → "FX rates stale · 9d" (pulsing danger) — past the 7-day
//                carry-forward window, where consumers stop resolving dates at
//                all rather than guessing
//   - missing  → "FX rates missing" (pulsing danger) — a pair with NO rate,
//                which is worse: those legs have no CAD figure at all
//
// It renders NOTHING while loading, on an error, and when the tenant holds no
// foreign currency: a status chip is never worth breaking a page for, and a
// tenant with no FX dependency has nothing to report.
import { useQuery } from "@tanstack/react-query";

import { getFxStatus } from "../../api/fx";
import { Badge } from "./Badge";

/** Per-pair detail for the hover title: "USD/CAD 2026-10-08 (2d) · EUR/CAD missing". */
function detailLine(pairs: { base: string; quote: string; as_of: string | null; age_days: number | null }[]): string {
  return pairs
    .map((pair) =>
      pair.as_of === null
        ? `${pair.base}/${pair.quote} missing`
        : `${pair.base}/${pair.quote} ${pair.as_of} (${pair.age_days}d)`,
    )
    .join(" · ");
}

export function FxStalenessChip() {
  const status = useQuery({
    queryKey: ["fx", "status"],
    queryFn: getFxStatus,
    staleTime: 60_000,
  });

  const data = status.data;
  if (status.isPending || status.isError || !data || data.pairs.length === 0) {
    return null;
  }

  const missing = data.pairs.filter((pair) => pair.as_of === null);
  const label =
    missing.length > 0
      ? `FX rates missing${missing.length < data.pairs.length ? ` · ${missing.map((pair) => pair.base).join(", ")}` : ""}`
      : data.stale
        ? `FX rates stale · ${data.age_days}d`
        : `FX · ${data.as_of} · ${data.age_days}d`;

  return (
    <span
      className="inline-flex"
      title={`Bank of Canada rates used for your non-CAD money — ${detailLine(data.pairs)}. Refreshed on a schedule; a rate older than 7 days is no longer carried forward.`}
    >
      <Badge tone={data.stale ? "danger" : "neutral"} pulse={data.stale}>
        {label}
      </Badge>
    </span>
  );
}
