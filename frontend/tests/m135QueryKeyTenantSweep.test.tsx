// M135 §3.4-4a — the stale-cache sweep for React Query.
//
// Two different caches can serve a removed or restored feature wrongly; this
// file owns the React Query half.
//
//   1. TENANT SCOPE. The cache is per-browser, not per-user: the app's keys
//      are NOT user-scoped (`["accounts", 200]`, `["transactions","all-rows"]`
//      and friends are shared across identities), so nothing but eviction at
//      the auth seam separates two tenants. `tests/AuthContext.test.tsx` pins
//      that seam with a handful of hand-picked keys — this file ENUMERATES
//      every key the source declares, so a NEW key added anywhere is swept
//      automatically and a leak cannot arrive through a key nobody remembered
//      to test.
//
//   2. LOCK STATE. docs/33 §Server-first rule 7c: a query whose ANSWER depends
//      on the vault's lock state must carry that state in its key, or the
//      locked answer survives the unlock. M156's §3.4-5 remedy exercise
//      proved one such query missed it (`pages/Transactions.tsx`'s list
//      query) — the A18 notice's own inline unlock re-rendered against the
//      locked page still cached, and the withheld rows never came back. This
//      sweep makes that class enumerable too.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import type { User } from "../src/types";

const { restoreSession, demoLogin, logout } = vi.hoisted(() => ({
  restoreSession: vi.fn<() => Promise<unknown>>(),
  demoLogin: vi.fn<() => Promise<unknown>>(),
  logout: vi.fn<() => Promise<void>>(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession, demoLogin, logout };
});

import { AuthProvider, useAuth } from "../src/auth/AuthContext";

const SRC = "src";

// ── enumeration ────────────────────────────────────────────────────────────

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      out.push(...sourceFiles(path));
    } else if (/\.tsx?$/.test(entry) && !/\.test\./.test(entry)) {
      out.push(path);
    }
  }
  return out.sort();
}

/** Split an array literal into its top-level elements. */
function splitTopLevel(body: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of body) {
    if (ch === "[" || ch === "(" || ch === "{") depth += 1;
    if (ch === "]" || ch === ")" || ch === "}") depth -= 1;
    if (ch === "," && depth === 0) {
      parts.push(current.trim());
      current = "";
      continue;
    }
    current += ch;
  }
  if (current.trim() !== "") parts.push(current.trim());
  return parts;
}

const LITERAL = /^("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)$/;

/**
 * Turn a key's source text into a value: literals keep their value, anything
 * else (an id, a filter object, the user) collapses to a stable placeholder —
 * the SHAPE is what eviction has to cover, and `removeQueries()` is shape
 * agnostic, so a placeholder exercises the same invariant.
 */
function keyFromSource(text: string): unknown[] {
  // `arrayAt` hands over the WHOLE `[ … ]`; only the contents are elements.
  const inner = text.trim().replace(/^\[/, "").replace(/\]$/, "");
  return splitTopLevel(inner).map((part) => {
    if (LITERAL.test(part)) {
      if (part.startsWith('"')) return JSON.parse(part) as string;
      if (part === "true") return true;
      if (part === "false") return false;
      if (part === "null") return null;
      return Number(part);
    }
    return `«${part.replace(/\s+/g, " ").slice(0, 40)}»`;
  });
}

/** The balanced `[ … ]` starting at `open`, or null when it never closes. */
function balancedArray(text: string, open: number): string | null {
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "[" || ch === "(" || ch === "{") depth += 1;
    else if (ch === "]" || ch === ")" || ch === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(open, i + 1);
    }
  }
  return null;
}

/**
 * The balanced `[ … ]` that BEGINS at `from` (whitespace skipped). Anything
 * else is not an array literal at this position, so the caller can tell a
 * real `queryKey: [...]` from a `queryKey: SOME_CONSTANT` reference without
 * accidentally pairing it with a bracket from a later statement.
 */
function arrayAt(text: string, from: number): string | null {
  const open = from + (text.slice(from).match(/^\s*/) ?? [""])[0].length;
  if (text[open] !== "[") return null;
  return balancedArray(text, open);
}

/** Every `queryKey` the tree declares, as a JSON-able key array. */
function enumerateQueryKeys(): unknown[][] {
  // `keyFromSource` collapses a non-literal element (an id, a filter object,
  // the user) to `«…»` — and a nested array is not a literal either, so it
  // arrives as a STRING, e.g. `["accounts", "«[\"partner\", \"shared\"]»"]`
  // for `["accounts", ["partner", "shared"]]`. Parse those back, because a
  // key of strings and a key of arrays are different cache entries and the
  // sweep has to seed the real shape.
  const keys: unknown[][] = [];
  const seen = new Set<string>();
  const add = (source: string) => {
    const key = keyFromSource(source).map((element) => {
      if (typeof element === "string" && element.startsWith("«[") && element.endsWith("»")) {
        try {
          return JSON.parse(element.slice(1, -1)) as unknown;
        } catch {
          return element;
        }
      }
      return element;
    });
    const id = JSON.stringify(key);
    if (!seen.has(id)) {
      seen.add(id);
      keys.push(key);
    }
  };

  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, "utf8");
    // A module-level `const X = [...]` that a `queryKey: X` reference points at.
    const constants = new Map<string, string>();
    for (const m of text.matchAll(/^const\s+([A-Za-z0-9_$]+)\s*=\s*/gm)) {
      const array = arrayAt(text, m.index + m[0].length);
      if (array !== null) constants.set(m[1], array);
    }
    for (const m of text.matchAll(/queryKey:/g)) {
      const array = arrayAt(text, m.index + m[0].length);
      if (array !== null) {
        add(array);
        continue;
      }
      const name = /^[\s]*([A-Za-z0-9_$]+)/.exec(text.slice(m.index + m[0].length));
      const resolved = name === null ? undefined : constants.get(name[1]);
      if (resolved !== undefined) add(resolved);
    }
    // `cachedInput(key, fn)` — the shared reports/s3 input cache.
    for (const m of text.matchAll(/cachedInput\(/g)) {
      const array = arrayAt(text, m.index + m[0].length);
      if (array !== null) add(array);
    }
  }
  return keys;
}

