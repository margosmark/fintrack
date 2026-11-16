# 32 — Help guides & static marketing site

## Help center (in-app)

- Hub at `/help` (`frontend/src/pages/Help.tsx`): one card per guide, linking to `/help/:slug`.
- Guide page at `/help/:slug` (`frontend/src/pages/HelpGuide.tsx`): numbered steps with screenshots; a guide may also carry `tables` (`caption`, `columns`, `rows`) rendered as semantic comparison tables under its steps — `migrate-from-ynab` uses two of them for the honest gaps tables. Unknown slug renders a not-found `EmptyState` with a back link.
- Copy lives once in `frontend/src/pages/helpGuides.ts` (`GUIDES`, `getGuide`). Fifteen guides: `first-import`, `first-budget`, `agent-tour` (M89 learning path — the three story-shaped walkthroughs the quickstart tracks link to; see below), `getting-started`, `transactions`, `rules`, `imports`, `migrate-from-ynab`, `reports`, `taxes`, `import-guides` (M71 — per-institution export paths, formats, quirks, sample files; spec in docs/03 § "Per-institution import guides (M71)"), `portability` (M75 — the published no-lock-in promise: exact file list, CSV/manifest schema, OFX round trip, scheduled export; spec in docs/11 § "One-click export (M75)"), `agent-integration` (M80 — mint a scoped MCP token, point a client at `/mcp/mcp`, what a read-only session sees, and how to read the per-call receipts; M189 (docs/59) added the BYO-AI section: the `read_propose` mint, the 54-tool listing, the no-apply confirmation gate and the pinned "Your data stays between you and your AI" egress blockquote; spec in docs/40 § 4), `configuration` (M80 — the MCP reference: endpoints, payload shapes, the `full` / `read_only` / `read_propose` policies (M189), the receipt-code table and the three container variables; spec in docs/40 § 4), `brokerage-imports` (M82 — where each broker keeps its activity export and exactly what the importer refuses; spec docs/03 § "Brokerage CSV import profiles (M82)"). Both M80 guides carry tables (receipt fields; endpoints and policies) rather than screenshots — there is no UI surface to photograph, which is the point. Each step: `heading`, `body`, optional `image` (+ `imageAlt`), optional `links` (`{label, href}` download chips rendered by `HelpGuide.tsx`); `Help.tsx` renders the count as `N steps · M screenshots` from the data, so the card never lies about what is inside. `portability` carries `/help/portability.png` (a real capture of Settings → Privacy & data, vendored to `site/assets/portability.png`).
- Screenshots are real captures of the demo tenant, stored in `frontend/public/help/*.png` and served at `/help/*.png` (vite `public/` passthrough; no import needed). CSP `img-src 'self' data:` already covers same-origin images.
- Tests: `frontend/tests/help.test.ts` (hub links, guide render + image src, unknown slug, the migrate-from-ynab steps + both tables) and `frontend/tests/importGuides.test.tsx` (M71: every institution renders on `/help/import-guides`, every `GUIDES` slug resolves so no page 404s, one sample download per format, no jargon in the DOM, and the static-site copy-drift check below).
- **Downloadable samples** (M71): `frontend/public/help/import-samples/{sample.csv,sample.ofx,sample.qfx,sample.qif}` — synthetic statements with invented merchants, linked from `import-guides`. Duplicated under `site/guides/samples/` because the site is a separate static root.

### M89 learning path (quickstart tracks + three walkthroughs)

- **Three quickstart tracks** above the guide grid on `/help` (`Help.tsx`,
  `TRACKS`): `1 Try it in 2 minutes` → `/dashboard`, `/help/first-import` (+ the
  sample OFX download), `/transactions`; `2 Own your data` → `/register`,
  `/imports`, `/settings`; `3 Drive it from your agent` →
  `/help/agent-integration`, `/help/configuration`, `/help/agent-tour`. Every
  step is a working link, and the section header carries **Take the tour**
  (docs/09 § "Guided first tour").
  Track 1's import step points at the `first-import` **walkthrough**, not at
  `/imports`, because the demo tenant answers `403 demo_account_restricted` to
  `POST /imports/upload` (docs/20) — a live demo upload is a promise the app
  cannot keep. The step's hint states the restriction out loud and the sample
  download stays, for after sign-up (M92 F-M91-1). `tests/m89HelpPath.test.tsx`
  pins that Track 1 links to the walkthrough and to `/imports` nowhere.
