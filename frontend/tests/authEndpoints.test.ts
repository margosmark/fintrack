// @vitest-environment node
// M74 client contract (docs/38 "Login + challenge" + the refresh-path rule).
//
// Three things live in api/client.ts that a page test cannot see:
//   1. `login()` branches on `challenge` and stores an access token ONLY for a
//      real token pair — a challenge carries no session;
//   2. the second-factor / passkey / OIDC routes are on the 401-skip list, so
//      `401 invalid_totp_code` (etc.) surfaces to the caller instead of
//      entering the 401 → refresh → replay → redirect dance and swallowing
//      the error the user needs;
//   3. read-only authed routes still refresh on 401, so the skip list has not
//      quietly disabled session recovery.
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError, api, login, loginWithTotp, tokens } from "../src/api/client";

/** Minimal Response shape covering exactly what client.ts reads. */
function fakeResponse(opts: {
  status?: number;
  body?: unknown;
  statusText?: string;
}): Response {
  const status = opts.status ?? 200;
  const headers = new Map<string, string>();
  if (opts.body !== undefined && opts.status !== 204) {
    headers.set("content-length", String(JSON.stringify(opts.body).length));
  }
  const body = opts.body;
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText: opts.statusText ?? "OK",
    headers: { get: (k: string) => headers.get(k.toLowerCase()) ?? null },
    json: async () => (typeof body === "string" ? JSON.parse(body) : body),
  } as unknown as Response;
}

const USER = { id: "u1", email: "alice@example.com" };
const PAIR = { access_token: "access-1", token_type: "bearer", user: USER };
const CHALLENGE = { challenge: "totp", challenge_token: "ct-1", expires_in: 300 };

/** Every URL the mocked fetch has been called with, in order. */
function urls(fetchMock: Mock): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

let fetchMock: Mock;

beforeEach(() => {
  tokens.clear();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("window", { location: { assign: vi.fn() } });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("login() union", () => {
  it("returns a challenge WITHOUT storing an access token", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: CHALLENGE }));

    const result = await login("alice@example.com", "pw");

    expect(result).toEqual(CHALLENGE);
    expect(tokens.access).toBeNull();
  });

  it("stores the access token for a real token pair", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: PAIR }));

    const result = await login("alice@example.com", "pw");

    expect("challenge" in result).toBe(false);
    expect(tokens.access).toBe("access-1");
  });

  it("completes the challenge and stores the pair from /auth/login/totp", async () => {
    fetchMock.mockResolvedValue(fakeResponse({ status: 200, body: PAIR }));

    const result = await loginWithTotp("ct-1", "123456");

    expect(result.user.id).toBe("u1");
    expect(tokens.access).toBe("access-1");
    expect(urls(fetchMock)).toEqual(["/api/v1/auth/login/totp"]);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      challenge_token: "ct-1",
      code: "123456",
    });
  });
});

describe("401 handling on login-adjacent routes", () => {
  it("surfaces a domain 401 from /auth/login/totp without touching /auth/refresh", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 401, body: { detail: "That code was not valid", code: "invalid_totp_code" } }),
    );

    let caught: ApiError | null = null;
    try {
      await loginWithTotp("ct-1", "000000");
    } catch (err) {
      caught = err as ApiError;
    }

    expect(caught).not.toBeNull();
    expect(caught!.code).toBe("invalid_totp_code");
    expect(caught!.message).toBe("That code was not valid");
    // One request, and it is NOT the refresh endpoint: a mid-sign-in refresh
    // would 401, clear the token and hard-redirect to /login, hiding the error.
    expect(urls(fetchMock)).toEqual(["/api/v1/auth/login/totp"]);
  });

  it("surfaces a domain 401 from /auth/oidc/confirm without a refresh", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse({ status: 401, body: { detail: "Sign-in request expired", code: "oidc_pending_expired" } }),
    );

    await expect(api("/auth/oidc/confirm", { method: "POST" })).rejects.toMatchObject({
      code: "oidc_pending_expired",
    });
    expect(urls(fetchMock)).toEqual(["/api/v1/auth/oidc/confirm"]);
  });

  it("still refreshes on a 401 from a read-only authed route", async () => {
    // /auth/totp/status is NOT on the skip list: a 401 there means the ACCESS
    // token expired, which is exactly what the refresh dance exists for.
    fetchMock
      .mockResolvedValueOnce(fakeResponse({ status: 401, body: { detail: "no", code: "invalid_token" } }))
      .mockResolvedValueOnce(fakeResponse({ status: 401, body: { detail: "refresh failed" } }));

    // Refresh is ATTEMPTED (the skip list did not disable session recovery);
    // it fails, so the client clears the token and bails to /login — the
    // shipped pre-existing behaviour for an expired access token.
    await expect(api("/auth/totp/status")).rejects.toMatchObject({ code: "invalid_token" });
    expect(urls(fetchMock)).toEqual([
      "/api/v1/auth/totp/status",
      "/api/v1/auth/refresh",
    ]);
    expect(tokens.access).toBeNull();
    expect(vi.mocked(window.location.assign)).toHaveBeenCalledWith("/login");
  });
});
