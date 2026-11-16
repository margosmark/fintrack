// dividend-provenance — the freshness/provenance block every dividend view
// carries (docs/07 §M184).
//
// Three questions, one shape: WHICH source produced these numbers, WHEN it last
// answered, and whether it is stale or missing. `kind` is the honesty label —
// a `market_data` source ages and can go stale, a `ledger` source is a
// recorded fact whose only failure mode is `missing`.
//
// `status` is the WORST source status and `as_of` the OLDEST source timestamp:
// a view is only as fresh as its least fresh evidence.

export type DividendProvenanceStatus = "ok" | "stale" | "missing";

export interface DividendProvenanceSource {
  key: string;
  kind: "market_data" | "ledger";
  label: string;
  provider: string | null;
  /** Date-string (a market-data source may answer with a full timestamp). */
  as_of: string | null;
  age_days: number | null;
  status: DividendProvenanceStatus;
  detail: string;
}

export interface DividendProvenance {
  status: DividendProvenanceStatus;
  as_of: string | null;
  sources: DividendProvenanceSource[];
}

/**
 * What the flat `X-Dividend-History-State` header of
 * `GET /trades/dividend-suggestions` means in this vocabulary.
 *
 * The header counts per tenant/security source; the adapter folds them into
 * ONE market-data entry so the Investments view reports its provenance with the
 * same words the report panels use. Never-successful sources are `missing`
 * (there is no evidence at all, only work in flight), a successful refresh
 * older than 24 hours is `stale`, and retained last-good history is neither.
 */
export function provenanceFromHistory(history: {
  pending: number;
  unavailable: number;
  empty: number;
  stale: number;
  as_of: string | null;
  missing?: number;
  source?: string | null;
  age_days?: number | null;
}): DividendProvenance {
  const missing = history.missing ?? 0;
  const notes: string[] = [];
  if (missing > 0)
    notes.push(
      `${missing} holding${missing === 1 ? " has" : "s have"} no provider history yet (refresh never succeeded)`,
    );
  if (history.pending > 0)
    notes.push(`${history.pending} refresh${history.pending === 1 ? " is" : "es are"} in flight`);
  if (history.unavailable > 0)
    notes.push(
      `${history.unavailable} source${history.unavailable === 1 ? "" : "s"} unreadable this time — last-good history, if any, is retained`,
    );
  if (history.empty > 0)
    notes.push(`${history.empty} source${history.empty === 1 ? "" : "s"} answered with no dividend events`);
  if (history.stale > 0)
    notes.push(`${history.stale} source${history.stale === 1 ? "" : "s"} last refreshed over 24h ago`);
  if (notes.length === 0)
    notes.push(
      history.as_of === null
        ? "No provider history in this scope yet"
        : `Provider history last refreshed ${history.as_of}`,
    );

  const status: DividendProvenanceStatus =
    missing > 0 || history.as_of === null ? "missing" : history.stale > 0 ? "stale" : "ok";
  const source: DividendProvenanceSource = {
    key: "dividend_history",
    kind: "market_data",
    label: "yfinance dividend history",
    provider: history.source ?? "yfinance",
    // The header carries a UTC timestamp; the strip shows the day it answered.
    as_of: history.as_of ? history.as_of.slice(0, 10) : null,
    age_days: history.age_days ?? null,
    status,
    detail: notes.join(". ") + ".",
  };
  return { status, as_of: source.as_of, sources: [source] };
}