- **Three walkthrough guides**, written as the story of doing the thing, each
  step backed by a real prod capture:
  - `first-import` — 7 steps, 7 screenshots: `imports-empty`,
    `imports-file-chosen`, `imports-preview-first`, `imports-preview-duplicate`
    (the red exact duplicate / amber near-duplicate / green new state the copy
    explains), `imports-review-decision`, `imports-committed`,
    `imports-in-transactions`. Also links the sample OFX download chip.
  - `first-budget` — 5 steps, 2 screenshots: `budget-create`, and
    `budgets-over-100` (the worked example: `$123 / $60`, **206% used** —
    `Math.round(pct_used)`, bar fill capped at 100%, the number never capped).
  - `agent-tour` — 4 steps, 2 images: `agent-run`, `agent-receipts`. Both are
    M80's committed evidence painted as images by
    `docs/assets/m80/render_quickstart_images.py`, whose source of truth is
    `docs/assets/m80/quickstart-transcript.txt` — regenerate from there when the
    transcript changes; nothing in the images is hand-authored.
- **Site mirrors**: `site/guides/{first-import,first-budget,agent-tour}.html`
  with the same step headings (pinned by `tests/m89HelpPath.test.tsx`) and the
  vendored PNGs under `site/assets/`. The landing page no longer carries a
  `#guides` card grid (t_fe44260e rebuild): its nav **Guides** link points at
  `site/guides/getting-started.html`, and the import proof card points at
  `site/guides/import-guides.html`.
- **Tests**: `frontend/tests/m89HelpPath.test.tsx` (tracks + working links, the
  resurface button fires `fin_tracker:tour:start`, the three walkthroughs exist,
  every referenced screenshot exists with an `imageAlt`, each mirror carries the
  headings plus a `<main>` landmark and a favicon) and
  `frontend/tests/siteTokens.test.ts` (the static contract: no colour literal
  outside `:root`, contrast pairs documented, `--border-control` on the ghost
  button, landmarks/favicon/no inline styles on **every** page — `sitePages()`,
  a recursive scan, since M93 W3 gave the twelve legacy guides the same shell —
  plus the changelog link and its nginx location, and the zero-third-party
  scan) and
  `frontend/tests/siteTrust.test.ts` (the trust-page copy-drift pins: every
  Settings path, the deletion endpoint and the AI payload caps stated on
  `site/trust.html` must exist verbatim in the source they describe — the page
  goes red instead of quietly becoming false).

## Static site (`site/`)

