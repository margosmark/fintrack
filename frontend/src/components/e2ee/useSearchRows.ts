/**
 * Full committed-row fetch + resolution — the E2EE/OFFLINE substrate
 * (docs/33 §Server-first computing, M137).
 *
 * M137 moved the three search surfaces (⌘K `/search`, `/transactions?q=`,
 * `/transactions/similar`) to server-scored queries, so this hook is no
 * longer their data path. It STAYS as the fallback substrate P2-5 retires:
 * `fetchAllTransactions` still feeds the report loaders that compute in the
 * browser (`api/reports.ts`), and the hook itself pages through
 * GET /transactions (which already enforces user scope + docs/03
 * committed-only staging and returns both cipher and plaintext fields),
 * resolving rows through the mixed-mode display hook — v0 legacy rows
 * resolve without a vault, cipher rows need an unlocked vault and otherwise
 * resolve to a locked marker.
 *
 * Query key sits under the ["transactions"] prefix so every ledger mutation
 * invalidation refreshes the cache too.
 */

import { useQuery } from "@tanstack/react-query";
import { listTransactions, type Transaction } from "../../api/transactions";
import { useResolvedTransactions } from "./useResolved";

const PAGE_SIZE = 200; // server max page size (deps.get_pagination)

/**
 * Identity-stable empty rows. `query.data ?? []` would allocate a fresh array
 * on every render while the query has no data — which is the *normal* state of
 * this query, since it stays disabled until the search popover opens. The
 * consumer's resolve effect keys on `items` identity (`useResolved`'s
 * `[items, tick]`) and answers with `setResolved(new Map())`, so a per-render
 * array makes render → effect → setState → render run forever: an unbounded
 * re-render loop that pins the renderer and wedges keyboard input. Keep the
 * fallback a module constant.
 */
const EMPTY_ROWS: Transaction[] = [];

export async function fetchAllTransactions(): Promise<Transaction[]> {
  const all: Transaction[] = [];
  let offset = 0;
  for (;;) {
    const page = await listTransactions({}, { limit: PAGE_SIZE, offset });
    all.push(...page.items);
    offset += page.items.length;
    // No row cap: docs/12's identity rule — the client must fetch the same
    // population the server computes over (a 10k cut truncated both the
    // search substrate and every report input fed from here).
    if (page.items.length === 0 || offset >= page.total) break;
  }
  return all;
}

export interface SearchRows {
  rows: Transaction[];
  /** Resolved display values keyed by row id (payee/raw/amount/locked). */
  resolved: ReturnType<typeof useResolvedTransactions>;
  /** True only while the fetch this caller enabled is still running. */
  isPending: boolean;
  isError: boolean;
  error: unknown;
}

export function useSearchRows(enabled: boolean): SearchRows {
  const query = useQuery({
    queryKey: ["transactions", "all-rows"],
    queryFn: fetchAllTransactions,
    enabled,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const rows = query.data ?? EMPTY_ROWS;
  const resolved = useResolvedTransactions(rows);
  return {
    rows,
    resolved,
    isPending: enabled && (query.isPending || query.data === undefined),
    isError: query.isError,
    error: query.error,
  };
}
