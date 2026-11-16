# 09 — UI & UX

Desktop-first responsive; **dark mode default**. React + TypeScript + Vite, TanStack Query for server state, Recharts for charts, Tailwind for styling (docs/01).

## Pages

| Page | Contents |
| --- | --- |
| **Dashboard** | KPI strip (Net worth, Range net, Positions value — the last one **hidden entirely when it is zero or empty**, M138 D-5 — Cash) with animated numbers + delta chips (each chip is the latest bucket vs the one before it; labels are single-line `whitespace-nowrap` so the four value baselines align; the header row is `flex-wrap`, so a chip that cannot fit beside its label drops to its own right-aligned line instead of hard-clipping its basis, at any width or zoom), **60s auto-refresh**; spending-by-category donut; net-worth **trend area chart** (cash balance + positions valued at the newest cached close ≤ trend date; manual overrides win over yfinance — docs/05 valuation definition); recent transactions list; **BudgetCards** (per-category progress bars with green/amber/red color coding — M25); **InsightsCard** (spending velocity, daily burn rate, month-over-month deltas — M28); **SubscriptionsPanel — the "Bills you probably have" card** (strict-detector suggestions fed from `GET /transactions/recurring/candidates?source=subscriptions`, capped at 5 rows with `Show all`, per-row `Not a bill` + header `Dismiss all (N)` confirming through a 6-second Undo toast, frequency chips in a **neutral** tone — cadence is not a semantic state, so the Calendar kind color keeps the subscription meaning — M138, §Bills you probably have); **layout presets (M97)**: a `SegmentedControl` in the header (`aria-label="Dashboard layout"`) picks one of three FIXED orders over these same cards — `overview` (default) / `import_first` / `budget_first` — stored in `users.dashboard_preset` and rendered in DOM order (**§Dashboard layout presets**) |
| **Transactions** | Filterable table (account, category, date range, payee text, amount, direction All/Paid/Received), inline category assignment (combo-box: type to filter, create new on the fly) with a per-row "similar past entries" popover (fuzzy payee matches from history — anchor excluded, categorized AND uncategorized listed, categorized ranked first and alone carrying the one-click apply; an empty result over a locked vault says so instead of claiming there are none — docs/04 §Similar past examples; "See all similar" opens the full fuzzy search view at `/transactions?q=<payee>` — relevance-ordered **in the browser** (the URL param is read client-side; `GET /transactions` no longer accepts `q`, docs/33 S5), uncategorized included, dismissible chip), the shared add/edit transaction editor (account, date, posting date, payee, raw description, amount, currency, category), per-row Edit/Delete controls with confirmation, suggestion queue panel fed by `llm_suggestions` with a **Run** action that kicks the categorization pipeline (docs/04); the sidebar global search box lands on the same fuzzy view. Horizontal overflow of the table is owned by `TableScroller` (M64, §Horizontal table scroll). **M96**: a checkbox column + bulk actions toolbar (§Bulk selection toolbar) and the dismissible recurring-prompt card with its row badge (§Recurring prompt) |
| **Imports** | Upload wizard: file → account → parse preview → review screen resolving unmapped payees/categories → commit/discard (docs/03 staged flow); **YNAB migration card** (M76, docs/38): file → read-only preview of the whole plan (accounts, group→category tree, targets, every row) with each refused row listed by number + reason → import into per-account review batches — the `/help/migrate-from-ynab` guide carries the export recipes and the "what maps cleanly" / "what does not come across" tables |
| **Investments** | **Portfolio header card (M130, §Portfolio header)** first: portfolio value (holdings + the cash in investment accounts) with a `live` badge on a stream tick, the day change vs the prior market-day's close labelled with that real date, the selected range's server-computed move, and the whole-portfolio value chart with the shared `1M…All` range selector driving both; then Positions table (qty, **book cost CAD**, price, market value native + CAD, staleness indicator, trend sparkline, and since M126 a per-row **expand** into the inline price-history chart — no navigation), trades log (investment-capable accounts only; searchable existing-securities picker), Exposure card with dimension tabs (Currency — the listing-currency view — | Sector | Geography | Asset class | Companies; non-currency dimensions render a donut + legend table from the recursive look-through breakdown (M19, docs/05); since M167 every dimension's list AND its donut fold to the **top 8** (top 10 for Companies) plus an **Other** bucket carrying the tail's exact cents, behind a show-the-rest expander that remembers per session, and Geography additionally renders a **world choropleth** above the list — shaded per-country by weight from a bundled, generated SVG path set (no map library), activating a country scrolling to and highlighting its row, with any bucket the geometry cannot draw printed under the map instead of dropped (§Exposure visuals); since M147 the breakdown table carries the **Unclassified honesty note** (§Exposure honesty): while a fund's composition rows are missing, the `Unclassified` slice names each such fund — `VEQT — Vanguard All-Equity ETF ($12,345.67): open the fund and use Import holdings to fill this slice.` — rendered as a `role="note"` block under the table, muted like the Unclassified row it explains; the note disappears the moment the fund has any composition rows (auto-enrichment or a manual Import), and it never invents a classification for the invisible); since M169 every bucket row is also a **drill target** (§Exposure drill) — clicking it (or its chevron, the keyboard path) opens an inline panel under the row naming the positions, funds and holdings that bucket is made of, each leaf carrying the `via` chain of funds the money passed through, its provenance badge and the M166 estimate / unattributed residual flags, bounded at 25 per level with an `Other` tail; watchlist card (its picker reaches the market for a ticker you do not hold — §Watchlist ticker search), price-refresh action; position and watchlist symbols link to the security detail page; **SSE stream indicator** (green pulsing dot + `Stream connected` label in header, connected via `usePriceStream` hook; the stream is cache-only, so freshness is shown separately by the price date and stale-age badge; when ANY displayed row's price age is >24h (`price_age_days > 0`, day-granular), the indicator switches to an amber `prices stale` state instead of the green connected label — connection state and data freshness may no longer both be neutral (M65, m61 #7)); auto-refresh prices via background tasks on each positions fetch (only during market hours 6 AM–8 PM ET); **Dividend Suggestions panel** (yfinance-derived pending dividends per open holding — Record with DRIP options, dismiss / dismiss-all persisted to `localStorage`) and an inline **New security** creation form (yfinance autofill + manual fallback; since M142 a typed NAME resolves too — the probe runs while you type and offers the canonical listing as a suggestion chip, §New security — the symbol suggestion chip) |
|| **Reports** | One tab per family under a shared range picker + granularity switcher, plus (M108) a **`Chart view` segmented control on the Spending tab** (`Bars` \| `Treemap`, docs/12 §Spending treemap), a **`Group by` segmented control** on Spending + Categories (`Category` \| `Tag` always; `Owner` only while linked, §Owner dimension), and the **`Save this view` / `Saved views` row** under the header (§Saved report views): Spending stacked bars or treemap, Categories donut, Cash flow composed chart, Net worth areas, Performance sortable table, **Dividends** (year selector from available years, totals strip incl. trailing 12 months, monthly bar chart, per-security income table), **Projections** (start value prefilled from current holdings, annual contribution + years slider 1–50 + inflation %, Conservative/Balanced/Aggressive preset chips plus custom scenarios, multi-line chart with the first scenario's real value dashed, per-scenario results table — **server-computed since M157** — `POST /projections/run`, a documented demo-write exemption so the demo tenant runs it too; `lib/projectionEngine.ts` stays as the offline/E2EE fallback, docs/12 §7), **Insights** (spending velocity — M28), **Comparison** (period-over-period with per-category deltas — M30), **Dividend Calendar** (upcoming income projections — M29), **Tax Alerts** (unrealized loss positions — M26), **Budget Cards** (spending limits progress — M25). Per-report **CSV download** buttons (Spending / Categories / Cashflow / Net worth / Performance) via `downloadCsv`; **Dividends**, **Projections**, and other analytics tabs are JSON-only (no CSV) ||
|| **Forecast** (`/forecast`, M109) | The owner's decision surface (docs/45): a Recharts line chart overlaying the **Baseline** plus any subset of saved what-if scenarios (≤ 6 per run), fed by `GET /forecast/run`. Header carries `as_of` + horizon + the four honesty strings verbatim; a `Future dollars` / `Today's dollars` `SegmentedControl` swaps `values` ↔ `real_values` **client-side with no refetch** (both series always arrive), and the editable inflation rate (default 2% CAD) re-runs. Controls: horizon months (1–120, default 24), granularity (`day` default — a 2-year daily run is 48 ms, docs/45 §1.7), flow window (6 or 12 months), flat annual return (−50…50). Scenario list with create / rename / copy / delete + per-scenario overlay checkboxes; a typed event editor offering **exactly the five v1 event types** (no formula box — not-scriptable = not buildable). A collapsible **Assumptions** panel renders the server's own `inputs` figures as `income = recurring + residual`, the run parameters, and the disclosures: house value held constant, flat-rate mortgage (no Canadian semi-annual compounding), career break suppresses ALL income, tax projection not recalculated, not Monte Carlo — plus the response `disclaimer`. Reached from the rail's **Forecast** row (directly after Reports) and from the Reports Projections card's `Open what-if scenarios` link. **Owner-only**: never rendered from partner data, and the demo tenant can read but not write (403 on any mutation) |||
| **Retirement** (`/retirement`, M188) | The Monte Carlo simulator (docs/57), computed **entirely client-side** over the two reads Reports already makes (`GET /reports/net-worth` for the starting portfolio, `GET /reports/cashflow` for the money defaults): seeded paths (default 2,000, reproducible via the seed field) render a **probability of success %** (`data-testid="retirement-success"`, 1 dp) with the readable `X of N simulated paths never ran out of money before age 95.` count line, plus a net-worth timeline fan (`ChartFrame` + `chartAriaLabel`, median area with a p10–p90 band and a `ReferenceLine` at the retirement age; Y axis through the mask-aware `chrome.moneyAxis`). The left **Assumptions** panel edits current/retirement age (range knob), monthly contribution, annual withdrawal, expected return, volatility, scenarios and seed (the stub's tax-rate input was removed when the page
switched to the real engine, t_1ab83b11 — tax is no longer a dial) — draft state updates per keystroke and commits to the sim **300 ms** later (`SIM_DEBOUNCE_MS`; delay 0 until the first human edit so the seeded defaults never race the verdict), money inputs clamped on blur. Tax runs through the `TaxEngine` seam, which the page fills with `caTaxEngine()` (`id: "ca-engine"`, default province ON — docs/58, wired by t_1ab83b11); the panel shows the engine's own badge `Tax engine: Canadian tax — ON`. The seam's flat-rate stub stays exported as the reference implementation but the page no longer constructs it. Four honesty strings verbatim: `Simulation, not a prediction.` / `Runs in your browser on the data you already track — no bank sync, no new data sources.` / `Tax on withdrawals is estimated with Canadian tax tables (federal + Ontario) — a simplification, not a tax filing.` / `Not financial advice.` Reached from the rail's **Retirement** row (directly after Forecast). No server twin by design — rule-8 inventory rows in docs/33. **Since M189 (docs/58)** the results branch also carries two more cards: **Life events** (type dropdown from the `lifeEventEngine` registry — Home purchase · New child · Sabbatical · Early retirement — year + per-type fields with bounds in the label, `Add event` / `Save event` + `Cancel`, a list of `<li data-testid="life-event-item">` rows with per-field `formatCad` summaries and `Edit/Remove <type> <year>` buttons, a `role="alert"` `data-testid="life-event-error"` for out-of-range/stale sets, and — while events exist — the delta line `Success with events: X% · without events: Y% · delta ±D pp` (`data-testid="life-event-success-delta"`; the headline shows the WITH-events verdict, the baseline lives only in the delta line)) and **Year-by-year cash flow** (`data-testid="life-event-timeline"`: one row per plan year — Year · Age · Phase · Starts this year · Event impact · Net cash flow — money cells through `formatCad`, so they mask; the chart's retirement `ReferenceLine` and the readouts row move to the EFFECTIVE retirement age while events are valid). Adding a home purchase for $100k immediately drops the verdict, writes `-$100,000.00` into that year's impact cell and shows `with < without`; remove it and everything reverts. Two runs on the same seed isolate the delta (baseline + plan); an invalid/stale set renders the baseline with the error and never crashes. Events are page state — no persistence, no new endpoints. |||
|| **Calendar** (`/calendar`) | Month grid over `GET /reports/cashflow-calendar` (M52, docs/36): one cell per day of the month (weeks start Monday), kind-colored dots (bill / dividend / subscription), a today ring driven by the response's `today_et()`, and a portaled day popover listing each event's label, signed amount **in the currency it was recorded in** and status with click-through to `/bills`, `/securities/:securityId` or the M55 `/bills?autofill=…` hand-off. Prev/Next month + **Today** (the client never derives the current month itself); zero-filled empty months render a "No cash-flow events in …" hint under the grid, never an error panel. **M170 honesty contract (docs/36 §M170):** the subscription dots are CONFIRMED state only — an active bill or a group the user accepted on /transactions, never the detector's guess — and a month that shows none of them renders one muted line under the legend (`data-testid="calendar-subscriptions-empty"`) worded from the response's `confirmed_subscription_count`: `No confirmed subscriptions yet — recurring candidates ask on /transactions before they land here.` when it is 0, `No subscription payments in <Month> <year> — a subscription appears here once it is confirmed: a bill, or a candidate you accepted on /transactions.` otherwise (never silence). The click-through is unchanged: it still hands the payee to the M55 `/bills?autofill=…` hand-off, though the `amount` param is written only for a CAD figure (a CAD bill draft must not be prefilled with a USD one). **M170 follow-up currency honesty (docs/36 decision 15):** each amount renders via `formatMoney(amount, currency)` — `-US$11.99` for a USD group, `-$11.99` for a CAD one — and a mixed/unknown-currency group (or a variable bill) carries `null`/`null` and paints the app's `—` dash instead of a fabricated CAD figure; no fx read happens on this path. Lazy like every other route; reachable from the **Calendar** sidebar row between Reports and Taxes (M68) ||
| **Security detail** (`/securities/:securityId`) | Per-security dashboard fed by `GET /securities/{id}/overview`: header (symbol, name, kind badge, exchange + listing currency, watchlist toggle), latest-price card with source + staleness warning, 52w high/low + window-change strip, price-history area chart (last ≤365 cached closes), Fundamentals card (EPS, P/E, market cap, dividend yield %, beta, sector/industry/country, 52w range — the figures and their tooltips render ••••• on a masked session, docs/46 §D2/F-12; "No fundamentals available" empty state keeps manual entry discoverable) with inline manual-edit override, Holdings composition card for ETFs/mutual funds (symbol, name, asset-class badge, region, sector, weight bar; total-weight footer with unclassified remainder; "Import from market data" ghost button; "Edit manually" inline editor with add/remove rows), holdings table across accounts with totals ("Not held — watchlist only" empty state), recent-trades card; **Record dividend** card (record a dividend / distribution / return-of-capital on the security — docs/22) |
| **Accounts** (`/accounts`, M100) | The accounts INDEX — the destination the rail's Accounts entries open (expanded section heading, collapsed wallet icon, ⌘K `page-accounts`). Grouped list using the SAME `lib/accountGroups.ts::TYPE_GROUPS` the sidebar renders (Cash / Credit / Registered / Non-registered), one row per account → `/accounts/${accountId}`, showing name · type label · `total_value` in the account's OWN currency. **No cross-currency total is computed anywhere on the page**, so no FX rate is needed and none is fetched (docs/01 money rule). Header action `Add account` → `/settings`, where the accounts manager's create form lives (docs/14 C2). Empty state = the standing three-part contract: what lives here / a real capture of this page populated (`/help/accounts.png`) / the one action that fills it. Shares the `["accounts"]` cache key with the rail and the Settings manager, so one request serves all three. Lazy like every other route (`tests/routeCodeSplit.test.tsx`) |
| **Account detail** (`/accounts/:accountId`) | Per-account dashboard: header (name, type badge, currency, cash_balance), **account value history chart (M118, §Account value history)** — on investment accounts it is the FIRST module (above holdings, full width, 280px: value per period in the reporting currency + marker pins for significant contributions/withdrawals/transfers, `Show` buttons in its data table jump to the row behind a pin); on cash accounts it REPLACES the old *Balance Over Time* ledger chart in the same slot with the same `RangePicker` (the ledger still feeds the Activity table), editable starting balance (inline edit, save on Enter/blur), filterable transactions table (same filter set as Transactions page — date range, payee, amount, direction; glass filter bar), the shared add/edit transaction editor with this account preselected, per-row Edit/Delete controls with confirmation, inline category combo-box per row, transfer detection buttons, paginated (50/page), transfer suggestions panel (accept/dismiss), Account Info sidebar on wide screens (type, currency, starting balance, transaction count). Styled to match Transactions page: two-column layout, sticky uppercase headers, row stagger animation; same `TableScroller` horizontal-scroll contract as Transactions (M64). **Transfer Suggestions panel (M112)**: both sides of a pair render inside ONE `grid-cols-1` shared track — every line tagged `data-side`/`data-cell` and `truncate`, a `"-"`/blank payee omitted, action buttons a `shrink-0` sibling of the grid (contract in docs/17 §Frontend; probe record below, §Transfer suggestions panel — one shared track). **Pending-import banner (M113)**: while this account has `pending_review` batches, a dismissible `role="status"` banner renders above the view — `N uncommitted import(s) for <type>` + a link to `/imports`, count only, absent for 0 (pattern + verification below, §Account page pending-import banner) |
| **Taxes** | ACB per holding, annual gains report view, ROC adjustments; export to CSV / printable HTML per docs/06 output format |
| **Settings** | LLM provider selection + model config; category management (hierarchy editor — a 409 `category_in_use` on delete renders the "reassign first" copy keyed off the envelope `code`, M113, docs/08 §Error envelope); **self-service account management** (display name, email + current-password re-auth, change password, delete account with confirm); **accounts & institutions manager** (grouped list, computed balances, inline creation); **import profiles** (pointer/CTA to the Imports wizard — the actual editor is `ColumnMappingWizard` on the Imports page, docs/03); **NotificationsSection** (alert settings for large transactions, low balance, dividends, price alerts, monthly summary — M31); privacy mode toggle (since M93 W2 it also masks every amount the partner's `/shared` page receives, docs/43 §9), encrypted backup controls, data export (M7 plan); **`ScheduledBackupPanel`** (M56, docs/11 — schedule form `frequency`/`hour`/`weekday`/`day of month`/`keep last N`, honest status line, artifact list with Download / Restore / Delete, restore-from-file input, "Back up now"; the runner itself is `ScheduledBackupRunner` mounted in `AppShell`); **`SharingSection`** (M90, docs/43 §13 — owner consent card: invite form, pending card with expiry countdown / in-session Copy invite link / Cancel, active card with the live account multi-select + two-step Revoke, collapsed `GET /sharing/audit` list; the `email_sent` truth is always shown) + **`PartnerSharingRow`** (the "You can see <owner>'s data — N shared accounts, since <date>" line, rendered only when `GET /partner/state` answers `200 {linked: true}`; `200 {linked: false}` renders NOTHING — it is data, not an error, so the invite CTA above carries the next step, M92; plus **Leave** — M93 D6 follow-up, docs/43 §13: two-step confirm ("Confirm leave" / "Stay" + what it does) → `POST /sharing/leave`; a success toasts "You left the shared household" and the row disappears on the refetched `{linked: false}`, rendered in place — never a redirect) || **Tags** (`/tags`) | Tag CRUD management: create, rename, delete tags (409 `tag_in_use` if referenced by transactions or rules; the friendly 409 copy on this page and in the manager keys off the envelope `code` via `hasErrorCode`, M113 — docs/08 §Error envelope). Tags label transactions beyond the single category FK. |
| **Assistant** (`/assistant`, M188) | The plain-English assistant (docs/57 §6): a question box (Enter to ask) → the answer, and when the reply implies a change, a **ProposedChangeCard** — target label + summary, a *Now* vs *With change* table pairing `before`/`after` keys (field labels incl. `category_id` resolved to the category name and `events` as "N events → M events"), **Apply** (primary) and **Dismiss** — plus the **History list** (`GET /assistant/history`): `Applied`/`Undone` status, applied time and an **Undo** button while `status == applied`; after undo the row renders the restored state with no Undo button. Refusals render the envelope `detail` verbatim with "nothing was applied" copy and NO card. All server state through react-query (`["assistant","history"]`); one shared `api()` client, no key, no upstream URL in the bundle. Reached from one nav row (rail `PRIMARY_GROUP`, label "Assistant", icon `Sparkles`) |
| **Rules** (`/rules`) | Categorization rules editor (match_type `regex` / `exact_payee` / `contains`, `priority`, `enabled`; first match by ascending priority wins — docs/04). Standalone page, not part of Settings. |
| **Goals** (`/goals`) | Savings targets with progress bars: create/edit/delete goals with name, target amount, target date, optional account link. Shows progress_pct, monthly_needed, status (on_track / behind / ahead / completed). |
| **Budgets** (`/budgets`) | Per-category spending limits: create/edit/delete budgets with monthly/yearly period. Progress bars with green (`--color-gain`, <80%) / amber (`--color-warn`, 80–100%) / red (`--color-danger`, >100%) color coding — the healthy fill is the semantic gain token, never `--color-accent` (accent stays the interactive/Calendar-kind color). Labels print the true ratio rounded to a whole per cent (`202% used` at 201.94% actual — never clamped; only the bar fill caps at 100%), and the Dashboard alert banner carries the same whole-per-cent value from one server-side helper (M65 / m61 #3). **M96**: a "Committed monthly (tagged recurring)" LABEL above the list — a sum over recurring-tagged rows, never a change to the progress math (§Recurring prompt). **M110 (§Budget carry)**: each row carries a one-click `Carry leftover` toggle (`aria-pressed`, off by default) and, when a value was carried in, its OWN line — `+$60 carried in` (gain) / `−$100 carried in — a debt to pay down first` (danger) — while the money line shows `spent / spendable` (the pool = assigned + carried_in) and the bar/label keep the true ratio against that pool; `pct_used: null` renders as **over budget** (danger), never a fabricated percent. |
| **Reconciliation** (`/reconciliation/:accountId`) | Bank statement matching: upload statement CSV (date, amount, description), view matched/unmatched items, summary cards (total/matched/unmatched/difference), match items to transactions. Linked from the Account Detail header **and** from every row of the Settings → accounts manager (M68). |
| **Bills** (`/bills`) | Bill reminders CRUD: create/edit/delete with name, amount, frequency (monthly/quarterly/yearly/one_time), due date. Mark-paid advances next due date. Color-coded urgency: overdue (red), due soon (amber), upcoming (neutral). **M116 (docs/25 §M116):** each row is a link to its drill-down and carries the two numbers its match rule found — `N payment(s)` (or the honest **0 payments yet**) and `avg $X` (the 12-period rolling mean, absent for a variable bill); the create/edit form gains an inline **Match transactions** block (mode select, **M121 term chips + the ALL/ANY switch**, amount tolerance) with a **Find past payments** preview pane that groups the hits per period BEFORE anything is saved. **M121 (§Match terms):** the term field is a CHIPS input — Enter commits one chip, Backspace on an EMPTY field removes the last chip, every chip has its own labelled ✕, the list caps at 10 with visible copy — and the `ANY`/`ALL` segmented control sits BESIDE it (default **ANY**, the widening mode); the preview caption lists every term the run used. |
| **Bill detail** (`/bills/:billId`, M116) | The drill-down docs/25 §M116 B2: header stating the CURRENT rule in prose ("Matches transactions whose payee *contains* … at $15.99 exactly"), a `N payments` badge, both rolling windows (mean + median over the last 6 and 12 periods), the period bar chart with its data-table fallback, and the paginated matched-transactions table — a row click opens the SAME `TransactionEditor` the ledger uses. **Edit rule** opens inline and says out loud that saving re-matches the ledger (D3), and (M121) edits the term CHIPS with the ALL/ANY switch beside them while the header joins the terms with **or** / **and** per the rule's mode — `Matches transactions whose payee contains “ROGERS” or “RCOM” …` — so the sentence can never describe one rule while the counts show another. **Live a11y (M116):** axe **0 violations in all six width × theme runs** (390 / 412 / 1440 × dark / light, real viewport recorded per run) and the chart's `<details>` data table reached in **7 Tab stops**, opening on Enter with the accent focus ring — evidence `frontend/scripts/a11y/m116-bill-probe.json` + `m116-bill-{390,412,1440}-{dark,light}.png`. |
| **Debts** (`/debts`) | Debt tracking: CRUD for mortgages, loans, credit cards with interest rate, minimum payment. Payoff projection (months, total interest, total paid). Summary cards for total debt and interest. |
| **Tax Projection** (`/tax-projection`) | CRA tax bracket estimation: employment income, capital gains, dividend income inputs with deductions. Federal + provincial (ON, BC, AB) tax computation showing taxable income, tax amounts, average/marginal rates. |
| **Shared household** (`/shared`, M90) | The partner's read-only page (docs/43 §13): owner identity from `GET /partner/state`, the scope banner ("Shared accounts: N — figures exclude everything else"), account cards, a paged transaction feed and budget cards labelled "shared accounts only" — every cipher field resolved with `dek = null` (🔒), NO write control anywhere. **Privacy mode (docs/43 §9, M93 W2):** with the OWNER's `privacy_mode` on, a `role="status"` banner under the scope banner reads **"Amounts hidden — the owner has privacy mode on, so balances and amounts are not shared with you. Names, dates and categories still show."** (the exact caption); each account card's figure becomes `🔒` with `title="Hidden by the owner’s privacy mode"`; each feed row resolves to `🔒` (`resolveAmount` treats a null `amount_native` as the locked shape); the Budgets section keeps its heading and the "shared accounts only" pill but replaces the cards with **"Budget amounts are hidden while the owner has privacy mode on."** — never a blank cell, never a bar drawn from hidden figures. Privacy off ⇒ the pre-M93 rendering, unchanged. `401 sharing_not_linked` renders the in-place empty state "No shared household — access was removed", never a redirect to /login (M92: that 401 comes from the /partner data routes, or from `loadSharedView`'s own guard on `200 {linked: false}` — same envelope, same rendering). |
| **Invite accept** (`/sharing/accept`, M90) | The invitee's landing (docs/43 §13): token from the query string only (never localStorage); Confirm → `POST /sharing/invites/accept` → `/shared`. Errors render the envelope verbatim (`detail` + `code`) plus a hint, never a redirect; the token survives sign-in/register via route state (`state.from`). |
| **Help** (`/help`) | In-app documentation: hub of per-feature guide cards linking to `/help/:slug` guide pages (numbered steps with demo screenshots). Copy lives in `frontend/src/pages/helpGuides.ts`; screenshots in `frontend/public/help/`. Full spec docs/32. Deep links to `/help` are served `index.html` by an exact-match nginx location in `frontend/Dockerfile` (`dist/help/` is a real directory of screenshots, so the generic `try_files $uri $uri/` fallback would 301 to `/help/` then 403; `absolute_redirect off`/`port_in_redirect off` additionally pin any redirect to the internal port). |
| **NotFound** (`*`, M68) | Branded catch-all for any unmatched URL: `404` mark + "Page not found" + one line of explanation, primary **Go to Dashboard** and a **Back** button (`navigate(-1)`), rendered inside `AppShell` so the chrome stays. Never shows React Router's "Unexpected Application Error!". |
| **Login / Register** | Email + password forms. **M87 (docs/42): register does NOT auto-login any more** — `POST /auth/register` answers the same token-free `200 {status: "accepted", detail}` for a free and a taken address (an account-existence oracle otherwise), so the form renders one "Registration accepted — sign in to continue" state with a **Sign in** button leading to `/login`; the session always comes from `POST /auth/login`. The "Explore the demo" ghost button (one-click-loads the seeded demo tenant `demo@fintracker.local` · `demo-password`) appears on **Login only** |

## App shell & auth flow

Routes: `/login`, `/login/oidc`, `/register` (public); `/dashboard`, `/transactions`, `/imports`, `/investments`, `/reports`, `/forecast`, `/assistant`, `/retirement`, `/calendar`, `/taxes`, `/settings`, `/currencies`, `/securities/:securityId`, `/accounts` (M100), `/accounts/:accountId`, `/tags`, `/rules`, `/goals`, `/budgets`, `/reconciliation/:accountId`, `/bills`, `/bills/:billId` (M116), `/debts`, `/tax-projection`, `/shared`, `/sharing/accept`, `/help`, `/help/:slug` behind an auth guard redirecting to `/login`. All 32 `src/pages/*.tsx` modules load lazily, one per route (the list above plus the `*` catch-all) — see **Route loading & code-splitting** below.

> **M185 — IMPLEMENTED (card `t_778537dd`).** The Ledger shell described in
> `docs/47` replaced the **left rail** with a **top tab bar
> (`components/shell/TopBar.tsx`) + `More` panel + persistent truth bar
> (`shell/TruthBar.tsx`)** on desktop and a **five-tab bottom bar (Home ·
> Activity · Add · Portfolio · More, `shell/MobileTabBar.tsx`) + drawer** below
> 768 px; `components/layout/MobileNav.tsx` and the rail were deleted. Routes,
> the auth guard, lazy loading and every keyboard contract below are unchanged;
> the mask chip's home is now the top bar's right cluster (`MaskChip` testid,
> `aria-pressed` and `⌘⇧H` untouched), its second row is the account menu's
> `mask-toggle-row`, and the drawer carries the More panel's three groups plus
> the session controls. The rail/six-tab paragraphs that follow are the
> PRE-M185 state, kept as history; where they conflict, this callout and
> `docs/47` win.

**Mask chip (M139, docs/46; placement updated by M185).** The desktop top
bar's right cluster carries `MaskChip` — the session display mask's indicator
**and** its switch, one control, one tap. It is a `<button aria-pressed>` with
three state channels (icon `Eye`/`EyeOff`, text `Masked`/`Unmasked`, and
colour), so the state never rides on colour alone. The same switch appears once
more as the account menu's `mask-toggle-row` (avatar menu, beside the theme
switch, where every other session control lives) and again in the phone
drawer's footer; all read one store, so they cannot disagree. Below `md` the
cluster chip is icon-only and reuses `RailTip` (M100) for its label — and it is
a FIXED `size-11` (44×44) icon button whose collapsed branch builds its OWN
class list. Never give it a percentage width (`w-full`) or compose the
collapsed look out of shared utilities with overrides (`px-0` sitting next to
`px-3`): QA-360-TOUCH-REG (production, candidate 2df3629) measured the
`w-full` chip resolving to the appbar's whole 110px cluster max-content — its
12px padding rode along through the cascade — which evicted the trailing
`shrink-0` 44px command-palette trigger past the viewport edge (x=358..402 at
360px, ~2px visible; x=388..432 at 390px). The cluster therefore reads
[44px chip][6px gap][44px trigger] at every width; `tests/m95CommandPalette.test.tsx`
pins the resolved 44px chip width, zero lateral chip padding, the last-child
trigger structure and the row's own grid arithmetic at 360 AND 390 in the
logged-in (mask chip present) and logged-out states, and the real-engine
center-tap hit (whole trigger visible, tap on it opens the palette) is the
exact-SHA geometry gate on top of that. The
switch belongs in the shell rather than on a page so a masked user never has to
visit a particular screen to unmask.

**Real-time flip, no re-render exceptions (M140, docs/46 §D3/§D6).** The flip is
instant on **every** money surface — not just the shell's own chrome. Each of the
73 components that renders a money string (the sweep prints the authoritative
count) calls `useMasked()` itself, so a flip
re-renders it in place (same DOM nodes, no remount) and the mask-aware formatters
re-read the ambient mask on that pass. It fires **zero network requests**: no
query invalidation, no refetch, no session re-probe — the mask lives below the
data layer. Recharts' memoized `XAxis`/`YAxis` redraw their money ticks too,
while date labels and percentages stay readable (the scale hides, the trend does
not). This is enforced, not asserted-by-review: `scripts/sweep_money.mjs --check`
fails the build on a money component with no subscription, on money built inside
`useMemo`/`useCallback`/`useEffect`, and on a currency formatter that bypasses the
mask-aware helpers without consulting the mask itself.

**Sidebar nav rows (M68).** `NAV_ITEMS` in `components/layout/AppShell.tsx` runs
Dashboard → Transactions → Imports → Investments → Reports → **Calendar** →
Taxes → **Tax Projection** → **Shared household** → Settings → Help. Calendar sits between Reports and
Taxes (M52 shipped the page with no row — closed here); Tax Projection sits
beside Taxes rather than in the **Tools** group, because Tools is gated on
`user.currency_tracker_enabled` and a Tools entry would re-orphan the page for
every account with the tracker off. `/reconciliation/:accountId` keeps its
contextual entry on Account detail and gains a `Reconcile` link on every row of
the Settings → accounts manager, so neither surface is reachable from only one
place. **Shared household** (M90) sits between Tax Projection and Settings and
is shown to every account — a user with no link simply gets the page's empty
state, so the shell needs no per-user fetch and the row never flickers.
`MobileNav` keeps its fixed six tabs (Home / Txns / Import / Invest /
Reports / Settings) — below 768px the sidebar collapses to the icon rail, which
carries every nav row including the new M90 one.

**Catch-all route.** `path: "*"` is the LAST child of the guarded shell route,
so every real path outranks it; it renders the branded `NotFound`
(`data-testid="not-found"`: chrome, a primary **Go to Dashboard** and **Back**).
React Router's own error boundary — the developer-facing "Unexpected
Application Error!" — is therefore unreachable, including for unauthenticated
visitors (they are bounced to `/login` first). `SecurityDetail`'s not-found
panel additionally carries **Back to Investments**.

**Auth guard → login → post-auth destination.** `RequireAuth` stashes
`pathname + search` on the `/login` navigation state; `postAuthRedirect(state)`
(`src/auth/postAuthRedirect.ts`) resolves it for **both** the password form and
the "Explore the demo" button, so a deep link requested while logged out
survives the trip (`/rules` while logged out → login *or* demo → `/rules`).
Anything that is not a same-document path falls back to `/dashboard`. Pins:
`tests/m68Navigation.test.tsx`.

### Read-only sessions (demo tenant) — M61 #2, M65

`src/auth/readOnlySession.ts::isReadOnlySession(user)` is the client's mirror of
the server's write boundary (`api/deps.py::_assert_demo_mutation_allowed` →
403 `demo_account_restricted` for `demo@fintracker.local`; reads stay open,
because the demo is the front door). Three rules follow from it:

1. **A write boundary must never hide a calculation.** Anything the demo cannot
   persist but CAN compute must still produce its result on screen — a 403 may
   never replace the Results area with an `ErrorPanel` before a single chart
   line drew. The Projections calculator was this rule's worked example: it
   moved into the browser (docs/12 §7, `lib/projectionEngine.ts`, zero network
   calls) precisely because POSTing first 403ed for the demo tenant.
   **[M157]** the calculator POSTs again — the honest fix turned out to be
   removing a write boundary that was guarding a *non-write* (`POST
   /projections/run` is pure request-body math and joined
   `DEMO_MUTATION_EXEMPT_PATHS`, docs/20 §6), not keeping the compute in the
   browser. The rule itself is unchanged and still governs anything the demo
   genuinely cannot call.
2. **A write that can only fail degrades to a non-blocking `toast.info`**
   ("Demo is read-only — scenario not saved") shown *alongside* the rendered
   result, never to an `ErrorPanel` that replaces it.
3. **One message per page, maximum.** The Projections submit button used to
   render an inline read-only note next to itself AND the `ErrorPanel` below
   for the same failure; the duplicate inline copy is removed.
   **[M185]** the shared shape every write surface now uses is ONE
   `data-testid="demo-refusal"` element with `role="status"` plus ONE
   `toast.info("<Action> — the demo account is read-only")`. A
   `demo_account_restricted` response NEVER renders `role="alert"` —
   `components/ui/Toast.tsx` renders an error tone AS one — nor an
   `error-panel`. The full refusal contract, the padlock rules and the
   per-control treatment registry (which controls lock, which stay disabled
   with a visible reason and why) are `docs/47` §4.

## Route loading & code-splitting (M49)

Every page module loads on demand; `frontend/src/App.tsx` is the only wiring point.

**Shape.** `route(loader)` (exported from `App.tsx`) wraps one dynamic import per route in the route-loading primitives:

```tsx
{ path: "dashboard", element: route(pages.dashboard) }
// pages.dashboard = () => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard }))
// route(...)      = <RouteLoadError><Suspense fallback={<RouteFallback />}><Page /></Suspense></RouteLoadError>
```

- **All 24 page modules are dynamic** — one `route(pages.*)` entry per file in `src/pages/`, covering the 24 routes listed above (22 since M49, plus `Calendar.tsx` in M52 and `NotFound.tsx` in M68). A page added without a `route(...)` entry fails `tests/routeCodeSplit.test.tsx`.
- **Eager, never split:** the router, `AuthProvider` + `RequireAuth` (session probe and branded splash), `AppShell`, `ToastProvider`, and the route-loading primitives themselves.
- **Loading fallback:** `RouteFallback` (`frontend/src/components/ui/RouteFallback.tsx`) — shimmer placeholder (page-title `Skeleton` + two `SkeletonCard` KPI cards + a chart `SkeletonCard`), `role="status"` `aria-label="Loading page"` `data-testid="route-fallback"`. Loading states are shimmer placeholders, never a spinner or "Loading…" text (the `Skeleton` ui-contract).
- **Failed chunk:** `RouteLoadError` (same file) renders the shared `ErrorPanel` with the underlying message plus a primary **Reload** button, `data-testid="route-load-error"`. React caches a rejected dynamic import for the life of the document, so an in-place retry cannot succeed — a stale `index.html` pointing at a purged hashed chunk shows this panel instead of a blank page.
- **Test pin:** `frontend/tests/routeCodeSplit.test.tsx` (6 tests) — source contract (every `src/pages/*.tsx` has a matching dynamic import and `route(...)` entry, zero eager `from "./pages/…"`, shell/auth still eager) plus behaviour (fallback while pending → page after resolve → error panel on rejection).

### Bundle facts (`npm run build`, raw bytes in `dist/assets`)

| Build | Entry chunk | JS chunks |
| --- | --- | --- |
| Before M49 (`38b0fec`, 2026-09-28) | `index-vsfXyiJi.js` = **1,250,336 B** (gzip 354.65 kB) | 1 |
| After M49 | `index-BIYGCyGu.js` = **443,573 B** (gzip 140.35 kB) | 57 (entry + 56) |
| After M52 (2026-09-28) | `index-DWADSRoq.js` = **448,848 B** (gzip 141.32 kB) | 61 (entry + 60) |
| After M52 + M56, wave-3 integration (2026-09-28) | `index-gUoZzd_S.js` = **465,886 B** (gzip 146.09 kB) | 58 (entry + 57) |
| After M72 (2026-09-30) | `index-Dc8Qbt5v.js` = **487,894 B** (gzip 150,822 B) | 61 (entry + 60) |
| After M95 P1 (2026-10-04) | `index-DLWzjq1v.js` = **503,617 B** (gzip 155.94 kB) | 72 (entry + 71; `CommandPalette` 5,645 B and the shared `helpGuides` 44.36 kB load on the FIRST ⌘K, never at startup) |
| After M95 P1–P3, integrated with M96 + M97 (2026-10-04) | `index-DTgK0rOi.js` = **506,964 B** (gzip 157.01 kB) | 80 (entry + 79; P2's 47 empty-state sites and P3's undo snackbar + blast-radius dialog all live in the entry — no new lazy boundary. The chunk count is the integrated tree's, so the delta over the P1 row also carries the sibling M96/M97 commits this rebased onto) |
| After M100 (2026-10-04) | `index-TGmGY3P-.js` = **510,878 B** (gzip 158.18 kB) | 82 (entry + 81; M100 adds the lazy `Accounts` page chunk, and the count is the integrated tree's — the delta over the M97 row also carries the M98/M98b/M99 siblings) |
| After M101 (2026-10-05) | `index-gG-lSvsK.js` = **510,858 B** (gzip 157.53 kB) | 82 (entry + 81; M101 only swaps one class string in `AppShell`, so the entry moves **−20 B** — the change itself is CSS: `index-CgjxB6hq.css` = **47,123 B** (gzip 9,423 B), of which the new M101 media block is **141 B raw / 122 B gzip**) |
| After M185 + the keyboard/focus hardening (card `t_23b8d18e`, 2026-10-16) | `index-CV_s0WR2.js` = **544,429 B** (gzip 167,369 B) | 103 (entry + 102; the first row since M101 — it carries the whole integrated M185 redesign tree plus this card, so it is not this card's own delta. Docs are not part of the bundle, so the number matches the commit it is recorded in) |

M50–M55 added route wiring only: the entry chunk grew **443,573 → 448,848 B (+1.2%)**, still far under the ≤ 700,000 B target, and **M52's `Calendar` chunk is 8,470 B raw (3,197 B gzip)** — the month grid is never paid for by a user who does not open `/calendar`. M56 has no new route but its code sits in the entry (`ScheduledBackupRunner` mounts in `AppShell`, the panel is statically imported by Settings), which is what takes the integrated build to **465,886 B (+3.8% vs M49)** — still well inside the 700,000 B target.

M72 measured **475,085 B** (`index-DJyZX9-C.js`) on its own branch, then landed
cherry-picked on top of M75, whose `ScheduledExportRunner` also mounts in
`AppShell` — so the entry that ships is the **487,894 B** row above. That row
records the build example.internal runs (`215fe19`): `npm run build` at that SHA
reproduces `index-Dc8Qbt5v.js` byte-for-byte, which is what the deployed
nginx serves. The **raw byte count is the stable figure, not the hash** — a
later integrated tree (M71 re-hashed its route chunk) rebuilds to the same
487,894 B raw with a different filename and gzip 150,826 B, so treat a hash
in this table as "as measured", never as a pin.

Entry chunk **1,250,336 → 443,573 B (−64.5%)**, against the ≤ 700,000 B target. Each page ships as its own chunk (raw bytes; these are the M49 snapshot — later milestones moved a few: Settings is 46,593 B after M56's panel): Login 2,965 · Register 1,940 · Dashboard 12,321 · Transactions 27,055 · Imports 33,763 · Investments 34,647 · SecurityDetail 24,011 · AccountDetail 22,242 · Reports 43,569 · Taxes 12,608 · Settings 36,331 · Tags 3,806 · Rules 20,083 · Currencies 2,428 · Goals 5,182 · Budgets 4,660 · Reconciliation 5,211 · Bills 5,493 · Debts 5,186 · TaxProjection 6,214 · Help 1,124 · HelpGuide 1,750. Shared chunks load with the first page that needs them (`chartDefaults` 315,651 B recharts, `CartesianChart` 55,291 B, `CategoriesDonut` 41,802 B).

`NEXT_WAVE_PLAN.md`'s M49 baseline (1,212,611 B in `index-BVcX3a8M.js`, "21 page components", 0 `lazy` / 0 `Suspense`) was measured before M48-S5 and counts one page short: `src/pages/` has 22 `.tsx` page modules (22 since 2026-09-03), and the pre-M49 entry at `38b0fec` is 1,250,336 B.

### Verification (M49)

- Gates: `npx tsc --noEmit` → exit 0; `npx eslint . --max-warnings 0` → exit 0; `NODE_ENV=test npx vitest run` → **28 files / 260 tests pass** (6 new in `routeCodeSplit.test.tsx`); `npm run build` → exit 0.
- Browser smoke (2026-09-28): this build served by `vite preview` on `http://localhost:4173` with `/api` proxied to the deployed API; a `MutationObserver` injected into `dist/index.html` **ahead of the app module script** (build artifact only, not committed) recorded every fallback/error render from document start.
  - **All 22 routes** swept in one document by client-side navigation (1.9 s settle each): every route had `path === requested`, rendered its page (`main h1` or page body, 111–5,669 `innerText` chars), fetched exactly its own `assets/<Page>-*.js` chunk, and reported 0 load-error panels, 0 stuck fallbacks, rAF at the 600-frame cap with `visibilityState: "visible"`.
  - **Fallback rendered on 22/22 routes**: 19 during the sweep; `/dashboard`, `/transactions` and `/investments` on their earlier cold visit in the same document (their chunks were already resident when the sweep began, so React resolved them without suspending).
  - Fresh-document deep links render through the fallback and then the page: `/login` (fallback at t=49 ms), `/dashboard` (t=103 ms), `/reconciliation/1` (t=136 ms) — session restored by `POST /auth/refresh` → 200 on each load.

## Data fetching & query keys

All client reads go through React Query (`@tanstack/react-query`) over the typed wrappers in `frontend/src/api/*`. The cache is keyed by the **exact `queryKey` literal only** — it has no knowledge of the `queryFn` — so the contract is **one key = one `queryFn` = one payload shape**, per component:

| key | queryFn | payload |
| --- | --- | --- |
| `["categories"]` | `listCategories(200)` (AppShell, Budgets, Rules, Transactions, SimilarExamples, AccountDetail) | page `{items: Category[], total, limit}` |
| `["categories","all"]` | `fetchAllCategories()` (Settings `CategoryManager` only) | bare `Category[]` (pages looped to `total`) |
| `["cashflow-calendar", month ?? "current"]` | `getCashflowCalendar(month)` (Calendar page, M52) | `CashflowCalendar {month, today, days: [{date, events[]}]}` — the default-month fetch and an explicit month are separate cache entries (`"current"` vs `"2026-09"`) |
| `["price-history", securityId, from, to, granularity]` | `getPriceHistory(...)` (`hooks/usePriceHistory.ts`, M126) | `PriceHistoryPage {security_id, from, to, granularity, requested_granularity, items: [{date, price}], count}` — one entry per (security, window, bucket size), so changing the range is a different payload, not a client-side view. `granularity` rides the key because the SERVER buckets the series (docs/08 §Price history) |
| `["bill-detail-chart", id, from, to]` | `getBillDetail(id, {from, to})` (Bill detail, M126) | `BillDetail` — only the `chart` field is read: the period chart gets its own window while the stats and transactions table stay all-time. Disabled for the `All` default, which is exactly the payload `["bill-detail", id, offset]` already carries |
| `["portfolio-value-history", from, to]` | `getPortfolioValueHistory({from, to})` (Portfolio header card, M130) | `PortfolioValueHistory` — the summed investment-account series + markers + the `stats` block the header's range sentence renders. `from` is `null` on the `All` default (`rangeWindow("All")` has no floor, so the request omits the parameter and the SERVER picks the tenant's floor) and an ISO date on every preset; `to` is today. One cache entry per preset, and the SERVER buckets the series |
| `["search", trimmedQuery, vaultUnlocked]` | `fetchGlobalSearch(...)` (`frontend/src/lib/globalSearchQuery.ts`, the ONE shared queryFn) | `SearchResults` (docs/08 §Search, docs/33 §Server-first computing) — AppShell's app-bar search panel and the ⌘K palette are two RENDERINGS of one cached `GET /search` request (limit 10, vault-aware blind-index contract), so the key literal appears in both components ON PURPOSE and the pin guarantees both delegate to the same callee: a query typed in one surface is a cache hit in the other. `vaultUnlocked` rides the key because the payload genuinely differs by it (blind-index token + encrypted rows vs `hide_encrypted=true` + a `withheld` count); the fn re-reads LIVE vault state around the async token generation (the key's term is only a render-time snapshot) so a lock landing mid-flight can never send a stale token — races pinned by `tests/lockedVaultSearchRace.test.tsx` and `tests/globalSearchQueryFn.test.ts` |

Any other `list*(limit)` key follows the first row's pattern: page shape, read as `.data?.items`.

**Why it is pinned.** Two components declaring the same literal key with different `queryFn`s silently swap payloads: whichever fetched first decides what the other one reads. This shipped — `Rules` held `["categories","all"]` with `() => listCategories(200)` (page) while `Settings`' `CategoryManager` held it with `fetchAllCategories()` (`Category[]`), so a visit to `/rules` followed by `/settings` handed `buildTree(categories.data)` a non-iterable page object and the M49 route error panel replaced the whole page with `TypeError: t is not iterable` (`frontend/src/pages/Settings.tsx` `CategoryManager` → `buildTree`; stack `Settings-*.js:11` `const _=l.data,g=ls(_)` → `for(const a of t)`). The reverse order fails silently: `Rules` reads `categories.data?.items ?? []`, an `Array.data.items` is `undefined`, and the rule form renders with no category names. Fix: `Rules` moved to the shared page-shape `["categories"]` key (`frontend/src/pages/Rules.tsx`). Invalidation always uses the prefix `["categories"]`, which matches both keys.

**Enforcement.** `frontend/tests/queryKeyShapes.test.ts` parses every `useQuery`/`useInfiniteQuery`/`useQueries` options object under `src/`, groups the normalized literal keys, and fails when one key maps to more than one `queryFn` callee, or when an options object carries a literal key with no inline `queryFn` (an unresolvable shape), plus a floor on the number of keys scanned so the check cannot go vacuous. It is regression-proven: on the pre-fix source it fails with exactly `[\"categories\",\"all\"] <- listCategories, fetchAllCategories`, and passes after the fix. The second collision it caught — AppShell and the palette each declaring an inline queryFn under the shared `["search", trimmedQuery, vaultUnlocked]` key — was resolved by extraction, not key-splitting (the two surfaces share one payload, so splitting the key would have thrown away a legitimate cache hit): both components now delegate one line each to `lib/globalSearchQuery.ts`, and the pin stays exactly as strict as before. The second collision it caught — AppShell and the palette each declaring an inline queryFn under the shared `[\\\"search\\\", trimmedQuery, vaultUnlocked]` key — was resolved by extraction, not key-splitting (the two surfaces share one payload, so splitting the key would have thrown away a legitimate cache hit): both components now delegate one line each to `lib/globalSearchQuery.ts`, and the pin stays exactly as strict as before.

## Tooltips & in-app help

All inline explanations use the shared `HelpHint` component (`frontend/src/components/ui/HelpHint.tsx`): the app's ONE tooltip pattern for EXPLANATIONS — no tooltip library (YAGNI). Copy lives inline at the usage site (no copy catalog — YAGNI at this size); the `/help` hub and `/help/:slug` guide pages (`frontend/src/pages/helpGuides.ts`, screenshots in `frontend/public/help/`) carry the full guides, mirrored on the static site. Full spec docs/32.

**The contract (M97) — enforced by `frontend/tests/helpHint.test.ts` (the primitive) and `frontend/tests/m97Tooltips.test.tsx` (each surface's copy):**

| Property | Behaviour |
| --- | --- |
| Trigger | a real `<button>` — keyboard focusable, Enter/Space activates it; accessible name is `What does this mean?` (the explanation rides the *description*, so it is announced once, not twice) |
| Association | `aria-describedby` → the tooltip element **at all times**. While closed the content is `sr-only`, still in the accessibility tree: a screen reader reads it from the focused trigger with no hover anywhere in the loop |
| Show | on **focus** (a superset of `:focus-visible`), on pointer hover, and on **tap** — nothing anywhere is hover-only |
| Dismiss | `Escape` on the focused trigger (`stopPropagation`, so a tooltip inside a dialog does not close the dialog), pointer-leave for hover, second tap for tap |
| Mobile | tap toggles it. Pointer events with `pointerType === "touch"` deliberately never hover-open: browsers fire `pointerenter` on touch too, which would open it and close it again on the matching leave |
| Placement | portalled to `document.body` and measured from the trigger rect (flipped below when it would clip, clamped to the viewport, re-placed on scroll/resize; `max-w-[min(20rem,calc(100vw-1.5rem))]` so a long derivation cannot overflow 390px). Portalled because `TableScroller`/`Card` overflow would clip an in-tree bubble and a `framer-motion` `transform` ancestor would make `position: fixed` resolve against the wrapper |
| Contrast | opaque `bg-surface-raised` + `text-text` = pair **2** (≥4.5:1) behind a `border-control` edge = pair **12** (≥3:1), both themes — measured by channel B; the classes are pinned by `tests/helpHint.test.ts`, so moving the bubble off those tokens fails the suite |
| `className` prop | REPLACES the default `ml-1` gap only (the `/rules` Priority cell passes `ml-0.5 lg:ml-1`); every other class is the component's own |

### Rail label bubble — `RailTip` (M100)

The collapsed rail is the one surface where a control has **no visible text
at all**, so its bubble shows the control's *name*, not an explanation of it.
That is the opposite of `HelpHint`'s job, and wiring an identical string to
`aria-describedby` would announce the label twice (name + description) — the
one thing HelpHint's own contract exists to avoid. Hence a second, narrower
primitive, `components/layout/RailTip.tsx`, sharing HelpHint's rules but not
its trigger:

| Property | Behaviour |
| --- | --- |
| Trigger | the icon-only control itself, which carries `aria-label = destination` (== the expanded row's visible label). The bubble is its VISUAL twin and is deliberately **not** `aria-describedby`-linked: nothing is conveyed by hover that the accessibility tree does not already carry, so no hidden copy sits in the tree |
| Show | on **focus** and on pointer hover (never hover-only); `pointerType === "touch"` never hover-opens |
| Dismiss | `Escape` on the focused trigger (`stopPropagation` — the shell's Escape router must not also fire), pointer-leave for hover |
| Expanded rail | `enabled={false}` makes the component a pass-through: no handlers, no portal, byte-identical DOM (the label is visible text there) |
| Placement | portalled to a named `region` layer at the end of `<body>` (`#rail-tip-layer`), 8px right of the trigger, vertically centred and clamped to the viewport — the rail's middle column is `overflow-y-auto`, so an in-tree bubble would be clipped at the 76px edge |
| Contrast | the same tokens as HelpHint: opaque `bg-surface-raised` + `text-text` behind `border-[rgb(var(--color-border-control))]`, both themes |
| Size floor | every collapsed control it wraps is `min-h-11` (44px) — see §Touch-target floor |

Pinned by `tests/m100AccountsNav.test.tsx` (bubble text == destination,
portalled out of the rail, Escape closes only the bubble, expanded rail renders
no bubble, axe-core zero violations with a bubble open).

**M97 sweep — what gets a hint, and what deliberately does not.** Non-self-evident aggregates, erring toward labelling; but **no hint where the derivation is literally on the same screen**: the net-worth trend chart already draws cash + positions stacked to net, every `% used` card prints spent / limit directly above its bar, and the spending donut prints its own total.

| Aggregate | Surface | The hint says |
| --- | --- | --- |
| Net worth | Dashboard KPI | `Assets − Liabilities, as of <min(bucket end, range end)>` with **both numbers inline** plus the exclusions — backed by the bucket's `assets_cad` / `liabilities_cad` split (docs/12 §4). The as-of date is clamped because a month bucket's end is in the future mid-month while its data is only as new as the range |
| Range net · Positions value · Cash | Dashboard KPIs | the population each number sums (Range net = income − spending over **every bucket in the range**, M138 D-5), and the price/FX rule (newest cached ≤ date, unresolved = excluded, never fetched) |
| This month · vs last month · daily burn · projected month-end | Spending insights card | the exact formula, including the honest shape of the comparison — this month **to date** against **all** of last month — and that the projection is a straight-line pace, not a forecast |
| `% used` | Dashboard budget cards, Budgets page, Shared household | the M65 true ratio: `spent ÷ limit — never clamped (201.94% prints as 202%); only the bar fill stops at 100%`. **M110 variant (§Budget carry):** with a carry in play the same sentence reads `spent ÷ (limit + carried in) — the same true ratio, never clamped. A carried debt bigger than this period's limit leaves no ratio to print, so the row says "over budget" instead of inventing a percent.` (`LIMIT_HINT` / `CARRY_HINT` in `frontend/src/lib/budgetCarry.ts` — the carry-OFF string is byte-identical to the pre-M110 one, so nothing about a row that never opted in changes) |
| TWR · MWR · net flows · investment income | Investments `Period return` card | one sentence each, which figure the app shows where, and why the other exists (docs/05 §Total return) — parallel to the card's own standing paragraph, which stays as the no-hover fallback. **TWR / MWR additionally hang a precision hint off the VALUE itself** (M105): the card slices the 8-decimal wire string to a 2-decimal display, so the value carries `<full string>% precise` + the same string in its `aria-label` — the trim is recoverable, not lossy (docs/05 §Total return → Display) |

### Help hub quickstart — three tracks (M89 L2)

Above the guide grid on `/help`, the hub opens with a **Start here — three ways
in** section: an `<ol>` of three `Card`s, each headed by a nowrap `TRACK N`
eyebrow above its title (stacked, not inline — inline wraps mid-word in a
240px card), a blurb, and numbered steps where **every step is a working link**
(the page is executable, not descriptive):

| # | Track | Steps (in order) |
|---|---|---|
| 1 | **Try it in 2 minutes** | `/dashboard` → `/help/first-import` (+ `download` chip to `/help/import-samples/sample.ofx`) → `/transactions` |
| 2 | **Own your data** | `/register` → `/imports` → `/settings` (export & backup) |
| 3 | **Drive it from your agent** | `/help/agent-integration` → `/help/configuration` → `/help/agent-tour` |

Track 1's import step deliberately points at the `first-import` walkthrough
rather than `/imports`: `POST /imports/upload` under a demo session answers
`403 demo_account_restricted` (docs/20 — the guard is load-bearing, it
protects the demo dataset), so promising a live demo upload would be a broken
promise. The step's hint says so out loud ("The demo can't upload — it is
read-only") and step 3 is a browse of the seeded feed, not "the transactions
you just imported". The section header also carries the **Take the tour**
button (below), and the guide grid follows under a plain `All guides`
heading. Pinned by `tests/m89HelpPath.test.tsx`.

### Guided first tour — coach marks (M89 G1)

`frontend/src/components/tour/Tour.tsx`, mounted in `AppShell` as
`<Tour user={user} />` (props, not context — the tour needs the user but must
not own a session).

**Trigger.** Demo tenant (`isReadOnlySession(user)`) **and** first visit to
`/dashboard` **and** no stored outcome **and** not already opened in this
tab-session. Targets are four `data-tour` hooks resolved by AppShell:
`nav` (the nav `<ul>`; `nav-toggle` — the phone header's menu button — is the
fallback when the rail is off-canvas), `imports`, `reports`, `help`.

**State — two flags, client-side only, never a server field** (it teaches the
UI, it is not user data):

| flag | store | key |
|---|---|---|
| outcome (`completed` / `skipped`) | `localStorage` | `fin_tracker.tour.v1.<user.id>` |
| already opened this tab-session | `sessionStorage` | `fin_tracker.tour.session-started` |

Every access is `try`-wrapped: unavailable storage degrades to "offers itself
again next time", never to a crash. `requestTour()` (Help's button) dispatches
`fin_tracker:tour:start`, which ignores the stored outcome — that is the
resurface path, so nobody is trapped by having skipped it. Spanish/other i18n
is out of scope: one locale, English.

**Four marks, one sentence each**, keyword-highlighted with `<strong>`:
the nav list ("everything lives in this list"), Imports ("how your data gets
in" — staging batch), Reports ("how you get it back out"), Help ("three
quickstart tracks"). The Imports mark also states that the demo itself is
read-only and points at Help's first-import walkthrough (M92): it may never
imply a live demo upload, which answers `403 demo_account_restricted`.

**Interaction contract.** `role="dialog"` with `aria-labelledby`/`aria-describedby`
(and `aria-modal` deliberately absent — the rest of the app stays live), focus
moves to the primary control on every step change, Tab cycles inside the card
(first↔last wrap), Escape dismisses from anywhere and persists as `skipped`,
all controls `min-h-11` (44px). **Never modal:** the scrim is four
`pointer-events-none` veils cut *around* the highlighted region, the ring is a
`border-2 border-accent` box at `z-[61]` (veils `z-[60]`, card `z-[62]`), so
the app stays clickable — the marks highlight, they do not seize the page.
Geometry transitions only, which the global `prefers-reduced-motion` rule
collapses to instant: under reduce, the static highlight is what remains.
A target not fully inside the viewport (collapsed rail, phone drawer closed,
short viewport) drops the ring and docks the card bottom-left — the words still
teach and nothing is ever pointed at a hidden element.

Pinned by `tests/tour.test.tsx` (11 tests: first-visit trigger, second-session
silence, skip persistence, non-demo no-fire, off-dashboard no-fire, the
tab-session flag, resurface, Escape, focus trap, per-account key, spotlight).

### Guide screenshots: real prod captures, refreshed by the standing sweep (M89 G2)

Guides render **real captures of the running product, never mockups**, and each
walkthrough shows every state it talks about (`first-import`: empty → file
chosen → preview → the red/amber/green verdicts → the decision → committed →
in the feed; `first-budget`: the form and an over-100% card; `agent-tour`: the
M80 transcript rendered from `docs/assets/m80/`). A screenshot is only true for
the UI it was taken from, so **the standing QA sweep re-captures every guide
screenshot when a shipped change moves the surface underneath it** — that
refresh is part of the sweep's definition of done, not an optional tidy-up. The
in-app copy and its `site/guides/<slug>.html` mirror must move together in the
same change (docs/32's copy-drift rule), and `tests/m89HelpPath.test.tsx`
fails when a referenced screenshot is missing from `frontend/public/help/`.

- **Sidebar accounts section**: between nav items and bottom controls, accounts fetched via `listAccounts(200)`, grouped by type (Cash / Credit / Registered / Non-registered) using `TYPE_GROUPS` — the constant now lives in `lib/accountGroups.ts` and is shared with the `/accounts` index, so the rail and the page can never disagree on groups or labels. Each account is a `<NavLink to={/accounts/${id}}>` showing name + `total_value` (cash + positions, in the account's native currency).

  **Section heading → `/accounts` (M100).** The `Accounts` heading is a `<NavLink to="/accounts" end>`: it is the index page's entry in the expanded rail, and `end` keeps its active pill off `/accounts/:id`, where the per-account row owns the highlight (two simultaneous `nav-active-pill`s would fight over the shared `layoutId`). The section renders even with **zero** accounts — that is when the index's empty state matters — so the rows alone are still gated on `accounts.data.items.length > 0`.
- **Collapsed sidebar (M100): wallet icon → `/accounts`.** Before M100 this control was `<NavLink to="/settings" title="N accounts">` from M21 (b7d03a7) onward — spec-blessed never-worked, and the reason Mark's ACCOUNTS click landed on Settings' Security card (docs/14 C7 carried the same wrong sentence). It now points at `/accounts`, renders whether or not any account exists, and carries the same active pill as every other rail row. Every collapsed control (nav rows, wallet, coins, notifications, log out, expand, theme) gets `aria-label` == the expanded row's visible label, a `min-h-11` (44px) floor, the `focus-visible` ring it already had, and **`RailTip`** (below) instead of a native `title`.

- **Tools sidebar group**: when `user.currency_tracker_enabled`, a "Tools" group renders below the nav with the `Currencies` link (`/currencies`) — the currency-tracker board (docs/15). In the collapsed rail it becomes a coins icon link to `/currencies`. Since M150 the page header also carries the **FX staleness chip** (§FX staleness chip) — the board is cache-only, so the chip is what says how old that cache is.

- **Viewport-portal surfaces.** Transaction editor, transfer-link modal, similar-entry
  popover, and account editor are `position: fixed` but portal to `document.body`,
  outside the transformed route-animation container. Their `z-50`/`z-[90]`
  surfaces therefore remain above the `z-40` mobile bottom navigation; lowering
  the nav alone would not escape that route stacking context.
- **Sidebar stacking context.** The sidebar `nav` is `sticky` + `backdrop-blur-xl`,
  which opens a stacking context — any `z-50` dropdown inside it (search menu,
  notification bell panel) is trapped there and loses to the `main` area's
  `.glass` cards (own `backdrop-filter` contexts, later in tree order), silently
  occluding taps. The `nav` therefore carries **`z-40`**: enough to lift the whole
  rail above the card layer, but not `z-50` — nav and `MobileNav` then tie at 40
  and tree order decides, with `MobileNav` rendered after the nav, so the bottom
  bar stays above the rail. Pinned by `tests/m68Navigation.test.tsx` ("AppShell
  stacking contract"). Verified by hit-test grid (`elementFromPoint`): 54%
  occlusion on the collapsed rail before, 0% after.
  **M101 (scroll performance):** below `768px` every `backdrop-filter` is off
  (§Scroll performance), so `.glass`'s own context no longer comes from a
  filter — the same block sets `isolation: isolate` on `.glass` and
  `.glass-strong` there, and the `nav`'s `z-40` context is unaffected either
  way (`z-index` alone opens it). At `≥768px` the filter is still there and
  this bullet reads exactly as written.
- **Mobile layout** (M35): below 768px the sidebar becomes an icon rail and `MobileNav` provides a fixed bottom tab bar; tables convert to card layout; KPI grids stack vertically; touch targets are at least 44px.
- **Underline tab strips wrap, never squeeze** (M82-F1): the `/reports` family strip (`Spending · Categories · Cash flow · Net worth · Performance · Dividends · Projections` — 7 labels ≈ 740px at 14px + `px-3.5`) must never shrink a button below its label. A flex row without `flex-wrap` shrinks the buttons instead, so the text node outgrows its box and prints into the neighbour (up to 30.4px of text-on-text overlap at 412) while the last tab lands beyond the viewport with `document.scrollWidth == innerWidth` — unreachable, not scrollable. Contract: container `flex flex-wrap gap-1 border-b`, buttons `shrink-0 whitespace-nowrap px-2 sm:px-3.5`; the row packs onto as many lines as the viewport needs (1 row ≥ ~1000px content, 2 rows at 412, 3 rows at ≤393) and the morphing `layoutId` underline keeps tracking the active tab across rows. Verified by the M82 re-measure at 320–412: 0 `kind=flow` text-node overlaps on `/reports`, every label's Range rect inside its button (`fitsInBtn=true`, measured with `br.right`/`br.left` — `DOMRect` has no `.r`, so the QA harness' `br.r` form reads `undefined` and can only ever return `false`), right-most glyph ≤ viewport width, page `scrollWidth == innerWidth`.
- **The `/rules` editor rows stack below `md`** (M82-F2): both rows in
  `Rules.tsx` are `grid grid-cols-1 … md:grid-cols-12` and every child spans a
  column only from `md:` up (`md:col-span-N`). A bare `grid-cols-12` with
  unprefixed spans kept the 12-col row live down to 320px, where a 2-col track
  is ~45px at a 412px viewport while the `Enabled` toggle alone needs ~98px:
  the row pushed the toggle and the Priority `?` past the right edge, and
  `main#main-content { overflow-x: clip }` swallowed them with no horizontal
  scrollbar to recover them (3 clipped nodes at 412/400/393/384/375/360/344/320,
  worse under Android font scaling — 25.1px lost at 130%). Below `md` each
  field is full-width; F2 itself left `md` up byte-identical to the previous
  one (verified at 768/1024/1280 × 16px/20.8px root font: identical toggle
  rects pre- and post-fix). Pinned by `tests/rules.test.ts` ("Rules form
  responsive contract (M82-F2)"), measured end-to-end by
  `frontend/scripts/local/rules_probe.py` against a local build through a
  static + `/api` proxy (`frontend/scripts/local/serve_proxy.py`): 48
  width × font × theme runs go 3 → 0 clipped nodes, `Enabled` ≥ 119.6px inside
  the viewport-minus-8 limit, `?` inside its parent in every run.

- **`/rules` parent-overflows that need 130% root font** (M82-F2 residual,
  same probe): `parentOverflow` counts children sticking out of their own
  parent — invisible to the viewport clip (`clipped=0` throughout) but real
  boxes sitting on top of their neighbour. Three were reported; two still
  needed code changes (`CardTitle`'s had already been fixed by M82-F3 in the
  meantime, so it only needed re-measuring):
  - **Row 3's prefill box wraps.** The box was `flex flex-1` (i.e.
    `flex: 1 1 0%`) with no `flex-wrap`, so the row's line breaker clamped it
    at its min-content — label + gap + the nowrap `Use as pattern` button,
    ~245px at 130% — which *was* its width and put it 53.9px past the row at
    320 (11.3px at 320/115%, 29.9px at 344/130%, 13.9px at 360/130%).
    `flex flex-1 flex-wrap items-end gap-2` drops the min-content to the
    widest single child (the button, ~142px), so the box fits the row and the
    button moves below the input when the two can no longer share a line.
    Desktop is untouched: at ≥768 both children still land on one line, so the
    label keeps exactly the width it had.
  - **`Enabled` owns a row from `md` up** (`md:col-span-12`, the same shape
    the `Stop here on match` toggle already uses). Row 1 is exactly 12 tracks
    (3 + 5 + 2 + 2), so the `Enabled` track is 55.4px at 768/130% while the
    pill measures 128px (40px switch + gap + the word) — 72.7px of overflow,
    and the pill needs 5 tracks (154px) where only 2 are free. This is the one
    desktop delta of the residual fix: at 768/1024/1280 only the `Enabled`
    cell and its pill move (cell `x` 632.7 → 304 and pill right 760.7 → 432 at
    768/130%, cell right edge unchanged); the grid template/gap and the
    Match type / Pattern / Priority / Stop cells and every `?` hint are
    byte-identical to `m82f2/out/combined_desktop.json`.
  - **`CardTitle`'s `right` slot** ("2 rules") no longer overflows at
    320/130% — the shared title row is `mb-3 flex flex-wrap
    items-center justify-between gap-3` (M82-F3, `components/ui/Card.tsx`), so
    the counter drops to its own line instead of pushing 17.7px past the card.
    Nothing needed changing here; the residual re-measured it at 0 across the
    whole matrix.

  Pinned by `tests/rules.test.ts` ("Rules form overflow residuals (M82-F2
  residual)" — both fixes fail on the pre-fix source), measured by
  `rules_probe.py`: `parentOver=0` at 320/344/360 × 18.4/20.8 and at
  768/1024/1280 × 16/18.4/20.8, `clipped=0` for all 48 runs of
  412…320 × 16/18.4/20.8 × dark/light. At Android's maximum font scale the
  same probe reports five more parent-overflows — see **Android 200% root
  font (M82-F2 200% residual)** below.

- `frontend/src/api/client.ts`: fetch wrapper attaching `Authorization: Bearer <access>`; on 401 attempts ONE `POST /auth/refresh`, replays the original request once, otherwise redirects to `/login` (token mechanics docs/01). Auth endpoints (`POST /auth/login`, `/auth/register`, `/auth/demo`, `/auth/refresh` — plus every login-adjacent route since M74: TOTP/recovery/passkey/OIDC, docs/38) skip the dance entirely (M47, docs/29 F5c): their 401 propagates to the caller so a wrong password (or bad second-factor code) renders the error panel — no redirect, no token clearing. Refresh itself is **single-flight**: `refreshTokens()` is the only path to `POST /auth/refresh`, holds one in-flight request per tab, and serialises the tabs of one browser behind the Web Lock `fin_tracker.auth.refresh` (see **Session refresh is single-flight** below) — two requests carrying one refresh cookie is a *reuse* signal to the backend, not a duplicate.
- Tokens live in memory only — the access token is never persisted; the refresh token is an httpOnly cookie the backend manages (docs/02 `refresh_tokens`).
- Vite dev proxy `"/api" -> http://localhost:8000` keeps dev and prod origin-identical (docs/01 CORS note is belt-and-braces).
- Theming & design system: dual-mode token system + chart palette + motion vocabulary — see the **Theming & design system** section below.

## Dashboard layout presets (M97)

The dashboard is ONE fixed layout that the user picks a preset for — the
smallest honest version of "dashboard flexibility", not an editor. Three
orders over the same six cards, chosen from a `SegmentedControl` in the
dashboard header (`aria-label="Dashboard layout"`), persisted server-side on
`users.dashboard_preset` (docs/02; migration `0036_dashboard_preset`) so the
choice roams across devices like every other preference:

| Preset | Order (first → last) | Intent |
| --- | --- | --- |
| `overview` — **first visit, default** | KPI strip → Spending insights → Budget progress → Subscriptions → charts (spending donut + net-worth trend) → Recent transactions | today's order, byte-for-byte: the G1 tour teaches on it, so first visit never changes what its marks point at |
| `import_first` | Recent transactions → KPI strip → Spending insights → Budget progress → Subscriptions → charts | what you just imported first (that card's empty state IS the import prompt), then what it did to the numbers |
| `budget_first` | Budget progress → Spending insights → KPI strip → Subscriptions → charts → Recent transactions | the yardstick first, then the spending summary it measures |

`CARD_ORDER` in `frontend/src/pages/Dashboard.tsx` is the single source of the
three orders; the picker persists with `PATCH /users/me {dashboard_preset}`
(docs/08).

**The rules a preset must not break** — each is a test in
`frontend/tests/m97DashboardPresets.test.tsx`:

- **It only reorders.** All six cards render in every preset: none is created,
  removed, hidden or configured, and every one stays in the DOM one scroll
  away. There is deliberately no per-card state to get out of sync.
- **DOM order IS visual order.** Reordering happens in the React tree, never
  with CSS `order` — an `order` reflow moves pixels without moving the
  accessibility tree, so reading order and drawn order would drift apart per
  preset. Below `sm` (640px) every grid is single-column, so a 390px screen
  draws exactly the DOM sequence: importance order preserved, not just the
  desktop grid.
- **First visit is Overview.** The column defaults to `overview`; a stored
  value the client does not recognise falls back to it instead of blanking the
  page.
- **Coach marks stay valid under all three.** The G1 tour's marks target
  `data-tour` SURFACES in AppShell (`nav`, `imports`, `reports`, `help`, plus
  the phone-only `nav-toggle` fallback) — never a dashboard card or a position
  in one — so no order can strand them.
  `frontend/tests/m68Navigation.test.tsx` walks Tour.tsx's own mark list
  against a rendered shell for all three presets, pair-wise (`target` +
  `fallback`) exactly as `Tour.tsx` resolves them.
- **The choice is server-owned.** A reload reads it back; the shared demo
  tenant cannot write it (the standing `403 demo_account_restricted`), so the
  picker rolls the order back and says why instead of pretending the save
  worked.
- **Spacing lives on the slot, one gap everywhere.** Every card slot rendered
  from `CARD_ORDER` carries the same `mt-4`, so KPI → spending insights →
  budgets are separated by one consistent gap in **every** preset — the three
  cards that ship no margin of their own (insights, budgets, subscriptions)
  used to butt against the card above whenever they were not the leading slot.
  The leading slot supplies the same single `mt-4` under the page header it
  always did, so Overview's first card is unchanged; the KPI strip and the
  charts grid keep their own internal `gap-4` (strip cohesion is the grid's,
  not the slot's) and no card stacks a second `mt-4` on top of its slot's.
  Pinned by `frontend/tests/dashboardSectionRhythm.test.tsx`.

**Deviation, recorded (AGENTS §3).** The M97 brief described Import-first as
an "import prompt/status card hero, recent-imports strip second". The
dashboard ships no such cards and building one is out of scope for this
milestone (*no new dashboard cards*), so Import-first maps onto the nearest
existing surfaces: **Recent transactions** — where every imported row lands,
and whose empty state is the import prompt ("Import your first statement to
get started") — with the KPI strip second and the rest in default order. If an
import-status card ever ships, it takes the hero slot and `import_first`
gains one entry.

**Rejected scope (documented, not silently omitted):** drag-and-drop editor,
widget hiding / minimisation, custom card sets, per-card sizing, card creation
or deletion. All three presets are data — `CARD_ORDER` — so adding a fourth
later is a table entry plus a control option, not a new subsystem.

## Owner dimension (M108 P3)

A `Group by` segmented control (`aria-label="Group by"`) sits in the Reports
header **only** on the Spending and Categories tabs, and carries `Category` |
`Tag` for EVERY session (M124 — tags need no partnership and no extra query
surface, docs/09 §The `Group by` control). Only the `Owner` option is gated:
it is appended **only** while `GET /partner/state` answers `{linked: true}`.
A tenant with no partner — and the OWNER seat, whose own `GET /partner/state`
answers `{linked: false}` because the link row is keyed on
`SharingLink.partner_user_id` (docs/43 §6), so the owner has no link OF THEIR
OWN — therefore sees no Owner option and no empty group, and the M93
row projection is never fetched (docs/12 §Owner dimension has the full
contract, the row table and the reason the income-side tabs are excluded).

- Choosing `Owner` re-renders the SAME chart (stacked bars, treemap or donut)
  over ONE household population and swaps the accessible summary title to
  *"Spending by category — own + shared household"*. The range-total caption
  changes to **"Range total (own + shared household)"** followed by a line
  naming the population — a number never changes what it is made of without
  saying so. The pre-M159 `Me` / `Partner` two-series split retired with the
  browser compute that drew it: the server's `scope=household` shape is a UNION
  and cannot be split (docs/12 §Owner dimension).
- **The lens covers the whole tab, not one panel (M179).** On Categories the
  Category × Tag matrix below the donut, and the category drill on either tab,
  are drawn from the SAME scope — before M179 the matrix disappeared under the
  lens and the drill was unavailable. A household block or donut slice opens
  that category's household periods; the drill then **withholds** the
  "View these transactions" link (it filters `/transactions`, which reads your
  own rows only) and says why, instead of silently showing a different
  population. The matrix's own CSV button carries the lens too.
- **The lens only ever reaches routes that declare it (M179 ruling).**
  The control renders on the two transaction-backed tabs only, and every request
  the page makes under it carries `scope=household` on exactly
  `/reports/spending`, `/reports/spending/categories` and
  `/reports/category-tag-matrix`. Of the remaining report GETs, ten explicitly
  accept only the defaulted `own` population; household, unknown and empty
  values are rejected by ordinary query validation, including implemented
  format twins (docs/08, docs/12). The Owner-lens EXPANSION added
  `own|household` to the other two — `/reports/insights` and
  `/reports/spending-comparison` — at the API level (docs/12 §Owner dimension):
  no control sends it, so no screen or report math changed. The UI does not
  send unsupported scope values anywhere.
- Every control option is ≥44px on a phone and keyboard-reachable through the
  shared `SegmentedControl` pattern.
- A saved view captures the lens as `state.group_by` (docs/02 §users).
- **Verification** — `frontend/tests/m108Reports.test.tsx`: lone tenant ⇒ no
  Owner option (the control stays, `Category` | `Tag`, M124) and zero
  household fetches; linked ⇒ the Owner option on Spending/Categories
  and absent on Cash flow; the household scope requested only once the lens is
  chosen; the household caption; the household drill carries `scope=household`
  and withholds the ledger link while the narrow drill keeps it; the Categories
  tab's donut AND matrix both request the household scope; revocation drops back
  to Category (and clears any open drill) instead of parking on a skeleton.

## Tag dimension, drill-down & the matrix (M124)

The Reports page groups by CATEGORY; M124 adds the two halves that were
missing — a **Tag** lens on the same charts, a **drill into one category's**
periods, and a **Category × Tag** cross-tab. The contracts live in docs/12
(§Tag dimension, §Category drill-down, §Category × Tag matrix); this section
is what the user SEES.

### The `Group by` control

`Category` (default) `|` `Tag`, and `|` `Owner` only while
`GET /partner/state` answers `{linked: true}` (docs/12 §Owner dimension) — the
control sits in the Reports header on the Spending and Categories tabs only.

- The lens is part of the query key, so switching Category↔Tag refetches
  rather than relabelling stale numbers, and a saved view restores it
  (`state.group_by` now accepts `tag`, docs/02 §users).
- Every chart takes a normalized `SpendSlice {bucket, key, label, total_cad}`
  plus a `dimension` label, so donut / treemap / stacked bars need no
  per-dimension branch: the summary title simply reads "Spending by tag".
- Under `Tag` a transaction with several tags counts in EACH of its tag
  buckets. The caption says so in words and names both figures —
  **"Sum of tag totals: $X across N data points — totals per tag; multi-tag
  transactions count in each, so this can exceed the $Y actually spent"**,
  where `$Y` is the response's once-counted `spend_total_cad`. The category
  lens keeps the plain `Range total: $X` caption, unchanged.
- `Untagged` is always its own block, on the same rule as `Uncategorized` —
  never merged away, never hidden (docs/09 §Hidden data).
- Tag blocks link to `/transactions?tag=<id>` (M104); the `Untagged` block has
  no such target and is static.

### Category drill-down

Clicking a donut slice, treemap block or stacked-bar segment in the **category**
lens replaces the tab's chart with that category's period breakdown.

- The chart is the M116 Bills-history family (`BillHistoryBarChart`), so the
  drill adds no chart component — only a `count`-less prop set and a
  parametrized empty message.
- The header is a real breadcrumb: a `← All categories` button (min-h-11,
  focus-visible ring), a separator, and the category name on
  `aria-current="page"`, with the range and its total on the line below.
- The ledger affordance is KEPT, not dropped: `View these transactions →`
  under the chart is the old `/transactions?category=` link, moved rather than
  removed — a drill that removed it would be a regression dressed as a feature.
- Leaving the tab clears the drill, so the breadcrumb's back can never point
  at a view that is no longer there. There is no drill under `Owner`
  ("Partner" has no category target — docs/12 §Owner dimension).
- **No extra round trip**: the drill is computed from the rows the page
  already holds (`reportEngine.ts::spendingReport` with a `categoryId`).

### Category × Tag matrix

A table on the **Categories** tab under the donut, answering "Groceries
tagged vacation?" without a second query.

- A real `<table>`: `<caption class="sr-only">`, `<th scope="col">` per tag
  column and `<th scope="row">` per category, so the two axes are announced as
  axes and not as a wall of numbers.
- The grid overflows horizontally at 390px, so it lives in a **focusable
  scroll region** (`role="region"`, `tabIndex={0}`, `aria-label`) — the
  docs/09 §Scroll regions rule, not an optional nicety.
- Cells are heatmap-tinted with the existing `--color-accent` token, capped at
  24% so `text-text` keeps its contrast pair, and the amount is ALWAYS printed
  — colour never carries the meaning alone.
- The caption states both honesty facts in one paragraph: rows are the
  transaction's own category (not the donut's roll-up), and cells count a
  transaction once per tag so a row can exceed the range total it sits under.
- `CSV` (a ≥44px button) exports the long-form rows through the same
  `format=csv` the API serves.
- Empty range renders `NoDataPanel`, never an empty grid.

### Verification

- `backend/tests/test_reports_tags.py` — the tag aggregation, the
  once-counted denominator, the Untagged bucket and the drill subtree against
  hand-computed vectors (the `m124TagEngine` client port and its vectors
  retired with M160; `backend/tests/test_m160_replay_s2a_matrix.py` replays
  the frozen `matrix-vectors.json` against the route).
- `frontend/tests/m124TagReports.test.tsx` — the control's presence per tab,
  the honesty caption's two figures, the breadcrumb round trip, the matrix
  table semantics and the Owner interaction.
- `backend/tests/test_reports_tags.py` — the wire parity of every shape above,
  including `format=csv` columns per dimension.

## Saved report views (M108 P2)

Two controls sit on their own row between the header and the tab strip, on
**every** Reports tab — `Save this view` and `Saved views` (`Saved views (N)`
when this tab has presets).

- **What a preset is.** `{id, title, tab, kind?, state}` stored in `users.saved_report_views`
  (docs/02 §users), so it is per-USER and roams across devices exactly like
  M97's `dashboard_preset`. `kind` (`reports` | `transactions`) picks WHICH
  `state` contract validates the row — `reports` (the original, default so
  every M108 row still reads unchanged) or `transactions` (M123, see §Create
  from search) — and it is always echoed on reads. A report preset's `state` is
  the tab's OWN filter fields only —
  the raw `from`/`to`, the `granularity`, the tab's variant (`view` on
  Spending, `year` on Dividends, `sort_key`/`sort_desc` on Performance).
  **The preset chips (YTD / 1M / …) are deliberately NOT stored**: they are
  derived from the range, and persisting derived UI would let a chip and a
  range drift apart after a rename.
- **Save** opens an inline disclosure panel with a title field (prefilled from
  the tab + range, 1–80 chars) and Save/Cancel. It is disabled while the range
  is invalid — there is nothing worth saving.
- **Saved views** opens the same style of panel listing only the presets whose
  `tab` is the one you are on (another tab's preset is not offered as if it
  applied here; an empty list says so in words). Clicking one restores tab +
  range + grain + variant/selector in one step and toasts "View restored".
- **Rename** (inline field, Escape cancels) and **Delete** are whole-list
  writes: `PATCH /users/me {saved_report_views: [...]}` — one write shape, no
  per-preset CRUD (docs/08).
- **Shape is a disclosure, not a portal popover.** The panels render inline on
  a `basis-full` row, so there is no positioning maths, no click-outside
  listener and no focus trap to get wrong, Tab walks the surface in DOM order,
  and nothing can be clipped at 390px. The triggers carry
  `aria-expanded`/`aria-controls` (docs/09 naming contract).
- **Touch targets** — both triggers and every list row are ≥44px
  (`min-h-11`) on a phone, with the shared `focus-visible` ring.
- **Demo tenant** — the standing write guard covers `PATCH /users/me`
  (docs/20), so `Save this view` answers `403 demo_account_restricted` and the
  toast explains it rather than silently dropping the preset.
- **Verification** — `frontend/tests/m108Reports.test.tsx` (save persists the
  raw state and NOT the chips; restore moves tab/range/grain/variant; rename
  and delete are whole-list writes; only this tab's presets are listed),
  `backend/tests/test_saved_report_views.py` (round-trip across sessions,
  closed `state` key set, duplicate ids / oversize list / unknown tab → 422,
  per-user isolation).

## Create from search (M123)

The Transactions page is the only surface that OWNS a search, so it is the only
one that can turn "what I am looking at" into something reusable. One control
sits on the row directly above the filter card (outside the filter `<form>` —
a title input in there would submit "Apply filters" on Enter):

```
[Create ▾]  [Views (N)]
```

### The criteria — one shape, four encodings

`lib/createFromSearch.ts` defines `SearchCriteria = {q?, category?, account?,
tag?}` — the four URL-addressable `/transactions` params, verbatim:

| key | value | validation (client AND server) |
|-----|-------|-------------------------------|
| `q` | the fuzzy search term (the `?q=` view, **not** the structured "Payee contains" filter) | non-blank after trim, ≤ 240 chars |
| `category` | a category id, or the literal `uncategorized` | `^\d+$` or `uncategorized` |
| `account` | an account id | `^\d+$` |
| `tag` | comma-separated tag ids ("matches ALL") | `^\d+(,\d+)*$` |

Absent key = "not filtered"; an **empty string is never encoded**, so the shape
round-trips with no sentinel. It is derived from `applied` (what the rows on
screen match) plus `searchQ` — never from `draft`, so the menu never offers a
search the screen is not in.

The same shape is written three ways:

1. **the live filter state** on Transactions (`applied` + `searchQ`);
2. **`?q=…&category=…&account=…&tag=…`** — the restore/bookmark URL, and also
   exactly what a `kind="transactions"` saved view stores as `state`;
3. **`?prefill=transactions&…`** — the deep link every target editor reads once
   on mount.

Because (2) and (3) are one codec, a saved view IS a bookmark, the ⌘K palette
can re-emit any of them as a link, and there is one parser rather than three.

**The address bar tracks the four criteria.** `Apply filters` and `Clear all`
write them back (`replace: true` — a filter change is a state edit, not a
history entry), so a reload shows what is on screen. The other bar fields
(`from`/`to`, `payee`, `amountMin`/`amountMax`, `direction`, `reviewed`) are
deliberately NOT URL state and NOT part of a saved view; the save panel names
them (`Not saved: date range, …`) instead of dropping them silently. The inbound
effect still only OVERWRITES params that are present, so an inbound
`/transactions?q=x` link does not wipe a filter you already had.

### C1 — Saved view

- Shape: `{id, title, tab: "transactions", kind: "transactions", state}` in
  `users.saved_report_views` — the same whole-list `PATCH /users/me` M108 uses,
  so rename and delete stay client-side list edits and no per-preset CRUD
  exists (docs/08).
- `state` is the four criteria verbatim; the closed key-set is validated
  server-side (`app/schemas/users.py::_validate_transaction_state`) — an
  unknown key, a bad id format, an empty value or `kind`/`tab` disagreeing is a
  `422 validation_error`.
- **Restore is a navigation**: `setSearchParams(criteriaToParams(criteria))`
  AND the filter state is set directly, because the inbound effect never clears
  an absent param — a view without `account` must clear it here or it would
  survive.
- The save panel's title defaults to the criteria as the user reads them
  (`“rogers” · Groceries · Chequing · tags wifi, household`), and its hint
  states exactly what is kept.
- `Views (N)` opens the rename/delete list showing the same rows, each described
  by its criteria. The panels are the SAME `components/ui/SavedViewPanels.tsx`
  the Reports header uses — one implementation of M108's list edit.

### C2 / C3 / C4 — the three creation targets

Each menu item is a `<Link>` to its existing editor carrying `prefill=transactions`
plus the criteria. The receiving page reads it **once** (a ref guard, not "on
every param change") so a later change can never clobber a live draft, and keeps
the params until the draft resolves — then drops them, exactly like M55's
`autofill` on `/bills`, so a reload restores an unsaved draft but a saved one
cannot come back.

| target | carries | cannot carry |
|--------|---------|--------------|
| **Rule** (`/rules`) | `q` → `pattern` with `match_type: "contains"`; `category` → the rule's **assignment** (`category_id`) | `account`, `tag` — no rule schema has such a column (docs/04) |
| **Bill** (`/bills`) | `q` → `match_mode: "contains"` + `match_terms: [q]`; `category` → the matcher's **scope**; `amount` → `None` (amounts are not search params) | `account`, `tag` (docs/25) |
| **Budget** (`/budgets`) | `category` → the budget's category, period `monthly` (a monthly budget IS the current month) | `q`, `account`, `tag` — a budget is a per-category amount |

`uncategorized` is a real transaction filter but no target can express it (a
rule/bill scopes or assigns a category **id**, a budget needs one), so it is
treated as not-carried rather than silently dropped.

Every target shows **`Created from search: <what landed>`** above its form
(`data-testid="prefill-provenance"`), plus — when filters were set that this
target cannot express — **`… <fields> not carried into a rule|bill|budget.`**
A prefill that quietly drops half the search is a lie the user only discovers
by re-running the search.

**Budget** is the only target with a hard precondition: without a real
`category` its menu item is `aria-disabled` (still focusable, so a screen-reader
user meets the reason) and prints `Set a category filter first` /
`Uncategorized cannot carry a budget` inline. Nothing is ever created by
landing: each target opens its existing editor with the fields filled and the
submit button still gated (a budget needs its amount; a bill its name/due date).

**The bill form gains a `Category (blank = any)` select** (the matcher's scope —
`bill_reminders.category_id` has existed since M40 but no form exposed it). It
is sent on create only when set, and on PATCH always (`null` clears, otherwise
`exclude_unset` would keep an old scope while the select showed blank).
`POST /bills/{id}/find-past` accepts `category_id` as one more optional
override for the same reason: an edit that moves the scope must be testable
before it is saved.

### ⌘K

- `Create from this search` is offered **only while `/transactions` is the open
  route** (so it can never be a dead click). It does not navigate: it fires
  `requestCreateFromSearch()`, which the on-page menu listens for — the live
  `applied` state may be newer than the URL.
- Every `kind="transactions"` saved view becomes a `View` group entry whose `to`
  is its bookmark URL. Both additions land AFTER the fixed registry, so the
  empty-query default (seven pages + Import) is unchanged.

### Menu ARIA

The Create trigger is a real menu: `aria-haspopup="menu"` + `aria-controls` +
`aria-expanded`, `role="menu"` with `role="menuitem"` children, roving focus
(first target focused on open), ArrowUp/ArrowDown/Home/End to move, Escape to
close and return focus to the trigger. The **Views** trigger is a plain
disclosure (`aria-expanded`/`aria-controls`, no `role="menu"`): its panel is a
list with rename/delete inline, and docs/09 forbids Escape-closing this page's
disclosures — Escape belongs to the menu only.

### Create from a selection (M174)

The same menu, with a different **criteria source**. M123 derives its four
targets from the live filter; a multi-row selection has no filter, so the
selection path derives them from the rows the user ticked. It renders in the
bulk toolbar the moment anything is selected, FIRST in the row beside the count
(so it is the first control on a phone, not something to scroll for), as a
`Create from selection` trigger opening a menu of two targets:

```
Create from selection ▾
  Rule from these 3      Categorize the payees these rows match
  Bill from these 3      A recurring bill for this payee
```

**Why rule + bill and not the search's four.** A selection carries PAYEES, not a
preset or a category total. "Saved view" records a filter and there is no filter
here; "Budget" is a per-category amount a set of rows cannot state. Offering
either would be a control whose click cannot mean anything.

**The criteria derive from the shared normalize, never from the first row**
(`lib/createFromSearch.ts::selectionCriteria`):

1. **Identity** is `lib/payeeKey.ts` — the byte-identical port of the server's
   `services/subscriptions.py::normalize_payee_key`, the same key M165/M170 use.
   The rows are one merchant family only if EVERY selected row collapses to ONE
   key. A selection spanning two merchants therefore has no single pattern: both
   targets are `aria-disabled` WITH the reason (`2 payees selected — pick rows
   from one merchant`), never a dead click and never an invented pattern.
2. **The pattern** is the longest common run of the family's payees (compared
   lower-cased, which is the relation `services/rules_engine.py::contains`
   itself applies). By construction it is a substring of every selected row, so
   it matches all of them on the rule engine AND on the bill matcher (which
   normalizes both sides through `normalize_payee_key`). The M165 fixture is the
   reason this exists: `GOOGLE *YouTubePremium` and `Google*Youtubepremium` are
   ONE merchant but neither string contains the other, so a pattern taken from
   the first row would silently miss its sibling — the manufactured duplicate
   M165 fixed one layer down, recreated in the editors. That pair yields
   `YouTubePremium`, which both spellings contain.
   The run is bounded to the server's `MAX_TERM_LENGTH` (120) — a prefix of a
   common run is still a common run — and a run shorter than 3 characters, or
   with no letter in it, is refused rather than carried.
3. **The category rides along only when every selected row agrees on one.** A
   unanimous category is the user saying "these are Groceries", which is exactly
   what a rule's assignment (`category_id`) and a bill's matcher scope mean; a
   mixed selection carries no category rather than an arbitrary one.
4. **Everything else is unchanged**: the same `?prefill=transactions&…` deep
   link, the same target editors, the same provenance line, the same
   read-once-then-drop discipline. One menu component
   (`components/transactions/CreateTargetMenu.tsx`) is shared with M123, so the
   ARIA contract above holds identically on both surfaces.

**The MATCHED path has no selection-derived menu**, deliberately: "every row the
filter matches" carries no rows to derive a payee from, and the page's own M123
`Create` menu already encodes the filter that defines that set. The control is
therefore present exactly while an explicit id set is selected (docs/09 §Bulk
selection toolbar, §Select-all-matched).

**Boundary — a payee that carries its own date is two merchants.** The shared
normalize strips ONE trailing `#?digits` run, not a date, so `… SUPERMARK
2025-12-31` and `… SUPERMARK 2026-10-04` are two keys and the selection is
refused with the reason above. That is the honest outcome under "identity is the
shared normalize": the pattern that would cover both (`… SUPERMARK 202`) also
matches a third merchant, and this feature never guesses a matcher.

### Touch points

`frontend/src/lib/createFromSearch.ts` (codec + provenance + handoff event +
`selectionCriteria`), `frontend/src/components/transactions/CreateFromSearch.tsx`,
`frontend/src/components/ui/SavedViewPanels.tsx`,
`frontend/src/components/reports/SavedViewsControl.tsx`,
`frontend/src/pages/{Transactions,Rules,Bills,Budgets}.tsx`,
`frontend/src/components/layout/{CommandPalette,AppShell}.tsx`,
`frontend/src/types/index.ts` (`SavedReportView.kind`, `SavedTransactionViewState`),
`backend/app/schemas/users.py`, `backend/app/schemas/bill_reminders.py`,
`backend/app/api/bill_reminders.py`. M174 adds
`frontend/src/components/transactions/{CreateTargetMenu,CreateFromSelection}.tsx`
and `frontend/src/components/transactions/BulkActionsToolbar.tsx` (the control's
host).

### Verification

- `backend/tests/test_saved_report_views.py` — transaction views round-trip the
  four params across sessions; `kind` is echoed; an unknown key, a bad id format,
  an empty value, `q > 240`, and every `kind`/`tab` mismatch → `422`; report
  presets still validate unchanged.
- `frontend/tests/m123CreateFromSearch.test.tsx` — one menu, four targets, the
  menu ARIA + keyboard, the disabled budget with its reason, the `?prefill=`
  payload per target, `kind="transactions"` save + restore + rename + delete,
  the palette's `View` entries and contextual action, and the empty-query
  default list unchanged.
- `frontend/tests/m108Reports.test.tsx` (20) — the extracted panels did not move
  the Reports control by a pixel or a label.
- `frontend/tests/m174CreateFromSelection.test.tsx` (16) — M174: the derivation
  (the M165 pair collapses to ONE pattern that is a `contains` hit on both
  spellings; two merchants and a date-suffixed payee both refuse; a unanimous
  category rides along and a mixed one does not; the run never exceeds 120
  chars), the toolbar control (menu ARIA + roving focus + Escape, the
  `?prefill=transactions&q=` payload on both targets, blocked-with-reason and
  never an anchor, absent on the MATCHED path), and the Settings list write.
- `backend/tests/test_m174_recurring_intent_tags.py` (20) — the tag source's own
  rules (ALL rows tagged, the empty set = off, two nominated tags on one row =
  one group, suppression wins, a bill and an accepted group each win the dedup,
  the cadence/occurrence floor and the money-OUT rule still apply), the calendar
  event + `confirmed_subscription_count`, the `PATCH /users/me` write seam
  (foreign id → `422 invalid_recurring_intent_tag_ids`, malformed → `422
  validation_error`, `[]` clears), and the cross-language half: the pattern the
  browser derives from the M165 pair is a real hit on `rule_matches` AND on
  `payee_matches`.
- `frontend/scripts/a11y/m174_parity_probe.py` (21 checks, `--base`) — the live
  half of M174, run against the deployed build at **390×844**: the bulk bar fits
  with no horizontal scroll (`scrollWidth == clientWidth == 350`, every control's
  right edge < 390) and `Create from selection` is the first control after the
  count; the menu is a real `role="menu"` with exactly two targets opening
  focused; both targets carry `?prefill=transactions` plus ONE `q` that is a
  case-insensitive substring of BOTH selected rows; `/rules` really prefills that
  pattern and prints the provenance line; `/bills` prefills the SAME pattern as
  the bill's `contains` match term, scoped by the unanimous category, amount left
  blank; Escape returns focus to the trigger;
  **bulk set-category driven for real at 390** (Recategorize → the select inside
  the viewport → Apply → both rows re-read with the new category through the
  API); the tag source measured on the calendar (count `0` → nominate a tag →
  `1` + a `subscription` event → clear the setting → `0`); and axe 0
  critical/serious with the menu open.
- `frontend/scripts/a11y/m123_create_probe.py` — 22 checks through a real
  browser against a running stack: `/transactions` → set filters → `Apply`
  (URL mirrors) → `Create ▾` (ARIA + focus + axe) → save → restore-by-navigation
  → each target lands prefilled with its provenance line. Reproduced against the
  deployed edge (`m123-create-probe-prod.json`); `axe_scan.py` = 0
  critical/serious over 6 pages × 2 themes (`m123-axe.json`).

## Interaction notes

- Suggestion queue supports accept/reject inline; accepts immediately improve categorization via the learning loop (docs/04).
- Import review screen highlights duplicates-skipped and unresolved rows distinctly.
- Tax outputs carry the informational disclaimer from docs/06 in both HTML and CSV exports.
- Reports graphs zero-fill missing buckets; empty ranges show a "No data" panel (docs/12).
- The unified **Search** launcher in the desktop top bar and mobile app bar opens the command palette; `⌘K` / `Ctrl+K` opens the same surface. Its debounced global record search hits the existing `GET /search` endpoint for queries of at least two characters, with limit 10 and the vault-aware blind-index/`hide_encrypted` contract in docs/33. Search re-reads live vault state after asynchronous token generation and immediately before the request; if state changed or is locked, it omits the token and forces `hide_encrypted=true`. A vault lock cancels and removes cached search results, and the endpoint excludes `payee_cipher` rows from both token and fuzzy matches when `hide_encrypted=true`. Results remain grouped and selectable: transactions open `/transactions?q=<payee>`, categories and accounts open the Transactions view with their corresponding filter, securities open their detail page, and tags open `/tags`. A locked-vault withheld-result hint appears only when `withheld > 0`; encrypted payees never appear in results. Palette navigation, report tabs, guides, saved views and actions remain available alongside record results. The Transactions page applies those URL params even when already mounted and since M123 writes them back on Apply/Clear (`replace: true`), so the address bar and saved-view bookmark stay aligned. The mobile drawer contains no additional search control; records remain reachable from its app-bar launcher.
- All money displays respect the row's native currency with CAD equivalents where relevant.

## Empty-state choreography (M95 P2)

Every surface that waits for data renders ONE three-part contract
(`components/ui/EmptyState.tsx`), and **the type system enforces it**: a call
site that omits `preview` or `action` does not compile (`tsc --noEmit` is the
sweep — it listed all 47 sites when the contract landed).

1. **What will live here** — `title` (+ optional `hint` body copy), rendered at
   the guides' hierarchy: `text-base font-semibold` for the lead,
   `text-sm text-muted` for the body (the same scale `/help` uses for step
   headings and blurbs). Deliberately NOT a heading element: 48 sites would
   drop `<h2>`/`<h3>` into arbitrary heading order across pages, and axe's
   `heading-order` is a per-page contract — a `<p>` at heading weight keeps the
   visual hierarchy without touching the landmark rules.
2. **What it looks like when real** — `preview`, a REAL product capture in the
   guides' figure style (`rounded-lg border border-veil/[0.08]`, lazy,
   uncropped), never an illustration. Testids: `empty-state-title`,
   `empty-state-preview`, `empty-state-action` on the root `empty-state`.
3. **The one action that fills it** — `action` is either `{to}` (a route that
   is the real entry — `Import a statement → /imports`, `New budget → /budgets`
   …) or `{onClick}` (the page's OWN create/clear/refresh handler: the actions
   are the controls that already existed, not new API paths). If the same label
   already exists as a control on that page, the action carries
   `ariaLabel: "<label> — <title>"` so accessible names stay unique (M92).

**A route action may never be a no-op.** Three surfaces are hosted on the very
route their action names, so they act in place instead of linking to
themselves: Investments' `Record a trade` scrolls to (and focuses) the
`#trade-form` form below the panel, its `Add a symbol` focuses the watchlist
picker, and the trades log's `Clear all filters` resets its three selects.
SecurityDetail's two price empty states trigger the page's shared
`refreshSecurity` mutation — the header button and both panels are one handler,
so "Refresh prices" really refreshes.

**A disabled action must say why (M141, the M140 G2 pattern).** When the one
action genuinely cannot deliver — a `dead_symbol` security whose "Refresh
prices" could never produce a price — the `action` carries `disabledReason`
instead of relying on `onClick` alone: the control renders DISABLED, the
reason renders beneath it, and `aria-describedby` binds the two so a screen
reader hears the reason with the button (testid
`empty-state-action-reason`). This is the empty-state half of the same rule
the VaultLocked gates encode: a control that cannot run is never presented as
if it will. SecurityDetail's dead-symbol state uses it for BOTH the price card
and the chart panel, and keeps a separate, plainly-labelled **`Try refresh`**
(`data-testid="try-refresh"`) as the one explicit path that can still probe
the row — an honest escape hatch, not a promise that prices will arrive.

## No public quote (M141) — the Watch-section honesty copy

The reported defect: adding a symbol with no public listing (APOTEX — private
since 2022) produced a SecurityDetail page reading **"No prices yet — nothing
has been fetched for this symbol *yet*"** over an enabled "Refresh prices"
button that could never succeed. Every surface that renders a security now
states the dead-symbol state instead, and none of them lies about waiting:

- **The add form** (`SecurityForm`, reached via the trade form's `+ New
  security` toggle) runs `GET /securities/quote-check` on submit. A
  `quote_found: false` answer raises a **non-blocking** warning
  (`data-testid="quote-warning"`) — *"No public quote found for `APOTEX` —
  $APOTEX.TO: No data found, symbol may be delisted Add anyway?"* (the
  `reason` is whatever Yahoo answered for the last candidate `probe_quote`
  tried; live on 2026-10-10 that is the `.TO` candidate, whose history-layer
  wording for the bare symbol is *"$APOTEX.TO: possibly delisted; no timezone
  found"*) — with an
  explicit **`Add anyway`** (`add-anyway`) and a **`Cancel`**
  (`dismiss-quote-warning`). It is never a hard block: a never-fetched symbol
  is legitimate to watch (a future listing, a cash equivalent). A
  `quote_found: null` (we could not ask) or a failed check adds silently — an
  answer we do not have is never reported as "no quote". Once confirmed, the
  same symbol is not asked about again. A symbol WITH a quote still adds in
  one click; the probe is invisible on the happy path.
- **The price card** shows *"No public quote for `APOTEX`"* with the verbatim
  verdict and *"…this symbol will not track unless a public listing appears"*,
  "Refresh prices" DISABLED with the reason, and `Try refresh` beneath it.
- **The chart panel** drops the enabled refresh action entirely and repeats
  the honest reason instead of the "backfills up to a year of daily closes"
  promise — which would be exactly as false there.
- **The watchlist row** says **"no public quote"** (danger badge, the reason
  on its `title`) rather than "no price", which reads as waiting.
- **The backfill poller stops**: `SecurityDetail`'s 5-second re-read while the
  cache is empty does not run for a dead symbol — it would re-read the same
  payload for three minutes.
- **"Refresh prices" on Investments** reports the skip: its toast names how
  many symbols had no public quote and were skipped, rather than implying
  every row was refreshed.

The state itself is server-derived (`security.dead_symbol`,
`dead_symbol_reason` on `SecurityRead`/`WatchlistItemRead`), so no surface can
render one truth while another renders the other.

**Preview sources** (`frontend/public/help/*.png`, served at `/help/<name>.png`,
every alt string written to match what the capture shows): `dashboard` ·
`transactions` · `imports` · `budgets` · `reports` · `rules` · `taxes` — the
M89 guide captures — plus **seven new real captures** taken for M95 from this
branch's own production build against the demo tenant at 1600×913:
`goals` · `bills` · `debts` · `investments` · `security-detail` (RY.TO) ·
`currencies` · `settings-accounts` (scrolled to the Accounts manager), plus
**`accounts`** — the `/accounts` index WITH data, captured the same way for
M100 (this branch's own production build, demo tenant, 1600×913), which is the
preview the accounts empty state shows. They carry the M95 sidebar (including
the palette row), and the standing QA sweep's re-capture rule (§Guide
screenshots) owns their refresh from here.

**Eleven documented exceptions** — `preview={{ none: "<reason>" }}`, title +
action still required, pinned as an exact set by `tests/m95EmptyStates.test.tsx`
so a twelfth cannot appear silently:

| Surface | Why no populated capture can exist |
| --- | --- |
| `Tags` | the demo tenant owns zero tags, so no capture of a populated tags list exists |
| `BillDetail` (no payments) | the state only exists when a match rule finds nothing — a capture would be this same copy on a page that otherwise always has rows |
| `Reconciliation` ×2 | a populated reconciliation needs an uploaded statement, which the demo tenant cannot create |
| `SharedHousehold` | the partner's populated view needs a second account, which the demo household cannot have |
| `HelpGuide` (not found) | a guide that does not exist has no real capture |
| `OidcLogin` (expired link) | an authentication hand-off status, not a data surface |
| `SuggestionQueue` | the demo tenant has zero pending suggestions |
| `SharingSection` | an active sharing link cannot exist on the demo tenant |
| `Retirement` (M188) | net worth ≤ 0 means there is nothing to simulate — inventing a positive portfolio would fabricate data (docs/34 §1, docs/57 §4) |
| `Assistant` (M188) | the History list's populated state needs an applied change, and no capture of one can exist on the demo tenant without fabricating the tenant's own data (docs/57 §6) |

**The locked-vault partial state REPLACES the empty state (M136, A18/F4).**
The three-part contract above describes an empty result. When the emptiness is
CAUSED by a locked vault withholding cipher rows, the surface must not render
the three-part `EmptyState` at all — it renders the shared `VaultLocked`
`partial` variant in its place (`components/e2ee/VaultLocked.tsx`), which
carries a MEASURED withheld count ("Showing X of Y rows — unlock to search the
other Z"), the canonical destination string, and an inline unlock whose
`vaultchange` genuinely restores the rows (docs/33 §Server-first computing
rules 7c/7d/7f). The count is read from the population the client scorer
actually saw (`substrate.data.items`, filtered on `payee_cipher != null`) —
never invented. A surface where the lock withholds NOTHING keeps the ordinary
three-part empty state and carries NO lock copy (rule 7a). The S5 search view
(`/transactions?q=`) is the shipped instance; before M136 it printed "No
transactions match these filters." with a "Clear all" action that deliberately
preserved `q` — a dead end by construction, now pinned as fixed by
`tests/lockedVaultKnownGaps.test.tsx` → "A18/F4 — FIXED".

**Server-side writes are gated on ENROLMENT, not on lock state (M181
follow-up).** `components/e2ee/ServerSideBlocked.tsx` is the second write gate
and is deliberately NOT `VaultLocked`: these surfaces are refused by the
SERVER for an `encryption_required` tenant whatever the browser holds (`trades`
has no cipher columns at all, and the statement rows are written by the
importer), so the notice must not offer unlock — a remedy that cannot restore
the feature is banned by docs/33 rule 7d — and the gate must not read lock
state at all. `useServerSideWriteBlocked()` is `users.encryption_required`
alone, so an unlocked-but-enrolled user sees the same reason, and an ordinary
user (locked or not) sees nothing (rule 7a).

The component renders `role="status"` with `data-testid="server-side-blocked"`
plus `data-server-side-capability="<capability>"`, a Lock icon and ONE
sentence that names the capability first and the reason second:
*"<capability> is unavailable while client-side encryption is enabled: this
path runs server-side and cannot safely write encrypted data."* The whole
sentence — phrase AND tail — is verbatim the matching endpoint's refusal
message (`_refuse_when_encrypted(...)`), minus only that message's trailing
"…so nothing was saved" — its separating comma closing as this sentence's
final period — because the gate renders before any submission: the
word "safely" is part of the endpoint's own copy and is kept (a dropped
"here" of it was security finding F3, 2026-11-07). It carries no unlock
control and no `VAULT_LOCK_DESTINATION`, and it lives outside `VaultLocked.tsx`
so that module's FOUR lock-state constants stay four (rule 7g).

Each entry REPLACES the control before submit (docs/33 rule 6) — never a
disabled-looking button that still posts, and never a post-hoc 422:

| Surface | Capability | Control replaced |
| --- | --- | --- |
| Imports — after the bank dry run | `import:statement` | **Confirm import of N rows** (`import-confirm-blocked`) |
| Imports — a staged batch opened from history | `import:statement-commit` | **Commit N new transactions** (`import-commit-blocked`) |
| Imports — a staged trade batch | `import:trades-commit` | **Commit N new trades** (`trade-commit-blocked`) |
| Imports — investment upload | `import:trades` | **Upload & parse** (`import-upload-blocked`) |
| Investments — trade form | `write:trade` | **Record trade** (`trade-form-blocked`) |
| Investments — dividend suggestion row | `write:dividend` | **Record** and the DRIP options that only feed it |
| SecurityDetail — record dividend | `write:dividend` | **Record dividend** (`record-dividend-blocked`) |
| Imports — YNAB migration card | `import:ynab` | **Import N transactions** (`ynab-import-blocked`) — was `VaultLocked`/G3, moved here by the F4 follow-up (PM option A); its cases stay in `tests/lockedVaultWriteGates.test.tsx`, which owns the card harness |

**What stays live, deliberately:** the zero-write previews
(`POST /imports/preview` and the mapping wizard — they persist no row), the
**Cancel** beside Confirm, **Discard** / **Revert** (removal only: with the
commit gone they are the only way to clear rows staged before enrolment), and
the suggestion's **Dismiss**. The form fields themselves are not hidden — the
reason sits where the submit control would be, from first paint, so the
refusal is read before anything is typed.

**Scope boundary.** The G1–G2 gates (`SplitEditor`, `BulkActionsToolbar`) are
unchanged and still keyed on `encryption_required && !unlocked`, because the
browser CAN supply the missing ciphers there; only a server-side write gets
the enrolment-only gate. `YnabMigrationCard` must NOT be read as part of that
justification: its commit runs SERVER-side, so "the browser can supply the
ciphers" never applied to it — by the F4 follow-up (PM option A, 2026-11-07)
it joined the table above as `import:ynab`, enrolled-and-UNLOCKED included.
There is no sync surface to gate yet (`frontend/src` holds zero `/sync`
references), so `SERVER_SIDE_BLOCK_COPY["sync:pull"]` is reserved for the
future sync status UI. Pinned both ways — including each gate still holding
with the vault UNLOCKED — by `tests/serverSideWriteGates.test.tsx` (all
eight capability strings, `sync:pull` included) and `tests/lockedVaultWriteGates.test.tsx` (G1/G2
lock-state gates plus the migrated G3 case).

**No fake loading.** A state that can never resolve to data must not show a
skeleton: the audit that swept every `enabled:` query found exactly one
viability defect — Investments' `DividendSuggestionsSection` was gated on the
tenant HAVING investment accounts and skeleton-ed forever for a tenant without
any (it now distinguishes "accounts still loading" from "no investment
accounts → hide", the same way it already hid when no suggestion was visible).
Gates that ARE honest while pending (an account id that has not resolved, a
tab's own range, a query behind an open disclosure) are unchanged.

**Verification (M95 P2, count current as of M188).** `tests/m95EmptyStates.test.tsx`
(6 tests): all 54 call sites carry `preview` + `action`; the ten `none`
exceptions are exactly the table above; every referenced capture exists on disk; Budgets-before-setup
renders all three parts with their testids, honest alt text, and an action that
really opens the create form; a deep-link action renders as a working
`<a href>`; zero console errors while rendering.

## Command palette — ⌘K / Ctrl+K (M95 P1)

One palette, one registry, **existing routes and existing global-search API** —
the route table remains the source for navigation/actions; record search reuses
`GET /search` (docs/08, docs/33) and adds no endpoint or server-side matcher.

**Triggers (one shared state).**
- `⌘K` (macOS) / `Ctrl+K` (elsewhere) anywhere in the shell — handled **inside
  the shell's single keydown listener** (see §Escape routing below), never a
  second `window` listener. Escape after shortcut-open returns focus to the
  visible Search launcher (falling back to the main page if no launcher is
  present); launcher-click open keeps the normal dialog opener restoration;
- one **Search** launcher in the desktop top bar and one in the fixed mobile app
  bar — the mobile control is `size-11` (44px touch floor),
  `aria-haspopup="dialog"`, and
  `aria-expanded` tracks the dialog state; its accessible name is Search (the
  icon-only mobile control retains the explicit "Open command palette" label);
- the mobile drawer has no second search field or palette launcher. Opening its
  app-bar Search control reaches both record search and all palette commands.

**Registry** (`components/layout/CommandPalette.tsx::paletteEntries`, priority
order): the seven navigate-to pages (Transactions, Budgets, Reports, Goals,
Settings, Help, **Accounts → `/accounts`** since M100) → the actions (Import a
statement → `/imports`, Take the tour → the tour's resurface event, Mint an MCP
token → `/help/agent-integration`, and since M123 **Create from this search** —
`/transactions` only, see below) → the `View` entries (every saved
`kind="transactions"` view → its bookmark URL, M123) → the seven report tabs →
every Help guide.
**An empty query shows the first 8 entries verbatim** — the seven pages plus
the Import action; M100 appended Accounts LAST among pages, so every pre-M100
entry keeps the rank it had and "Take the tour" now sits at #9. The M123 entries
are appended AFTER the fixed registry, so both are searchable but neither moves
that default.

- **Matching is plain case-insensitive prefix-then-substring** — label prefix,
  then label word-prefix, then label substring, then group/keyword haystack —
  deliberately with **no fuzzy/typo-tolerance dependency** (`budgetsx` matches
  nothing). In parallel, a two-character-or-longer query is sent to the
  existing global search API after a 200 ms debounce (limit 10); it is not a
  second matching implementation.
- **Record groups** are Transactions, Categories, Accounts, Securities and
  Tags. Stable ids are type-prefixed (`record-<group>-<id>`); each option's
  accessible name includes its record kind so grouped results remain
  distinguishable to assistive technology. Selecting a row preserves the
  former global-search destination. The server remains the source of grouped
  results, blind-index matching and fuzzy ranking.
- **Vault privacy:** `blindIndexTokenForQuery` supplies a token only while the
  vault is unlocked; locked requests set `hide_encrypted=true`. A positive
  `withheld` count renders the shared locked-vault search explanation in place
  of the empty-record state; when visible records already match, no empty-state
  hint is shown. No encrypted payee is rendered while locked.
- **Loading/error/empty:** record-search loading and rejected states are
  exposed as polite `role="status"` live regions without replacing local
  palette commands. A rejected search request (for example, HTTP 503) settles
  immediately to the visible `Search failed` state: this query sets
  `retry: false` rather than inheriting the app-wide React Query retry policy,
  so `Searching…` is removed and the request is issued only once. A true
  zero-result response says no matching records; an empty local command list
  retains `No match — Try Import or Help`.
- **Report jumps** land on `/reports?tab=<id>`; `?tab=` is now part of the
  Reports route contract: read on mount, followed when it changes in-page,
  written back with `replace` on every tab click (unknown value → falls back to
  Spending). The tab ids/labels live in `lib/reportTabs.ts`, shared by the page
  and the palette so the two cannot drift; the palette must not import the
  lazy page module into the shell's chunk.
- **Mint an MCP token** routes to `/help/agent-integration`. The card's
  "settings section anchor" does not exist — minting is an API action (docs/40)
  and Settings has no token surface; the guide is the entry point Help's third
  track already links to. Adding a Settings token panel would be a separate
  feature, not a palette wire-up.

**Dialog contract.** `role="dialog"` + `aria-modal="true"` +
`aria-labelledby`, focus moves in on open and returns to the invoking element
on close (`dialogA11y::useDialogFocus` — one implementation, every surface, no
keyboard trap), the input is a `role="combobox"` over a `role="listbox"` of
`role="option"` rows with `aria-activedescendant` tracking the active one;
arrows move, Enter goes, `Esc` closes **through the shell router** (the palette
registers no Escape listener of its own and is the TOP-most layer: one press
closes only the palette, the next reaches the inbox/search/drawer behind it).
Motion is opacity only — no slide — so `prefers-reduced-motion` resolves it to
instant (the reduced-motion contract in §Motion).

- **`role="option"` must not contain an interactive descendant (M95 axe fix).**
  The rows shipped wrapping their label in a real `<button>`; axe reported
  `nested-interactive` (impact **serious**) on all 8 default rows of the OPEN
  palette in the production build. The `<li role="option">` itself carries
  `onClick` / `onMouseMove` and the button's classes (`min-h-11` included), and
  nothing is lost: pointer users click the row, keyboard and screen-reader go
  through the combobox + `aria-activedescendant` path that never touched the
  button. Pinned two ways — `tests/m95CommandPalette.test.tsx` runs axe-core
  over the open palette and asserts zero violations **plus** that no option row
  contains `button/a/input/select/textarea`; the live browser scan is
  channel A.

**Chunk.** `AppShell` mounts it through `React.lazy` + `Suspense` **only while
open**, because the registry carries the whole guide-copy table and the search
panel: the entry
chunk keeps its ≤ 700,000 B ceiling (§Bundle facts) and a user who never presses
⌘K never pays for the guide copy.

**Verification (M95 P1).** `tests/m95CommandPalette.test.tsx` (16 tests):
registry is routes-only + top-8 order + prefix/substring/no-fuzz + all 7 report
tabs + guide jumps; open → type → Enter → route → close → focus HANDS OFF to
the routed page (§Keyboard contract, route focus handoff), with a separate
pin for the non-navigating close returning focus to the opener; arrow/Enter
navigation; `?tab=` jump; the tour action firing its event
and navigating nowhere; the no-results copy verbatim; a rejected global-search
request renders `Search failed`, removes `Searching…`, and makes exactly one
request under a retry-enabled QueryClient; ⌘K toggles (meta and
ctrl) and one Escape closes exactly one layer; press-1-closes-palette /
press-2-closes-drawer ordering; **no new `keydown` listener across open/close
cycles**; mobile trigger present at `size-11` with dialog semantics; rail row
when labels show; zero console errors across a full open/type/navigate/reopen
loop; **axe-core zero violations over the open palette** (the
`nested-interactive` pin above). `tests/m82ReportsTabStrip.test.tsx` pins the
`?tab=` deep link (honoured, unknown value falls back, click writes it back).

## Destructive actions — the 5-second undo (M95 P3)

**Context.** Three surfaces deleted forever behind a `window.confirm`: a
transaction row (Transactions and AccountDetail), an account (Settings →
Accounts), an import batch's revert (Imports → Batch history). One misclick was
unrecoverable, and the account delete never said what would go with it.

**Approach — two stages, and nothing is sent in stage 1.**

```
click Delete/Revert
  → stage 1 (immediate): row leaves the table, snackbar offers Undo — NO request
  → Undo during the window  : timer cancelled, row restored exactly, still no request
  → window closes / Dismiss : stage 2 — the real request fires
       success → refetch (server truth replaces the optimistic state)
       failure → row restored + the envelope `detail` surfaced VERBATIM
```

The seam is `src/components/ui/deferDelete.ts::deferDelete(toast, options)`:
`{title, detail?, hide(), restore(), commit(): Promise, onCommitted?(),
onError(message)}`. The snackbar API is `toast.undo({title, description?,
durationMs?, onCommit, onUndo})` on the existing `ToastProvider`
(`useToast()`), with `UNDO_WINDOW_MS = 5000` exported from
`src/components/ui/Toast.tsx`. A single settled-flag inside the toast makes
commit/undo **exactly-once** — the timer that would fire behind a pressed
button is cleared first.

**Timer ownership (pinned by `tests/toastTimerCleanup.test.tsx`):** every
deadline is armed through `window.setTimeout` — `AUTO_DISMISS_MS = 4200` for a
plain `success|error|info` toast, `UNDO_WINDOW_MS = 5000` for the undo window,
both exported from `src/components/ui/Toast.tsx` — and registered in the
provider's `armedTimers` set. A timer that fires removes its own entry,
`finish` (button or expiry) removes it *and* clears the handle, and the
provider's unmount effect sweeps whatever is genuinely still armed, so the set
stays bounded by the visible stack. This is not cosmetic bookkeeping: a
`setTimeout` outlives the React render that scheduled it, so an un-swept toast
timer fires into a torn-down jsdom `window` under vitest, react-dom throws
`ReferenceError: window is not defined` from `getCurrentEventPriority`, and the
runner reports an unhandled error and **exits 1 with every test passing** — a
red web gate with nothing to point at (~1 full-suite run in 9 under load). The
two halves are pinned: every armed handle is cancelled on unmount, and a spent
timer is not left behind for the sweep.

**Snackbar anatomy (pinned by test):** tone `info` (accent border, `Undo2`
icon), `role="status"` + `aria-live="polite"`, `data-testid="undo-snackbar"`;
copy is `Deleted <thing>` / `Reverted import batch` with the payee, account
name, or batch file name as the second line; two native `<button>`s —
`data-testid="undo-snackbar-undo"` labelled `Undo (Ns)` with N re-counted every
second from the real deadline (never above 5) and
`data-testid="undo-snackbar-dismiss"` (**Dismiss acknowledges: it closes the
snackbar and commits immediately** — the same end state as waiting, with no
hidden timer the user can't see). Buttons carry `min-h-11` below `md`: the
toast stack sits outside `main`, so the §Touch-target floor rule never reaches
it. The stack keeps five items (oldest dropped), same as every other toast.

**The staged surfaces and their stage-2 actions:**

| surface | stage 1 (`hide`) | stage 2 (`commit`) | on success |
| --- | --- | --- | --- |
| Transactions row / AccountDetail row | id into a `pendingDeletes` set, filtered out of the rendered `items` | `deleteTransaction(id)` | page-adjust (offset guard) + `invalidateLedgerData()` |
| Account (Settings → Accounts) | id into `pendingDeletes`, filtered from both the institution buckets and `ungrouped` | `deleteAccount(id)` | `invalidate()` (`["accounts"]`, `["institutions"]`) |
| Import batch (Batch history) | batch id into `pendingReverts`, filtered from `items` | `revertBatch(id)` | invalidate `["imports"]`, `["accounts"]`, `["transactions"]` |

**Account delete — the blast-radius dialog.** `window.confirm` is gone; the
trash button opens a `role="dialog"` confirm that spells out what goes with the
account **before anything is sent**. It loads live counts for the seven tables
`DELETE /accounts/{id}` pre-checks (docs/29 F3, docs/17):

| testid row | label shown | source |
| --- | --- | --- |
| `blast-transactions` | transactions | `listTransactions({accountId}, {limit:1})` → `total` |
| `blast-trades` | trades | `listTrades({account_id, limit:1})` → `total` |
| `blast-lots` | tax lots | `listAcbPools({account_id, limit:1})` → `total` |
| `blast-batches` | import batches | `listImports({account_id})` → `total` (the `account_id` paging param is new on the **client**; the server accepted it since docs/03) |
| `blast-debts` | debts | `listDebts()` filtered client-side by `account_id` |
| `blast-goals` | goals | `listGoals()` filtered client-side by `account_id` |
| `blast-reconciliation` | reconciliation items | `getReconciliation(accountId)` → `summary.total` |

Container `data-testid="delete-account-blast"`; loading state is
`data-testid="blast-loading"` (`Checking references…`). **Budgets are
deliberately absent** — they hang off categories, never off an account
(docs/02), so the "N transactions, N lots, M budgets" shorthand this card
asked to verify was wrong about budgets. Any count > 0 disables
`data-testid="confirm-delete-account"` and shows `data-testid="blast-blocked"`
quoting the server's refusal **verbatim**: `Account is in use; reassign
references first` (the 409 `account_in_use` detail) — that is exactly what the
request would answer. If the counts cannot be fetched, every value renders `?`
with `Couldn't check references here — the server will decide.` and the button
stays usable: unverifiable data must not block the user, the server still
refuses. A **raced** 409 (counts were zero but the server disagreed) takes the
stage-2 failure path: row restored, envelope detail surfaced verbatim.

**Portal rule (found by this card, live-verified).** `AnimatePresence` MUST NOT
wrap `createPortal(...)`: framer's `onlyElements()` keeps only
`React.isValidElement()` children, and portals are **not** elements — the child
is silently dropped and the dialog never renders. Both AccountsSection dialogs
were written that way, so Settings → Accounts → New account did nothing in the
running stack (checked with a headless click against the production build).
Dialogs use a plain conditional + `motion initial/animate` for the open (the
`LinkModal` idiom in Transactions.tsx if an exit transition is wanted — a
portal-returning component is a valid AnimatePresence child). This is now part
of the Dialogs bullet in §Keyboard contract.

**Out of scope, observed:** Imports' *institution* delete
(`pages/Imports.tsx`, `Delete this institution…`) still uses `window.confirm`
— it is a separate blast radius (the institution's accounts and rows) that this
card did not enumerate.

**Verification (M95 P3).** `tests/m95UndoDelete.test.tsx` (11 tests) covers all
three surfaces on the same three questions — no request while the window is
open, Undo during it restores order + count exactly, window expiry commits once
and the refetch reflects it — plus the 409 path per surface (row restored,
detail verbatim), the snackbar contract (`UNDO_WINDOW_MS === 5000`, polite
live region, focusable native button, countdown ≤ 5), the seven-row blast
radius with blocked copy and a disabled button, and the sibling fix (New
account opens the editor dialog, Escape closes it).

## Theming & design system

Dual-mode system ("midnight terminal" dark hero ↔ "daybreak" light). `ThemeProvider` (frontend/src/theme/ThemeProvider.tsx) owns the `dark` class on `<html>`, persists the choice to `localStorage` (`theme`), and defaults to saved → `prefers-color-scheme` → dark; framer-motion's `MotionConfig reducedMotion="user"` honors OS reduced-motion. Components consume ONLY semantic CSS variables — no hardcoded surface/ink colors.

### CSS tokens (frontend/src/index.css)
- Colors: `--color-surface`, `--color-surface-raised`, `--color-border`, `--color-text`, `--color-muted`, `--color-accent`, `--color-accent-soft`, `--color-accent-ink`, `--color-veil` (overlay base for chips/inputs/hovers), `--color-warn` (amber — warnings/momentum only), `--color-gain` (semantic green — gains only), `--color-danger`, `--color-overlay` (modal scrim base).
- Glass: `--glass-bg`, `--glass-bg-strong`, `--glass-border`, `--glass-border-hover`. The `.glass` / `.glass-strong` **blur** is applied at `≥768px` only — below md the tokens (and their alphas) stay, the `backdrop-filter` does not (§Scroll performance, M101).
- Shadows / glows: `--shadow-card`, `--shadow-glow`, `--shadow-glow-sm`, `--shadow-glow-danger`.
- Skeleton shimmer: `--skeleton-lo`, `--skeleton-hi`.
- Modal scrim: `--scrim`.
- Ambient login blobs: `--ambient-a`, `--ambient-b`, `--ambient-c`.

Each token has a dark (`:root`) and light (`:root:not(.dark)`) value; the light theme flips ink/veil polarity and swaps neon glows for soft shadows.

### Chart palette (frontend/src/components/charts/palette.ts)
`useChartPalette()` returns a mode-aware `ChartPalette` (ACCENT, ACCENT_SOFT, GAIN, LOSS, WARN, SERIES_COLORS[12], AXIS_TICK, GRID_STROKE, CURSOR_FILL/STROKE, LEGEND_COLOR, GRAD_TOP) with dark/light variants; every Recharts usage re-colors instantly on theme toggle. Tooltip styling lives in the shared `GlassTooltip`. Both palettes are exported as `CHART_PALETTES` for the CVD tests; charts still consume them through `useChartPalette()`.

- **Legend text renders in `LEGEND_COLOR`, never the series colour.** Recharts' `DefaultLegendContent` paints the label span with the series colour (`finalLabelStyle.color || entry.color`), and light series cannot clear 4.5:1 there — the audited "housing" label `#d97706` measures **2.92:1** on the card. `legendFormatter()` in `chartDefaults.ts` wraps each label in a span carrying `palette.LEGEND_COLOR`, so every chart that spreads `chrome.legend` (and `Donut.tsx`) inherits readable text; the swatch icon keeps the series colour so the colour→series mapping stays visible. `LEGEND_COLOR` is `#606978` light (**5.07:1** on `--color-surface`, 5.54:1 on `--color-surface-raised`) and `#8b95a7` dark (6.48:1 / 6.12:1); both are pinned in `tests/chartChrome.test.tsx`.
- **CVD-safe categorical slots.** `SERIES_COLORS` is checked under deuteranopia / protanopia / tritanopia (Machado et al. 2009, severity 1.0, applied to linear sRGB) with CIE76 ΔE in Lab — `tests/chartPaletteCvd.test.ts` pins, per simulation: the measured floor for the **first five slots**, the three M67 swaps each above its pre-swap worst case (dark slot 12 `#67e8f9 → #0ea5e9`, light slot 4 `#7c3aed → #6d28d9`, light slot 8 `#ea580c → #c2410c`), and a whole-palette floor of **2 ΔE76** so no two slots collapse to one colour. The audit's ≥ 25 ΔE target for the first five slots is *not* reachable inside the audited three-swap scope (measured 8.2 dark / 2.9 light worst-case; the light first five reach only 14.8 ΔE even in normal vision) — the limiting pairs sit in slots outside the audited set, so any move there must raise the floors in that test in the same commit.
- **Slot rules.** The 12 categorical slots are chart-only: semantic colours (GAIN/LOSS/WARN/ACCENT) and every non-chart surface stay on semantic tokens, and the cash-flow calendar kind dots use `bg-warn` (bill, amber) / `bg-gain` (dividend, green) / `bg-accent` (subscription, indigo in light, cyan in dark) — pinned by `tests/calendarPage.test.tsx`, never by a palette slot. Swap a slot only with a measured worst-case ΔE improvement; do not repurpose a semantic token for a series.
- **Money axis is compact and signed.** `useChartChrome().moneyAxis` is `formatAxisMoney()` — `$165K` / `-$55K` at ≥ $10K (compact notation, sign outside), `$5,000` / `-$55` / `$12.5` below (no forced cents), `$0` never `-0`. Chart axis ticks deliberately differ from `formatCadCompact()`, which keeps a fraction digit everywhere ("$165.0K", "$5,000.00") for value labels, not gridlines.

### Motion vocabulary (frontend/src/components/ui/motion.ts)
`EASE = [0.22, 1, 0.36, 1]` (ease-out-quart, never bounce/elastic); `SPRING`; `staggerContainer` (staggers cards in), `fadeUp` (fade + 12px rise), `routeFade` (route/tab swap), `splashPulse` (session-probe loop), `enterVariant`. All animations are transform/opacity only (compositor-friendly).

## Accessibility — WCAG 2.1 AA (M58)

Docs/09 already pinned three fragments of this (semantic tokens only, both
themes must pass the documented contrast pairs, `prefers-reduced-motion`);
M58 generalizes them into one auditable bar covering keyboard operation,
screen-reader output, contrast, and motion.

### Acceptance bar

Applies to the whole UI; it is *verified* on the six highest-traffic pages
(**Dashboard, Transactions, Investments, Taxes, Budgets, Bills**).

1. **Automated** — axe-core reports **0 critical and 0 serious** violations per
   page, per theme (12 page×theme runs), over the production build.
2. **Contrast** — every pair in the Contrast audit table below meets its ratio
   in **both** themes.
3. **Keyboard** — the six pages are operable with Tab / Shift+Tab / Enter /
   Space / Escape / arrows alone; every focus stop paints a visible focus
   indicator; every dialog closes on Escape and hands focus back to its opener;
   no keyboard trap.
4. **Screen reader** — every control has an accessible name, every chart has a
   text summary, every error and status change lands in a live region.
5. **Motion** — no animated surface runs under `prefers-reduced-motion: reduce`.
6. **Gates** stay green (AGENTS §4) and this doc is reconciled in the same commit.

### Audit method (three channels — all must be green)

| # | Channel | Harness | What it proves |
| --- | --- | --- | --- |
| A | Automated DOM scan | axe-core 4.13.0 injected into headless Chrome (`--headless=old`, AGENTS §5) against `npm run build` served by `npx vite preview` with `/api` proxied to a live backend; demo tenant; `axe.run(document, {resultTypes: ["violations"]})` with **zero rules disabled** | names/roles/ARIA validity, landmark + heading structure, `button-name`, `select-name`, `aria-prohibited-attr`, `color-contrast` on every opaque surface |
| B | Token contrast audit | `frontend/tests/contrastPairs.test.ts` parses the `:root` and `:root:not(.dark)` token blocks out of `src/index.css` and asserts every pair in the table below; `frontend/tests/focusRing.test.ts` (M66) walks the **class strings** in `src/` — focus rings, hover boundaries, banned text alphas — and composites them over surface / surface-raised / glass in both themes | both themes, canvas-free and deterministic — it covers exactly what channel A **cannot** see: axe returns `incomplete` whenever the background is glass or a gradient (48 nodes on `/settings` alone) and it cannot read `::placeholder` at all |
| C | Keyboard + motion walkthrough | `frontend/scripts/a11y/` (`a11y_driver.py`, `keywalk.py`): scripted Tab traversal over CDP — one full lap of the page, recording each focused element with its computed focus indicator and class string, Escape-closes-dialog checks plus the **observable-effect classification** of a trigger with no dialog to test (M103: navigation, inline form, live region, focus move, elements added — then and only then `no-effect`, a broken control), a renderer-idle measurement at rest, session-loss recovery, and the reduced-motion emulation pass; screenshots in `docs/assets/a11y/` | what no rule engine checks: focus order, focus visibility, focus restoration, no keyboard trap, whether a control *does* anything when clicked, renderer at rest |

Channels A and C run headless (`--headless=old`); the scripts and their JSON
output live in `frontend/scripts/a11y/`. The `DISABLED_RULES` allowlist in the
scan script is empty — adding a suppression is a visible, reviewed diff.

Channel C's `STOPS` press cap is sized from a measurement, never a guess:
`keywalk.json` records each page's `stops`, the `presses` that lap actually
spent (what the cap clears), how it ended (`lapEnd`) and the run's own
`stopsCap`; the `STOPS` comment in `keywalk.py` states the measured per-page
requirement and the `--stops <n> --out <file>` command that re-derives it. A
lap closes **only** when focus is observed returning to its first stop — the
DOM's focusable count is recorded for context but is never a stopping rule,
because a count taken before the page finishes mounting undercounts (it ended
`/transactions` and `/investments` mid-page — 638 counted against a 688-stop
lap — while reporting success). A page that exhausts the cap fails with
`uncycled: true` — partial coverage can never pass as a full lap.

**Channel C's keyboard input is itself under test.**
`frontend/scripts/a11y/press_regression.py` builds a throwaway `file://` page
(no stack, no login, no network) and prints the **three-way measurement**
behind `a11y_driver.press()` before asserting on it — `rawKeyDown`+`keyUp`
fires no default action (a focused button does not click on Enter, a form does
not submit), `keyDown` + text + an extra `char` fires it **twice**, and the
driver's own `press("Enter")`/`press("Space")` fire it exactly once. It also
re-measures Tab focus traversal every run, because the original bug report
claimed focus never moved and it does. Exit 0 only if all 16 checks pass;
evidence lands in `press-regression.json`. Without this harness a channel-C
walk built on a dead `press()` asserts nothing and can only ever report a
false PASS — which is how `press_enter()` came to exist inside
`m106_review_probe.py` (now removed; the shared path is what runs).

### Keyboard contract

- **Focus visibility.** `src/index.css` carries a global baseline in
  `@layer base`:
  ```css
  *:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
  .field-ring:focus-visible { outline: none; }   /* keeps its own ring + halo */
  ```
  Because it sits in the base layer, any element that already declares
  `outline-none focus-visible:ring-2 …` (Tailwind utilities layer) or
  `.field-ring` (components layer) keeps its existing indicator untouched —
  the baseline only lights up controls that previously had **none** (the shared
  `Button`, bare `<button>`, `<a>`, `<select>`…). Components must not remove an
  indicator without replacing it — and a replacement must be **full-strength**
  (M66): `focus-visible:ring-2 focus-visible:ring-accent`, no `/alpha`, because
  the painted ring itself is what pair 14 / WCAG 1.4.11 measure.
- **Landmarks + bypass.** `AppShell` renders the sidebar as
  `<nav aria-label="Main navigation">` and the page body as
  `<main id="main-content" tabIndex={-1}>`; a **skip link**
  (`Skip to main content`) is the first focus stop on every page, so the
  repeated rail is bypassable (WCAG 2.4.1) without walking it.
- **Route focus handoff (SPA navigation).** After every route change
  `AppShell` moves focus into `#main-content`, so a keyboard user who left the
  chrome through a primary tab, the `More` popover or the bottom tab bar lands
  in the routed page instead of still sitting on the control they just
  pressed (WCAG 2.4.3) — the handoff is what keeps the chrome from being
  walked twice. Two-phase on purpose: the pathname effect only CLOSES the
  shell's surfaces, and the handoff runs one commit later, after each surface
  has handed focus back to its opener — issuing it earlier would be undone by
  the `More` panel's restore, leaving focus on the `More` button. Excluded on
  purpose: the **first paint** (the skip link stays the first stop of a
  freshly loaded page), focus already inside `#main-content`, and an open
  modal (`[role="dialog"]` / `[aria-modal="true"]`, i.e. the transaction
  editor and the ⌘K palette keep focus). `focus({ preventScroll: true })` —
  the shell does not reset scroll on a route change, and focus management
  must not smuggle a scroll in with it. Pinned by
  `tests/ledgerShellFocus.test.tsx`.
- **Dialogs** (`role="dialog"` + `aria-modal="true"` + `aria-labelledby`)
  must: move focus in on open, close on Escape from anywhere in the document,
  and restore focus to the element that opened them. Implemented once in
  `src/components/ui/dialogA11y.ts` (`useDialogFocus`, `useEscapeToClose`,
  `focusables`) — never hand-roll it per site. Every surface dialog uses it:
  the Transactions link modal, `TransactionEditor`, `SimilarExamples`, the
  `AccountsSection` editor and delete-confirm (M95 P3) and the M95 P1 command
  palette; `a11yComponents.test.tsx` pins the focus-in / Escape /
  focus-back sequence.
- **Shell popovers** (`More`, notifications, the account menu) are
  **disclosures**, not dialogs: no `role="dialog"`, no `aria-modal`, no focus
  trap — and no `role="menu"` unless the panel really is one (required
  `menuitem` children plus roving arrow-key focus; the account menu dropped a
  borrowed `role="menu"` it never implemented, which is exactly what axe's
  `aria-required-children` reports, docs/47 §6). All three use
  `useDialogFocus`, so focus moves into the panel on open and returns to its
  trigger on Escape or an outside press — otherwise Escape inside the panel
  unmounts the focused control and drops focus on the body. None of them
  registers a `keydown` listener: Escape reaches them through the single
  router in §Escape routing. Triggers carry `aria-expanded` + `aria-controls`
  (the `More` button's shape) and drop `aria-haspopup` rather than promise a
  popup type the panel does not deliver. Pinned by
  `tests/ledgerShellFocus.test.tsx` (including a live axe run over the OPEN
  account menu).
- **Never wrap `createPortal(...)` in `<AnimatePresence>`.** framer's
  `onlyElements()` keeps only `React.isValidElement()` children, and a portal
  is not an element — the dialog is silently dropped and never renders
  (live-verified: Settings → Accounts did nothing until M95 P3 removed the
  wrapper). Use a plain conditional + `motion initial/animate` for the open, or
  a portal-returning component (the `LinkModal` idiom) when an exit transition
  is wanted. See §Destructive actions.
- **No keyboard traps.** Every focusable surface must advance with Tab.
- `autoFocus` is allowed only inside a dialog/popover (it *is* the focus move).
- **`⌘⇧H` / `Ctrl+Shift+H` — display masking** (M139, docs/46). Toggles the
  session mask. Handled inside `AppShell`'s **single existing** keydown
  listener, not a second one — the duplicate-listener bug class M72 already
  caught. Plain `⌘H`, a bare `h`, and `⌘K` (the palette) must all *not* fire
  it. After a keyboard toggle, focus returns to the chip so the next toggle is
  one keystroke away.

### Naming & semantics contract

- **Icon-only controls** carry `title` + `aria-label` naming the target
  (`Edit Mapleview Rent`, `Delete Budget — Groceries`) — the `Bills` pattern is
  canonical.
- **A control's accessible name is unique within its page** (M92). Settings
  mounts two schedule forms, so both primary buttons read "Save schedule" and
  a first-match click silently saved the wrong one. Both now carry a distinct
  name that *is* their visible text — "Save backup schedule" and "Save export
  schedule", `aria-label` identical to the label so WCAG 2.5.3 label-in-name
  holds. Any control that would duplicate a name already on the page must
  name its target (`Delete backup 2026-10`, never `Delete`).
- **`aria-label` never sits on a generic element.** `<span>`/`<div>` without a
  role may not carry it (axe `aria-prohibited-attr`). Announce a value by
  rendering it as visually-hidden text next to an `aria-hidden` visual — that
  is what `AnimatedNumber` does (`<span aria-hidden>$151.8K</span>` plus an
  `.sr-only` exact figure).
- **Every `<select>` / `<input>` / `<textarea>` is named**: implicit `<label>`
  wrapping (the `Field` helper), `aria-label`, or `aria-labelledby`. The
  `placeholder` attribute is *not* a label.
- **No internal jargon in copy (M68).** Spec pointers (`docs/03`), raw enum
  tokens (`pending_review`), the market-data vendor name (`yfinance`) and
  storage vocabulary (`bucket/category rows`) stay out of rendered text — the
  spec reference lives in comments, the enum renders its display label
  (`StatusBadge` → "Review"), the vendor renders as "Market data"
  (`sourceLabel` in `components/charts/format.ts`), and report summaries count
  *data points*. Pinned twice: DOM assertions over the real pages plus an AST
  scan of every JSX text node and string literal in `src/**` in
  `tests/jargonLeaks.test.tsx`.
- **Errors announce.** Every error surface carries `role="alert"` so it is
  *spoken* when it appears: `ErrorPanel` (failed loads), the form-level
  validation / API message in `TransactionEditor`, the inline `TagPicker`
  error, and the error panels on `Dashboard`, `Investments` and `Rules`.
  `Toast` maps `error → role="alert"`, `success|info → role="status"`.
  An `ErrorPanel` retry announces its own pending state too: while the request
  runs the `Try again` control is disabled, reads `Retrying…`, carries
  `aria-busy`, and a polite `role="status"` region says the same — so the
  "pressed, waiting" state is spoken and not only painted (docs/47 §3).
  Forms report at **form scope** — one alert under the fields, not one message
  per input — so no `aria-invalid` / `aria-describedby` wiring exists to drift;
  a field-level message added later must set `aria-invalid` on that input and
  point `aria-describedby` at the message's `id`.

### Chart summaries

Every chart renders an accessible summary — exact shape in **docs/12**
("Chart accessibility"). The wrapper carries `role="img"` and an `aria-label`
naming the chart, its series, its range and its endpoints; the Recharts `<svg>`
below it is `aria-hidden`, so assistive tech reads one sentence instead of
walking hundreds of `<path>` nodes.

### Contrast audit

**Method B** computes WCAG 2.1 relative-luminance ratios straight from the
token blocks (`rgb(var(--color-x))` triples and the alpha-composited control
states) for both themes. Thresholds: **4.5:1 for text** (WCAG 1.4.3),
**3:1 for non-text** (WCAG 1.4.11 — control boundaries and the focus
indicator).

| # | Pair | Where it shows | Min |
| --- | --- | --- | --- |
| 1 | `--color-text` on `--color-surface` | body copy | 4.5 |
| 2 | `--color-text` on `--color-surface-raised` | card copy | 4.5 |
| 3 | `--color-muted` on `--color-surface` | secondary copy, placeholders | 4.5 |
| 4 | `--color-muted` on `--color-surface-raised` | card secondary copy | 4.5 |
| 5 | `--color-accent` on `--color-surface` | links, ghost/soft action text | 4.5 |
| 6 | `--color-accent` on `--color-surface-raised` | links on cards | 4.5 |
| 7 | `--color-accent-ink` on `--color-accent` | primary button label | 4.5 |
| 8 | `--color-danger` on `--color-surface` | destructive text | 4.5 |
| 9 | `--color-warn` on `--color-surface` | warning text (incl. `bg-warn/10` chips) | 4.5 |
| 10 | `--color-gain` on `--color-surface` | positive/gain text | 4.5 |
| 11 | `--color-border-control` on `--color-surface` | form-control boundary | 3.0 |
| 12 | `--color-border-control` on `--color-surface-raised` | form-control boundary on cards | 3.0 |
| 13 | `hover:border-accent/70` composited on `--color-surface` | control hover state | 3.0 |
| 14 | `--color-accent` on `--color-surface` | focus indicator (2.4.7 / 1.4.11) | 3.0 |

Notes pinned with the table:

- **`--color-border` (card/hairline dividers) is decorative**, not a control
  boundary: cards, rows and separators are identified by fill, spacing and
  typography, and the hairline never carries meaning. Form controls use the
  separate `--color-border-control` token (pairs 11/12) precisely because there
  the border *is* the component boundary.
- **Focus indicators are painted at full strength (M66).** A control that
  replaces the global baseline paints its own with
  `outline-none focus-visible:ring-2 focus-visible:ring-<token>` at **full
  strength** — `ring-accent`, `ring-danger`, never `/<alpha>`: the 40% ring
  composites to **2.58:1 (dark) / 1.87:1 (light)** and `ring-danger/40` to
  2.00:1 / 2.04:1, under the 3:1 row 14 requires, even though axe accepts them
  (a ring *painted* is all channel A can see) and row 14 itself passes (it
  reads the token, not the class). Hover boundaries obey the same rule
  (`hover:border-accent/70`, row 13). `frontend/tests/focusRing.test.ts` walks
  every class string in `src/`, composites each focus ring and hover boundary
  over `surface`, `surface-raised` and glass in **both** themes, and fails with
  file:line — so this class cannot regress. It also enforces two source rules
  the token table cannot see: `outline-none` must never appear without a
  replacement indicator (the utilities layer beats the `@layer base`
  baseline, so an unreplaced `outline-none` paints nothing at all), and every
  `field-ring` control must carry the pair-11 `border-control` boundary.
- **Alpha-composited text is banned.** `text-muted/70` measured 3.89:1 (dark) /
  2.83:1 (light) — placeholders and micro-labels use the full-strength token.
  Only borders/fills may carry an alpha, and only where a pair above covers the
  composited result.
- **`--color-warn` is theme-scoped** like every other ink token. Its absence
  from `:root:not(.dark)` shipped amber-on-paper at 1.53:1 — the exact failure
  channel B exists to catch, since axe reads `bg-warn/[0.06]` glass and reports
  `incomplete`.

### Reduced-motion contract

Four independent layers, all mandatory:

1. `MotionConfig reducedMotion="user"` (framer-motion) — every `motion.*`
   variant collapses to opacity.
2. `src/index.css` `@media (prefers-reduced-motion: reduce)` — kills CSS
   keyframes, shimmer and `transition-duration`.
3. `AnimatedNumber`, `Toast` and `Donut` read `useReducedMotion()` and jump
   straight to the final state.
4. **Every other Recharts primitive spreads `{...chrome.anim}`**
   (`useChartChrome().anim = {isAnimationActive: !reduced}`), so bars, areas,
   lines and composed charts render their final geometry with no tween.
   A chart component that animates without spreading `chrome.anim` fails the
   audit.

### Touch points

- `frontend/src/index.css` — focus baseline, theme tokens (warn / danger /
  border-control).
- `frontend/src/components/ui/` — `AnimatedNumber`, `EmptyState` (`ErrorPanel`),
  `dialogA11y` (`useDialogFocus`, `useEscapeToClose`), `formStyles`; chart
  primitives under `components/charts/` and `components/reports/`.
- `frontend/src/components/layout/AppShell.tsx` — a11y attributes only
  (skip link, `nav`/`main` naming); no notification-surface behaviour changes.
- Pages: icon-only control names (Budgets, Goals, Debts…), `select` names
  (Investments trades log), micro-label colour (Settings, sidebar).
- `frontend/src/components/ui/dialogA11y.ts` — the shared dialog focus /
  Escape contract (M58b: focus now waits for a portal that attaches late).
- `frontend/src/index.css` — the global `*:focus-visible` baseline (M58b: it
  was invalid CSS, so it never applied).
- `frontend/src/components/e2ee/useSearchRows.ts` — M58b renderer-idle fix
  (stable empty rows; see the walkthrough section).
- `frontend/tests/contrastPairs.test.ts`, `frontend/tests/focusRing.test.ts`
  (M66: class-string focus-ring / hover-boundary / alpha-text sweep),
  `frontend/tests/a11yComponents.test.tsx`,
  `frontend/tests/transactionEditor.test.ts`,
  `frontend/tests/searchRowsRenderLoop.test.tsx`.
- `frontend/scripts/a11y/` — the audit harness (channels A and C) plus the
  committed JSON evidence (`baseline-pre-fix.json`, `axe-post-fix.json`,
  `keywalk.json`).

### Verification (M58 · M58b)

Runs (AGENTS §5 — headless Chrome `--headless=old`, demo tenant, production
build served by `npx vite preview --port 4173` with `/api` proxied to a live
backend):

```
cd frontend && npm run build
python3 scripts/a11y/axe_scan.py --out <file>            # channel A — 6 pages x 2 themes
python3 scripts/a11y/axe_scan.py --width 390 --height 915 --out <file>   # narrow width (M102: --width is
    # enforced with Emulation.setDeviceMetricsOverride and the report records `viewport` — headless
    # Chrome clamps --window-size below 500 CSS px, so --width alone used to lay the page out at 500)
python3 scripts/a11y/keywalk.py                          # channel C — tab order, focus, dialogs, motion
NODE_ENV=test npx vitest run tests/contrastPairs.test.ts tests/focusRing.test.ts tests/a11yComponents.test.tsx   # channel B + contract tests
```

**Channel A — baseline vs. current.** Committed JSON evidence:
`frontend/scripts/a11y/baseline-pre-fix.json` (first run of the harness, before
the fix pass) and `frontend/scripts/a11y/axe-post-fix.json` (re-run on the M58b
tree this doc ships with: 12/12 page x theme runs still 0 / 0 / 0 / 0, zero
rules disabled). The baseline was identical in both themes:

| page | critical | serious | rule -> nodes |
| --- | --- | --- | --- |
| Dashboard | 0 | 1 | `aria-prohibited-attr` -> 5 (`span[aria-label="$151.8K"]`, `AnimatedNumber`) |
| Transactions | 0 | 0 | — |
| Investments | 1 | 1 | `select-name` -> 3 (trades-log filters); `aria-prohibited-attr` -> 1 |
| Taxes | 0 | 0 | — |
| Budgets | 1 | 0 | `button-name` -> 6 (icon-only row actions) |
| Bills | 0 | 0 | — |

Totals: **4 critical + 4 serious** (8 of 12 runs affected). After the fix pass
the same 12 runs report **0 critical / 0 serious / 0 moderate / 0 minor**, zero
rules disabled.

**Channel B + contract tests.** `contrastPairs.test.ts` (32 assertions over the
14 pairs above, both theme blocks), `focusRing.test.ts` (M66: every focus ring
and hover boundary composited over three backdrops in both themes, plus the
banned-alpha-text sweep) and `a11yComponents.test.tsx` (dialog
focus-in / Escape / focus-back, chart `role="img"` summaries, error live
regions) are green in `npx vitest`.

**Channel C — full keyboard walkthrough of all six pages (M58b).**

`python3 scripts/a11y/keywalk.py` writes `frontend/scripts/a11y/keywalk.json`
(committed) and one focus screenshot per page into `docs/assets/a11y/`. Since
M58b the harness:

- Tab-walks until focus completes a **full lap** of the page (every visible
  focusable visited once) instead of a fixed 25 presses — the sidebar rail on
  its own is ~25 stops, so the old budget never reached a page's own content,
  which is where the audited controls live;
- measures the renderer **at rest** before walking (idle gate: >1.5 s of
  script in a 3 s window fails the page before it can wedge the walkthrough);
- detects a session that died into `/login`, re-signs-in and re-walks
  (`walkRetries` is reported, and a retry is a failure so a partial walk can
  never pass as evidence);
- allows 5 identical presses before declaring a keyboard trap, because Chrome's
  `<input type="date">` walks month/day/year with `document.activeElement`
  staying on the host (3 was a false positive on Dashboard and Transactions);
- records the class string of every stop, so a stop without an indicator points
  straight at the offending component;
- **classifies what a non-dialog trigger actually did (M103)** before it is
  allowed to call the control broken: navigation, an inline form, a live-region
  announcement, a **focus move**, or **elements added** are all
  `non-dialog-surface`; only when none of the six sees anything is the result
  `no-effect`. Both new checks read the DOM immediately after the
  click (a control's own effect is synchronous; anything landing seconds later
  is data arriving), and `m103_click_probe.py` measures the premise — a page
  left alone for 7 s moves **0** elements at 390 and 1440, while a genuinely
  inert button still comes back `no-effect`.

Run of the build this doc ships with (2026-10-05, M103) — **`failures=0`**,
zero console errors. The number is only as good as the channel behind it:
`Tab.console_errors()`
used to read `Runtime.consoleAPICalled` + `Runtime.exceptionThrown` alone, and
Chrome reports a CSP refusal on neither — so this row said 0 while the
deployed build was blocking **both** inline scripts on every load (QA F2, §Inline
scripts and the CSP). The driver now reads `Log.entryAdded`
(`source=security`) too, and `frontend/scripts/m72/csp_console_probe.py` makes
the channel's liveness checkable in one command: **6 errors / 3 loads** on the
pre-fix header, **0 / 3** once the hashes are pinned. The run below:

| page | stops | skip link first | stops w/o indicator | keyboard trap | at rest (script / 3 s) | dialog probe |
| --- | --- | --- | --- | --- | --- | --- |
| Dashboard | 73 | yes | 0 | none | 0.03 s | `non-dialog-surface` — "Create bill reminder" hands off to `/bills?autofill=` |
| Transactions | 385 | yes | 0 | none | 0.00 s | `pass` — focus moves in, Escape closes from anywhere, focus returns to the trigger |
| Investments | 159 | yes | 0 | none | 0.03 s | `non-dialog-surface` — "Add a symbol" moves focus into the watchlist combobox, which opens its `role="listbox"` (M103; reported `no-effect` before it) |
| Taxes | 53 | yes | 0 | none | 0.00 s | `no-dialog-on-page` |
| Budgets | 43 | yes | 0 | none | 0.00 s | `non-dialog-surface` — create form is inline (+3 visible controls) |
| Bills | 48 | yes | 0 | none | 0.00 s | `non-dialog-surface` — create form is inline (+4 visible controls) |

Evidence for that run: `frontend/scripts/a11y/keywalk.json` (committed) and,
for the M103 classification, `m103-click-probe.json` — the click → DOM delta
at **390×915** and **1440×900** (control window with no click: 0 changes;
click: focus `body` → the combobox, `aria-expanded` `false` → `true`,
`+29` elements / `+1` `role="listbox"`, visible form controls `+0`, which is
exactly what the old four checks could not see) plus the negative control: an
injected button with no handler still reports `no-effect`.

Screenshots: `docs/assets/a11y/{dashboard,transactions,investments,taxes,budgets,bills}-focus.png`,
plus `dialog-open.png` (TransactionEditor) and `reduced-motion-dashboard.png`.
With `prefers-reduced-motion: reduce` emulated, the reduced-motion pass reports
`total=0 running=0` on all six pages — the surfaces that animate are already
gated on the query.

### The three defects the walkthrough exposed (M58b fixes)

**1. The renderer wedge — why M58's channel C could only reach 2 of 6 pages.**
An identity bug in `frontend/src/components/e2ee/useSearchRows.ts`:
`query.data ?? []` allocated a fresh array on every render while the query is
*disabled* (its normal state — the query enables only when the search popover
opens), and `useResolvedTransactions` keys its resolve effect on `items`
identity and answers `setResolved(new Map())`. render -> effect -> setState ->
render, forever. Fixed with a module-constant `EMPTY_ROWS`;
`tests/searchRowsRenderLoop.test.tsx` pins the settled behaviour at the hook
boundary (bounded render count, stable row identity across re-renders).
Measured at rest on the fixed build: **0.00-0.03 s of script per 3 s** on all
six pages, against the pre-fix baseline (5-second window, production build):

| page | ScriptDuration / 5 s wall | LayoutCount |
| --- | --- | --- |
| Dashboard | 0.1 s | 34 |
| Budgets | 0.0 s | 0 |
| Transactions | 4.7 s | 0 |
| Investments | 4.6 s | 0 |
| Taxes | 4.7 s | 0 |
| Bills | 4.7 s | 0 |

Those four pages pinned their renderer at ~94-100% JavaScript with **no frame
ever committing** (`AppShell` rendered 618 -> 2,998 times in 3 s on
`/transactions` against 23 on `/dashboard`). Keyboard input queued behind it,
so Tab never returned and every later CDP call timed out. The wedge predates
M58 — it reproduced on the deployed build (`fintrac.princessandmark.com`,
d88c4e2) — and is now fixed.

**2. Focus never entered the transaction editor (WCAG 2.4.3).**
`TransactionEditor` mounted its portal through a `ModalPortal` that attached
`document.body` one commit late, so `dialogRef.current` was still `null` when
`useDialogFocus` ran; its deps `[ref, open]` never change again, so the single
`node?.focus()` silently did nothing and the dialog opened with focus still on
the page behind it (Escape worked, focus never went in). Fixed at both layers —
`ModalPortal` attaches synchronously, and `useDialogFocus` polls for the node
(32 ms, 2 s bound) so any surface with a deferred portal inherits the contract.
Pinned by `tests/transactionEditor.test.tsx` plus a late-portal case in
`tests/a11yComponents.test.tsx`, and proven end-to-end by the dialog probe above.

**3. The global focus baseline was dead CSS (WCAG 2.4.7).**
`index.css` declared `*:focus-visible { outline: 2px solid var(--color-accent) }`
while `--color-accent` is a bare triplet (`79 70 229`), which makes the whole
shorthand invalid — the declaration never applied. Every control relying on the
baseline instead of its own `focus-visible:ring-*` (all shared `Button`
variants, budgets/bills row actions, transaction account links, investments'
quote and "+ New security" controls) painted **no** indicator on focus. Now
`rgb(var(--color-accent))`; the harness measures `outline: solid 2px` on those
controls immediately after the keypress.

### Session refresh is single-flight — resolved: demo-session rotation race

*(Open item raised by the M58b first walkthrough; closed here.)*

**Symptom.** The demo session died 39 s after sign-in: every later
`POST /auth/refresh` answered 401, the app redirected to `/login`, and Taxes,
Budgets, Bills and Investments walked the login page instead of the feature.
`refresh_tokens` for user 1 on example.internal (`fin_tracker-db-1`), 2026-09-28 14:55
UTC:

| id | created_at | revoked_at | lived | successor |
|---|---|---|---|---|
| 1144 | 14:55:25.731 | 14:55:33.472 | 7.74 s | 1145 — normal rotation |
| 1145 | 14:55:33.481 | **14:55:33.591** | **110 ms** | **none** |

A token issued at .481 was revoked 110 ms later with no successor. That is
not expiry, it is `_revoke_all_refresh_tokens` (docs/29 reuse detection)
firing on a *second* presentation of 1144 — and the same handler clears the
requester's cookie, so nothing could rotate back into the session.

**Root cause: three callers, one dedupe.** Rotation is conditional by design:
`_claim_refresh_token` (docs/29) lets exactly one request spend a token and
treats the loser as reuse, so two `POST /auth/refresh` requests carrying the
SAME httpOnly cookie cannot both win — the loser ends the whole chain
(docs/02 `refresh_tokens`). The client reached that endpoint from three
places and only one of them deduped, and then only against itself:

| caller | path | deduped pre-fix |
|---|---|---|
| session probe on mount | `AuthContext.tsx` → `restoreSession()` → `refreshTokens()` | no |
| SSE reconnect (`onerror`) | `usePriceStream.ts` → `restoreSession()` | no |
| 401 replay | `client.ts` `withAuthRetry` → `pendingRefresh` | against itself only |

The exact pair that fired at 14:55:33 cannot be recovered: that traffic went
through the local vite preview proxy straight to uvicorn rather than nginx,
nginx logged nothing for `/auth/refresh`, and uvicorn runs without an access
log. The mechanism is not in doubt —
`frontend/scripts/auth/refresh_trace.py` attributes both non-deduped call
sites in the production bundle (the `AuthContext` `useEffect` frame and
`SSEStatusIndicator`'s `onerror` frame — never a fourth), and
`frontend/scripts/auth/session_stress.py` reproduces the row signature on
demand.

**Reproduction — `frontend/scripts/auth/session_stress.py`.** Two tabs sharing
one cookie jar, 60 s of lockstep page loads, plus a 5 s burst with the SSE
price stream blocked so `usePriceStream` calls `restoreSession()` directly
(`python3 session_stress.py --duration 60`; exit 0 means zero 401s *and* zero
rounds that ended on `/login`):

| | pre-fix `session_stress_pre_fix.json` | post-fix `session_stress.json` | deployed `session_stress_deployed.json` |
|---|---|---|---|
| target | prod build, `vite preview` | prod build, `vite preview` | example.internal stack through nginx |
| refresh POSTs | 21 | 22 | 23 |
| 200 / 401 | 2 / **19** | **22 / 0** | **22 / 0** (1 in flight at snapshot) |
| 429 (30/min budget, counted separately) | 0 | 0 | 0 |
| max refreshes in flight in one cookie jar | **2** | **1** | **1** |
| overlapping pairs | 5 (1–24 ms apart) | 0 | 0 |
| rounds that ended on `/login` | **19**, first at +0.7 s | **0** | **0** |
| verdict | FAIL (exit 1) | PASS (exit 0) | PASS (exit 0) |

Pre-fix, tab 1 was on `/login` 0.7 s into the run and never recovered;
post-fix all rotations returned 200 and no tab left its page — in the
container as well as in the local build.

**Side by side — the pre-fix run's network log against `refresh_tokens`**
(2026-09-28, both columns read after the run; `session_stress_pre_fix.json`
carries the wall-clock stamps, example.internal `fin_tracker-db-1` carries the rows):

| when (UTC) | in-page trace | `refresh_tokens`, user 1 |
|---|---|---|
| 16:54:54.818 → `55.170` | run starts; tab1 refreshes `.818 → .856` **200**, then tab0 `55.101 → .170` **200** — one cookie jar, rotating normally | 1283 `54.199` → revoked `.843` (tab1's rotation), 1284 `54.851` → revoked `55.120` (tab0's), **1285** created `55.144` as tab0's successor; 1282 (`53.967`) still live on a sibling branch |
| `55.146 → .208` | tab1's next POST starts at `55.146` — **45 ms** after tab0's, overlapping it by 24 ms (tab0's response was still in flight) — and comes back **401**: the run's first failure | 1285 created `55.144337`, revoked `55.162636` (18 ms) — the *same microsecond* as 1282's `revoked_at`, i.e. one `_revoke_all_refresh_tokens` swept the whole chain while tab0's 200 was still travelling — and **no successor row** |
| `55.2` → 16:55:49.015 | 18 more 401s: two per round (1–77 ms apart within a round, every ~6 s), because the cookie is now gone; every round lands on `/login` | nothing created again until `16:58:18` — the next run |

Identical signature to the incident: 1145 created `14:55:33.481`, revoked
`14:55:33.591`, no successor — two claims of one cookie, the whole chain
swept, the cookie cleared, session unrecoverable.

*Trap when re-running against the container:* the deployed env sets
`COOKIE_SECURE`, so `http://192.0.2.109:8080` (plain http to the container)
makes Chrome discard the refresh cookie on write — every refresh then 401s
and the run "fails" for a reason that has nothing to do with rotation (curl
shows the same: the `Secure` Set-Cookie never round-trips over http). The
deployed run above therefore tunnels to `localhost`, which browsers treat as
a trustworthy origin. The API side of that stack is verified independently:
demo sign-in → two sequential refreshes → `200, 200` through nginx.

**Fix — `frontend/src/api/client.ts`, backend deliberately untouched.**
`refreshTokens()` is now the single entry point to the endpoint:

- *per tab*: `pendingRefresh` was already set by the 401 path, and
  `restoreSession()` now joins it instead of starting its own POST;
- *across the tabs of one browser*: the Web Lock `fin_tracker.auth.refresh`
  (via `navigator.locks.request`) serialises the POSTs. Tabs share the cookie
  jar but not JS state, so a flag alone cannot see another tab's request; a
  tab that waits its turn then spends the cookie the previous response just
  set, which is a legitimate rotation rather than a second claim of a spent
  token;
- *degradation*: if Web Locks are unavailable (non-secure origin, older
  browser) or the lock itself errors, the refresh still runs (once, unlocked)
  — a browser-internal lock failure must never cost the session, and a
  refresh that already happened is never repeated. Note that this covers the
  *refresh*, not the cross-tab guarantee: see the last residual below.

The server keeps its reuse contract on purpose: it already answers this race
correctly (one winner, loser → cascade), and relaxing it to let the loser win
would mint two live tokens from one cookie. The concurrency is pinned on the
server too, by `test_concurrent_http_refresh_has_exactly_one_winner` in
`backend/tests/test_auth_security_hardening.py`.

**Verification.** `frontend/tests/refreshSingleFlight.test.ts` (8 cases:
all three callers sharing one POST, five-way concurrency, no reuse of a
settled refresh, the slot cleared on failure, the lock's
acquire → post → release order, and the three lock-absent / lock-fails-before
/ lock-fails-after paths) is red against the pre-fix `client.ts` on POST
counts and green after. Backend: two barrier-synchronised refreshes carrying
one cookie at the app — using the production per-request `get_db`, not
conftest's shared session — assert exactly one 200, the loser's
`invalid_or_revoked_refresh_token`, and at most one live token afterwards
(passed 3/3 runs). Frontend gates: `tsc --noEmit`, `eslint .`, `vitest run` —
40 files, 415 passed / 1 skipped.

**Residual risks (accepted).**
- Two *different browsers or people* holding one demo cookie can still race:
  no client-side lock spans devices, and the demo tenant is shared by
  construction (the reason this bug hurt).
- `POST /auth/refresh` is `@limiter.limit("30/minute")` per client IP; a 429
  counts as a failed refresh and logs the tab out. Two tabs spent 22 of that
  budget in the 60 s stress (0 × 429); `refresh_429` is reported separately
  precisely so a budget hit is never misread as the rotation race.
- The single-flight only merges *concurrent* callers: two tabs that refresh
  10 ms apart still each POST (sequentially, both 200), because the second
  one's request was already on its way.
- The M58b walkthrough harness still treats `/login` as a signal to
  re-sign-in and re-walk (it must, for a 429 or a genuinely expired
  session) — that recovery path is unchanged, it is simply no longer the
  normal path.
- *Cross-tab serialisation needs a secure context.* `navigator.locks` is a
  `[SecureContext]` API (Web Locks spec §3.1), so `withCrossTabLock` takes
  its unlocked branch whenever the page is not a secure context — measured on
  this stack at `http://192.0.2.109:8080` →
  `{isSecureContext: false, locks: "undefined"}`. On such an origin only
  per-tab `pendingRefresh` applies, so **two tabs loading at once can still
  present one cookie and reproduce the reuse cascade** — i.e. the original
  defect survives in exactly that deployment shape (same for browsers
  without Web Locks: Safari ≤ 15.3). The `COOKIE_SECURE` interaction cuts
  both ways: over plain http the `Secure` refresh cookie never round-trips,
  so every refresh 401s *before* any race can happen (the trap note above),
  which is what hides this today — and flipping `COOKIE_SECURE=false` to make
  that URL work removes that accidental shield while still leaving the origin
  insecure, exposing the race rather than fixing it. Accepted: run the app
  on a trustworthy origin (https, or http to a loopback host — what the
  deployed stress run does via the tunnel) so the cross-tab half of the fix
  exists; otherwise treat the two-tab race as still open.

## Horizontal table scroll (M64)

**Context — the M61 #1 friction finding.** Both mirrored transactions tables
(`/transactions`, `/accounts/:id`) render ~1113px / ~933px of columns inside a
**646px** box at a 1280×800 viewport: the page grid is
`lg:grid-cols-[1fr_20rem]`, so once the rail, the `p-8` padding and the 20rem
side panel are taken, the content column is that wide. The columns that
overflowed were exactly the ones the page exists for — the inline category
combo-box, the tag editor and Edit/Delete — and `overflow-x: auto` showed
**no scrollbar at all** in the audit environment (overlay scrollbars and
headless `--hide-scrollbars` paint no native scrollbar), so the controls were
reachable only by a blind horizontal drag.

**Approach — one wrapper owns the scroll.** `frontend/src/components/ui/TableScroller.tsx`
wraps the `<table>`; the pages no longer put `overflow-x-auto` on a bare div.

```tsx
<TableScroller label="Transactions table" className="mt-4 min-w-0">
  <table className="w-full text-sm">…</table>
</TableScroller>
```

Exact shape (pinned — a page must not re-derive it):

| Piece | Contract |
| --- | --- |
| Wrapper | `relative <page className>` (page owns margins + `min-w-0` for grid shrink) |
| Card | `glass overflow-hidden rounded-xl` — the scroll box sits inside it, so the bar can float |
| Scroll box | `table-scroll overflow-x-auto` + `role="region"` + `aria-label={label}` + `tabIndex={0}` — WCAG 2.1.1: a keyboard user can reach and scroll a region whose content overflows |
| Affordance | rendered **iff `scrollWidth − clientWidth > 0.5`**, inside `sticky bottom-[calc(57px_+_env(safe-area-inset-bottom)_+_1rem)] z-30 mt-2 flex items-center gap-3 rounded-xl border border-control bg-surface/95 px-3 py-1.5 shadow-glow-sm backdrop-blur-sm md:bottom-4` |
| Hint button | `Scroll for actions →` → scrolls to the end; `← Scroll back` once at the end (never a dead control). Smooth scroll, `auto` under `prefers-reduced-motion: reduce` |
| Scrollbar | `role="scrollbar"` `aria-orientation="horizontal"` `aria-controls={region id}` `aria-valuemin/max/now` (0–100) `aria-valuetext` `aria-label="{label} scrollbar"` `tabIndex={0}`; track `h-2.5` (**10px ≥ the 8px floor**) `bg-border`, thumb `bg-muted`; pointer drag on the thumb, click-to-jump on the track, keys `ArrowLeft/Right` ±48px, `PageUp/Down` ±0.9×clientWidth, `Home`/`End` |
| Native scrollbar | suppressed by `.table-scroll` in `src/index.css` (`scrollbar-width: none`, `::-webkit-scrollbar { height/width: 0; display: none }`) — exactly **one** scrollbar per box, and it is the custom one, so it renders in environments that hide native scrollbars. Wheel / trackpad / touch / Shift+wheel scrolling is untouched |

**Why the bar is sticky, not static.** 50 rows ≈ 2950px of table: a bar parked
under the card sits two screenfuls below the fold — where the audit never found
it. Riding the viewport bottom keeps the scrollbar + hint on screen for exactly
as long as the table is (sticky cannot leave its own wrapper, so nothing floats
when the table is off-screen), then it settles under the card.

**Why the bottom offset clears the nav (M82 F5).** Below `md` the fixed
`nav.mobile-bottom-nav` (`z-40`, opaque, `md:hidden`) owns the viewport bottom,
so a bar stuck at `bottom-4` rested *inside* the nav band: the nav painted over
the hint and `elementFromPoint` at its centre returned the `Txns` link. The bar
therefore sticks at `calc(57px + env(safe-area-inset-bottom) + 1rem)` — the
nav's `h-14` + 1px `border-t` + its safe-area padding, plus 1rem of air — and
`md:bottom-4` restores the desktop offset at exactly the width where the nav
disappears. Contract: while the hint is on screen its centre hit-tests to the
hint itself, and the hint's bottom stays above the nav's top, at every scroll
offset (measured at 412×915 and 360×800, scroll 0 / 0.5 / 0.9 / 1.0).

**Column order (M64).** The wrapper alone cannot make eight columns fit in
646px (the header row alone needs ~676px), so the workflow columns lead and
the reference columns trail:

- `/transactions`: **Date, Payee, Amount, Category**, Description, Account, Tags, Actions
- `/accounts/:id`: **Date, Payee, Amount, Category**, Description, Tags, Actions

The first four measure 615px on demo data → the scan + categorize columns and
their combo-box are fully inside the viewport at 1280px with nothing scrolled;
Description/Account/Tags (and the row actions) are behind the scroll, announced
by the hint. Rows are unchanged otherwise — same cells, same controls, same
focus indicators (M58) and the same `colSpan` empty/error rows.

**Touch points.** `src/components/ui/TableScroller.tsx` (new),
`src/index.css` (`.table-scroll`), `src/pages/Transactions.tsx`,
`src/pages/AccountDetail.tsx`, `tests/tableScroller.test.tsx`.
Column order is re-checked from source in that test, so a page cannot quietly
revert to `overflow-x-auto`.

**Verification (M64, production build on `vite preview` + live API, 1280×800).**

| Check | `/transactions` | `/accounts/74` |
| --- | --- | --- |
| Box vs content | clientW **646** / scrollW **1113** (maxScroll 467) | clientW **646** / scrollW **933** (maxScroll 287) |
| Native scrollbar drawn | **0px** (`scrollbar-width: none`) | 0px |
| Custom track | **10px** tall, `aria-valuenow=0`, `aria-controls` → region | same |
| Hint in the initial viewport | `Scroll for actions →` ✓ | ✓ |
| Core columns fully visible at rest | Date, Payee, Amount, Category (right edge 616 ≤ 646) | same (right edge 528 ≤ 646) |
| Tab from row 1 → Edit | reached (`activeElement` = `Edit transaction 3362`) | reached (`Edit transaction 3224`) |
| Hint click / `End` / `Home` | 0 → 467 → 0, label flips to `← Scroll back` | 0 → 287 → 0 |
| Console errors | 0 | 0 |

Honest note on the keyboard contract: the walk reaches Edit without any manual
drag, but the box *does* scroll while focus passes the trailing reference
control (the row's account link / the `+ Tag` select) — that is the browser's
focus-into-view, and those controls sit in columns that only exist beyond the
646px box. Reaching Edit with `scrollLeft` pinned at 0 would require the five
scan/action columns to fit in 646px, which they cannot with a 176px combo-box
in the row; the M58 contract itself (visible focus, no trap, reachable control)
holds — verified per page by the run above and by `tests/tableScroller.test.tsx`.

## Scroll regions — every `overflow-x-auto` box is focusable (M102)

**Context — the 8 serious violations M101's sweep was scoped out of fixing.**
M101's mandated mobile a11y pass found axe-core `scrollable-region-focusable`
(*"Scrollable region must have keyboard access"*, serious) at the narrow
widths only: **8 of the 24 page×theme×width runs** (4 at 390 + 4 at 412; 12
violating nodes — 1 on `/investments`, 2 on `/taxes` per combo) — byte-identical
before and after M101's CSS work, and 0 at 1440 because the content fits there.
All three were a bare
`<div className="… overflow-x-auto">` around a wide table inside its card:

| page | card heading (live DOM, 390×915) | source |
| --- | --- | --- |
| `/investments` | Trades log | `src/pages/Investments.tsx` |
| `/taxes` | Capital gains {year} | `src/pages/Taxes.tsx` |
| `/taxes` | ACB per holding (non-registered) | `src/pages/Taxes.tsx` |

A keyboard user can focus every *cell* but never the **box**, so nothing scrolls
it (WCAG 2.1.1); a screen reader meets an unlabelled, unnamed div.

**Approach — two tiers, one rule.** M64's `TableScroller` stays the answer for
tables whose overflowing columns carry the controls the page exists for; every
other horizontal scroll box gets the minimal shape inline.

| Tier | When | Shape |
| --- | --- | --- |
| `TableScroller` (M64, §Horizontal table scroll) | a table whose right-hand columns hold row controls | labelled + focusable region, custom 10px scrollbar, `Scroll for actions` hint |
| inline scroll region (M102) | a wide table/strip inside a card that already owns its chrome | `role="region"` + `aria-label` + `tabIndex={0}` on the scrolling element itself |

Exact shape (pinned — a page must not re-derive it):

```tsx
<div role="region" aria-label="Positions table" tabIndex={0} className="overflow-x-auto">
```

- `tabIndex={0}` is a **literal and unconditional**, exactly like
  `TableScroller`'s box: a box that fits today can overflow tomorrow (more
  rows, a longer payee, a narrower phone, 200% font scale), and a source-level
  contract cannot evaluate "overflows right now".
- `aria-label` is **required and must be unique within the page**: an unnamed
  `role="region"` is not a landmark at all, and two regions sharing a
  role+name trip axe's `landmark-unique` (moderate).
- Focus scrolling is native — a focused `overflow-x: auto` box pans with the
  arrow keys — and `*:focus-visible` (§Accessibility, `src/index.css`) paints
  the 2px accent outline when it takes focus.
- Sites whose scroll box **is** the card
  (`<Card className="overflow-x-auto">` in `DividendsPanel` /
  `ProjectionsPanel`) carry the same three attributes on the `Card`; `Card`
  spreads them onto its `motion.div` (pinned by a test).
- `SegmentedControl`'s strip already had `role="tablist"` + `aria-label` and
  keeps them; it gains `tabIndex={0}` so a segment row that does not fit is
  still scrollable (its tabs stay individually focusable).

**Enforcement — `frontend/tests/m102ScrollRegions.test.tsx` (4 tests).** It
parses every `.ts`/`.tsx` under `src/` with the TypeScript parser and fails
on any `overflow-x-auto` string literal that is not the `className` of a JSX
element carrying a non-negative literal `tabIndex` **and**
`aria-label`/`aria-labelledby` (or a `<TableScroller>`, which owns that shape
itself) — so a new page, a `clsx()` call, or a class hidden in a variable all
fail the same check, and a page cannot quietly reintroduce an unreachable box.

**Touch points.** `src/pages/{Investments,Taxes,SecurityDetail,Imports,Rules,Reports,HelpGuide,Reconciliation}.tsx`,
`src/components/rules/RuleDryRun.tsx`,
`src/components/reports/{TaxAlertsPanel,DividendCalendarPanel,DividendsPanel,ProjectionsPanel,ComparisonPanel}.tsx`,
`src/components/ui/SegmentedControl.tsx` (26 sites),
`tests/m102ScrollRegions.test.tsx`, `scripts/a11y/axe_scan.py`,
`scripts/a11y/m102_keyprobe.py` + `scripts/a11y/m102-axe-*.json` (evidence),
docs/09 (this section) + docs/10 (M102).

**Verification (M102 · production build served by `scripts/local/serve_proxy.py`
with `/api` → prod, demo tenant).**

| Check | Before (pre-M102 build) | After |
| --- | --- | --- |
| axe at **390×915**, 6 pages × 2 themes | **4 serious** runs, 6 nodes (`investments` ×2, `taxes` ×2) | **0 / 0 / 0 / 0** |
| axe at **412×915**, 6 pages × 2 themes | **4 serious** runs, 6 nodes (same) | **0 / 0 / 0 / 0** |
| axe at 1440×900, 6 pages × 2 themes | 0 | 0 |
| console errors | 0 | 0 |
| Keyboard, `/investments` "Trades log table" @390 | box never focusable | Tab reaches it (154 presses), `ArrowRight`×5 scrolls `0 → 200` of a 412px overflow |
| Keyboard, `/taxes` "Capital gains {year}" @390 | box never focusable | Tab (31 presses), arrows `0 → 200` of 253 |
| Keyboard, `/taxes` "ACB per holding" @390 | box never focusable | Tab (31 presses), arrows `0 → 122` of 122 (scrolled to end) |
| Scroll-region contract tests | `tests/tableScroller.test.tsx` only (M64) | + `tests/m102ScrollRegions.test.tsx` (4); full frontend suite **858 pass / 1 skipped** (82 files) |

**Committed evidence.** `scripts/a11y/m102-axe-{390,412,1440}-after.json` and
`scripts/a11y/m102-axe-{390,412}-before.json` (each records the real `viewport`
it ran at, its console errors and every violation), plus the keyboard probe
`scripts/a11y/m102_keyprobe.py` and its output `scripts/a11y/m102-keyprobe.json`
(the Tab/arrow rows below).

**Harness correction this card ships with (`scripts/a11y/axe_scan.py`).**
`--window-size` is **not** the viewport: headless Chrome clamps a window below
**500 CSS px**, so a run launched with `--width 390` (or 412, or 360) laid the
page out at 500 and never exercised the narrow-width rules. The scanner now
forces the target with `Emulation.setDeviceMetricsOverride` and records the
real `window.innerWidth` in the report (`viewport: [390, 915]` …). Every
before/after number above was re-run through that path. M58's channel A runs at
1440 are unaffected (above the clamp); any other harness that passes a width
below 500 as `--window-size` has the same clamp. That fix now lives on the
driver — `a11y_driver.Tab.force_viewport()` (re-applies the override on a live
document and returns the real `innerWidth`), which `axe_scan.py` calls
through. **The site harness was the one other instance and is fixed the same
way:** `site_axe_scan.py` used to pass `SITE_WIDTH` straight to `--window-size`,
so its committed `site-axe-results-360.json` (and `-390`) were 500 px runs
labelled 360/390. It now forces the same override, aborts before scanning if
the page will not honour the width, re-asserts and records `viewport` top level
and on every page entry, and refuses to scan a document that is not under
`SITE_BASE` (Chrome's `chrome-error://` page is `readyState: complete` too, so
`Tab.navigate()` alone cannot tell a served page from a failed load). All three
artifacts
were regenerated through that path — results unchanged (0 violations / 0
console errors, 17 pages × 2 themes), provenance now real; docs/32 §Page
contract carries the claim.

## Bulk selection toolbar (M96 B1)

**Context — misclassification arrives in volume.** Import-heavy ledgers get
categorized wrong in runs of dozens, and the honest (uncapped) `pct_used` a
category rollup prints is what makes the run visible. Fixing forty rows one
inline picker at a time was the top post-import friction on `/transactions`,
so the page gained a multi-select plus ONE toolbar that drives the routes
docs/08 already had — **no new bulk endpoint and no new write contract**: a
batch is the browser issuing the existing `PATCH`/`DELETE` per row.

**M125 keeps that contract and adds a second way to run it.** When the
selection is the whole **matched set** rather than the rows in front of you,
the toolbar issues ONE `POST /transactions/bulk/*` (docs/08 §Bulk over a
matched set) whose body carries the page's filter — not 345 ids, not 345
requests. §Select-all-matched below specifies it. The PAGE path (an explicit
id list, ≤ `PAGE_SIZE`) still drives the single-row M96 routes one row at a
time, unchanged.

### Selection

- **Checkbox column, first column**, in the header and in every row. The
  header checkbox is a **three-state cycle** (D1 under §Select-all-matched):
  tick 1 is **select-all-VISIBLE** (tick 3 clears), and tick 2 — offered only
  while the server's `total` for the filter is genuinely larger than the page
  — is **select-all-matched**. It reports `indeterminate` while the page is
  partly selected, and again while a matched set carries deselected rows.
- **`shift+click`** on a row checkbox ADDS the range between the anchor (the
  last plain click) and the clicked row, resolved in the current visible
  order; a shift-click never deselects, a plain click toggles and re-anchors.
  An anchor no longer on screen is ignored (plain toggle instead): a stale
  anchor may never select an arbitrary span. (While a MATCHED set is marked
  the gesture is its mirror: everything in that set is already selected, so
  the range marks rows to EXCLUDE — D3.)
- **Mobile**: checkbox taps only, each in a `min-h-11 min-w-11` (44px) wrapper
  that resets at `md`. The global M72 floor in `index.css` explicitly exempts
  `input[type=checkbox]`, so the hit area is declared here on the wrapper.
  (No long-press drag — this is the spec's checkbox-tap alternative.)
- **Lifetime = the view, split into two view keys (M125).** `filterKey =
  JSON.stringify(applied):searchQ` and `pageKey = offset`. A filter or
  search-mode change clears BOTH scopes; a page change clears only the
  PAGE-scoped one, because a matched set is defined by the filter rather than
  by what happens to be on screen — surviving pagination IS the feature. The
  row-stagger remount and unmount still clear everything. Nothing is stored,
  nothing rides in the URL, no multi-stage state exists. The page path's
  `intersectSelected(selection, visibleIds)` remains the only id list an
  action may touch — an id that left the view can never reach the API; the
  matched path transmits no ids at all (D4).

### Toolbar

One element, two placements: an inline header row above the table at `md+`,
and — below `md` — a bar fixed above the mobile bottom nav at
`bottom: calc(3.5rem + env(safe-area-inset-bottom))`, with `pb-20 md:pb-0` on
the column so it never covers the pagination row. It renders only while
something is selected; the count (`3 selected`) is an
`aria-live="polite" aria-atomic` region, so the change is announced without
hunting for it. Controls are the shared `Button` primitives (**Create from
selection** → Rule/Bill from the ticked rows (M174 — the FIRST control after the
count, so on a phone it is the first thing in the bar rather than something to
scroll for; §Create from a selection), Recategorize →
the shared create-in-place category combo + Apply (§Recategorize — create in
place, M176), Set payee → field + Apply, **Mark reviewed** and
**Mark unreviewed** (M106 — one `POST /transactions/{id}/review` per row, no
bulk schema), **Add tag** → tag select + Apply (M125 — the five actions of
docs/08's bulk set), Delete → confirm, Clear), which gives every one of them
the 44px mobile floor from `index.css` for free. On the matched path the
`n / total` readout is replaced by `Applying to 345 transactions…` — there is
one request, so a per-row counter would have nothing true to count.

**Mobile placement is the same control, not a reduced one (M174).** Below `md`
the bar is `fixed inset-x-0` with `flex-wrap`, so every control wraps onto more
lines inside the viewport: nothing needs horizontal scrolling and no action is
behind a hover, a long-press or a scroll region. Recategorize (`Set category`)
and Add tag (`Set tag`) are therefore reachable at 390 exactly as they are on
desktop, and the M174 create menu joins them at the top of the bar. The one
deliberate omission is the MATCHED path (above): its set is described by a
filter, and the page's own M123 `Create` menu carries that filter — so nothing
is hidden there, it is simply answered by the control that owns the criteria.

### Recategorize — create in place (M176)

**Context — the batch is where misclassification lands, and it could not mint
the category it needed.** With a bulk selection (Mark's screenshot: payee
`orange`, 126 rows ticked) the Recategorize control was a native `<select>`
whose options were the categories that ALREADY existed, while the single-row
category cell beside every transaction rendered `CategoryPicker` — a
type-to-filter combo that offers a `Create "…"` row. The one surface built for
correcting runs of rows was the one that could not name a new one.

**What changed.** The mode renders the SHARED `CategoryPicker`
(`components/transactions/CategoryPicker.tsx`) with the selected rows' scope
unchanged: choose an existing name, or type a new one and pick the explicit
`Create "…"` row (which `POST /categories` — the existing CRUD, **no second
category API**). Creating writes a CATEGORY and NOTHING else: no transaction
is touched until the separate, explicit **Apply**, which then runs the same
PAGE path (one `PATCH /transactions/{id}` per selected id) or MATCHED path
(one `POST /transactions/bulk/recategorize` over `{filter, exclude_ids}`) the
toolbar already had. Search, filter, selection and scope survive
create/cancel/failure.

**Immediate and honest.** The created category is merged over the `categories`
prop locally (deduped by id) so its name is the control's value the instant it
exists — before the `["categories"]` invalidation could refetch — and the
cache is refreshed for every other reader in the same click. A refused create
is SHOWN at the control (`role="alert"`, e.g. the server's `detail`), keeps the
selection, and is retryable by the same click; there is no silent catch. A
create can run once at a time (a second click is a no-op) and Apply is
unavailable without a valid id, including while a create is in flight. A name
that case/whitespace-equivalently matches an existing one
(`"  groceries "` ≈ `Groceries`) offers NO Create row — the duplicate check and
the POSTed value share one normalize — and typing in the payee field never
creates a category.

**Keyboard, touch, placement.** Option rows and the Create row are real
`<button type="button">`s whose activation is `click` (what Enter/Space and a
tap both produce) — the old `onMouseDown`-only handler made them mouse-
exclusive, so this fix is what makes creation reachable by keyboard and touch
at all. The menu opens UPWARD below `md` (`bottom-full`) where the bar is
pinned to the viewport bottom, and downward once the bar is the inline desktop
row (`md:top-full`) — a responsive class string, so the M176 placement flips
with the same breakpoint as the bar and is never clipped off-screen.

### Actions v1 — existing routes, sequenced

`components/transactions/bulkActions.ts::runSequential(ids, call, onProgress)`
issues ONE request per row, in order, and records every rejection:

```ts
interface BulkConflict { id: number; status: number | null; code: string | null; detail: string; label?: string }
interface BulkOutcome { okIds: number[]; okCount: number; attempted: number; conflicts: BulkConflict[] }
```

`okIds` is the page path's own record of what landed; `okCount` and
`attempted` exist because a MATCHED run answers into the same shape without
ever holding the ids (D4) — `okCount` is what landed, `attempted` is what the
run set out to touch (`okCount + conflicts.length` on a page run, the server's
`matched` on a matched run). `label` is the matched run's per-row name, for a
row the browser never loaded.

A `n / total` readout (`data-testid="bulk-progress"`) shows while the run is
in flight. Parallel was rejected deliberately: N authenticated requests at
once trades that indicator for `429`s on the one surface this feature exists
to hammer.

- **Recategorize** → `PATCH /transactions/{id}` `{category_id}` per row.
- **Set payee** → `PATCH /transactions/{id}` `{normalized_payee}` per row;
  with the vault unlocked it dual-writes `payee_cipher`/`payee_token` through
  the same `encryptTransaction` bundle the single-row editor uses (docs/33).
- **Delete** → `DELETE /transactions/{id}` per row, so each row keeps the
  single-delete blast radius byte-for-byte (docs/19): its `ReconciliationItem`,
  `TransactionTag` and `LlmSuggestion` rows go with it and the paired
  counterpart is unlinked by the FK.
- **Mark reviewed / Mark unreviewed** (M106) → `POST /transactions/{id}`
  `/review` per row. Because the route is idempotent, re-running a bulk
  review writes NO extra receipts — the log stays one line per real flip.

**Partial success is the reported outcome.** A refused row drops out of the
batch and the run continues: the toast prints `Deleted 7 of 9 — 2 skipped` and
a `role="status"` notice under the toolbar lists one line per skipped row —
payee, the server's own `detail`, and the envelope `code`
(`(transaction_in_pending_batch)`). Never silent, never a whole-batch failure.

**Rollup invalidation.** Every run invalidates `transactions`, `accounts`,
`ledger`, `reports`, `budgets`, `search`, `suggestions` **and
`transaction-events`** (M106: a flip writes a receipt, so the History drawer's
cache moves with it) — `pct_used` on
Budgets and the Dashboard cards and every report split read category rollups,
so a bulk recategorize that left them stale would be a lie the next page load
tells. The single-row inline picker got the same fix (it invalidated only
`transactions` + `suggestions` before).

### Touch points
- `frontend/src/components/transactions/useRowSelection.ts` — the two scopes,
  the three-state header cycle, and the range/toggle/select-all helpers.
- `frontend/src/components/transactions/bulkActions.ts` — `runSequential`,
  `outcomeFromResult`, `BulkConflict`/`BulkOutcome`, `summarizeOutcome`.
- `frontend/src/components/transactions/BulkActionsToolbar.tsx` — the bar, the
  five actions, both execution paths, the progress readout, the conflict
  notice.
- `frontend/src/pages/Transactions.tsx` — checkbox column, header checkbox,
  `filterKey`/`pageKey`, wiring, `invalidateLedgerData` + `budgets`.
- `frontend/src/pages/AccountDetail.tsx` — the same toolbar, the same
  selection column, `bulkFilter` pinning this page's account.
- `frontend/src/api/transactions.ts` — `buildBulkFilter` (the ONE
  `TransactionFilters` → wire mapping) and the five `bulk*` calls.
- `frontend/tests/bulkTransactions.test.tsx`,
  `frontend/tests/m125SelectAllMatched.test.tsx`.

### Verification
`frontend/tests/bulkTransactions.test.tsx` (17): selection lifecycle
(single / shift-range + re-anchor / select-all-visible / unmount-clears /
`filterKey`-clears / `pageKey` drops a page-scoped selection but keeps a
matched one), the three helper contracts, sequential DELETE proven by one
in-flight request at a time with the `0 / 3 → 1 / 3 → 2 / 3` readout,
partial-success notice text + the exact per-row conflict shape, `budgets` +
`reports` invalidation after a bulk recategorize, the rename payload, and the
two source contracts (both 44px checkbox wrappers, and the empty/error
`colSpan` spans — 9 at M96, **10** after M106 added the review column: the
same honesty assertion, retargeted).

`backend/tests/test_m125_bulk_matched.py` (17) is the server half:
filter-equivalence over ≥18 filter shapes against a MIXED three-page set
(the ids a bulk run moved ARE the ids `GET /transactions` would have paged
through, and nothing outside them moved), the `q_removed` and ownership-404
refusals matching GET's order, one request over a 121-row set, `exclude_ids`
subtracted (and past the documented cap refused), the count-aware delete gate
(`409 bulk_count_mismatch`, zero writes, exceptions honoured inside the
count), one receipt per row that actually moved, review/tag idempotency,
split-child `skipped`, **and the acceptance's own sequence**: read ONE page
of a mixed 121-row set, mutate the OTHER pages' rows through the matched path,
return — page 1 is byte-identical and the count still reads 121.

`frontend/tests/m125SelectAllMatched.test.tsx` (16): the three-state cycle
(page → matched → clear, with the SERVER total as the counter), the
exception's count adjustment + indeterminate header, the two refusal cases
(matched set that fits the page; a view the server never counts), the
`buildBulkFilter` wire mapping (including that an empty filter is an empty
body, not zeroed defaults), one request per matched action with the
single-row routes proven untouched, the count-first delete confirmation and
its `expected_count`, the `409 bulk_count_mismatch` keeping the selection
open with the server's sentence verbatim, and the account view's source
contract (toolbar mounted, `filter: bulkFilter`, 8 `colSpan`s, `select` in
`ACCOUNT_SPLIT_LAYOUT`).

`frontend/tests/bulkCategoryCreate.test.tsx` (10, M176): creating a category
from the bar shows the Create row, writes ONLY a category (no transaction
touched until Apply), keeps the new name as the control's value immediately,
invalidates `["categories"]`, and assigns on the explicit Apply — 3 page rows
via the single-row route, or ONE `bulkRecategorize` over `{filter,
exclude_ids}` with the count/exceptions unchanged; Cancel keeps the selection
and assigns nothing; a refused create shows the envelope `detail` at the
control, keeps the selection and retries; Apply is guarded while a create is
pending and a double submit is a no-op; a case/whitespace-equivalent existing
name offers no Create row; the menu opens upward below `md`; the Create control
is a keyboard/touch-activatable button and Enter-in-the-box does not create;
and an existing category still assigns through the shared picker.

Lint, `npx tsc --noEmit`, `npx eslint . --max-warnings 0` and the production
build stay green; browser evidence (both scroll positions, selection,
progress, partial-success notice, axe/console clean) is captured against the
deployed stack.

## Bulk over a matched set — Select-all-matched (M125)

**Context — the page could only act on the rows it had shown.** M96's
selection was deliberately client state: `intersectSelected(visibleIds)` is
the only set an action may touch, because an id that left the view can never
reach the API. That rule is what kept the feature honest, and it is also what
made it stop at `PAGE_SIZE`: with a month filter matching 345 rows, ticking
all five pages and pressing Delete meant 345 requests and five separate
confirmations. docs/08 §Transactions already carries the M104 **tag** and
**un-tag** operations (§Tags), and the actions the toolbar runs all exist as
`PATCH`/`DELETE`/`POST …/review` — so the missing piece was never a
per-row capability, it was a way to SAY which set, once.

The answer is the page's own filter. `GET /transactions` already takes every
filter as a discrete query param, so the same object becomes a JSON body, and
the matched set is re-derived **on the server, at execution time** — which is
also the only place the count can be trusted (the ledger moved under an
expensive client-side `total`).

### D1 — The header checkbox is a three-state cycle

| State | `checked` | `indeterminate` | `aria-label` | Click advances to |
| --- | --- | --- | --- | --- |
| Nothing / partial page | off | off / on | `Select every transaction on this page` | **page selected** (all visible ids) |
| Whole page, server `total` **>** page | on | off | `Select all 345 matched transactions` | **matched** |
| Whole page, no larger set | on | off | `Clear selection` | cleared |
| **Matched**, no exceptions | on | off | `Clear selection — 345 matched transactions selected` | cleared |
| **Matched**, with exceptions | off | on | `Select all 345 matched transactions` | matched, exceptions dropped |

- The label **names the next state**, so a screen reader is told that a second
  tick reaches 345 rows instead of only hearing "checked"; `title` carries the
  same sentence for a mouse.
- **No second tick where there is nothing to reach.** When the server's `total`
  equals (or is below) the page, or when `matchedTotal === null`, the cycle is
  page → clear — M96's behaviour exactly. `matchedTotal` is
  `page.data.total`, and `null` in the fuzzy-search view, because that view
  scores in the browser (docs/33 S5) and has no server `total` to select.
- `indeterminate` is set in JS (`element.indeterminate`): no attribute can
  express it.
- A visible **`Select all 345 matched`** button sits in the toolbar while the
  page is fully selected and the filter reaches further — the same state,
  a second door (Gmail's pattern).

### D2 — Matched survives pagination; a filter change still clears it

`filterKey = JSON.stringify(applied):searchQ`, `pageKey = offset`. Page-scoped
selection dies with its page (M96's rule — and the reason no id can leave the
view); the matched set survives it, because paging says nothing about what the
filter matches. Selecting 345 rows and then paging to check the next 50 rows
is the whole reason the state exists.

### D3 — Deselecting out of a matched set is an EXCEPTION, not a collapse

`selected` (page) and `excluded` (matched) are separate state. In matched mode
`isSelected(id) = !excluded.has(id)`, so the count is
`matchedTotal - excluded.size` — the SERVER's total minus what the user
unchecked — never a client sum of a set the client never enumerated.
`shift+click` on a matched set marks a VISIBLE RANGE to exclude (everything in
the set is already selected, so "add" has nothing left to add); on a page it
still adds, as M96 wrote it. The group checkbox (`toggleMany`) subtracts a
split group's children the same way.

### D4 — The body carries the FILTER, not the ids

`buildBulkFilter` is derived from the SAME `filterEntries()` mapping
`buildQuery` uses (one function, two routes), so a filter the page builds can
never mean two different things depending on whether it travels as a query
string or as JSON. The request is:

```jsonc
{ "filter": { …the page's filters… },
  "exclude_ids": [41, 42],      // what the user unchecked — never the matched ids
  "category_id": 12 }           // action-specific fields, extra=forbid
```

A 345-row run therefore costs ONE authenticated request — the entire point of
the path — and an id that left the view still cannot reach the API, because no
ids are sent at all. `_build_txn_filters()` is the single `TxnFilters`
constructor both `GET /transactions` and the bulk routes call, so drift
between what the page rendered and what a run touches is a build error, not a
surprise.

`POST /transactions/bulk/*` is declared **before** `POST /transactions/{id}/…`
in the router: Starlette matches in declaration order and `bulk` is not an
int, so a later declaration would 422 instead of reaching the route.

### D5 — Count-first, and the count travels with the request

Delete confirms `Delete 345 transactions? This cannot be undone via this
batch.` — the exact count, and an explicit statement that a BATCH has no undo
(the M95 per-row undo toast never covered a run). The same number goes to the
server as `expected_count`, which must equal the current `matched` or the run
is refused `409 bulk_count_mismatch` with **zero writes**. The selection is
kept open on that refusal so the new count can simply be confirmed again; the
`okCount`-based pagination fix runs only after a run that actually landed.

### D6 — Partial success, reported exactly as the page path does

`BulkResult {matched, updated, skipped: [{id, label, code, detail}]}` maps
through `outcomeFromResult` into the same `BulkOutcome` the page path builds,
so the toast, the `role="status"` conflict notice and the pagination fix never
learn which path ran: `Deleted 340 of 345 — 5 skipped`, one line per row, the
server's `detail` and its envelope `code`. `label` exists because a matched
set can skip a row on page 7 that the browser never loaded, and `#4711` is not
a name a person can act on.

### D7 — The account view gets the same toolbar, with ITS account id

`/accounts/{id}` (the per-account ledger) mounts the same selection column,
the same header cycle and the same toolbar. Its bulk body is
`bulkFilter = { ...filters, accountId: id }` — the list query's filter, account
id included — so "apply an account filter → select all matched → bulk applies"
can only ever reach that account's rows. The `select` column is prepended to
`ACCOUNT_SPLIT_LAYOUT` and every empty/error span moves `colSpan={7}` →
`colSpan={8}` (3 spans), so an unmatched sub-state never overflows its own
grid — the same honesty assertion M64 and M106 retargeted.

## Bills you probably have (M138)

**Context — one substrate, two complaints.** M133's dashboard review found the
subscriptions card fed by the client-side M27 detector (`s3Engine.ts`, "2 or
more charges", average-gap bucketing, **no amount band at all**), listing
FILM.CA ×2, LOBLAW ×53, TIM HORTONS ×95 and SHELL ×18 as bill suggestions —
4 of 9 rows were not bills — and offering no dismissal of any kind. The
verdict line: *the loose 2-occurrence detector is the single source of both
complaints (junk suggestions + no way to dismiss them), so one fix addresses
both.* The card also earned M133 D-4: at 688px it was the tallest module on
the page (29% at 1440, 31% at 390).

### The substrate

`components/reports/SubscriptionsPanel.tsx` reads
**`GET /transactions/recurring/candidates?source=subscriptions`** — the same
strict engine the M96 prompt uses (`services/recurring.py`, §Recurring prompt
→ Detection: ≥3 occurrences, ±15% same-sign band, every gap must fit one
cadence). The M27 detector is no longer dashboard substrate: the card no
longer reads `listSubscriptions` at all (M137 re-mounted `GET /subscriptions`
as a thin server read, and that module now has no `src/` consumer), and
`s3Engine.subscriptionsReport` was the parity port for that golden until
M160 (P2-5) retired it with the engine; the frozen `subscriptions.detect`
golden now pins the ROUTE instead, replayed by
`backend/tests/test_m160_replay_s3.py` (docs/08 §Subscriptions, docs/24 §M27,
docs/33 A12). The rows are `RecurringCandidate`s; the response's `dismiss_key`
is the key this surface suppresses under.

There is **no calibration knob**. The only exposed control is the evidence
window (`LOOKBACK_DAYS = 400`, the same trailing ~13 months the M27 rule
used): a false negative costs one manual "New bill", a false positive costs
the panel's credibility. The card's own help text states the rule verbatim,
because M133's evidence was a surface quoting its bug back at the user
("2 or more charges").

### The card

- **Headline: `Bills you probably have`** — honest about the inference.
- **Cap 5 + `Show all (N)`** (D-4), toggling back with `Show fewer`.
- **Header `Dismiss all (N)`** and a per-row **`Not a bill — <payee>`**.
- Meta line: `{N} suggestion(s) · $X/mo ($Y/yr)` where the total counts
  **money going out only** — a credit (PAYROLL clears the strict rule as
  happily as a bill) still earns its row but never inflates a figure headed
  "Bills" (M133 D-11's sign complaint, pre-empted here).
- **Per row: `Create bill reminder` ONLY when no bill covers it** (M165,
  §Recurring prompt → Already a bill): an enabled bill whose rule matches the
  row's payee at a compatible cadence replaces the button with the same muted
  `covered by '<name>' → /bills` line the prompt card prints — one component,
  one sentence, both surfaces. `Not a bill` stays either way.

### Dismissal — one vocabulary, deferred write

```
click "Not a bill" / "Dismiss all (N)"
  → stage 1 (immediate): row leaves the card, toast.undo offers Undo — NO request
  → Undo during the window: rows restored locally, still no request
  → window closes (6s) / Dismiss: stage 2 — one idempotent POST per key
       success → the candidates query is invalidated
       failure → rows restored + the envelope `detail` surfaced VERBATIM
```

- The window is **`DISMISS_UNDO_MS = 6000`** (`lib/recurring.ts`), deliberately
  6s rather than M95's 5s (§Destructive actions) — a bulk dismissal can hide
  many rows and the reader needs the count first. It is the SAME constant on
  both surfaces, so "dismiss" means one thing app-wide.
- **The key is per payee and per source**: `services/recurring.py::make_
  subscriptions_group_key(payee)` = `<normalize_payee_key(payee)>|subscriptions`,
  amount-free for the same reason `make_group_key` is, and `subscriptions` is
  deliberately **not** a `RecurringCadence` value — so a dashboard dismissal
  can never equal a prompt-group key.
- **No unsuppress route, still.** Nothing is written until the window closes,
  so Undo needs no compensating request and `recurring_suppressions` stays
  write-once and `UNIQUE(user_id, group_key)`.
- **Boundary (pytest-pinned):** dismissing here hides this card and leaves the
  M96 prompt's candidate on screen (and the reverse). Both surfaces read the
  same endpoint; `?source=` decides which keys are subtracted, and each
  answers `dismiss_key` for its own source.
- **Empty state never says "No subscriptions detected" after a dismissal.**
  It says `Dismissed N suggestion(s)` with a hint pointing at the Bills page
  (there is deliberately no suppression-management screen); with nothing
  dismissed it says `No bill suggestions right now` — **but only while the
  user has no bills defined.** Once `GET /bills` resolves with ≥1 bill (any
  status), that notice is onboarding advice already taken and the card
  renders nothing instead (no title, no hint, no preview, no CTA — absence
  is the only statement, and the panel never claims anything about the
  user's bills). While the shared `["bills"]` query is still pending the
  card holds `SkeletonRows rows={3}` so the notice never flashes then
  vanishes, and an ERRORED bills query still shows the notice — unknown is
  never read as "has bills". Pinned in
  `tests/m138SubscriptionsPanel.test.tsx` (the four bills-state branches).

### Verification

`tests/m138SubscriptionsPanel.test.tsx` (substrate, cap, both dismissals,
Undo/commit/failure, both empty states, bills resolved EMPTY so the uncovered
affordances stay pinned), `tests/m165BillCoverage.test.tsx` (the M138 half of
the coverage rule — CTA suppressed + line printed, and kept when no bill
matches), `tests/test_m138_subscriptions_card.py`
(the gate against the four M133 junk histories and the six real bills, key
parity + source discriminator, the route boundary), `tests/recurringPrompt.test.tsx`
(the prompt's `Dismiss all (N)` + Undo), `tests/billAutofill.test.tsx` (the
/bills hand-off off a candidate).

## Tags that mark recurring (M174)

**Context — the user's own intention as a recurrence signal.** Mark: *"for
recurring stuff can we make that based on tags since there can be multiple"*.
A tag is the one thing in the ledger that states what the USER means rather than
what the bank printed, and it is already multi-valued (docs/09 §Tag UX), so it
can name a subscription even when the payee string, the cadence or the amount
alone would not.

### The setting — the user names the tags

Settings → **Tags that mark recurring**, a multi-select over the tenant's OWN
tags (each a ≥44px `aria-pressed` chip), with `Save tags`.

- **NO magic tag names.** A heuristic over the tag's name
  (`/sub|recurring|monthly|bill/i`) would be a second, invisible normalizer: it
  would silently decide that "Subscriptions (old)" counts and "abos" does not,
  and the user could only find that out by seeing a wrong calendar. The tag set
  is an explicit list — `users.recurring_intent_tag_ids`, a `JSON` column
  (`[]` = the feature is OFF), docs/02.
- **The card says OUT when it is out.** The caption reads `Off — no tag marks a
  subscription.` with nothing selected, because "no dots" is otherwise
  indistinguishable from "the feature is broken" (the degradation contract:
  a degraded feature explains itself in place).
- **One write, the whole list** — `PATCH /users/me {recurring_intent_tag_ids:
  [...]}`, the same shape `saved_report_views` uses; adding or removing a tag is
  a client-side list edit. An id the caller does not own is `422
  invalid_recurring_intent_tag_ids` (docs/08), and the demo tenant is
  `403 demo_account_restricted`.

### What it confirms (docs/36 §M174)

`services/confirmed_subscriptions.py::confirmed_subscriptions` gains a THIRD
source beside active bills and accepted groups: a detector group
(`services/recurring.py::detect_recurring`, the same M96 pass the prompt cards
run over the trailing `LOOKBACK_DAYS=400`) **every one of whose rows carries at
least one nominated tag**. Four rules, each one load-bearing:

1. **ALL of the group's rows**, not any — one untagged row in the run leaves it
   unconfirmed. A partly-marked run is ambiguity, and ambiguity is what turns a
   fact surface back into a guess (the M96 discipline).
2. **The math gate stays.** A tag is intent, never arithmetic: the ±15% amount
   band, the ≥3-occurrence floor and the every-gap-fits-one-cadence rule still
   have to hold, so a tagged pair of charges is not a subscription.
3. **Money OUT only** — the calendar's `subscription` series is signed
   money-out by contract (docs/36 decision 6), the same rule the accepted path
   applies, so a tagged `PAYROLL` group cannot render income in a bills colour.
4. **One merchant answers once.** The union is deduped by the shared
   `normalize_payee_key`: an active bill wins, an accepted group wins, and a row
   carrying TWO nominated tags is still ONE row and its group still ONE group —
   the multi-tag case is one dimension, never many.

**A suppression still wins.** `recurring_suppressions` (read through the same
`rekey_suppression` re-read the candidates route does) is subtracted, so a group
the user dismissed never returns as confirmed because a tag also matches it —
the same "a decision cannot be re-asked" promise the prompt cards make.

**Where it shows.** A tag-matched group appears exactly where an accepted one
does: the cash-flow calendar's `subscription` series (docs/36) and the payload's
`confirmed_subscription_count`. It does NOT appear on the dashboard's "Bills you
probably have" card, which is a PROMPT surface reading detector candidates
(§Bills you probably have) — tagging a run says "this is recurring"; it does not
say "make me a bill reminder", and the card keeps offering that.

**Boundary.** Naming a tag is not the same as accepting a group: nothing is
written to `transactions.is_recurring`, so removing the tag from the setting (or
from the rows) is immediately reversible — the tag source is a read-time JOIN,
never a stored assertion.

### Verification

`backend/tests/test_m174_recurring_intent_tags.py` (20) pins all of the above —
the ALL-rows rule, the empty set = off, the multi-tag single-group case, the
suppression, the two dedup winners, the cadence/occurrence floor, the credit
rule, the calendar event + count, and the write seam (foreign id → `422
invalid_recurring_intent_tag_ids`, malformed → `422 validation_error`, `[]`
clears). `frontend/tests/m174CreateFromSelection.test.tsx` (16) covers the
Settings control: the whole-list PATCH, the `aria-pressed` state, the OFF
caption, and the "you have no tags yet — make one on the Transactions page"
empty state.

## Recurring prompt (M96 B2)

**Context — tagging, not reporting.** The M27 tracker already *reports*
recurring charges (docs/24); what an import-heavy user lacks is a way to MARK
the rows so the ledger itself says "this repeats". This is a passive,
on-demand pass over the committed ledger that offers the answer and lets the
user take it or kill it — no LLM, no cloud call, no new dependency, and no
background job: the scan runs when `/transactions` is entered, once per
session. Since M170 the CALENDAR reads confirmed state only (docs/36 §M170), and
since M174 that confirmed set also includes a group the user marked with one of
their own "recurring" tags (§Tags that mark recurring), so a candidate that is
neither a bill, nor accepted, nor tag-marked appears on these prompt surfaces —
this card and §Bills you probably have — and nowhere else (the card's own
behaviour is unchanged).

### Detection (server-side, computed, never stored)

`GET /transactions/recurring/candidates` runs `services/recurring.py` over the
caller's committed rows for the trailing `LOOKBACK_DAYS=400`, keeping rows that
are **not** tagged yet, and returns a group only when all four hold:

1. same normalized merchant — `subscriptions.py::normalize_payee_key`, the ONE
   grouping rule in the codebase (`SUPERSTORE 1234` ≈ `Superstore #1234`), and
   — since M165 — **whitespace is not identity**: every space run is dropped
   before the trailing `#?digits` strip, so `GOOGLE *YouTubePremium` ≈
   `Google *Youtubepremium` ≈ `Google*Youtubepremium` (the three statement
   spellings of Mark's one YouTube Premium subscription, which used to arrive
   as TWO candidates, each offering **Create bill**). Case, whitespace and
   trailing branch numbers, and nothing else — no fuzzy match;
2. an ±15% amount band around the cluster's first amount, same sign (a refund
   is not a repeat);
3. **every** consecutive gap fitting one cadence — monthly-same-day first
   (gaps 26–32 **and** day-of-month spread ≤ 4, which is what separates a
   30/31-day cycle from a 28-day one), then fixed 28 / 30 / 31 / 14 / 7-day;
4. at least **3** occurrences.

A group the user declined (`recurring_suppressions`) and a merchant that
already carries a tagged row are subtracted before the response is built, so a
decision cannot be re-asked. Detection writes nothing: the accept writes three
columns on the chosen rows, the decline writes one suppression row.

**Which suppressions count is chosen by `?source=` (M138).** The default
`recurring_prompt` subtracts this card's own keys
(`<merchant>|<cadence>`); `subscriptions` subtracts the dashboard card's
(`<merchant>|subscriptions`). Each answer is scoped to the surface that gave
it — see §Bills you probably have.

### The card

`components/transactions/RecurringPromptCard.tsx`, mounted on `/transactions`
between the filter bar and the table:

- **One scan per session.** The first entry fetches; the result (and every
  answer given afterwards) lives in `sessionStorage`
  (`lib/recurringSession.ts`), so a reload or a re-entry reads the cache and
  the endpoint is hit **once** per browser session. Storage blocked → the scan
  re-runs, correctness over caching; a failed scan → the card stays hidden and
  caches nothing, so it retries next entry instead of nagging.
- **One identity per cache entry (privacy boundary).** `sessionStorage` is
  scoped to the **tab**, not to the user, so this cache must not outlive the
  identity that filled it. Every entry is stored as
  `{owner, groups}` where `owner` is the identity `AuthProvider` bound to the
  tab — normalized to a string, because the wire's user object carries an
  integer `id` (`backend/app/schemas/users.py::UserPublic`, the model behind
  login, restore and `GET /users/me`) while `types.User.id` says `string`, and
  `1` versus `"1"` compared unequal would make a same-user reload drop its own
  cache —
  and `readCandidates()` **deletes** — never merely hides — an entry whose
  `owner` is not the live one. The binding (`scopeCandidatesTo`) runs on every
  seam the mask boot contract runs on: session restore, every explicit
  identity change (password / TOTP / recovery / passkey / OIDC / demo login,
  logout), and a failed boot; binding `null` clears outright, and an entry
  written before this contract existed (a bare array under the key) is
  unowned by construction and is deleted on sight. Consequences, both pinned
  by `tests/ledger-ui-build/b2-recurring-tenant-scope.test.tsx`: signing out
  of one tenant and into another in the same tab shows the second tenant no
  prior rows — not for one paint, not after a hard reload, and nothing of the
  first tenant's remains in storage — while a reload as the **same** user
  keeps their entry and still does not re-scan.
- **Per group**: `Tag as recurring` (→ `POST …/recurring/accept` with exactly
  the ids shown, then `transactions` + `budgets` + `reports` invalidated so the
  badge and the rollups re-read) and `Not recurring` (→ `POST …/recurring/suppress`).
  **Create bill only when nothing covers it** (§Already a bill).
- **Card-level `Dismiss all (N)`** (M138): declines everything still listed,
  once — never all-or-nothing on ACCEPT, all-at-once only on dismiss. The
  count is in the label because it is the only thing that says how much is
  about to be lost for good, and the write waits out the same 6-second Undo
  toast the dashboard card uses (§Bills you probably have).
- Headline `These look recurring — tag them?`, each group showing payee,
  cadence label, occurrences, next expected date and its monthly amount, plus
  the combined monthly total of what is still listed.

### Already a bill (M165) — never offer to create what exists

The card is a creation surface, so it may not claim a creation is needed when
it is not: **if an ENABLED bill already tracks the candidate's payee, the row
gets no Create-bill button at all** — it gets the muted line

> covered by '<bill name>' → /bills

(`components/bills/CoveredByLine.tsx`, the ONE copy of that sentence — a
`Link`, never a `Button`, because there is no action left: the record exists).
The row keeps `Tag as recurring` and `Not recurring`, its share of the card's
count, and its suppression POST — coverage removes an affordance, never an
answer.

- **How a candidate is checked** (`lib/billCoverage.ts::coveringBill`): the
  `["bills"]` cache entry the Bills page already owns (`GET /bills`, same
  react-query family — no new endpoint, no per-candidate request) × the ONE
  payee key (`lib/payeeKey.ts`, the port of the backend's
  `normalize_payee_key`, so the browser's "same payee" is the server's) × the
  bill's OWN rule (`readTerms`/`readLogic` + `match_mode`, ANY or ALL) — i.e.
  the same comparison `services/bill_match.py` would run. Amount and category
  scope are deliberately NOT part of it: those decide which PAYMENTS a bill
  counts, not whether a bill exists (docs/25 §M116 D2).
- **Cadence tolerance**: `monthly` covers `monthly` however the real gaps fall
  (the detector's 26–32-day fit includes a 28-day cycle), and `four_weekly` —
  a 28-day cycle, a month by another name — is covered by a monthly bill too.
  Weekly/biweekly have no bill cadence, and `one_time` is not a stream, so
  neither is ever claimed.
- **Unknown is not covered.** While the bills read is in flight or after it
  fails, the row keeps its Create-bill hand-off (which only opens a DRAFT —
  M55 d6: nothing is written until the form is submitted). Absence of evidence
  is never printed as `covered by …`.
- **Both surfaces, one rule**: the dashboard's M138 card
  (§Bills you probably have) drops its `Create bill reminder` button under the
  same test and prints the same line.

### Privacy copy (the honesty rule)

The card MUST carry this sentence, verbatim:

> this detection runs entirely in your browser / on your server; nothing leaves

It is exported as `RECURRING_PRIVACY_COPY` and asserted by
`tests/recurringPrompt.test.tsx` — a copy edit that drops or rewords it fails
the build. The claim is literally true of both halves: the browser sends **one
same-origin request** to the caller's own server, and that server computes with
`services/recurring.py`, which imports no HTTP client, no LLM and neither of
the two network seams AGENTS §2 allows (`services/fx.py`,
`services/market_data.py`) — pinned by
`tests/test_recurring.py::test_detection_module_makes_no_network_calls`. The
network log during detection therefore shows **zero off-origin requests**.

### Row badge

A tagged row renders a neutral chip in the Tags cell (`↻ recurring · monthly`,
`title` = `Monthly · next 2026-11-03`) — neutral tone because cadence is not a
semantic state (the M27 frequency badges use the same rule), sourced from
`transactions.is_recurring` / `recurring_cadence` / `recurring_next_date`.

### Budgets label

`/budgets` prints **"Committed monthly (tagged recurring): $X across N tagged
rows"** from `lib/recurring.ts::committedMonthly` over the rows
`listBudgets` already loads (CAD only — a foreign row counts at its
`cad_amount` mirror, a row without one is excluded, docs/12's rule). It is a
LABEL: `s3Engine.budgetsProgress` and the server's `pct_used` are untouched.
The key is `["budgets","recurring-committed"]`, so tagging a row from the
prompt card (which invalidates `["budgets"]`) refreshes it.

### Touch points
- `backend/app/services/recurring.py` — the pure detector (+ M165's
  `rekey_suppression`).
- `backend/app/services/subscriptions.py` — `normalize_payee_key`, the ONE
  grouping key (M165: whitespace-insensitive).
- `backend/app/api/transactions.py` — `…/recurring/candidates|accept|suppress`.
- `backend/app/models/recurring_suppressions.py` + migration `0037_recurring`.
- `frontend/src/components/transactions/RecurringPromptCard.tsx`,
  `frontend/src/lib/recurring.ts`, `frontend/src/lib/recurringSession.ts`.
- M165 coverage: `frontend/src/lib/payeeKey.ts`,
  `frontend/src/lib/billCoverage.ts`,
  `frontend/src/components/bills/CoveredByLine.tsx` (shared with the
  dashboard card).
- `frontend/src/pages/Transactions.tsx` (card + badge),
  `frontend/src/pages/Budgets.tsx` (label).
- `backend/tests/test_recurring.py` (24), `frontend/tests/recurringPrompt.test.tsx`
  (15), `frontend/tests/billCoverage.test.ts` (13),
  `frontend/tests/m165BillCoverage.test.tsx` (8).

### Verification
- Backend: fixture matrix (mixed merchants, branch-number collapse, ±15% band
  splits and merges, a broken cadence producing nothing, the three fixed
  cadences, monthly clamp), the route round-trip (candidates → accept → the
  three columns + the badge fields on `GET /transactions` → the merchant gone),
  decline idempotency and per-user isolation, pending-batch rows neither
  proposed nor taggable, 404/409/422 paths, and the two local-only proofs
  (static import scan + detection with `socket.socket` replaced) — plus M165:
  the three YouTube Premium spellings collapse to ONE merchant key and ONE
  candidate group, `rekey_suppression` is pure and idempotent, and a
  suppression row written in the PRE-M165 key format still answers the group
  on both sources (the dashboard "no" still leaves the prompt's candidate up).
- Frontend: `tests/recurringPrompt.test.tsx` — verbatim privacy copy, combined
  total, accept invalidating `transactions`/`budgets`/`reports`, decline
  suppressing, **one network call per session** (fresh mount, fresh query
  cache), dismiss-all, failure-hides, `committedMonthly` math;
  `tests/billCoverage.test.ts` + `tests/m165BillCoverage.test.tsx` (M165) —
  one row per payee, no Create CTA under a covering bill, the `covered by …`
  line as a LINK, the suppression POST still per-group, the cadence tolerance,
  a disabled/absent/unknown bill keeping the CTA, and the same rule on the
  M138 card.
- Network log during the card's detection on the deployed stack: same-origin
  requests only, zero off-origin calls (evidence in the completion comment).

## Match terms (M121)

The rule editor's term field is a **chips input** paired with an **ANY/ALL
segmented control** (`components/bills/MatchTermsField.tsx`, used by both the
`/bills` create/edit form and **Edit rule** on `/bills/:billId` — docs/25
§M121 D8).

**Contract**

- **One chip per term**, in the rule's order, each with its own remove button
  named `Remove term — <term>` (so a screen-reader user can delete the middle
  term without walking past the others). The chip list is a `ul` named
  `Match terms`, same accessible name as the input.
- **Keyboard**: `Enter` commits the typed text as a chip (and never submits
  the form — a term is not a submission); `Backspace` with an EMPTY field
  removes the LAST chip, and stops at zero; `Backspace` with text edits the
  text and removes nothing. Committing also happens on blur, so clicking
  **Save** keeps a half-typed term — and the payload is composed from chips +
  draft anyway (`composeTerms`), so no path can drop it.
- **Bounds are visible**: the field stops at 10 terms with the hint
  `Up to 10 terms — remove one before adding another.` and keeps the typed
  text instead of swallowing it; duplicates are not added twice; each term is
  capped at 120 chars by `maxLength`.
- **ANY / ALL** is a `SegmentedControl` (`role="tablist"`, each segment a
  `role="tab"` with `aria-selected`) labelled `Match logic — ANY or ALL`, sitting
  BESIDE the field on the same flex row. The visible words are literally `ANY`
  and `ALL` — no icon, no abbreviation — so the two remain readable at **390 px**
  (they are the whole point of the control: ANY widens the set, ALL narrows
  it). `ANY` is the default; each segment's `title` spells the effect out for
  pointer users. On a narrow viewport the row wraps and the strip itself is a
  focusable `overflow-x-auto` region (M102 contract), so a keyboard user can
  still reach both segments.
- The hint under the field states the keyboard contract and the cap
  (`Enter to add · Backspace on an empty field removes the last term · up to
  10 terms`), wired to the input with `aria-describedby`.
- **The header says the conjunction**: `/bills/:billId` renders
  `contains “ROGERS” or “RCOM”` for ANY and `contains “ROGERS” and “WIRELESS”`
  for ALL (docs/25 §M121 D9) — the prose widens/narrows with the counts.

**Verification**: `frontend/tests/m121MatchTerms.test.tsx` (9 tests — Enter,
Backspace-when-empty, per-chip removal, the 11th-term refusal, dedupe,
ANY/ALL words + `aria-selected`, `composeTerms`, `readTerms`, `describeTerms`);
`m116BillDetail` and `billAutofill` pin the page wiring and the payload.

## Rule suggestions & draft Test (M122)

The M121 chips field grows two surfaces (docs/25 §M122): **suggestions from
the user's own ledger** and the **dry-run "Test"** panel. Same components, no
new pages.

**Suggestions — the listbox conventions** (`MatchTermsField.tsx` +
`useTermSuggestions.ts`)

- The term input is a **combobox** over a `role="listbox"` of
  `role="option"` rows, tracked with `aria-activedescendant` — the SAME
  contract the M104 TagPicker pins (above, §Tag UX): the INPUT keeps real
  focus, typing never stops, `aria-expanded`/`aria-controls` ride the input
  while the list is open, and the `id` sits on the SCROLLING listbox so axe's
  `scrollable-region-focusable` exemption applies.
- **The gate is the contract**: an EMPTY draft fetches the top payee groups
  (they render as quick-add buttons, `Add suggested term — ROGERS
  COMMUNICATIONS ×14`, under an `In your ledger:` label — no focus needed);
  a draft of **≥3 chars** opens the listbox; 1–2 chars fetches NOTHING.
  Fetches are debounced 200 ms (`useTermSuggestions`, react-query with
  `keepPreviousData`), and the server excludes what the SAVED rule already
  matches, so nothing offered is noise.
- **Keyboard**: ArrowUp/ArrowDown walk the list (wrapping), Home/End jump to
  the ends — only while the list is open, so caret movement stays natural
  without one; Enter adds the HIGHLIGHTED suggestion (replacing the draft
  text), and with NO highlight it commits the typed text exactly as M121 did;
  Escape dismisses the list first and only that. A click on an option
  `preventDefault`s on `mousedown`, so the input never blurs into a draft
  commit under the user's cursor.
- Each option shows the real payee plus its `×count` (frequency as social
  proof — every string comes out of the caller's own committed rows); a term
  already committed as a chip is never re-offered.

**Draft Test panel** (`RuleTestPanel.tsx`)

- Both editors run the same action under the same word: **Test** beside
  **Save rule** on `/bills/:billId`, **Test rule** in the `/bills` form. The
  panel is labelled `Draft preview — not saved` (a `Badge`, plus the outer
  div's `aria-label`) and renders the SERVER's summary — N matching
  transactions, first → last date, the last-12-period sum, per-account
  counts, the first 8 rows and the period table. Editing the draft clears a
  stale preview; nothing is written anywhere.

**Chart line + Spending stats** (`BillHistoryBarChart.tsx`, `BillDetail.tsx`)

- The period chart overlays a **dashed 6-period rolling-average line**
  (`Line`, accent token, `strokeDasharray="5 4"`) fed by the server's
  `point.rolling_six`; the chart's aria sentence gains `6-period average
  ends at …` and the fallback table gains the `6-period avg` column — the
  SVG is never the only way to read a number (docs/12 "Chart accessibility").
- `/bills/:billId`'s card is renamed **Spending stats**: the two rolling
  windows (missed months as `$0.00`) beside all-time total, per-payment mean,
  `This year (YYYY)` YTD and yearly average — all read straight off the
  server's `stats`, never re-averaged on the page. Compact `/bills` rows gain
  `· YTD $x` from `ytd_total`.

**Verification**: `frontend/tests/m122BillSuggestions.test.tsx` (8 tests —
the fetch gate, quick-add chips, the keyboard lap, Enter-without-highlight,
the Test panel + stale clearing, the chart aria + fallback column, the stats
card). `focusRing` covers the chips' hover boundary in both themes.

## Mobile & PWA (M72)

**Scope.** The six core pages (Dashboard, Transactions, Budgets, Bills,
Calendar, Reports) have to be usable at 390×844: no horizontal *page* scroll,
touch targets ≥ 44px, `TableScroller` engaged wherever the columns overflow,
the rail becoming a real drawer, pull-to-refresh, and an installable PWA.
The milestone explicitly forbids changing the desktop layout, so every rule
below is scoped to `< 768px` (`@media (max-width: 767.98px)`); at `≥ 768px`
the shell, the control metrics and the palette are what they were in M71.

### Text fit — the flexible field ellipsizes, the facts never disappear (M82 F6)

Two rows used to put a *fact* (a date, a security name) inside a box that could
shrink below its own width, so the fact was the first thing clipped:

- **Dashboard → Recent transactions** (`pages/Dashboard.tsx`): payee and date
  each get their **own line** inside the row's flexible left column
  (`span.min-w-0.flex-1.flex-col`), with the amount in its own `shrink-0`
  column — the payee is the row's only ellipsized node. A ≥19-char payee or a
  ≥115% Android root font used to clip date characters (payee+date shared one
  `span.min-w-0.truncate`: 6.5px of date gone at a 20-char payee, ~96px of a
  105px date at 130%, both at 412px), and at 360px the three fields could not
  fit one line at all — even the demo's 16-char payee was cut by 35px.
- **`/securities/:id` header** (`pages/SecurityDetail.tsx`): the issuer name is
  plain text — no `truncate`. Long names wrap to a second line instead of
  clipping (it was cut 55px at 412px, 107px at 360px), and the info column
  keeps `min-w-40` so the Refresh/Watching controls wrap onto their own row
  below ~420px instead of squeezing the name column under 50px.

Contract for any new list row on a narrow viewport: exactly one field may carry
`truncate`; every field the user must *read* (date, amount, status) gets
`shrink-0` — or its own line.

Verification: paint-proof `document.elementFromPoint` grid over the date rect at
412px with a 30-char payee injected and root font 16/18.4/20.8px (100/115/130%)
returns `paintsDate=true` at every sample; the overflow audit
(`scrollWidth - clientWidth > 1` over every `overflow:hidden` node) reports **0
cut nodes** on `/dashboard` and `/securities/67` at 412 and 360 with demo data.

### Navigation — one `<nav>` that is a rail at `md+` and a drawer below it

`AppShell.tsx` keeps a single `<motion.nav id="app-nav">` and moves it between
the two roles (no second navigation tree to keep in sync):

| State | Geometry | Contract |
| --- | --- | --- |
| `≥ 768` | `md:sticky md:z-40`, width `pinned ?? 224` | byte-identical behaviour to M71 — the M68 stacking contract (rail under the bell panel's `z-50`, tied with the bottom tabs' `z-40`) still holds; `tests/m68Navigation` untouched and green |
| `< 768`, closed | `fixed`, `x = -248`, width 76, `inert` | parked off-canvas **and** out of the focus order / accessibility tree — translating it alone would leave every rail link tabbable behind an invisible edge |
| `< 768`, open | `x = 0`, width 224, scrim `z-40` | `aria-expanded="true"`, focus moves into the nav (shared `useDialogFocus`), Escape closes (single shell-level keydown router — see §Escape routing), scrim click and route change close it, focus returns to the toggle |

The toggle lives in a new mobile-only `<header class="md:hidden">`: fixed,
`pt-[env(safe-area-inset-top)]`, a 44px (`size-11`) toggle carrying
`aria-expanded` + `aria-controls="app-nav"`, the brand, and — inside the
drawer's brand row — a 44px close button. The bottom tabs keep their 44px
minimum, gain `min-w-[44px] flex-1` (they were 28–40px wide) and
`box-content pb-[env(safe-area-inset-bottom)]` so the bar grows over the home
indicator instead of squeezing its labels.

**Max font scale (M82 F7).** The tab floor is a *pixel* constant, never a rem
one: `min-w-11` (2.75rem) resolved to 88px per tab at Android's largest font
setting (root 32px = 200%), so 6 × 88 = 528px pushed the 6th tab (Settings) to
x=440..528 — outside a 412px viewport, and `main`'s `overflow-x:clip` dropped
it with no scrollbar to recover it. The tab label is now capped at
`min(0.75rem, 14px)` and `max-w-full truncate`d, so it grows with the user's
setting only up to the size a 1/6 column can hold. At 100–115% the cap never
engages (`0.75rem ≤ 14px`) and the bar is unchanged; at 130% labels render
14px instead of 15.6px; at 200% all six tabs sit inside the viewport with
un-elided labels. Pinned by
`tests/m82FontScale.test.tsx`; browser evidence (412×915 + 360×800, root
32px/16px × 8 routes: `fullyOffRight = 0`, every tab hit-testable) is on the
F7 card. **Known limitation:** this milestone scoped the *nav* only — page
content at 200% still overflows in places (step bar, dense tables, badge
rows); full reflow at max font scale is a separate card.

### Escape routing — one keydown listener for the whole shell

The shell owns exactly **one** `window` `keydown` listener (`AppShell.tsx`,
`escapeState` + `onKeydown`), and it reads live state from a ref that a
`useLayoutEffect` refreshes after every commit. It closes the top-most open
layer per press, innermost first: **command palette → inbox panel → search
overlay → `More` panel → account menu → nav drawer** (the palette is M95 P1's
fourth surface; the M185 shell added the `More` panel and the account menu as
its fifth and sixth, both joining THIS listener instead of registering their
own — the same listener also owns the `⌘K` / `Ctrl+K` toggle, see §Command
palette). Everything else about Escape is unchanged (dialogs still go through
`dialogA11y::useEscapeToClose`, see §Keyboard contract).

Why one listener and not three: the drawer used to register its own
`useEscapeToClose(() => setDrawerOpen(false), drawerActive)`, and that inline
arrow is a **new identity on every AppShell render**, so the effect removed and
re-added the `window` listener on every render. A render landing *inside* a
running keydown dispatch — which the search overlay's `closeSearch()` setState
did on every press — therefore deleted the drawer's listener before the
dispatcher reached it. Escape arrived, the handler never ran (`hits: 0` in the
QA capture), the drawer stayed open: **33 of 35 fresh-session attempts failed**
(QA F1, `probe13_out.json` shows `removeEventListener` executing inside the
dispatch stack). Closing the drawer any other way still worked, which is why
the defect hid behind "Escape closes it" in this table.

Contract, now pinned by `tests/m72Mobile.test.tsx`:

- Escape on the **input path** (event starts at the focused element and
  bubbles through the document to `window` — the same DOM path a physical key
  takes) closes the open drawer; the synthetic `window.dispatchEvent` control
  QA used as a stopgap is no longer the only thing that passes;
- a render landing **in the middle of the dispatch** cannot unhook the handler
  (forced with `flushSync` in the test — the exact interleaving of the QA
  capture);
- the shell registers its `keydown` listener **once** while mounted — no
  re-registration per render, so the race cannot re-form when a further Escape
  surface is added (the M95 P1 palette, the M185 `More` panel and the M185
  account menu are exactly that: further consumers of the same listener,
  `⌘K` toggle included, never a second listener — the `More` panel's own
  "no listener" claim is pinned in `tests/ledgerShellFocus.test.tsx`, which
  spies on `window.addEventListener`);
- `dialogA11y::useEscapeToClose` follows the same rule (register once per
  `enabled`, call through a latest-closure ref), so the class of bug cannot
  come back through a dialog either.

`frontend/scripts/m72/drawer_escape_probe.py` is the live check: three focus
positions (inside the drawer / back on the toggle / in the search field with a
query), two rounds each, trusted CDP keys, exit 0 only if every one closes —
**6/6 PASS on the deployed stack** (the search case pins the layering: press 1
closes the search panel and leaves the drawer up, press 2 closes the drawer).

### Touch-target floor (44px, `< 768px` only)

A `< 768px` block in `index.css` raises every standalone control in `main`
(`button`, `select`, `input`, `a[href]`) to `min-height: 44px`, buttons also to
`min-width: 44px` (opt out with `.touch-inline`), and adds two fixes the naive
rule cannot express:

- **the nav drawer** (`#app-nav …`, same four selectors) — the floor follows
  the *surface*, not the element type. The drawer sits outside `main`, so the
  rule above never reached it and its rows measured **184×36** (menu links) and
  **184×32** (account rows) at 390 while this section's measurement reported
  "0 of 856 controls under 44px" — the parked drawer is `inert`, and the audit
  skipped it, so the surface a phone user touches most was outside the
  instrument. PM ruling on QA F3: extend the floor to the drawer (M72's own
  acceptance is "390px-first, ≥44px targets"), still scoped to `< 768px` so
  the desktop rail keeps M71's metrics byte-for-byte;
- `main td a[href]` → `inline-flex; min-height: 44px` — account/category links
  in data rows are *inline* boxes, where `min-height` is ignored;
- `[role="scrollbar"]::before` → `top/bottom: -17px` — the M64 thumb is a
  pinned 10px visual bar, so the hit band is grown on a pseudo element: 44px
  of touch area, **zero** change to what is painted (clicks in the band land on
  the track's own click-to-jump).

**M100 — the collapsed desktop rail opts IN.** The `< 768px` scoping exists
so M71's desktop metrics stay byte-for-byte identical; the collapsed rail is a
DESKTOP surface, so its icon rows still measured ~36px tall. The M100 card pins
**≥44px on every collapsed control** (nav rows, wallet, coins, notifications,
log out, expand, theme), so those rows now carry `min-h-11` directly in
`AppShell` rather than through this CSS block — 44px tall across the rail's
content width. `responsive_audit.py`'s "floor not applied" columns stay true
at ≥768 (it measures `main`, not the rail); the rail's own floor is pinned by
`tests/m100AccountsNav.test.tsx`.

Measured by `frontend/scripts/m72/responsive_audit.py` (6 pages × 2 themes per
row; "escapes" = elements whose box leaves the viewport while only `clip`/
`hidden` ancestors would hide it — scrollable ancestors are not counted):

| Viewport | Runs | Page horizontal scroll | Escapes past the edge | Controls < 44px | Console errors |
| --- | --- | --- | --- | --- | --- |
| 390×844 | 12 | **0** | **0** | **0** of 856 controls measured (Transactions alone measures 292/page) | 0 |
| 768×1024 | 12 | **0** | 4 (Reports only — see Residuals) | floor not applied (`≥ 768` = desktop metrics) | 0 |
| 1280×800 | 12 | **0** | **0** | floor not applied | 0 |

These three rows were re-measured **against the deployed stack** (example.internal
nginx through the LAN tunnel, `http://127.0.0.1:18080`, 2026-09-30) and came
back identical: 36/36 runs, 0 page h-scroll, 0 escapes at 390 and 1280, 0 of
856 controls under 44px at 390, 0 console errors — with one caveat the same
re-measurement exposed: the "0 console errors" column came from a channel that
cannot see a CSP refusal (see §Inline scripts and the CSP), so it was not
evidence about the header either way.

The sweep now measures the **drawer** too (below md only): the parked drawer is
`inert` and therefore skipped by the loop above, which is exactly why its rows
— **184×36** menu links, **184×32** account rows at 390 — never appeared in any
count above (QA F3). The harness opens the drawer, measures every control
inside it, screenshots it (`drawer-<page>-<theme>-390x844.png`), then presses a
**real Escape** (CDP trusted key — the input path QA found broken) and records
whether it closed (`drawer` / `drawerEscapeClosed` in the JSON).

Re-measured for this change on 2026-09-30 against the **deployed stack**
(example.internal nginx through the LAN tunnel, `http://127.0.0.1:18080`, after the
release build landed): 36 runs (6 pages × 3 widths × 2 themes), 0 failures —
the same numbers the pre-release run of the identical bundle produced against
`scripts/m72/preview_with_csp.py` (which serves `dist/` with the Dockerfile's
own CSP header, so the header-dependent claims were checkable before the
deploy):

| Check | Result |
| --- | --- |
| page horizontal scroll | **0** of 36 runs |
| escapes past the edge | 0 at 390 · 4 at 768 (Reports, Residuals) · 0 at 1280 |
| controls < 44px at 390 — page surface | **0** of 856 (Transactions alone measures 292/page) |
| controls < 44px at 390 — open drawer | **0** of 336, `minHeight` **44** on all 12 drawer runs |
| Escape closes the drawer (390, trusted key) | **12 / 12** (6 pages × 2 themes) |
| console errors | **0** across 36 loads, on the channel that reads `Log.entryAdded` |

The committed `m72-audit-{390,768,1280}.json` are these runs (`base` field
`http://127.0.0.1:18080`, the deployed stack) and carry the `drawer` /
`drawerEscapeClosed` fields per run.

**Zero-console-error baseline (standing, re-checked by every sweep).** The
`0` above is a *bar*, not a one-off: the QA console sweep re-measures it on
every build, and a first-page route that fires a request guaranteed to fail
breaks it even though the page still renders. `/settings` did exactly that
until M92 — `PartnerSharingRow` unconditionally fetched
`GET /api/v1/partner/state`, which answered `401 sharing_not_linked` for
every user without a sharing link, so **6/6 settings loads errored**. The
route now answers `200 {linked: false}` for an authenticated caller with no
link (docs/43 §6), and the row treats that as data rather than a failure. Any
new unconditional fetch on a first-page route must be checked against this
baseline before it ships.

`TableScroller` reports `scrollWidth − clientWidth > 0` on Transactions at all
three widths (390: one scroll box, with the `role="region"` wrapper and the
"Scroll for actions" hint present), so the M64 affordance — not a horizontal
page scrollbar — carries the wide table.

### Safe areas

`index.html` ships `viewport-fit=cover`, `theme-color`, the iOS install meta
(`apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style:
black-translucent`, `apple-touch-icon`) and a manifest link. The shell
compensates, all of it `md:`-overridden so desktop padding is untouched:
mobile header `pt-[env(safe-area-inset-top)]` + `main`
`pt-[calc(3.5rem_+_env(safe-area-inset-top))]`, route content
`pb-[calc(3.5rem_+_env(safe-area-inset-bottom))]`, bottom tabs
`pb-[env(safe-area-inset-bottom)]`.

### Inline scripts and the CSP

`index.html` carries the only two inline `<script>` blocks in the app:

| snippet | size | job |
| --- | --- | --- |
| theme bootstrap | 529 chars | `localStorage.theme` → `prefers-color-scheme` → dark, applied to `<html>` before first paint (no FOUC) |
| status-bar theming (M72) | 413 chars | re-pins `<meta name="theme-color">` to `#090c12` / `#f3f5f8` for the installed window |

nginx sends `Content-Security-Policy` with `script-src 'self'` on every
response — at server level (so `location /`, the SPA fallback, inherits it)
**and again** inside `location = /index.html` and `location /assets/`, because
a location that declares its own `add_header` does not inherit the
server-level ones. `'self'` alone never allows an inline script, so both
snippets are pinned by content hash in all three copies:

```
script-src 'self' 'sha256-+ZALdw0iadt/tBIxsuaCO41RtzhTbzMDens3eBB3Ntc=' 'sha256-qf3l4/KPFG0mZjBijINyOtWln+1RnQ+Cz7atNIcdFpk=';
```

Hashes (not a nonce) because the response is a static file served by nginx —
a nonce would need per-response signing. `frontend/scripts/csp-hash-check.mjs`
(`npm run check:csp`) runs in the Dockerfile **build stage** and fails the
image build when the pinned set and `dist/index.html` disagree: it reports
every `add_header` line missing a hash by line number, plus any dead hash the
build no longer produces. The hashes are computed over the built file's exact
script text (vite rewrites `index.html`, so the source bytes are not what the
browser sees).

Defect this replaced (QA F2): with `script-src 'self'` and no hash, Chrome
blocked **both** scripts on every load — 2 console errors per page, **72
across 36 loads** — contradicting the "zero console errors" claims in
§Verification below, and the pre-hydration theme never ran: the shipped HTML is
hard-coded `class="dark"` + `theme-color #090c12`, so a light-theme user got a
dark first paint until React hydrated (post-hydration theme and `theme-color`
already tracked correctly, light `#f3f5f8` ↔ dark `#090c12`).

Proven both ways by `scripts/m72/f2_theme_bootstrap.py`, which sets
`theme=light` and then **blocks `/assets/*.js`** so the bundle cannot hydrate
and repair the class behind the script — whatever the class is after load is
what the inline bootstrap alone produced:

| header | `html.class` | `theme-color` | CSP violations |
| --- | --- | --- | --- |
| `script-src 'self'` (pre-fix, deployed) | `dark` | `#090c12` | 2 per load — **bootstrap did not run** |
| `+ sha256 …` (this change) | `""` | `#f3f5f8` | 0 — **bootstrap ran, light first paint** |

### Pull-to-refresh

`frontend/src/components/ui/PullToRefresh.tsx` — touch-only and gated so it
can never steal another surface's gesture:

- engages only at `scrollY === 0` **and** with a vertically dominant drag
  (`|dy| > 1.5·|dx|`): a horizontal drag stays `TableScroller`'s (M64), a
  mid-list pull stays the list's;
- rubber band `0.5×`, capped at 140px; releasing at ≥ 72px commits;
- committing runs `queryClient.invalidateQueries()` — the gesture **never
  serves local data**, it re-reads every active query from the server;
- disabled while the drawer is open;
- the indicator is `role="status"` (`Pull to refresh` → `Release to refresh`
  → `Refreshing…`); under `prefers-reduced-motion` it renders static text, no
  transition and no spinning glyph (the global reduce rule in `index.css`
  collapses the CSS side).

Evidence (`m72-pwa.json`, real CDP touch input against the production build,
re-run after the F1/F2/F3 release): `pulling → pulling → pulling → ready →
ready`, release → `refreshing`, then settled to idle; API requests on the page
**19 → 35** (genuine refetch), **0 console errors** — and that 0 now means
something, because the channel reads `Log.entryAdded` too (§Inline scripts and
the CSP): the same JSON would have carried two CSP violations per load on the
pre-fix header.

### Installable PWA

`frontend/public/manifest.json` (served `application/json`), linked from
`index.html`:

| Field | Value |
| --- | --- |
| `id`, `scope` | `/` |
| `start_url` | `/dashboard` |
| `display` | `standalone` |
| `name` / `short_name` | `fin_tracker — personal finance` / `fin_tracker` |
| `theme_color` / `background_color` | `#090c12` (dark surface); `ThemeProvider` re-pins `theme-color` to `#f3f5f8` in light, and the document bootstrap does the same before first paint |
| icons | 192, 512 (`purpose: any`), 512 `maskable`, 180 Apple touch — generated from one SVG by `frontend/scripts/m72/make_icons.py` (headless Chrome; this box has no image toolchain) and committed |

`frontend/public/sw.js` caches the **shell, never the data**:

| Request | Strategy | Why |
| --- | --- | --- |
| `/api/*` | not intercepted, never cached | data is server-backed by construction — offline the app renders its own error/empty state instead of numbers that look current |
| navigations | network-first → cached `/index.html` | a deploy is picked up immediately; a cold offline open still boots the app shell |
| `/assets/*` | cache-first, **404 → stale-shell self-heal (M114)** | hashed, immutable — re-fetching is waste; but a lazily-needed chunk (M95 gave `CommandPalette` its own chunk) is fetched only when the user opens it, so a shell from BEFORE a deploy asks for a hash the new image no longer has |
| manifest, icons, other same-origin GET | stale-while-revalidate | cheap, self-healing |

`src/pwa.ts` registers it only when `import.meta.env.PROD && isSecureContext`
(the http LAN deployment behaves exactly as before), with
`updateViaCache: "none"`; a registration failure is swallowed — the worker is
an enhancement, the app never depends on it.

**Stale shell after a deploy (M114 — the ⌘K palette's `error loading
dynamically imported module …CommandPalette-DMeurLAO.js`).** Two things made
cache-first on `/assets/*` wrong for lazy chunks: `CACHE_NAME` was the literal
`fintrack-shell-v1` (the activate handler only purges caches whose NAME
differs, so no deploy ever rotated it), and a 404 fell straight through to the
page, where React Router kept the error on screen. Three legs, one commit:

- **BUILD_ID stamp.** `frontend/scripts/stamp-sw.mjs`, wired into `npm run
  build`, rewrites `const BUILD_ID = "__BUILD_ID__"` in `dist/sw.js` with
  `sha256(dist/index.html)[0:12]` → `CACHE_NAME = fintrack-shell-v1-<id>`.
  Every deploy whose shell changed activates a NEW cache and `activate` drops
  the previous build's chunks; `install` precaches `/index.html` **and the
  `/assets/*` names it**, because the new cache owns no entry chunk otherwise
  (the offline boot this section pins would break right after a deploy).
  `public/*` reaches `dist/` verbatim, so this literal — not `vite define`,
  which the bundler only applies to `src/` — is what the build can rewrite.
- **404 self-heal (worker).** An asset cache miss that reaches a 404 means the
  document in memory is from another deploy: the worker deletes any cached
  attempt, re-fetches `/index.html` with `cache: "reload"` and stores it,
  broadcasts `fintrack:reload` (+ the BUILD_ID) to every window client, and —
  because a `<script>` cannot be answered with HTML — answers a `.js` request
  with a **reload marker** module: one guarded `location.reload()` plus a
  component that renders nothing while the navigation is in flight and THROWS
  when the guard suppressed it, so a genuinely broken asset stays visible
  instead of looping. Non-script destinations keep their 404 (the message
  still goes out), and with no fresh `/index.html` there is no heal at all.
- **Client half.** `watchStaleShell()` reloads on `fintrack:reload` and on an
  `unhandledrejection` carrying a dynamic-import failure — but only if a
  freshly fetched `/index.html` names different `/assets/` than the live
  document (a missing chunk on the CURRENT shell is a broken deploy, not a
  stale shell), so a dev build or a broken image never triggers it. Both
  doors share `fintrack-stale-reload-at` (30 s, per tab), which is what bounds
  the "self-healing loop" to one reload.
- **nginx — verified already correct, no change.** `location = /index.html`
  sends `Cache-Control: no-cache, must-revalidate`, and the SPA fallback's
  internal redirect re-enters that location, so `/`, `/login` and `/dashboard`
  all carry it on prod (measured 2026-10-08); `/assets/*` keeps
  `max-age=31536000, immutable`. The remaining stale path was therefore the
  worker's, exactly as the two legs above assume.

Browser evidence (`m72-pwa.json`): manifest + all four icons `200` with
`image/png`, worker registered at `/sw.js` (scope `/`, `controller: true`
after reload), and with the network forced **offline** a navigation to
`/budgets` still boots the app (React root mounted, app text rendered, no
Chrome error page) — landing on `/login`, because the session probe needs the
API. Nothing financial is ever served from cache.

### Verification

- **Tests** — frontend suite **535 passed / 1 skipped** (55 files). New:
  `tests/pwaManifest.test.ts` starts a real HTTP server over `public/` and
  follows the document link → manifest → every icon (status, content type,
  PNG magic, IHDR = declared `sizes`), pins the SW contract (no `/api`
  caching, offline navigation fallback) and the registration wiring;
  `tests/m72Mobile.test.tsx` covers drawer open/close (toggle, close button,
  scrim, Escape), the desktop rail + `z-40` token, `TableScroller` engaged in
  a 390px box, and the 44px / `env()` source contracts;
  `tests/pullToRefresh.test.tsx` covers the threshold, horizontal-drag and
  top-of-page gating, the disabled state, and reduced motion.
- **Tests, re-run with the QA F1/F2/F3 fixes** — **597 passed / 1 skipped**
  (64 files), `tsc` ✓, `eslint --max-warnings 0` ✓, `vite build` ✓,
  `npm run check:csp` ✓. `tests/m72Mobile.test.tsx` gained four tests
  (12 in that file) — three F1 regressions: Escape on the **bubbling input
  path** from the focused element, Escape with a **`flushSync` render landing
  inside the same dispatch** (the QA capture's interleaving), and **exactly
  one** `keydown` registration while mounted; plus the F3 source contract
  (`#app-nav` selectors inside the `< 768px` block, and nowhere outside it)
  and a pin that a **focused-but-empty** search field does not swallow the
  press (the ref only reports a layer open when it is actually on screen).
  RED first: on the pre-fix `AppShell`/`dialogA11y` the file reports
  **2 failed | 9 passed** (mid-dispatch render leaves the drawer open; 3
  registrations for 1 listener); with the fix **12 passed**.
- **Lighthouse 11.7.0** (v12+ removed the PWA category, so the milestone is
  measured on the last release that still has it), run against the **deployed
  stack** (`http://127.0.0.1:18080/login`, 2026-09-30): **PWA 100**,
  **accessibility 100** — `installable-manifest`, `maskable-icon`,
  `splash-screen`, `themed-omnibox`, `content-width`, `viewport` all pass, 0
  failing a11y audits. Runs land on `/login` (Lighthouse cannot hold the demo
  session), so the app pages are covered by the responsive audit above and by
  the behaviour tests rather than by a second full-page scan.
- **axe-core (channel A) was attempted twice and did not complete**: the shared
  `scripts/a11y` driver opens its CDP socket with a hard 60s read timeout, and
  `axe.run` on this box (4 cores, shared with sibling agents) overran it both
  times — `WebSocketTimeoutException` before the first page's JSON came back.
  Recorded as a residual rather than reported green; Lighthouse's accessibility
  category (which is axe under the hood) did complete at 100.
- **Gates** — `tsc --noEmit` ✓, `eslint --max-warnings 0` ✓, `vitest` ✓ (56
  files, **549 passed / 1 skipped**; `tests/pwaManifest`, `tests/m72Mobile`,
  `tests/pullToRefresh` all green), `vite build` ✓ (entry 487,894 B / 61
  chunks, well inside the ≤ 700,000 B target), backend `ruff check` ✓
  `ruff format --check` ✓ `pytest` ✓
  (**947 passed** in 427s, run against a dedicated `fintrack_m72` database
  through the example.internal tunnel — the suite needs its own database, it must never
  point at a sibling's).
- **CI** — the push (`215fe19`, cherry-picked onto M75's main) ran the
  repository `CI` workflow on `main`: **success**.
- **Evidence** — committed beside the harness in
  `frontend/scripts/m72/`: `responsive_audit.py` (now with the drawer pass,
  the trusted-Escape check and a failure record that survives an unready
  page), `m72-audit-390.json`, `m72-audit-768.json`, `m72-audit-1280.json`,
  `preview_with_csp.py` (serve `dist/` with the Dockerfile's own CSP header +
  an `/api` proxy, so header-dependent claims are checkable before a deploy),
  `csp_console_probe.py` (the 2-per-load → 0-per-load proof),
  `f2_theme_bootstrap.py` (the blocked-bundle light-first-paint proof),
  `drawer_escape_probe.py` (the three-focus-position Escape check),
  `m72-pwa.json`.
  Screenshots: 48 (6 pages × 2 themes × 3 widths + 12 open-drawer) delivered
  with the task, captured from this build, plus
  `docs/assets/m72/drawer-dashboard-{dark,light}-390x844.png` committed as the
  F3 row-height evidence.

- **M114 — deploy-sim A/B in a real Chrome** (`scripts/m114/stale_shell_probe.py`
  → `scripts/m114/m114-stale-shell.json`): build **A** is `origin/main` at
  `096138d` (its `index-ClU-gcha.js` / `CommandPalette-BsjooXzr.js` are the
  hashes prod was serving), build **B** is A plus this fix
  (`index-CyiLHR8G.js` / `CommandPalette-YZ9uTooi.js`, `CACHE_NAME
  fintrack-shell-v1-b121b3e9149c` — the id IS `sha256(index.html)`, so it
  moves with the shell, not with the clock). The probe loads A, signs in as
  the demo user through a proxied `/api`, re-points the origin at B — that IS
  the deploy — and opens the palette:
  - `before` (pre-fix worker): **0/3** — every run died on
    `Unexpected Application Error … CommandPalette-BsjooXzr.js` (2-3 console
    errors), nothing reloaded, and `caches` stayed `["fintrack-shell-v1"]`;
  - `after` (stale shell, worker updated the way any client's next navigation
    updates it — the `fintrack:reload` message has no listener in a pre-fix
    shell, so the reload MARKER carries it): **3/3 healed** — entry
    `index-ClU-gcha.js` → `index-CyiLHR8G.js`, `fintrack-stale-reload-at`
    written, palette opened on the new shell, `error_after_reload: false`;
  - `after-new-shell` (fixed shell + worker, message door live): **3/3 healed**
    with the same numbers.
  Screenshots `scripts/a11y/m114-{before,after,after-new-shell}-0.png`. Runs
  where the error UI flashed BEFORE the heal are recorded as `error: true`
  next to `healed: true` — that flash is the 404 itself, and it is exactly
  what the pre-fix runs keep forever.
- **M114 — live, prod A/B** (`scripts/m114/live_check.py`, same probe run
  against `https://fintrac.princessandmark.com` before and after the deploy;
  records `scripts/m114/m114-live-before.json` (pre-deploy, 1440×900),
  `m114-live-after.json` (1440×900) and `m114-live-after-mobile.json`
  (**390×844**, the mobile-PWA-ish run — the header magnifier trigger, M95 P1);
  screenshots `scripts/a11y/m114-live-before.png`,
  `m114-live-after-1440x900.png`, `m114-live-after-390x844.png`):

  | probe | before deploy | after deploy |
  | --- | --- | --- |
  | shell entry | `index-ClU-gcha.js` | `index-D5rUDzcF.js` |
  | worker caches | `["fintrack-shell-v1"]` — never rotated | `["fintrack-shell-v1-aad525685e9c"]`, equal to `sha256(index.html)[0:12]` of the live document |
  | `fetch("/assets/CommandPalette-DMeurLAO.js")` **through the worker** (the URL from the bug report) | **404** `text/html`, no marker | **200** `text/javascript`, marker body `/* M114 stale-shell self-heal…` |
  | `GET /index.html` (`/login` and `/dashboard`) | `no-cache, must-revalidate` | unchanged |
  | ⌘K on the current shell | opens, 0 console errors | opens, 0 console errors (same at 390×844) |

  The server still has no `CommandPalette-DMeurLAO.js` (curl: 404) — the heal
  is client-side by construction, so the A/B is measured through the worker.
  Post-deploy battery: `scripts/smoke.sh` → **SMOKE_DONE**, login burst hits
  429 at attempt 10 (expect ≤ 11). What is NOT measured here: the OS-level
  cold-resume of a home-screen PWA (no navigation event at all) cannot be
  driven from headless Chrome — that path delivers no new JavaScript either
  way, and the deploy-sim's stale-document scenario (document in memory,
  assets replaced underneath it) is the condition the heal actually covers.

- **M114 tests** — `tests/m114StaleChunkSelfHeal.test.ts` (**14**) executes the
  real `public/sw.js` in a `node:vm` worker over a swappable origin: the 404 →
  marker + fresh shell + client broadcast, NO heal when the origin has no newer
  shell, cache-first for served chunks, the entry precache + offline
  navigation, `/api` never intercepted, BUILD_ID stability/rotation with the
  activate purge, and the marker's bound (reloads once / renders nothing while
  the client half's navigation is in flight / throws once suppressed).
  `tests/pwaStaleShellClient.test.ts` (**5**) drives `src/pwa.ts` in jsdom:
  message door, stale-shell-only dynamic-import heal, current-shell non-heal,
  dev-document non-heal, guard window.
- **M114 gates** — `tsc --noEmit` ✓, `eslint --max-warnings 0` ✓, `vitest run`
  **1027 passed / 1 skipped** (98 files) ✓, `vite build` + `stamp-sw` ✓,
  `npm run check:csp` ✓; backend `ruff format --check` ✓ `ruff check` ✓
  `pytest -q` **1319 passed** in 722 s (12:01). Frontend-only change: no
  backend file,
  no migration, no enum/contract touched outside this section.

### Residuals & deviations

- **Docker is not installed on this LXC** (same documented deviation as the
  M64/M68/M69 runs) — the rebuild runs on **example.internal**, where the checkout is at
  `215fe19` and `fin_tracker-web-1` was recreated 2026-09-30 10:33 EDT
  (running, healthy), so the deployed stack carries M72. The smoke therefore
  goes through the **real nginx** (`http://127.0.0.1:18080`, LAN tunnel), not
  a local static server: manifest + all four icons `200` with the right
  content types, `sw.js` registered and controlling after reload, offline
  navigation renders the app shell (no Chrome error page), pull-to-refresh
  drives a real refetch (19 → 35 `/api` calls), and the served entry hash
  `index-Dc8Qbt5v.js` is identical to a local `npm run build` at this SHA.
  CI runs the real build as well.
- **Reports at exactly 768px** — the report-tab bar has 2 buttons past the
  right edge in both themes (4 escapes total across the 12 runs at 768).
  Outside this milestone's surface: M72 rules apply `< 768px` (where Reports
  measures 0 escapes), `Reports.tsx` is untouched and the `≥ 768px` shell path
  is unchanged — so it is pre-existing, and left for a follow-up card.
- **Controls < 44px at `≥ 768px`** are by design: the card forbids desktop
  layout changes, so desktop keeps M71's measured metrics (WCAG 2.5.8's 24px
  AA floor is unchanged there).

## Scroll performance — no `backdrop-filter` below md (M101)

**Report:** Mark, on Android Firefox — *"really slow to scroll"* on his daily
surface. **Cause, measured (not guessed): `backdrop-filter`, not translucency.**

**Rule (this is the contract):** at `≤ 767.98px` **no element may carry a
`backdrop-filter`**. One unlayered, `!important` block in `src/index.css` kills
both spellings for `*`, so a `backdrop-blur-*` utility added later cannot
quietly reintroduce a per-frame blur; `.glass` / `.glass-strong` get
`isolation: isolate` in the same block because their filter is what opened
their stacking context (§Sidebar stacking context — behaviour is unchanged,
only the paint cost is). The fixed mobile header is now **`bg-surface-raised`
(opaque)** instead of `/80`: with no blur behind it, an 80% alpha would ghost
scrolling rows straight through it (that element is `md:hidden`, so this is a
mobile-only edit). Every other alpha (`/70`, `/80`, `/95`, `--glass-bg`) stays
exactly as it was — **alpha measured free, blur is the cost**, so tokens,
borders and the contrast-audit pairs are untouched.

**Advisory:** *glass blur is disabled at ≤768px viewports for scroll
performance and kept on desktop* — the signature glass surface, the `md+`
rail and every `≥768px` rendering are byte-identical to before (screenshot
proof below).

**Measurement** (`frontend/scripts/perf/scrollbench.py`, method + full tables in
docs/34 §Scroll-frame budget): Firefox 153.0esr, mobile viewport @ DPR 2.6,
software rasterisation (this host has no GPU), demo tenant, one scripted
top→bottom→top sweep of the whole document at 1400 px/s per run, median of 3:

| page | long frames (>50 ms) before | fps before | after | fps after |
| --- | --- | --- | --- | --- |
| `/transactions` | 26 | 43.4 | **0** | **59.1** |
| `/dashboard` | 27 | 25.1 | **0** | **59.1** |
| `/investments` | 35 | 22.4 | **0** | **59.0** |

**A/B, one suspect at a time** (why the rule is blanket rather than
surface-by-surface): on `/transactions`, dropping `.glass`'s blur alone took
the sweep to 2 long frames while making `.glass` opaque alone changed nothing
(25), and dropping the fixed header + drawer blur alone changed nothing (26).
On `/dashboard` each of those two groups was *independently* enough to keep the
page at 26 long frames — only all of them together reach 0. Chromium 154 at the
same viewport dropped no frames either way, but its compositor draw wall-time
for one sweep fell **4025 → 274 ms (−93%)**, so the work is real on both
engines; Firefox is simply the one that shows it as lost frames.

**Visual regression:** `docs/assets/m101/mobile-dashboard-{before,after}.png`
(dark) and `…-light-{before,after}.png` are the pre-M101 and post-M101
captures of the same page at 412 × DPR 2.6. The full 16-shot set (both themes ×
`/dashboard` + `/transactions` × 412 and 1440, before/after) is attached to the
M101 card with the raw JSON: mobile pixels differ **only** by the frost —
re-injecting the removed blur into the new build reproduces the old build's PNG
**byte-for-byte**, all 4 theme×page combinations — and desktop before/after
differ by **less than the same-build capture noise floor** (0.10 % / 0.23 % /
0.16 % / 2.31 % changed vs 0.07 % / 0.23 % / 0.16 % / 2.44 % capture-vs-capture)
— i.e. desktop is unchanged.

**Measured residual, deliberately not shipped:** desktop `/dashboard` still
loses 7–10 of ~120 frames per sweep with glass on this (GPU-less) host. That is
a desktop-aesthetic trade this card did not take: the advisory line above is the
shipping scope, and widening it is a one-line change if Mark ever reports
desktop jank.

**Tests:** `tests/m101ScrollPerf.test.ts` pins the four properties that make
the fix hold in the built CSS — the kill rule exists for both spellings, it is
**unlayered** (a layered rule would lose to Tailwind's own utilities and do
nothing), every `backdrop-filter: none` in the sheet sits inside a
`max-width: 767.98px` block while `.glass` still declares `blur(14px)` and
`background: var(--glass-bg)` for wider views, `.glass` keeps
`isolation: isolate`, and the fixed header carries an opaque
`bg-surface-raised` with no `backdrop-blur`.

## Narrow-viewport card layout (M82-F3)

The M82 mobile sweep measured `/imports` overflowing `main#main-content`
(`overflow-x: clip`) at ≤360px: the `Upload → Review → Done` stepper ran 18px
past the viewport (`3 Done` cut off) and the YNAB file field sat 2px past it and
39px past its own wrap row. Two reusable rules now own that class of defect:

- **`CardTitle`'s title row wraps.** `components/ui/Card.tsx` renders
  `mb-3 flex flex-wrap items-center justify-between gap-3`. The `right` slot
  (stepper, help hint, counters) has a min-content floor that a `nowrap` row
  cannot shrink past, so when title + slot exceed the row the slot drops to its
  own line instead of pushing the row out of the card. Where the pair already
  fits (desktop, and any width ≥ ~416px on `/imports`) nothing changes.
- **Intrinsic-width fields declare their own floor.** `<input type="file">` is
  ~325px wide intrinsically, and inside a flex row that width *is* the parent's
  min-content — so a file input in a wrap row must be
  `min-w-0 w-full max-w-<n>` (YNAB migration card, statement upload). The
  intrinsic size then caps at `max-w-*` and shrinks with the row; a bare file
  input drags its whole row past the clipper.

**Contract** (browser-measured with the M82 CDP harness — `m82/detail.py` +
`m82/probe.js`, 360×800 and 320×800, Pixel 10 UA, dark + light, both Import-mode
tabs): 0 nodes overflowing the viewport outside a scroller, 0 child-overflows-
parent, `ol.flex.items-center.gap-2` right ≤ viewport width (281 ≤ 360 and
320), `pageHScroll=false`. jsdom has no layout engine, so these numbers come
from Chrome, not Vitest; the Vitest suite pins only behavior (597 passing).

## Fixed-width action rows (M82-F4)

The same sweep measured `/reports → Projections` at ≤360px: 5 nodes fully past
the right edge of a viewport with **no** horizontal scroll to recover them
(`document.scrollWidth == 360`, `pageHScroll=false`, clipper
`main#main-content`), one of them the icon-only **remove (trash) button** —
x=361..405 at 360, i.e. unreachable.

The scenario row in `components/reports/ProjectionsPanel.tsx` was a single
`flex items-center gap-2` line of *fixed-width* controls: colour dot (8) +
label input (`w-44` = 176) + return input (`w-24` = 96) + the `% / yr` unit
(~35) + a 44px remove button ≈ **391px**, while the card's content column is
286px at a 360px viewport and 248px at 320px. Negative free space went to
shrink: the `% / yr` unit collapsed to its 12px min-content and still overran
the row by 30px, and the button landed outside the clipper.

- **Rows of fixed-width controls wrap; they never decide what fits.** The row
  is `flex flex-wrap items-center gap-2`. While it fits it is one line (from
  ≈463px up: 391px of content + the card's `p-4` + the page's `p-5`); below
  that it breaks so the label input keeps line 1 and the **return input, its
  unit and the remove button move together** onto line 2 — the action stays
  beside the field it belongs to. Same rule `CardTitle` already follows
  (M82-F3): when min-content cannot shrink, give the line a break point.

**Contract** (CDP-measured with the M82 harness against the dev server and,
after deploy, the running stack — 360×800, 320×720 and 412×915, Pixel 10 UA,
`Projections` tab active): `fullyOffRight == 0` at every width,
`document.scrollWidth == innerWidth` and `pageHScroll=false` (nothing trades
the clip for a horizontal scroll), the remove button's centre hit-tests to
itself (`elementFromPoint` → the button or its own SVG), and clicking it still
removes the scenario row (1 → 0). At 412 the A/B against unmodified code moves
the button's right edge 405px → 124px and the occluded-interactive count
3 → 2. jsdom has no layout engine, so those numbers come from Chrome;
`tests/m82ProjectionsRow.test.tsx` pins the source of the fix (every rendered
scenario row carries `flex-wrap`, the widths stay fixed, no `flex-nowrap` in
the file).

## Android 200% root font (M82-F2 200% residual)

Android's **maximum** font scale makes the root font 32px (200% of 16) and
leaves the viewport at 320–412 CSS px, so every rem-based floor grows while the
screen does not: card `p-4` becomes 32px a side, form `p-3` 24px, the 12-col
gutter 16px per column, the `Enabled` pill 128 → 197px, the word
"Categorization" 168 → 241.7px. M82-F2's acceptance only pinned 16/18.4/20.8px
roots (100/115/130%), so the 200% case was measured separately on `/rules` at
commit `1761282`: `rules_probe.py --fonts 32` reported **320 → `clipped=2
parentOver=5`** (the `CardTitle` h2 42.7px past the viewport, 115.7px past its
row; the prefill box 93.5; `Enabled` 73; `Stop here on match` 51.9; the submit
row 16.6), 360 → 2/4, 412 → 0/2, 768 → 0/1 (the Priority `?` 3.3px outside its
label span), 1024/1280 clean.

Every one of the five is a **floor that scales with the root font**, and each
fix lowers the floor instead of moving the box:

- **`CardTitle`'s h2 may break a word** (`components/ui/Card.tsx`, shared — the
  title lives there, not in `/rules`): `<h2 class="text-sm font-semibold
  tracking-wide text-text [overflow-wrap:anywhere]">`. The h2 is a flex item, so
  an unbreakable word *is* its min-size: icon 32 + `gap-2` 16 + "Categorization"
  241.7 = 289.7px against a 174px card row at 320 — the row could not shrink it,
  so it stood outside the viewport where `main { overflow-x: clip }` cut it.
  `anywhere` lowers min-content to one character (Tailwind 3 has no
  `wrap-anywhere`; `break-words` would NOT work — it does not affect
  min-content), so the row shrinks the h2 and the text breaks only when a line
  genuinely cannot hold the word. Where a line does fit, the break never
  engages: 1024/1280 keep byte-identical rects.
- **The form keeps one padding step below `sm`** (`Rules.tsx`, its own `<Card>`
  and `<form>`): card `p-4` → `p-3 sm:p-4`, form `p-3` → `p-2 sm:p-3`. rem
  padding scales too, so at 320/200% the form column was 124px — narrower than
  the prefill label's own longest word ("transaction", 142.7px) *and* narrower
  than the submit row (140.6px). One step each gives 156px, which holds both
  with room to spare; `sm:` (640px) keeps ≥640 exactly as it was, so the ≥640
  geometry is untouched.
- **`Button` can be told its label may wrap** (`components/ui/Button.tsx`): a
  new `wrap?: boolean` (default `false`) swaps `whitespace-nowrap` →
  `whitespace-normal` **and** `h-7/h-9/h-11` → `min-h-7/min-h-9/min-h-11` of the
  same value — a wrapped label is two lines tall, and a fixed height would
  print the second line outside the button. It is opt-in: without `wrap` the
  emitted class list is byte-identical to before (the submit button still ships
  `whitespace-nowrap` + `h-7`). `/rules`' prefill **Use as pattern** button is
  the one `wrap` caller: its min-content was label + padding = 217.5px at root
  32 against a 156px line, which is what pushed the whole prefill box out of
  row 3.
- **`Toggle` pills wrap their label** (`components/ui/Toggle.tsx`, shared):
  `inline-flex … flex-wrap`. The pill's min-content is switch (80) + `gap-2.5`
  (20) + label (97 for `Enabled`, 75.9 for the Stop label) = 197px against a
  156px cell; with `flex-wrap` it becomes the *wider* of switch and label
  (97px), so the label drops under the switch. A wrapping flex line sizes
  exactly like a nowrap one while everything fits, which is why 768/1024/1280
  do not move a single rect.
- **`HelpHint`'s gap is replaceable** (`components/ui/HelpHint.tsx`, shared): a
  new `className = "ml-1"` prop replaces *only* the margin (the rest of the
  classes stay the component's), because two utilities for `margin-left`
  resolve by stylesheet order — a leftover `ml-1` would beat `ml-0.5` silently.
  `/rules`' Priority hint passes `ml-0.5 lg:ml-1`: at 768/200% the Priority
  cell is 2 of 12 tracks = 36.7px (the gutters alone are 11 × 16), so the
  default `ml-1` + the 32px box stood 3.3px outside the label span, while 4px
  still separates the `?` from the word and `lg:` leaves 1024/1280 alone.

**Contract** (CDP-measured with `frontend/scripts/local/rules_probe.py` against
a production build served by `frontend/scripts/local/serve_proxy.py`, Pixel 10
UA):
- `--viewports 320,360,412,768,1024,1280 --fonts 32 --themes dark,light` →
  **12/12 runs `clipped=0 parentOver=0`**, `?` inside its parent in every run,
  `Enabled.right ≤ viewport-8` (238/312 at 320, 543/760 at 768).
- the inherited matrix `--viewports 412,400,393,384,375,360,344,320,768,1024,1280
  --fonts 16,18.4,20.8 --themes dark,light` → **66/66 PASS**, i.e. the 48 mobile
  runs M82-F2 pinned are still `clipped=0 parentOver=0`.
- **≥640 is untouched**: every field of the 1024/1280 runs (`vw`, `docScrollW`,
  `rootFont`, `clippedCount`, `parentOverflowCount`, `grid` incl. all five
  children's `x/right/w/clientW/scrollW`, `toggle`, all four `hints`) is
  byte-identical to `m82res/out/font32_probe.json` at root 32 *and* to
  `m82res/out/final_acceptance.json` at 16/18.4/20.8. The only ≥640 delta
  anywhere is the Priority `?` at 768 (right 634 → 632 at 16px root, i.e. more
  slack: `overRight` −0.6 → −2.6).
- still true at 200%/320 and accepted as the floor of this fix: the title
  breaks mid-word (`Categoriz` / `ation rules`) because no layout fits a
  241.7px word in a 174px row — the text is fully visible rather than clipped;
  input placeholders ellipsize inside their box; the rules table keeps its own
  `overflow-x-auto` scroller. Full reflow at max font scale remains the
  separate card noted under **Max font scale (M82 F7)**.

Vitest pins the source of all five (`tests/rules.test.ts` → "Rules 200%
font-scale contract (M82-F2 residual, root 32)", plus `tests/helpHint.test.ts`
for the replaceable gap); jsdom has no layout engine, so the numbers above come
from Chrome.

## Tag UX (M104)

**Context — tagging was the slowest common action on the page.** Tags are the
primary organizing tool for import-heavy users, yet adding one lived entirely
inside the edit modal: open the editor → find the Tags row → focus the picker →
type → Enter (4–5 interactions). The list's own tag chips were read-only 10px
pills, the filter bar offered a single-tag equality `<select>` next to rows that
carry *several* tags (a HelpHint explained the mismatch instead of the UI
resolving it), the Tags section sat last in the modal before the fold, and a
typo'd tag had no fix anywhere: `PATCH /tags/{id}` renames into an existing name
and answers 409 `duplicate_tag`.

**Approach — five surfaces, one picker.**

1. **Row-level quick-tag.** The Tags cell of every `/transactions` row carries a
   `+` button (`title="Add tags"` + `aria-label="Add tags — <payee> (transaction
 <id>)"` — the payee alone collides the moment two rows share one, so the id
 names the target exactly the way `Edit transaction N` does, M92;
 `min-h-11 min-w-11` under `md`, so the hit area is ≥44px on a phone). It opens `QuickTagPopover` — a portal anchored to the
   button's rect with `position: fixed`, clamped horizontally
   (`left = clamp(8, rect.left, innerWidth − 272 − 8)`) and flipped above the
   trigger when the space below it is under 240px, so the popover can never
   leave a 390px viewport. Focus moves into it and returns to the button when it
   closes (the shared `useDialogFocus` / `useEscapeToClose` helpers); any scroll
   outside it (capture phase — the table's own scroller counts) or a resize
   closes it, so a stale anchor cannot survive a scrolled row. It renders the
   SAME `TagPicker`, writes through the same
   `PUT /transactions/{id}/tags` the editor uses, then invalidates
   `transactions` + `search` and closes. **Acceptance: click → type → Enter = 2
   interactions** (was ~5).
2. **The picker is now a combobox** (`TagPicker.tsx`, mechanics kept):
   `role="combobox"` on the input (`aria-expanded`, `aria-autocomplete="list"`,
   `aria-activedescendant`), `role="listbox"` on the scrolling list and
   `role="option"` on every row. ArrowDown/ArrowUp move a virtual highlight
   (wrapping), Home/End jump to the ends, Enter attaches the highlight — or,
   with no highlight, the exact match / the create row exactly as before, so
   typing a full name + Enter is unchanged. A new query clears the highlight, so
   Enter never silently picks a *different* tag than the one typed. Mouse
   behavior is unchanged (`mousedown` attaches, which is what keeps the input
   focused). Size: `min-w-56` input, `w-64` list, `text-sm` (was `w-40` / 11px).
   While the query is empty the list is ordered by session recency
   (`src/lib/tagRecency.ts` — a module-level stack written on successful
   attach/create, never persisted, reset between test runs via
   `resetTagRecency()`).
   The `id` sits on the SCROLLING listbox rather than its wrapper: axe's
   `scrollable-region-focusable` exempts a combobox popup by finding
   `[role=combobox][aria-controls~=id]` on the scrollable node itself, and a
   listbox of non-focusable options would otherwise be a serious violation while
   it overflows.
3. **Escape nests (docs/09's layering rule).** With the list open, Escape closes
   only the list — the handler stops propagation, so the editor modal behind it
   stays up. With the list closed the key bubbles, and the modal closes on the
   second press. Inside the popover the same two-press order applies (list,
   then popover).
4. **Modal promotion.** The Tags block moves ABOVE `Raw description`, gets the
   section label **"Tags — as many as you like; category stays single"**, renders
   its chips and picker on separate lines with the picker at `w-full` (input and
   list), and keeps the `role="alert"` tag error.
5. **Multi-tag filter that matches the rows.** The single-tag `<select>` (and
   its HelpHint) is replaced by `TagFilterPicker` — the same picker in filter
   mode: removable chips for the selection, no inline-create, and a footer entry
   **"Manage tags…"**. The label prints the semantics where the choice is made —
   `Tags · matches all selected` — which is what
   `GET /transactions?tag_ids=…` implements: one repeated param per id, every id
   AND-composed, each ownership-checked (docs/08). The draft state is
   `tagIds: string[]`; `?tag=` in the URL now accepts `12` or `12,14`.
6. **Management modal** (`TagManagerModal.tsx`, opened from that footer):
   every tag with its usage (`transaction_count` / `rule_count` from
   `GET /tags`), inline rename through `PATCH /tags/{id}`, delete ONLY for an
   unused tag (an in-use tag shows the count instead of a button — the 409
   `tag_in_use` guard stays the backstop), and merge: checkboxes select ≥2 tags,
   a `Keep as` select names the survivor, and `POST /tags/merge` folds the rest
   into it — the fix for "Goceries" + "Groceries" both existing. The scrollable
   list is a named, focusable `role="region"` (the M100/M102 scroll contract).

**M113 — the 409s render their friendly copy off `code`.** The Tags page
(create / rename / delete) and this modal (rename / delete) answer
`409 duplicate_tag` / `409 tag_in_use` with friendlier copy than the server's
human `detail` ("A tag with that name already exists.", "That tag is in use —
remove it from its transactions or rules first."). The branch keys off
`hasErrorCode(err, …)` — the envelope's `code` field — because
`ApiError.message` is always the `detail`, so the pre-M113
`err.message.includes("duplicate_tag")` test could never match and the raw
`detail` shipped instead (docs/08 §Error envelope). Both directions are pinned
in `frontend/tests/m113DetailVsCode.test.tsx`.

**Focus-trap deviation, stated.** The PM brief asked for a focus trap here; the
modal uses the shared dialog helpers instead — focus in on open, Escape, focus
returned to the opener — because docs/09's keyboard contract says dialogs are
deliberately NOT traps and every focusable surface must keep advancing with Tab.
AGENTS §0 resolves that in favour of the numbered doc.

**Touch points.** `frontend/src/components/transactions/` — `TagPicker.tsx`
(attach + `TagFilterPicker`), `QuickTagPopover.tsx`, `TagManagerModal.tsx`,
`TransactionEditor.tsx` (section order); `frontend/src/pages/Transactions.tsx`
(filter bar, row trigger, quick-tag mutations); `frontend/src/lib/tagRecency.ts`;
`frontend/src/api/tags.ts` (`TagWithUsage`, `mergeTags`),
`frontend/src/api/transactions.ts` (`tagIds`); backend
`app/api/tags.py` (usage counts + `POST /tags/merge`), `app/api/transactions.py`
(`tag_ids`), `app/schemas/tags.py`.

**Verification (M104).**

- `backend/tests/test_tags.py` — 10 tests: the pre-existing attach/replace/
  pagination contracts plus usage counts, merge (junctions move, a row carrying
  both sides keeps one link, `rule_tags` re-pointed, sources 404 afterwards,
  self/empty → 422, foreign → 404) and `?tag_ids=` AND.
- `frontend/tests/m104TagUx.test.tsx` — 14 tests: arrow navigation +
  `aria-activedescendant` over `role=listbox/option`, Enter on highlight and on
  exact match, recency order, the two-press Escape nesting inside the editor,
  the Tags-before-description order and `w-full` picker, quick-tag
  (click → type → Enter writes `PUT` with the row's existing ids + the new one
  and closes), per-row unique trigger names, the filter chips driving
  `tagIds` into the list query, "Manage tags…" opening the modal, filter-mode
  selection without a create row, and the manager's counts / gated delete /
  rename / merge payload.
- `frontend/tests/focusRing.test.ts` carries two documented
  `outline-none` exemptions (the popover and manager surfaces are
  `tabIndex=-1` `useDialogFocus` targets, same category as `SimilarExamples`).
- **Against the deployed stack** (`http://127.0.0.1:18080` — example.internal nginx through
  the LAN tunnel; the served `index-*.js` hash matched this branch's own
  `npm run build`, 2026-10-05):
  - `scripts/a11y/m104_tag_probe.py` → **19/19 checks at 1440 AND 412**
    (`m104-tag-probe-1440.json`, `m104-tag-probe-412.json`), each on a throwaway
    tenant the probe deletes afterwards (204 — the demo tenant answers 403 on
    every tag write): ONE `PUT /transactions/{id}/tags` from click → type →
    Enter with the chip landing and no dialog opening; the `+` trigger measures
    **28×28 at 1440 and 44×44 at 412**; the open popover sits **inside** the
    viewport at both widths (1440: 1035–1307 of 1440; 412: 132–404 of 412);
    editor Escape #1 flips `aria-expanded` true → false while the dialog stays,
    #2 closes it; the filter sends `tag_ids=…&tag_ids=…` and keeps only the row
    carrying both; the manager prints `1 transaction`, renames onto the row chip,
    and `Merge 1 into …` folds the selection; **0 console/CSP errors**.
  - The same probe runs axe on the **open** surfaces (a closed-page scan cannot
    see them): quick-tag popover **0/0/0/0**, manager **0/0/0/0**, editor with
    the picker open **0 critical / 0 serious** (one pre-existing `aria-allowed-role`
    *minor* on the editor's `<form role="dialog">`, untouched by M104).
    That run is what caught `aria-required-attr` on the expanded combobox (the
    id now always points at a rendered popup) and the unlandmarked popover (now
    a named `region`).
  - Channel A sweep: `axe_scan.py` at **1440, 390 and 412 × 2 themes × 6 pages =
    36 runs, 0 violations of any severity** (`m104-axe-1440.json`,
    `m104-axe-390.json`, `m104-axe-412.json`).
  - The same probe, pointed at the **public origin** (`https://fintrac.princessandmark.com`,
    the bundle hash identical to the tunnel's): **19/19 at 1440 and 19/19 at 412**
    (`m104-tag-probe-prod-{1440,412}.json`), and `axe_scan.py --width 1440`
    there too — **12/12 runs, 0 violations of any severity, both themes**
    (`m104-axe-prod-1440.json`).
  - Channel C: `keywalk.py` → **`failures=0`** — skip link first on all six
    pages, **434 stops on `/transactions`** (M103: 385 — one new stop per row
    for the `+` button), 0 stops without a focus indicator, 0 traps, 0 running
    animations under `prefers-reduced-motion`, `dialog=pass` on Transactions
    (`keywalk.json`). The walk's `STOPS` backstop moved 400 → 600 for that same
    reason: the cap, not the page, was failing.

## Review queue & activity log (M106)

**Context — the trust feature.** After Monarch's 11k-edits/day data-loss
incident, their users demanded proof that an edit survives. `/transactions`
answers with two paired surfaces: a **review queue** (which rows still need a
human) and a **History drawer** (what happened to this row, in order, forever).
The rule both are built on: *edits never silently vanish; every change is a
receipt* (docs/02 §transaction_events).

### Row affordance — the review toggle

A **second column**, immediately right of the selection checkbox, holding one
toggle per row: an outlined `Circle` when the row is unreviewed, an accent
`Check` on an accent-tinted chip when it is reviewed.

- **No modal, one click.** `POST /transactions/{id}/review` and back — the
  same route the bulk toolbar drives, so the row and the batch can never
  disagree about what a flip means.
- **`aria-pressed` + a per-row label**: `Reviewed — <payee> (transaction N)`.
  The name is constant (the toggle-button contract: name describes the
  control, `aria-pressed` describes the state) and the `title` carries the
  action in words — "Needs review — click to mark it reviewed". The label
  names ITS row, so two rows sharing a payee never read alike (M92).
- **Touch floor**: the button itself is `min-h-11 min-w-11` below `md` and
  `md:min-h-7 md:min-w-7` at `md+`, the same shape the quick-tag button uses.
- The header cell is `sr-only` "Reviewed": a visible fourth one-word column
  would compete with Date/Payee/Amount, and every cell already announces
  itself.

### The queue — a chip-family filter shortcut

Beside the Quick range control sits a **`Needs review` chip** with the live
count in a pill.

- It is a FILTER shortcut, not a field: pressing it applies immediately
  (`setDraft` + `setApplied` + reset pagination) instead of waiting for
  "Apply filters", and it moves ONLY its own key — a half-typed payee beside
  it is not silently submitted along for the ride.
- `aria-pressed` marks the on state; the count pill disappears at zero
  (nothing to queue is not a number worth printing).
- It is an ordinary AND-composed filter underneath (`draftToFilters` →
  `?reviewed=false`), so every other filter still applies on top of it.

### The header hint — "N unreviewed"

A small accent pill next to the `Transactions` heading while
`unreviewedCount > 0`, carrying `role="status"` on the number so the count is
announced when it appears.

- **Dismissible per session**: the × writes `sessionStorage` under
  `fintrack.m106.unreviewed-hint.dismissed.<scope>` (`<scope>` is
  `tenantScope(user)` — `lib/tenantStorage.ts`, security follow-up F-6: one
  tenant's dismissal never swallows another's hint in the same browser), so
  it is gone for the rest of
  this tab and back the next time there is work waiting. It never nags a
  user who has already waved it off, and it never shows at zero.
- **One number, two surfaces.** The hint and the chip both read
  `GET /transactions/unreviewed-count` over the SAME query params as the list
  — that is what makes "4 unreviewed" and the list you get after pressing the
  chip the same number. The endpoint ignores its own `reviewed` param, so
  asking while the queue is ON cannot cancel the count out. It is its own
  route (not a field on the page) because `GET /transactions` carries a
  5-query budget (docs/34 — 4 before M108 P4 added the batched receipt flag)
  and the hint needs the number with the filter off.
- **The count is read where the rows are read.** Client-side the query sits in
  the `transactions` cache family — `["transactions", "unreviewed-count",
  filters]`, beside `["transactions", "list"]` and `["transactions", "split"]`
  — so EVERY ledger invalidation (the row toggle, a bulk run on the M96
  toolbar, an inline edit, a delete, an import commit) refetches it together
  with the list. Keyed on its own it went stale the moment the ROWS moved: a
  bulk **Mark reviewed** left the hint and the chip printing the pre-run number
  over rows that had already flipped.

### History drawer

A right-side dialog (`components/transactions/HistoryDrawer.tsx`) listing one
receipt per line, **newest first**: date stamp, the server-rendered sentence
(`"Oct 5 — you set category Groceries"`, and since M120 a machine's own hand
shows as `"Oct 5 — the system set category Groceries (rule)"`), and a muted
`<kind> · <hash12>…` line whose `title` carries the full `payload_hash`.

- **Pinned sentences (M120).** The drawer never parses a payload — it prints
  whatever `summary` arrives, so the wording contract lives server-side in
  `services/tx_events.py::render_summary`. The lines that changed with M120,
  exactly as they render to the owner who did NOT do it (the same receipt read
  from the clicker's chair substitutes `you` for `the system`):
  - `the system set category Groceries (rule)` — layer 1 of the pipeline
  - `the system set category Groceries (learned from your edits)` — layer 2
  - `the system set category Groceries (assistant suggestion, auto-applied)` — layer 3 auto-apply
  - `you set category Groceries (rule backfill)` — `POST /rules/{id}/apply`
  - `you set category Groceries (accepted suggestion)` — `POST /suggestions/{id}/accept`
  - `you edited this transaction` — `POST /transactions/recurring/accept`, unchanged
    (a plain `edited`, no `via` key, so it renders exactly like a PATCH on the
    same columns)
  A receipt with NO `via` — every ordinary PATCH — still renders
  `you set category Groceries`, byte-identical to what M106 shipped.

- **Two entry points on every list that edits a row**, exactly as specified: a
  `History` button in the row's Actions cell, and a `History` button in the
  edit modal's footer. `/transactions` carries both, and so does the account
  activity list (`pages/AccountDetail.tsx` renders the same
  `TransactionEditor`) — a surface whose edit modal hid the link would be the
  one place a row's history is unreachable. The editor hands the row over and
  closes first — one dialog at a time, so focus lands in the drawer rather
  than behind both.
- **The sentence is rendered server-side** from `event_kind` + the stored
  `payload` (`services/tx_events.py::render_summary`), so rewording the drawer
  never needs a data migration and the payload stays the single source the
  line is drawn from. `actor_label` is computed per VIEWER: the same row reads
  "you set category …" to the person who did it and "Mark set category …" to
  anyone else, without storing the sentence twice.
- **The hash is shown, not explained**: `receipt <sha256>` in the `title`. The
  line is what happened; the hash is the proof it was written then.
- **One row's history, and nobody else's**: `GET /transactions/{id}/events`
  answers for that row of that owner only, so a neighbouring line in the same
  list, or another person's row entirely, can never reach a drawer — a foreign
  id is `404 not_found`, never an empty `200` that would read as "no history".
- Dialog contract is the shared `dialogA11y` one (focus in, Escape closes,
  focus returns, Tab keeps advancing — no focus trap). Empty state is a
  sentence, not `EmptyState`: the three-part preview contract's exceptions are
  a pinned set (docs/09 §Empty states).
- A deleted row's drawer 404s (there is no row left to open) — which is the
  whole reason the `deleted` receipt exists in the table and in
  `/export/full`: the event outlives its subject.

### Touch points
- `backend/app/api/transactions.py` (routes: `review`, `events`,
  `unreviewed-count`, the `reviewed` filter) — `get_txn_filters` /
  `_ledger_scope` are shared by the list and the count so the scope cannot
  drift.
- `backend/app/services/tx_events.py` — `payload_hash`, `jsonable`,
  `record_event`, `record_edit_event`, `actor_label`, `render_summary`.
- `backend/app/models/transaction_events.py`, migration `0039`.
- `frontend/src/api/transactions.ts` — `setTransactionReviewed`,
  `listTransactionEvents`, `getUnreviewedCount`.
- `frontend/src/components/transactions/HistoryDrawer.tsx`.
- `frontend/src/pages/Transactions.tsx` — review column, queue chip, header
  hint, both History entry points.
- `frontend/src/pages/AccountDetail.tsx` — the same two History entry points
  on the account activity list.
- `backend/tests/test_m106_review_events.py`,
  `frontend/tests/m106ReviewQueue.test.tsx`.
- `frontend/scripts/a11y/m106_review_probe.py` — the live probe (below).
- `frontend/scripts/a11y/m106_account_history_probe.py` — the same two entry
  points on the account activity list (below).

### Verification
`frontend/tests/m106ReviewQueue.test.tsx` (18): the row toggle's constant
label + `aria-pressed` + 44px declaration, its keyboard reachability and its
single-route flip; the queue chip applying `reviewed=false` with the count on
both surfaces, that count moving live after a single-row flip and behind a
bulk run; the header hint's sessionStorage dismissal and its silence at zero;
both History entry points opening the drawer with the receipts newest-first;
the drawer printing a unicode sentence (accents, emoji, CJK) verbatim rather
than escaped; a second row fetching its OWN history, so no cross-transaction
line can be shown; and the page contract — both list surfaces mount
`<HistoryDrawer>`, pass `onHistory` to the editor and carry the row's
`History` affordance.

`backend/tests/test_m106_review_events.py` (12): every mutation path writes
exactly one receipt (and every no-op writes none), two hand-recomputed
`payload_hash` digests including a unicode body, the queue filter/count/flip
liveness, import-batch seeding (`created from import Batch #N` + an unreviewed
queue), `/export/full` + ZIP manifest coverage, the partner projection
boundary (system receipt dropped, payload stripped, money absent), the four
transfer routes receipting every row they move (accept/detect/link/unlink,
and a no-op unlink writing none), the 422/429 pre-handler refusals writing
nothing, migration `0039`'s own up/down: both review columns NULLable
with no server default, the log and its four indexes present at head and gone
at `0038`, one linear alembic head, and a row written BELOW `0039` landing
`reviewed_at IS NULL` after the re-upgrade — the no-backfill criterion
measured on real data — plus the drawer's two acceptance clauses: one row's
receipts are disjoint from its neighbour's and a stranger's row answers 404,
and a unicode `category_name` reaches the `summary` (and the wire bytes) as
the exact characters the user typed. Lint, the backend suite,
`npx tsc --noEmit`, `npx eslint . --max-warnings 0`, `npm run test` and the
production build all stay green.

- **Against the deployed stack** (`https://fintrac.princessandmark.com`, the
  public origin, 2026-10-06 — `m106-review-probe.json`, run at the first
  deploy, again after the redeploy of `1efc4f6`, and a third time at final
  verification; the committed JSON is that third pass):
  `scripts/a11y/m106_review_probe.py` → **27/27 checks all three times**, on a
  throwaway tenant. Staged rows stay out of the count (3 manual rows ⇒ count 3)
  and a committed 5-row QFX import takes it to 8, with `?reviewed=false` total 8
  and `?reviewed=true` 0 — one predicate, two consumers. Each row's log opens at
  exactly one receipt: the manual one `source: manual`, the imported one
  `source: import` + its batch id (`you created this from import Batch #148`).
  `POST /review` drops the count 8 → 7 and appends a `reviewed` receipt (2
  total); un-flagging appends a third. `/export/zip` ships
  `transaction_events.csv` alongside the 39 other tables. In the UI the header
  reads **"8 unreviewed"**, the `Needs review` chip carries the same number,
  one toggle click takes it to **"7 unreviewed"** with `aria-pressed=true` and
  the flipped title, and the History drawer lists the sequence newest-first
  with its payload hashes — **0 axe critical/serious with the drawer open**.
- **Fourth pass, 2026-10-07, after the count-cache fix** (the committed JSON
  is this run): **29/29**. The probe grew two checks, one per acceptance
  clause the original 27 could not see.
  - *keyboard-only flip* — focus moves to an unreviewed toggle and one real
    Enter goes through CDP, mouse untouched; the row lands `aria-pressed=true`.
    (At the time of this pass the driver's plain `press()` sent `rawKeyDown`
    with no char, so Chrome ran no default action for Enter and the probe
    needed a local `press_enter()`. The driver is now fixed — `press()` sends
    `keyDown` + `text` for Enter/Space, and `press_regression.py` re-measures
    the three-way table on a `file://` page every run. Note the earlier wording
    here claimed Tab focus traversal also failed; it did not, and the
    regression harness now asserts that it moves.)
  - *bulk `Mark reviewed` empties the queue on BOTH surfaces* — select-all,
    toolbar run, `Reviewed 8 of 8`, and the "8 unreviewed" pill disappears
    from the header hint AND from the chip — which only happens because the
    count query rides the `transactions` cache family (above).
- **The account activity surface, 2026-10-07** — `scripts/a11y/
  m106_account_history_probe.py` → **12/12** against the public origin on a
  throwaway tenant (`m106-account-history.json`, screenshot
  `m106-account-history-drawer.png`). `/accounts/{id}` now carries BOTH entry
  points: the row's `History` control opens the drawer with THIS row's two
  receipts newest-first — `Oct 7 you set category Café ☕ 食品 😀` above
  `Oct 7 you created this transaction`, unicode verbatim, each line carrying
  `<kind> · <hash12>` — and the edit modal's `History` hands the row over so
  exactly ONE dialog is mounted and it is the drawer, arriving with the
  receipts already loaded rather than an empty shell. **0 axe critical/serious
  with the drawer open.**

## Split transactions (M107)

**Context.** One statement line often belongs in several budget categories
(`COSTCO #123 -187.42` is groceries, kids' clothes and a pharmacy refill —
docs/44). The register renders a split as **one group**: a header that carries
the statement, an expandable panel that carries the parts. Every surface that
renders ledger rows does it through the SAME component, so the grouping cannot
drift between the register, the account activity, the Dashboard and the
partner feed.

### Grouping — `SplitGroupRow`

`frontend/src/components/transactions/SplitGroupRow.tsx` exports
`groupSplitRows(items, split_parents)` (pure: LEDGER `items` + the page's
headers → render order) and `SplitGroupRow` (the markup). The rule:

- group by `parent_split_id`; the **header lands at the group's first
  occurrence** in the page and later children fold into it, ordered
  `id ASC`. Children on different pages simply don't show — `child_count` on
  the header still reports the true number of parts in the TABLE.
- the header renders the **parent's** date / payee / account / `amount_native`
  total, and the literal badge **`Split · N parts`** (`N` = `child_count`)
  **in place of the category chip**. The parent's own category, tags and note
  are never rendered while it is split — they exist only to come back on
  merge (docs/44 D5), so the header's category and tags cells are empty and
  the review-toggle cell is empty too (a header is not a ledger row).
- the header's checkbox toggles **every child of that group present in the
  page**, with an indeterminate state when only some are selected.
  *Recategorize* then works per child; *Delete* is refused per child by the
  server (`409 split_leg_immutable`) and lands in M96's existing
  `BulkOutcome.conflicts` channel, so the run reports `Deleted 7 of 9 — 2
  skipped` with one line per refusal instead of failing the batch.
- the header carries **no** inline category combo-box, **no** quick-tag
  popover, **no** `Edit` and **no** `Delete` — the affordance is hidden
  wherever the server refuses it (the same rule investment accounts get,
  docs/44 §12). Its action cluster is the **Expand** disclosure and **Merge
  back**; *Re-split* and a second *Merge back* sit in the open panel.
- expansion always **fetches `GET /transactions/{id}/split`** on the owner's
  surfaces, so the panel is the complete, checksum-verified group even when a
  content filter hid some children (the partner feed is the one exception: it
  has no split route of its own and expands **locally** from the children the
  page already holds — docs/43 §13). Either way the panel states the invariant
  in words: `Parts add to -187.42`, or `Parts do not add to -187.42` when
  `sum_ok` is false — never silently fixed up client-side.
- **fail open**: a child whose header is missing from `split_parents` (server
  bug) renders as an ordinary row — never a crash, never a phantom group.
- column layout stays with the surface (the two tables have different
  columns) via the `layout` prop: `REGISTER_SPLIT_LAYOUT` (10 columns) and
  `ACCOUNT_SPLIT_LAYOUT` (7 columns); `variant` is `table` (register,
  account activity), `list` (Dashboard recent list — the header and the
  region are sibling `<li>`s so every list item stays inside a list) or
  `div` (the partner feed).
- **surfaces in scope**: `/transactions` (including its fuzzy-search mode,
  which ranks the same `items` and therefore groups the same way),
  `/accounts/:id`, the Dashboard's recent list, and the partner `/shared`
  feed — all read `TransactionPage` / `PartnerTransactionPage`, so all of
  them get `split_parents` from one payload.

### The editor — `SplitEditor`

`frontend/src/components/transactions/SplitEditor.tsx`, a modal over the
row's amount (`role="dialog"`, `aria-modal`, the shared `dialogA11y` contract:
focus in, Escape closes, backdrop click closes, no focus trap — identical to
`TransactionEditor`).

- Opens with **two** children; the first is prefilled with the parent's
  category and the rest are empty. That prefill is a **client-side
  convenience only** — the server copies identity fields and never a
  `category_id` or a tag (docs/44 D4/D5).
- Per child: amount (`type="number" step="0.01"`, signed), `CategoryPicker`
  (M104), `TagPicker`, and a `note` (`maxLength={500}`, plaintext). *Add a
  part* / *Remove* keep the count in **2 … 20**; the row counter reads
  `N of 20 parts · 2 minimum`.
- The gate is the **`Remaining: <x>` line**, computed in exact fixed-point
  Decimal arithmetic over integer cents (`remainingLabel`, exported from the
  component because the arithmetic IS the contract — `0.30 − (0.10 + 0.20)`
  must read `0.00`, never `-0.00`). Submit stays disabled until it reads
  `Remaining: 0.00`, and until every leg is non-zero with the parent's sign.
- A server envelope renders `detail` **verbatim** plus its `code` —
  `Children must sum to -187.42; they sum to -177.42 (residual -10.00)
  (split_sum_mismatch)` — never a generic "something went wrong".
- When the vault is unlocked the editor **dual-writes `amount_cipher`** for
  every child (`encryptTransaction`), because under
  `users.encryption_required` the server refuses a child without one
  (docs/33 F-10).

### Entry point, re-split and merge

- **`Split`** sits in the row's action cluster beside `Edit`/`Delete` on
  `/transactions` and `/accounts/:id` (`aria-label="Split transaction <id>"`,
  `title="Split this transaction into parts"`, keyboard reachable,
  `min-h-11`). It is **hidden entirely** when the row's account type ∈
  `INVESTMENT_ACCOUNT_TYPES` and when the row is itself a child (a nested
  split is never accepted) — client hides, server refuses
  (`422 split_account_not_allowed` / `422 split_nested`), defence in depth.
  While the account type is not loaded the button stays hidden: never offer
  an action the server would turn down.
- **`Re-split`** in the open panel reopens the editor prefilled with the
  current children and issues `PUT …/split`; **`Merge back`** is M95's
  two-stage destructive action (§ Destructive actions — stage 1 arms
  `Confirm merge?` and sends NOTHING, stage 2 fires `DELETE …/split`), with a
  comma-joined `sr-only` context so two groups' buttons never share an
  accessible name.
- **Honesty (this doc's rule):** the UI never claims a feature it does not
  have — no "auto-split", no "AI split". The copy is *Split this transaction
  into parts*, and the honesty sweep stays clean because nothing else
  mentions splitting.
- **No new ⌘K palette entry in v1** (out of scope, docs/44 §12).

### Accessibility

- One focusable disclosure button per group carrying **`aria-expanded` +
  `aria-controls`**, pointing at a **labelled `role="region"`**
  (`aria-labelledby` → the header button). The badge text and the statement
  facts ride in the button's accessible name via an `sr-only` span
  (`, <date>, <amount>, Split · 3 parts`), so the header announces what it
  is without a visible fourth column.
- The panel row stays in the DOM while collapsed (carrying `hidden`), so
  `aria-controls` always resolves and there is never a hidden duplicate of
  the children.
- Touch floor: disclosure, merge, re-split and the group checkbox are all
  `min-h-11` (44 px) at the mobile breakpoint; the chevron is
  `aria-hidden` and its rotation is `motion-reduce:transition-none`
  (§ Reduced-motion contract).
- axe clean **with a group open**, both themes, at 390/412 and 1440.

### Touch points
- `frontend/src/components/transactions/SplitGroupRow.tsx` — grouping,
  header, badge, disclosure, region, merge button.
- `frontend/src/components/transactions/SplitEditor.tsx` — the modal,
  `remainingLabel` / `legsAreValid`.
- `frontend/src/api/transactions.ts` — types (`SplitChildInput`,
  `SplitParentPublic`, `parent_split_id` / `is_split` / `note` /
  `split_parents`) and the four calls.
- `frontend/src/pages/Transactions.tsx`, `AccountDetail.tsx`,
  `Dashboard.tsx`, `SharedHousehold.tsx` — surfaces, `Split` affordance,
  editor state, bulk-select hook-up, partner `expandMode="local"`.
- `backend/app/api/transactions.py` — the four routes, `_transaction_page`
  (+ `_split_parent_rows`), `_refuse_split_row_write`.

### Verification
`frontend/tests/splitTransactions.test.tsx` (5): `splitGrouping` (one header
for three children, `Split · 3 parts`, `id ASC` on expand, header bulk-select
selects the children), `splitEditor` (`Remaining:` in exact Decimal
arithmetic, submit disabled until `0.00`, the server's `split_sum_mismatch`
rendered verbatim), `splitInvestmentHide` (no Split affordance on
an investment row, accessible-name sweep clean), `splitA11y`
(`aria-expanded`/`aria-controls` wiring, labelled region, axe-core 0
violations with the group open, both themes) and `splitPartnerFeed`
(`/shared` groups the same way; privacy mode shows `🔒` in the header
amount).

`backend/tests/test_split_transactions.py` (22) is the executable version of
docs/44's contract (§ Named tests), including
`test_perf_list_query_count_unchanged_with_splits` — a page containing
children costs **exactly one** statement more than a page without.


## Budget carry (M110)

**Context.** A budget is a standing per-period limit, so every period starts
from zero: an under-spend is lost and an over-spend is forgotten. The
rollover (Monarch-scan's top add) lets a category carry its leftover into the
next period — **opt-in per category, off by default**, so a row that never
opts in renders exactly the numbers and labels it did before M110.

### The layering — one row, three numbers, never merged

| layer | wire field | formula | shown as |
| --- | --- | --- | --- |
| assigned | `amount` | the limit the user set | unchanged (the `amount` in the edit form) |
| carried in | `carried_in` | the FROZEN value from the period before | its own line: `+$60 carried in` / `−$100 carried in — a debt to pay down first`, or absent when `null` |
| spendable | *(derived)* | `amount + carried_in` | the denominator of the money line (`$100 / $660 spendable`) and of `pct_used` |
| left | `remaining` | `spendable − spent` | `… left` on the cards (`$560 left`) |

`pct_used` is the M65 true ratio against the **spendable** pool — `null` only
when that pool is `<= 0`. `carried_in` is never folded into `amount`, and a
deficit is never folded away: the debt keeps its own danger-toned line
(AGENTS §2 money rules; docs/08 §Budget rollover for the wire).

### Tone rules (the carried-tone convention)

| state | tone | the row prints |
| --- | --- | --- |
| carry off / `carried_in: null` | none | nothing extra — the pre-M110 row, byte for byte |
| `carried_in > 0` | `--color-gain` | `+$60 carried in — extra room this period` |
| `carried_in < 0` | `--color-danger` | `−$100 carried in — a debt to pay down first` |
| `pct_used: null` (pool `<= 0`) | `--color-danger`, bar full | `over budget — the carried debt is bigger than this period's limit` — **no percent at all**: inventing `0%` or clamping to `100%` would both be numbers the data does not support |
| bar fill / label | unchanged | the bar still caps at 100%, the label still prints the true ratio (M65) |

The help hint switches with the denominator: `LIMIT_HINT` (byte-identical to
the pre-M110 sentence) while nothing carries, `CARRY_HINT` otherwise — both
live in `frontend/src/lib/budgetCarry.ts`, the one place the three budget
surfaces read this layering from, so the Budgets page, the Dashboard cards and
the shared-household card cannot drift apart.

### The engagement surface

Each `/budgets` row carries a **`Carry leftover`** pill: one click, no wizard,
`aria-pressed` carrying the state (`on` = `carry: "forward"`), a `HelpHint`
that says what the flag does and that the VALUE itself is computed and frozen
server-side. It PATCHes `{carry: "off" | "forward"}` and invalidates
`["budgets"]`; the toast names the new state. Toggling OFF hides the carried
line and restores today's math instantly; toggling it back ON shows the SAME
frozen value (the flag governs whether a value is READ, never what it was).
The Dashboard budget cards (M97's presets order untouched) and the shared
household's budget cards show the carried line wherever it exists.

### Scope rules the UI inherits (docs/08 §Budget rollover)

- **One period deep.** `carried_in(P) = amount − spent(P−1)` — a carried value
  never feeds its own successor, so a chain can never form (no compounding).
- **A budget governs only periods it existed for.** No snapshot is ever
  materialized for a period the budget did not exist in front of — which is
  why a budget opened mid-period shows no carry until it has a full prior
  period behind it.
- **Frozen at the boundary.** The first read of a period writes the snapshot;
  every later read (including a back-view) returns it unchanged.
- **Alerts use the same denominator.** The Dashboard alert banner and the
  budget card still print one number (M61 #3): with a carry on, both measure
  `spent ÷ spendable`.

### Touch points
- `backend/app/models/budgets.py` (`Budget.carry`, `BudgetCarryover`) +
  migration `0040_budget_carry`.
- `backend/app/services/budgets.py` (`spent_between`, `carry_in`, `pct_ratio`,
  `progress` — the single producers), `backend/app/api/budgets.py`,
  `backend/app/api/spending_alerts.py`, `backend/app/api/partner.py`.
- `frontend/src/lib/budgetCarry.ts` (display layering + the two hints),
  `frontend/src/pages/Budgets.tsx` (toggle + carried line),
  `frontend/src/components/reports/BudgetCards.tsx`,
  `frontend/src/pages/SharedHousehold.tsx`, `frontend/src/lib/s3Engine.ts`
  (`budgetsProgress` — the exact port).
- Tests: `backend/tests/test_budget_carry.py` (11), the partner mask case in
  `backend/tests/test_household_sharing.py`,
  `frontend/tests/budgets.test.ts` (5), `frontend/tests/lockedVaultDegradation.test.ts`
  → “A13: budget definitions/progress are lock-invariant and match the golden”
  (the surviving `budgets.progress` golden, since M160 the compute lives in
  `lib/budgets.ts`) + `backend/tests/test_m160_replay_s3.py` (the same golden
  replayed at `GET /budgets`).

### Verification
- Backend: carry on puts `$60.00` in `carried_in` next period with
  `remaining = 660.00 → 560.00` after `$100` spent and `pct_used =
  float(100/660*100)`; a deficit carries `-100.00` and shrinks the pool to
  `500.00`; carry off leaves `carried_in: null`, `remaining: 500.00`,
  `pct_used: float(100/600*100)` and writes no snapshot row; a budget with no
  governed source period carries nothing; the back-view returns the snapshot
  after the source period is edited; `spendable <= 0` answers `pct_used: null`
  and still fires a critical alert at the documented `999.99` ceiling; the
  alert measures `660.00` (pool) not `600.00`, so `actual_pct` equals the
  card's rounded `pct_used`; deleting a budget takes its snapshots and alerts.
- Frontend: the toggle PATCHes `{carry: "forward"}`, the layers render as
  `+/$−` lines with the right tone, `pct_used: null` prints `over budget`
  (bar full, danger), carry off is the pre-M110 row, and the engine port
  reproduces the server's `budgets.progress` golden exactly.
- Partner: `GET /partner/budgets` carries `carry` (never masked) and
  `carried_in` (masked to `null` under the owner's privacy mode, real when
  off) — the M93 honesty pattern extended.


## Receipt images (M108 P4)

Attach ONE photo of a paper receipt to a transaction: the row gets a chip that
opens it, the editor gets a field that attaches, replaces or removes it, and
the image leaves this browser only for this app's own API. Contract pins:
docs/02 §transaction_receipts (the table), docs/08 (the three routes).

**The name matters.** "Receipt image" here; M106's `transaction_events` rows
are "activity receipts". Same word, different thing — the History drawer below
lists what HAPPENED to a row, this section is a picture of a till slip. They
share no column, route, constant or helper, and every comment says which one
it means.

### Two entry points

1. **The row.** A paperclip icon button in the actions cluster arms the page's
   ONE shared file picker — `<input type="file" class="sr-only">`, never
   `display:none`, because a hidden-but-focusable control still answers a
   keyboard user while `display:none` would strand them. One input for 200
   rows rather than 200 extra tab stops. When `has_receipt` is true a chip
   also appears in the badge area. Both controls name their row (M92): two
   rows can share a payee, and a probe selecting by name must not hit the
   wrong one.
   - `Attach an image — {payee} (transaction {id})`
   - `View the image attached to {payee} (transaction {id})`
   Clicking the chip fetches the blob through the owner's session and opens
   it from an object URL — never a `/receipt` URL in an address bar, so there
   is no token-free link to share, bookmark or leak in a screenshot.
2. **The editor modal.** A `Receipt image` field after the ledger grid:
   - no image → **Attach image** + `webp/jpeg/png/gif, up to 2 MB — shrunk in
     the browser first.`
   - image → **One image attached** chip + **View** + **Remove**.
   Attaching when there is none and replacing when there is are the same PUT,
   so there is deliberately no third "Change" control, and Remove is exactly
   one button that cannot be mistaken for it.
   **Both surfaces move together**: the LIST is the row chip's only source, so
   the editor's attach and Remove call `onReceiptChanged` (each host passes its
   own `invalidateLedgerData()`). Without it the editor would report "no image"
   while the row behind it still wore the chip — the exact contradiction this
   section pins.

### Compression before the upload

`src/lib/receiptImage.ts`, platform APIs only — no new dependency (the locked
stack has no image codec and `package-lock.json` gains nothing): decode with
`createImageBitmap`, downscale to a 1600 px longest edge through `fitWithin`
(never upscales, never returns a zero edge), then `canvas.toBlob` at 0.85 →
0.7 quality, webp then jpeg, first result under `RECEIPT_MAX_BYTES` (2 MB)
wins. Inputs over 10 MB are refused before they are decoded at all. Failures
raise `ReceiptImageError` carrying a machine `reason` and a sentence written
for a person; the toast shows the sentence.

The client does it rather than the server because a phone photo is over the
2 MB cap as shot: either the browser shrinks it before the trip or the server
turns it down after. Doing it here makes the failure local and instant, and
keeps the server's cap a single dumb `413` instead of a transcoding service.

### Sizes & touch targets

Every control this adds clears the docs/09 floor: `min-h-11 min-w-11` on the
row's paperclip and on the chip (both collapse under `md:` to the table's
compact size), `min-h-11` on the editor's Attach / View / Remove. Both
icon-only controls carry `title` + a per-row `aria-label`, and every icon is
`aria-hidden`. `View` and `Remove` disable while a request is in flight rather
than being removed, so the layout does not jump under the cursor.

### OCR viability check — measured, not assumed

The card asks for this check before choosing between local OCR and an honest
manual attach, so the numbers are recorded here rather than asserted. Run
2026-10-06 on this host (Debian trixie, x86_64, tesseract 5.5.0 /
leptonica 1.84.1, eng + osd tessdata); inputs were a real 1440x813 UI
screenshot and a 1634x1246 receipt-shaped JPEG at the size the client's
`fitWithin` would send.

| probe | measured |
| --- | --- |
| `tesseract` pre-integrated? | **No** — `backend/Dockerfile` is `python:3.12-slim` + `uv sync --frozen` with no apt layer, and `backend/pyproject.toml` carries neither `Pillow` nor `pytesseract` |
| install on this host (`apt-get install tesseract-ocr`) | 2.63 s wall, 18 MB on disk |
| first OCR call, including model load (1440x813) | 0.81 s |
| warm OCR, median of 5 (1440x813) | 0.83 s (range 0.74–0.88 s) |
| OCR alone on the 1634x1246 receipt JPEG | 0.60 s median (0.56–0.61 s) |
| preprocessing: decode + greyscale + downscale + autocontrast + binarise | **9.5 ms** median (9.0–14.5 ms) |
| full pipeline: preprocess + write + OCR | 0.48 s median (0.46–0.52 s) |

Read against the card's two thresholds — an API call over 5 s, or image
preprocessing beyond 200 ms — **neither is crossed**: OCR here is fast enough
that "too slow" is not the reason it is absent. What the check actually
answers is the card's own parenthetical (`tesseract pre-integrated?`), and the
answer is no; the remaining cost is integration, not time:

1. **No OCR contract exists to implement.** docs/02, docs/08 and this doc pin
   no payload shape, no field list, no storage column and no rendering rule
   for extracted values, and the milestone names OCR as later work. Inventing
   that contract inside a polish batch would be a spec written at execution
   time (AGENTS §7), which is the thing docs-first forbids.
2. **Two new locked artifacts**, which AGENTS §3 has the orchestrator stage
   upfront rather than mid-card: an apt layer in `backend/Dockerfile`
   (+18 MB image) and a codec dependency in `pyproject.toml` / `uv.lock`.
3. **CI cannot exercise it as the suite stands**: the runner has no
   tesseract, so either the OCR tests are `skipif`-gated (never run in CI) or
   `.github/workflows/ci.yml` grows an install step.

So v1 ships the manual attach flow, labelled **Attach image** and never
"scanned" (§Privacy copy, the honesty rule), and the table above is the input
to the OCR spec when it is picked up. It is logged as a measurement, not as a
verdict, because on timing alone OCR would have passed.

### What deliberately never happens

- **No OCR — see the viability check above** (measured: fast enough, but not
  pre-integrated and not yet specced). What exists today is a panel that says
  an image is attached — exactly where extracted fields land when OCR is
  picked up. Shipping it now would mean adding a second boolean later to
  distinguish "image" from "image + fields", which is worse than one honest
  flag today.
- **No email forwarding.** An always-open inbound address is an abuse surface
  (the Monarch scan's privacy posture, carried verbatim).
- **No cloud.** `app/services/fx.py` and `app/services/market_data.py` remain
  the only egress seams (AGENTS §2); a receipt cannot leave this server, and
  `services/exposure_breakdown.py`-style cache-only reads stay cache-only.
- **No per-handler demo check.** The central `get_current_user` guard
  (docs/20) refuses demo writes; there is no `_assert_not_demo` for someone
  to forget in a fourth route.

### Verification

`backend/tests/test_transaction_receipts.py` (11): byte-for-byte round trip
with the sniffed content type, replace-not-accumulate (one row after two
uploads), all four accepted types, `413 receipt_too_large` on a 2 MB + 1
body, `422 receipt_unsupported_type` for magic-byte mismatch AND for a
`Content-Type` that lies about the bytes, `422 receipt_empty`, ownership (a
foreign tenant gets 404 in all three directions), idempotent `204`,
`404 receipt_not_found`, and `/export/full` carrying the base64 verbatim so
the docs/11 restore guarantee holds for images too.
`frontend/tests/m108Receipts.test.tsx` (12): the chip renders only when
`has_receipt`, labels are unique per row, exactly ONE file picker exists per
page, clicking the chip asks the API for `/transactions/{id}/receipt`, the
editor shows both states and notifies the list after an attach or a Remove
(`onReceiptChanged`), the multipart PUT carries `FormData` with no
hand-written `Content-Type`, and `fitWithin` / `ReceiptImageError` behave as
pinned above.

## Forecasting — honest labels (M109)

The four strings below are **load-bearing copy**, exported from
`frontend/src/lib/forecastCopy.ts` as the single source and asserted verbatim
by `frontend/tests/m109Forecast.test.tsx` (F15). They appear on `/forecast`
itself — not only inside a tooltip or the collapsed panel — because the label
IS the product's honesty:

- `Projection, not a prediction.`
- `Not a tax simulator — a scenario that changes income does not recalculate your tax projection.`
- `Deterministic, not Monte Carlo — the same inputs always give the same output.`
- `Not financial advice.`

Rules that go with them:

- The **Assumptions** panel is collapsible but never conditional: it always
  states the inflation rate in force, the flat return (or 0), the flow window,
  and the decomposition `income = recurring + residual` using the SERVER's own
  decimal strings verbatim — no reformatting, no `parseFloat` round trip, so
  the three figures the panel prints are the three the engine used.
- When a `house_purchase` event exists the panel adds: home value held
  constant at the purchase price (no appreciation, no maintenance), and the
  mortgage is a **flat-rate monthly model** — no Canadian semi-annual
  compounding and no stress test.
- When a `career_break` exists it states that ALL income is suppressed between
  the two dates, because salary and any other income are indistinguishable at
  the row level. Suppressing "income" is the honest reading of "no income from
  A to B"; silently picking which income to drop would be a guess.
- An event whose date falls beyond the rendered horizon is never hidden: a
  neutral line says it will not appear until the horizon covers it.
- Scenario mutations surface the server envelope verbatim (`detail` + `code`),
  exactly as §Error envelope prescribes — `409 scenario_limit_reached` reads as
  itself, never as a generic "something went wrong".

## Transfer suggestions panel — one shared track (M112)

AccountDetail's Transfer Suggestions panel used to give each side of the pair
its OWN inline `flex` row: amount, date and account were only as wide as
their content, so the two rows staggered into a staircase, the account label
broke mid-paren (`(CIBC` / `Chequing)`), the date stacked `2026-` / `09-12`,
the card's own box stayed 374 px wide at every viewport from 320 to 420 (14 px
past a 360 px screen), and a payee of `"-"` — a real value from the data
meaning "no description" — printed as its own truncated dash line above the
amount.

**Contract** (`components/transactions/TransferSuggestions.tsx`; full spec in
docs/17 §Frontend):

- **ONE grid holds both sides on a single shared track** (`grid-cols-1`), so
  every amount / date / account / payee line of both rows starts at the same x.
- **Every line carries `data-side="source|candidate"` +
  `data-cell="amount|date|account|payee"`** — the hooks the unit test and the
  browser probe key on (the probe reads those roles, never className
  position).
- **Every line is `truncate`** — never a mid-label break, never a multi-line
  paint, never a card shoved wider than its container.
- **A `"-"` or blank payee renders NO cell** (the line is omitted entirely);
  a `null` account renders no account cell either.
- **Action buttons (check / X) stay a `shrink-0` flex sibling of the grid**,
  never inside it.
- **Side-by-side `amount | date | account` columns were measured OUT, not
  assumed** (AGENTS §3): content box **92 / 128 / 158 / 188 px** at
  320 / 360 / 390 / 420 (160 px in the desktop sidebar) against **294 px**
  needed — they fit at no supported width. The measurement also lives in the
  component comment.
- **Milestone label:** the fix's commits `8330033` / `f75828c` say
  `fix(M110)` / `test(M110)`, but docs/10's M110 is the budget rollover and
  M111 was taken by `5dfbbd4`. This work is **M112** (next free number);
  don't propagate M110.

**Verification.** Unit: `frontend/tests/transferSuggestionsLayout.test.tsx`
— **9/9** (one grid holds both rows; missing account keeps the track;
candidate row keeps its own gap; `"-"`/blank payee renders no cell on either
side; a real payee is kept; a long account ellipsizes instead of wrapping;
every line stays on the clipped track; buttons stay outside the grid; empty
state). Browser: `frontend/scripts/a11y/transfer_suggestions_probe.py`
against the deployed origin (`https://fintrac.princessandmark.com`) at
**320 / 360 / 390 / 420 / 1440** — **78/153 before → 154/154 after**. The 75
before-failures: `flex` shape not `grid` ×10, date column staggered
(5.0–5.1 px) ×10, account column staggered (1.8–5.2 px) ×10, date painted on
2 lines ×20, account painted on 2–7 lines ×20, stray `-` payee line ×5. Both
runs: M82 text-node overlap sweep **0** at every width, axe **0
critical/serious** at 390, console errors **0**. Committed evidence:
`scripts/a11y/transfer-suggestions-probe-{before,after}.json` (each carries
the full `checks` array) plus `transfer-suggestions-{before,after}-<width>`
screenshots with their `-panel` crops.

## Account page pending-import banner (M113)

Mark staged an import into account 731, opened `/accounts/731`, saw no
securities and no hint that a batch was parked at `/imports` awaiting commit,
and abandoned the flow twice at discard/revert. The staging model was right
(docs/03); the *discoverability* was not — pending rows are deliberately
invisible to every list read (docs/03 staging rule), so the page had nothing
to show and no way to say "your import is waiting".

**Contract** (`components/imports/PendingImportBanner.tsx`, mounted at the top
of `pages/AccountDetail.tsx` for BOTH the bank and the investment view):

- **Data is a count and only a count.** `Account.pending_import_count` from
  `GET /accounts/{id}` (docs/08): no file name, batch id, status or row ever
  reaches the client (E2EE row-count rule).
- **Renders only when `count > 0`.** `0` covers no-batch, committed,
  discarded *and* reverted alike — that distinction must never leak, so there
  is exactly one non-rendering state.
- Copy: **"`N uncommitted import(s) for <account type>`"** plus one line that
  the rows are not live until Commit, plus a **`Go to the Imports page`** link
  to `/imports` (a client-side route, M68).
- `role="status"` + `aria-live="polite"` — announced on arrival, never a
  modal, never `role="alert"`.
- **Dismissible once per account per browser session**: `Dismiss` writes
  `sessionStorage` key
  `fintrack.m113.pending-import-banner.<scope>.<accountId>.dismissed` = `1`
  (the same pattern as §The header hint — "N unreviewed"), so the
  dismissal survives navigation and reload inside the tab and expires with the
  session. The key is per ACCOUNT **and per tenant**: `<scope>` is
  `tenantScope(user)` (user id → email → `signed-out`, `lib/tenantStorage.ts`,
  security follow-up F-6), so dismissing one account never hides another's
  banner and one tenant's dismissal never suppresses the same account id for a
  different tenant. The key is re-read when the route's `accountId` changes
  under a still-mounted element.
- Semantic tokens only (`border-accent/40`, `bg-accent/[0.07]`, `text-text`,
  `text-muted`, `text-accent`), so both themes keep the documented contrast
  pairs; the link and the `Dismiss` button are both `min-h-[44px]`
  (§Touch-target floor).

**Touch points.** `backend/app/schemas/accounts.py`
(`AccountPublic.pending_import_count`), `backend/app/api/accounts.py`
(`_pending_import_count` — owner-scoped, computed on the single-account
responses), `frontend/src/api/accounts.ts`, `components/imports/PendingImportBanner.tsx`,
`pages/AccountDetail.tsx`; docs/08 carries the field, this section the pattern.

**Verification.** Backend `tests/test_m113_pending_import_banner.py` (Mark's
case → 1; N > 1 → N; committed / discarded / reverted → 0; payload carries a
count and no batch field; another user's batch never counts; the list keeps
its documented 0; the `account_in_use` delete guard is unchanged). Frontend
`tests/m113PendingImportBanner.test.tsx` (Mark's case → banner + `/imports`
link at `role="status"`, N > 1 pluralises, 0 renders nothing, dismissal is per
account and per session, and `/accounts/731` mounts the banner from
`pending_import_count`).

**Live, against the deployed stack** — `frontend/scripts/a11y/m113_pending_import_probe.py`
on `https://fintrac.princessandmark.com`: **17/17**. The pre-M113 parser (git
`3b17b95`, loaded from `git show`) reports **20 unmapped of 91** for Mark's real
TFSA export (4 `MoneyMovement/TRANSFER_TF` + 16 `Tax/NRT`) and **1 of 20** for
his RESP export; the deployed stack answers `summary.unmapped == 0` for both
byte-strings (legacy parser, `profile: null`), stages the TFSA file 91/91, and
renders the banner on the account page in BOTH themes with `role="status"`,
`aria-live="polite"`, a 44 px `/imports` link that routes on click. The TFSA
batch's `commit` answers the documented `422 fx_rate_missing` — never a 500 — so
the probe discards it and observes the banner's 0-state; Mark's RESP export
(all CAD) **imports 18 + 2 in-file duplicates and COMMITS**, running the full
ACB replay including the quantity-0 cash transfer_out. Console errors 0 — the real account it watched kept exactly the batches it had (`discarded=1, reverted=1`) —
the probe never wrote to his tenant. Evidence committed next to this file's
script: `m113-pending-import-probe.json` + `m113-banner-prod-1440-{light,dark}.png`.

## Account value history (M118)

Mark's ask: *"total value over time on THAT account, with markers for the
contributions/withdrawals/transfers that moved it"* — the account page's own
history, not the user-level Reports net-worth line (docs/12 §4) and not the
forecast's hypotheticals (docs/45).

**Placement.**

- **Investment accounts** (`INVESTMENT_ACCOUNT_TYPES`): `AccountValueChart` is
  the FIRST module of `InvestmentAccountView` — above the summary cards and
  above holdings, full width, `ChartFrame`-styled, **280px** tall.
- **Cash accounts**: the same component sits in the exact slot the old
  *Balance Over Time* card occupied, WITH that card's `RangePicker` as its
  header extra (`from`/`to` query params). It replaces that chart rather than
  sitting beside it: both answered "what was this account worth over time", and
  two near-identical lines on one page is a defect, not a feature. The ledger
  query still feeds the Activity table and its running-balance column.
- **Currency.** The series is the reporting currency (CAD, docs/05 §Portfolio
  value) — this is the one chart on the account page that is, which is why the
  header carries a `values in CAD` note whenever `native_currency != currency`.
  The summary cards keep showing native amounts (docs/01 money rule).

**Chart chrome** (`components/charts/AccountValueChart.tsx`):

- `ChartFrame` `role="img"` + one sentence naming the series, granularity,
  range, point count and its facts (endpoints, marker count, estimated and
  unvalued periods) — docs/12 "Chart accessibility".
- The chart box is the M102 scroll region: `role="region"` +
  `aria-label="… chart"` + `tabIndex={0}` + `overflow-x-auto`, with a
  `min-w-[36rem]` inner box. At 390/412 the chart PANS horizontally instead of
  squeezing its ticks, and `XAxis interval="preserveStartEnd"` thins the middle
  ticks while keeping the first and last — no clipped labels, no overlap
  (§Horizontal table scroll, §M82 F6).
- Honesty chips in the header, all `Badge` tones (already contrast-audited):
  `~ N estimated` (warn), `N without a valuation` (danger), and a muted
  `weekly buckets — the finer ask would be over 400 points` note when the
  endpoint coarsened the granularity (docs/08 §Point cap).

**Tooltip (M97 honest-tooltip pattern).** Header = the bucket label; then
`Value` / `Cash` / `Holdings` for that period; then, as applicable:

| line | when |
| --- | --- |
| `no valuation for this period` (danger) | `valued: false` with `gap_reason: no_price` or `fx` — the line is GAPPED here, never interpolated, never 0 |
| `not held yet` (muted) | `valued: false` with `gap_reason: not_held` — the account held nothing on this day, so nothing is missing (M144) |
| `estimated — a price for this period was carried forward` (warn) | `estimated: true` |
| `+ $16,500.00 contribution` / `− $24,318.76 withdrawal` + `(categorized Deposit — from Wealthsimple)` | the period's markers, evidence verbatim from the row that caused it |

**Empty-state copy is ONE function** (`AccountValueChart.tsx::gapReasonNote`)
so the tooltip, the data table's Notes column and any future surface can
never drift into two sentences for one fact. The distinction is not cosmetic:
a wall of red "no valuation for this period" reads as *your data is broken*,
and M144's measured case was a day the account simply had not started holding
anything yet. Only `no_price`/`fx` buckets are counted by the header's red
`N without a valuation` chip and by the chart's text-alternative sentence —
`not_held` periods are honest absences and never wear it.

The evidence line is composed from what the row actually carries: a transaction
prints `categorized <category> — from <payee>` (each clause dropped when
absent), a cash trade says `recorded on the trades ledger`, an in-kind transfer
says `<qty> <symbol> transferred in kind`. Nothing is invented to fill a gap.

**Markers → evidence (the whole point of the marker).**

- Pins are `ReferenceDot` circles ON the line at the event's bucket, green
  (`GAIN`) for value in and red (`LOSS`) for value out, accent-ringed when
  selected, each with an SVG `<title>` of its heading.
- Click hands the SAME event object to the page, which routes it by
  `source`:
  - `transaction` → `AccountActivity` sets its filter to `From = To =
    event.date`, resets to page 1, then scrolls the row into view and rings it
    (`data-txn-id`, `aria-current="true"`, `bg-accent/10` + inset accent ring).
    Narrowing to the event's OWN day is what makes the jump land: under the
    default sort an older row can sit pages away, and a jump that silently
    lands on nothing is worse than no jump. The filter bar showing the dates is
    the visible reason the table narrowed.
  - `trade` → `TradesLog` sets the `action` + `security` filters (a
    contribution/transfer set is a handful of rows where the unfiltered log
    would put an old row pages away), resets to page 1, then the same
    scroll + ring on `data-trade-id`.
  - The row is ringed until another pin wins, so the answer stays on screen
    while the user reads it.
- **Keyboard path.** The pins live inside `role="img"` (presentational by
  design), so the `<details>` table underneath carries a real `<button>` per
  marker — `Show the <kind> of <date> in the table` — reachable through the
  native `summary`, in the same labelled scroll region as the period table.

**Empty state.** `has_data: false` (no committed row yet, no starting balance)
renders the three-part contract — `No value history yet` + a real capture
(`/help/investments.png`) + **Import a statement** → `/imports` — instead of a
flat $0 line. A brand-new account must not look like a broke one.

**Touch points.** `backend/app/services/account_value.py`,
`backend/app/api/accounts.py` (`value_history`),
`backend/app/schemas/accounts.py` (`AccountValueHistory`), docs/08 carries the
route; frontend `api/accounts.ts` (`getAccountValueHistory`),
`components/charts/AccountValueChart.tsx`, `pages/AccountDetail.tsx` (both
views + the Activity focus prop), `pages/Investments.tsx` (`TradesLog`
focus prop).

**Verification.** Backend `tests/test_account_value_history.py` (12 tests:
three hand-recomputed probe dates, staged rows excluded, default threshold and
`min_event_value=0`, the no-price gap, the unresolved-fx gap, both granularity
coarsenings, 404/422/`Cache-Control`, the empty-state flag, and a 366-day ×
6-security sweep under the 1.5 s house cap). Frontend
`tests/m118AccountValueChart.test.tsx` (aria sentence facts, chips, the gap's
copy and em dash — never a `$0.00`, the marker table + `Show` handing back the
event, the empty state, and the three evidence-line shapes) and
`tests/m118MarkerJump.test.tsx` (the narrowing request, the `aria-current` row,
the visible From/To).

## Live prices & price history (M126)

Three surfaces, one honesty rule: **the price you see is a cached close that may
be arriving live; the money derived from it is the server's to compute.**

### Live-price display convention

- **What the stream may patch client-side:** a positions row's `price`,
  `price_date`, `price_source`, `price_age_days` and `price_stale` — the QUOTE,
  nothing else. `price_age_days` is whole calendar days from `price_date`, not
  fetch time. The market-date label states source + MARKET DATE ("yfinance ·
  today" / "yfinance · Oct 25"); where the API row also carries a real
  `price_fetched_at` stamp, a separate `fetched … ago` segment is rendered from
  that timestamp only. NULL means no evidence and omits that segment (no
  "just now" or unavailable placeholder). The SSE event remains quote-only and
  does not invent or patch a fetch timestamp. One
  `setQueriesData({queryKey: ["positions"]}, …)` covers every key in that family (`["positions"]`, `["positions", accountId]`), so the
  Investments page, an account's holdings view and any future consumer get the
  same tick from one patch (docs/18 §Frontend).
- **What it may never patch:** `market_value_native`, `market_value_cad`,
  `book_cost_cad`, quantities, the exposure donut, and every total. Those are
  Decimal valuation computed server-side on the 60s cadence (or `Refresh`), and
  the same tick also *invalidates* `["positions"]` and `["exposure"]` so the
  server re-read is scheduled (docs/18). Client-side recompute of money is
  banned by AGENTS §2 — this is the display half of that rule, not an
  exception to it.

- **The M129 carve-out, and its exact borders:** on the SECURITY PAGE only
  (the `["security-overview", id]` cache), the tick may re-evaluate the
  **price-derived** figures — the day Δ, the market value and the unrealized
  gain of YOUR position — from server primitives (`quantity`, the patched
  `price`, `latest.previous_close`, and the row's own `fx_rate_to_cad`), and
  must label the state "live" while it does (§Day change & own-position
  impact). Three properties keep this inside the rule rather than against it:
  every multiplier is a server-provided number (the client invents no rate,
  no cost basis, no quantity); book cost is history and never re-evaluated;
  and the marker's lifetime is the whole story — the same changed-payload
  gate *also* invalidates `["security-overview"]`, so the next server
  payload replaces every live figure and drops `livePatchedAt`. The
  the positions TABLE stays outside the carve-out entirely: its Day Δ and gain
  columns render the server's fields and move only on a server re-read.
- **The M130 carve-out** (§Portfolio header): the portfolio HEADER card may
  re-evaluate its now-value and day Δ while `livePatchedAt` rides the payload,
  from `quantity × price × fx_rate_to_cad` plus the row's own
  `previous_close_native` — the same four server primitives M129's card uses,
  and **nothing else**. Cash is not priced and never moves with a quote;
  `day_change_as_of` is the server's pinned baseline and is never re-derived;
  both figures say `live` while they are provisional. The card's range sentence
  is NOT re-evaluated — it belongs to the `/portfolio/value-history` response,
  which has no quote in it. Everything returns to server ownership on the next
  payload (the invalidation the patch already schedules), with no timer.
- **The indicator:** the patched price cell carries `.price-live-tick` — a
  single 1.4s accent sweep over the cell (semantic `--color-accent` token only,
  both themes; the global `prefers-reduced-motion` rule collapses it). It is
  applied ONLY when the row's own price actually changed, so a payload that
  moved a different security flashes nothing. The `SSEStatusIndicator` chip is
  untouched and remains the ONLY statement about **connection** state — a flash
  is not a health signal.
- **The totals footnote:** the patch stamps `livePatchedAt` on the cached
  positions payload. The next SERVER payload replaces that object outright and
  drops the marker, so while it is present the card prints — *"Prices above are
  live from the stream; market value, day change, gains and totals update on
  next refresh (≤60s or on Refresh)."*
  No timer, no second clock: the marker's lifetime IS the gap between the live
  quote and the server's own cadence. Privacy mode opens no stream, so no
  marker and no footnote can appear (docs/18 §Privacy mode).
- **The header hint** (`HelpHint` beside `Positions`) states the same split in
  one sentence: each price/source/age ticks in live; market value, day change,
  gains and totals are computed server-side and refresh on the normal cadence.

### Expanded rows — a position's own price history (M126 L2)

- A positions row is interactive: clicking anywhere on it toggles an inline
 expansion, and the chevron beside the sparkline is the keyboard path —
 `aria-expanded` plus `aria-controls` pointing at the row's OWN cell (only
 while that cell exists: an idref with no element behind it is an invalid
 ARIA value), and an accessible name of `Show <SYMBOL> price history` /
 `Hide <SYMBOL> price history`. The symbol stays a real link and stops propagation, so navigating
  and expanding are separate actions.
- The expansion renders the SAME `PriceHistoryChart` the security page draws,
  through `components/charts/PriceHistoryPanel.tsx` — header, range control,
  chart, and the three honest states below. No route change, no navigation.
- **Cost:** the sparkline comes from the positions payload's `spark`
  (last ≤30 closes, one bulk query server-side) — rendering the table costs
  ZERO extra requests. Only an EXPANSION fetches `GET /prices/history`, once
  per (security, range), cached by that key, so collapse → re-expand is free
  and a stream tick never refetches history.
- **States, always with the header so the range control never vanishes
  mid-request:** loading → `SkeletonRows`; error → `ErrorPanel` with the API
  message; zero points → *"No price history yet"* plus the honest hint (and the
  `Refresh prices` action where the caller has one). A chart is never drawn
  from an empty or failed response.

### Range control (M126 L3)

`components/ui/ChartRangeControl.tsx` — presets `1M / 3M / 6M / YTD / 1Y / 5Y /
All`, rendered in the chart's own header (docs/12 §Range → granularity owns the
formula; docs/12 §Chart ranges owns which chart gets which control).

- **Semantics:** a labelled `role="group"` of toggle buttons with
  `aria-pressed` per option — NOT `aria-selected`, which would claim tab
  semantics with no tabpanel behind it. Every option is a native `<button>`,
  so Tab reaches all seven, Enter/Space activate, and each carries
  `focus-visible:ring-2 focus-visible:ring-accent`. The selected option is
  marked by the accent token + an inset ring, never by colour alone.
- **Narrow viewports:** the group is `flex-wrap`, deliberately NOT an
  overflow-scrolled strip. At 390/412 the seven short labels wrap onto two
  lines and every option stays visible and hit-testable — a control you must
  scroll horizontally to reach is not reachable (docs/09 §Scroll regions is the
  opposite rule for *tables*, which have many rows to hide; seven labels do
  not).
- **Defaults:** price history opens at `1M` for a holding; the M118 account
  timeline opens at `All` (its pre-M126 behaviour) until a range is chosen; the
  bill chart opens at `All`. The page-level `RangePicker` keeps its own preset
  set (1W…1Y + custom From/To inputs) and is unaffected.
- **Data-table fallback:** every chart's `<details>` table renders the SAME
  `points` array the chart draws, so it reflects the selected range by
  construction — no second range state to keep in sync.

## Day change & own-position impact (M129)

The delta story, in three surfaces. Every number on them is server-computed
from cached closes + fx_rates (docs/08 pins the fields); "None" always means
*no data* and renders as an em dash or a withheld line — never a zero.

### Security page — the daily delta + "Your position"

- **The delta line** (under the latest price): `▲ +7.42 (+1.45%)` vs the
  previous trading day's close, house-coloured (`text-gain` / `text-danger`),
  `tabular`, with a plain "vs previous close" caption. **No previous close
  (a first-day listing) → no line at all**, exactly like the server sends
  `previous_close: null`.
- **The impact card** ("Your position") renders ONLY when the user holds the
  security: *You hold 35.2491 MSFT · book $17,432.11 · market $18,239.44 ·
  +$807.33 (+4.63%) since first buy · +590.22 USD*, plus a *Today:* line with
  `day_change_cad` (the day's move × your shares). With no position the card
  is **suppressed entirely** — a zero row would be a fiction (the Holdings
  card below already states "Not held — watchlist only").
- **Live tick** (the M129 carve-out in §Live prices): while `livePatchedAt`
  rides the cached payload, the delta line and the card's market value /
  unrealized / today figures re-evaluate from `quantity × price ×
  fx_rate_to_cad` and both say **"live"**; book cost never moves. The next
  server payload (the tick invalidates `["security-overview"]` too) replaces
  them and the label disappears — no timer anywhere.
- **The holdings table** (per-account split) renders the server's
  `unrealized_gain_cad` / `unrealized_gain_pct` per row and on the totals
  row — the table never subtracts anything itself.

### Positions table — Day Δ · Your gain (CAD) · Your gain %

- **Columns:** `Day Δ` (the server's `day_change_pct`, signed + coloured,
  right-aligned, `—` when unknown) sits after Trend; `Your gain (CAD)` and
  `Your gain %` (server `unrealized_gain_*`) close the row. Below `sm` the
  `Source · age` diagnostics column hides and the gain % folds under the
  amount as a stacked `text-xs` line — the deltas are the user-facing
  priority, the provenance pair is a diagnostic (the SSE chip and the stale
  path still carry freshness).
- **The Σ row** (`data-testid="positions-total-row"`) carries the SERVER's
  totals with `sr-only` labels: `total_unrealized_cad` ("Total unrealized
  (CAD):"), and since M130 `total_day_change_pct` in the `Day Δ` column
  ("Total day change:") and `total_value_cad` in `Market value (CAD)` ("Total
  market value (CAD):"). All three are sums of the column above them, so the
  footer can never disagree with the rows — and the day Δ / CAD total are the
  SAME numbers the header card at the top of the page renders, which is what
  makes them assertable as equal (§Portfolio header). The native `Market
  value` and the quantity cells stay empty on purpose: the payload computes no
  cross-currency total and no share count, and the client will not invent one.
- These columns are **server-cadence** like the rest of the table's derived
  figures: a live tick patches the price cell and the footnote names the gap
  (§Live prices). Both tables live on `/investments` and the account page
  (one `PositionsSection`).

### Positions by security (M146) — the own-position lens

Mark's real portfolio holds the SAME ETF in five accounts, so the flat table
makes him add five rows in his head. `PositionsSection` gains a lens:
**By account** (the payload's rows, one per account+security pair — today's
table, unchanged) | **By security** (the same rows folded into one row per
security, with the per-account rows inline under it).

- **A lens, not a page.** `SegmentedControl` in the `CardTitle` `right` slot
  (`layoutId="positions-view"`, `ariaLabel="Positions view"`), rendered ONLY
  when `showAccount` — an account-scoped page holds one account's rows, where
  every group would be a single row. State lives in localStorage under
  `fintrack:positions_view` (`"account"` | `"security"`; anything else reads
  as `"account"`), so it is a view over ONE table and never a server-stored
  preference the way the dashboard preset is. Switching views performs **no
  fetch**: `lib/positionGroups.ts::groupPositionsBySecurity` folds the rows
  the page already holds (the S3 pattern, `docs/33 §Server-first computing`).
- **The fold** (`frontend/src/lib/positionGroups.ts`, pure):
  * **Group key = `security_id`, never the symbol** — two listings can carry
    the same bare ticker on different exchanges (M143), and folding on the
    string would merge two securities into one row.
  * **Sums of the SERVER's own per-row figures** (cent-quantized): quantity,
    `book_cost_cad`, `market_value_cad`, `market_value_native` (a single
    currency inside a group, unlike the portfolio Σ the payload refuses to
    compute), `day_change_cad`, `unrealized_gain_cad`.
  * **Percent columns are VALUE-WEIGHTED, never a mean of percentages.** Group
    `day_change_pct` = Σ `day_change_cad` ÷ Σ (`market_value_cad` −
    `day_change_cad`); group `unrealized_gain_pct` = Σ `unrealized_gain_cad` ÷
    Σ `book_cost_cad`. Both halves are counted over the SAME rows (a row
    missing either side of a ratio contributes to neither), rounded 2 dp
    half-away-from-zero, and `null` on a zero/empty denominator.
  * **Nulls stay null** (`docs/09` anti-fabrication, applied to a sum): a
    column with no known row folds to `null`; a group nobody has priced
    renders the flat row's own *"no price — enter one manually"* plus `—` in
    every money cell — never an invented `$0.00`.
  * **One sparkline per group** — every row of a security carries the same
    closes, so the group renders the first non-empty one.
  * **Order**: Σ `market_value_cad` descending; an unvalued group sorts LAST
    (never first as a zero), ties break on `security_id`. NOTE (deviation,
    reported): the card's stated rationale was "matching today's order", but
    `services/valuation.py::compute_positions` emits rows keyed
    `(account_id, security_id)` — the FLAT view is account-ordered, not
    value-ordered. Only the GROUPED view is value-ordered.
- **The group row** (`PositionGroupRow`, `data-testid="position-group-row"`):
  the same 12 columns, filled from the aggregates. Its `Account` cell carries
  the **"N accounts"** chip (the split rows' badge shape — the duplicated-
  holding signal; no concentration hint yet), its `Trend` cell the single
  sparkline plus the disclosure. Its book cost is **display only**: each
  account's own cost basis summed for the row, never a pooled ACB — grouping
  feeds no tax math (`docs/05 §ACB`).
- **Disclosure** (`PositionGroupAccounts`): a chevron button with
  `aria-expanded` + `aria-controls` (bound only while the panel exists), the
  count in its accessible name (`", 5 accounts"`), whole-row click as the
  pointer path — the split-rows pattern (`docs/44 §6.3`). The per-account
  rows are `PositionRow` in a **sibling `<tbody>`** (not a nested table, so
  they sit on the same columns), `hidden` while collapsed so the `idref`
  always resolves, `aria-labelledby` its group header, each linking its
  **account name to `/accounts/{id}`**. ONE panel open at a time. A
  **single-account security renders FLAT** — an ordinary `PositionRow`, no
  chip, no chevron, nothing to reveal.
- **The Σ row is byte-identical across both views** — one `<tfoot>` rendering
  the payload's own `total_value_cad` / `total_unrealized_cad` /
  `total_day_change_*`, which the grouped view surfaces rather than
  recomputing. That equality IS the trust check between the two perspectives
  and is asserted in `tests/m146PositionsBySecurity.test.tsx`.

**Verification** (`frontend/tests/m146PositionsBySecurity.test.tsx`, 13
tests): the fold grouped by id not symbol; the 5-account sums; value-weighted
percents (0.72% vs the naive 2.75% mean on a 20×-skewed pair); all-null groups
kept null and sorted last; partial-null sums; one spark and the value-desc
order; the lens present on the portfolio view and ABSENT on the account page;
the group roll-up (`5 accounts`, Σ 1500 / $60,000.00 / +$15,000.00 / +33.33%);
the linked sub-rows with one panel open at a time; no chevron on a
single-account row; the "no price" group never printing `$0.00`; and the Σ row
equal byte-for-byte in both views, both directions.

## Portfolio header (M130)

Mark's ask, verbatim: *"a total view on /investments with up and down for the
day (or perhaps selection of range) with an overall chart too."* One card at
the very top of `/investments` (portfolio view only — an account-scoped page
already has its own timeline), then the page's existing order unchanged:
**Portfolio header → Positions → Exposure → Total return → Benchmark →
Dividend suggestions → Watchlist + Trade form → Trades log.**

`components/investments/PortfolioHeaderCard.tsx`, fed by TWO payloads and
adding nothing on top of either:

### The numbers — one payload, one sum

Rendered from the SAME `/portfolio/positions` response the table below it
renders (docs/08 §Portfolio summary):

- **Portfolio value** = `portfolio_value_cad` (`total_value_cad + cash_cad`,
  added server-side). `total_value_cad` is literally the Σ of the `Market value
  (CAD)` column, so with a zero cash leg the headline **is** the table's Σ —
  that equality is the anti-fabrication check the probe asserts.
- **Cash line** — rendered only when there is something to explain (docs/08's
  rule: *when the sum is non-zero*): `Holdings $1,021,759.37 · Cash in
  investment accounts −$3,872.72`, so a headline that differs from the table is
  never a mystery. Zero cash (the common case) prints nothing at all. When the
  cash leg could NOT be converted (`cash_cad: null`) the card refuses to claim
  a total: the label switches to **Holdings value** and the line reads *Cash in
  investment accounts —*.
- **Day change** = `total_day_change_cad` + `total_day_change_pct`, house
  coloured, `tabular`, with the caption `vs Oct 7 close` from
  `day_change_as_of` — the real prior market day, never "yesterday", which is
  a lie from Friday onward. `day_change_mixed_baseline` adds *· some holdings
  last closed earlier* instead of implying one market day. The whole line is
  **withheld** when `total_day_change_cad` is null (no row has a baseline) —
  the M129 honesty rule applied to a sum.
- **Range sentence** = `stats` from the `/portfolio/value-history` response for
  the CURRENT range: `Over 6 months: +$2,900.00 (+41.43%)  Oct 4 → Mar 31`.
  The server subtracted this response's own first and last valued point, so the
  sentence and the line it describes can never come from two different windows
  (docs/08 §Portfolio value history). The two halves are independent facts: the
  CAD move always renders when the server has one, and the percent appends only
  when `change_pct` is non-null — a window that STARTED at zero has a real move
  and no denominator, and withholding the whole line would hide a number the
  server already computed. Fewer than two valued points → `—`.
  The sentence's source is the value-history pass, NOT the positions header
  above it: on fractional-cent prices the two can read a rounding-order penny
  apart (header `Σ round(xᵢ)` per security vs the walk's `round(Σ xᵢ)` per
  account — drift bounded at 0.005 CAD per row, F-M130-1, docs/08
  §Portfolio value history). Neither number is derived from the other and the
  card never reconciles them by hand — that would break the single-source
  rule both payloads pin.

### The chart — M118's body, M130's payload

`GET /portfolio/value-history` (docs/08) is rendered by **the same drawing
body** as the account timeline — `AccountValueChartBody`, exported for exactly
this — through a `ValueChartPayload` interface that is the intersection of the
two wire contracts (the body never reads the scope id nor `stats`). Same
chrome, same marker pins, same `<details>` data table, same honest gaps and
`estimated` badges; only the fetch and the numbers differ.

**One range control drives both.** The `ChartRangeControl` lives in the header
card (not the chart's own header) because the state it changes has to move the
sentence AND the line: `range` is owned by `PortfolioHeaderCard`, passed down
as `headerExtra` to turn the body's own control off (the M126 cash-view escape
hatch) while its coarsened/estimated/gap badges still render. The key is
`["portfolio-value-history", from, to]` with `staleTime: 60_000`, mirroring the
endpoint's `Cache-Control`.

### Live tick — the M130 carve-out

While the stream's client-only `livePatchedAt` rides the positions payload,
`lib/impact.ts::livePortfolio` re-evaluates the headline and the Δ from
server primitives — `quantity`, the patched `price`, `previous_close_native`
and the row's own `fx_rate_to_cad` — and both carry a **`live`** badge. Cash
never moves with a quote (it is not priced), and `day_change_as_of` is the
server's pinned baseline, untouched by the tick. Everything else is the same
border M129 drew: no rate, quantity or cost basis is invented; the marker's
lifetime IS the gap (the changed-payload gate invalidates `["positions"]`, so
the next server payload replaces every figure and the badge disappears); no
timer anywhere. `tests/m130PortfolioHeader.test.tsx` pins the arithmetic
(100 × 110 + 200 × 20 = 15,000 and 100 × (110−90) + 200 × (20−18) = 2,400
against the server's own baseline).

### Touch points

- `backend/app/services/account_value.py` — `investment_accounts`,
  `investment_cash_cad`, `portfolio_value_history`, `_merge_buckets`,
  `_merge_events`, `_range_stats`
- `backend/app/api/portfolio.py` — `GET /portfolio/value-history`,
  `_positions_summary` (folded into `GET /portfolio/positions`)
- `backend/app/services/valuation.py` — `previous_close_date` on each row
- `backend/app/schemas/accounts.py` — `ValueHistoryStats`,
  `PortfolioValueHistoryEvent`, `PortfolioValueHistory`
- `frontend/src/components/investments/PortfolioHeaderCard.tsx` (new),
  `components/charts/AccountValueChart.tsx` (`AccountValueChartBody` +
  `ValueChartPayload` exported), `api/portfolio.ts`, `lib/impact.ts`
  (`livePortfolio`), `pages/Investments.tsx` (composition + the Σ row)

### Verification

- `backend/tests/test_m130_portfolio_value.py` — 13 tests (§Portfolio value
  history in docs/08 lists them).
- `frontend/tests/m130PortfolioHeader.test.tsx` — 8 tests: the header/Σ-row
  equality, the cash line, the withheld day line, the mixed-baseline caption,
  the live tick's hand-recomputed figures, and the range selector re-requesting
  its window with the sentence and the data table following THAT response.
- Live: `frontend/scripts/a11y/m130_portfolio_header_probe.py` (AGENTS §4 step
  5), axe 0 at 390/412 × dark/light.

## New security — the symbol suggestion chip (M142)

The add form used to make the user do the ticker's job: type a symbol, press
`Look up`, and if you typed a NAME (`APOTEX`) you got *"not found — it won't
be tracked automatically"* for a company Yahoo resolves in one call. Since
M142 the lookup also answers names (docs/07 §Symbol search fallback), and the
form offers the answer instead of taking over the field.

**The probe runs while you type.** 600 ms after the Symbol field settles, one
`GET /securities/lookup` fires — once per settled value, so a slow network
never turns keystrokes into a request storm. Editing the field again cancels
the timer and clears whatever was offered, so an offer cannot outlive the
string it was about. The probe is **silent on a miss**: a string that matches
nothing is not an error while it is being typed, and the dead-symbol copy
(M141) belongs to the Security detail page, not to a half-typed field. The
explicit **Look up** button keeps its loud, persistent `lookup-warning` (W11
pin 7) and its autofill.

**One listing → a chip, one click.** When the answer's symbol differs from the
typed string, the form renders under the field:

> Did you mean **APTX.TO** — APOTEX HEALTH CORP (Toronto, CAD)? *(you typed
> APOTEX)* — `[Use APTX.TO]` `[✕]`

`role="status"` (a suggestion is not an alert), the venue is Yahoo's human
name (`exchange_display`) falling back to the normalized code, and the
currency is the resolved listing's — the same value the FX seams key off.
Accepting writes the canonical symbol **and every autofilled field** (name,
kind, listing currency, exchange) in one action, so the fields can never
half-apply; nothing is stored until the user submits the form. A resolved
symbol identical to the typed one renders no chip — there is nothing to
correct.

**Several listings → a pick-one list, never a guess.** `APOTEX` matches both
`APTX.TO` (Toronto) and `APTX.NE` (NEO), so the form renders the chooser:

> Several listings match "APOTEX" — pick one
> `APTX.TO` APOTEX HEALTH CORP (Toronto, CAD)
> `APTX.NE` APOTEX HEALTH CORP (NEO, CAD)

The same chooser is the answer to the **dual-market collision** (M143, docs/07
§Candidate resolution): a bare ticker that also exists on the TSX — `VGRO`
(Virtus Silvant, NYSEArca, USD) *and* `VGRO.TO` (Vanguard, Toronto, CAD) — is
a question only the user can answer, so the form never picks bare-first:

> Several listings match "VGRO" — pick one
> `VGRO` Virtus Silvant Growth Opportuni (NYSEArca, USD)
> `VGRO.TO` VANGUARD GROWTH ETF PORTFOLIO (Toronto, CAD)

**Each row shows the CURRENCY** (`listingLabel(row, row.listing_currency)`)
because the currency *is* the choice: a bare venue code alone does not tell a
user that one option is USD and the other CAD, and getting that wrong is what
put the wrong instrument on security 294. The row also carries `kind` — a
candidate is a **create-ready listing**, so picking applies it directly (one
`GET /securities/lookup` per typed string, total). M142 used to send the pick
back through the endpoint; since M143 that would re-open the very collision
the user just answered, so `onPick` writes the listing into the form instead.
Nothing is stored until the user submits.

The same chooser renders in the sidebar search's market-data fallback
(`Search market data for "…"` → the same `CandidatePicker`), where picking
then offers the one-click **Add** row as before.

**No guessed currency, ever (M143).** The form's Currency field starts EMPTY,
not `"CAD"`: it is filled by the lookup for every listing that resolves, and
an empty value means *we could not identify this instrument*. Submitting one
posts `listing_currency: ""`, which the API stores as an **unresolved** row —
typed name, no invented venue, `dead_symbol` set up front, no backfill queued
— and `/securities/:id` shows `Unresolved symbol — no market listing found,
enter prices by hand` in place of the `exchange · currency` line
(docs/07 §Unresolved symbols). The pre-filled default made an unresolved row
indistinguishable from a resolved one until a price never arrived.

- **Touch points:** `frontend/src/components/investments/SymbolSuggestion.tsx`
  (new — `SuggestionChip` + `CandidatePicker`), `pages/Investments.tsx`
  (`SecurityForm`: the debounced probe, `applyListing`, the two offers,
  `EMPTY_SECURITY.listing_currency = ""`), `components/layout/AppShell.tsx`
  (the market-data fallback's `ambiguous` branch — the pick applies the
  candidate directly), `pages/SecurityDetail.tsx` (the unresolved line),
  `api/securities.ts` (`SecurityLookupResult.match` / `candidates`,
  `candidateListing`, `resolvedListing`, `listingLabel`).
- **Verification:** `frontend/tests/m142SymbolSuggestion.test.tsx` — the chip
  copy, the accept writing symbol + the three autofilled fields and posting
  the CANONICAL listing, the chooser + pick applying without a second lookup,
  continue-to-type clearing the offer, the probe's silence on a miss versus
  the button's warning. `frontend/tests/m143SymbolCollision.test.tsx` — both
  collision rows with their currencies, the CAD and the US pick each posting
  their own listing after ONE lookup, and a miss leaving no guessed currency
  behind.

## Watchlist ticker search — the market lookup fallback (M186)

**The reported defect.** Typing `cour` into the Watchlist card's combobox
printed **"No matching securities."** The Watchlist exists to follow a ticker
you do NOT hold, but its picker filtered only the tenant's STORED `securities`
(`GET /securities?limit=200`, filtered in the browser), so any listing never
added before came back empty-handed and the card's own *"add a symbol to
follow it"* promise was unreachable. The header search and the New security
form already reach the market through `GET /securities/lookup`; the watchlist
was the one add-surface that did not.

**Approach — an optional market fallback inside the SAME combobox.**
`components/investments/SecurityPicker.tsx` gains two OPTIONAL props:

```ts
onSearchMarket?: (query: string) => Promise<SecurityLookupResult>;
onPickMarket?:   (listing: SecurityInput) => void;
```

Both absent → the previous local-only behaviour, byte for byte. The **trade
form's picker stays local-only on purpose** (§Pages: "searchable
existing-securities picker" — a trade is recorded against a security you
already have; `+ New security` right beside it is that surface's create path).

**When the market is asked.** Local substring matching (symbol + name,
case-insensitive) runs first and its rows are unchanged. A probe fires only
when ALL of these hold:

1. `onSearchMarket` is present — the Watchlist passes it only once
   `securities.isSuccess` (`securities.isSuccess ? lookupSecurity : undefined`),
   so a query typed while the list is still LOADING never probes against an
   unknown list (and can never race a stored row into a duplicate create);
2. the trimmed query is **≥ 2 characters**;
3. **zero** stored securities matched — one local hit and the market is never
   consulted (the row you can already pick IS the answer);
4. that query has not been probed since it last settled (`probedRef`).

Like the M142 add-form probe, the request fires **600 ms after the query
settles** — one `GET /securities/lookup` per settled value, so keystrokes can
never become a request storm — and it is **silent on a miss**: a miss is a
state, never an error banner. The endpoint's own 30/minute limiter
(docs/08's `GET /securities/lookup` row) answers `429 {code:"rate_limited"}`,
which degrades to exactly that same silent miss.

**The listbox rows (exact shapes).** The fallback is *active* while its own
probe term is still the current query AND local matches are still 0:

| State | Row |
| --- | --- |
| pending | `<li data-testid="security-market-searching">Searching the market…</li>` — the no-match row is withheld while a request is out |
| `direct` / `suffix` / `search` | `<button role="option" aria-selected="false" data-testid="security-market-option-<SYMBOL>">` → `<SYMBOL>` + ` — ` + `listingLabel(result, result.listing_currency)` + a muted `· from market` tail — e.g. `COUR — Coursera, Inc. (NASDAQ, USD) · from market` |
| `ambiguous` | one option per candidate, `data-testid="security-market-candidate-<SYMBOL>"`, labelled `listingLabel(candidate, candidate.listing_currency)` so the CAD/USD choice is on the row itself (M143 rule) |
| miss | nothing extra — the existing `security-no-match` row keeps printing **"No matching securities."** |

An answer is dropped two ways, so a slow response can never paint itself over
a newer one: at arrival (`probedRef.current !== term` means a later probe is
already in flight) and at render (`market.term === query`, so a result for a
string the user has since edited does not show).

**Picking is ONE action, and it writes.** `onPickMarket(listing)` runs
`WatchlistSection`'s `addFromMarket` mutation:

1. reuse a stored row whose symbol matches exactly, when the loaded list has
   one; otherwise **`POST /securities`** (`createSecurity` — the same create
   the New security form and the header search use, so the 365-day backfill,
   M141's dead-symbol guard and M143's unresolved-row rule all apply
   unchanged);
2. **`POST /watchlist`** with that id;
3. invalidate `["watchlist"]` and `["securities"]`, toast
   **"Watchlist updated" / "<SYMBOL> added."**

The picker clears its query and closes on the pick, so the field ends empty
and the watched row appears below. Errors keep the card's existing
vocabulary: `409 already_watched` → "Already watched"; `409
duplicate_security` (another surface created the row while this list was
stale) → invalidate `["securities"]` and say **"Already in your list" /
"Pick it from the list and press Add."**; anything else → "Watchlist update
failed". Nothing is ever guessed into storage — an `ambiguous` answer is only
stored when the user clicks one of ITS rows (M143).

**The explicit Add path is untouched.** Local rows still only SELECT
(`value` = the security id) and the **Add** button / Enter does the writing,
exactly as before. The market row is the only row that acts on click, and it
is labelled so (`· from market`).

**The combobox contract the probe forced into the open (M186).** Running axe
over the OPEN watchlist popup — the only scan that can see it — reported
`aria-required-attr` (critical: the expanded `role="combobox"` named no node),
`aria-required-children` / `aria-required-parent` (critical: the listbox's
owned children were `<li>` wrappers around `role="option"` BUTTONS) and
`listitem` (serious: those `<li>` sat in a `ul` whose `role="listbox"` had
replaced the list role). All three were pre-existing — the popup's structure
predates M186 — and are now fixed the way §Tag UX already fixed the same
class on `TagPicker`:

- the input carries `aria-controls` (while expanded) + `aria-autocomplete="list"`;
- `role="listbox"` — and its `aria-label="Securities"` — exist ONLY while the
  popup can own an option; the pending / no-match popup is a plain `<ul>`
  carrying the same `id`, so `aria-controls` always resolves to a rendered
  node without a listbox that holds nothing (TagPicker's `No matches` shape);
- each option row keeps its `<button role="option">` (Tab/Enter activation is
  the keyboard contract here) inside an `<li role="presentation">`, so the
  listbox owns the options and the wrapper disappears from the a11y tree.

- **Touch points:** `frontend/src/components/investments/SecurityPicker.tsx`
  (the two optional props, the debounced probe, the three new row states),
  `frontend/src/pages/Investments.tsx` (`WatchlistSection`: `onSearchMarket`
  gated on `securities.isSuccess`, the `addFromMarket` mutation),
  `frontend/src/api/securities.ts` (unchanged — reuses `lookupSecurity`,
  `createSecurity`, `resolvedListing`, `candidateListing`, `listingLabel`),
  `frontend/scripts/a11y/axe_scan.py` (`_axe_source_path` — axe resolves from
  the nearest `node_modules`, frontend's own first, `AXE_CORE` overrides).
- **Verification:** `frontend/tests/watchlistMarketFallback.test.tsx` — a
  local hit short-circuits the probe; one probe per settled query; the found
  row's exact label and the create→watch sequence with its invalidations; the
  ambiguous chooser storing the PICKED candidate (currency and all) after one
  lookup; a miss keeping "No matching securities."; no probe while the list
  is still loading; the trade-form picker never probing.
  **Running stack:** `frontend/scripts/a11y/m186_watchlist_market_probe.py`
  (`A11Y_BASE=http://localhost:8080`, real Yahoo seam) — 27 checks green
  (`m186-watchlist-market-probe.json`): the stored-match short-circuit, the
  live market row `COUR — Coursera, Inc. (NYSE, USD) · from market`, the pick
  creating + watching + clearing the field, the FRESH-session confirmation
  that `COUR`/USD landed in `securities` and `watchlist` (and that the local
  hit was NOT auto-watched), the silent miss, and axe-core **0 critical /
  0 serious with the popup open at 1440 and at 390**.

## Exposure honesty — naming the Unclassified slice (M147)

A fund with NO `etf_holdings` rows decomposes its whole position to
`Unclassified` — before M147 that was 84.75% of Mark's portfolio ($1.22M)
with no explanation anywhere in the UI. The breakdown response now carries
`unclassified_funds` (docs/08) and the Exposure card renders it under the
legend table as a `role="note"` block, muted (`text-muted`, same weight as
the Unclassified row it explains):

> Unclassified because these funds' holdings haven't been imported yet:
> - VGRO — Vanguard Growth ETF Portfolio ($400.00): open the fund and use
>   Import holdings to fill this slice.

The note names TWO honest reasons (the payload's `reason` field), because
they need different words:

* **missing** (zero composition rows): "open the fund and use Import
  holdings to fill this slice" — one click fixes it (auto-enrichment,
  M147, empties this list after the next commit);
* **partial** (rows sum to <99 — the provider publishes only the top
  slice, docs/07; sub-1% residuals are noise and name nothing): "only 33.5% of this fund is imported — providers
  publish just the top holdings; the rest of the slice waits on a fuller
  source" — NO Import click can fix it; the fix is a new provider.

Rules (the copy must never lie):

* `missing` entries are ONLY funds with zero composition rows; `partial`
  entries are only funds whose OWN rows sum to <100 (a nested residual
  inside a fully-known fund-of-funds never claims either);
* each entry carries the fund's symbol, name and market value (`partial`
  also carries `imported_weight_pct`), so the Unclassified row reconciles
  to the list;
* the payload field is optional (`unclassified_funds?`) — an older API
  simply renders no note (pinned by tests/m147UnclassifiedCopy.test.tsx).

## Exposure honesty — labelling the estimate (M166)

M147's chain solved country / asset class / company, but SECTOR had no
source for the sub-holdings of the US-listed index funds: the Vanguard US
profile publishes no per-holding sector at all, so 62.77% of one real
portfolio's sector pie sat `Unclassified` while the very same companies were
already labelled elsewhere in that user's own cache (docs/16 §Sector chain).
M166 fixes the classification with a read-time tier chain, and fixes the
honesty with a label.

The breakdown response carries `estimated_cad` (total) plus a per-row
`estimated_cad` and `estimated` (docs/08). A slice is estimated ONLY when it
was attributed from a fund's own PUBLISHED sector mix — the cross-fund index
and the static ticker map are exact facts about a company and are reported
sourced. The Exposure card renders it in two places:

* a muted **`est`** marker after the bucket name in the legend table
  (`text-[10px] uppercase tracking-widest text-muted`, no colour, no badge —
  it must read as a caveat, not a highlight), whose `title` states that
  bucket's own number: *"20,368.08 of this slice is allocated from the fund's
  own published sector mix."*;
* a `role="note"` block under the legend, in the same voice and same
  `text-muted` weight as the M147 note it sits beside:

> ≈3.4% of this pie ($49,757.41) is allocated from each fund's own published
> sector mix, not from a per-holding figure — the rows it feeds are marked
> **est**. Company, country and asset class stay exact.

Rules (the copy must never lie):

* the marker and the note key off the NUMBERS (`estimated_cad`), never off
  the dimension — a portfolio whose funds all publish their rows renders
  neither, and geography / asset class / company always report `"0.00"`;
* a bucket can be part sourced and part estimated, so the note quotes the
  dollar total and the table marks the row rather than replacing its value;
* "Company, country and asset class stay exact" is a claim the payload
  proves: `estimated_cad` is `"0.00"` on those three dimensions;
* both fields are optional (`estimated_cad?`, `estimated_cad?` per row) —
  an older API renders neither the marker nor the note (pinned by
  tests/m166EstimatedCopy.test.tsx).

## Exposure visuals — the world choropleth and the Other fold (M167)

Mark's mobile screenshot of /investments → Exposure → Geography: the list was
correct and unreadable — forty countries deep, and nothing to look at. M167
gives the Geography view a world map and folds EVERY dimension's list to a
head you can scan plus an `Other` bucket behind an expander. Still no new
chart library: the map is hand-rolled SVG on the ChartFrame family's terms
(one labelled group, focusable labelled children), and its geometry is
generated once at authoring time.

### The choropleth — `components/charts/ExposureWorldMap.tsx`

* **Generated geometry, zero runtime cost.** `frontend/scripts/gen_world_map.py`
  projects Natural Earth's public-domain 1:110m Admin 0 Countries layer into an
  equirectangular frame (640 × 256 viewBox units, latitudes clipped to
  −60…84 so Antarctica's band and the empty polar caps do not eat the frame)
  and writes `components/charts/worldMapData.ts` — 181 countries, 49.6 KiB,
  committed. The browser never parses GeoJSON and never fetches anything; the
  app gains no dependency. Re-run the script with the same source file to
  regenerate; the coordinates are never edited by hand.
* **A dot is a country too.** Anything below the 110m polygon floor (Malta,
  Bahrain, Liechtenstein) — and the entities that layer does not carry at all
  (Hong Kong, Singapore, Faroe Islands) — ships as a hand-placed dot at its
  capital's coordinates, with a transparent stroke halo as the hit area. A
  country that exists must always be shadeable and focusable.
* **A six-step sequential scale in house tokens.** Fixed buckets — Under 1% ·
  1–2.5% · 2.5–5% · 5–10% · 10–25% · 25% or more — mixed in linear light from
  the card's surface toward the donut's OWN first-slice accent
  (`palette.ACCENT`), so the map is the same palette as the pie, in both
  themes. `palette.SURFACE` / `SURFACE_RAISED` mirror the `index.css` tokens
  (a CSS variable cannot be interpolated in JS) and
  tests/m167Choropleth.test.ts pins them to those token blocks, so the mirror
  cannot drift.
* **The measured floors** (same test, CIE76 ΔE in Lab, D65):

  | theme | loudest stop vs card | loudest stop vs neutral land | lightest stop vs neutral land |
  |---|---|---|---|
  | dark | 10.2:1 | 10.8:1 | 4.45:1 (ΔE 75.5) |
  | light | 8.3:1 | 7.6:1 | 1.43:1 (ΔE 24.4) |

  The light theme's quietest stop is a LAVENDER TINT of the near-white card,
  not a 3:1 boundary — a six-step ramp that started at 3:1 would leave one
  usable step in a light theme. What must hold does hold: the quietest stop is
  unmistakably a different colour from the neutral land (ΔE 24.4 ≫ the 8
  floor the test pins), and every number the shading claims is printed in
  text beside it (legend + table + readout), so the map never carries
  information that only the eye can get.
* **No invented zeros.** A country with no exposure and a country the payload
  never mentioned render the SAME neutral land (`palette.SURFACE`, hairlined
  in the card's own colour). The map shades what the portfolio holds and
  nothing else — M147's "not shown" vs "zero" distinction, drawn.
* **Buckets it cannot draw are printed, not dropped.** The holdings `region`
  column can answer a REGION ("North America") and a provider can emit a name
  with no polygon; those rows are listed under the map with their weight, and
  the map's own summary says how many buckets sit outside it.
* **The table stays the source of truth.** Every shaded country is a
  `role="button"` labelled `"United States, 44.70% of exposure"`; hover, focus
  or tap drives a `role="status"` readout (name · weight · value — masked
  under M139 like every other money surface, percentages stay visible), and
  activating one scrolls to and highlights that row, unfolding the list first
  when the fold is what hides it. Unshaded countries are `aria-hidden`, so the
  map is ONE labelled group for a screen reader, not 181 stray nodes.
* **Mobile.** The map sits above the list at full width and collapses behind
  a `Hide map` / `Show map` disclosure (`aria-expanded` + `aria-controls`)
  below `lg` only — at `lg` and up it is always present, so resizing never
  eats the visual. No horizontal scroll at 390: the SVG is `viewBox`-scaled,
  never a fixed width.

### The Other fold — `frontend/src/lib/exposureFold.ts`

* **One rule, four dimensions.** Sector, geography and asset class fold at 8;
  companies — potentially hundreds of tickers — fold at 10. The donut is fed
  the SAME folded list the table renders, so a pie slice and a table row can
  never disagree about what "Other" contains (M167 removed the company-only
  ad-hoc fold that used to live inline in the page).
* **`Other` is arithmetic, not an estimate.** Its market value is the CENT-sum
  of the rows it folds and its weight their exact sum, so head + Other === the
  full list — pinned in cents, because forty float weights drift a dollar
  otherwise.
* **`Unclassified` is never folded.** "Other" means "the long tail you chose
  not to read"; "Unclassified" means "the pipeline has no answer". The honesty
  bucket keeps its slot at any rank, and the M147 note under the table keeps
  naming its funds.
* **The expander is a button, and it remembers.** "Show remaining 32 countries
  (3.10%)" → the full sorted list inline, then "Show the top 8 countries only"
  back, with `aria-expanded` on the control and `aria-controls` on the tbody.
  The choice is remembered per SESSION in sessionStorage
  (`fintrack.exposure-expanded.v1`) — M139's pattern: a new tab starts
  collapsed again, and nothing is written server-side. **Intentionally
  global** (recorded as such under security follow-up F-6): the payload is the
  dimension VOCABULARY (`sector` / `geography` / `asset_class` / `company`) —
  no row id, no amount, no tenant data — so the only cross-tenant effect is
  which sections are expanded; every storage key that carries identifiers IS
  tenant-scoped (`lib/tenantStorage.ts`).

### What did not change

No endpoint, no payload field, no server code: both surfaces are list-level
behaviours over `GET /portfolio/breakdown` exactly as it already answers, and
the four dimensions' tables keep their columns, their sort and their honesty
notes.

### Verification

* `tests/m167Choropleth.test.ts` — bucket boundaries, both ramps hex for hex,
  monotonicity, the contrast/ΔE floors above, and the palette↔`index.css`
  token pin.
* `tests/m167ExposureFold.test.ts` — the fold's cent-exact arithmetic, the
  Unclassified exemption at any rank, the per-dimension depths, the session
  memory round-trip.
* `tests/m167ExposureTruncate.test.tsx` / `tests/m167WorldMap.test.tsx` — the
  real Investments page against 12-row fixtures: collapse, expand, totals
  identity against the donut's centre label, map → row selection (including a
  folded-away country unfolding the list), keyboard activation, M139 mask
  inheritance, and the "not on the map" honesty line.
* Deployed: axe 0 over /investments at 390/412/1440 in both themes, no
  horizontal scroll at 390, keyboard walk of the expander and the shaded
  countries.

## Exposure drill — clicking a bucket to see what made it (M169)

Mark's follow-on to the map: "neat would be to drill down what security or
fund is from that place and in the case of fund what holding(s) if possible."
The engine had the trail all along; this is the surface that shows it.

### Why an expand panel, not navigation

`components/ExposureDrillPanel.tsx`, mounted INSIDE the breakdown table under
the row it describes. The question the user is asking is always comparative —
"Canada is 20.5%, *and these are the names*"; "VEQT is the biggest slice of
it, *and this is what's inside it*" — and a page navigation destroys the
comparison the moment it answers the question. M126 established this for a
position's price history and the fold (M167) relies on the same property: the
Exposure card stays on screen, the drill opens under the row, and the map, the
donut, the totals and the honesty notes are all still readable beside it.

One drill is open at a time. A table with five open drills is not a table.

### The wire is one query parameter

`getBreakdown(dimension, accountId, drill?)` — the SAME
`GET /portfolio/breakdown`, one extra parameter carrying the bucket's exact
`key`. A caller that never opens a drill sends no second request and receives a
payload byte-identical to the pre-M169 one (docs/08 §Breakdown drill). The
panel's own query is keyed on `[breakdown, dimension, accountId, "drill", key]`
with a 60s `staleTime`, so re-opening the same drill within a minute is free.

### What the panel renders

* **The server's total, always.** The header quotes `contributions_cad` —
  the server's number — because each row rounds to cents independently and the
  rows can drift a cent or two from the bucket. The client never re-adds them
  (M91's anti-fabrication pin, applied to a drill).
* **The `via` chain, in words.** A leaf reached two funds down prints
  `via VEQT → VXUS` beside its name. That single string is the difference
  between "you hold Microsoft" and "your money reaches Microsoft of the USA
  through VEQT, which holds VXUS, which holds Microsoft" — and it is the walk's
  own path, never a reconstruction.
* **The provenance badge, per row.** `issuer`/`vanguard` → a tiny `issuer CSV`
  label (a first-party holdings file); `yfinance` → `top holdings` (a scrape of
  the fund's top slice, which is thinner and says so); `manual` → `manual`.
* **The estimate and the residual, as rows.** M166's fund-mix split renders as
  a row on the FUND's own line, marked `est` with a tooltip quoting the dollar
  amount; the denominator residual renders as `not placed`. Both exist because
  those dollars are really in the bucket and no holding accounts for them —
  merging them into a total would make the drill fail to reconcile.
* **A fund's holdings, on demand.** The contribution row carries a chevron;
  opening it lists the leaves sorted by contribution weight, each indented
  under its fund. A fund with NO imported holdings says `no holdings imported`
  instead of offering a chevron that opens a hole.
* **The depth cap, disclosed.** A footnote states how far the walk goes and
  that anything deeper stays inside its fund — the server's `depth_cap`, not a
  client-side limit.

### a11y and mobile

The bucket row is clickable, and the chevron beside its weight is the keyboard
path: a real `<button>` with `aria-expanded` and `aria-controls` (M126's
contract, so a screen reader hears the state and Tab reaches it). `aria-controls`
is set only while the panel exists — an idref with nothing behind it is an
invalid ARIA value (axe's `aria-valid-attr-value`). A fund's own holdings toggle
carries the same contract, named `Show <symbol> holdings` / `Hide <symbol>
holdings`. The drill is a stack of rows, not a nested table, so it renders at
390 with no horizontal scroll; values mask under M139 via the ambient
`fmtCad`, while percentages and names stay visible.

### Verification

* `tests/m169ExposureDrill.test.tsx` — the real Investments page against a
  three-country fixture: the drill opens inline (the table survives), the
  request carries `?drill=Canada`, no drill is fetched for a bucket nobody
  opened, the `via` chain renders both hops, a fund's holdings expand and
  collapse through the keyboard-reachable toggle, the server's total is what
  renders, the provenance badges read `issuer CSV` / `top holdings`, the M166
  estimate and the residual each carry their flag, a 30-holding drill folds to
  25 plus an `Other` whose head+tail is exactly the contribution, the mask
  hides values while percentages stay, the depth cap is disclosed, a second
  click closes the drill, a 404 renders in place without blanking the table,
  and every dimension offers the drill.
* Backend: `backend/tests/test_m169_exposure_drill.py` (18 tests) pins the
  wire, the reconciliation and the depth honesty (docs/16 §Verification).

## FX staleness chip (M150)

`components/ui/FxStalenessChip.tsx`, mounted in the **Currency Tracker
header** (`/currencies`, docs/15). It is the user-visible half of the M150
rate-book refresh (docs/07 §Scheduled refresh): every exact-date conversion
carries a rate forward up to 7 days, so a book that stopped being fetched keeps
answering — off an older rate, with nothing erroring and nothing logging. The
chip says whether that is actually happening.

- **Data**: one `GET /fx/status` per mount (`["fx","status"]`,
  `staleTime: 60_000`) — cache-only server-side, so mounting it can never
  trigger a BoC fetch.
- **Three states, all server-derived** (no client-side age math, no second
  threshold to drift):

  | state | copy | treatment |
  | --- | --- | --- |
  | current | `FX · 2026-10-09 · 1d` | `Badge` neutral |
  | stale | `FX rates stale · 9d` | `Badge` danger + pulsing dot |
  | a pair with NO rate | `FX rates missing` (…`· EUR` naming only the missing ones) | `Badge` danger + pulsing dot |

- **Nothing at all** while loading, on an error, and when `pairs: []` — a
  tenant holding no foreign currency has no FX dependency to report on, and a
  status chip is never worth breaking a page for (no spinner, no error text).
- The hover `title` carries the per-pair detail
  (`USD/CAD 2026-10-08 (2d) · EUR/CAD missing`) plus the one sentence that
  explains what the number means — the degradation rule from docs/09's
  opening: a degraded state explains itself at the moment it degrades.
- Only semantic tokens (`Badge` tones, already contrast-audited in both
  themes); the pulse respects the shared `animate-pulse` treatment the price
  staleness badge already uses, so `prefers-reduced-motion` behaves the same
  here as everywhere else.

**Verification:** `frontend/tests/m150FxStaleness.test.tsx` — the current copy,
the pulsing stale badge (the pulse lives on the Badge's dot, not the label),
the missing-vs-partially-missing copy, the deliberate nothing on empty/error,
and the chip riding in the `/currencies` header.

## Dividend source strip (docs/07 §Dividend-view provenance)

`components/reports/DividendProvenanceStrip.tsx` — one `Source` row of `Badge`s
answering, for every dividend view, the three questions a projection or an
aggregate cannot answer itself: WHICH source produced these numbers, WHEN it
last answered, and whether it is stale or missing. It is the dividend twin of
the FX staleness chip above and of the price `source · age` badges, and it
reuses their treatment deliberately.

- **One shape in, badges out** (docs/07 §Dividend-view provenance): the payload
  of `GET /reports/dividends` and `GET /reports/dividend-calendar`, or — for
  `GET /trades/dividend-suggestions` — `provenanceFromHistory` folding that
  route's flat `X-Dividend-History-State` header into the same shape
  (`api/dividendProvenance.ts`).
- **Badge text is the content**: `label · provider (only when the label does not
  already name it) · as_of · age · (stale)|(missing)`, so the state word is
  never hidden behind the hover. The per-source sentence (WHY it is stale or
  missing) rides in the container's `title`, following docs/09's rule that a
  degraded state explains itself at the moment it degrades.
- **Treatment**: `Badge` neutral for `ok`, `danger` + pulsing dot for
  `stale`/`missing` — the same tokens the price staleness badge and the FX chip
  use, so both themes are already audited and `prefers-reduced-motion` behaves
  the same everywhere.
- **Mounted**: `DividendsPanel` (under the title, ABOVE the empty state),
  `DividendCalendarPanel` (above the table, and above the empty state) and
  `DividendSuggestionsSection` (above the prose notice). The strip prints dates,
  counts and labels only — never money — so a mask flip never changes it
  (docs/46). It renders nothing when the payload has no `provenance` (an older
  server) instead of showing a stub.

**Verification:** `frontend/tests/dividendProvenance.test.tsx` (both report
panels, the header adapter's missing/stale/ok transitions, the pulsing badge and
the deliberate nothing) and `frontend/tests/dividendCalendarPanel.test.tsx`
(the ledger strip and the missing verdict above the empty state).

## Ledger redesign (M185) — the approved mockup mapped to real routes

**Spec: [`docs/47-ledger-redesign.md`](47-ledger-redesign.md).** It is the
implementation source of truth for the Ledger wave (design artifact
`fintrack-ledger-design-revision-5.zip`, md5 `12b08f221d75f2f4d4d4210ff3857f01`;
UX final gate `t_b02664ad` READY; parent epic `t_d4216d9f`; baseline
`4887221`). This section is the index; docs/47 carries the full contract.

What it pins, in one list:

- **Surfaces** — `TopBar` + `More` panel + `TruthBar` (desktop ≥ 768) and a
  five-tab bottom bar with a centre **Add** plus the drawer (< 768); routes are
  untouched (docs/47 §1, §2's screen→route→endpoint map).
- **States** — one taxonomy (route-loading, query-loading, empty, error+retry,
  stale, unpriceable, dead-symbol, E2EE, read-only demo) with the render and
  the testid each; the compact `state-row` is **additive** to the three-part
  `EmptyState` contract, never a replacement (docs/47 §3).
- **No faux locks** — the artifact's 90 padlocks are a demo-tenant fact: the
  product's `DemoLock` renders only for `isReadOnlySession(user)` on a
  genuinely blocked mutating path (mirroring `backend/app/api/deps.py`'s
  exemption list), stays enabled, explains itself locally from the real
  `403 demo_account_restricted`, and never appears on a live tenant. Downloads
  are reads and are never locked (docs/47 §4).
- **Tokens / truth** — the Ledger themes re-skin the existing semantic tokens'
  values in both blocks; accent is never money, money is never accent; a
  comparison never appears without a basis derived from payload dates;
  unknowns render `—`, stale renders the real number plus its as-of date
  (docs/47 §5, §9).
- **API gaps** — 24 rows adjudicated against the baseline payload: what ships
  from existing fields, what is derived by a documented pure function, and what
  is dropped. No backend work exists in this wave (docs/47 §10).
- **Ownership** — docs: ux; `frontend/src/**`: soft-dev; new automation only in
  `frontend/tests/ledger/**`: tester; acceptance and security read-only
  (docs/47 §12), with 17 exact acceptance examples (docs/47 §13).
- **PM binding decisions W1/W2** — `GET` exports stay live on the demo tenant
  (never locked, never disabled; `docs/11` §Demo tenant unchanged), and the
  demo `Test connection` control is disabled **before** dispatch with its exact
  local copy and no request sent — the single named exception to the
  "a locked control stays enabled" rule, with no new demo exemption
  (docs/47 §4).

## Report-only Ledger performance probe

`frontend/scripts/perf/ledger_perf.py` measures cold-route and warm 390px interaction budgets without changing the target application. It is a local/report-only tool, not a CI gate or a deployment step. Every invocation that can visit protected routes must name an environment variable containing an already-issued read-only bearer token. Targets default to loopback (`127.0.0.1`, `localhost`, or `::1`); a remote target is refused unless its exact HTTPS origin is explicitly supplied with repeatable `--allow-origin`.

```text
READ_ONLY_TOKEN=<pre-provisioned token> \\
  python frontend/scripts/perf/ledger_perf.py cold \\
  --targets local=http://127.0.0.1:4173 \\
  --auth-token-env READ_ONLY_TOKEN --out /tmp/ledger-cold.json
```

The probe never clicks the demo control, calls `POST /auth/demo`, logs in, refreshes, or otherwise provisions an account. The token is held only in the probe process and an isolated browser's in-memory request wrapper; it is not written to the report. Without `--auth-token-env` or a non-empty named variable, the command refuses to start Chrome. The supplied token must be for a throwaway/read-only test tenant; the harness cannot make an arbitrary token read-only.

Targets are validated before any output or browser start. They must be `http` or `https` URLs with no userinfo, query string, or fragment, and must be loopback or match an exact origin passed via `--allow-origin https://host.example`. Approved remote origins must use HTTPS; a path on the target is allowed, but it does not broaden the origin allowlist. Reports contain only the validated target and route paths. The bearer wrapper is installed only for the currently approved exact origin, strips authorization from other origins, rejects unsafe methods (anything other than `GET`, `HEAD`, or `OPTIONS`), and uses `redirect: "error"` for API reads. Each run creates a private 0700 browser profile and asks Chromium for an ephemeral loopback debugging port. The profile carries an ownership marker; the harness verifies the browser command line and loopback DevTools endpoint before attaching and removes files only when the marker is still owned by that run. It never uses shared CDP ports or a fixed profile.

Readiness requires both the expected page heading and the exact requested `location.pathname`. A redirect to another page is reported as `route_mismatch` with `expected_path` and `path`; a redirect to `/login` or `/register` is reported as `auth_redirect`. The JSON remains an observation report even when a route is refused or a phase fails.

Verification: `python -m pytest frontend/scripts/perf/tests/test_ledger_perf_safety.py` covers loopback-default and exact-origin allowlisting, malicious remote rejection before browser launch, URL rejection without secret echo, origin-bound bearer injection, unsafe-method blocking, per-run ownership cleanup, and wrong-path failure. A safe local invocation must use a throwaway read-only token and a local target only; no production target or deployment is part of this probe's contract.

### Deployed Ledger baseline (2026-10-17, release `7fad455f`)

The definitive post-deploy numbers, captured with the probe above against the production origin (`--allow-origin https://fintrac.princessandmark.com`; the read-only demo-tenant bearer was issued by `POST /api/v1/auth/demo` outside the probe, one fresh token per phase). Shared-box conditions are recorded as load-average brackets per phase: 3.1–8.0 during this capture, while the 2026-10-16 local baseline itself was bracketed at 8.3–12.9 — conditions were equal or better, and the brackets are the comparability record. Medians of 3; per the adopted gate, the 390px cold baseline pools three invocations at runs=1 with shuffled route orders (reports-first, transactions-first, imports-first) to de-confound the route-order/API-cache warming that made the 2026-10-16 mobile-cold numbers inconclusive.

Cold route timings @ 1440×900, median [min–max] ms, adopted T1 ceilings in parentheses — all PASS:

| route | ttfb | fcp | heading | settle | lcp |
|---|---|---|---|---|---|
| dashboard | 50 | 120 | 460 [248–544] (≤1,250) | 896 [647–912] (≤2,450) | 1,168 |
| transactions | 40 | 112 | 225 [205–404] (≤1,200) | 582 [417–668] (≤1,950) | 520 |
| imports | 37 | 112 | 211 [188–270] (≤1,000) | 508 [406–555] (≤1,750) | 352 |
| investments | 47 | 144 | 333 [302–347] (≤1,050) | 1,121 [900–1,243] (≤3,150) | 484 |
| reports | 43 | 116 | 294 [274–398] (≤1,050) | 566 [426–623] (≤1,750) | 572 |

Every median sits under both the 1.5× ceilings and the local-build baseline medians (heading 650–801, settle 1,131–2,080). These deployed medians are now the paired-run regression reference used by `thresholds.json`'s adopted policy: warn at >10% regression in heading or settle, block at >20% with valid repeats.

Bundle (T2, raw bytes listed from the deployed web container's `assets/` directory): entry JS 544,435 (≤560,200), total JS 1,811,962 across 103 chunks (≤1,866,000), CSS 50,895 (≤52,400) — PASS.

Cold @ 390×844, pooled over 15 clean runs (order-controlled; no hard cold-mobile threshold is set yet — regressions use the paired policy): heading pool 273–616, settle pool 449–1,494, LCP 232–1,352, horizontal overflow 0 on every run. CLS remains data-shape dependent (investments 0.582; tracked under T4, not gated).

Warm 390px budget, median [min–max] of 3, adopted T3 — all PASS:

| workload | measured | gate |
|---|---|---|
| overflow (dashboard/transactions/reports) | 9/9 == 0 px | 0 px hard |
| scroll p95 frame | 16.7–16.8 | ≤50 med; hard fail >100 |
| long tasks during sweep | 0 per sweep | ≤250 ms max |
| tab switch | 499 [389–682] | ≤1,500 med; investigate >2,500 |
| More open / close | 419 [408–419] / 349 [333–353] | ≤700 / ≤600 |
| Add first / repeat | 532 / 364 [361–366] | ≤1,500 / ≤800 |

Re-run protocol: on an idle-as-achievable box, one fresh demo token per phase; medians of 3 with loadavg brackets recorded before and after each phase; for the 390px cold baseline, three invocations at runs=1 with different `--routes` orders, pooled. Raw run records and phase logs live with the measurement report (board attachment for t_03ccfc6f).

## File & naming conventions

- Pages: `src/pages/<Name>.tsx`, one per row of the Pages table above.
- API clients: `src/api/<resource>.ts` named after the backend router it wraps (docs/08 resources); shared fetch/refresh logic lives only in `client.ts`.
- Components: PascalCase `.tsx` files grouped by feature under `components/layout/`, `components/charts/`, `components/transactions/`, `components/settings/`, `components/investments/`, `components/reports/`, `components/ui/`.
- Utilities: camelCase modules in `src/lib/` (`privacyBackup.ts`, docs/11); shared API payload types in `src/types/`.
