// Shell + breakpoint contract (docs/47 §1.1–1.3, §13 examples 2/6): the demo
// session boots into the real Ledger shell at 1440 and at both phone widths,
// with the truth bar's exact copy and no vendor leakage.
import { sleep } from "../harness/cdp.mjs";

const BANNER = "Read-only demo — changes are not saved";

export default {
  name: "shell",
  async run(ctx) {
    const { page, check, note } = ctx;

    for (const vp of [1440, 390, 360]) {
      await ctx.viewport(vp);
      await ctx.demoLogin();
      await page.waitFor(`!!document.querySelector('[data-testid="demo-banner"]')`, {
        timeout: 20000,
      }, "demo-banner");
      const label = await page.textOf('[data-testid="demo-banner"]');
      const role = await page.eval(
        `document.querySelector('[data-testid="demo-banner"]')?.getAttribute('role')`,
        { awaitPromise: false },
      );
      check(
        `shell.banner.${vp}`,
        `demo banner renders verbatim with role=status at ${vp}`,
        label === BANNER && role === "status",
        `role=${role} text=${JSON.stringify(label)}`,
      );

      const body = await page.bodyText();
      // docs/09 M68's pinned tokens: `yfinance`, `BoC`. "Yahoo" in the M141
      // dead-symbol copy is reported as an observation, not a failure.
      const vendorHit = /yfinance|BoC\b/i.exec(body);
      const yahooHit = /\byahoo\b/i.exec(body);
      if (yahooHit) note(`"Yahoo" visible in rendered copy at ${vp}: ${JSON.stringify(yahooHit[0])}`);
      check(
        `shell.no-vendor.${vp}`,
        `no yfinance/BoC vendor tokens in rendered copy at ${vp}`,
        !vendorHit,
        vendorHit ? `visible: ${JSON.stringify(vendorHit[0])}` : `clean${yahooHit ? ` (Yahoo named: ${yahooHit[0]})` : ""}`,
      );

      const chrome = await page.eval(`(() => {
        const vis = (sel) => { const el = document.querySelector(sel); if (!el) return false;
          const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        return {
          topbar: vis('[data-testid="app-topbar"]'),
          tabs: vis('[data-testid="topbar-tabs"]'),
          tabbar: vis('[data-testid="mobile-tab-bar"]'),
          drawerToggle: vis('[data-testid="nav-drawer-toggle"]'),
          moreToggle: vis('[data-testid="more-toggle"]'),
          maskChip: vis('[data-testid="mask-chip"]'),
          truthBar: vis('[data-testid="truth-bar"]'),
          skip: !!document.querySelector('[data-testid="skip-link"]'),
          url: location.pathname,
        }; })()`, { awaitPromise: false });

      if (vp === 1440) {
        check(
          "shell.desktop-chrome.1440",
          "desktop shell: top bar + More + mask chip + truth bar, mobile bar hidden",
          chrome.topbar && chrome.tabs && chrome.moreToggle && chrome.maskChip && chrome.truthBar && !chrome.tabbar && !chrome.drawerToggle,
          JSON.stringify(chrome),
        );
      } else {
        check(
          `shell.mobile-chrome.${vp}`,
          `phone shell at ${vp}: bottom tab bar + drawer toggle, desktop top bar hidden`,
          chrome.tabbar && chrome.drawerToggle && chrome.maskChip && chrome.truthBar && !chrome.topbar,
          JSON.stringify(chrome),
        );
      }

      // Truth bar segments (docs/47 §1.3): payload range on dashboard, FX when
      // rates exist, privacy/mask segment, demo segment — all present.
      const truth = await page.textOf('[data-testid="truth-bar"]');
      check(
        `shell.truthbar.${vp}`,
        `truth bar carries mask + demo segments at ${vp}`,
        /masked/i.test(truth || "") && /Read-only demo/.test(truth || ""),
        JSON.stringify((truth || "").replace(/\n/g, " · ").slice(0, 220)),
      );

      // Mask defaults to the tenant default on boot (docs/46 D1 — demo boots
      // masked), so every viewport starts from a known state.
      // Below 768 the chip collapses to an icon whose label lives in
      // aria-label; read both channels.
      const chip = await page.eval(`(() => {
        const b = document.querySelector('[data-testid="mask-chip"]');
        if (!b) return null;
        return ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim(); })()`, { awaitPromise: false });
      check(
        `shell.mask-default.${vp}`,
        `demo boots masked at ${vp} (docs/46 D1 tenant default)`,
        !!chip && chip.split(/\s+/).includes("Masked"),
        `chip=${JSON.stringify(chip)}`,
      );

      // docs/09 §Command palette → Triggers: one Search launcher in the
      // desktop top bar and one in the fixed mobile app bar; the mobile
      // drawer has NO second search field or palette launcher.
      const trigger = await page.exists('[data-testid="command-palette-trigger"]');
      const triggerMobile = await page.exists('[data-testid="command-palette-trigger-mobile"]');
      const drawerSearch = await page.eval(
        `!!document.querySelector('[data-testid="search-panel"]') || !!document.querySelector('input[placeholder*="Search"], input[aria-label*="Search"]')`,
        { awaitPromise: false },
      );
      check(
        `shell.palette-trigger.${vp}`,
        vp === 1440
          ? "command palette trigger rendered in the top bar at 1440"
          : `palette launcher reachable on the phone shell at ${vp} (docs/09 §Command palette Triggers)`,
        vp === 1440 ? trigger : triggerMobile || trigger,
        `trigger=${trigger} triggerMobile=${triggerMobile} drawerSearch=${drawerSearch}`,
      );
    }

    // Back to desktop for the suites that follow.
    await ctx.viewport(1440);
    await ctx.demoLogin();
    await sleep(200);
    note("shell contract sampled at 1440/390/360");
  },
};
