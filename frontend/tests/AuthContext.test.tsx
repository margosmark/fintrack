// Cross-user query-cache leakage (frontend tenancy invariant, AGENTS.md §2
// "user_id on every query"): the React Query cache is per-browser, not
// per-user. Every cached query is scoped by the access token that fetched it,
// so ANY cached entry that survives an identity change is served to the next
// user from the wrong tenant. AuthProvider is therefore the one seam where the
// whole cache must be dropped.
//
// These tests pin that contract at the AuthProvider boundary rather than at any
// page: seed the cache as user A, change identity, and assert nothing from A
// remains reachable.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import type { User } from "../src/types";

const { restoreSession, login, register, demoLogin, logout } = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<import("../src/types").AuthResponse | null>>(),
  login: vi.fn<() => Promise<import("../src/types").AuthResponse>>(),
  register: vi.fn<() => Promise<import("../src/types").AuthResponse>>(),
  demoLogin: vi.fn<() => Promise<import("../src/types").AuthResponse>>(),
  logout: vi.fn<() => Promise<void>>(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession, login, register, demoLogin, logout };
});

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { MASK_SESSION_KEY, initMaskForSession, isMasked, setMasked } from "../src/lib/mask";

function makeUser(id: string, email: string): User {
  return {
    id,
    email,
    name: null,
    privacy_mode: false,
    currency_tracker_enabled: true,
    llm_provider: "none",
    llm_base_url: null,
    llm_model: "",
    encryption_required: false,
  };
}

const alice = makeUser("1", "alice@example.com");
const bob = makeUser("2", "bob@example.com");

const authPair = (user: User) => ({
  access_token: `token-${user.id}`,
  refresh_token: "unused",
  token_type: "bearer" as const,
  user,
});

/** A user-scoped query: its cached value belongs to whoever is signed in. */
function useScopedQuery() {
  const { user } = useAuth();
  return useQuery({
    queryKey: ["watchlist", user?.id ?? "anonymous"],
    queryFn: async () => `secrets-for-${user?.id ?? "anonymous"}`,
    staleTime: Infinity,
  });
}

/** Actions captured from the context so tests can drive auth transitions. */
let actions: AuthActions | null = null;

/** Pushes a profile update through the context, as Settings.tsx does. */
function pushUserUpdate(next: User): void {
  actions?.setUser(next);
}

/** Exposes the auth actions and the live scoped query to the test. */
function Harness({ onCache }: { onCache: (data: string | undefined) => void }) {
  const auth = useAuth();
  const { data } = useScopedQuery();

  useEffect(() => {
    onCache(data);
  }, [data, onCache]);

  useEffect(() => {
    actions = {
      login: () => auth.login("alice@example.com", "pw"),
      demoLogin: auth.demoLogin,
      logout: auth.logout,
      setUser: auth.setUser,
    };
  }, [auth.login, auth.demoLogin, auth.logout, auth.setUser]);

  return (
    <div>
      <span data-testid="user">{auth.user ? auth.user.email : "anonymous"}</span>
      <span data-testid="query">{data ?? "no-data"}</span>
      {actions && (
        <>
          <button type="button" onClick={() => void actions?.login()}>
            login
          </button>
          <button type="button" onClick={() => void actions?.demoLogin()}>
            demo
          </button>
          <button type="button" onClick={() => void actions?.logout()}>
            logout
          </button>
        </>
      )}
    </div>
  );
}

interface AuthActions {
  login: () => Promise<void>;
  demoLogin: () => Promise<void>;
  logout: () => Promise<void>;
  setUser: (user: User) => void;
}

function renderProvider(initialUser: User | null, before?: (client: QueryClient) => void) {
  restoreSession.mockResolvedValue(initialUser ? authPair(initialUser) : null);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // A hook for tests that need cache state in place BEFORE the session probe
  // resolves (the first-load case seeds an entry that must survive the restore).
  before?.(client);
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <Harness onCache={() => {}} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { client };
}

/** Cached values currently held, as `queryKey -> data`. */
function cacheContents(client: QueryClient): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const query of client.getQueryCache().getAll()) {
    out[JSON.stringify(query.queryKey)] = query.state.data;
  }
  return out;
}

/**
 * Assert every cached entry belongs to `owner` — never to another tenant.
 * The cache is not required to be EMPTY: mounted observers legitimately
 * refetch under the new token, so the correct end state is "only the current
 * user's data", not "no data".
 */
function assertOnlyOwnerCached(client: QueryClient, owner: string) {
  const expected = `secrets-for-${owner}`;
  for (const [key, data] of Object.entries(cacheContents(client))) {
    expect(`${key} => ${String(data)}`).toBe(`["watchlist","${owner}"] => ${expected}`);
  }
}

