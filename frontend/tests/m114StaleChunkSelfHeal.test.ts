// @vitest-environment node
// M114 — the ⌘K palette's stale-chunk 404 (docs/09 §Installable PWA).
//
// The bug: `CommandPalette` is its own lazy chunk (M95), fetched only when the
// user OPENS the palette — which on a phone happens after the deploy that
// replaced its hash. The worker used to answer `/assets/*` cache-first with no
// 404 handling, so the stale document's import hit the network, got a 404 from
// the new image, and Vite's error boundary showed
// "Unexpected Application Error! error loading dynamically imported module
// …/assets/CommandPalette-DMeurLAO.js".
//
// These tests run the REAL `public/sw.js` inside a `node:vm` worker with an
// in-memory CacheStorage and a swappable origin (a deploy = the file table
// changes under the running worker), so the deploy-sim asserts behaviour, not
// source text: install → deploy → palette import → self-heal. The marker that
// replaces the 404 is then evaluated on its own to prove the heal is BOUNDED
// (one reload per guard window) rather than a reload loop. The client half
// The client half (`src/pwa.ts`) lives in tests/pwaStaleShellClient.test.ts,
// where jsdom supplies the window/document/sessionStorage this file has no use for.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import vm from "node:vm";
import { describe, expect, it } from "vitest";

import {
  RELOAD_GUARD_KEY,
  RELOAD_GUARD_MS,
  RELOAD_MESSAGE,
  assetRefsFromHtml,
  guardedReload,
  shellRefsDiffer,
} from "../src/pwa";
import { buildIdFromIndexHtml, stampSw } from "../scripts/stamp-sw.mjs";

const ORIGIN = "http://localhost";
const SW_SOURCE = readFileSync(path.join(process.cwd(), "public", "sw.js"), "utf8");
const PWA_SOURCE = readFileSync(path.join(process.cwd(), "src", "pwa.ts"), "utf8");

type Asset = { status: number; body: string; type: string };

type Server = {
  files: Map<string, Asset>;
  hits: string[];
  offline: boolean;
  fetch: (input: string | { url: string }) => Promise<Response>;
};

type SwRequest = { method: string; mode: string; url: string };
type SwEvent = {
  request: SwRequest;
  respondWith: (response: Response | Promise<Response>) => void;
  waitUntil: (promise: Promise<unknown>) => void;
};
type SwListener = (event: SwEvent) => void;

function toResponse(asset: Asset): Response {
  return new Response(asset.body, {
    status: asset.status,
    headers: { "Content-Type": asset.type },
  });
}

function script(name: string): Asset {
  return { status: 200, body: `/* ${name} */\nexport default {};\n`, type: "text/javascript" };
}

function shellHtml(entry: string, css: string): string {
  return `<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <link rel="stylesheet" crossorigin href="/assets/${css}" />
    <script type="module" crossorigin src="/assets/${entry}"></script>
  </head>
  <body><div id="root"></div></body>
</html>
`;
}

