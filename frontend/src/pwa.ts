// Service-worker registration for the installable PWA (M72, docs/09
// "Installable PWA") + the M114 stale-shell self-heal.
//
// The worker itself is `public/sw.js` (plain JS — it is served verbatim from
// dist, so it must not depend on the bundler). This module only decides WHEN
// to register:
//
//   - production builds only: a dev server serving `/src/*` module graphs from
//     memory would hand the worker a shell it cannot re-serve, and a stale
//     worker is the single most confusing failure mode a SPA can have;
//   - secure contexts only: `navigator.serviceWorker` exists on plain http,
//     but registering there pins an unauthenticated origin to whatever the
//     network served — the LAN http deployment (docs/09) therefore runs
//     exactly like today, without a worker;
//   - registration failures never surface to the user: an installed-app
//     affordance is an enhancement, and the app must work identically with
//     the worker blocked, uninstalled or unsupported.
//
// Scope is `/` (the whole app shell), and `register()` resolves after the
// first successful registration — `updateViaCache: "none"` keeps nginx's
// default caching from serving a stale `sw.js` on the 24-hour update check.
//
// M114 — stale shell: a lazily-imported chunk (`CommandPalette-*.js`) is only
// fetched when the user opens the palette, i.e. usually AFTER the deploy that
// replaced its hash, so the dynamic import 404s and Vite's error boundary
// shows "Unexpected Application Error". The worker answers such a 404 with a
// reload marker and posts RELOAD_MESSAGE to every window; `watchStaleShell()`
// below is the other half of that handshake — it listens for the message, and
// it also watches `unhandledrejection` so a shell with NO worker at all (the
// registration is an enhancement, remember) heals from the same evidence. Both
// paths are bounded by RELOAD_GUARD_KEY/RELOAD_GUARD_MS, mirrored in
// `public/sw.js`, so a genuinely broken asset costs at most one reload per
// window instead of a loop.

const SW_URL = "/sw.js";
const SW_SCOPE = "/";

/** Same literal as `public/sw.js` (pinned by tests/m114StaleChunkSelfHeal). */
export const RELOAD_GUARD_KEY = "fintrack-stale-reload-at";
export const RELOAD_GUARD_MS = 30_000;
export const RELOAD_MESSAGE = "fintrack:reload";

export function canRegisterServiceWorker(): boolean {
  return (
    import.meta.env.PROD &&
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    window.isSecureContext
  );
}

/** Register the app-shell worker. Never throws; resolves to the registration
 *  path taken so callers/tests can assert on it. */
export async function registerServiceWorker(): Promise<"registered" | "skipped"> {
  if (!canRegisterServiceWorker()) return "skipped";
  try {
    await navigator.serviceWorker.register(SW_URL, {
      scope: SW_SCOPE,
      type: "classic",
      updateViaCache: "none",
    });
    return "registered";
  } catch {
    // Offline shell is a bonus; the app itself never depends on it.
    return "skipped";
  }
}

/** The `/assets/*` names a document (index.html source or the serialized live
 *  DOM) actually references — script srcs and stylesheet links only, so a path
 *  that merely appears as text in the page can never look like a shell ref. */
export function assetRefsFromHtml(html: string): string[] {
  const refs = [
    ...html.matchAll(/<(?:script|link)\b[^>]*?\s(?:src|href)=["'](\/assets\/[^"']+)["']/g),
  ].map((match) => match[1]);
  return [...new Set(refs)].sort();
}

/** True only when BOTH sides are real shells that name different assets — an
 *  empty side (dev server, partial document) is "cannot tell", never "stale",
 *  so this can never reload a dev build. */
export function shellRefsDiffer(freshHtml: string, liveHtml: string): boolean {
  const fresh = assetRefsFromHtml(freshHtml);
  const live = assetRefsFromHtml(liveHtml);
  if (fresh.length === 0 || live.length === 0) return false;
  return fresh.join("\n") !== live.join("\n");
}

/** At most one reload per RELOAD_GUARD_MS per tab (sessionStorage is per-tab).
 *  Returns true when the caller may reload NOW. */
export function guardedReload(
  now: number = Date.now(),
  store?: Pick<Storage, "getItem" | "setItem"> | null,
): boolean {
  const storage =
    store ?? (typeof sessionStorage === "undefined" ? null : sessionStorage);
  const last = Number(storage?.getItem(RELOAD_GUARD_KEY) ?? 0);
  if (now - last < RELOAD_GUARD_MS) return false;
  storage?.setItem(RELOAD_GUARD_KEY, String(now));
  return true;
}

/** M114, worker-less path: the shell in memory named an asset the server no
 *  longer has. Prove it against a freshly fetched /index.html before touching
 *  the page — a missing chunk on a CURRENT shell is a broken deploy, and
 *  reloading would only hide the error. */
async function healIfShellStale(): Promise<void> {
  try {
    const response = await fetch("/index.html", { cache: "reload" });
    if (!response.ok) return;
    const fresh = await response.text();
    if (shellRefsDiffer(fresh, document.documentElement.outerHTML) && guardedReload()) {
      location.reload();
    }
  } catch {
    // Offline: there is no newer shell to reload into.
  }
}

const DYNAMIC_IMPORT_ERROR =
  /failed to fetch dynamically imported module|failed to load dynamically imported module|error loading dynamically imported module/i;

/** Wire the stale-shell heal. Idempotent enough for tests (listeners are added
 *  once per call site: `startPwa`). Safe with no worker, no sessionStorage and
 *  no window — it never throws and never reloads on evidence it cannot read. */
export function watchStaleShell(): void {
  if (typeof window === "undefined") return;

  if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (event: MessageEvent) => {
      const data = event.data as { type?: unknown } | null | undefined;
      if (data && data.type === RELOAD_MESSAGE && guardedReload()) location.reload();
    });
  }

  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    const reason = event.reason as { message?: unknown } | null | undefined;
    const message = String(reason?.message ?? reason ?? "");
    if (!DYNAMIC_IMPORT_ERROR.test(message)) return;
    void healIfShellStale();
  });
}

/** Kick registration off after first paint (module load is inside main.tsx,
 *  before React mounts — the worker then controls this document on the NEXT
 *  load, which is what the spec wants). The M114 heal is wired first and
 *  unconditionally: it must protect a page the worker never registered for. */
export function startPwa(): void {
  watchStaleShell();
  if (!canRegisterServiceWorker()) return;
  if (document.readyState === "complete") {
    void registerServiceWorker();
  } else {
    window.addEventListener("load", () => void registerServiceWorker(), {
      once: true,
    });
  }
}
