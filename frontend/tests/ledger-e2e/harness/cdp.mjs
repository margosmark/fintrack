// Minimal CDP driver for the Ledger E2E suite — no third-party deps (Node 22
// ships a global WebSocket). One browser target per Page object, helpers only
// for what the acceptance examples in docs/47 §13 need: navigation waits, DOM
// reads, trusted mouse/key input, viewport emulation, a network log with
// markers (the mask "zero network requests" pin), and Fetch interception
// (forced 500s / latency for the loading + error-state pins).
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const CHROME = process.env.LEDGER_E2E_CHROME || "google-chrome";

let seq = 0;

class Connection {
  constructor(ws) {
    this.ws = ws;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message} (${msg.method || ""})`));
        else resolve(msg.result || {});
        return;
      }
      const handlers = this.listeners.get(msg.method);
      if (handlers) for (const h of handlers) h(msg.params || {});
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = ++seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 30000);
    });
  }

  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(handler);
  }

  off(method, handler) {
    const list = this.listeners.get(method) || [];
    const i = list.indexOf(handler);
    if (i >= 0) list.splice(i, 1);
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* already gone */
    }
  }
}

export async function launchChrome({ port = 9334, headless = true, name = "ledger-e2e" } = {}) {
  const userDataDir = mkdtempSync(path.join(tmpdir(), `ledger-e2e-${name}-`));
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--hide-scrollbars",
    "--window-size=1440,900",
  ];
  if (headless) args.push("--headless=new");
  args.push("about:blank");

  const proc = spawn(CHROME, args, { stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  proc.stderr.on("data", (d) => {
    stderr += d.toString();
    if (stderr.length > 20000) stderr = stderr.slice(-20000);
  });

  // Wait for the debugging endpoint.
  const deadline = Date.now() + 20000;
  let version = null;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) {
        version = await res.json();
        break;
      }
    } catch {
      /* not up yet */
    }
    await sleep(200);
  }
  if (!version) {
    proc.kill("SIGKILL");
    rmSync(userDataDir, { recursive: true, force: true });
    throw new Error(`chrome did not expose CDP on :${port}. stderr tail:\n${stderr.slice(-1500)}`);
  }

  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const conn = new Connection(ws);
  const { targetId } = await conn.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await conn.send("Target.attachToTarget", { targetId, flatten: true });
  const page = new Page(conn, sessionId, targetId);

  await page.init();
  const cleanup = async () => {
    try {
      await conn.send("Browser.close", {}, undefined).catch(() => {});
    } catch {
      /* ignore */
    }
    proc.kill("SIGKILL");
    // Chrome needs a beat to release its profile dir; rmSync on a live tree
    // throws ENOTEMPTY, which must never fail a completed run.
    for (let i = 0; i < 20 && proc.exitCode === null; i++) await new Promise((r) => setTimeout(r, 100));
    for (let i = 0; i < 3; i++) {
      try {
        rmSync(userDataDir, { recursive: true, force: true });
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 300));
      }
    }
  };
  return { proc, conn, page, version, userDataDir, cleanup, headless };
}

export class Page {
  constructor(conn, sessionId, targetId) {
    this.conn = conn;
    this.sessionId = sessionId;
    this.targetId = targetId;
    this.requests = []; // every Network.requestWillBeSent seen since reset
    this.responses = [];
    this.downloads = [];
    this.interception = null; // fn(request) -> action, or null
  }

  async init() {
    await this.send("Page.enable");
    await this.send("Network.enable", { maxTotalBufferSize: 10 * 1024 * 1024 });
    await this.send("Runtime.enable");
    await this.send("DOM.enable");
    await this.send("Log.enable");
    // One page target per connection (flatten mode): every Network event on
    // this connection belongs to this page, so no sessionId filtering.
    this.conn.on("Network.requestWillBeSent", (p) => this.requests.push({ ...p, at: Date.now() }));
    this.conn.on("Network.responseReceived", (p) => this.responses.push({ ...p, at: Date.now() }));
    this.conn.on("Fetch.requestPaused", (p) => this._onPaused(p));
    this.conn.on("Browser.downloadWillBegin", (p) => this.downloads.push(p));
    await this.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: mkdtempSync(path.join(tmpdir(), "ledger-e2e-dl-")),
      eventsEnabled: true,
    }).catch(() => {
      /* older builds */
    });
  }

  send(method, params = {}) {
    return this.conn.send(method, params, this.sessionId);
  }

  // ---- network log -------------------------------------------------------
  netMark() {
    return this.requests.length;
  }

  netSince(mark) {
    return this.requests.slice(mark);
  }

  resetNetworkLog() {
    this.requests = [];
    this.responses = [];
  }

  // ---- Fetch interception ------------------------------------------------
  async intercept(handler) {
    this.interception = handler;
    if (handler) {
      await this.send("Fetch.enable", {
        patterns: [{ urlPattern: "*", requestStage: "Request" }],
      });
    } else {
      await this.send("Fetch.disable").catch(() => {});
    }
  }

  async _onPaused(p) {
    const handler = this.interception;
    if (!handler) {
      await this.send("Fetch.continueRequest", { requestId: p.requestId }).catch(() => {});
      return;
    }
    let action;
    try {
      action = await handler(p);
    } catch (err) {
      action = { kind: "continue" };
      console.error("interceptor error", err);
    }
    try {
      if (action.kind === "fulfill") {
        await this.send("Fetch.fulfillRequest", {
          requestId: p.requestId,
          responseCode: action.status || 500,
          responseHeaders: [
            { name: "Content-Type", value: action.contentType || "application/json" },
            ...(action.headers || []),
          ],
          body: b64(action.body ?? ""),
        });
      } else if (action.kind === "fail") {
        await this.send("Fetch.failRequest", {
          requestId: p.requestId,
          errorReason: action.reason || "Failed",
        });
      } else if (action.kind === "delay") {
        await sleep(action.ms || 1000);
        await this.send("Fetch.continueRequest", { requestId: p.requestId });
      } else {
        await this.send("Fetch.continueRequest", { requestId: p.requestId });
      }
    } catch {
      /* request may have been invalidated */
    }
  }

  // ---- viewport ----------------------------------------------------------
  async setViewport(width, height) {
    await this.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 768,
      screenWidth: width,
      screenHeight: height,
    });
  }

  // ---- navigation --------------------------------------------------------
  async navigate(url, { timeout = 30000 } = {}) {
    const done = this._waitEvent("Page.loadEventFired", timeout);
    await this.send("Page.navigate", { url });
    await done.catch(() => {});
    await this.waitFor(
      "document.readyState === 'complete'",
      { timeout: 15000 },
      "document readyState complete",
    );
    await sleep(150);
  }

  _waitEvent(method, timeout) {
    return new Promise((resolve, reject) => {
      const handler = (params) => {
        this.conn.off(method, handler);
        resolve(params);
      };
      this.conn.on(method, handler);
      setTimeout(() => {
        this.conn.off(method, handler);
        reject(new Error(`timeout waiting for ${method}`));
      }, timeout);
    });
  }

  // ---- evaluation --------------------------------------------------------
  async eval(expression, { awaitPromise = true } = {}) {
    const res = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise,
      userGesture: true,
    });
    if (res.exceptionDetails) {
      const text = res.exceptionDetails.exception?.description || res.exceptionDetails.text;
      throw new Error(`page eval failed: ${text}`);
    }
    return res.result?.value;
  }

  async waitFor(expression, { timeout = 10000, label } = {}, msg) {
    const deadline = Date.now() + timeout;
    let lastErr = null;
    while (Date.now() < deadline) {
      try {
        const v = await this.eval(expression);
        if (v) return v;
      } catch (err) {
        lastErr = err;
      }
      await sleep(100);
    }
    // A timeout without page context is undiagnosable — snapshot where the
    // browser actually was.
    let where = "";
    try {
      const snap = await this.eval(
        `({ path: location.pathname + location.search, ready: document.readyState,
            text: (document.body.innerText || '').slice(0, 220) })`,
        { awaitPromise: false },
      );
      where = ` | page=${snap.path} ready=${snap.ready} body=${JSON.stringify(snap.text)}`;
    } catch {
      /* page gone */
    }
    throw new Error(
      `waitFor timed out after ${timeout}ms: ${label || msg || expression}` +
        (lastErr ? ` (last error: ${lastErr.message})` : "") +
        where,
    );
  }

  async exists(selector) {
    return (await this.eval(`!!document.querySelector(${JSON.stringify(selector)})`)) === true;
  }

  async textOf(selector) {
    return this.eval(
      `(document.querySelector(${JSON.stringify(selector)})||{}).innerText ?? null`,
      { awaitPromise: false },
    );
  }

  async allText(selector) {
    return this.eval(
      `Array.from(document.querySelectorAll(${JSON.stringify(selector)})).map(e=>e.innerText)`,
      { awaitPromise: false },
    );
  }

  async count(selector) {
    return this.eval(`document.querySelectorAll(${JSON.stringify(selector)}).length`, {
      awaitPromise: false,
    });
  }

  // Trusted mouse click at the element's centre (real events, real handlers).
  async click(selector, { timeout = 10000 } = {}) {
    await this.waitFor(`!!document.querySelector(${JSON.stringify(selector)})`, { timeout }, selector);
    const rect = await this.eval(
      `(() => { const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null;
        el.scrollIntoView({block:'center', inline:'center'});
        const r = el.getBoundingClientRect();
        return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height}; })()`,
      { awaitPromise: false },
    );
    if (!rect || rect.w === 0 || rect.h === 0) {
      throw new Error(`click target not visible/zero-size: ${selector} ${JSON.stringify(rect)}`);
    }
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x, y: rect.y });
    await this.send("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: rect.x,
      y: rect.y,
      button: "left",
      clickCount: 1,
    });
    await this.send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: rect.x,
      y: rect.y,
      button: "left",
      clickCount: 1,
    });
    return rect;
  }

  // Trusted press hardened against post-load layout shift (t_94548fa5 →
  // t_2c3839ca — the states and acceptance F-RETRY presses): the same trusted
  // Input.dispatchMouseEvent chain `click` uses, but the rect is re-captured
  // immediately before dispatch and every candidate point is validated with
  // elementFromPoint against the control (`hit` semantics: the landed element
  // must still BE the selector). While layout is still moving — zero-size
  // rect or a point that no longer reaches the control — it re-arms ~150ms
  // apart inside `rearmMs`. Never an untrusted el.click().
  async pressValidated(selector, { rearmMs = 12000 } = {}) {
    const target = JSON.stringify(selector);
    const capture = `(() => { const el = document.querySelector(${target});
        if (!el) return null;
        el.scrollIntoView({block:'center', inline:'center'});
        const r = el.getBoundingClientRect();
        return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height}; })()`;
    const deadline = Date.now() + rearmMs;
    let rect = null;
    for (;;) {
      rect = await this.eval(capture, { awaitPromise: false });
      const sized = rect !== null && rect.w !== 0 && rect.h !== 0;
      const hit =
        sized &&
        (await this.eval(
          `(() => { const el = document.elementFromPoint(${rect.x}, ${rect.y});
        return el ? el.closest(${target}) !== null : false; })()`,
          { awaitPromise: false },
        )) === true;
      if (hit) {
        await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x, y: rect.y });
        await this.send("Input.dispatchMouseEvent", {
          type: "mousePressed",
          x: rect.x,
          y: rect.y,
          button: "left",
          clickCount: 1,
        });
        await this.send("Input.dispatchMouseEvent", {
          type: "mouseReleased",
          x: rect.x,
          y: rect.y,
          button: "left",
          clickCount: 1,
        });
        return rect;
      }
      if (Date.now() >= deadline) {
        throw new Error(
          `pressValidated: elementFromPoint never reached ${selector} within ${rearmMs}ms (last rect=${JSON.stringify(rect)})`,
        );
      }
      await sleep(150);
    }
  }

  // Trusted key press. modifiers: {alt,ctrl,shift,meta} booleans.
  async press(key, { modifiers = {}, text } = {}) {
    let mask = 0;
    if (modifiers.alt) mask |= 1;
    if (modifiers.ctrl) mask |= 2;
    if (modifiers.meta) mask |= 4;
    if (modifiers.shift) mask |= 8;
    const codeMap = {
      Tab: 9,
      Enter: 13,
      Escape: 27,
      " ": 32,
      Space: 32,
      ArrowDown: 40,
      ArrowUp: 38,
      ArrowLeft: 37,
      ArrowRight: 39,
      H: 72,
      K: 75,
    };
    const keyCode = codeMap[key] ?? (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    const params = {
      key,
      code: key.length === 1 ? `Key${key.toUpperCase()}` : key,
      windowsVirtualKeyCode: keyCode,
      nativeVirtualKeyCode: keyCode,
      modifiers: mask,
      bubbles: true,
      cancelable: true,
    };
    if (key === " ") params.code = "Space";
    if (text !== undefined) params.text = text;
    await this.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...params });
    if (text !== undefined && text.length > 0) {
      await this.send("Input.dispatchKeyEvent", { type: "char", ...params });
    }
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...params });
  }

  async type(text, { delay = 12 } = {}) {
    for (const ch of text) {
      await this.send("Input.insertText", { text: ch }).catch(async () => {
        await this.press(ch, { text: ch });
      });
      if (delay) await sleep(delay);
    }
  }

  async focus(selector) {
    await this.eval(
      `document.querySelector(${JSON.stringify(selector)})?.focus()`,
      { awaitPromise: false },
    );
  }

  async activeElement() {
    return this.eval(
      `(() => { const a = document.activeElement; if (!a) return null;
        return {tag: a.tagName, testid: a.getAttribute('data-testid'), text: (a.innerText||a.value||'').slice(0,80),
                aria: a.getAttribute('aria-label'), cls: (a.className||'').toString().slice(0,120)}; })()`,
      { awaitPromise: false },
    );
  }

  async activeElementMatches(selector) {
    return this.eval(
      `!!document.activeElement && document.activeElement.matches(${JSON.stringify(selector)})`,
      { awaitPromise: false },
    );
  }

  async url() {
    return this.eval("location.href", { awaitPromise: false });
  }

  async bodyText() {
    return this.eval("document.body.innerText", { awaitPromise: false });
  }
}

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function b64(str) {
  return Buffer.from(String(str), "utf8").toString("base64");
}
