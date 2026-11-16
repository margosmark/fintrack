// Real-data truth (docs/47 §9 financial-number truth, §13 examples 8/9, QA
// matrix anti-invention pins): every money figure the Ledger renders must be
// reproducible from the payload the server actually served — no invented
// numbers, no static fixtures, server-derived basis dates. The display mask is
// switched off before each comparison (a full page load boots back to the demo
// tenant default, docs/46 D1).
import { sleep } from "../harness/cdp.mjs";

/** "$78.2K" -> 78200 (the product's compact KPI formatter, docs/09). */
function parseCompact(text) {
  if (typeof text !== "string") return null;
  const m = /^\$?(-?[\d.]+)([KMB])?/.exec(text.trim());
  if (!m) return null;
  const n = Number(m[1]);
  if (Number.isNaN(n)) return null;
  const unit = { K: 1e3, M: 1e6, B: 1e9 }[m[2] || ""] || 1;
  return n * unit;
}

function bucketLabel(isoDate) {
  return new Date(`${isoDate}T12:00:00`).toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
  });
}

export default {
  name: "truth",
  async run(ctx) {
    const { page, check, note, money } = ctx;
    await ctx.viewport(1440);

    const token = await ctx.demoToken();

    const unmask = async () => {
      const chip = ((await page.textOf('[data-testid="mask-chip"]')) || "").trim();
      if (chip === "Masked") {
        await page.click('[data-testid="mask-chip"]');
        await sleep(500);
      }
    };
    const settle = async (route, predicate) => {
      await ctx.demoLogin();
      await page.navigate(`${ctx.app}${route}`);
      await page.waitFor(predicate, { timeout: 25000 }, `${route} settled`);
      await unmask();
      await sleep(900);
    };

    // ---- Dashboard: KPI delta + basis vs the exact request the page issued
    await settle("/dashboard", `!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`);

    const netWorthReq = [...page.requests]
      .map((r) => r.request.url)
      .filter((u) => u.includes("/api/v1/reports/net-worth?"))
      .pop();
    check("truth.networth-request", "dashboard really requested /reports/net-worth", !!netWorthReq, netWorthReq || "no request seen");

    if (netWorthReq) {
      const path = netWorthReq.replace(ctx.app, "");
      const { status, json } = await ctx.apiReq(token, path);
      const buckets = json?.buckets || [];
      check("truth.networth-payload", "net-worth payload has server buckets", status === 200 && buckets.length > 1, `status=${status} buckets=${buckets.length}`);
      if (buckets.length > 1) {
        const last = buckets[buckets.length - 1];
        const prev = buckets[buckets.length - 2];
        const delta = Number(last.net_cad) - Number(prev.net_cad);
        const kpi = await page.eval(`(() => {
          const label = [...document.querySelectorAll('p')].find(e => /^net worth\\s*\\??$/i.test((e.textContent||'').trim()));
          if (!label) return null;
          const chip = label.nextElementSibling;
          const lines = ((chip && chip.innerText) || '').split('\\n').map(s=>s.trim()).filter(Boolean);
          const sr = chip ? chip.querySelector('.sr-only') : null;
          return { value: lines[0] || null, basis: lines[1] || null, sr: sr ? sr.innerText.trim() : null }; })()`, { awaitPromise: false });
        check("truth.networth-kpi-present", "the NET WORTH comparison chip renders", !!kpi, JSON.stringify(kpi));
        if (kpi) {
          const shown = parseCompact(kpi.value);
          // 1-decimal K rounding: tolerance of half the last displayed digit.
          const tolerance = 50;
          // The pill prints the MAGNITUDE: `DeltaChip` renders Math.abs(delta)
          // behind a direction icon + gain/loss tone, and ComparisonChip puts
          // the whole visible group aria-hidden behind the sr sentence that
          // carries the word (docs/47 §9.3 "signed delta", §6.5 — state never
          // rides on the arrow or on colour alone). So the magnitude is
          // compared unsigned and the DIRECTION is asserted separately from
          // the payload's own sign. (Comparing a signed delta against the
          // magnitude only held while the weekly delta happened to be
          // positive — it went red on the first negative week.)
          const direction = delta >= 0 ? "up" : "down";
          const magnitudeOk = shown !== null && Math.abs(Math.abs(shown) - Math.abs(delta)) <= tolerance;
          const directionOk = !!kpi.sr && new RegExp(`\\b${direction}\\b`).test(kpi.sr);
          check(
            "truth.networth-kpi",
            "KPI magnitude equals the server's bucket-to-bucket delta (last − previous net_cad) and the sentence states its direction",
            magnitudeOk && directionOk,
            `chip=${JSON.stringify(kpi.value)} (${shown}) vs |delta|=${Math.abs(delta).toFixed(2)} (${prev.bucket_end}→${last.bucket_end}) payloadSign=${direction} sr=${JSON.stringify(kpi.sr)}`,
          );
          const expectedBasis = `vs ${bucketLabel(prev.bucket_end)}`;
          check(
            "truth.networth-basis",
            "KPI basis is the previous bucket's own date, never a wall clock",
            kpi.basis === expectedBasis,
            `chip=${JSON.stringify(kpi.basis)} expected=${JSON.stringify(expectedBasis)}`,
          );
          check(
            "truth.networth-sentence",
            "the screen-reader sentence carries the same figure and basis",
            !!kpi.sr && kpi.sr.includes(kpi.value) && kpi.sr.includes(expectedBasis.replace("vs ", "")),
            JSON.stringify(kpi.sr),
          );
        }
      }
    }

    // ---- More panel: every account balance equals GET /accounts ----------
    await settle("/dashboard", `!!document.querySelector('[data-testid="more-toggle"]')`);
    const { json: accounts } = await ctx.apiReq(token, "/api/v1/accounts?limit=200&offset=0");
    const items = accounts?.items || [];
    await page.click('[data-testid="more-toggle"]');
    await sleep(500);
    const panelText = (await page.textOf('[data-testid="more-panel"]')) || "";
    await page.press("Escape");
    await sleep(200);

    const expected = items
      .filter((a) => Number(a.total_value) !== 0)
      .map((a) => ({ name: a.name, value: money(a.total_value, a.currency) }));
    const missing = expected.filter((e) => !panelText.includes(e.name) || !panelText.includes(e.value));
    check(
      "truth.more-balances",
      "More panel account rows show the server's balances",
      expected.length > 0 && missing.length === 0,
      expected.length === 0
        ? "no accounts returned"
        : `${expected.length} accounts checked; missing=${JSON.stringify(missing.slice(0, 4))} panel=${JSON.stringify(panelText.slice(0, 200))}`,
    );

    // ---- Investments: headline value + server basis date (example 8) -----
    await settle("/investments", `!!document.querySelector('[data-testid="portfolio-header"]')`);
    const posUrl = [...page.requests].map((r) => r.request.url).filter((u) => u.includes("/api/v1/portfolio/positions")).pop();
    if (posUrl) {
      const { json: pos } = await ctx.apiReq(token, posUrl.replace(ctx.app, ""));
      const expectedValue = money(pos.portfolio_value_cad ?? pos.total_value_cad);
      const body = await page.bodyText();
      check(
        "truth.portfolio-value",
        "portfolio headline equals the server's portfolio_value_cad (holdings + cash)",
        body.includes(expectedValue),
        `expected ${expectedValue} (portfolio_value_cad=${pos.portfolio_value_cad}, total_value_cad=${pos.total_value_cad})`,
      );
      const basis = pos.day_change_as_of;
      if (basis) {
        const label = `vs ${bucketLabel(basis)} close`;
        check(
          "truth.basis-date",
          "header basis uses the server's day_change_as_of, never a wall clock",
          body.includes(label),
          `expected "${label}" (server day_change_as_of=${basis})`,
        );
      } else {
        note("no day_change_as_of in payload — first-day listing renders no delta");
      }
      // Unknown price rule: a holding with price null renders an em dash,
      // never $0.00 (docs/47 example 9 / QA money truth rule).
      const nullPrice = (pos.items || []).find((p) => p.price == null);
      if (nullPrice) {
        const rowText = await page.eval(
          `(() => { const rows=[...document.querySelectorAll('[data-testid="position-group-row"],[data-testid="position-history-row"],tr,li')];
            const hit=rows.find(r=>(r.innerText||'').includes(${JSON.stringify(nullPrice.symbol || nullPrice.name || "")}));
            return hit ? hit.innerText : null; })()`,
          { awaitPromise: false },
        );
        check(
          "truth.unpriced-dash",
          "a price-less holding renders an em dash, not $0.00",
          !!rowText && !/\$0\.00/.test(rowText) && /—/.test(rowText),
          JSON.stringify(rowText || "").slice(0, 200),
        );
      } else {
        note("no price-less holding in this seed — em-dash rule not exercised here");
      }
    } else {
      check("truth.portfolio-request", "investments issued /portfolio/positions", false, "no positions request in the network log");
    }

    // ---- Transactions: server total vs the rendered counter -------------
    await settle("/transactions", `!!document.querySelector('main') && !document.querySelector('[data-testid="route-fallback"]')`);
    const { json: txPage } = await ctx.apiReq(token, "/api/v1/transactions?limit=50&offset=0");
    const body = await page.bodyText();
    const showing = /Showing\s+[\d,]+\s*[–-]\s*[\d,]+\s+of\s+([\d,]+)/.exec(body);
    const total = txPage?.total;
    check(
      "truth.tx-total",
      "rendered 'Showing X–Y of N' total equals the server's total",
      !!showing && !!total && Number(showing[1].replace(/,/g, "")) === Number(total),
      showing ? `page=${showing[1]} api=${total}` : "no 'Showing … of …' counter found on screen",
    );

    const { json: unreviewed } = await ctx.apiReq(token, "/api/v1/transactions/unreviewed-count");
    if (unreviewed && typeof unreviewed.count === "number") {
      check(
        "truth.unreviewed-count",
        "unreviewed counter equals the server's count",
        body.includes(`${unreviewed.count} unreviewed`),
        `api count=${unreviewed.count}`,
      );
    }

    // Every rendered money string on an unmasked page must parse as money —
    // catches mock copy that slips a non-numeric figure into a money slot.
    const moneyStrings = await page.eval(`(() => {
      const out = new Set();
      const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walk.nextNode())) {
        for (const m of (n.nodeValue || '').matchAll(/\\$?-?\\d{1,3}(?:,\\d{3})*\\.\\d{2}\\b/g)) out.add(m[0]);
      }
      return [...out]; })()`, { awaitPromise: false });
    const unparsed = (moneyStrings || []).filter((s) => ctx.parseMoney(s) === null);
    check(
      "truth.money-parse",
      "every rendered money string parses as a decimal amount",
      unparsed.length === 0 && moneyStrings.length > 0,
      `${(moneyStrings || []).length} money strings; unparsed=${JSON.stringify(unparsed.slice(0, 5))}`,
    );
  },
};
