// M183 failed-boot mask contract — end to end through the REAL client
// (t_5900dd66).
//
// c3135f5 put the failed-boot half of the mask contract in the session probe's
// `.catch` (`AuthContext`), but `restoreSession()` only ever RESOLVES: on a
// non-ok status `postRefreshToken` returns `null` and on a thrown fetch it
// catches and returns `null` too — it never rejects (src/api/client.ts). The
// catch therefore never ran in production, so a stale `fintrack.mask` key
// outlived the session it described until the next successful transition —
// exactly the state the fix's commit message claims to close.
//
// This file mocks NO seam: `fetch` is stubbed at the network boundary, the REAL
// `restoreSession` runs against it and the REAL AuthProvider boots on top. It
// is red on c3135f5's tree and green with the fix, unlike the seam-level pin it
// complements — that one stubbed a rejection the client cannot produce
// (jsdom-green, prod-red).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider, useAuth } from "../src/auth/AuthContext";
import { MASK_SESSION_KEY, initMaskForSession, isMasked } from "../src/lib/mask";

/** The real client fetches exactly one endpoint on boot: POST /auth/refresh. */
function probe(url: string): boolean {
  return String(url).endsWith("/auth/refresh");
}

/** The two failure shapes the client actually sees: a non-ok status, and a
 *  network error. Both funnel into `postRefreshToken`'s two `return null`s. */
const UNAUTHORIZED = {
  status: 401,
  ok: false,
  statusText: "Unauthorized",
  headers: { get: () => null },
  json: async () => ({ detail: "refresh token expired" }),
} as unknown as Response;

function Probe() {
  const { user, ready } = useAuth();
  return <span data-testid="user">{ready ? (user?.email ?? "anonymous") : "pending"}</span>;
}

function renderProvider() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  initMaskForSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a FAILED boot clears the stale mask key (t_5900dd66)", () => {
  it("clears the key when the refresh cookie is dead (non-ok status)", async () => {
    // The state the defect leaves behind: a masked session died in this tab, the
    // reload has no cookie left to send, and the key it wrote outlives it.
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    const fetchMock = vi.fn(async (url: string) => {
      expect(probe(url)).toBe(true);
      return UNAUTHORIZED;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderProvider();

    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("anonymous"));
    // The boot contract runs on a boot that found NO session too (docs/46 D1: "a
    // boot clears any stale key and starts from the tenant default"). There is
    // no tenant, so the default is unmasked.
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);
    // Not vacuous: the real client really did probe the refresh endpoint.
    expect(fetchMock.mock.calls.filter((call) => probe(call[0] as string))).toHaveLength(1);
  });

  it("clears the key when the probe fails at the network layer", async () => {
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    renderProvider();

    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("anonymous"));
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBeNull();
    expect(isMasked()).toBe(false);
  });

  it("still honours a choice the user already made when the failed probe lands late", async () => {
    // The guard the restore path keeps, pinned on the failed-boot branch too: a
    // session default is a default, not a lock, and a live choice is not stale
    // state to be cleared.
    sessionStorage.setItem(MASK_SESSION_KEY, "1");
    const { setMasked } = await import("../src/lib/mask");
    setMasked(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    renderProvider();

    await waitFor(() => expect(screen.getByTestId("user").textContent).toBe("anonymous"));
    expect(isMasked()).toBe(true);
    expect(sessionStorage.getItem(MASK_SESSION_KEY)).toBe("1");
  });
});