const SHELL_FILES = [
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

function createServer(): Server {
  const server: Server = {
    files: new Map(),
    hits: [],
    offline: false,
    fetch: async () => {
      throw new TypeError("unreachable");
    },
  };
  server.fetch = async (input) => {
    if (server.offline) throw new TypeError("network down");
    const raw = typeof input === "string" ? input : input.url;
    const url = new URL(raw, ORIGIN);
    server.hits.push(url.pathname);
    const hit = server.files.get(url.pathname);
    if (!hit) {
      return new Response("<html>404 page not found</html>", {
        status: 404,
        headers: { "Content-Type": "text/html" },
      });
    }
    return toResponse(hit);
  };
  for (const file of SHELL_FILES) {
    server.files.set(file, { status: 200, body: "shell", type: "text/html" });
  }
  return server;
}

/** Deploy A: the shell the phone loaded (palette chunk present, unopened). */
function serveDeployA(server: Server): void {
  server.files.set("/index.html", {
    status: 200,
    body: shellHtml("index-A.js", "index-A.css"),
    type: "text/html",
  });
  server.files.set("/assets/index-A.js", script("index-A.js"));
  server.files.set("/assets/index-A.css", { status: 200, body: "body{}", type: "text/css" });
  server.files.set("/assets/CommandPalette-A.js", script("CommandPalette-A.js"));
}

/** Deploy B: the image is replaced — every A asset is gone from the server. */
function deployB(server: Server): void {
  for (const key of [...server.files.keys()]) {
    if (key.startsWith("/assets/")) server.files.delete(key);
  }
  server.files.set("/index.html", {
    status: 200,
    body: shellHtml("index-B.js", "index-B.css"),
    type: "text/html",
  });
  server.files.set("/assets/index-B.js", script("index-B.js"));
  server.files.set("/assets/index-B.css", { status: 200, body: "body{color:red}", type: "text/css" });
  server.files.set("/assets/CommandPalette-B.js", script("CommandPalette-B.js"));
}

class FakeCache {
  private readonly store = new Map<string, Asset>();
  constructor(private readonly server: Server) {}
  private key(request: string | { url: string }): string {
    return typeof request === "string" ? new URL(request, ORIGIN).href : request.url;
  }
  async match(request: string | { url: string }): Promise<Response | undefined> {
    const hit = this.store.get(this.key(request));
    return hit ? toResponse(hit) : undefined;
  }
  async put(request: string | { url: string }, response: Response): Promise<void> {
    const body = await response.text();
    this.store.set(this.key(request), {
      status: response.status,
      body,
      type: response.headers.get("content-type") ?? "text/plain",
    });
  }
  async add(request: string | { url: string }): Promise<void> {
    const response = await this.server.fetch(request);
    if (!response.ok) throw new Error(`cache.add ${this.key(request)} -> ${response.status}`);
    await this.put(request, response);
  }
  async delete(request: string | { url: string }): Promise<boolean> {
    return this.store.delete(this.key(request));
  }
  keys(): string[] {
    return [...this.store.keys()];
  }
}

class FakeCacheStorage {
  private readonly caches = new Map<string, FakeCache>();
  constructor(private readonly server: Server) {}
  async open(name: string): Promise<FakeCache> {
    let cache = this.caches.get(name);
    if (!cache) {
      cache = new FakeCache(this.server);
      this.caches.set(name, cache);
    }
    return cache;
  }
  async keys(): Promise<string[]> {
    return [...this.caches.keys()];
  }
  async delete(name: string): Promise<boolean> {
    return this.caches.delete(name);
  }
  async match(request: string | { url: string }): Promise<Response | undefined> {
    for (const cache of this.caches.values()) {
      const hit = await cache.match(request);
      if (hit) return hit;
    }
    return undefined;
  }
  peek(name: string): FakeCache | undefined {
    return this.caches.get(name);
  }
  names(): string[] {
    return [...this.caches.keys()];
  }
}

type Worker = {
  listeners: Map<string, SwListener[]>;
  posted: { type: string; build: string }[];
  skipped: number;
  claimed: number;
  caches: FakeCacheStorage;
};

function loadWorker(source: string, server: Server, storage?: FakeCacheStorage): Worker {
  const worker: Worker = {
    listeners: new Map(),
    posted: [],
    skipped: 0,
    claimed: 0,
    caches: storage ?? new FakeCacheStorage(server),
  };
  const selfObject = {
    location: new URL("/sw.js", ORIGIN),
    addEventListener(type: string, listener: SwListener) {
      const bucket = worker.listeners.get(type) ?? [];
      bucket.push(listener);
      worker.listeners.set(type, bucket);
    },
    skipWaiting: async () => {
      worker.skipped += 1;
    },
    clients: {
      claim: async () => {
        worker.claimed += 1;
      },
      matchAll: async () => [
        { postMessage: (message: { type: string; build: string }) => worker.posted.push(message) },
      ],
    },
  };
  // A worker's `new Request("/manifest.json")` resolves against self.location;
  // Node/jsdom's Request has no base, so the harness supplies one.
  class WorkerRequest extends Request {
    constructor(input: string | URL | Request, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, ORIGIN).href : input, init);
    }
  }
  const context = vm.createContext({
    self: selfObject,
    caches: worker.caches,
    fetch: server.fetch,
    Request: WorkerRequest,
    Response,
    URL,
    console,
  });
  vm.runInContext(source, context, { filename: "sw.js" });
  return worker;
}

