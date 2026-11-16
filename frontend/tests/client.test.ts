// @vitest-environment node
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { api, ApiError, tokens } from "../src/api/client";
import { getDividendSuggestions } from "../src/api/trades";

/** Minimal Response shape covering exactly what client.ts reads. */
function fakeResponse(opts: {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  statusText?: string;
}): Response {
  const status = opts.status ?? 200;
  const headers = new Map(
    Object.entries(opts.headers ?? {}).map(
      ([k, v]) => [k.toLowerCase(), v] as [string, string],
    ),
  );
  const body = opts.body;
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: opts.statusText ?? "OK",
    headers: { get: (k: string) => headers.get(k.toLowerCase()) ?? null },
    json: async () => (typeof body === "string" ? JSON.parse(body) : body),
    clone: () => fakeResponse(opts),
  } as unknown as Response;
}

describe("api() client", () => {
  let fetchMock: Mock;
  let locationAssign: Mock;

  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
      clear: () => store.clear(),
    });
    locationAssign = vi.fn();
    vi.stubGlobal("window", { location: { assign: locationAssign } });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns parsed JSON on a 2xx response", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: { hello: "world" } }));
    const data = await api<{ hello: string }>("/things");
    expect(data).toEqual({ hello: "world" });
    expect(locationAssign).not.toHaveBeenCalled();
  });

  it("preserves the list body and parses additive dividend history metadata", async () => {
    const status = {
      pending: 1,
      unavailable: 2,
      empty: 3,
      stale: 4,
      quote_miss: 1,
      as_of: "2026-10-13T12:00:00Z",
    };
    fetchMock.mockResolvedValue(
      fakeResponse({
        body: [
          {
            security_id: 9,
            account_id: 4,
            symbol: "TEST",
            date: "2026-10-01",
            amount: "0.25",
            quantity: "2",
            gross_amount: "0.50",
            currency: "USD",
          },
        ],
        headers: { "X-Dividend-History-State": JSON.stringify(status) },
      }),
    );

    const result = await getDividendSuggestions();

    expect(result).toHaveLength(1);
    expect(result[0].symbol).toBe("TEST");
    // A pre-provenance server's five keys pass through verbatim, and the three
    // additive ones it does not send are DEFAULTED (docs/07 §M184) so the
    // source strip can read `missing` / `source` / `age_days` without a
    // version check against the server.
    expect(result.history).toEqual({
      missing: 0,
      source: null,
      age_days: null,
      ...status,
    });
  });

  it("passes the additive provenance keys of the header straight through", async () => {
    const status = {
      pending: 0,
      unavailable: 1,
      empty: 0,
      stale: 2,
      quote_miss: 1,
      missing: 3,
      source: "yfinance",
      as_of: "2026-10-13T12:00:00Z",
      age_days: 4,
    };
    fetchMock.mockResolvedValue(
      fakeResponse({ body: [], headers: { "X-Dividend-History-State": JSON.stringify(status) } }),
    );

    const result = await getDividendSuggestions();

    expect(result).toHaveLength(0);
    expect(result.history).toEqual(status);
  });

  it("raises ApiError with the {detail, code} envelope on error", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 422, body: { detail: "nope", code: "validation_error" } }),
    );
    let caught: ApiError | null = null;
    try {
      await api("/things");
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught).not.toBeNull();
    expect(caught!.status).toBe(422);
    expect(caught!.message).toBe("nope");
    expect(caught!.code).toBe("validation_error");
  });

  it("returns undefined for a 204 / empty body", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 204 }));
    const data = await api("/things");
    expect(data).toBeUndefined();
  });

  it("returns undefined when content-length is 0", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 200, headers: { "content-length": "0" } }),
    );
    const data = await api("/things");
    expect(data).toBeUndefined();
  });

  it("on 401 whose refresh also fails, clears tokens, redirects, and throws", async () => {
    // Two fetches: the original request, then POST /auth/refresh (the
    // httpOnly cookie lives with the browser, not this mock).
    fetchMock
      .mockResolvedValueOnce(fakeResponse({ status: 401 }))
      .mockResolvedValueOnce(fakeResponse({ status: 401 }));
    let caught: ApiError | null = null;
    try {
      await api("/things");
    } catch (e) {
      caught = e as ApiError;
    }
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(tokens.access).toBeNull();
    expect(locationAssign).toHaveBeenCalledWith("/login");
    expect(caught?.status).toBe(401);
    expect(caught?.code).toBe("invalid_token");
  });

  it("on 401, refreshes once, replays, and returns the data", async () => {
    tokens.storeAccess("oldAccess");
    let callCount = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith("/auth/refresh")) {
        return fakeResponse({ status: 200, body: { access_token: "newA", refresh_token: "newR" } });
      }
      callCount += 1;
      if (callCount === 1) return fakeResponse({ status: 401 });
      return fakeResponse({ status: 200, body: { ok: true } });
    });

    const data = await api<{ ok: boolean }>("/things");

    expect(data).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(locationAssign).not.toHaveBeenCalled();
    expect(tokens.access).toBe("newA");
  });

  it("on 401, refreshes, but replays into 401 -> clears, redirects, throws", async () => {
    tokens.storeAccess("oldAccess");
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith("/auth/refresh")) {
        return fakeResponse({ status: 200, body: { access_token: "newA", refresh_token: "newR" } });
      }
      return fakeResponse({ status: 401 });
    });

    let caught: ApiError | null = null;
    try {
      await api("/things");
    } catch (e) {
      caught = e as ApiError;
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(locationAssign).toHaveBeenCalledWith("/login");
    expect(caught?.code).toBe("invalid_token");
  });

  it("401 on POST /auth/login propagates without refresh or redirect (docs/29 F5c)", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 401, body: { detail: "Invalid credentials", code: "invalid_credentials" } }),
    );
    let caught: ApiError | null = null;
    try {
      await api("/auth/login", { method: "POST", body: { email: "a@b.c", password: "nope" } });
    } catch (e) {
      caught = e as ApiError;
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(locationAssign).not.toHaveBeenCalled();
    expect(tokens.access).toBeNull();
    expect(caught?.status).toBe(401);
    expect(caught?.message).toBe("Invalid credentials");
    expect(caught?.code).toBe("invalid_credentials");
  });

  it("401 on /auth/register and /auth/demo also skip the refresh dance", async () => {
    fetchMock
      .mockResolvedValueOnce(fakeResponse({ status: 401, body: { detail: "taken", code: "email_taken" } }))
      .mockResolvedValueOnce(fakeResponse({ status: 401, body: { detail: "demo down", code: "demo_unavailable" } }));
    let caught: ApiError | null = null;
    try {
      await api("/auth/register", { method: "POST", body: { email: "a@b.c", password: "pw" } });
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught?.code).toBe("email_taken");

    let demoCaught: ApiError | null = null;
    try {
      await api("/auth/demo", { method: "POST" });
    } catch (e) {
      demoCaught = e as ApiError;
    }
    expect(demoCaught?.code).toBe("demo_unavailable");

    // Two failed calls, zero refresh attempts, zero redirects.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(locationAssign).not.toHaveBeenCalled();
  });

  it("401 on other /auth-prefixed paths still triggers the refresh dance", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith("/auth/refresh")) {
        return fakeResponse({ status: 200, body: { access_token: "newA", refresh_token: "newR" } });
      }
      return fakeResponse({ status: 401 });
    });
    let caught: ApiError | null = null;
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (e) {
      caught = e as ApiError;
    }
    expect(caught?.code).toBe("invalid_token");
    expect(locationAssign).toHaveBeenCalledWith("/login");
  });

  // --- M98b F-5: /partner/* 401s are told apart by the ERROR BODY ------
  // The prefix is a family, not a verdict: sharing_not_linked is the domain
  // state the skip exists for, invalid_token is a dead session that must run
  // the dance or the UI parks on a fail-closed error panel forever.

  const partner401 = (code: string, detail = "nope") =>
    new Response(JSON.stringify({ detail, code }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });

  it("401 invalid_token on /partner/* refreshes, replays and returns the data", async () => {
    tokens.clear();
    tokens.storeAccess("staleAccess");
    let partnerCalls = 0;
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.endsWith("/auth/refresh")) {
        return fakeResponse({ status: 200, body: { access_token: "newA", refresh_token: "newR" } });
      }
      partnerCalls += 1;
      if (partnerCalls === 1) {
        return partner401("invalid_token", "Invalid or expired access token");
      }
      return fakeResponse({ status: 200, body: { items: [] } });
    });

    const data = await api<{ items: unknown[] }>("/partner/accounts");

    expect(data).toEqual({ items: [] });
    // original 401 -> POST /auth/refresh -> replay: the standard dance.
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(locationAssign).not.toHaveBeenCalled();
    expect(tokens.access).toBe("newA");
  });

  it("401 invalid_token on /partner/* whose refresh fails clears tokens and redirects to /login", async () => {
    tokens.clear();
    tokens.storeAccess("staleAccess");
    fetchMock
      .mockResolvedValueOnce(partner401("invalid_token", "Invalid or expired access token"))
      .mockResolvedValueOnce(fakeResponse({ status: 401 }));

    let caught: ApiError | null = null;
    try {
      await api("/partner/transactions");
    } catch (e) {
      caught = e as ApiError;
    }

    expect(fetchMock).toHaveBeenCalledTimes(2); // original + refresh, no replay
    expect(locationAssign).toHaveBeenCalledWith("/login");
    expect(tokens.access).toBeNull();
    expect(caught?.status).toBe(401);
    expect(caught?.code).toBe("invalid_token");
  });

  it("401 sharing_not_linked on /partner/* stays a quiet skip, body intact", async () => {
    tokens.clear();
    fetchMock.mockResolvedValue(
      partner401("sharing_not_linked", "No shared household is linked to this account"),
    );

    let caught: ApiError | null = null;
    try {
      await api("/partner/accounts");
    } catch (e) {
      caught = e as ApiError;
    }

    // ONE call: no refresh, no replay, no hard redirect — and the body the
    // caller parses is still readable (the code is read from a CLONE).
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(locationAssign).not.toHaveBeenCalled();
    expect(caught?.status).toBe(401);
    expect(caught?.message).toBe("No shared household is linked to this account");
    expect(caught?.code).toBe("sharing_not_linked");
  });

  it("401 on /partner/* with a body we cannot read keeps the quiet skip", async () => {
    tokens.clear();
    // Not JSON at all (a proxy's HTML error page): "cannot tell" must not be
    // read as "session dead" — logging someone out on a guess is worse.
    fetchMock.mockResolvedValue(
      new Response("<html>401 Unauthorized</html>", {
        status: 401,
        headers: { "Content-Type": "text/html" },
      }),
    );

    let caught: ApiError | null = null;
    try {
      await api("/partner/categories");
    } catch (e) {
      caught = e as ApiError;
    }

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(locationAssign).not.toHaveBeenCalled();
    expect(caught?.status).toBe(401);
    expect(caught?.code).toBeNull();
  });
});