- Pure static landing + the same fifteen guides as hand-written HTML. Self-contained by design: no build step, no backend, no DB — `docker build -t fintrack-site ./site` + `docker run -p 8090:8080 fintrack-site` runs it on any host. The image is `nginxinc/nginx-unprivileged:alpine` running non-root and listening on **8080** in-container (audit L22).
- Layout: `site/index.html` (the public landing page, rebuilt in t_fe44260e for the public URL: positioning hero with the absolute demo CTA and the `dashboard.png` shot; `#why` — three proof cards, each linking to the guide that proves it (imports → `guides/import-guides.html`, taxes → `guides/taxes.html`, export → `guides/portability.html`); `#features` — six feature cards; a **See it working** strip (`imports.png`, `taxes.png`, `reports.png`); `#privacy` and `#android` text panels; `#selfhost` quickstart with two `<code tabindex="0">` blocks (full stack, then the site-only image); `#limits` — the honest `ul.nots` limitations closing on the enforced no-trackers bullet and a repeat demo CTA), `site/changelog.html` (M-number → user-visible benefit tables per wave, TL;DR first — the `/changelog` route), `site/trust.html` (the trust & privacy page, t_09ed864f: Kilter's three promises — "Never sold" / "Never used to train AI" / "Yours to take or delete" — restated as checkable facts about this codebase, plus the exact AI payload table, the complete egress inventory (fund-issuer hosts and the conditional OIDC IdP included), a where-data-lives / who-can-see-it section and the honest not-zero-knowledge status; linked from `#privacy` ("Read how fin_tracker protects your data →") and the footer, served at `/trust`), `site/guides/<slug>.html`, `site/styles.css` (midnight/cyan identity matching the app — the site palette is the pinned cyan `#22d3ee` family, deliberately not root `landing.html`'s green; `landing.html` itself stays unserved), `site/assets/*.png` (vendored copies of the same demo screenshots — duplicated, not symlinked, so the image stays portable). Exactly four `<img>` load on `index.html` (all `width="1600" height="1000" loading="lazy"`), and the page carries zero JavaScript — no `<script>` anywhere in `site/**`.
- **Routing:** the image has no build step, so `/changelog` and `/trust` are served by nginx exact-match locations in `site/Dockerfile` (`location = /changelog { try_files /changelog.html =404; }`, `location = /trust { try_files /trust.html =404; }`) — a bare `try_files $uri` would 404 them, since each is a file, not a directory. Each page has to be `COPY`ied into the image alongside `index.html` for its location to resolve (caught by `tests/siteTokens.test.ts`, which reads the Dockerfile).
- **Public URL (t_fe44260e):** the page is reachable at `https://fintrac.princessandmark.com/site/`. The app's web nginx (generated inline in `frontend/Dockerfile`) gained exactly two locations, nothing else: `location = /site { return 301 /site/; }` and a `location /site/` that `resolver 127.0.0.11`-resolves a variable upstream `http://site:8080/` (the `/mcp/` pattern — a recreated site container is re-resolved by DNS instead of a cached IP) with `rewrite ^/site/(.*)$ /$1 break`, because a variable `proxy_pass` does no URI substitution and the `/site/` prefix must be stripped for the site image's own `location = /changelog`, relative asset paths and guide links to resolve. The location declares no `add_header` — the server-level hardening set (CSP/HSTS/XFO/nosniff/Referrer-Policy/PP/COOP/CORP) is inherited — and additionally `proxy_hide_header`s the four headers the **site image** sends itself (`Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` in `site/Dockerfile`): the site image keeps its own set because it must stay hardened when run standalone (`docker run -p 8090:8080 fintrack-site`, per the self-contained rule above), so without the hide every proxied `/site/` response carried those four headers **twice** and two CSPs (t_c5d1b304, found in security's PASS on the landing wave). The browser now sees exactly one set — the server-level one, which is the stricter superset — while the standalone image is untouched. Everything else is untouched: the SPA shell still answers `/`, `/login`, `/changelog`, `/guides/*` and `/api/v1/*`. The pre-change route table (all four `200` with `<title>fin_tracker</title>`) is recorded on card t_fe44260e; revert = restore that Dockerfile hunk and rebuild `web`.
- **Page contract (M89, extended to every page in M93 W3):** every page is `header` → `main.wrap` → `footer` (axe `landmark-one-main` / `region` — both were failing on the twelve pre-existing guides, which were a bare `<div class="wrap">` with the `<footer>` nested inside it, so moving the footer out of `<main>` is part of the contract: a `contentinfo` landmark must be top level), carries a data-URI SVG favicon (a missing `/favicon.ico` is a console error on every load), keeps every colour in `:root` tokens with the contrast pairs documented in that block, titles the page `<h1>` with `<h2>` step headings (axe `page-has-heading-one`; no level may be skipped, axe `heading-order`), and makes every horizontally scrollable region focusable — `<code tabindex="0">` and `.table-wrap tabindex="0"` (axe `scrollable-region-focusable`, which only fires at narrow widths). The demo/login CTA is an absolute link to the app origin (`https://fintrac.princessandmark.com/login`) because the site image may be hosted away from the app — a relative link would be dead on the portable deployment.
- **Legacy-guide shell refresh (M93 W3, the M89 finding QA filed):** the twelve
  pre-existing `site/guides/*.html` pages were moved onto that contract —
  favicon link, the same `header.nav` the M89 guides carry, `<div class="wrap">`
  → `<main class="wrap">` with the footer lifted out of it, the page heading
  `h2` → `h1` and every step heading `h3` → `h2`, `.table-wrap tabindex="0"`.
  **Shell only**: copy, screenshots, links, order and page structure are
  byte-for-byte untouched (the headings are markup, not editorial). One
  stylesheet rule was added so a promoted table heading keeps its card size —
  `section.block .table-card h2 { margin: 0 0 4px; font-size: 17px; }`
  (`section.block h2`'s 26px would otherwise win by specificity) — and the
  `ol.steps` comment above the step-counter block was corrected: no guide page
  keeps `h3` under an `h2` title any more.
  **Verification:** `python3 frontend/scripts/a11y/site_axe_scan.py` (needs the
  site served, e.g. `python3 -m http.server 8877 --directory site`, and
  `SITE_BASE`) runs axe-core over every `site/**/*.html` in both themes and
  exits non-zero on any violation or console error. Result after W3: **0
  violations, 0 console errors on all 17 pages × 2 themes at 1440, 390 and
  360 px** (`frontend/scripts/a11y/site-axe-results-{1440,390,360}.json`);
  the pre-change baseline was 3 violations × 12 pages at 1440 (`landmark-one-main`,
  `page-has-heading-one`, `region`). `tests/siteTokens.test.ts` pins the same
  contract for every page so a thirteenth guide cannot ship without it.
- **What that command actually measures (harness correction).** `SITE_WIDTH` is
  no longer passed to `--window-size`: headless Chrome clamps a window below
  **500 CSS px**, so the 360 and 390 artifacts above used to be 500 px runs
  labelled 360/390 — the narrow-width rules (including the
  `scrollable-region-focusable` the page contract relies on) were never
  exercised. The scan now forces the width with
  `Emulation.setDeviceMetricsOverride` (`a11y_driver.Tab.force_viewport()`,
  shared with `axe_scan.py` — docs/09 §Scroll regions), **aborts before
  scanning** if `window.innerWidth` != `SITE_WIDTH`, re-asserts and records
  `viewport` on the report and on **every page entry**, and aborts if the
  loaded document is not under `SITE_BASE` (Chrome's `chrome-error://` page is
  `readyState: complete` too, so `Tab.navigate()` alone cannot tell a served
  page from a failed load). All three committed artifacts were
  regenerated through that path — numbers unchanged, evidence now real.
- **Widths covered:** 1440 (desktop), 390 and 360 (the narrow band the page
  contract's focusable-scroll rules target), each with
  `SITE_WIDTH=<w> python3 frontend/scripts/a11y/site_axe_scan.py`, which writes
  `site-axe-results-<w>.json` by default.
- **Honesty claim, enforced (M92 F-M91-2):** the **No trackers** bullet under
  `index.html`'s `#limits` (Honest limitations) says the page ships one HTML
  document, one stylesheet and four
  screenshots of the product, and that every request the site makes stays on
  this site. `tests/siteTokens.test.ts` enforces the second half by scanning
  **every** `site/**/*.html` for a resource load pointing off-origin
  (`<script>` / `<img>` / `<link>` / `<iframe>` / `<source>` / `<video>` with
  an `http(s)://` or `//` `src`/`href`, plus `url()` / `@import` in
  `site/styles.css`); external `<a href>` navigations — the app login CTA —
  are links, not loads, and are deliberately out of scope. The four PNGs carry
  `width="1600" height="1000"` and `loading="lazy"` so they cannot shift the
  layout; they are 97% of the page's 922 KB (944,046 B = 10,879 B HTML +
  10,649 B CSS + 922,518 B of PNGs) and stay because they are the
  proof-render of the product. If the bullet's wording changes, change the
  test with it — never the other way round.
- Compose service `site` (port `127.0.0.1:8090:8080`, healthcheck probes `http://127.0.0.1:8080/`) for local preview alongside the stack; moving it off-site later means pointing DNS at a host running just this image.
- Copy drift rule: guide text is duplicated between `helpGuides.ts` and `site/guides/`. On any guide edit, update both. If a third surface needs the copy, extract a generator then (YAGNI until that day). (The two M80 pages were *initially* produced by rendering `helpGuides.ts`, which is why their copy matches character for character; they are maintained by the same rule as every other guide.)