function event(overrides: Partial<SwEvent>): SwEvent {
  return {
    request: { method: "GET", mode: "cors", url: new URL("/", ORIGIN).href },
    respondWith: () => {},
    waitUntil: () => {},
    ...overrides,
  };
}

function dispatch(worker: Worker, type: string, swEvent: SwEvent): void {
  const listeners = worker.listeners.get(type) ?? [];
  expect(listeners.length, `${type} listener`).toBeGreaterThan(0);
  for (const listener of listeners) listener(swEvent);
}

async function install(worker: Worker): Promise<void> {
  let done: Promise<unknown> | undefined;
  dispatch(worker, "install", event({ waitUntil: (promise) => (done = promise) }));
  await done;
}

async function activate(worker: Worker): Promise<void> {
  let done: Promise<unknown> | undefined;
  dispatch(worker, "activate", event({ waitUntil: (promise) => (done = promise) }));
  await done;
}

async function fetchAs(
  worker: Worker,
  url: string,
  mode = "cors",
): Promise<Response | undefined> {
  let responded: Promise<Response> | undefined;
  dispatch(
    worker,
    "fetch",
    event({
      request: { method: "GET", mode, url: new URL(url, ORIGIN).href },
      respondWith: (response) => (responded = Promise.resolve(response)),
    }),
  );
  return responded ? await responded : undefined;
}

