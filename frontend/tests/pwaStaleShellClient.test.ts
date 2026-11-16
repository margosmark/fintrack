// @vitest-environment jsdom
// M114 — the client half of the stale-shell handshake (docs/09 §Installable
// PWA). `public/sw.js` answers a 404'd lazy chunk with a reload marker and
// posts RELOAD_MESSAGE to every window; this file drives `src/pwa.ts` against
// jsdom's real window/document/sessionStorage to prove the other two doors of
// the same heal:
//
//   1. a dynamic-import failure reloads ONLY a shell that a freshly fetched
//      /index.html proves to be OUT OF DATE (a missing chunk on the CURRENT
//      shell is a broken deploy — reloading would just hide the error), and
//   2. the worker's RELOAD_MESSAGE reloads immediately,
//
// both bounded by the same per-tab guard as the worker's marker, so the pair
// can never turn into a reload loop.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RELOAD_GUARD_KEY, RELOAD_MESSAGE, startPwa } from "../src/pwa";

const HEAD_A = `
  <link rel="stylesheet" crossorigin href="/assets/index-A.css" />
  <script type="module" crossorigin src="/assets/index-A.js"></script>
`;
const HEAD_B = HEAD_A.replace(/-A\./g, "-B.");

type StubbedMessage = { data?: unknown };

/** jsdom's navigator has no ServiceWorkerContainer — stand one in that only
 *  records what `src/pwa.ts` registers, so a message can be fired at it
 *  exactly the way the browser delivers one. */
function fakeServiceWorker(): { emit: (data: unknown) => void; listeners: () => number } {
  const listeners: ((event: unknown) => void)[] = [];
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      addEventListener: (type: string, listener: (event: unknown) => void) => {
        if (type === "message") listeners.push(listener);
      },
    },
  });
  return {
    listeners: () => listeners.length,
    emit: (data) => {
      for (const listener of [...listeners]) listener({ data } satisfies StubbedMessage);
    },
  };
}

function rejectImport(message: string): void {
  const event = new Event("unhandledrejection");
  Object.defineProperty(event, "reason", { value: new TypeError(message) });
  window.dispatchEvent(event);
}

/** The document this tab is running: the shell's own head markup. */
function serveShell(head: string): void {
  document.head.innerHTML = head;
  document.body.innerHTML = '<div id="root"></div>';
}

const originalFetch = globalThis.fetch;

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("M114 — client heal (src/pwa.ts)", () => {
  it("reloads on the worker's RELOAD_MESSAGE, once per guard window", () => {
    const worker = fakeServiceWorker();
    startPwa();
    expect(worker.listeners()).toBe(1);

    worker.emit({ type: RELOAD_MESSAGE, build: "abc123def456" });
    const written = sessionStorage.getItem(RELOAD_GUARD_KEY);
    expect(written).not.toBeNull();

    worker.emit({ type: RELOAD_MESSAGE, build: "abc123def456" });
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBe(written);
  });

  it("ignores messages that are not the worker's reload signal", () => {
    const worker = fakeServiceWorker();
    startPwa();
    worker.emit({ type: "something-else" });
    worker.emit(null);
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBeNull();
  });

  it("reloads a stale shell when a dynamic import fails — and only then", async () => {
    serveShell(HEAD_A);
    const worker = fakeServiceWorker();
    // The origin now serves deploy B.
    globalThis.fetch = (async () => ({ ok: true, text: async () => HEAD_B })) as unknown as typeof fetch;
    startPwa();

    // Unrelated rejections are none of our business.
    rejectImport("network timeout elsewhere");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBeNull();

    // The palette import failed and the document in memory is deploy A.
    rejectImport("Failed to fetch dynamically imported module: /assets/CommandPalette-A.js");
    await vi.waitFor(() => expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).not.toBeNull());
    const first = sessionStorage.getItem(RELOAD_GUARD_KEY);

    // The guard holds for a repeat inside the window (no reload loop).
    rejectImport("Failed to fetch dynamically imported module: /assets/CommandPalette-A.js");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBe(first);
    expect(worker.listeners()).toBe(1);
  });

  it("does NOT reload when the import fails against the CURRENT shell", async () => {
    serveShell(HEAD_B);
    fakeServiceWorker();
    globalThis.fetch = (async () => ({ ok: true, text: async () => HEAD_B })) as unknown as typeof fetch;
    startPwa();

    rejectImport("Failed to fetch dynamically imported module: /assets/CommandPalette-B.js");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBeNull();
  });

  it("never reloads on a document it cannot read (dev shells name /src/*)", async () => {
    document.head.innerHTML = '<script type="module" src="/src/main.tsx"></script>';
    fakeServiceWorker();
    globalThis.fetch = (async () => ({ ok: true, text: async () => HEAD_B })) as unknown as typeof fetch;
    startPwa();

    rejectImport("Failed to fetch dynamically imported module: /assets/whatever.js");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sessionStorage.getItem(RELOAD_GUARD_KEY)).toBeNull();
  });
});
