// One-off triage probe (test-only, not part of the suite): dump the Dashboard
// NET WORTH comparison chip exactly as it renders, plus the payload's last
// two buckets, so the truth.networth-kpi red can be attributed.
import { launchChrome, sleep } from "./harness/cdp.mjs";

const APP = process.env.LEDGER_E2E_APP || "http://127.0.0.1:8160";
const API = process.env.LEDGER_E2E_API || "http://127.0.0.1:8060";

const chrome = await launchChrome({ port: 9339 });
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
await sleep(3000);

// Unmask so the comparison reads real figures.
const chipBefore = ((await page.textOf('[data-testid="mask-chip"]')) || "").trim();
if (chipBefore === "Masked") { await page.click('[data-testid="mask-chip"]'); await sleep(900); }

const dump = await page.eval(`(() => {
  const label = [...document.querySelectorAll('p')].find(e => /^net worth\\s*\\??$/i.test((e.textContent||'').trim()));
  if (!label) return { found: false };
  const chip = label.nextElementSibling;
  return {
    found: true,
    labelText: label.innerText,
    chipText: chip ? chip.innerText : null,
    chipHTML: chip ? chip.outerHTML.slice(0, 1600) : null,
    prevHTML: label.previousElementSibling ? label.previousElementSibling.innerText : null,
  };
})()`, { awaitPromise: false });
console.log("CHIP:", JSON.stringify(dump, null, 2));

const req = [...page.requests].map((r) => r.request.url).filter((u) => u.includes("/reports/net-worth?")).pop();
console.log("REQ:", req);
if (req) {
  const path = req.replace(APP, "");
  const tokenRes = await fetch(`${API}/api/v1/auth/demo`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  const token = (await tokenRes.json()).access_token;
  const res = await fetch(API + path, { headers: { authorization: "Bearer " + token } });
  const json = await res.json();
  const b = json.buckets || [];
  console.log("BUCKETS:", b.length, "last3:", JSON.stringify(b.slice(-3)));
  if (b.length > 1) {
    const delta = Number(b[b.length - 1].net_cad) - Number(b[b.length - 2].net_cad);
    console.log("DELTA:", delta.toFixed(2), b[b.length - 2].bucket_end, "->", b[b.length - 1].bucket_end);
  }
}
await chrome.cleanup();
process.exit(0);
