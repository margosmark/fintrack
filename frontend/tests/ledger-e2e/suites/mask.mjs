// Display mask (docs/46, docs/47 §13 examples 6/7, QA matrix M-pins): the flip
// is instant, purely presentational, and issues ZERO network requests; the
// keyboard shortcut announces and returns focus; the choice survives SPA
// navigation but not a boot; and every money surface — including the chart
// axes — repaints with it.
import { loadavg, cpus } from "node:os";
import { sleep } from "../harness/cdp.mjs";

// A stopwatch on this box measures the BOX as much as the app: sibling agents
// running `vitest` push load1 well past the core count and a click that lands
// in 98 ms idle lands in 750 ms saturated. House rule (AGENTS.md §4): gate
// wall-clock budget pins on load, keep every functional assertion
// unconditional — the measurement is always recorded, only the verdict is
// withheld, and the detail says so explicitly.
const loadReading = () => ({ load1: loadavg()[0], cores: cpus().length });
const underLoad = (r) => r.load1 >= r.cores;

const pctLabels = () =>
  `(() => { const out = new Set(); const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
     let n; while ((n = w.nextNode())) { for (const m of (n.nodeValue||'').matchAll(/\\d+(?:\\.\\d+)?\\s*%/g)) out.add(m[0]); }
     return [...out].sort(); })()`;

const moneyCount = () =>
  `(() => { const out = new Set(); const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
     let n; while ((n = w.nextNode())) { for (const m of (n.nodeValue||'').matchAll(/\\$?-?\\d{1,3}(?:,\\d{3})*\\.\\d{2}\\b/g)) out.add(m[0]); }
     return [...out].length; })()`;

const bullets = () =>
  `(document.body.innerText.match(/•/g) || []).length`;

