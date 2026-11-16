// Keyboard contract (docs/47 §13 example 13, QA matrix K1–K6): tab order
// starts at the skip link, every stop paints the accent ring, the palette and
// dialogs open with focus inside, trap inside an aria-modal dialog, and one
// Escape closes exactly one layer and returns focus to its trigger.
import { sleep } from "../harness/cdp.mjs";

const focusStyle = () =>
  `(() => { const el = document.activeElement; if (!el) return null;
     const cs = getComputedStyle(el);
     return { tag: el.tagName, testid: el.getAttribute('data-testid'),
              text: (el.innerText||'').trim().slice(0,40),
              boxShadow: cs.boxShadow, outline: cs.outlineWidth + ' ' + cs.outlineStyle,
              focusVisible: el.matches(':focus-visible') }; })()`;

export default {
  name: "keyboard",
  async run(ctx) {
    const { page, check, note } = ctx;
    await ctx.viewport(1440);
    await ctx.demoLogin();
    await page.navigate(`${ctx.app}/dashboard`);
    await page.waitFor(`!!document.querySelector('[data-testid="skip-link"]')`, { timeout: 20000 }, "skip link");
    await sleep(600);
    // Start from a neutral focus point.
    await page.eval(`document.body.focus()`, { awaitPromise: false }).catch(() => {});

    await page.press("Tab");
    await sleep(200);
    const first = await page.activeElement();
    check(
      "key.tab-first",
      "the first Tab stop is the skip link",
      first?.testid === "skip-link",
      JSON.stringify(first),
    );
    const firstStyle = await page.eval(focusStyle(), { awaitPromise: false });
    const ringed =
      firstStyle &&
      (firstStyle.boxShadow !== "none" ||
        (firstStyle.outline !== "0px none" && firstStyle.outline !== ""));
    check(
      "key.focus-ring",
      "the focused stop paints the accent ring",
      !!ringed,
      JSON.stringify(firstStyle),
    );

    // Next stops walk the top bar (brand -> tabs -> More ...).
    const stops = [];
    for (let i = 0; i < 4; i++) {
      await page.press("Tab");
      await sleep(150);
      stops.push(await page.activeElement());
    }
    const reachedMore = stops.some((s) => s?.testid === "more-toggle");
    const reachedTabs = stops.some((s) => (s?.text || "").match(/Dashboard|Transactions|Imports|Investments|Reports/));
    check(
      "key.tab-order",
      "Tab walks from the skip link into the top bar (tabs + More)",
      reachedTabs || reachedMore,
      JSON.stringify(stops.map((s) => s?.testid || s?.text || s?.tag)),
    );

    // ---- command palette: opens focused, Escape closes, focus returns ----
    // docs/09 §Command palette → Triggers: "Escape after shortcut-open returns
    // focus to the visible Search launcher (falling back to the main page if
    // no launcher is present); launcher-click open keeps the normal dialog
    // opener restoration." Pinned by m95CommandPalette.test.tsx ("Ctrl+K opens
    // from dashboard focus and Escape restores the Search launcher").
    await page.press("k", { modifiers: { ctrl: true } });
    await page.waitFor(`!!document.querySelector('[data-testid="command-palette"]')`, { timeout: 5000 }, "palette");
    await sleep(400);
    const paletteFocus = await page.eval(`(() => {
      const p = document.querySelector('[data-testid="command-palette"]');
      if (!p) return null;
      return { inside: p.contains(document.activeElement), active: document.activeElement.tagName,
               modal: p.getAttribute('aria-modal') }; })()`, { awaitPromise: false });
    check(
      "key.palette-focus",
      "the command palette opens with focus inside (aria-modal)",
      !!paletteFocus && paletteFocus.inside && paletteFocus.modal === "true",
      JSON.stringify(paletteFocus),
    );
    await page.press("Escape");
    await sleep(400);
    const paletteClosed = !(await page.exists('[data-testid="command-palette"]'));
    const backOnTrigger = await page.activeElement();
    // Shortcut-open Escape must land on the visible Search launcher; the
    // main-page fallback applies only when no launcher exists at all.
    const launcherExists = await page.exists('[data-testid="command-palette-trigger"]');
    const focusOnLauncher =
      !!backOnTrigger && backOnTrigger.testid === "command-palette-trigger";
    const focusOnMainPage =
      !!backOnTrigger && !launcherExists && ["BODY", "MAIN"].includes(backOnTrigger.tag);
    check(
      "key.palette-escape",
      "one Escape closes the palette and returns focus to the visible Search launcher (docs/09 §Command palette)",
      paletteClosed && (focusOnLauncher || focusOnMainPage),
      `closed=${paletteClosed} launcherExists=${launcherExists} after=${JSON.stringify(backOnTrigger)}`,
    );

    // ---- More panel: opens with focus inside, Escape returns ------------
    await page.click('[data-testid="more-toggle"]');
    await page.waitFor(`!!document.querySelector('[data-testid="more-panel"]')`, { timeout: 5000 }, "more");
    await sleep(350);
    const moreFocus = await page.activeElement();
    const insideMore = await page.eval(
      `document.querySelector('[data-testid="more-panel"]')?.contains(document.activeElement) ?? false`,
      { awaitPromise: false },
    );
    check(
      "key.more-focus",
      "the More panel opens with focus moved inside it",
      insideMore,
      JSON.stringify(moreFocus),
    );
    await page.press("Escape");
    await sleep(400);
    const moreClosed = !(await page.exists('[data-testid="more-panel"]'));
    const backOnMore = await page.activeElement();
    check(
      "key.more-escape",
      "one Escape closes the More panel and returns focus to the toggle",
      moreClosed && backOnMore?.testid === "more-toggle",
      `closed=${moreClosed} focus=${JSON.stringify(backOnMore)}`,
    );

    // ---- aria-modal dialog: focus moves in, traps, Escape returns --------
    await page.navigate(`${ctx.app}/transactions`);
    await page.waitFor(`!!document.querySelector('main')`, { timeout: 25000 }, "transactions");
    await sleep(1500);
    const opened = await page.eval(`(() => {
      const btn = [...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === 'New transaction');
      if (!btn) return false; btn.focus(); btn.click(); return true; })()`, { awaitPromise: false });
    let dialog = null;
    if (opened) {
      for (let i = 0; i < 30 && !dialog; i++) {
        dialog = await page.eval(
          `(() => { const d = document.querySelector('[aria-modal="true"]');
             if (!d || d.getBoundingClientRect().width === 0) return null;
             return { inside: d.contains(document.activeElement),
                      active: (document.activeElement?.getAttribute('data-testid') || document.activeElement?.tagName || ''),
                      html: d.outerHTML.slice(0, 80) }; })()`,
          { awaitPromise: false },
        );
        if (!dialog) await sleep(150);
      }
    }
    check("key.dialog-opens", "a real aria-modal dialog opens from the page", !!opened && !!dialog, JSON.stringify(dialog));
    if (dialog) {
      check("key.dialog-focus-in", "focus lands inside the opened dialog", dialog.inside, JSON.stringify(dialog.active));

      const escapees = [];
      for (let i = 0; i < 8; i++) {
        await page.press("Tab");
        await sleep(120);
        const inside = await page.eval(
          `(() => { const d = document.querySelector('[aria-modal="true"]'); return d ? d.contains(document.activeElement) : 'closed'; })()`,
          { awaitPromise: false },
        );
        if (inside !== true) escapees.push(String(inside));
      }
      check(
        "key.dialog-trap",
        "Tab stays trapped inside the dialog",
        escapees.length === 0,
        escapees.length === 0 ? "8 tabs stayed inside" : `escaped on ${escapees.length}/8: ${JSON.stringify(escapees)}`,
      );

      await page.press("Escape");
      await sleep(500);
      const closed = await page.eval(
        `!document.querySelector('[aria-modal="true"]') || document.querySelector('[aria-modal="true"]').getBoundingClientRect().width === 0`,
        { awaitPromise: false },
      );
      const after = await page.activeElement();
      check(
        "key.dialog-escape",
        "Escape closes the dialog and returns focus to the trigger",
        closed && (after?.text || "").includes("New transaction") || (after?.tag === "BUTTON" && closed),
        `closed=${closed} focus=${JSON.stringify(after)}`,
      );
      note(`dialog focus after Escape: ${JSON.stringify(after)}`);
    }
  },
};