/** Stamp a copy of the worker exactly the way `npm run build` does. */
function stampedWorker(indexHtml: string): { source: string; id: string; cacheName: string } {
  const dir = mkdtempSync(path.join(tmpdir(), "m114-sw-"));
  const swPath = path.join(dir, "sw.js");
  const indexPath = path.join(dir, "index.html");
  writeFileSync(swPath, SW_SOURCE);
  writeFileSync(indexPath, indexHtml);
  try {
    const id = stampSw({ swPath, indexPath });
    return {
      source: readFileSync(swPath, "utf8"),
      id,
      cacheName: `fintrack-shell-v1-${id}`,
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const INDEX_A = shellHtml("index-A.js", "index-A.css");
const INDEX_B = shellHtml("index-B.js", "index-B.css");

describe("M114 — deploy-sim: a stale shell's palette chunk 404s and self-heals", () => {
  it("answers the 404 with a reload marker, publishes the fresh shell and tells every window", async () => {
    const server = createServer();
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);

    // The palette was never opened before the deploy, so its chunk is in no cache.
    const cache = worker.caches.peek(stamped.cacheName);
    expect(cache?.keys()).toContain(`${ORIGIN}/assets/index-A.js`);
    expect(cache?.keys()).not.toContain(`${ORIGIN}/assets/CommandPalette-A.js`);

    // Deploy: the origin now serves only B's assets.
    deployB(server);
    server.hits.length = 0;

    const response = await fetchAs(worker, "/assets/CommandPalette-A.js");
    expect(response, "the asset branch must intercept the import").toBeDefined();
    expect(response?.status).toBe(200);
    expect(response?.headers.get("content-type")).toContain("javascript");
    const marker = await response!.text();
    expect(marker).toContain("location.reload()");
    expect(marker).toContain(RELOAD_GUARD_KEY);
    expect(marker).toContain(String(RELOAD_GUARD_MS));

    // The worker stored the shell it just proved exists (cache-first for the
    // next navigation / the offline open) …
    const cachedShell = await worker.caches.match("/index.html");
    expect(await cachedShell!.text()).toBe(INDEX_B);

    // … and every window client was told to reload into it.
    expect(worker.posted).toEqual([{ type: RELOAD_MESSAGE, build: stamped.id }]);
    expect(worker.posted[0].build).toMatch(/^[0-9a-f]{12}$/);
  });

  it("never heals when the origin cannot serve a newer shell (keeps the real 404)", async () => {
    const server = createServer();
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);
    deployB(server);

    // /index.html itself is gone: there is nothing to reload into, so the 404
    // must reach the page unchanged rather than start a reload loop.
    server.files.delete("/index.html");
    const response = await fetchAs(worker, "/assets/CommandPalette-A.js");
    expect(response?.status).toBe(404);
    expect(worker.posted).toEqual([]);
  });

  it("keeps /assets cache-first: a served chunk is fetched from the origin exactly once", async () => {
    const server = createServer();
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);

    const first = await fetchAs(worker, "/assets/CommandPalette-A.js");
    expect(first?.status).toBe(200);
    const hitsAfterFirst = server.hits.filter((hit) => hit.includes("CommandPalette-A")).length;
    expect(hitsAfterFirst).toBe(1);

    const second = await fetchAs(worker, "/assets/CommandPalette-A.js");
    expect(second?.status).toBe(200);
    expect(await second!.text()).toBe(await first!.text());
    expect(server.hits.filter((hit) => hit.includes("CommandPalette-A")).length).toBe(
      hitsAfterFirst,
    );
  });

  it("precaches what index.html names, so the NEW cache can still boot offline", async () => {
    const server = createServer();
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);

    const cache = worker.caches.peek(stamped.cacheName);
    expect(cache?.keys()).toEqual(
      expect.arrayContaining([
        `${ORIGIN}/index.html`,
        `${ORIGIN}/assets/index-A.js`,
        `${ORIGIN}/assets/index-A.css`,
        `${ORIGIN}/manifest.json`,
      ]),
    );

    // Offline navigation still lands on the shell (M72 contract, re-proved
    // after the BUILD_ID rotation changed what the fresh cache holds).
    server.offline = true;
    const navigation = await fetchAs(worker, `${ORIGIN}/budgets`, "navigate");
    expect(navigation?.status).toBe(200);
    expect(await navigation!.text()).toBe(INDEX_A);
  });

  it("never intercepts /api — data stays server-backed", async () => {
    const server = createServer();
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);
    server.hits.length = 0;
    const response = await fetchAs(worker, "/api/v1/transactions");
    expect(response).toBeUndefined();
    expect(server.hits).toEqual([]);
  });
});

describe("M114 — BUILD_ID stamping rotates the cache on every deploy", () => {
  it("derives a stable id from the shell and stamps it into CACHE_NAME", () => {
    expect(stampedWorker(INDEX_A).id).toBe(stampedWorker(INDEX_A).id);
    expect(stampedWorker(INDEX_A).id).not.toBe(stampedWorker(INDEX_B).id);
    const stamped = stampedWorker(INDEX_A);
    expect(stamped.source).toContain(`const BUILD_ID = "${stamped.id}"`);
    expect(stamped.source).not.toContain("__BUILD_ID__");
    expect(buildIdFromIndexHtml(INDEX_A)).toBe(stamped.id);
  });

  it("activate purges the previous build's cache (and its dead chunks)", async () => {
    const server = createServer();
    serveDeployA(server);
    const first = stampedWorker(INDEX_A);
    const storage = new FakeCacheStorage(server);
    const oldWorker = loadWorker(first.source, server, storage);
    await install(oldWorker);
    await activate(oldWorker);
    expect(storage.names()).toEqual([first.cacheName]);

    // Deploy B → a new shell, a new id, a new worker: the old cache (with the
    // A chunks no document may name any more) must not survive activation.
    deployB(server);
    const second = stampedWorker(INDEX_B);
    expect(second.cacheName).not.toBe(first.cacheName);
    const newWorker = loadWorker(second.source, server, storage);
    await install(newWorker);
    await activate(newWorker);

    expect(storage.names()).toEqual([second.cacheName]);
    expect(newWorker.claimed).toBe(1);
    expect(newWorker.skipped).toBe(1);
    expect(storage.peek(first.cacheName)).toBeUndefined();
    expect(storage.peek(second.cacheName)?.keys()).toContain(`${ORIGIN}/assets/index-B.js`);
  });
});