/** The balanced `{ … }` object passed to `useQuery({`. */
function useQueryBlocks(text: string): Array<{ key: string; body: string }> {
  const blocks: Array<{ key: string; body: string }> = [];
  let from = 0;
  for (;;) {
    const at = text.indexOf("useQuery({", from);
    if (at < 0) break;
    let depth = 0;
    let i = at + "useQuery({".length - 1;
    for (; i < text.length; i += 1) {
      if (text[i] === "{") depth += 1;
      else if (text[i] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    blocks.push({ key: text.slice(at, i + 1), body: text.slice(at, i + 1) });
    from = i + 1;
  }
  return blocks;
}

// ── the AuthProvider harness (the same seam AuthContext.test.tsx pins) ──────

const makeUser = (id: string, email: string): User => ({
  id,
  email,
  name: null,
  privacy_mode: false,
  currency_tracker_enabled: true,
  llm_provider: "none",
  llm_base_url: null,
  llm_model: "",
  encryption_required: false,
});

const alice = makeUser("1", "alice@example.com");
const bob = makeUser("2", "bob@example.com");
const authPair = (user: User) => ({
  access_token: `token-${user.id}`,
  refresh_token: "unused",
  token_type: "bearer" as const,
  user,
});

let actions: { demoLogin: () => Promise<void> } | null = null;

function Probe() {
  const auth = useAuth();
  useEffect(() => {
    actions = { demoLogin: () => auth.demoLogin() };
  }, [auth.demoLogin]);
  // A mounted observer, so the cache holds a LIVE entry the way the app does.
  useQuery({ queryKey: ["__probe__"], queryFn: async () => "probe", staleTime: Infinity });
  return null;
}

function cacheIds(client: QueryClient): string[] {
  return client.getQueryCache().getAll().map((q) => JSON.stringify(q.queryKey));
}

afterEach(() => {
  actions = null;
});

describe("§3.4-4a(1) — a logout→login can never serve the previous tenant's rows", () => {
  it("sweeps EVERY query key the source declares", async () => {
    const keys = enumerateQueryKeys();
    // The enumeration must not be able to quietly match nothing.
    expect(keys.length).toBeGreaterThan(40);
    // The two keys the M135 card named by hand are in the sweep.
    expect(keys).toContainEqual(["accounts", 200]);
    expect(keys).toContainEqual(["transactions", "all-rows"]);

    restoreSession.mockResolvedValue(authPair(alice));
    demoLogin.mockResolvedValue(authPair(bob));
    logout.mockResolvedValue(undefined);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(actions).not.toBeNull());

    // Alice's tenant: seed every production key with a tenant marker.
    await waitFor(() => expect(cacheIds(client)).toContain(JSON.stringify(["__probe__"])));
    for (const key of keys) client.setQueryData(key, `alice:${JSON.stringify(key)}`);
    expect(keys.every((key) => client.getQueryData(key) !== undefined)).toBe(true);

    // Bob signs in on the same page. Every one of Alice's entries must be
    // unreachable — not re-keyed, not merely stale.
    await actDemoLogin();
    const survivors = keys.filter((key) => client.getQueryData(key) !== undefined);
    expect({ survivors, swept: keys.length }).toEqual({ survivors: [], swept: keys.length });
  });
});

async function actDemoLogin(): Promise<void> {
  await act(async () => {
    await actions!.demoLogin();
  });
}

describe("§3.4-4a(2) — a lock-dependent query carries the lock state in its key", () => {
  it("sweeps every useQuery whose answer depends on the vault", () => {
    const offenders: string[] = [];
    let checked = 0;
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, "utf8");
      if (!/useQuery\(\{/.test(text)) continue;
      for (const block of useQueryBlocks(text)) {
        // The ANSWER depends on the lock state when the query function reads
        // it. Rule 7c: that state must be part of the key, or the locked
        // answer survives the unlock (and vice versa).
        const queryFn = block.body.slice(block.body.indexOf("queryFn"));
        if (!/\b(vaultUnlocked|isUnlocked\(\))/.test(queryFn)) continue;
        checked += 1;
        if (!/vaultUnlocked/.test(block.body.slice(0, block.body.indexOf("queryFn") || 0))) {
          offenders.push(`${file}: ${block.key.replace(/\s+/g, " ").slice(0, 160)}`);
        }
      }
    }
    // The sweep must actually be looking at something.
    expect(checked).toBeGreaterThanOrEqual(2);
    expect(offenders).toEqual([]);
  });
});
