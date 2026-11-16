// The ONE global record search behind the `["search", trimmedQuery,
// vaultUnlocked]` cache key (docs/09 "Data fetching & query keys", docs/33
// §Server-first computing).
//
// AppShell's app-bar search panel and the ⌘K command palette are two
// RENDERINGS of the same `GET /search` request: same endpoint, same
// `SearchResults` payload, same limit 10, same vault-aware blind-index
// contract. They share one cache entry on purpose — a query typed in one
// surface is an instant cache hit in the other — so the queryFn lives HERE,
// exactly once. `tests/queryKeyShapes.test.ts` pins that the shared literal
// key maps to this single callee; the inline `queryFn:` in each component is
// a one-line delegation so the pin can see it.
//
// Why the vault re-reads: the key's `vaultUnlocked` term is a RENDER-TIME
// snapshot, but blind-index token generation is async — a vault can lock
// while a token is being computed. The live state (`liveVaultUnlocked`, kept
// current by each component's `useVaultUnlocked` subscription) is re-read
// after the token await and immediately before the request; if it changed or
// the vault is locked, the token is dropped and `hide_encrypted=true` is
// forced, so a locked-vault request can never carry a stale token and an
// unlocked-cache entry can never be seeded from locked-state data. The
// render-time snapshot term also keeps a lock→unlock flip conservative: the
// already-keyed `[..., false]` entry stays hidden until the rerender starts
// its own `[..., true]` query. Races pinned by
// `tests/lockedVaultSearchRace.test.tsx` (palette) and
// `tests/globalSearchQueryFn.test.ts` (this function directly).
import { searchGlobal } from "../api/search";
import type { SearchResults } from "../api/search";
import { blindIndexTokenForQuery } from "./e2ee";

/** Window both surfaces render (server caps at 10). */
export const GLOBAL_SEARCH_LIMIT = 10;

/** Mutable live-state handle — each component owns one fed by `useVaultUnlocked`. */
export interface LiveVaultState {
  current: boolean;
}

/**
 * Run the shared global search (docs/08 `GET /search`, docs/33 §Blind index).
 *
 * @param trimmedQuery   debounced, trimmed search text (the key's middle term)
 * @param vaultUnlocked  render-time snapshot that formed the cache key
 * @param liveVaultUnlocked  live vault state, re-read around the async steps
 * @param signal         React Query's abort signal for this fetch
 */
export async function fetchGlobalSearch(
  trimmedQuery: string,
  vaultUnlocked: boolean,
  liveVaultUnlocked: LiveVaultState,
  signal?: AbortSignal,
): Promise<SearchResults> {
  const unlockedBeforeToken = liveVaultUnlocked.current;
  const generatedToken = unlockedBeforeToken
    ? await blindIndexTokenForQuery(trimmedQuery)
    : undefined;
  const unlockedAfterToken = liveVaultUnlocked.current;
  const safeUnlocked =
    vaultUnlocked && unlockedBeforeToken && unlockedAfterToken;
  const payeeToken = safeUnlocked ? generatedToken : undefined;
  const unlockedBeforeRequest = liveVaultUnlocked.current;
  const hideEncrypted =
    !(safeUnlocked &&
    unlockedBeforeRequest &&
    unlockedBeforeRequest === unlockedAfterToken);
  const results = await searchGlobal(
    trimmedQuery,
    payeeToken,
    hideEncrypted,
    GLOBAL_SEARCH_LIMIT,
  );
  // An aborted fetch (the palette's lock cleanup cancels `["search"]`) and a
  // vault that locked after an unlocked request was sent must not resolve
  // into the cache as if they were a clean unlocked answer.
  if (signal?.aborted || (!liveVaultUnlocked.current && !hideEncrypted)) {
    throw new DOMException(
      "Search cancelled after vault state changed.",
      "AbortError",
    );
  }
  return results;
}
