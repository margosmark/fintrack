/* fin_tracker app-shell service worker (M72, docs/09 "Installable PWA";
 * M114 stale-shell self-heal).
 *
 * Contract — the shell offline, the DATA never offline:
 *   - `/api/*` is NEVER cached and never answered from cache. Financial data
 *     stays server-backed by construction; offline the app renders its own
 *     error/empty states over a shell that still opens, rather than stale
 *     numbers that look current.
 *   - navigations are network-first with an `/index.html` fallback, so a cold
 *     offline open lands on the app shell and the client router takes over
 *     (deep links offline resolve to the shell, not to a 404 from the server).
 *   - hashed build assets (`/assets/*`, immutable per nginx) are cache-first:
 *     they never change under a name, so re-fetching them is pure waste.
 *   - everything else same-origin (manifest, icons, screenshots) is
 *     stale-while-revalidate.
 *
 * BUILD_ID is stamped into this file by `frontend/scripts/stamp-sw.mjs` (wired
 * into `npm run build`), so CACHE_NAME changes on EVERY deploy: activate drops
 * every other cache, a stale `index.html` cannot outlive its assets, and the
 * cache stops accumulating dead chunks. `public/*` is copied to dist verbatim,
 * so this literal — not `vite define` — is what the build can rewrite.
 *
 * M114 (the ⌘K palette 404): a lazily-needed chunk (`CommandPalette-*.js`, own
 * chunk since M95) is only fetched when the user OPENS the palette, i.e. long
 * after the deploy that replaced it. If the document in memory still names the
 * old hash, the cache misses, the network answers 404 and the dynamic import
 * dies as "Unexpected Application Error". The /assets/ branch therefore treats
 * a 404 as a STALE SHELL, not as a missing file: it re-fetches `/index.html`
 * over the network (cache: "reload"), stores it, tells every window client to
 * reload, and answers a script request with a reload MARKER module so the page
 * heals even when its own JS predates this worker. The marker is guarded by
 * RELOAD_GUARD_KEY/RELOAD_GUARD_MS — the same key `src/pwa.ts` uses — so a
 * genuinely broken image can never turn into a reload loop: one reload, and if
 * the chunk is STILL missing the marker component throws and the failure stays
 * visible.
 */

const BUILD_ID = "__BUILD_ID__";

const CACHE_NAME = `fintrack-shell-v1-${BUILD_ID}`;

/** Mirrors src/pwa.ts (tests/m114StaleChunkSelfHeal.test.ts pins the parity). */
const RELOAD_GUARD_KEY = "fintrack-stale-reload-at";
const RELOAD_GUARD_MS = 30000;
const RELOAD_MESSAGE = "fintrack:reload";

