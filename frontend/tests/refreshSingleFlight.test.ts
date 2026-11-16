// @vitest-environment node
/**
 * Regression suite for the demo-session rotation race (docs/09 "demo-session
 * rotation").
 *
 * `POST /auth/refresh` rotates a cookie-bound token: the backend claims the
 * presented token with one conditional UPDATE and treats the loser as reuse —
 * chain-wide revocation + cleared cookie. Two refreshes carrying ONE cookie
 * therefore log the tenant out, so every caller in the app must share a single
 * in-flight POST (per tab via `pendingRefresh`, across tabs via the Web Lock).
 * Pre-fix these tests failed on the POST count: `restoreSession()` (AuthContext
 * mount, usePriceStream SSE reconnect) bypassed the 401 path's dedupe.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, restoreSession, tokens } from "../src/api/client";

const USER = {
  id: "1",
  email: "demo@fintracker.local",
  privacy_mode: false,
  currency_tracker_enabled: false,
  llm_provider: "openai",
  llm_base_url: null,
  llm_model: "m",
  encryption_required: false,
};
const PAIR = { access_token: "newA", token_type: "bearer" as const, user: USER };

function fakeResponse(status: number, body?: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

type FetchMock = ReturnType<typeof vi.fn>;
function refreshPostCount(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/auth/refresh"))
    .length;
}

describe("refresh single-flight", () => {
  let fetchMock: FetchMock;
  let locationAssign: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    tokens.clear();
    locationAssign = vi.fn();
    vi.stubGlobal("window", { location: { assign: locationAssign } });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mount probe + SSE reconnect + 401 path issue ONE POST together", async () => {
    const gate = deferred<Response>();
    let things = 0;
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.endsWith("/auth/refresh")) return gate.promise;
      things += 1;
      // First API call 401s (expired access token), its replay succeeds.
      return things === 1 ? fakeResponse(401) : fakeResponse(200, { ok: true });
    });
    tokens.storeAccess("staleAccess");

    // AuthContext's session probe, usePriceStream's SSE onerror, and an api()
    // request that came back 401 — the three callers in production.
    const probe = restoreSession();
    const reconnect = restoreSession();
    const dataPromise = api<{ ok: boolean }>("/things");

    await new Promise((r) => setTimeout(r, 0));
    gate.resolve(fakeResponse(200, PAIR));

    const [probeResult, reconnectResult, data] = await Promise.all([
      probe,
      reconnect,
      dataPromise,
    ]);
    expect(probeResult).toEqual(PAIR);
    expect(reconnectResult).toEqual(PAIR);
    expect(data).toEqual({ ok: true });
    expect(refreshPostCount(fetchMock)).toBe(1);
    expect(tokens.access).toBe("newA");
    expect(locationAssign).not.toHaveBeenCalled();
  });

  it("concurrent restoreSession() calls share one in-flight POST", async () => {
    const gate = deferred<Response>();
    fetchMock.mockImplementation(() => gate.promise);

    const callers = [
      restoreSession(),
      restoreSession(),
      restoreSession(),
      restoreSession(),
      restoreSession(),
    ];
    // All five are pending on the same request before any response lands.
    expect(refreshPostCount(fetchMock)).toBe(1);

    gate.resolve(fakeResponse(200, PAIR));
    const results = await Promise.all(callers);
    for (const result of results) expect(result).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(1);
  });

  it("does not reuse a settled refresh — the next caller posts again", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, PAIR));
    expect(await restoreSession()).toEqual(PAIR);
    expect(await restoreSession()).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(2);
  });

  it("a failed refresh clears the slot so a later caller can retry", async () => {
    fetchMock.mockResolvedValueOnce(fakeResponse(500));
    expect(await restoreSession()).toBeNull();

    fetchMock.mockResolvedValueOnce(fakeResponse(200, PAIR));
    expect(await restoreSession()).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(2);
  });

  it("runs the POST inside the cross-tab refresh lock", async () => {
    const events: string[] = [];
    const request = vi.fn(
      async (name: string, callback: () => Promise<unknown>): Promise<unknown> => {
        events.push(`acquire:${name}`);
        try {
          return await callback();
        } finally {
          events.push("release");
        }
      },
    );
    vi.stubGlobal("navigator", { locks: { request } });
    fetchMock.mockImplementation(async () => {
      events.push("post");
      return fakeResponse(200, PAIR);
    });

    expect(await restoreSession()).toEqual(PAIR);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe("fin_tracker.auth.refresh");
    expect(events).toEqual(["acquire:fin_tracker.auth.refresh", "post", "release"]);
  });

  it("refreshes normally when Web Locks are unavailable", async () => {
    vi.stubGlobal("navigator", {});
    fetchMock.mockResolvedValue(fakeResponse(200, PAIR));
    expect(await restoreSession()).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(1);
  });

  it("still refreshes when the lock fails before running the callback", async () => {
    vi.stubGlobal("navigator", {
      locks: { request: vi.fn(() => Promise.reject(new Error("lock blocked"))) },
    });
    fetchMock.mockResolvedValue(fakeResponse(200, PAIR));
    expect(await restoreSession()).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(1);
  });

  it("keeps the refresh result when the lock fails after it ran", async () => {
    vi.stubGlobal("navigator", {
      locks: {
        request: vi.fn(async (_name: string, callback: () => Promise<unknown>) => {
          await callback();
          throw new Error("lock failed late");
        }),
      },
    });
    fetchMock.mockResolvedValue(fakeResponse(200, PAIR));
    // No rejection, and no second POST — a refresh that already happened is
    // never repeated, whatever the lock does around it.
    expect(await restoreSession()).toEqual(PAIR);
    expect(refreshPostCount(fetchMock)).toBe(1);
    expect(locationAssign).not.toHaveBeenCalled();
  });
});
