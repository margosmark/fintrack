// Node-side API helpers: the E2E suite cross-checks rendered money against the
// real payloads the browser consumed (same server, same seed), and creates its
// own disposable tenant for every mutation / non-demo assertion.

export const APP = process.env.LEDGER_E2E_APP || "http://127.0.0.1:8100";
export const API = process.env.LEDGER_E2E_API || "http://127.0.0.1:8000";

/** Authorization scheme prefix for a JWT header (never a secret itself). */
const BEARER_PREFIX = "Bearer ";

/** Live bases. `--app` / `--api` are parsed by run.mjs AFTER this module is
 *  imported, so a const snapshot would honour the flags only for the browser
 *  half and leave every Node-side call knocking on the default :8000 —
 *  observed as suite-aborting ECONNREFUSED while the page itself was fine.
 *  Resolve the env at CALL time; run.mjs exports LEDGER_E2E_* from its flags. */
const apiBase = () => process.env.LEDGER_E2E_API || API;
const appBase = () => process.env.LEDGER_E2E_APP || APP;
export { apiBase, appBase };

export async function req(token, path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(apiBase() + path, {
      method,
      headers: {
        "content-type": "application/json",
        ...(token ? { authorization: BEARER_PREFIX + token } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // `TypeError: fetch failed` alone is undiagnosable — surface the socket
    // cause (ECONNRESET on a stale keep-alive vs ECONNREFUSED vs EMFILE) so a
    // red says WHICH, not just that the round trip did not happen.
    const cause = err?.cause;
    throw new Error(
      `fetch failed: ${method} ${path} -> ${apiBase()} :: ${cause?.code || cause?.message || err?.message || err}` +
        (cause?.message && cause?.code ? ` (${cause.message})` : ""),
    );
  }
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* 204 / non-json */
  }
  return { status: res.status, json };
}

export async function demoToken() {
  const { status, json } = await req(null, "/api/v1/auth/demo", { method: "POST", body: {} });
  if (status !== 200 || !json?.access_token) {
    throw new Error(`demo login failed: ${status} ${JSON.stringify(json)}`);
  }
  return json.access_token;
}

export async function registerTenant(email, password) {
  const { status, json } = await req(null, "/api/v1/auth/register", {
    method: "POST",
    body: { email, password },
  });
  if (status !== 200 && status !== 201) {
    throw new Error(`register failed: ${status} ${JSON.stringify(json)}`);
  }
  return { email, password };
}

export async function loginToken(email, password) {
  const { status, json } = await req(null, "/api/v1/auth/login", {
    method: "POST",
    body: { email, password },
  });
  if (status !== 200 || !json?.access_token) {
    throw new Error(`login failed: ${status} ${JSON.stringify(json)}`);
  }
  return json.access_token;
}

/** Tear a disposable tenant down (docs/20 DELETE /users/me cascade — the
 *  endpoint re-authenticates with the account password in the body). */
export async function deleteTenant(token, password) {
  const { status } = await req(token, "/api/v1/users/me", {
    method: "DELETE",
    body: { current_password: password },
  });
  return status;
}

// ---- money formatting (mirrors frontend/src/lib/format.ts expectations) ----
const CAD = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "CAD",
  minimumFractionDigits: 2,
});
const USD = new Intl.NumberFormat("en-CA", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
});

export function money(value, currency = "CAD") {
  const n = Number(value);
  return currency === "USD" ? USD.format(n) : CAD.format(n);
}

/** "$1,518.63" | "-$1,086.51" | "US$58,434.73" -> number (null when unparseable) */
export function parseMoney(text) {
  if (typeof text !== "string") return null;
  const cleaned = text.replace(/US\$/g, "").replace(/[$,\s]/g, "");
  if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return null;
  return Number(cleaned);
}
