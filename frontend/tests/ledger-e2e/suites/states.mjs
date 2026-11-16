// State taxonomy (docs/47 §3, §13 example 15): loading, error and stale are
// REAL states against the running API — the loading pin delays the real
// request, the error pin injects a real envelope-shaped 500 through the
// network layer, and the retry must genuinely recover.
import { sleep } from "../harness/cdp.mjs";

const ENVELOPE = { detail: "e2e injected upstream failure", code: "e2e_injected" };

export default {
  name: "states",
  async run(ctx) {
    const { page, check, note } = ctx;
    await ctx.viewport(1440);
    await ctx.demoLogin();

    // ---- loading: delay the real API and catch the pending state --------
    await page.navigate(`${ctx.app}/reports`);
    await page.waitFor(`!!document.querySelector('main')`, { timeout: 25000 }, "reports shell");
    await sleep(800);

    await page.intercept(async (p) =>
      p.request.url.includes("/api/v1/") ? { kind: "delay", ms: 2500 } : { kind: "continue" },
    );
    const mark = page.netMark();
    await page.eval(`location.href = ${JSON.stringify(ctx.app + "/dashboard")}`, { awaitPromise: false });

    let loadingSeen = null;
    let seenAtMs = null;
    // The window must outlive BOOT + delay, not just the delay: the pending
    // state exists from query-fire (after the bundle parses and mounts) until
    // the delayed response lands (t_7faf22da). Sampling is WALL-CLOCK and
    // back-to-back: on a route whose chunk is already warm the pinned
    // `route-fallback` is up for a few hundred milliseconds, and a fixed
    // 80 x 100ms cadence plus a CDP round trip on a loaded box spaces samples
    // far enough apart to step over it (measured on this candidate: the state
    // is rendered in EVERY run — first sighting 323ms-2940ms after the
    // navigation, held ~200-500ms — but the coarse poll caught it in only 1 of
    // 3 full runs). Poll tightly against a 12s deadline; if the loading state
    // exists at all inside it, it is sampled.
    const pollStart = Date.now();
    while (Date.now() - pollStart < 12000 && !loadingSeen) {
      const state = await page.eval(`(() => {
        const fb = document.querySelector('[data-testid="route-fallback"]');
        const sr = document.querySelector('[data-testid="state-row-loading"]');
        if (fb) return 'route-fallback';
        if (sr) return 'state-row-loading';
        return null; })()`, { awaitPromise: false });
      if (state) {
        loadingSeen = state;
        seenAtMs = Date.now() - pollStart;
      } else {
        await sleep(15);
      }
    }
    check(
      "state.loading",
      "a cold route with a slow API renders a loading state (example 15)",
      !!loadingSeen,
      loadingSeen
        ? `${loadingSeen} observed ${seenAtMs}ms into the delayed load`
        : "neither route-fallback nor state-row-loading observed within 12s of a 2.5s-delayed API",
    );
    const delayed = page.netSince(mark).filter((r) => r.request.url.includes("/api/v1/"));
    check("state.loading-real-request", "the pending state was reached through a real API call", delayed.length > 0, `${delayed.length} API requests`);
    note(
      `pinned loading selector observed on this path: ${loadingSeen || "none"}; ` +
        `state-row-loading rows once settled = ${await page.count('[data-testid="state-row-loading"]')}`,
    );
    await page.intercept(null);
    await page.waitFor(
      `!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]') && !document.querySelector('[data-testid="state-row-loading"]')`,
      { timeout: 20000 },
      "dashboard settles after the delay",
    );
    await sleep(600);

    // ---- error: a real envelope-shaped 500 through the network layer -----
    await page.intercept(async (p) => {
      if (p.request.url.includes("/api/v1/reports/net-worth")) {
        return { kind: "fulfill", status: 500, body: JSON.stringify(ENVELOPE) };
      }
      return { kind: "continue" };
    });
    await page.eval(`location.href = ${JSON.stringify(ctx.app + "/dashboard")}`, { awaitPromise: false });
    let errShown = false;
    for (let i = 0; i < 60 && !errShown; i++) {
      errShown = await page.exists('[data-testid="error-panel"]');
      if (!errShown) await sleep(150);
    }
    const errText = errShown ? (await page.textOf('[data-testid="error-panel"]')) || "" : "";
    check("state.error-panel", "a 500 renders the error panel", errShown, errShown ? JSON.stringify(errText.slice(0, 200)) : "error-panel never appeared");
    check(
      "state.error-detail-verbatim",
      "the envelope detail is shown verbatim (no raw 500 string)",
      errText.includes(ENVELOPE.detail),
      JSON.stringify(errText.slice(0, 240)),
    );
    const rawLeak = /Internal Server Error|Traceback|500 \(|<html/i.test(errText);
    check("state.error-no-raw", "no raw server error text leaks into the panel", !rawLeak, JSON.stringify(errText.slice(0, 160)));

    // ---- retry genuinely recovers ----------------------------------------
    const hasRetry = await page.exists('[data-testid="error-panel-retry"]');
    check("state.error-retry-present", "the error panel offers a retry", hasRetry, `retry=${hasRetry}`);
    if (hasRetry) {
      // ---- trusted press, hardened against layout shift (t_94548fa5) ------
      // On the StateRow candidate the pending sibling chart card is compact
      // and grows ~216px when its chart resolves, so a press at a rect
      // captured 2+ CDP round trips earlier can land on a <p> inside the
      // error panel (t_f24a5886: every red was recovered=false
      // netWorthRequests=0 — a missed press, not a failed retry). Three
      // independent guards:
      //   1. settle ~1.2s after the error panel is detected, so the sibling
      //      chart card has finished growing before the press;
      //   2. press at a rect re-captured immediately before dispatch, and
      //      re-arm (up to ~1.2s) if elementFromPoint there is NOT the retry
      //      control — layout still moving; shared with acceptance.mjs as
      //      page.pressValidated() in harness/cdp.mjs (t_2c3839ca);
      //   3. one re-press when the post-click net-worth count is 0, which is
      //      the kit's own definition of a trusted press that never reached
      //      the control; the second press re-resolves everything fresh.
      const waitRecovered = async () => {
        let recovered = false;
        for (let i = 0; i < 60 && !recovered; i++) {
          recovered = !(await page.exists('[data-testid="error-panel"]'));
          if (!recovered) await sleep(150);
        }
        return recovered;
      };
      const netWorthCount = () =>
        page
          .netSince(recMark)
          .map((r) => r.request.url)
          .filter((u) => u.includes("/api/v1/reports/net-worth")).length;

      await sleep(1200); // guard 1: let the sibling chart card settle
      await page.intercept(null);
      const recMark = page.netMark();
      // guard 2: the harness helper re-captures and validates the rect
      // before dispatch.
      await page.pressValidated('[data-testid="error-panel-retry"]');
      let recovered = await waitRecovered();
      let recRequests = netWorthCount();
      // guard 3: the trusted press never reached the control — re-press once
      if (recovered === false && recRequests === 0) {
        await page.pressValidated('[data-testid="error-panel-retry"]');
        recovered = await waitRecovered();
        recRequests = netWorthCount();
      }
      const body = await page.bodyText();
      // The request count is part of the verdict's evidence: a red with
      // requests=0 is the harness's trusted click landing on a rect that moved
      // under load (no press reached the control at all), not a product that
      // failed to recover from a retry that never fired.
      check(
        "state.error-retry-recovers",
        "retry clears the error panel and real data returns",
        recovered && recRequests > 0 && /NET WORTH|Net worth/i.test(body),
        `recovered=${recovered} netWorthRequests=${recRequests}`,
      );
    }

    // ---- stale: seeded demo price freshness in the truth bar -------------
    await page.navigate(`${ctx.app}/investments`);
    await page.waitFor(`!!document.querySelector('[data-testid="truth-bar"]')`, { timeout: 25000 }, "truth bar");
    await sleep(2500);
    const truth = (await page.textOf('[data-testid="truth-bar"]')) || "";
    const pricesAsOf = /Prices as of ([\d-]+)/.exec(truth);
    check(
      "state.stale-truthbar",
      "investments truth bar states the price as-of date explicitly",
      !!pricesAsOf,
      JSON.stringify(truth.replace(/\n/g, " · ").slice(0, 200)),
    );
    const strip = await page.count('[data-testid="stale-strip"]');
    note(`price as-of=${pricesAsOf?.[1] || "n/a"}; stale-strip count on demo=${strip} (stale rows required for the strip)`);
    check(
      "state.stale-strip-consistent",
      "stale strip renders only when stale rows exist (no empty strip)",
      strip === 0 || ((await page.bodyText()) || "").match(/as-of/i) !== null,
      `strip=${strip}`,
    );
  },
};
