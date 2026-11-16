// search — global nav search (docs/08, docs/33 §Server-first computing).
// M137: the transaction group comes from BOTH halves of the matcher — the
// blind-index payee_token (exact, indexed) ranked first, then the pg_trgm
// fuzzy fill — so ⌘K is one server query. `hide_encrypted` is a locked
// vault's display request; `withheld` counts what it left out.
import { api } from "./client";
// The SAME schema `GET /transactions/similar` answers with (one shape, two
// surfaces — importing it here keeps the two routes from drifting).
import type { SimilarTransaction } from "./transactions";

export type { SimilarTransaction };

export interface CategoryHit {
  id: number;
  name: string;
}

export interface AccountHit {
  id: number;
  name: string;
  type: string;
}

/** Mirrors the backend `SecurityHit` schema — kind is the SecurityKind value. */
export interface SecurityHit {
  id: number;
  symbol: string;
  name: string;
  kind: string;
  exchange: string;
}

export interface TagHit {
  id: number;
  name: string;
}

export interface SearchResults {
  query: string;
  transactions: SimilarTransaction[];
  /** M137: matches left out only because `hide_encrypted` was set (a locked
   *  vault). 0 unless asked — the payload is otherwise unchanged. */
  withheld?: number;
  categories: CategoryHit[];
  accounts: AccountHit[];
  securities: SecurityHit[];
  tags: TagHit[];
}

/**
 * GET /search — grouped metadata hits, the blind-index payee_token matches
 * (ranked first, score 1.0) and the pg_trgm fuzzy fill (docs/33 §Server-first
 * computing).
 *
 * `payeeToken` is the blind-index token the client computes when the vault
 * is unlocked: HMAC(index_key, "payee-v1" | normalize(q)). `hideEncrypted`
 * is what a LOCKED vault sends — the server then omits cipher-payee rows and
 * reports their count in `withheld`, so the lock hint can name a measured
 * number instead of guessing.
 */
export async function searchGlobal(
  q: string,
  payeeToken?: string,
  hideEncrypted = false,
  /** Window the panel renders (server caps at 10; default 5 = docs/08). */
  limit?: number,
): Promise<SearchResults> {
  const params = new URLSearchParams({ q });
  if (payeeToken !== undefined) params.set("payee_token", payeeToken);
  if (hideEncrypted) params.set("hide_encrypted", "true");
  if (limit !== undefined) params.set("limit", String(limit));
  return api<SearchResults>(`/search?${params.toString()}`);
}