describe("M114 — the reload marker is bounded (no reload loop)", () => {
  const evaluate = (marker: string, storage: Map<string, string>, reloads: string[]) => {
    // `export default` is module syntax; the marker is otherwise plain script.
    const runnable = marker.replace("export default ", "");
    const context = vm.createContext({
      sessionStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => void storage.set(key, String(value)),
      },
      location: { reload: () => void reloads.push("reload") },
      Date,
      Number,
      Error,
      console,
    });
    vm.runInContext(runnable, context, { filename: "reload-marker.js" });
    return () => vm.runInContext("StaleShellMarker()", context) as unknown;
  };

  async function markerFrom(server: Server): Promise<string> {
    serveDeployA(server);
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    await install(worker);
    await activate(worker);
    deployB(server);
    const response = await fetchAs(worker, "/assets/CommandPalette-A.js");
    return response!.text();
  }

  it("reloads once for a stale shell, then renders nothing", async () => {
    const marker = await markerFrom(createServer());
    const storage = new Map<string, string>();
    const reloads: string[] = [];
    const component = evaluate(marker, storage, reloads);
    expect(reloads).toEqual(["reload"]);
    expect(storage.get(RELOAD_GUARD_KEY)).toBeDefined();
    expect(component()).toBeNull();
  });

  it("a repeat 404 inside the guard window does NOT reload again — it fails visibly", async () => {
    const marker = await markerFrom(createServer());
    const storage = new Map<string, string>([[RELOAD_GUARD_KEY, String(Date.now() - 10_000)]]);
    const reloads: string[] = [];
    const component = evaluate(marker, storage, reloads);
    expect(reloads).toEqual([]);
    expect(() => component()).toThrow(/stale shell/);
  });

  it("renders nothing when the client half already scheduled the reload", async () => {
    // src/pwa.ts shares RELOAD_GUARD_KEY, so the message path can win the race
    // by a few milliseconds: the marker must not throw into the error
    // boundary in the moment before the navigation replaces the page.
    const marker = await markerFrom(createServer());
    const storage = new Map<string, string>([[RELOAD_GUARD_KEY, String(Date.now() - 500)]]);
    const reloads: string[] = [];
    const component = evaluate(marker, storage, reloads);
    expect(reloads).toEqual([]);
    expect(component()).toBeNull();
  });
});

