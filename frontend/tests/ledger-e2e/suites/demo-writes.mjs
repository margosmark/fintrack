// Demo write honesty + PM rulings W1/W2 (docs/47 §4, §13 examples 3/4/17):
// the demo refusal is LOCAL and singular, GET downloads stay live, the LLM
// probe is disabled before dispatch with the exact PM copy, and no probe POST
// ever leaves the disabled control.
import { sleep } from "../harness/cdp.mjs";

const W2_COPY =
  "Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only.";

const toastCount = () =>
  `document.querySelectorAll('[role="region"][aria-label="Notifications"] > [role="status"], [role="region"][aria-label="Notifications"] > [role="alert"]').length`;

export default {
  name: "demo-writes",
  async run(ctx) {
    const { page, check, note } = ctx;
    await ctx.viewport(1440);
    await ctx.demoLogin();
    const token = await ctx.demoToken();

    // ---- 1. real refusal on the one wired DemoLock control ---------------
    await page.navigate(`${ctx.app}/investments`);
    await page.waitFor(`!!document.querySelector('[data-testid="refresh-prices"]')`, { timeout: 25000 }, "refresh control");
    await sleep(1000);
    const lock = await page.exists('[data-testid="refresh-prices"] [data-testid="demo-lock"], [data-testid="demo-lock"]');
    check("demo.lock-rendered", "demo Refresh prices carries the padlock affordance", lock, `demo-lock present=${lock}`);

    const urlBefore = await page.url();
    const mark = page.netMark();
    await page.click('[data-testid="refresh-prices"]');
    let refusal = null;
    try {
      await page.waitFor(`!!document.querySelector('[data-testid="demo-refusal"]')`, { timeout: 4000 }, "demo-refusal");
      refusal = await page.textOf('[data-testid="demo-refusal"]');
    } catch {
      refusal = null;
    }
    await sleep(700);
    const posts = page
      .netSince(mark)
      .map((r) => r.request)
      .filter((r) => r.method === "POST");
    // docs/47 §4 — no faux lock: the control stays enabled, the SERVER
    // refuses with the demo envelope, and the UI explains it locally.
    const refreshReq = page.netSince(mark).find((r) => r.request.url.includes("/portfolio/refresh"));
    let refreshStatus = null;
    let refreshCode = null;
    if (refreshReq) {
      const resp = page.responses.find((r) => r.requestId === refreshReq.requestId);
      refreshStatus = resp?.response?.status ?? null;
      try {
        const body = await page.send("Network.getResponseBody", { requestId: refreshReq.requestId });
        refreshCode = JSON.parse(body.body)?.code ?? null;
      } catch {
        /* body unavailable */
      }
    }
    const toasts = await page.eval(toastCount(), { awaitPromise: false });
    const errPanel = await page.exists('[data-testid="error-panel"]');
    const urlAfter = await page.url();

    check("demo.refusal-local", "clicking a demo write control shows demo-refusal", !!refusal, JSON.stringify(refusal));
    check(
      "demo.refusal-reason",
      "the refusal reason names the demo (aria-describedby chain)",
      !!refusal && /demo/i.test(refusal),
      JSON.stringify(refusal || ""),
    );
    check("demo.refusal-one-toast", "exactly one toast fires for the refusal", toasts === 1, `toasts=${toasts}`);
    check("demo.refusal-no-error", "no error-panel for a refused demo write", !errPanel, `error-panel=${errPanel}`);
    check("demo.refusal-no-nav", "no navigation occurs on refusal", urlBefore === urlAfter, `${urlBefore} -> ${urlAfter}`);
    check(
      "demo.refusal-server-403",
      "the refusal is the SERVER's real 403 demo_account_restricted (no faux lock)",
      posts.length > 0 && refreshStatus === 403 && refreshCode === "demo_account_restricted",
      `posts=${JSON.stringify(posts.map((p) => `${p.method} ${p.url}`))} status=${refreshStatus} code=${refreshCode}`,
    );

    // ---- 2. W1: GET downloads stay live ---------------------------------
    await page.navigate(`${ctx.app}/settings`);
    await page.waitFor(`document.body.innerText.includes('Download ZIP')`, { timeout: 25000 }, "export section");
    await sleep(600);

    const controls = await page.eval(`(() => {
      const pick = (label) => [...document.querySelectorAll('button,a[href]')]
        .filter(el => (el.innerText||'').trim().startsWith(label))
        .map(el => ({ label: (el.innerText||'').trim(), tag: el.tagName,
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
            lock: !!el.querySelector('[data-testid="demo-lock"]'),
            nearSelect: !!el.parentElement?.querySelector('select'),
            href: el.getAttribute('href') }));
      return { zip: pick('Download ZIP'), json: pick('Download JSON'), ofx: pick('Download OFX'),
               exportNow: pick('Export now'), backup: pick('Back up now'), saveSched: pick('Save export schedule') };
    })()`, { awaitPromise: false });

    // A read control is a W1 violation only if it is LOCKED or disabled with
    // no visible input explaining the state: `Download OFX` waits for the
    // account <select> beside it (a required parameter, not a demo gate).
    const reads = ["zip", "json", "ofx", "exportNow"];
    const lockedReads = reads.filter(
      (k) => controls[k].length === 0 || controls[k].some((c) => c.lock || (c.disabled && !c.nearSelect)),
    );
    check(
      "w1.downloads-live",
      "GET download controls are enabled and unlocked on demo (PM W1)",
      lockedReads.length === 0,
      lockedReads.length === 0
        ? `all four reads live; OFX waits on its account select: ${JSON.stringify(controls.ofx)}`
        : `locked/disabled: ${JSON.stringify(lockedReads)} :: ${JSON.stringify(controls)}`,
    );

    // The controls really work: the underlying GET answers 200 for demo.
    for (const [id, path] of [
      ["w1.zip-200", "/api/v1/export/zip"],
      ["w1.full-200", "/api/v1/export/full"],
    ]) {
      const res = await ctx.apiReq(token, path);
      check(id, `GET ${path} answers 200 for the demo tenant`, res.status === 200, `status=${res.status}`);
    }

    // The write siblings are the ones that must carry the lock (example 4) —
    // with the spec's ONE lockless shape: "vault-locked [`Back up now`] is
    // `disabled` with `VaultLocked`'s own reason instead, since E2EE
    // unavailability never borrows the demo lock" (docs/47 §13 ex.4). The demo
    // tenant is E2EE-unenrolled, so its vault is locked and that button is
    // legitimately disabled with a visible reason. A sibling violates only when
    // it is ENABLED with no demo-lock; a disabled one must show its reason.
    const vaultReason = await page.eval(
      `(() => { const el = document.querySelector('[data-testid="vault-locked-copy"]');
         return el && el.getClientRects().length ? (el.innerText||'').trim() : ''; })()`,
      { awaitPromise: false },
    );
    const writeControls = ["backup", "saveSched"].filter((k) => controls[k].length > 0);
    const enabledUnlockedWrites = writeControls.filter((k) =>
      controls[k].some((c) => !c.lock && !c.disabled),
    );
    check(
      "w1.writes-locked",
      "every demo write sibling is demo-locked, or vault-disabled with a visible reason (example 4)",
      writeControls.length > 0 && enabledUnlockedWrites.length === 0 && vaultReason.length > 0,
      `enabled-unlocked=${JSON.stringify(enabledUnlockedWrites)} vaultReason=${JSON.stringify(vaultReason.slice(0, 120))} :: ${JSON.stringify(controls.backup)}`,
    );
    note(`write sibling state: backup=${JSON.stringify(controls.backup)} saveSched=${JSON.stringify(controls.saveSched)}`);

    // ---- 3. W2: Test connection disabled before dispatch ----------------
    await page.waitFor(`document.body.innerText.includes('Test connection')`, { timeout: 25000 }, "llm section");
    const probeMark = page.netMark();
    const probe = await page.eval(`(() => {
      const btn = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Test connection');
      if (!btn) return null;
      const described = btn.getAttribute('aria-describedby');
      const reason = described ? document.getElementById(described) : null;
      return { disabled: !!btn.disabled, described, reason: reason ? reason.innerText.trim() : null,
               lock: !!btn.querySelector('[data-testid="demo-lock"]') || !!btn.closest('[data-testid="demo-lock"]') };
    })()`, { awaitPromise: false });

    check("w2.disabled", "demo Test connection is disabled", !!probe && probe.disabled, JSON.stringify(probe));
    check("w2.copy", "W2 copy is verbatim", !!probe && probe.reason === W2_COPY, JSON.stringify(probe?.reason));
    check("w2.describedby", "the reason is wired via aria-describedby", !!probe && probe.described === "llm-test-disabled-reason", JSON.stringify(probe?.described));
    check("w2.no-lock", "no demo-lock renders on the W2 control", !!probe && !probe.lock, JSON.stringify(probe));

    // Attempt the click anyway (trusted input) — a disabled control must not dispatch.
    if (probe) {
      await page.eval(`[...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'Test connection')?.click()`);
      await sleep(800);
      const probePosts = page
        .netSince(probeMark)
        .map((r) => r.request)
        .filter((r) => r.url.includes("/users/me/llm-test"));
      check(
        "w2.no-probe-request",
        "the disabled demo control issues no POST /users/me/llm-test",
        probePosts.length === 0,
        JSON.stringify(probePosts.map((p) => `${p.method} ${p.url}`)),
      );
    }

    // ---- 4. E2EE non-enrolled state on the demo tenant ------------------
    const badge = await page.eval(`(() => {
      const el = [...document.querySelectorAll('span,div')].find(e => /^(not enrolled|locked|unlocked)$/i.test((e.innerText||'').trim()) && (e.innerText||'').trim().length < 20);
      return el ? el.innerText.trim().toLowerCase() : null; })()`, { awaitPromise: false });
    check(
      "e2ee.demo-state",
      "demo Settings reports the vault's real E2EE state",
      badge === "not enrolled" || badge === "locked" || badge === "unlocked",
      `badge=${JSON.stringify(badge)}`,
    );
    note(`demo E2EE badge: ${JSON.stringify(badge)}`);
  },
};
