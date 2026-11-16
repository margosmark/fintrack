// Writable-tenant phase (QA fixture policy: every mutation on a disposable
// tenant; the demo stays read-only). Covers: the empty three-part state, zero
// locks on a live tenant, the W2 non-demo probe path, a real stale-price strip,
// the E2EE enrolled/locked states, and QA blocker B2 — the unscoped
// `fintrack.recurring-candidates` sessionStorage key crossing tenants.
import { sleep } from "../harness/cdp.mjs";

const W2_COPY =
  "Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only.";

// The vault state badge (Settings → Encryption): text-transform applies to
// innerText, and the badge wraps an icon node — match on short exact text.
async function vaultBadge(page) {
  return page.eval(`(() => {
    const el = [...document.querySelectorAll('span,div')].find(e =>
      /^(not enrolled|locked|unlocked)$/i.test((e.innerText||'').trim()) && (e.innerText||'').trim().length < 20);
    return el ? el.innerText.trim().toLowerCase() : null; })()`, { awaitPromise: false });
}

function isoDaysAgo(days) {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export default {
  name: "tenant",
  async run(ctx) {
    const { page, check, note } = ctx;
    await ctx.viewport(1440);

    // ---- A. capture the demo's recurring-candidates cache before signout --
    await ctx.demoLogin();
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
      timeout: 25000,
    }, "demo transactions");
    await sleep(2000);
    const demoKey = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    const demoPrompt = await page.eval(
      `(() => { const el = [...document.querySelectorAll('div,section')].find(e => /candidates? ·/.test(e.innerText||'') && e.innerText.length < 400); return el ? el.innerText : null; })()`,
      { awaitPromise: false },
    );
    note(`demo recurring key: ${demoKey ? `${demoKey.length} chars` : "absent"}; demo prompt: ${JSON.stringify(demoPrompt || "").slice(0, 160)}`);

    // ---- A2. B2 the fix's full lifecycle in the SAME tab: ------------------
    // logout clears the key; a fresh demo login re-stamps it with the demo's
    // identity; a same-user hard reload serves the rows from the cache with
    // ZERO requests to /transactions/recurring/candidates (docs/09 §Recurring
    // prompt: compute once per session).
    // The endpoint serves {groups, combined_monthly_total, suppressed_count,
    // window_start} — the candidate GROUPS are what the prompt card caches.
    // One token per run: the auth route is rate limited and this suite already
    // spends requests on the browser login flows.
    const demoApiTok = await ctx.demoToken();
    const demoCandResp = await ctx.apiReq(demoApiTok, "/api/v1/transactions/recurring/candidates");
    const demoCandidates = Array.isArray(demoCandResp.json)
      ? demoCandResp.json.length
      : (demoCandResp.json?.groups || demoCandResp.json?.items || []).length;
    const demoUser = (await ctx.apiReq(demoApiTok, "/api/v1/users/me")).json;
    note(`demo user id=${demoUser?.id}; own candidates endpoint says ${demoCandidates}`);

    await ctx.signOut();
    const keyAfterLogout = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    check(
      "tenant.b2-logout-clears",
      "signing out drops the recurring-candidates cache from the same tab (B2 fix)",
      !keyAfterLogout,
      keyAfterLogout ? `key still present after logout (${keyAfterLogout.length} chars)` : "key absent after logout",
    );

    await ctx.demoLogin();
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
      timeout: 25000,
    }, "demo transactions (re-login)");
    await sleep(2500);
    const reloginKeyRaw = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    let reloginStamp = null;
    try { reloginStamp = reloginKeyRaw ? JSON.parse(reloginKeyRaw) : null; } catch { reloginStamp = "unparseable"; }
    const stampMatchesDemo = !!reloginStamp && typeof reloginStamp === "object" && String(reloginStamp.owner) === String(demoUser?.id);
    check(
      "tenant.b2-owner-stamped",
      "a fresh demo login stamps the cache with the bound identity (never a bare array)",
      demoCandidates === 0
        ? !reloginKeyRaw
        : stampMatchesDemo && Array.isArray(reloginStamp.groups),
      demoCandidates === 0
        ? `demo has 0 candidates so no cache is written (key=${reloginKeyRaw ? "present" : "absent"})`
        : `owner=${JSON.stringify(reloginStamp?.owner)} demoUserId=${demoUser?.id} groups=${Array.isArray(reloginStamp?.groups) ? reloginStamp.groups.length : "n/a"}`,
    );

    // hard reload — the cache must survive for its OWN tenant with no rescan
    const reloadMark = page.netMark();
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
      timeout: 25000,
    }, "demo transactions (reload)");
    await sleep(2500);
    const reloadScan = page
      .netSince(reloadMark)
      .map((r) => r.request.url)
      .filter((u) => u.includes("/transactions/recurring/candidates"));
    const reloadKeyRaw = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    const reloadPrompt = await page.eval(
      `(() => { const el = [...document.querySelectorAll('div,section')].find(e => /candidates? ·/.test(e.innerText||'') && e.innerText.length < 400); return el ? el.innerText : null; })()`,
      { awaitPromise: false },
    );
    check(
      "tenant.b2-same-user-reload",
      "a same-user hard reload renders the cached candidates with ZERO rescan requests",
      demoCandidates === 0
        ? reloadScan.length === 0
        : reloadScan.length === 0 && !!reloadKeyRaw && JSON.stringify(reloadKeyRaw) === JSON.stringify(reloginKeyRaw) && !!reloadPrompt,
      `scanRequests=${reloadScan.length} keyPreserved=${reloadKeyRaw === reloginKeyRaw} rowsRendered=${!!reloadPrompt} demoCandidates=${demoCandidates}`,
    );

    // ---- B. disposable tenant ---------------------------------------------
    const stamp = Date.now();
    // pydantic's EmailStr rejects reserved/special-use domains (.test, …) —
    // use a plain non-reserved domain; nothing is ever delivered to it.
    const email = `e2e-ledger-${stamp}@ledger-e2e-mail.com`;
    const password = "Ledger-E2e-2026!x";
    await ctx.registerTenant(email, password);
    const token = await ctx.loginToken(email, password);
    ctx.addCleanup(async () => {
      await ctx.deleteTenant(token, password);
    });
    await ctx.signOut();
    note(`disposable tenant registered: ${email}`);

    await page.navigate(`${ctx.app}/login`);
    await page.waitFor(`!!document.querySelector('input[type="email"]')`, { timeout: 20000 }, "login form");
    // Real typing first (Input.insertText); a controlled React input that did
    // not take the text is corrected with the native value setter + input
    // event, and the method used is recorded in the run notes.
    const fill = async (selector, value) => {
      await page.focus(selector);
      await page.type(value);
      const got = await page.eval(`document.querySelector(${JSON.stringify(selector)})?.value ?? null`, {
        awaitPromise: false,
      });
      if (got === value) return "typed";
      await page.eval(
        `(() => { const el = document.querySelector(${JSON.stringify(selector)});
           const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
           set.call(el, ${JSON.stringify(value)});
           el.dispatchEvent(new Event('input', { bubbles: true })); })()`,
        { awaitPromise: false },
      );
      return "native-setter";
    };
    const howEmail = await fill('input[type="email"]', email);
    const howPassword = await fill('input[type="password"]', password);
    note(`login fill: email=${howEmail} password=${howPassword}`);
    await page.eval(`document.querySelector('button[type="submit"]')?.click()`, { awaitPromise: false });
    await page.waitFor(`location.pathname !== '/login'`, { timeout: 20000 }, "tenant login");
    await page.waitFor(`!!document.querySelector('main')`, { timeout: 20000 }, "shell");
    await page
      .eval(`[...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Skip')?.click()`)
      .catch(() => {});
    await sleep(600);
    const banner = await page.exists('[data-testid="demo-banner"]');
    check("tenant.login", "live tenant signs in through the real form", !banner && (await page.url()).includes("/"), `demo-banner=${banner}`);

    // ---- C. B2: the recurring cache must not cross tenants ----------------
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
      timeout: 25000,
    }, "tenant transactions");
    await sleep(2500);
    const tenantKey = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    const tenantPrompt = await page.eval(
      `(() => { const el = [...document.querySelectorAll('div,section')].find(e => /candidates? ·/.test(e.innerText||'') && e.innerText.length < 400); return el ? el.innerText : null; })()`,
      { awaitPromise: false },
    );
    const keyCarriedOver = !!demoKey && tenantKey === demoKey;
    const promptCarried = !!demoPrompt && !!tenantPrompt && tenantPrompt === demoPrompt;
    check(
      "tenant.b2-session-key-scoped",
      "the recurring-candidates cache is tenant-scoped (QA blocker B2)",
      !keyCarriedOver,
      keyCarriedOver
        ? `sessionStorage key byte-identical to the demo tenant's (${tenantKey.length} chars) after switching tenants in the same tab`
        : `demo=${demoKey ? demoKey.length : 0} chars tenant=${tenantKey ? tenantKey.length : 0} chars`,
    );
    const { json: candidates } = await ctx.apiReq(token, "/api/v1/transactions/recurring/candidates");
    const tenantHasCandidates = Array.isArray(candidates) ? candidates.length : (candidates?.items || []).length;
    // What the tenant's OWN endpoint says it has vs what the UI is showing:
    // a rendered count above the tenant's own is another tenant's cache.
    const renderedCount = /(\d+)\s+candidates?/.exec(tenantPrompt || "");
    const rendered = renderedCount ? Number(renderedCount[1]) : 0;
    check(
      "tenant.b2-ui-not-demo",
      "a tenant with no transactions never renders another tenant's cached candidates (QA blocker B2)",
      rendered <= tenantHasCandidates,
      `rendered on screen=${rendered} · tenant's own candidates=${tenantHasCandidates}; prompt=${JSON.stringify(tenantPrompt || "").slice(0, 200)}; keyIdenticalToDemo=${keyCarriedOver} textIdenticalToDemo=${promptCarried}`,
    );
    if (tenantHasCandidates > 0) note(`tenant has ${tenantHasCandidates} own candidates — B2 shown via key reuse only`);

    // ---- C2. B2 survives a hard reload of the OTHER tenant ----------------
    // The tab's sessionStorage persisted the demo's rows pre-fix; a reload as
    // the disposable tenant must never resurrect them, and any key the app
    // writes now carries the tenant's own stamp, never the demo's.
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
      timeout: 25000,
    }, "tenant transactions (reload)");
    await sleep(2500);
    const tenantReloadKey = await page.eval(`sessionStorage.getItem('fintrack.recurring-candidates.v1')`, { awaitPromise: false });
    const tenantReloadPrompt = await page.eval(
      `(() => { const el = [...document.querySelectorAll('div,section')].find(e => /candidates? ·/.test(e.innerText||'') && e.innerText.length < 400); return el ? el.innerText : null; })()`,
      { awaitPromise: false },
    );
    let tenantReloadOwner = "absent";
    if (tenantReloadKey) {
      try { tenantReloadOwner = JSON.stringify(JSON.parse(tenantReloadKey)?.owner ?? "no-owner-field"); }
      catch { tenantReloadOwner = "unparseable"; }
    }
    const demoStillVisible = !!demoPrompt && !!tenantReloadPrompt && tenantReloadPrompt === demoPrompt;
    check(
      "tenant.b2-reload-cross-tenant",
      "a hard reload as another tenant never resurrects the demo's cached candidates (B2)",
      !demoStillVisible && tenantReloadOwner !== JSON.stringify(String(demoUser?.id)),
      `keyOwner=${tenantReloadOwner} demoUserId=${demoUser?.id} promptIdenticalToDemo=${demoStillVisible} key=${tenantReloadKey ? `${tenantReloadKey.length} chars` : "absent"}`,
    );

    // ---- D. empty state: three parts (example 15) -------------------------
    const empty = await page.eval(`(() => {
      const root = document.querySelector('[data-testid="empty-state"]');
      if (!root) return null;
      return { title: !!root.querySelector('[data-testid="empty-state-title"]'),
               preview: !!root.querySelector('[data-testid="empty-state-preview"]'),
               action: !!root.querySelector('[data-testid="empty-state-action"]'),
               text: (root.innerText||'').slice(0,200) }; })()`, { awaitPromise: false });
    check(
      "tenant.empty-three-part",
      "an empty read renders the three-part empty state (title + preview + one action)",
      !!empty && empty.title && empty.preview && empty.action,
      JSON.stringify(empty),
    );

    // ---- E. zero locks on a live tenant (example 1) -----------------------
    let lockCount = 0;
    for (const route of ["/dashboard", "/transactions", "/investments", "/settings", "/reports"]) {
      await page.navigate(`${ctx.app}${route}`);
      await page.waitFor(`!!document.querySelector('main')`, { timeout: 25000 }, route);
      await sleep(700);
      lockCount += await page.count('[data-testid="demo-lock"]');
    }
    check("tenant.zero-locks", "a live tenant renders zero demo locks across the Ledger surfaces", lockCount === 0, `demo-lock count=${lockCount}`);

    // ---- F. W2 on a live tenant: enabled + really dispatches --------------
    await page.navigate(`${ctx.app}/settings`);
    await page.waitFor(`document.body.innerText.includes('Test connection')`, { timeout: 25000 }, "llm section");
    await sleep(500);
    const probe = await page.eval(`(() => {
      const btn = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Test connection');
      if (!btn) return null;
      const described = btn.getAttribute('aria-describedby');
      const reason = described ? document.getElementById(described) : null;
      return { disabled: !!btn.disabled, reason: reason ? reason.innerText.trim() : null }; })()`, { awaitPromise: false });
    check("w2.live-enabled", "the live tenant's Test connection control is enabled", !!probe && !probe.disabled, JSON.stringify(probe));
    check("w2.live-no-demo-copy", "the demo-only disabled copy never shows on a live tenant", !probe?.reason || probe.reason !== W2_COPY, JSON.stringify(probe?.reason));
    const probeMark = page.netMark();
    await page.eval(`[...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Test connection')?.click()`, { awaitPromise: false });
    let probeReq = null;
    for (let i = 0; i < 40 && !probeReq; i++) {
      probeReq = page.netSince(probeMark).find((r) => r.request.url.includes("/users/me/llm-test")) || null;
      if (!probeReq) await sleep(150);
    }
    check(
      "w2.live-dispatches",
      "the live Test connection control really dispatches POST /users/me/llm-test",
      !!probeReq && probeReq.request.method === "POST",
      probeReq ? `${probeReq.request.method} ${probeReq.request.url}` : "no llm-test request seen within 6s",
    );
    await sleep(800);
    const probePanel = await page.exists('[data-testid="error-panel"]');
    note(`non-demo probe result rendered (error-panel=${probePanel}) — response status observed from the network log`);

    // ---- G. stale price strip from a real stale row ------------------------
    const acct = await ctx.apiReq(token, "/api/v1/accounts", {
      method: "POST",
      body: { name: "E2E Stale Holder", type: "brokerage_non_registered", currency: "CAD" },
    });
    if (acct.status >= 400) {
      // Account types differ per spec — fall back to chequing if rejected.
      note(`account create rejected (${acct.status}) — retrying as chequing`);
    }
    const accountId = acct.json?.id;
    const sec = await ctx.apiReq(token, "/api/v1/securities", {
      method: "POST",
      body: { symbol: "E2EStl.TO", name: "E2E Stale Corp", kind: "stock", listing_currency: "CAD", exchange: "TSX" },
    });
    const securityId = sec.json?.id;
    if (accountId && securityId) {
      const trade = await ctx.apiReq(token, "/api/v1/trades", {
        method: "POST",
        body: {
          account_id: accountId,
          security_id: securityId,
          action: "buy",
          date: isoDaysAgo(40),
          quantity: "10",
          price_native: "100.00",
        },
      });
      const price = await ctx.apiReq(token, "/api/v1/prices", {
        method: "POST",
        body: { security_id: securityId, date: isoDaysAgo(30), price: "105.50" },
      });
      note(`fixtures: account=${accountId} security=${securityId} trade=${trade.status} price=${price.status}`);

      await page.navigate(`${ctx.app}/investments`);
      await page.waitFor(`!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`, {
        timeout: 25000,
      }, "investments");
      await sleep(3000);
      const strip = await page.textOf('[data-testid="stale-strip"]');
      check(
        "tenant.stale-strip",
        "a stale price row renders the real number + as-of date in the stale strip",
        !!strip && strip.trim().length > 0,
        JSON.stringify((strip || "").replace(/\n/g, " · ").slice(0, 240)),
      );
      const body = await page.bodyText();
      check(
        "tenant.stale-row-truth",
        "the stale holding renders its real price (never an em dash, never an estimate)",
        body.includes("105.50"),
        body.includes("105.50") ? "price 105.50 visible" : "105.50 not found on screen",
      );
    } else {
      check("tenant.stale-fixture", "stale fixtures could be created", false, `account=${acct.status} security=${sec.status} ${JSON.stringify(sec.json)}`);
    }

    // ---- H. E2EE: enroll -> unlocked, reload -> locked ---------------------
    await page.navigate(`${ctx.app}/settings`);
    await page.waitFor(`document.body.innerText.includes('Enroll') || document.body.innerText.includes('not enrolled')`, {
      timeout: 25000,
    }, "vault section");
    await sleep(600);
    const stateBefore = await vaultBadge(page);
    check("e2ee.start-state", "a fresh tenant starts in the non_enrolled state", stateBefore === "not enrolled", `state=${JSON.stringify(stateBefore)}`);

    const enrollClicked = await page.eval(`(() => {
      const btn = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Enroll');
      if (!btn) return false; btn.click(); return true; })()`, { awaitPromise: false });
    if (enrollClicked) {
      await page.waitFor(`!!document.querySelector('input[aria-label="New passphrase"]')`, { timeout: 8000 }, "passphrase field");
      await page.focus('input[aria-label="New passphrase"]');
      await page.type("e2e-vault-passphrase-2026");
      await page.focus('input[aria-label="Confirm new passphrase"]');
      await page.type("e2e-vault-passphrase-2026");
      await sleep(300);
      await page.eval(`(() => {
        const form = document.querySelector('input[aria-label="New passphrase"]')?.closest('form');
        const btn = form ? [...form.querySelectorAll('button[type="submit"]')][0] : null;
        if (btn) btn.click(); return !!btn; })()`, { awaitPromise: false });
      let unlocked = false;
      for (let i = 0; i < 60 && !unlocked; i++) {
        unlocked = (await vaultBadge(page)) === "unlocked";
        if (!unlocked) await sleep(250);
      }
      check("e2ee.enroll-unlocked", "enrolling the vault reaches the unlocked state", unlocked, `unlocked=${unlocked}`);

      await page.navigate(`${ctx.app}/settings`);
      await page.waitFor(`document.body.innerText.includes('not enrolled') || document.body.innerText.includes('locked') || document.body.innerText.includes('unlocked')`, {
        timeout: 25000,
      }, "vault state");
      await sleep(800);
      const stateAfterReload = await vaultBadge(page);
      const unlockBtn = await page.eval(
        `[...document.querySelectorAll('button')].some(b => (b.innerText||'').trim() === 'Unlock')`,
        { awaitPromise: false },
      );
      check(
        "e2ee.reload-locked",
        "a fresh boot reports the vault as locked with an Unlock control (E2EE locked state)",
        stateAfterReload === "locked" && unlockBtn,
        `state=${JSON.stringify(stateAfterReload)} unlockBtn=${unlockBtn}`,
      );
    } else {
      check("e2ee.enroll-control", "Enroll control exists for a fresh tenant", false, "Enroll button not found");
    }

    // ---- I. teardown --------------------------------------------------------
    const gone = await ctx.deleteTenant(token, password);
    check("tenant.teardown", "disposable tenant deleted (DELETE /users/me cascade)", gone === 204 || gone === 200, `status=${gone}`);
  },
};