export default {
  name: "mask",
  async run(ctx) {
    const { page, check, note } = ctx;
    // The chip collapses to an icon below md — state lives in aria-label there.
    const chipState = `(() => { const b = document.querySelector('[data-testid="mask-chip"]');
      if (!b) return null; return ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim(); })()`;
    const chipIs = (want) => `(${chipState}).split(/\\s+/).includes(${JSON.stringify(want)})`;

    for (const vp of [1440, 390, 360]) {
      await ctx.viewport(vp);
      await ctx.demoLogin();
      // Boot resets the mask to the tenant default (docs/46 D1).
      await page.navigate(`${ctx.app}/dashboard`);
      try {
        await page.waitFor(`!!document.querySelector('[data-testid="mask-chip"]')`, { timeout: 12000 }, "mask chip");
      } catch (err) {
        note(`mask chip missing after navigation (${String(err.message).slice(0, 140)}) — re-authenticating`);
        await ctx.demoLogin();
        await page.navigate(`${ctx.app}/dashboard`);
        await page.waitFor(`!!document.querySelector('[data-testid="mask-chip"]')`, { timeout: 20000 }, "mask chip (retry)");
      }
      await sleep(600);

      // The masked body paints with the dashboard data; on a saturated box the
      // chip can be there while the bullets have not landed yet, and sampling
      // then would fail `boot` for a reason that has nothing to do with the
      // tenant default. Wait — bounded — for the first masked figure; if the
      // mask never renders bullets, the check still fails with bullets=0.
      try {
        await page.waitFor(`(${bullets()}) > 0`, { timeout: 15000 }, "masked bullets rendered");
      } catch (err) {
        note(`no masked bullet rendered within 15s at ${vp}: ${String(err.message).slice(0, 120)}`);
      }
      const chip = (await page.eval(chipState, { awaitPromise: false })) || "";
      const bulletStart = await page.eval(bullets(), { awaitPromise: false });
      check(
        `mask.boot.${vp}`,
        `boots into the demo tenant default (masked) at ${vp}`,
        chip.split(/\s+/).includes("Masked") && bulletStart > 0,
        `chip=${JSON.stringify(chip)} bullets=${bulletStart}`,
      );

      // Percentages arrive with the dashboard body. Under load the sample can
      // land before first paint and come back empty, which would fail this pin
      // for a reason that has nothing to do with the mask. Wait — bounded —
      // for a label to exist, then sample: if percentages never render, the
      // check below still fails with before=[].
      try {
        await page.waitFor(`(${pctLabels()}).length > 0`, { timeout: 15000 }, "percentage labels rendered");
      } catch (err) {
        note(`no percentage label rendered within 15s at ${vp}: ${String(err.message).slice(0, 120)}`);
      }
      const pctBefore = await page.eval(pctLabels(), { awaitPromise: false });
      const moneyBefore = await page.eval(moneyCount(), { awaitPromise: false });

      // ---- click flip: instant + ZERO network ---------------------------
      await sleep(300);
      const mark = page.netMark();
      const t0 = Date.now();
      await page.click('[data-testid="mask-chip"]');
      await page.waitFor(chipIs("Unmasked"), { timeout: 5000 }, "chip flips to Unmasked");
      const flipMs = Date.now() - t0;
      await sleep(600);
      const net = page
        .netSince(mark)
        .map((r) => r.request.url)
        .filter((u) => !u.includes("favicon"));
      const moneyAfter = await page.eval(moneyCount(), { awaitPromise: false });
      const bulletAfter = await page.eval(bullets(), { awaitPromise: false });

      const load = loadReading();
      check(
        `mask.click-instant.${vp}`,
        `mask flips on click within one paint budget at ${vp}`,
        underLoad(load) || flipMs < 500,
        underLoad(load)
          ? `measured ${flipMs}ms, budget NOT enforced — load1=${load.load1.toFixed(1)} on ${load.cores} cores (sibling suites saturating the box)`
          : `${flipMs}ms (load1=${load.load1.toFixed(1)}/${load.cores} cores)`,
      );
      if (underLoad(load)) {
        note(`click-instant.${vp} load-gated: ${flipMs}ms observed while load1=${load.load1.toFixed(1)} >= ${load.cores} cores`);
      }
      check(
        `mask.click-no-fetch.${vp}`,
        `mask flip issues ZERO network requests at ${vp}`,
        net.length === 0,
        net.length === 0 ? "0 requests" : JSON.stringify(net.slice(0, 6)),
      );
      check(
        `mask.money-appears.${vp}`,
        `real money figures replace the bullets at ${vp}`,
        moneyAfter > moneyBefore && bulletAfter < bulletStart,
        `money ${moneyBefore}→${moneyAfter}, bullets ${bulletStart}→${bulletAfter}`,
      );
      const pctAfter = await page.eval(pctLabels(), { awaitPromise: false });
      // docs/47 example 6: percentages stay READABLE through the flip — the
      // pin is one-directional (nothing readable while masked may vanish when
      // it unmasks). Extra percentages that only exist once amounts are
      // visible are new content, not a masked label being hidden.
      const lost = pctBefore.filter((p) => !pctAfter.includes(p));
      const gained = pctAfter.filter((p) => !pctBefore.includes(p));
      check(
        `mask.pct-stay.${vp}`,
        `percentage labels stay readable through the flip at ${vp}`,
        lost.length === 0 && pctBefore.length > 0,
        `before=${JSON.stringify(pctBefore)} after=${JSON.stringify(pctAfter)} lost=${JSON.stringify(lost)} gained=${JSON.stringify(gained)}`,
      );

      // ---- keyboard flip: ⌘⇧H / Ctrl+Shift+H, focus + live region --------
      const mark2 = page.netMark();
      await page.press("h", { modifiers: { ctrl: true, shift: true } });
      await page.waitFor(chipIs("Masked"), { timeout: 5000 }, "chip flips back to Masked");
      await sleep(500);
      const net2 = page
        .netSince(mark2)
        .map((r) => r.request.url)
        .filter((u) => !u.includes("favicon"));
      const focus = await page.activeElement();
      const live = (await page.textOf('[data-testid="mask-live-region"]')) || "";
      check(
        `mask.keys-no-fetch.${vp}`,
        `Ctrl+Shift+H flip issues ZERO network requests at ${vp}`,
        net2.length === 0,
        net2.length === 0 ? "0 requests" : JSON.stringify(net2.slice(0, 6)),
      );
      check(
        `mask.keys-focus.${vp}`,
        `keyboard flip returns focus to the mask chip at ${vp}`,
        focus?.testid === "mask-chip",
        JSON.stringify(focus),
      );
      check(
        `mask.keys-announce.${vp}`,
        `keyboard flip announces through the live region at ${vp}`,
        live.trim().length > 0,
        JSON.stringify(live.trim().slice(0, 120)),
      );

      // ---- SPA navigation keeps the choice; a boot restores the default ---
      await page.navigate(`${ctx.app}/investments`);
      await page.waitFor(`!!document.querySelector('[data-testid="mask-chip"]')`, { timeout: 20000 }, "chip on investments");
      await sleep(700);
      const afterNav = (await page.eval(chipState, { awaitPromise: false })) || "";
      check(
        `mask.spa-keeps.${vp}`,
        `mask survives SPA navigation at ${vp} (docs/46 D1)`,
        afterNav.split(/\s+/).includes("Masked"),
        `chip=${JSON.stringify(afterNav)}`,
      );

      await page.navigate(`${ctx.app}/investments`);
      await sleep(1200);
      const afterBoot = (await page.eval(chipState, { awaitPromise: false })) || "";
      check(
        `mask.boot-resets.${vp}`,
        `a fresh boot returns to the tenant default at ${vp} (docs/46 D1)`,
        afterBoot.split(/\s+/).includes("Masked"),
        `chip=${JSON.stringify(afterBoot)}`,
      );

      // ---- F-9: masked ACCESSIBLE ALTERNATIVES must not leak raw money ----
      // The donut's aria-label printed "total 211004." and "47743.27 USD"
      // while every visible cell showed bullets (security F-9 at 6d4e445).
      // Screen-reader text is user-facing copy: while masked, no aria-label,
      // svg <title>/<desc> or role=img label may carry cents-precision
      // figures, currency-code-adjacent amounts, or bare 4+ digit totals.
      // Percentages ("77%") and dates ("2026-10-15") stay legal (example 6).
      const altLeaks = await page.eval(
        `(() => {
           const samples = [];
           for (const el of document.querySelectorAll('[aria-label],svg title,svg desc,[role="img"]')) {
             const t = el.getAttribute?.('aria-label') || el.textContent || '';
             if (t.trim()) samples.push(t.trim());
           }
           const LEAK = /\\d+\\.\\d{2}(?![\\d%])|\\$\\s*\\d|(?:CAD|USD)\\s+\\d(?![\\d\\s*%])|(?<![\\d-.,])\\d{4,}(?![\\d%,]|[-.]\\d)/i;
           return samples.filter((s) => LEAK.test(s)).slice(0, 6);
         })()`,
        { awaitPromise: false },
      );
      check(
        `mask.alt-no-leak.${vp}`,
        `masked aria-labels/svg alternatives carry no raw amounts at ${vp} (security F-9)`,
        altLeaks.length === 0,
        altLeaks.length === 0 ? "0 leaks across all labelled nodes" : JSON.stringify(altLeaks),
      );

      // ---- B1: chart axis must repaint with the mask, BOTH directions -----
      // The fix (t_fe3547f4) repaints through a reactive tickFormatter — no
      // refetch, no remount. Pin all three on the SAME mounted /investments
      // route: masked→unmasked, unmasked→masked, zero network in each flip
      // window, and the recharts wrapper node surviving both flips (an
      // expando property proves it is the SAME DOM node, not an equal copy).
      await page.waitFor(`!!document.querySelector('[data-testid="portfolio-value"]')`, { timeout: 25000 }, "portfolio value");
      await sleep(2000);
      const axis = () =>
        `[...document.querySelectorAll('svg text')].map(t=>t.textContent).filter(Boolean)`;
      await page.eval(
        `(() => { const el = document.querySelector('.recharts-wrapper'); if (el) el.__e2eMounted = 1; return !!el; })()`,
        { awaitPromise: false },
      );
      const ticksMasked = await page.eval(axis(), { awaitPromise: false });
      const moneyTicksMasked = ticksMasked.filter((t) => /\$?\d/.test(t) && !/[A-Za-z]{3}\s*\d/.test(t));

      // direction 1: masked -> unmasked
      const flip1 = page.netMark();
      await page.click('[data-testid="mask-chip"]');
      await page.waitFor(chipIs("Unmasked"), { timeout: 5000 }, "chip flips to Unmasked (axis phase)");
      try {
        await page.waitFor(`JSON.stringify(${axis()}) !== ${JSON.stringify(JSON.stringify(ticksMasked))}`, { timeout: 10000 }, "axis repaints (unmasked)");
      } catch (err) {
        note(`axis ticks did not visibly change within 10s after unmasking at ${vp}: ${String(err.message).slice(0, 120)}`);
      }
      const ticksUnmasked = await page.eval(axis(), { awaitPromise: false });
      const netFlip1 = page
        .netSince(flip1)
        .map((r) => r.request.url)
        .filter((u) => !u.includes("favicon"));
      const changed = JSON.stringify(ticksMasked) !== JSON.stringify(ticksUnmasked);
      const stillBullet = ticksUnmasked.some((t) => t.includes("•"));
      check(
        `mask.chart-axis.${vp}`,
        `chart axis ticks repaint when the mask flips at ${vp} (QA blocker B1)`,
        changed && !stillBullet,
        `masked=${JSON.stringify(ticksMasked.slice(-8))} unmasked=${JSON.stringify(ticksUnmasked.slice(-8))}`,
      );

      // direction 2: unmasked -> masked, same mounted route, no navigation
      const flip2 = page.netMark();
      await page.click('[data-testid="mask-chip"]');
      await page.waitFor(chipIs("Masked"), { timeout: 5000 }, "chip flips back to Masked (axis phase)");
      try {
        await page.waitFor(`(${axis()}).some(t=>t.includes('•'))`, { timeout: 10000 }, "axis repaints (masked)");
      } catch (err) {
        note(`axis ticks did not return to bullets within 10s after re-masking at ${vp}: ${String(err.message).slice(0, 120)}`);
      }
      const ticksBack = await page.eval(axis(), { awaitPromise: false });
      const netFlip2 = page
        .netSince(flip2)
        .map((r) => r.request.url)
        .filter((u) => !u.includes("favicon"));
      const backChanged = JSON.stringify(ticksUnmasked) !== JSON.stringify(ticksBack);
      const backToBullets = ticksBack.some((t) => t.includes("•"));
      check(
        `mask.chart-axis-reverse.${vp}`,
        `chart axis ticks repaint back to bullets on the return flip at ${vp} (same mounted route)`,
        backChanged && backToBullets,
        `unmasked=${JSON.stringify(ticksUnmasked.slice(-8))} back=${JSON.stringify(ticksBack.slice(-8))}`,
      );
      check(
        `mask.chart-axis-no-fetch.${vp}`,
        `both axis repaint flips issue ZERO network requests at ${vp}`,
        netFlip1.length === 0 && netFlip2.length === 0,
        `flip1=${netFlip1.length === 0 ? 0 : JSON.stringify(netFlip1.slice(0, 4))} flip2=${netFlip2.length === 0 ? 0 : JSON.stringify(netFlip2.slice(0, 4))}`,
      );
      const remountProbe = await page.eval(
        `(() => { const el = document.querySelector('.recharts-wrapper');
           return { probe: !!el && el.__e2eMounted === 1, path: location.pathname }; })()`,
        { awaitPromise: false },
      );
      check(
        `mask.chart-axis-no-remount.${vp}`,
        `both axis flips repaint in place — same chart node, no navigation at ${vp}`,
        remountProbe.probe === true && remountProbe.path === "/investments",
        JSON.stringify(remountProbe),
      );
      if (vp === 1440) {
        note(`money ticks while masked: ${JSON.stringify(moneyTicksMasked.slice(-6))}`);
      }
    }

    await ctx.viewport(1440);
    await ctx.demoLogin();
  },
};