beforeEach(() => {
  restoreSession.mockReset().mockResolvedValue(null);
  login.mockReset();
  register.mockReset();
  demoLogin.mockReset();
  logout.mockReset().mockResolvedValue(undefined);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AuthProvider query-cache isolation", () => {
  it("drops every cached query when the user logs out", async () => {
    const { client } = renderProvider(null);
    login.mockResolvedValue(authPair(alice));
    logout.mockResolvedValue(undefined);

    await screen.findByText("login");
    screen.getByText("login").click();
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    // Alice's query resolved and is now cached.
    await waitFor(() => expect(screen.getByTestId("query").textContent).toBe("secrets-for-1"));
    expect(Object.keys(cacheContents(client)).length).toBeGreaterThan(0);

    // Alice masks deliberately during her session…
    act(() => {
      setMasked(true);
    });
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");

    screen.getByText("logout").click();

    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("anonymous"));
    // The previous tenant's data must be unreachable, not merely re-keyed.
    expect(client.getQueryData(["watchlist", "1"])).toBeUndefined();
    assertOnlyOwnerCached(client, "anonymous");
    // …and the mask dies with the identity it belonged to (M139/M183): logout
    // is the other end of the boot contract the restore now runs.
    expect(isMasked()).toBe(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });

  it("drops every cached query when a different user signs in", async () => {
    const { client } = renderProvider(alice);
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    await waitFor(() => expect(screen.getByTestId("query").textContent).toBe("secrets-for-1"));
    expect(Object.keys(cacheContents(client)).length).toBeGreaterThan(0);

    // Demo login swaps the identity to a different user id.
    demoLogin.mockResolvedValue(authPair(bob));
    await screen.findByText("demo");
    screen.getByText("demo").click();

    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("bob@example.com"));
    expect(client.getQueryData(["watchlist", "1"])).toBeUndefined();
    assertOnlyOwnerCached(client, "2");
  });

  it("leaves the cache intact for the session restored on first mount", async () => {
    // A production-shaped entry (the real keys are NOT user-scoped) seeded
    // before the probe answers, plus a spy on the eviction call itself: the
    // restore path must run the MASK half of the boot contract without taking
    // the eviction half with it (M183 — sharing one helper must not smuggle
    // `removeQueries()` onto the first-load path).
    const { client } = renderProvider(alice, (c) =>
      c.setQueryData(["portfolio"], { total_value: "987654.32" }),
    );
    const removeSpy = vi.spyOn(client, "removeQueries");
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    await waitFor(() => expect(screen.getByTestId("query").textContent).toBe("secrets-for-1"));
    // A fresh page load has no cross-user history to discard: restoring the
    // session must not tear down the cache the app is still populating.
    expect(client.getQueryData(["watchlist", "1"])).toBe("secrets-for-1");
    expect(client.getQueryData(["portfolio"])).toEqual({ total_value: "987654.32" });
    expect(removeSpy).not.toHaveBeenCalled();
  });

  it("evicts unscoped production query keys, which the harness alone cannot separate", async () => {
    // The real app's keys are NOT user-scoped: `["watchlist"]`,
    // `["portfolio"]`, `["security-overview", 12]` and friends are shared
    // across identities, so nothing but eviction separates two tenants. The
    // other tests here scope their own key by user id, which is why this one
    // seeds the production shape directly: it is the actual leak vector, and
    // it would survive a fix that only re-keys user-derived queries.
    const { client } = renderProvider(alice);
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    client.setQueryData(["watchlist"], { items: [{ security_id: 7, latest_price: "1234.56" }] });
    client.setQueryData(["portfolio"], { total_value: "987654.32" });
    expect(client.getQueryData(["watchlist"])).toBeDefined();

    demoLogin.mockResolvedValue(authPair(bob));
    await screen.findByText("demo");
    screen.getByText("demo").click();
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("bob@example.com"));

    // Bob must not be able to read Alice's portfolio figures at all.
    expect(client.getQueryData(["watchlist"])).toBeUndefined();
    expect(client.getQueryData(["portfolio"])).toBeUndefined();
  });

  it("keeps the cache when setUser refreshes the SAME user's profile", async () => {
    // Settings.tsx pushes updated user objects after a PATCH /users/me
    // (currency tracker, encryption, LLM config). The tenant is unchanged, so
    // evicting here would discard a warm cache on every profile save — the
    // failure mode of a fix that clears unconditionally instead of comparing
    // identities.
    const { client } = renderProvider(alice);
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    await waitFor(() => expect(screen.getByTestId("query").textContent).toBe("secrets-for-1"));

    act(() => {
      pushUserUpdate({ ...alice, currency_tracker_enabled: false, name: "Alice A." });
    });
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));

    // Same tenant: the cached query survives the profile update.
    expect(client.getQueryData(["watchlist", "1"])).toBe("secrets-for-1");
    expect(screen.getByTestId("query").textContent).toBe("secrets-for-1");
  });
});

