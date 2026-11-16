// s3Inputs — shared input loaders the remaining client computes read.
//
// M137 P0-2/P1-2 (docs/33 §Server-first computing): the full-ledger loader
// `loadCommittedTransactions` is GONE. It existed to feed the three M48-S3
// client computes — budgets progress, subscription detection and rule
// preview — and all three now read a server route instead, so the one thing
// that made them expensive (a paginated download of EVERY committed row) has
// no callers left. The M96 recurring label moved with them, to
// `GET /budgets/recurring-summary`.
//
// What remains are the two bounded loaders the client engines that are
// still legitimately local need (the E2EE/offline fallbacks P2-5 retires,
// plus `api/sharing.ts`'s offline artifact). Cache keys mirror
// `src/api/reports.ts` on purpose: the datasets are the same rows with the
// same shapes, so the app fetches each one once per staleTime window and
// ledger invalidations under the ["categories"] prefix refresh every
// consumer. The fx key is deliberately SEPARATE from reports'
// `["fx","range",<base>]` — reports bound their range at a report's `to`,
// the offline artifact needs coverage up to `today_et()`, and two different
// windows under one key is the shape/window collision that serves whichever
// fetch ran first.

import { queryClient } from "../queryClient";
import type { RateRow } from "../lib/budgets";
import { listCategories, type Category } from "./categories";
import { getFxRange } from "./fx";
import type { Transaction } from "./transactions";

function cachedInput<T>(key: unknown[], fn: () => Promise<T>): Promise<T> {
  return queryClient.fetchQuery({ queryKey: key, queryFn: fn, staleTime: 30_000 });
}

/**
 * Every category, paginated (the server caps `limit` at 200 — one page
 * silently truncated the id->name map for >200-category users). Own key under
 * the ["categories"] prefix so pages' `["categories"]` entries stay
 * shape-compatible while prefix invalidation still reaches this one.
 */
export function loadAllCategories(): Promise<Category[]> {
  return cachedInput(["categories", "all-rows"], async () => {
    const all: Category[] = [];
    let offset = 0;
    for (;;) {
      const page = await listCategories(200, offset);
      all.push(...page.items);
      offset += page.items.length;
      if (page.items.length === 0 || offset >= page.total) break;
    }
    return all;
  });
}

/**
 * Cache-only fx rows (`GET /fx?...&fetch=false` — reporting must never
 * trigger a currency fetch, docs/12) for the currencies that can actually
 * reach the fallback path: rows with no `cad_amount` mirror priced in a
 * non-reporting currency. Ascending by date, the order `rateOn`'s
 * on-or-before binary search requires.
 */
export async function loadFallbackRates(
  transactions: readonly Transaction[],
  today: string,
  reporting = "CAD",
): Promise<Map<string, RateRow[]>> {
  const bases = [
    ...new Set(
      transactions
        .filter((t) => t.cad_amount === null && t.currency !== reporting)
        .map((t) => t.currency),
    ),
  ];
  const out = new Map<string, RateRow[]>();
  await Promise.all(
    bases.map(async (base) => {
      const items = await cachedInput(["fx", "range", base, "s3"], () =>
        getFxRange(base, reporting, "2000-01-01", today),
      );
      out.set(
        base,
        [...items]
          .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
          .map((r) => ({ date: r.date, rate: String(r.rate) })),
      );
    }),
  );
  return out;
}
