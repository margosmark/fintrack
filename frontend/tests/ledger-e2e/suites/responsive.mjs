// Responsive contract (docs/47 §13 example 14, §7): no horizontal overflow on
// any Ledger screen at 1440/390/360, the phone shell replaces the desktop bar
// below 768, controls keep their full box (no clipped CTA), and phone controls
// hold the 44 px touch floor.
import { sleep } from "../harness/cdp.mjs";

const ROUTES = ["/dashboard", "/transactions", "/imports", "/investments", "/reports", "/settings"];

export default {
  name: "responsive",
  async run(ctx) {
    const { page, check, note } = ctx;

    for (const vp of [1440, 390, 360]) {
      await ctx.viewport(vp);
      await ctx.demoLogin();

      for (const route of ROUTES) {
        await page.navigate(`${ctx.app}${route}`);
        await page.waitFor(
          `!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`,
          { timeout: 25000 },
          `${route} settled`,
        );
        await sleep(400);
        const overflow = await page.eval(`(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          bodyScroll: document.body.scrollWidth,
        }))()`, { awaitPromise: false });
        check(
          `resp.overflow.${vp}${route.replace(/\W/g, "")}`,
          `no horizontal overflow on ${route} at ${vp}`,
          overflow.scrollWidth <= overflow.innerWidth + 1 && overflow.bodyScroll <= overflow.innerWidth + 1,
          JSON.stringify(overflow),
        );
      }

      // Breakpoint shell: bottom tab bar + drawer below 768, top bar above.
      await page.navigate(`${ctx.app}/dashboard`);
      await page.waitFor(`!!document.querySelector('main')`, { timeout: 20000 }, "dashboard");
      await sleep(300);
      const shell = await page.eval(`(() => {
        const vis = (sel) => { const el = document.querySelector(sel); if (!el) return false;
          const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        return { tabbar: vis('[data-testid="mobile-tab-bar"]'), topbar: vis('[data-testid="app-topbar"]'),
                 drawer: vis('[data-testid="nav-drawer-toggle"]') }; })()`, { awaitPromise: false });
      if (vp === 1440) {
        check("resp.shell.1440", "1440 uses the top bar, not the phone bar", shell.topbar && !shell.tabbar, JSON.stringify(shell));
      } else {
        check(`resp.shell.${vp}`, `${vp} uses the phone bar + drawer, not the top bar`, shell.tabbar && shell.drawer && !shell.topbar, JSON.stringify(shell));
      }

      if (vp !== 1440) {
        // Touch floor: every visible control the phone shell + page render must
        // clear 44x44 (docs/47 §14 example 14). Inline links embedded in a
        // sentence are exempt (WCAG 2.5.8 target-size "inline" exception) —
        // they are reported in the detail, not counted as violations.
        const audit = await page.eval(`(() => {
          const sel = 'main button, main a[href], [data-testid="mobile-tab-bar"] button, [data-testid="mobile-tab-bar"] a[href], [data-testid="nav-drawer-toggle"]';
          const below = [];
          const inline = [];
          for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            // Sub-pixel boxes render as 44px after rounding — judge what the
            // user can actually hit.
            const w = Math.round(r.width);
            const h = Math.round(r.height);
            if (w >= 44 && h >= 44) continue;
            const label = (el.innerText || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 30);
            // Inline exception (WCAG 2.5.8): a link inside a longer sentence is
            // exempt — walk a few ancestors to find the prose container.
            let prose = "";
            let node = el.parentElement;
            for (let i = 0; i < 5 && node; i++) {
              prose = (node.innerText || '').trim();
              if (prose.length > 60) break;
              node = node.parentElement;
            }
            if (el.tagName === 'A' && prose.length > 60) inline.push(label);
            else below.push({ t: label, w, h });
          }
          return { below, inline }; })()`, { awaitPromise: false });
        check(
          `resp.targets.${vp}`,
          `all controls on the phone dashboard clear the 44px floor at ${vp}`,
          audit.below.length === 0,
          audit.below.length === 0
            ? `0 violations (${audit.inline.length} inline prose links exempted: ${JSON.stringify(audit.inline.slice(0, 3))})`
            : `${audit.below.length} violations: ${JSON.stringify(audit.below.slice(0, 8))}; ${audit.inline.length} inline prose links exempted`,
        );

        // The phone bar: five targets — three route tabs (real hrefs) plus the
        // centre Add and More buttons (docs/47 §1.2: Add is a real control,
        // More opens the drawer).
        const tabTargets = await page.eval(`(() => {
          const bar = document.querySelector('[data-testid="mobile-tab-bar"]');
          if (!bar) return null;
          return [...bar.querySelectorAll('a,button')].map(el => {
            const r = el.getBoundingClientRect();
            return { label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 20),
                     tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height),
                     href: el.getAttribute('href'),
                     named: ((el.innerText || el.getAttribute('aria-label') || '').trim().length > 0) };
          }); })()`, { awaitPromise: false });
        const hrefTabs = (tabTargets || []).filter((t) => t.href);
        const buttons = (tabTargets || []).filter((t) => !t.href);
        check(
          `resp.tabbar-targets.${vp}`,
          `phone bar renders 5 named targets (3 route tabs + Add + More) at ${vp}`,
          Array.isArray(tabTargets) &&
            tabTargets.length === 5 &&
            hrefTabs.length === 3 &&
            buttons.length === 2 &&
            tabTargets.every((t) => t.h >= 44 && t.named),
          JSON.stringify(tabTargets),
        );
      }

      // State-row / empty-state CTAs must not be clipped (clientWidth equals
      // scrollWidth) — checked where one is actually on screen (fresh states
      // live in the tenant suite; here we pin the primitives that exist).
      const clipped = await page.eval(`(() => {
        const out = [];
        for (const el of document.querySelectorAll('[data-testid="empty-state-action"], main button[type="submit"]')) {
          if (el.scrollWidth > el.clientWidth + 1) out.push((el.innerText||'').slice(0,30));
        }
        return out; })()`, { awaitPromise: false });
      check(
        `resp.no-clipped-cta.${vp}`,
        `no clipped primary CTA at ${vp}`,
        clipped.length === 0,
        clipped.length === 0 ? "clean" : JSON.stringify(clipped),
      );
    }

    await ctx.viewport(1440);
    await ctx.demoLogin();
    note("responsive sweep at 1440/390/360 across 6 routes");
  },
};
