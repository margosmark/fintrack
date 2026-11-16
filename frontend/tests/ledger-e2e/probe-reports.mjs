// One-off triage probe (test-only): docs/47 acceptance example 15 verbatim —
// a COLD /reports with every /api/v1/ call delayed 2.5s must show the pinned
// loading states (`route-fallback`, then one `state-row-loading`). Establishes
// the real observation window before the pin is tightened.
import { launchChrome, sleep } from "./harness/cdp.mjs";

const APP = process.env.LEDGER_E2E_APP || "http://127.0.0.1:8160";

const chrome = await launchChrome({ port: 9342 });
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
await sleep(400);

await page.intercept(async (p) =>
  p.request.url.includes("/api/v1/") ? { kind: "delay", ms: 2500 } : { kind: "continue" },
);

const sample = `(() => ({
  path: location.pathname,
  fb: !!document.querySelector('[data-testid="route-fallback"]'),
  row: document.querySelectorAll('[data-testid="state-row-loading"]').length,
  err: !!document.querySelector('[data-testid="error-panel"]'),
  skel: document.querySelectorAll('[data-testid="skeleton"], .animate-pulse, [class*="pulse"]').length,
  main: (document.querySelector('main')?.innerText || '').replace(/\\s+/g, ' ').slice(0, 110),
}))()`;

async function coldRun(label) {
  const t0 = Date.now();
  await page.eval(`location.href = ${JSON.stringify(APP + label)}`, { awaitPromise: false });
  let firstFb = null, firstRow = null, maxRow = 0, firstContent = null;
  const marks = [];
  for (let i = 0; i < 90; i++) {
    const s = await page.eval(sample, { awaitPromise: false }).catch(() => null);
    if (s) {
      if (s.fb && firstFb === null) firstFb = Date.now() - t0;
      if (s.row > 0) { if (firstRow === null) firstRow = Date.now() - t0; maxRow = Math.max(maxRow, s.row); }
      if (firstContent === null && s.main && s.main.length > 20 && !s.fb) firstContent = Date.now() - t0;
      if (s.fb || s.row > 0 || (i % 10 === 0)) marks.push({ t: Date.now() - t0, fb: s.fb, row: s.row, err: s.err, main: s.main });
    }
    await sleep(40);
  }
  await page.intercept(null);
  console.log(`\n=== ${label} ===`);
  console.log("first route-fallback ms:", firstFb, "| first state-row-loading ms:", firstRow, "| max rows:", maxRow, "| first content ms:", firstContent);
  for (const m of marks) console.log("  ", JSON.stringify(m));
}

await coldRun("/reports");
await sleep(1500);
await coldRun("/reports");
await chrome.cleanup();
process.exit(0);
