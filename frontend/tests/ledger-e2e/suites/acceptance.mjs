// Final-signature acceptance re-runs (docs/47 §13, task t_51b47d46).
//
// The preserved suite (suites/*.mjs) already pins the mask flip, B1/B2, demo
// writes, state taxonomy, nav, keyboard, responsive, static scan and truth.
// Three acceptance items in the release signature were NOT exercised in a
// browser by it, so this suite adds them — test-only, against the running
// candidate stack, never editing frontend/src:
//
//   F-10  the investments trades log prints no raw price/commission while
//         masked (and DOES print them unmasked — the control that keeps a
//         masked pass from being vacuous).
//   F-11  /forecast — end-value table, assumptions figures, event rows and
//         the chart's own aria-label carry no money while masked, same
//         control. The demo tenant seeds no scenarios, so the scenario event
//         row is covered by frontend/tests/f11ForecastMask.test.tsx and is
//         noted rather than asserted here.
//   RETRY a transient 5xx shows the error panel WITH a working retry, one
//         press is exactly one request (the in-flight guard), the retry
//         recovers in place, and a deterministic refusal (validation_error /
//         demo_account_restricted) renders the panel WITHOUT a retry and
//         without a second automatic request.
//
// Money detector: the SAME cents shape docs/46 §D2's mask parks and the
// f10/f11 unit tests assert against — ISO dates and percentages are parked
// first (a masked `Inflation: 2.00%` is legal, docs/46 §D2), then
// `\d{1,3}(?:,\d{3})*\.\d{2}` is a raw figure. Built through JSON.stringify
// so no template-literal escaping can reach the evaluated expression.
import { sleep } from "../harness/cdp.mjs";

const MONEY = "\\d{1,3}(?:,\\d{3})*\\.\\d{2}";
const PARK = ["\\d{4}-\\d{2}-\\d{2}", "\\d+(?:\\.\\d+)?\\s?%"];
const MASK = "•••••";
const NET_WORTH = "/api/v1/reports/net-worth";

/** Evaluated in-page: {missing, leaks, sample} for a root expression. */
function scanExpr(rootExpr) {
  const park = PARK.map((p) => `s = s.replace(new RegExp(${JSON.stringify(p)}, 'g'), ' ');`).join(" ");
  return `(() => {
    const root = ${rootExpr};
    if (!root) return { missing: true, leaks: [], sample: '' };
    const parts = [root.textContent || ''];
    for (const el of root.querySelectorAll('[aria-label]')) parts.push(el.getAttribute('aria-label') || '');
    let s = parts.join('\\n');
    ${park}
    const m = s.match(new RegExp(${JSON.stringify(MONEY)}, 'g'));
    return { missing: false, leaks: m || [], sample: (root.textContent || '').replace(/\\s+/g, ' ').slice(0, 220) };
  })()`;
}

const CHIP = `(() => { const b = document.querySelector('[data-testid="mask-chip"]');
  if (!b) return null; return ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim(); })()`;

const TRADES_REGION = `document.querySelector('[role="region"][aria-label="Trades log table"]')`;
const FORECAST_PAGE = `document.querySelector('main') || document.body`;
const END_VALUE = `document.querySelector('[role="region"][aria-label="End value per series"]')`;
const CHART_LABEL = `document.querySelector('[aria-label*="Net worth forecast"]')`;
const ASSUMPTIONS = `document.getElementById('forecast-assumptions')`;

async function ensureChip(page, want) {
  for (let i = 0; i < 4; i++) {
    const chip = (await page.eval(CHIP, { awaitPromise: false })) || "";
    if (chip.split(/\s+/).includes(want)) return chip;
    if (chip) {
      await page.click('[data-testid="mask-chip"]');
      await sleep(500);
      continue;
    }
    await sleep(500);
  }
  const chip = (await page.eval(CHIP, { awaitPromise: false })) || "";
  if (!chip.split(/\s+/).includes(want)) throw new Error(`mask chip never reached "${want}" (chip=${JSON.stringify(chip)})`);
  return chip;
}

