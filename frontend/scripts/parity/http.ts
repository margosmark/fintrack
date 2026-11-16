// Minimal HTTP client for the parity capture script (test infra only).
//
// Tokens never reach stdout/stderr: nothing here logs a response body on the
// login path, and callers print statuses + counts only.

import type { Json } from "./types.ts";

export interface RawResponse {
  status: number;
  body: Json;
}

export async function request(
  method: string,
  url: string,
  opts: { token?: string; body?: Json } = {},
): Promise<RawResponse> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.token !== undefined) headers.Authorization = `Bearer ${opts.token}`;
  let payload: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(opts.body);
  }

  let res: Response;
  try {
    res = await fetch(url, { method, headers, body: payload });
  } catch (error) {
    throw new Error(`${method} ${url} failed: ${error instanceof Error ? error.message : String(error)}`);
  }

  const text = await res.text();
  let body: Json;
  if (text === "") {
    body = null;
  } else {
    try {
      body = JSON.parse(text) as Json;
    } catch {
      body = text;
    }
  }
  return { status: res.status, body };
}

/** Same-origin API helper bound to one bearer token.
 *  (Plain fields, not parameter properties — Node's type stripping only
 *   erases types, it cannot rewrite constructor bodies.) */
export class Api {
  private readonly baseUrl: string;
  private readonly token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  get(path: string): Promise<RawResponse> {
    return request("GET", `${this.baseUrl}${path}`, { token: this.token });
  }

  post(path: string, body: Json): Promise<RawResponse> {
    return request("POST", `${this.baseUrl}${path}`, { token: this.token, body });
  }

  put(path: string, body: Json): Promise<RawResponse> {
    return request("PUT", `${this.baseUrl}${path}`, { token: this.token, body });
  }

  delete(path: string, body: Json): Promise<RawResponse> {
    return request("DELETE", `${this.baseUrl}${path}`, { token: this.token, body });
  }

  /**
   * Page through a `{items, total, limit, offset}` list endpoint the way the
   * frontend loaders do (200/page, stop on empty page or when offset >= total).
   * Returns the merged `items` array.
   */
  async paged(path: string, limit = 200): Promise<Json[]> {
    const items: Json[] = [];
    let offset = 0;
    const sep = path.includes("?") ? "&" : "?";
    for (let page = 0; page < 500; page += 1) {
      const res = await this.get(`${path}${sep}limit=${limit}&offset=${offset}`);
      if (res.status !== 200) {
        throw new Error(`GET ${path} returned ${res.status} (expected 200)`);
      }
      const body = res.body;
      if (body === null || typeof body !== "object" || Array.isArray(body)) {
        throw new Error(`GET ${path} did not return a page object`);
      }
      const batch = (body as { items?: Json }).items;
      if (!Array.isArray(batch)) {
        throw new Error(`GET ${path} page has no items[]`);
      }
      items.push(...batch);
      offset += batch.length;
      const total = (body as { total?: Json }).total;
      if (batch.length === 0 || (typeof total === "number" && offset >= total)) break;
    }
    return items;
  }
}

/** Demo session token. Never printed, never written to disk. */
export async function demoToken(baseUrl: string): Promise<string> {
  const res = await request("POST", `${baseUrl}/auth/demo`, { body: {} });
  if (res.status !== 200) {
    throw new Error(`POST /auth/demo returned ${res.status} (expected 200)`);
  }
  const token = (res.body as { access_token?: Json } | null)?.access_token;
  if (typeof token !== "string" || token.length === 0) {
    throw new Error("POST /auth/demo returned no access_token");
  }
  return token;
}

export function requireAccessToken(res: RawResponse, what: string): string {
  if (res.status !== 201 && res.status !== 200) {
    throw new Error(`${what} returned ${res.status}`);
  }
  const token = (res.body as { access_token?: Json } | null)?.access_token;
  if (typeof token !== "string" || token.length === 0) {
    throw new Error(`${what} returned no access_token`);
  }
  return token;
}