const SHELL = [
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

/** The body served in place of a 404'd `/assets/*.js` (see M114 above). */
const RELOAD_MARKER = `/* M114 stale-shell self-heal: this document names a chunk the server
 * no longer has, so the worker answers the import with a marker instead of a
 * 404 — schedule ONE guarded reload and render nothing until the current shell
 * replaces this page. */
var fintrackReloadScheduled = false;
var fintrackReloadInFlight = false;
try {
  var fintrackLastReload = Number(sessionStorage.getItem("${RELOAD_GUARD_KEY}") || 0);
  var fintrackSinceReload = Date.now() - fintrackLastReload;
  if (fintrackSinceReload > ${RELOAD_GUARD_MS}) {
    sessionStorage.setItem("${RELOAD_GUARD_KEY}", String(Date.now()));
    fintrackReloadScheduled = true;
    location.reload();
  } else if (fintrackSinceReload >= 0 && fintrackSinceReload < 2000) {
    // The client-side half of the handshake got here first (it shares this
    // key): a navigation is already on its way, so render nothing rather than
    // throwing into the error boundary a moment before the page is replaced.
    fintrackReloadInFlight = true;
  }
} catch (e) {
  /* storage denied — src/pwa.ts keeps the same guard as the backstop */
}
export default function StaleShellMarker() {
  if (fintrackReloadScheduled || fintrackReloadInFlight) return null;
  throw new Error("fintrack: stale shell — reload suppressed by the guard");
}
`;

/** `/assets/*` names the shell's own entry + every lazily-loaded chunk. */
function assetRefs(html) {
  return [...new Set([...html.matchAll(/\/assets\/[A-Za-z0-9_.-]+/g)].map((m) => m[0]))];
}

/** Store the freshly fetched shell so the next navigation/offline open agrees
 *  with the assets the server actually has. */
async function putShell(cache, response) {
  await cache.put("/index.html", response.clone());
  return response;
}

async function reloadMarkerResponse() {
  return new Response(RELOAD_MARKER, {
    status: 200,
    headers: { "Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-store" },
  });
}

/** M114: a 404 for a hashed asset means the shell in memory is from another
 *  deploy. Prove the server has a newer shell, publish it, then make the client
 *  reload into it. Only a script request can be answered with script; every
 *  other destination keeps its 404 (the window clients still get the message,
 *  and `src/pwa.ts` heals a shell whose own JS is too old to listen). */
async function healStaleShell(cache, request, notFound) {
  // Every step below that is not the proof itself is best-effort: one reset
  // connection must never be able to swallow the heal (measured: a transient
  // reset while fetching the shell turned a healing deploy into the same
  // error UI as before the fix).
  try {
    await cache.delete(request);
  } catch {
    /* nothing cached under a name the server no longer has, anyway */
  }

  // The proof: does the origin have a shell newer than this document?
  let fresh = null;
  for (let attempt = 0; attempt < 2 && !fresh; attempt += 1) {
    try {
      const response = await fetch("/index.html", { cache: "reload" });
      if (response.ok) fresh = response;
    } catch {
      /* reset connection — try once more before giving up on the heal */
    }
  }
  if (!fresh) return notFound;

  try {
    await putShell(cache, fresh);
  } catch {
    /* quota: the marker below is what actually heals the open document */
  }
  try {
    const windows = await self.clients.matchAll({ type: "window" });
    await Promise.all(
      windows.map((client) => client.postMessage({ type: RELOAD_MESSAGE, build: BUILD_ID })),
    );
  } catch {
    /* a client that closed mid-broadcast — the marker still carries the heal */
  }

  const path = new URL(request.url).pathname;
  if (path.endsWith(".js") || path.endsWith(".mjs")) return reloadMarkerResponse();
  return notFound;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // `cache: "reload"` bypasses the HTTP cache on every precache entry: a new
      // BUILD_ID must land on the server's CURRENT shell, not on whatever the
      // browser disk happens to hold (nginx serves index.html no-cache, but the
      // worker does not depend on that).
      const shell = await fetch("/index.html", { cache: "reload" });
      if (!shell.ok) throw new Error(`sw install: /index.html -> ${shell.status}`);
      await putShell(cache, shell);
      // Precache what index.html names. CACHE_NAME changes every deploy, so the
      // entry chunks the old cache held are about to be deleted — without this
      // the freshly activated worker could only hand out an index.html whose
      // entry script is nowhere to be found (offline boot would break, and the
      // first asset request would go straight to a 404).
      await Promise.all(
        assetRefs(await shell.text()).map(async (url) => {
          const response = await fetch(url, { cache: "reload" });
          if (response.ok) await cache.put(url, response);
        }),
      );
      await Promise.all(
        SHELL.map((url) => cache.add(new Request(url, { cache: "reload" }))),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Data is server-backed (docs/09): never cache, never intercept.
  if (url.pathname.startsWith("/api")) return;

  // App shell: network first so a deploy is picked up immediately, cache
  // fallback only when the network is unreachable.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put("/index.html", copy));
          return response;
        })
        .catch(() =>
          caches
            .open(CACHE_NAME)
            .then((cache) => cache.match("/index.html"))
            .then((cached) => cached || Response.error()),
        ),
    );
    return;
  }

  // Hashed build assets are immutable by name — cache-first, scoped to THIS
  // build's cache (a match from a previous BUILD_ID's cache must not outlive
  // the activate purge), with M114's 404 self-heal behind a cache miss.
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(request).then(
          (cached) =>
            cached ||
            fetch(request).then(async (response) => {
              if (response.ok) {
                await cache.put(request, response.clone());
                return response;
              }
              if (response.status === 404) return healStaleShell(cache, request, response);
              return response;
            }),
        ),
      ),
    );
    return;
  }

  // Manifest / icons / static images: serve fresh when we have it, refresh after.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached || Response.error());
      return cached || network;
    }),
  );
});
