// The shared global-search queryFn (docs/09 "Data fetching & query keys").
//
// AppShell's app-bar panel and the ⌘K palette delegate to this ONE function,
// so its vault-race contract belongs to BOTH surfaces. `queryKeyShapes`
// pins the key→callee identity at the source level; this file pins the
// behaviour of the callee itself — chiefly that the render-time
// `vaultUnlocked` snapshot (the key term) and the live state (re-read around
// the async steps) must BOTH hold before a blind-index token leaves the
// client. The component-level races are pinned separately in
// `lockedVaultSearchRace.test.tsx` (palette wiring).
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  blindIndexTokenForQuery: vi.fn(),
  searchGlobal: vi.fn(),
}));

vi.mock("../src/lib/e2ee", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/e2ee")>()),
  blindIndexTokenForQuery: mocks.blindIndexTokenForQuery,
}));
vi.mock("../src/api/search", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/search")>()),
  searchGlobal: mocks.searchGlobal,
}));

import { fetchGlobalSearch } from "../src/lib/globalSearchQuery";

const results = {
  query: "coffee",
  transactions: [],
  categories: [],
  accounts: [],
  securities: [],
  tags: [],
  withheld: 0,
};

function live(current: boolean) {
  return { current };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.blindIndexTokenForQuery.mockResolvedValue("blind-index-token");
  mocks.searchGlobal.mockResolvedValue(results);
});

describe("fetchGlobalSearch — vault-aware request contract", () => {
  it("sends the token only when snapshot AND live state agree the vault is unlocked", async () => {
    const out = await fetchGlobalSearch("coffee", true, live(true));
    expect(mocks.blindIndexTokenForQuery).toHaveBeenCalledWith("coffee");
    expect(mocks.searchGlobal).toHaveBeenCalledWith(
      "coffee",
      "blind-index-token",
      false,
      10,
    );
    expect(out).toBe(results);
  });

  it("generates no token and hides encrypted rows when the key says locked", async () => {
    await fetchGlobalSearch("coffee", false, live(false));
    expect(mocks.blindIndexTokenForQuery).not.toHaveBeenCalled();
    expect(mocks.searchGlobal).toHaveBeenCalledWith(
      "coffee",
      undefined,
      true,
      10,
    );
  });

  it("drops a token computed under a stale state when the vault locks mid-generation", async () => {
    const state = live(true);
    mocks.blindIndexTokenForQuery.mockImplementation(async () => {
      state.current = false; // the vaultchange lands while the HMAC runs
      return "blind-index-token";
    });
    await fetchGlobalSearch("coffee", true, state);
    expect(mocks.searchGlobal).toHaveBeenCalledWith(
      "coffee",
      undefined,
      true,
      10,
    );
  });

  it("never sends a token under a locked key even when the vault has since unlocked (snapshot term)", async () => {
    // A lock->unlock flip must not seed the already-keyed `[..., false]`
    // entry with unlocked data; the rerender starts its own `[..., true]` query.
    const state = live(true);
    await fetchGlobalSearch("coffee", false, state);
    expect(mocks.searchGlobal).toHaveBeenCalledWith(
      "coffee",
      undefined,
      true,
      10,
    );
  });

  it("throws AbortError instead of resolving when the vault locks after an unlocked request was sent", async () => {
    const state = live(true);
    mocks.searchGlobal.mockImplementation(async () => {
      state.current = false; // locked while the request is in flight
      return results;
    });
    await expect(fetchGlobalSearch("coffee", true, state)).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("throws AbortError when React Query aborts the fetch", async () => {
    const controller = new AbortController();
    mocks.searchGlobal.mockImplementation(async () => {
      controller.abort();
      return results;
    });
    await expect(
      fetchGlobalSearch("coffee", true, live(true), controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
  });
});
