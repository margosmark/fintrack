// Navigation (docs/47 §1.1–1.2, QA matrix G1/G2): the More panel and the phone
// shell reach every real route with real content — no placeholder hrefs, no
// dead fragments, Escape closes one layer at a time and returns focus.
import { sleep } from "../harness/cdp.mjs";

export default {
  name: "nav",
  async run(ctx) {
    const { page, check, note } = ctx;

    // ---------------- desktop: More panel --------------------------------
    await ctx.viewport(1440);
    await ctx.demoLogin();
    await page.navigate(`${ctx.app}/dashboard`);
    await page.waitFor(`!!document.querySelector('[data-testid="more-toggle"]')`, { timeout: 20000 }, "more toggle");
    await sleep(500);

    await page.click('[data-testid="more-toggle"]');
    await page.waitFor(`!!document.querySelector('[data-testid="more-panel"]')`, { timeout: 5000 }, "panel");
    await sleep(400);
    // GroupLabel renders a styled div (not a heading element) — read the
    // panel's own text, uppercased by CSS (PRIMARY / TOOLS / ACCOUNTS).
    const panelText = (await page.textOf('[data-testid="more-panel"]')) || "";
    const groups = ["Primary", "Tools", "Accounts"].filter((g) => new RegExp(g, "i").test(panelText));
    const links = await page.eval(
      `[...document.querySelectorAll('[data-testid="more-panel"] a[href]')].map(a=>({href:a.getAttribute('href'), label:(a.innerText||'').trim().split('\\n')[0]}))`,
      { awaitPromise: false },
    );
    check(
      "nav.more-groups",
      "More panel renders its three groups (Primary / Tools / Accounts)",
      groups.length === 3,
      `${JSON.stringify(groups)} panel=${JSON.stringify(panelText.slice(0, 160))}`,
    );
    check(
      "nav.more-links",
      "More panel exposes every destination as a real href",
      links.length >= 15 && links.every((l) => l.href && l.href.startsWith("/")),
      `${links.length} links: ${JSON.stringify(links.slice(0, 6))}`,
    );

    // Walk the destinations: each must land on a working route with content.
    const destinations = links.filter((l) => !/^\/accounts\/\d+$/.test(l.href));
    const accountLinks = links.filter((l) => /^\/accounts\/\d+$/.test(l.href));
    const broken = [];
    const visited = [];
    for (const dest of destinations) {
      await page.navigate(`${ctx.app}/dashboard`);
      await sleep(350);
      await page.click('[data-testid="more-toggle"]');
      await sleep(350);
      const clicked = await page.eval(`(() => {
        const a = [...document.querySelectorAll('[data-testid="more-panel"] a[href]')].find(a => a.getAttribute('href') === ${JSON.stringify(dest.href)});
        if (!a) return false; a.click(); return true; })()`, { awaitPromise: false });
      if (!clicked) {
        broken.push(`${dest.href} (link not found in panel)`);
        continue;
      }
      try {
        await page.waitFor(`location.pathname === ${JSON.stringify(dest.href.split("?")[0])}`, { timeout: 8000 }, "pathname");
      } catch {
        broken.push(`${dest.href} (no navigation)`);
        continue;
      }
      await page.waitFor(
        `!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`,
        { timeout: 15000 },
        "settled",
      ).catch(() => {});
      await sleep(250);
      const state = await page.eval(`(() => ({
        err: !!document.querySelector('[data-testid="error-panel"]'),
        notFound: !!document.querySelector('[data-testid="not-found"]'),
        empty: !!document.querySelector('[data-testid="empty-state"]'),
        text: (document.querySelector('main')?.innerText || '').trim().length,
      }))()`, { awaitPromise: false });
      visited.push(`${dest.href}:${state.err ? "ERR" : state.text > 40 ? "ok" : "thin"}`);
      if (state.err || state.notFound || state.text <= 40) broken.push(`${dest.href} (${JSON.stringify(state)})`);
    }
    check(
      "nav.more-destinations",
      "every More destination lands on a working, populated route",
      broken.length === 0,
      broken.length === 0 ? `${destinations.length} destinations visited` : `broken: ${JSON.stringify(broken)}`,
    );
    note(`More destinations visited: ${visited.join(", ")}`);

    // Account rows navigate to the real account route too.
    if (accountLinks.length > 0) {
      await page.navigate(`${ctx.app}/dashboard`);
      await sleep(400);
      await page.click('[data-testid="more-toggle"]');
      await sleep(350);
      const ok = await page.eval(`(() => {
        const a = [...document.querySelectorAll('[data-testid="more-panel"] a[href]')].find(a => /^\\/accounts\\/\\d+$/.test(a.getAttribute('href')||''));
        if (!a) return false; a.click(); return true; })()`, { awaitPromise: false });
      let landed = false;
      if (ok) {
        try {
          await page.waitFor(`/^\\/accounts\\/\\d+$/.test(location.pathname)`, { timeout: 8000 }, "account route");
          landed = true;
        } catch {
          landed = false;
        }
      }
      check("nav.account-row", "More's account rows navigate to the account route", landed, `${accountLinks.length} account rows`);
    }

    // ---------------- phone: drawer + tab bar ----------------------------
    for (const vp of [390, 360]) {
      await ctx.viewport(vp);
      await ctx.demoLogin();
      await page.navigate(`${ctx.app}/dashboard`);
      await page.waitFor(`!!document.querySelector('[data-testid="nav-drawer-toggle"]')`, { timeout: 20000 }, "drawer toggle");
      await sleep(400);

      await page.click('[data-testid="nav-drawer-toggle"]');
      await page.waitFor(`!!document.querySelector('[data-testid="nav-scrim"]') || !!document.querySelector('[data-testid="nav-drawer-close"]')`, {
        timeout: 5000,
      }, "drawer open");
      await sleep(400);
      const drawerLinks = await page.eval(
        `[...document.querySelectorAll('[data-testid="app-nav"] a[href]')].map(a=>a.getAttribute('href'))`,
        { awaitPromise: false },
      );
      check(
        `nav.drawer-links.${vp}`,
        `phone drawer exposes the non-tab destinations at ${vp}`,
        drawerLinks.length >= 10,
        `${drawerLinks.length} links: ${JSON.stringify(drawerLinks.slice(0, 8))}`,
      );

      await page.press("Escape");
      await sleep(500);
      const drawerClosed = await page.eval(
        `!document.querySelector('[data-testid="nav-scrim"]')`,
        { awaitPromise: false },
      );
      const focusBack = await page.activeElement();
      check(
        `nav.drawer-escape.${vp}`,
        `Escape closes the drawer once and returns focus at ${vp}`,
        drawerClosed && focusBack?.testid === "nav-drawer-toggle",
        `closed=${drawerClosed} focus=${JSON.stringify(focusBack)}`,
      );

      // Five-tab bar navigates for real.
      const tabs = await page.eval(
        `[...document.querySelectorAll('[data-testid="mobile-tab-bar"] a[href]')].map(a=>a.getAttribute('href'))`,
        { awaitPromise: false },
      );
      let tabFail = null;
      for (const href of tabs.slice(0, 5)) {
        await page.navigate(`${ctx.app}/dashboard`);
        await sleep(350);
        try {
          await page.click(`[data-testid="mobile-tab-bar"] a[href="${href}"]`);
          await page.waitFor(`location.pathname === ${JSON.stringify(href)}`, { timeout: 8000 }, `tab ${href}`);
        } catch (err) {
          tabFail = `${href} (landed on ${await page.url().catch(() => "?")}: ${String(err.message).slice(0, 120)})`;
          break;
        }
      }
      check(
        `nav.tabbar.${vp}`,
        `the phone bar's route tabs navigate at ${vp} (3 href tabs + Add + More buttons)`,
        tabs.length === 3 && !tabFail,
        `route tabs=${JSON.stringify(tabs)} failed=${tabFail}`,
      );
    }

    await ctx.viewport(1440);
    await ctx.demoLogin();
  },
};
