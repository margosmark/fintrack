#!/usr/bin/env node
// Ledger E2E runner (docs/47 §13 acceptance examples against the RUNNING
// candidate stack — never TestClient, never a mocked API).
//
//   node tests/ledger-e2e/run.mjs [--app http://127.0.0.1:8100]
//                                 [--api http://127.0.0.1:8000]
//                                 [--out results.json] [--suite name]
//
// Requires: a built frontend/dist, uvicorn with SEED_DEMO_ACCOUNT=true, and a
// static+proxy server (harness/serve.mjs). Every check records a real observable
// from the page or a real API payload; failures are findings, never edits.
import { writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { launchChrome, sleep } from "./harness/cdp.mjs";
import { demoToken, req, registerTenant, loginToken, deleteTenant, money, parseMoney, APP, API } from "./harness/api.mjs";

import shellSuite from "./suites/shell.mjs";
import responsiveSuite from "./suites/responsive.mjs";
import truthSuite from "./suites/truth.mjs";
import maskSuite from "./suites/mask.mjs";
import statesSuite from "./suites/states.mjs";
import demoWritesSuite from "./suites/demo-writes.mjs";
import navSuite from "./suites/nav.mjs";
import keyboardSuite from "./suites/keyboard.mjs";
import tenantSuite from "./suites/tenant.mjs";
import acceptanceSuite from "./suites/acceptance.mjs";
import staticScan from "./suites/static-scan.mjs";

const SUITES = [
  staticScan,
  shellSuite,
  responsiveSuite,
  truthSuite,
  maskSuite,
  demoWritesSuite,
  statesSuite,
  acceptanceSuite, // F-10 / F-11 / retry acceptance re-runs (t_51b47d46)
  navSuite,
  keyboardSuite,
  tenantSuite, // logs out the demo session — must stay LAST
];

function parseArgs(argv) {
  const out = { app: APP, api: API, out: "results.json", suite: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--app") out.app = argv[++i];
    else if (a === "--api") out.api = argv[++i];
    else if (a === "--out") out.out = argv[++i];
    else if (a === "--suite") out.suite = argv[++i];
  }
  return out;
}

const args = parseArgs(process.argv);
// Publish the flags to the Node-side helpers (api.mjs resolves them at call
// time) so one pair of flags drives BOTH the browser and every direct API
// call the suites make.
process.env.LEDGER_E2E_APP = args.app;
process.env.LEDGER_E2E_API = args.api;

async function assertStack(app, api) {
  const probe = await fetch(`${app}/`).catch(() => null);
  if (!probe?.ok) {
    throw new Error(
      `app not reachable at ${app} — build (npm run build) and serve it: node tests/ledger-e2e/harness/serve.mjs 8100 8000`,
    );
  }
  const health = await fetch(`${api}/api/v1/health`).catch(() => null);
  if (!health?.ok) {
    throw new Error(`API not reachable at ${api} — start uvicorn with SEED_DEMO_ACCOUNT=true`);
  }
}

const checks = [];
const notes = [];
const cleanups = [];
let currentSuite = "setup";
let currentViewport = "-";

function check(id, name, ok, detail) {
  const entry = {
    suite: currentSuite,
    viewport: currentViewport,
    id,
    name,
    pass: !!ok,
    detail: detail === undefined ? "" : String(detail).slice(0, 600),
  };
  checks.push(entry);
  const mark = entry.pass ? "  ok " : "FAIL ";
  console.log(`${mark}[${entry.suite} @${entry.viewport}] ${id} — ${name}${entry.detail ? ` :: ${entry.detail}` : ""}`);
  return entry.pass;
}

function note(msg) {
  notes.push(`[${currentSuite}] ${msg}`);
  console.log(`  note [${currentSuite}] ${msg}`);
}

async function main() {
  await assertStack(args.app, args.api);

  // A sibling lane (QA) can be holding the default CDP port, so make it
  // overridable rather than fighting over 9334.
  const chrome = await launchChrome({ port: Number(process.env.LEDGER_E2E_CDP_PORT || 9334) });
  console.log(`chrome: ${chrome.version.Browser}`);
  const page = chrome.page;
  await page.setViewport(1440, 900);
  // A headless target reports visibilityState "hidden", which pauses React
  // Query's focus/refetch machinery and reads as a product failure — the
  // known harness artifact behind "bring the page to the foreground before
  // any transient failure assertion". Pin the single target visible once,
  // up front, so no check measures the window instead of the app.
  await page.send("Page.bringToFront").catch(() => {});
  await page.send("Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});

  // ---- console: uncaught exceptions + console.error, whole run ----------
  // Resource-level log entries are excluded on purpose: the state/retry pins
  // DELIBERATELY inject 500s and a delayed API, and a failed resource under a
  // fault we injected is not a product error. What must never happen is an
  // uncaught exception escaping a React boundary.
  const pageErrors = [];
  const consoleErrors = [];
  page.conn.on("Runtime.exceptionThrown", (p) => {
    const d = p.exceptionDetails || {};
    pageErrors.push(String(d.exception?.description || d.text || "unknown").replace(/\s+/g, " ").slice(0, 300));
  });
  page.conn.on("Log.entryAdded", (p) => {
    const e = p.entry || {};
    if (e.source === "javascript" && e.level === "error") {
      consoleErrors.push(String(e.text || "").replace(/\s+/g, " ").slice(0, 300));
    }
  });

  const ctx = {
    page,
    app: args.app,
    api: args.api,
    check,
    note,
    sleep,
    money,
    parseMoney,
    demoToken: () => demoToken(),
    apiReq: (token, path, opts) => req(token, path, opts),
    registerTenant,
    loginToken,
    deleteTenant: (token, password) => deleteTenant(token, password),
    /** Register a teardown that runs even if the suite aborts mid-way. */
    addCleanup(fn) {
      cleanups.push(fn);
    },
    viewport(w, h) {
      currentViewport = `${w}x${h || (w < 768 ? 844 : 900)}`;
      return page.setViewport(w, h || (w < 768 ? 844 : 900));
    },
    /** Idempotent demo sign-in through the real login surface. */
    async demoLogin() {
      await page.navigate(`${args.app}/dashboard`);
      // The auth probe resolves after paint; wait for the real redirect rather
      // than sampling pathname once.
      try {
        await page.waitFor(`location.pathname === '/login'`, { timeout: 6000 }, "login redirect");
      } catch {
        /* still on the app — already authenticated */
      }
      if (await page.eval(`location.pathname === '/login'`, { awaitPromise: false })) {
        await page.waitFor(
          `[...document.querySelectorAll('button')].some(b=>b.innerText.includes('Explore the demo'))`,
          { timeout: 15000 },
          "Explore the demo control",
        );
        await page.eval(
          `[...document.querySelectorAll('button')].find(b=>b.innerText.includes('Explore the demo'))?.click()`,
        );
        await page.waitFor(`location.pathname !== '/login'`, { timeout: 25000 }, "post-login redirect");
      }
      await page.waitFor(`!!document.querySelector('main')`, { timeout: 20000 }, "main shell");
      // The first-run tour overlays the shell; dismiss it if present.
      await page
        .eval(`[...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Skip')?.click()`)
        .catch(() => {});
      await sleep(300);
    },
    async signOut() {
      await page.navigate(`${args.app}/dashboard`);
      await sleep(400);
      await page.click('[data-testid="account-menu-toggle"]');
      await sleep(300);
      const loggedOut = await page.eval(`(() => {
        const item = [...document.querySelectorAll('[role="menuitem"],button,a')].find(el => /sign out|log out/i.test(el.innerText||''));
        if (!item) return false; item.click(); return true; })()`);
      if (!loggedOut) throw new Error("sign-out control not found in account menu");
      await page.waitFor(`location.pathname === '/login'`, { timeout: 15000 }, "signed out");
    },
  };

  const selected = args.suite ? SUITES.filter((s) => s.name === args.suite) : SUITES;
  const startedAt = Date.now();
  for (const suite of selected) {
    currentSuite = suite.name;
    currentViewport = suite.viewportLabel || "-";
    console.log(`\n=== suite ${suite.name} ===`);
    try {
      await suite.run(ctx);
    } catch (err) {
      check(`${suite.name}.suite-error`, "suite aborted with an error", false, err.stack || err.message);
    }
  }

  // Teardowns registered by suites run no matter how the loop ended (a
  // disposable tenant must never outlive its run).
  for (const fn of cleanups.reverse()) {
    try {
      await fn();
    } catch (err) {
      console.error(`cleanup failed: ${err.message}`);
    }
  }

  // ---- console verdict (acceptance: responsive / keyboard / console / static
  // scan): an uncaught exception anywhere in the run escaped the React error
  // boundary and is a product defect, so it fails the report. console.error
  // entries are recorded as evidence rather than a verdict — the fault pins
  // deliberately inject 500s and a delayed API.
  currentSuite = "console";
  currentViewport = "-";
  check(
    "console.no-uncaught-exceptions",
    "the whole browser run raised no uncaught JS exception",
    pageErrors.length === 0,
    pageErrors.length === 0 ? "0 uncaught exceptions" : JSON.stringify(pageErrors.slice(0, 5)),
  );
  note(
    consoleErrors.length === 0
      ? "0 console.error entries"
      : `${consoleErrors.length} console.error entries (informational): ${JSON.stringify([...new Set(consoleErrors)].slice(0, 5))}`,
  );

  const failed = checks.filter((c) => !c.pass);
  const report = {
    suite: "ledger-e2e",
    app: args.app,
    api: args.api,
    chrome: chrome.version.Browser,
    startedAt: new Date(startedAt).toISOString(),
    durationMs: Date.now() - startedAt,
    total: checks.length,
    passed: checks.length - failed.length,
    failed: failed.length,
    checks,
    notes,
    pageErrors,
    consoleErrors,
    verdict: failed.length === 0 ? "PASS" : "FAIL",
  };
  const outPath = path.resolve(args.out);
  mkdirSync(path.dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(report, null, 2));
  console.log(`\n=== ${report.verdict}: ${report.passed}/${report.total} checks passed (${failed.length} failed) ===`);
  for (const f of failed) console.log(`  FAIL ${f.suite} @${f.viewport} ${f.id} — ${f.name} :: ${f.detail}`);
  console.log(`results: ${outPath}`);

  await chrome.cleanup();
  process.exit(failed.length === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
