// One-off triage probe (test-only): what does the Dashboard actually render
// while every /api/v1/ call is delayed 2.5s? Distinguishes "no loading state
// (product gap)" from "loading state present but not under the pinned
// selector (harness gap)" for state.loading.
import { launchChrome, sleep } from "./harness/cdp.mjs";

const APP = process.env.LEDGER_E2E_APP || "http://127.0.0.1:8160";

const chrome = await launchChrome({ port: 9341 });
const page = chrome.page;
await page.setViewport(1440, 900);

await page.navigate(`${APP}/dashboard`);
try {
  await page.waitFor(`location.pathname === '/login'`, { timeout: 8000 }, "login redirect");
} catch { /* authenticated */ }
if (await page.eval(`location.pathname === '/login'`, { awaitPromise: false })) {
  await page.waitFor(`[...document.querySelectorAll('button')].some(b=>b.innerText.includes('Explore the demo'))`, { timeout: 15000 }, "demo button");
  await page.eval(`[...document.querySelectorAll('button')].find(b=>b.innerText.includes('Explore the demo'))?.click()`);
  await page.waitFor(`location.pathname !== '/login'`, { timeout: 25000 }, "post-login");
}
await page.waitFor(`!!document.querySelector('main')`, { timeout: 20000 }, "main");
await page.eval(`[...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Skip')?.click()`).catch(() => {});
await sleep(500);

// Warm the /reports chunk exactly the way states.mjs does, then cold-load
// the Dashboard with a 2.5s-delayed API.
await page.navigate(`${APP}/reports`);
await page.waitFor(`!!document.querySelector('main')`, { timeout: 25000 }, "reports shell");
await sleep(500);

await page.intercept(async (p) =>
  p.request.url.includes("/api/v1/") ? { kind: "delay", ms: 2500 } : { kind: "continue" },
);

const t0 = Date.now();
await page.eval(`location.href = ${JSON.stringify(APP + "/dashboard")}`, { awaitPromise: false });

const sample = `(() => ({
  path: location.pathname,
  fb: !!document.querySelector('[data-testid="route-fallback"]'),
  row: document.querySelectorAll('[data-testid="state-row-loading"]').length,
  pulse: document.querySelectorAll('.animate-pulse').length,
  skelish: document.querySelectorAll('[aria-hidden="true"] .glass, .glass [class*="animate-pulse"]').length,
  err: !!document.querySelector('[data-testid="error-panel"]'),
  main: (document.querySelector('main')?.innerText || '').replace(/\\s+/g, ' ').slice(0, 140),
}))()`;

const hits = [];
let firstFb = null, firstRow = null, firstPulse = null;
for (let i = 0; i < 90; i++) {
  const s = await page.eval(sample, { awaitPromise: false }).catch(() => null);
  if (s) {
    if (s.fb && firstFb === null) firstFb = Date.now() - t0;
    if (s.row > 0 && firstRow === null) firstRow = Date.now() - t0;
    if (s.pulse > 0 && firstPulse === null) firstPulse = Date.now() - t0;
    if (i % 5 === 0 || s.fb || s.row > 0 || s.err) {
      hits.push({ t: Date.now() - t0, ...s });
    }
  }
  await sleep(80);
}
await page.intercept(null);

console.log("first route-fallback ms:", firstFb);
console.log("first state-row-loading ms:", firstRow);
console.log("first animate-pulse ms:", firstPulse);
console.log("samples:");
for (const h of hits) console.log(" ", JSON.stringify(h));
await chrome.cleanup();
process.exit(0);
