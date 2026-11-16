import type {
  AuthResponse,
  LoginChallenge,
  OidcConfigResponse,
  OidcPreview,
  PasskeyInfo,
  PasskeyOptionsResponse,
  PasskeyPage,
  RegisterAccepted,
  TotpConfirmResponse,
  TotpSetupResponse,
  TotpStatus,
  User,
} from "../types";

const BASE = "/api/v1";

// Access token lives in memory only — never persisted to localStorage.
// Refresh token is an httpOnly cookie set by the backend; JavaScript cannot
// read or write it directly.
let _accessToken: string | null = null;

export class ApiError extends Error {
  readonly status: number;
  /** Error envelope field (docs/08): `{ detail, code }`. */
  readonly code: string | null;

  constructor(status: number, detail: string, code: string | null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * Does this rejection carry the envelope's machine-readable `code`?
 * (docs/08 §Error envelope — the ONE way the client branches on an error.)
 *
 * Branch on `code`, NEVER on `detail`. `ApiError` calls `super(detail)`, so
 * `err.message` and `apiErrorMessage(err)` are always the human copy — testing
 * a `code` token against them compares two different envelope fields and the
 * branch is dead (`detail` carries the token only by coincidence, so it flips
 * with a copy edit). `code` is the contract field: it is what the backend
 * guarantees (`backend/app/core/errors.py::api_error`) and it survives
 * rewording. M112 removed the first such branch (the import wizard never
 * auto-opened); M113 removed the rest.
 *
 * Anything that is not the envelope (a network failure, a thrown plain
 * `Error`, a proxy's HTML body) has no `code` and correctly matches nothing.
 */
export function hasErrorCode(err: unknown, code: string): boolean {
  return err instanceof ApiError && err.code === code;
}

export const tokens = {
  get access(): string | null {
    return _accessToken;
  },
  /** Store the access token in memory (refresh cookie is managed by the backend). */
  storeAccess(token: string): void {
    _accessToken = token;
  },
  clear(): void {
    _accessToken = null;
  },
};

interface RequestOptions {
  method?: string;
  body?: unknown;
}


/**
 * The ONE in-flight `POST /auth/refresh` for this tab, shared by every caller.
 *
 * Rotation makes concurrent refreshes dangerous rather than merely wasteful:
 * the browser sends the same httpOnly cookie with both, `_claim_refresh_token`
 * lets exactly one win (backend/app/api/auth.py), and the loser is taken for
 * reuse — which revokes the user's whole chain *including the winner's
 * brand-new token* and clears the cookie (docs/29 reuse detection). One lost
 * race logs the tenant out everywhere; that is what killed the demo session
 * 39 s into the M58b walkthrough (docs/09).
 *
 * Every refresh caller funnels through `refreshTokens()` so they join this
 * promise instead of issuing a second POST: AuthContext's session probe on
 * mount, `usePriceStream`'s SSE reconnect, and the 401 path below. Deduping
 * only the 401 path (the previous behaviour) left the other two racing.
 */
let pendingRefresh: Promise<AuthResponse | null> | null = null;

/**
 * Web Lock serialising refreshes across the tabs of ONE browser.
 *
 * Tabs do not share JS state, so `pendingRefresh` cannot see a refresh started
 * by another tab — yet they DO share the cookie jar, so two tabs loading at
 * once present the same token and reproduce the race above (the stress
 * reproducer in `scripts/auth/session_stress.py` fails this way in ~7 s
 * pre-fix). `navigator.locks` is the platform primitive for exactly this:
 * whichever tab wins sends, the others wait and then send against the cookie
 * that response just set.
 *
 * Two distinct degradations, only one of them harmless:
 * - a lock that ERRORS never costs the session — the refresh still runs
 *   (withCrossTabLock, below);
 * - an ABSENT lock costs only the cross-tab half of the fix. `navigator.locks`
 *   is a `[SecureContext]` API, so on a plain-HTTP non-loopback origin it is
 *   simply `undefined` and only per-tab `pendingRefresh` applies — two tabs
 *   loading at once can still race the cookie (docs/09, "Residual risks
 *   (accepted)").
 */
const REFRESH_LOCK = "fin_tracker.auth.refresh";

/**
 * Fetch wrapper (docs/09): attaches `Authorization: Bearer` on every request.
 * On a 401, performs ONE `POST /auth/refresh` (cookie auto-sent), replays the
 * original request ONCE, and otherwise clears tokens and redirects to /login.
 * Auth endpoints (login/register/demo/refresh) let their 401 propagate
 * instead — docs/29 F5c.
 */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await withAuthRetry(() => send(path, options), true, path);
  return parse<T>(response);
}

/** Raw response access for additive metadata headers on an otherwise stable body. */
export async function apiWithResponse<T>(
  path: string,
  options: RequestOptions = {},
): Promise<{ data: T; response: Response }> {
  const response = await withAuthRetry(() => send(path, options), true, path);
  return { data: await parse<T>(response.clone()), response };
}

/**
 * Auth endpoints whose 401 must propagate to the caller (docs/29 F5c): a
 * failed login/register/demo/refresh is a rejected attempt, not an expired
 * session — refreshing and redirecting would hide the error panel and mask
 * the real failure.
 *
 * M74 (docs/38) widened this to every login-adjacent route: `401
 * invalid_totp_code`, `invalid_recovery_code`, `invalid_passkey`,
 * `invalid_or_expired_challenge` and `oidc_pending_expired` are DOMAIN
 * failures of an in-progress sign-in, not an expired access token. Letting
 * one of them fall into the refresh path would POST /auth/refresh mid-sign-in,
 * then hard-redirect to /login on the (expected) refresh failure — wiping the
 * error the user needs to see. Read-only authed routes (`/auth/totp/status`,
 * `/auth/passkeys` list, `/users/me`) are deliberately NOT listed: a 401 there
 * really IS an expired session and must still refresh.
 */
const AUTH_401_SKIP_PATHS: Record<string, true> = {
  "/auth/login": true,
  "/auth/register": true,
  "/auth/demo": true,
  "/auth/refresh": true,
  // M74 — second factor + passkeys + OIDC (docs/38).
  "/auth/login/totp": true,
  "/auth/login/recovery": true,
  "/auth/totp/setup": true,
  "/auth/totp/confirm": true,
  "/auth/totp/disable": true,
  "/auth/passkeys/options": true,
  "/auth/passkeys": true,
  "/auth/passkeys/login/options": true,
  "/auth/passkeys/login": true,
  "/auth/oidc/preview": true,
  "/auth/oidc/confirm": true,
  // M90 (docs/43 §7): the invitee answering an unknown/foreign/expired
  // token — `401 sharing_invite_invalid` / `sharing_invite_expired` are the
  // ANSWER to the accept attempt, not an expired session; the refresh dance
  // would mask the envelope the accept page must render verbatim.
  "/sharing/invites/accept": true,
};

/**
 * Route PREFIXES whose 401 MAY be a domain state (M90, docs/43 §7) — but the
 * prefix alone is not the verdict, the error BODY is (M98b F-5, below).
 * The four `/partner/*` DATA GETs answer `401 sharing_not_linked` when no
 * active link exists — the partner's own session is perfectly valid, and
 * running the refresh → clear-tokens → /login dance would log them out for
 * reading a page. The SAME routes answer `401 invalid_token` when the session
 * really is dead, and that one has to run the dance like any other endpoint:
 * skipping it leaves the UI on a fail-closed error panel that never refreshes
 * and never redirects. A PREFIX tuple (not five exact paths) so the set
 * cannot rot as partner routes are added; the partner surface is GET-only
 * and closed (pinned by `test_partner_router_is_get_only`), so no mutating
 * route can hide under it.
 */
const AUTH_401_SKIP_PREFIXES: readonly string[] = ["/partner/"];

/**
 * Runs `sendFn`, applying the shared 401 → refresh → replay → redirect dance
 * used by `api`, `apiForm`, and `apiBinary`. `requireOk` controls whether a
 * non-2xx (non-401) response throws `ApiError`. Returns the successful raw
 * `Response` so the caller can parse it (JSON, blob, or text) its own way.
 * `path` identifies the request so auth endpoints (docs/29 F5c) can bypass the
 * dance and let their 401 propagate, and so a `/partner/*` 401 can be told
 * apart by its error BODY (M98b F-5) — skipped when it is the
 * `sharing_not_linked` domain state, danced when it is `invalid_token`.
 */
async function withAuthRetry(
  sendFn: () => Promise<Response>,
  requireOk: boolean,
  path = "",
): Promise<Response> {
  const first = await sendFn();
  // Non-401s and auth-endpoint 401s (docs/29 F5c) resolve here directly.
  const bare = path.split("?")[0];
  if (first.status !== 401 || AUTH_401_SKIP_PATHS[bare]) {
    if (requireOk && !first.ok) return parseError(first);
    return first;
  }
  // A PREFIX match is a family of 401s, not a verdict (M98b F-5): the
  // body decides. `sharing_not_linked` is the domain state the skip exists
  // for — a valid session on an unlinked partner account — so it still
  // resolves here as an ApiError for the page to render. `invalid_token` is
  // a genuinely dead session and must fall through to the dance below, or
  // the partner surface renders a fail-closed error panel forever instead of
  // refreshing or landing on /login. Any OTHER code (or a body we cannot
  // read at all) keeps the quiet skip: an unreadable 401 is not evidence the
  // session died, and logging someone out on a guess is the worse error.
  if (AUTH_401_SKIP_PREFIXES.some((prefix) => bare.startsWith(prefix))) {
    const code = await readErrorCode(first);
    if (code !== "invalid_token") {
      if (requireOk && !first.ok) return parseError(first);
      return first;
    }
  }

  // Single-flight: every refresh caller (this 401 path, AuthContext's probe,
  // the SSE reconnect) shares the one in-flight POST — see pendingRefresh.
  const refreshed = await refreshTokens();
  if (!refreshed) {
    tokens.clear();
    window.location.assign("/login");
    throw new ApiError(401, "Session expired", "invalid_token");
  }

  const replayed = await sendFn();
  if (replayed.status === 401) {
    tokens.clear();
    window.location.assign("/login");
    throw new ApiError(401, "Session expired", "invalid_token");
  }
  if (requireOk && !replayed.ok) return parseError(replayed);
  return replayed;
}

/**
 * The `code` field of a `{detail, code}` error body, read from a CLONE of the
 * response so the original body stays unread for whoever consumes it next
 * (`parseError`, or the caller via `return first`). `null` when there is no
 * body, it is not JSON, or it carries no string `code` — the caller treats
 * "cannot tell" as "not the case" and keeps the quiet skip.
 */
async function readErrorCode(response: Response): Promise<string | null> {
  try {
    const payload = (await response.clone().json()) as { code?: unknown };
    return typeof payload.code === "string" ? payload.code : null;
  } catch {
    return null;
  }
}

/** Single network attempt; returns the raw Response for status handling. */
async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const access = tokens.access;
  if (access) {
    headers.set("Authorization", `Bearer ${access}`);
  }

  return fetch(`${BASE}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
}

/**
 * Run `fn` while holding the browser-wide refresh lock (REFRESH_LOCK).
 *
 * Falls back to running `fn` unlocked when Web Locks are unavailable — a
 * non-secure origin (plain HTTP to a non-loopback host: `navigator.locks` is
 * `[SecureContext]`, so it is `undefined` there), older browsers (Safari
 * ≤ 15.3), jsdom, a frame whose permissions policy blocks them. The lock is
 * an extra guarantee layered on top of per-tab single-flight, not a
 * precondition for refreshing, and this never rejects: a lock that fails
 * BEFORE running `fn` still refreshes (a lock failure must not cost the
 * session); one that fails AFTER `fn` resolved hands back `fn`'s own result,
 * because dropping a refresh that already happened would log the tab out for
 * a browser-internal reason.
 */
async function withCrossTabLock(
  fn: () => Promise<AuthResponse | null>,
): Promise<AuthResponse | null> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  if (!locks || typeof locks.request !== "function") return fn();
  let started = false;
  let result: AuthResponse | null = null;
  try {
    return (await locks.request(REFRESH_LOCK, async () => {
      started = true;
      result = await fn();
      return result;
    })) as AuthResponse | null;
  } catch {
    if (started) return result;
    return fn();
  }
}

/**
 * POST /auth/refresh — the browser auto-sends the httpOnly refresh cookie.
 * No body needed. Stores and returns the NEW access token on success, null on
 * any failure. Never loops through `api()` — the refresh endpoint itself must
 * not trigger another refresh.
 *
 * ONE request at a time, per tab and across the browser's tabs: the second
 * POST would carry the same cookie, lose `_claim_refresh_token`, and be taken
 * for reuse (chain-wide revocation — see pendingRefresh). `withAuthRetry`,
 * `restoreSession` and therefore every caller all land here.
 */
function refreshTokens(): Promise<AuthResponse | null> {
  if (pendingRefresh !== null) return pendingRefresh;
  pendingRefresh = withCrossTabLock(postRefreshToken).finally(() => {
    pendingRefresh = null;
  });
  return pendingRefresh;
}

/** The network POST itself — the body of `refreshTokens`' single-flight. */
async function postRefreshToken(): Promise<AuthResponse | null> {
  try {
    const response = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Empty body — cookie carries the refresh token.
      body: JSON.stringify({}),
    });
    if (!response.ok) {
      return null;
    }
    const pair = (await response.json()) as AuthResponse;
    tokens.storeAccess(pair.access_token);
    return pair;
  } catch {
    return null;
  }
}

/**
 * Try to restore a session from the refresh cookie. Called by AuthContext on
 * page load — if the cookie is valid, we get a fresh access token; otherwise
 * the user is logged out.
 *
 * Joins an in-flight refresh rather than starting its own (see
 * pendingRefresh): the mount probe and the SSE reconnect both call this, and
 * two POSTs carrying one cookie is the defect, not the fix.
 */
export async function restoreSession(): Promise<AuthResponse | null> {
  const pair = await refreshTokens();
  return pair;
}

/** Unwraps JSON on 2xx; raises the `{detail, code}` error envelope otherwise. */
async function parse<T>(response: Response): Promise<T> {
  if (response.ok) {
    if (response.status === 204 || response.headers.get("content-length") === "0") {
      return undefined as T;
    }
    return (await response.json()) as T;
  }
  return parseError(response);
}

/** Parses a non-2xx response into the `{detail, code}` error envelope. */
async function parseError(response: Response): Promise<never> {
  let detail = response.statusText || "Request failed";
  let code: string | null = null;
  try {
    const payload = (await response.json()) as { detail?: unknown; code?: unknown };
    if (typeof payload.detail === "string") {
      detail = payload.detail;
    }
    if (typeof payload.code === "string") {
      code = payload.code;
    }
  } catch {
    // Non-JSON error body: fall back to the status text above.
  }
  throw new ApiError(response.status, detail, code);
}

/** Single multipart POST attempt with bearer auth. */
async function sendForm(
  path: string,
  form: FormData,
  init: RequestInit,
): Promise<Response> {
  const headers = new Headers(init.headers);
  const access = tokens.access;
  if (access) {
    headers.set("Authorization", `Bearer ${access}`);
  }
  return fetch(`${BASE}${path}`, {
    method: init.method ?? "POST",
    headers,
    body: form,
  });
}

/**
 * Multipart POST (CSV uploads, etc.) reusing `api`'s 401 → refresh → replay
 * machinery and the `{detail, code}` envelope. Throws `ApiError` on non-2xx.
 */
export async function apiForm<T>(
  path: string,
  form: FormData,
  init: RequestInit = {},
): Promise<T> {
  const response = await withAuthRetry(() => sendForm(path, form, init), true, path);
  return parse<T>(response);
}

/** Single authed fetch attempt for non-JSON (CSV/text/blob) responses. */
async function sendBinary(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers);
  const access = tokens.access;
  if (access) {
    headers.set("Authorization", `Bearer ${access}`);
  }
  return fetch(`${BASE}${path}`, {
    method: init.method ?? "GET",
    headers,
    body: init.body,
  });
}

/**
 * Authed GET/POST for binary/text/download endpoints. Parses the `{detail, code}`
 * error envelope and throws `ApiError` on non-2xx; on success returns the raw
 * `Response` so the caller can read `.json()` / `.blob()` / `.text()`.
 */
export async function apiBinary(path: string, init: RequestInit = {}): Promise<Response> {
  return withAuthRetry(() => sendBinary(path, init), true, path);
}

// --- Typed auth helpers used by pages ---------------------------------------

/**
 * POST /auth/register (M87, docs/29 F15).
 *
 * Deliberately NOT a session: the endpoint answers the identical token-free
 * body for a free and for a taken address, because a `201 + token` vs
 * `409 duplicate_email` split is an account-existence oracle for anyone on
 * the internet. A free address does have the account created — the caller
 * signs in next with the password it just chose.
 */
export async function register(email: string, password: string): Promise<RegisterAccepted> {
  return api<RegisterAccepted>("/auth/register", {
    method: "POST",
    body: { email, password },
  });
}

/**
 * Password login (docs/38 "Login + challenge").
 *
 * The endpoint now answers with a UNION: a normal `AuthResponse`, or — when the
 * account has an active TOTP factor — a `LoginChallenge` (200, no session, no
 * cookie). The branch is the `challenge` field, so an existing caller that
 * only ever sees pairs keeps working. The access token is stored ONLY for a
 * real pair: a challenge carries no session to store.
 */
export async function login(
  email: string,
  password: string,
): Promise<AuthResponse | LoginChallenge> {
  const result = await api<AuthResponse | LoginChallenge>("/auth/login", {
    method: "POST",
    body: { email, password },
  });
  if ("challenge" in result) {
    return result;
  }
  tokens.storeAccess(result.access_token);
  return result;
}

// --- M74 second factor (docs/38) --------------------------------------------

/** POST /auth/login/totp — completes a password login challenged with TOTP. */
export async function loginWithTotp(
  challengeToken: string,
  code: string,
): Promise<AuthResponse> {
  const result = await api<AuthResponse>("/auth/login/totp", {
    method: "POST",
    body: { challenge_token: challengeToken, code },
  });
  tokens.storeAccess(result.access_token);
  return result;
}

/** POST /auth/login/recovery — same, with a single-use recovery code. */
export async function loginWithRecovery(
  challengeToken: string,
  code: string,
): Promise<AuthResponse> {
  const result = await api<AuthResponse>("/auth/login/recovery", {
    method: "POST",
    body: { challenge_token: challengeToken, code },
  });
  tokens.storeAccess(result.access_token);
  return result;
}

/** GET /auth/totp/status */
export function totpStatus(): Promise<TotpStatus> {
  return api<TotpStatus>("/auth/totp/status");
}

/** POST /auth/totp/setup — stashes a PENDING secret; active factor untouched. */
export function totpSetup(): Promise<TotpSetupResponse> {
  return api<TotpSetupResponse>("/auth/totp/setup", { method: "POST" });
}

/** POST /auth/totp/confirm — activates the pending secret, returns 10 codes ONCE. */
export function totpConfirm(code: string): Promise<TotpConfirmResponse> {
  return api<TotpConfirmResponse>("/auth/totp/confirm", {
    method: "POST",
    body: { code },
  });
}

/** POST /auth/totp/disable — `code` may be a TOTP code or a recovery code. 204. */
export function totpDisable(code: string): Promise<void> {
  return api<void>("/auth/totp/disable", { method: "POST", body: { code } });
}

// --- M74 passkeys (docs/38) -------------------------------------------------

/** GET /auth/passkeys */
export function listPasskeys(): Promise<PasskeyPage> {
  return api<PasskeyPage>("/auth/passkeys");
}

/** POST /auth/passkeys/options — creation options for `navigator.credentials.create`. */
export function passkeyRegisterOptions(): Promise<PasskeyOptionsResponse> {
  return api<PasskeyOptionsResponse>("/auth/passkeys/options", { method: "POST" });
}

/** POST /auth/passkeys — verify the produced credential (201). */
export function passkeyRegister(
  challengeToken: string,
  credential: unknown,
  name?: string,
): Promise<PasskeyInfo> {
  return api<PasskeyInfo>("/auth/passkeys", {
    method: "POST",
    body: { challenge_token: challengeToken, credential, name },
  });
}

/** DELETE /auth/passkeys/{id} — 204; a foreign id is 404, never a probe-able 403. */
export function deletePasskey(id: number): Promise<void> {
  return api<void>(`/auth/passkeys/${id}`, { method: "DELETE" });
}

/**
 * POST /auth/passkeys/login/options — the SAME 200 challenge envelope for an
 * email with passkeys, one without, and one with no account (M87, docs/38).
 * The no-credential case carries a throwaway challenge, so the ceremony is
 * what fails: `getPasskey` returns null on NotAllowedError and sign-in ends
 * silently, exactly like dismissing the prompt.
 *
 * Called with NO email (or a blank one) it starts the DISCOVERABLE ceremony
 * instead (docs/38 §Passkeys): an empty `allowCredentials` list and a
 * challenge bound to no account, so the browser offers every discoverable
 * credential it holds and the server resolves the account from the verified
 * assertion — sign-in without typing an address.
 */
export function passkeyLoginOptions(email?: string): Promise<PasskeyOptionsResponse> {
  return api<PasskeyOptionsResponse>("/auth/passkeys/login/options", {
    method: "POST",
    body: email ? { email } : {},
  });
}

/** POST /auth/passkeys/login — a WebAuthn assertion IS the second factor. */
export async function passkeyLogin(
  challengeToken: string,
  credential: unknown,
): Promise<AuthResponse> {
  const result = await api<AuthResponse>("/auth/passkeys/login", {
    method: "POST",
    body: { challenge_token: challengeToken, credential },
  });
  tokens.storeAccess(result.access_token);
  return result;
}

// --- M74 OIDC SSO (docs/38) -------------------------------------------------

/** The 302 the browser must FOLLOW, not fetch — navigate to it directly. */
export const OIDC_LOGIN_PATH = "/api/v1/auth/oidc/login";

/** GET /auth/oidc/config — always 200, so no error path on the login page. */
export function oidcConfig(): Promise<OidcConfigResponse> {
  return api<OidcConfigResponse>("/auth/oidc/config");
}

/** GET /auth/oidc/preview — 401 `oidc_pending_expired` when the cookie is gone. */
export function oidcPreview(): Promise<OidcPreview> {
  return api<OidcPreview>("/auth/oidc/preview");
}

/** POST /auth/oidc/confirm — no body; the `oidc_pending` cookie is the payload. */
export async function oidcConfirm(): Promise<AuthResponse> {
  const result = await api<AuthResponse>("/auth/oidc/confirm", { method: "POST" });
  tokens.storeAccess(result.access_token);
  return result;
}

/** One-click demo: POST /auth/demo seeds (first call) and logs in (docs/09). */
export async function demoLogin(): Promise<AuthResponse> {
  const result = await api<AuthResponse>("/auth/demo", { method: "POST" });
  tokens.storeAccess(result.access_token);
  return result;
}

/** Revokes the presented refresh token server-side (cookie auto-sent) and drops local access token. */
export async function logout(): Promise<void> {
  try {
    await fetch(`${BASE}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } finally {
    tokens.clear();
  }
}

export function me(): Promise<User> {
  return api<User>("/users/me");
}