/** A net-worth request count since a network marker. */
function netWorthSince(page, mark) {
  return page
    .netSince(mark)
    .map((r) => r.request.url)
    .filter((u) => u.includes(NET_WORTH)).length;
}

export default {
  name: "acceptance",
  async run(ctx) {
    const { page, check, note } = ctx;

    // Foreground the single headless target before the transient-failure
    // assertions: a hidden page pauses React Query's focus/refetch machinery
    // and reads as a product failure (known harness artifact, not a result).
    await page.send("Page.bringToFront").catch(() => {});
    await page.send("Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});

    // ---- F-10 / F-11 at two widths --------------------------------------
    for (const vp of [1440, 390]) {
      await ctx.viewport(vp);
      await ctx.demoLogin();

      // ===== F-10: the investments trades log =====
      await page.navigate(`${ctx.app}/investments`);
      await page.waitFor(`!!document.querySelector('[data-testid="mask-chip"]')`, { timeout: 25000 }, "mask chip on investments");
      await ensureChip(page, "Masked");
      await page.waitFor(`document.querySelectorAll('[role="region"][aria-label="Trades log table"] tbody tr').length > 0`, { timeout: 25000 }, "trades rows");
      await sleep(800);

      const tradesMasked = await page.eval(scanExpr(TRADES_REGION), { awaitPromise: false });
      const tradesMaskText = (await page.textOf('[role="region"][aria-label="Trades log table"]')) || "";
      check(
        `acceptance.f10-masked.${vp}`,
        `the trades log prints no raw price/commission while masked at ${vp} (security F-10)`,
        !tradesMasked.missing && tradesMasked.leaks.length === 0 && tradesMaskText.includes(MASK),
        tradesMasked.missing
          ? "trades log table region missing"
          : `leaks=${JSON.stringify(tradesMasked.leaks.slice(0, 6))} bullets=${(tradesMaskText.match(/•/g) || []).length} :: ${tradesMasked.sample}`,
      );

      // Control: the same rows DO print the figures once unmasked, so the
      // masked pass above is not vacuous (no rows / no money either way).
      await ensureChip(page, "Unmasked");
      await sleep(900);
      const tradesUnmasked = await page.eval(scanExpr(TRADES_REGION), { awaitPromise: false });
      check(
        `acceptance.f10-unmasked-control.${vp}`,
        `the same trades rows DO print raw figures unmasked at ${vp} (non-vacuous control)`,
        !tradesUnmasked.missing && tradesUnmasked.leaks.length > 0 && !(tradesUnmasked.sample || "").includes(MASK),
        `leaks=${JSON.stringify(tradesUnmasked.leaks.slice(0, 6))} :: ${tradesUnmasked.sample}`,
      );
      await ensureChip(page, "Masked");

      // ===== F-11: /forecast (page, end-value table, chart aria-label) =====
      await page.navigate(`${ctx.app}/forecast`);
      await page.waitFor(`!!document.querySelector('[data-testid="mask-chip"]')`, { timeout: 25000 }, "mask chip on forecast");
      await ensureChip(page, "Masked");
      await page.waitFor(`!!document.querySelector('[role="region"][aria-label="End value per series"]')`, { timeout: 25000 }, "end-value region");
      try {
        await page.waitFor(`!!document.querySelector('[aria-label*="Net worth forecast"]')`, { timeout: 20000 }, "forecast chart label");
      } catch {
        note(`forecast chart aria-label not rendered at ${vp}`);
      }
      await sleep(900);

      const endValue = await page.eval(scanExpr(END_VALUE), { awaitPromise: false });
      const endText = (await page.textOf('[role="region"][aria-label="End value per series"]')) || "";
      check(
        `acceptance.f11-end-value.${vp}`,
        `the /forecast end-value table is bullets with no money at ${vp} (security F-11)`,
        !endValue.missing && endValue.leaks.length === 0 && endText.includes(MASK),
        endValue.missing
          ? "end-value region missing"
          : `leaks=${JSON.stringify(endValue.leaks.slice(0, 6))} bullets=${(endText.match(/•/g) || []).length} :: ${endValue.sample}`,
      );

      const chartMasked = await page.eval(scanExpr(CHART_LABEL), { awaitPromise: false });
      const chartAriaMasked = (await page.eval(`document.querySelector('[aria-label*="Net worth forecast"]')?.getAttribute('aria-label') || ''`, { awaitPromise: false })) || "";
      check(
        `acceptance.f11-chart-aria.${vp}`,
        `the forecast chart's own aria-label carries no money while masked at ${vp} (F-11 / F-9 class)`,
        !chartMasked.missing && chartMasked.leaks.length === 0 && chartAriaMasked.trim().length > 0,
        chartMasked.missing
          ? "forecast chart aria-label missing"
          : `leaks=${JSON.stringify(chartMasked.leaks.slice(0, 6))} aria=${JSON.stringify(chartAriaMasked.slice(0, 200))}`,
      );

      const pageMasked = await page.eval(scanExpr(FORECAST_PAGE), { awaitPromise: false });
      check(
        `acceptance.f11-page.${vp}`,
        `the whole /forecast surface (text + aria-labels) carries no money while masked at ${vp}`,
        !pageMasked.missing && pageMasked.leaks.length === 0,
        pageMasked.missing ? "main surface missing" : `leaks=${JSON.stringify(pageMasked.leaks.slice(0, 8))}`,
      );

      // Assumptions — collapsed by default; open it and scan the figures.
      const opened = await page.eval(`(() => {
        const b = [...document.querySelectorAll('button')].find(x => (x.innerText || '').trim() === 'Assumptions');
        if (!b) return 'absent'; if (b.getAttribute('aria-expanded') === 'true') return 'open'; b.click(); return 'clicked'; })()`, { awaitPromise: false });
      await sleep(700);
      const assumptions = await page.eval(scanExpr(ASSUMPTIONS), { awaitPromise: false });
      const assumptionsText = (await page.textOf('#forecast-assumptions')) || "";
      check(
        `acceptance.f11-assumptions.${vp}`,
        `the /forecast assumptions figures are masked while percentages survive at ${vp} (security F-11)`,
        opened !== "absent" && !assumptions.missing && assumptions.leaks.length === 0 && /%/.test(assumptionsText),
        opened === "absent"
          ? "Assumptions control absent"
          : `state=${opened} leaks=${JSON.stringify(assumptions.leaks.slice(0, 6))} pct=${/%/.test(assumptionsText)} :: ${assumptions.sample}`,
      );
      const eventRow = /price,/.test((await page.bodyText()) || "");
      note(`demo scenario event rows on /forecast at ${vp}: ${eventRow ? "present" : "absent (demo tenant seeds no scenarios — see frontend/tests/f11ForecastMask.test.tsx for the scenario row)"}`);

      // Control: unmasking the same mounted page prints the figures back,
      // including in the chart's aria-label (docs/46 D6: no memoized money).
      await ensureChip(page, "Unmasked");
      await sleep(900);
      const pageUnmasked = await page.eval(scanExpr(FORECAST_PAGE), { awaitPromise: false });
      const chartAriaUnmasked = (await page.eval(`document.querySelector('[aria-label*="Net worth forecast"]')?.getAttribute('aria-label') || ''`, { awaitPromise: false })) || "";
      check(
        `acceptance.f11-unmasked-control.${vp}`,
        `unmasking the same /forecast mount prints money back, aria-label included, at ${vp} (non-vacuous control)`,
        !pageUnmasked.missing && pageUnmasked.leaks.length > 0 && chartAriaUnmasked !== chartAriaMasked && chartAriaMasked.length > 0,
        `pageLeaks=${pageUnmasked.leaks.length} ariaMasked=${JSON.stringify(chartAriaMasked.slice(0, 120))} ariaUnmasked=${JSON.stringify(chartAriaUnmasked.slice(0, 120))}`,
      );
      await ensureChip(page, "Masked");
    }

    // ---- RETRY: transient 5xx -> panel + one press = one request ---------
    await ctx.viewport(1440);
    await ctx.demoLogin();
    // Every interceptor is URL-scoped to the ONE query under test: fulfilling
    // everything also fails the auth probe, which logs the page out instead of
    // failing a query (the panel then never exists for a reason that is pure
    // harness).
    const transient500 = async (p) =>
      p.request.url.includes(NET_WORTH)
        ? { kind: "fulfill", status: 500, body: JSON.stringify({ detail: "e2e injected transient failure", code: "e2e_injected" }) }
        : { kind: "continue" };
    await page.intercept(transient500);
    let mark = page.netMark();
    await page.eval(`location.href = ${JSON.stringify(ctx.app + "/dashboard")}`, { awaitPromise: false });
    let panel = false;
    for (let i = 0; i < 100 && !panel; i++) {
      panel = await page.exists('[data-testid="error-panel"]');
      if (!panel) await sleep(150);
    }
    const panelText = panel ? (await page.textOf('[data-testid="error-panel"]')) || "" : "";
    const initialRequests = netWorthSince(page, mark);
    check(
      "acceptance.retry-5xx-panel",
      "a transient 5xx renders the error panel with the envelope detail verbatim",
      panel && panelText.includes("e2e injected transient failure") && !/Internal Server Error|Traceback|<html/i.test(panelText),
      `panel=${panel} requests=${initialRequests} text=${JSON.stringify(panelText.slice(0, 200))}`,
    );
    const retryBtn = await page.exists('[data-testid="error-panel-retry"]');
    check("acceptance.retry-5xx-button", "the transient 5xx panel offers a working retry", retryBtn, `retry=${retryBtn}`);

    // One press is ONE request: hold the flight open for 2s so a duplicate
    // press (or an unguarded double fire) would be visible in the log.
    await page.intercept(async (p) => {
      if (!p.request.url.includes(NET_WORTH)) return { kind: "continue" };
      await sleep(2000);
      return { kind: "fulfill", status: 500, body: JSON.stringify({ detail: "e2e injected transient failure", code: "e2e_injected" }) };
    });
    mark = page.netMark();
    // Port of the states.mjs layout-shift guards (t_94548fa5 → t_2c3839ca) —
    // this suite reproduced the missed-press class live: `requests=0` was the
    // kit's own marker for a trusted press that never reached the control
    // because a rect captured 2+ CDP round trips earlier no longer pointed at
    // the retry control once the sibling cards rendered. Three independent
    // shields:
    //   1. settle ~1.2s after the error panel mounts, so the surrounding
    //      cards have finished their post-load growth before the press;
    //   2. a rect re-captured immediately before the trusted dispatch and
    //      validated with elementFromPoint (page.pressValidated, shared with
    //      states.mjs in harness/cdp.mjs) — re-arming while layout moves;
    //   3. one re-press when the post-click request count is 0.
    await sleep(1200); // guard 1
    await page.pressValidated('[data-testid="error-panel-retry"]'); // guard 2
    // The control's own in-flight state. An isError-gated surface hands off to
    // its loading row while the refetch runs (QueryErrorPanel's documented
    // "there is nothing to hold") and a held surface shows the control
    // DISABLED + `Retrying…`. Either is honest; a control still mounted and
    // ENABLED during its own flight is not — that is the duplicate-press
    // window the ref guard exists to close.
    await sleep(180);
    const readInflight = () =>
      page.eval(
        `(() => { const b = document.querySelector('[data-testid="error-panel-retry"]');
         return b ? { text: (b.innerText || '').trim(), disabled: b.disabled === true, busy: b.getAttribute('aria-busy') } : null; })()`,
        { awaitPromise: false },
      );
    let inflight = await readInflight();
    // Outlast the deliberately slow 2s flight: after exactly ONE press the log
    // must hold exactly one net-worth request — no duplicate fire, no
    // automatic second call behind the button.
    await sleep(2700);
    let pressRequests = netWorthSince(page, mark);
    // guard 3: the trusted press never reached the control — re-press once.
    if (pressRequests === 0) {
      await page.pressValidated('[data-testid="error-panel-retry"]');
      await sleep(180);
      inflight = await readInflight();
      await sleep(2700);
      pressRequests = netWorthSince(page, mark);
    }
    const guardHeld = inflight === null || inflight.disabled === true;
    check(
      "acceptance.retry-one-press-one-request",
      "ONE press of the retry issues exactly ONE request, and the control never sits enabled mid-flight",
      pressRequests === 1 && guardHeld,
      `requests=${pressRequests} inflight=${JSON.stringify(inflight)}${inflight === null ? " (panel handed off to its loading row during the flight)" : ""}`,
    );
    const rearmedPanel = await page.exists('[data-testid="error-panel"]');
    const rearmedText = rearmedPanel ? (await page.textOf('[data-testid="error-panel"]')) || "" : "";
    const rearmed = await page.eval(
      `(() => { const b = document.querySelector('[data-testid="error-panel-retry"]');
         return b ? (b.innerText || '').trim() : null; })()`,
      { awaitPromise: false },
    );
    check(
      "acceptance.retry-rearms",
      "a retry that fails again keeps the detail verbatim and re-arms the control",
      rearmedPanel && rearmedText.includes("e2e injected transient failure") && rearmed === "Try again",
      `button=${JSON.stringify(rearmed)} text=${JSON.stringify(rearmedText.slice(0, 160))}`,
    );

    // Recovery: clear the fault, press once — one request, panel gone,
    // real data back. Same layout-shift guards as above (t_2c3839ca) —
    // pressValidated for the resolved rect, one re-press when the count is 0.
    await page.intercept(null);
    mark = page.netMark();
    const waitPanelGone = async () => {
      let gone = false;
      for (let i = 0; i < 80 && !gone; i++) {
        gone = !(await page.exists('[data-testid="error-panel"]'));
        if (!gone) await sleep(150);
      }
      return gone;
    };
    await page.pressValidated('[data-testid="error-panel-retry"]');
    let recovered = await waitPanelGone();
    await sleep(700);
    let recoverRequests = netWorthSince(page, mark);
    if (recoverRequests === 0) {
      await page.pressValidated('[data-testid="error-panel-retry"]');
      recovered = await waitPanelGone();
      await sleep(700);
      recoverRequests = netWorthSince(page, mark);
    }
    const body = (await page.bodyText()) || "";
    check(
      "acceptance.retry-recovers",
      "the retry clears the error panel and real net-worth data returns in place",
      recovered && recoverRequests === 1 && /NET WORTH|Net worth/i.test(body),
      `recovered=${recovered} requests=${recoverRequests}`,
    );

    // ---- RETRY: deterministic refusals get NO button and NO second call --
    for (const refusal of [
      { id: "validation_error", status: 422, code: "validation_error", detail: "e2e deterministic validation refusal" },
      { id: "demo_account_restricted", status: 403, code: "demo_account_restricted", detail: "e2e policy refusal" },
    ]) {
      await page.intercept(async (p) =>
        p.request.url.includes(NET_WORTH)
          ? { kind: "fulfill", status: refusal.status, body: JSON.stringify({ detail: refusal.detail, code: refusal.code }) }
          : { kind: "continue" },
      );
      mark = page.netMark();
      await page.eval(`location.href = ${JSON.stringify(ctx.app + "/dashboard")}`, { awaitPromise: false });
      let shown = false;
      for (let i = 0; i < 100 && !shown; i++) {
        shown = await page.exists('[data-testid="error-panel"]');
        if (!shown) await sleep(150);
      }
      await sleep(1800); // long enough for any automatic backoff to fire
      const btnCount = await page.count('[data-testid="error-panel-retry"]');
      const text = shown ? (await page.textOf('[data-testid="error-panel"]')) || "" : "";
      const reqs = netWorthSince(page, mark);
      check(
        `acceptance.no-retry.${refusal.id}`,
        `a deterministic ${refusal.code} refusal renders the panel WITHOUT a retry and fires no second request`,
        shown && text.includes(refusal.detail) && btnCount === 0 && reqs === 1,
        `shown=${shown} retryButtons=${btnCount} requests=${reqs} text=${JSON.stringify(text.slice(0, 180))}`,
      );
    }

    await page.intercept(null);
    await sleep(300);
  },
};