// ---------------------------------------------------------------------------
// The session-restore seam: the mask boot rules NOW RUN THERE (t_2d8ffedb P1).
//
// `applyUser` is documented as the explicit-transition seam, and the mask boot
// rules live there (docs/46 § D1/D5): it calls `initMaskForSession()` — "a boot
// NEVER resurrects a stored mask … clears any stale key" — and
// `applyDemoMaskDefault()` for the read-only demo tenant.
//
// The restore path is the ONE transition that does not go through it: it seeds
// `cacheOwner.current`/`setUser` directly (deliberately, so a reload keeps the
// cache the app is populating — pinned above), so the rules were skipped on
// every page load that restored a valid session.
//
// Measured on 185ecc5 (probe, real runs):
//   demo restored from cookie    -> isMasked() false, key null
//   restore + stale `fintrack.mask` -> key "1" SURVIVES the boot
//   demo LOGIN (goes via applyUser) -> isMasked() true, key "1"   <- the control
//
// So the rules work; one transition bypasses them. Per docs/46 D5 the demo
// tenant is a public storefront and boots masked; per D1 a boot clears any
// stale key. Both are violated here, and no test failed on it (the seam is
// uncovered — testerbot's paper-trail audit, all 0 hits for cacheOwner /
// sessionStorage / applyUser / session-drop across the three vault suites).
//
// Filed 2026-10-12 as a flip-pinned characterization: three assertions pinned
// today's defect, each naming its flip. The fix shipped in fef0ac9, so they are
// retargeted here to pin the CONTRACT. The failed-boot case needed a second half
// of the fix, and that half was WRONG in c3135f5: it ran the boot contract from
// the probe's `.catch`, but `restoreSession` only ever RESOLVES null on failure
// (`postRefreshToken` returns null on a non-ok status and on a thrown fetch), so
// the catch never fired in production and this case pinned a rejection the real
// client cannot emit (t_5900dd66). It now pins the resolve-null shape — the
// production one — and the probe's resolve-null branch runs the boot contract;
// the `.catch` keeps it for a probe that throws. Verified against the fixed
// tree, end to end through the real client by
// frontend/tests/m183FailedBootMask.test.tsx (stubbed `fetch`, real
// `restoreSession`, real AuthProvider) and through the chip by
// frontend/tests/m139RestoreMask.test.tsx.
describe("restore path — the mask boot rules run on a restored session", () => {
  // The mask module keeps process-wide state (`masked`/`decided`) and the key
  // lives in sessionStorage; reset both so each case starts from a clean boot.
  beforeEach(() => {
    sessionStorage.clear();
    initMaskForSession();
  });

  it("boots a restored DEMO session masked (D5)", async () => {
    renderProvider(makeUser("9", "demo@fintracker.local"));
    await waitFor(() =>
      expect(screen.getByTestId("user").textContent).toBe("demo@fintracker.local"),
    );
    // docs/46 D5 — the demo storefront is public, so it boots masked. Both lines
    // read `false`/`null` while the seam skipped the boot contract; they flipped
    // when the fix shipped (fef0ac9).
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });

  it("clears a stale mask key on a restoring boot (D1)", async () => {
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    renderProvider(alice);
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    // docs/46 D1 — "a boot clears any stale key and starts from the tenant
    // default"; the identity is non-demo, so the boot default is unmasked.
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);
  });

  it("clears a stale key on a FAILED boot too — a boot is a boot (D1)", async () => {
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    // The PRODUCTION shape of a failed probe: `postRefreshToken` returns null on
    // a non-ok status AND on a thrown fetch, so `restoreSession` RESOLVES null
    // and never rejects (src/api/client.ts). This case used to pin
    // `mockRejectedValue(new Error(...))` — a promise the real client cannot
    // emit — which left the failed-boot contract jsdom-green while dead in
    // production (t_5900dd66). resolve-null is the shape the shared helper above
    // already uses, and the shape refreshSingleFlight.test.ts pins against the
    // real client (`expect(await restoreSession()).toBeNull()`); the full
    // real-client path is covered by tests/m183FailedBootMask.test.tsx.
    restoreSession.mockResolvedValue(null);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Harness onCache={() => {}} />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("anonymous"));
    // Logged out: any mask belonged to a session that no longer exists, and D1
    // does not exempt a boot that FAILED. The probe's resolve-null branch runs
    // the boot contract; the `.catch` next to it stays for a probe that throws.
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);
  });

  it("holds the invariant a fix must not break: a non-demo restore stays unmasked", async () => {
    renderProvider(alice);
    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("alice@example.com"));
    // The boot default for an ordinary tenant is unmasked (D1) — a fix that
    // masks on every restore would break this and the reload-cache test above.
    expect(isMasked()).toBe(false);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
  });
});