describe("M114 — client contract (src/pwa.ts, no DOM needed)", () => {
  it("reads shell assets from a document, ignoring anything that merely looks like a path", () => {
    const refs = assetRefsFromHtml(
      `<link rel="stylesheet" href="/assets/index-A.css" />` +
        `<script type="module" crossorigin src="/assets/index-A.js"></script>` +
        `<script src="https://cdn.example.com/other.js"></script>` +
        `<p>see /assets/mentions-A.js for details</p>`,
    );
    expect(refs).toEqual(["/assets/index-A.css", "/assets/index-A.js"]);
  });

  it("calls the shell stale only when BOTH sides are real shells and they differ", () => {
    expect(shellRefsDiffer(INDEX_B, INDEX_A)).toBe(true);
    expect(shellRefsDiffer(INDEX_A, INDEX_A)).toBe(false);
    // A dev document names /src/*, not /assets/* — never a reason to reload.
    expect(shellRefsDiffer("<script type=module src=/src/main.tsx></script>", INDEX_A)).toBe(
      false,
    );
    expect(shellRefsDiffer(INDEX_A, "")).toBe(false);
  });

  it("bounds reloads to one per guard window per tab", () => {
    const store = new Map<string, string>();
    const fake = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
    };
    expect(guardedReload(1_000_000, fake)).toBe(true);
    expect(store.get(RELOAD_GUARD_KEY)).toBe("1000000");
    expect(guardedReload(1_000_000 + RELOAD_GUARD_MS - 1, fake)).toBe(false);
    expect(guardedReload(1_000_000 + RELOAD_GUARD_MS, fake)).toBe(true);
    expect(store.get(RELOAD_GUARD_KEY)).toBe(String(1_000_000 + RELOAD_GUARD_MS));
  });

  it("pins the guard/message literals shared with public/sw.js", () => {
    expect(SW_SOURCE).toContain(`"${RELOAD_GUARD_KEY}"`);
    expect(SW_SOURCE).toContain(`${RELOAD_GUARD_MS}`);
    expect(SW_SOURCE).toContain(`"${RELOAD_MESSAGE}"`);
    expect(PWA_SOURCE).toContain(`"${RELOAD_GUARD_KEY}"`);
    expect(PWA_SOURCE).toContain(`"${RELOAD_MESSAGE}"`);
  });
});
// M135 §3.4-4b — the PWA half of the stale-cache sweep.
//
// Two cache layers can serve a removed or restored feature wrongly; the React
// Query half lives in tests/m135QueryKeyTenantSweep.test.tsx. This block owns
// the other one: the worker is cache-first on `/assets/*` (M114's class), so
// the sweep walks a whole session — cold load, warm load, LOGOUT navigation,
// LOGIN navigation — and checks that each layer is honest about it.
//
// The contract it pins (docs/09 §Installable PWA):
//   * `/assets/*` is cache-first and immutable by name, so a WARM load costs
//     no network — and, because the name is the hash, a cached asset cannot
//     belong to a previous tenant any more than it can belong to a previous
//     deploy;
//   * a NAVIGATION is network-first, so logging out (or in) can never be
//     answered from a shell cached under the other identity;
//   * `/api/*` is never intercepted, so no response carrying tenant rows can
//     enter a cache at all.
describe("M135 §3.4-4b — the PWA shell across a logout → login", () => {
  it("cold, warm, post-logout and post-login each stay honest", async () => {
    const server = createServer();
    serveDeployA(server);
    // A navigation to `/` is what a logout actually issues.
    server.files.set("/", { status: 200, body: INDEX_A, type: "text/html" });
    const stamped = stampedWorker(INDEX_A);
    const worker = loadWorker(stamped.source, server);
    const asset = "/assets/index-A.js";
    const hits = () => server.hits.filter((h) => h === asset).length;

    // COLD: install precaches the shell and the assets index.html names.
    await install(worker);
    await activate(worker);
    const cache = worker.caches.peek(stamped.cacheName)!;
    expect(hits()).toBe(1);
    expect(cache.keys()).toContain(new URL(asset, ORIGIN).href);

    // WARM: the same request is answered from the cache — no second origin hit.
    expect(await (await fetchAs(worker, asset))!.text()).toContain("index-A.js");
    expect(hits()).toBe(1);

    // LOGOUT: the document is network-first, so the previous identity's shell
    // cannot be served to whoever signs in next.
    const beforeLogout = server.hits.length;
    expect(await (await fetchAs(worker, "/", "navigate"))!.text()).toContain("index-A.js");
    expect(server.hits.length).toBe(beforeLogout + 1);
    expect(server.hits[server.hits.length - 1]).toBe("/");

    // LOGIN: same document, same answer — and `/assets/*` is STILL cache-first
    // afterwards (the hashed name carries no identity).
    const beforeLogin = server.hits.length;
    await fetchAs(worker, "/", "navigate");
    expect(server.hits.length).toBe(beforeLogin + 1);
    await fetchAs(worker, asset);
    expect(server.hits.length).toBe(beforeLogin + 1);

    // The data half: /api is never intercepted and never cached, so a
    // cross-tenant leak through the worker is impossible by construction.
    expect(await fetchAs(worker, "/api/v1/transactions")).toBeUndefined();
    const cached = worker.caches
      .names()
      .flatMap((name) => worker.caches.peek(name)!.keys());
    expect(cached.filter((key) => key.includes("/api/"))).toEqual([]);
  });
});
