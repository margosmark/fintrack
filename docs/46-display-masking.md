# 46 — Display masking (M139)

A **render-time** mask that replaces every absolute dollar figure on screen with
`•••••`, so a user can hand their laptop to someone else — or turn toward a
camera — without the balances walking out with them.

This is **not** `docs/11` privacy mode. Privacy mode is an *egress* gate: it
decides what leaves the browser. M139 is a *presentation* fact: what the
already-fetched numbers look like on the glass. The two compose, and the mask
never reaches the wire.

## Context

- `docs/09` §Rail — the shell's account menu and its one-tap session controls.
- `docs/11` § privacy mode — the contract M139 must not disturb. `mask.ts`
  imports nothing from the egress path and the server is never asked to mask.
- Milestone M139 in `docs/10`.

## Decisions

### D1 — Session-scoped, restore-on-load OFF

| Property | Value |
| --- | --- |
| Storage | `sessionStorage`, key `fintrack.mask` |
| Survives SPA navigation / remounts in the tab | **yes** — this is the point |
| Survives a hard reload of that tab | **no** |
| Survives a new tab / window | no |
| Survives logout | no |
| Restore on boot | **never** |

`sessionStorage` is chosen for exactly one reason: it outlives route changes
and component remounts **within the tab**, so a user who masks on the Dashboard
and clicks through to Investments stays masked, and the chip never flickers
unmasked mid-navigation. It does not cross a tab boundary, and a boot **clears**
any stale key and starts from the tenant default.

That last part is deliberate, and it is the harder half of the rule. The mask
is a deliberate, in-the-moment act; resurrecting a half-remembered choice on a
fresh page load would make the control untrustworthy — the user would have to
remember what they last did before they could be sure what they are now seeing.
`initMaskForSession()` is the only writer on boot, and it runs on BOTH boot
paths: `AuthContext::applyUser` whenever the identity changes, and the
session-restore effect that seeds `cacheOwner` directly. A restored tab is still
a boot, so it must not skip this — but until t_2d8ffedb it did, because restore
deliberately bypasses `applyUser` to keep the cache it is repopulating
(`t_2d8ffedb` P1: a restored tab kept the stale key AND skipped the demo
default, so the chip said "Unmasked" while the key said masked, and a returning
demo visitor saw unmasked figures on a public storefront). (Verified against the
deployed stack: `scripts/a11y/m139_mask_probe.py` drives an SPA jump to
`/investments` and requires the mask to hold across it, while a fresh tab must
fall back to the tenant default rather than inheriting the prior tab's choice.)

**Scope: every re-entry, including a silent session drop.** There is no separate
"session-drop path" in this code — `demoLogin` and every other entry funnel
through `applyUser`, and the only way a tab re-enters without it is the
reload/restore above, which is now covered. That covers the boot that FAILS too:
the probe runs the same contract when it found no session, because D1 does not
exempt a boot that never found a session (the stale key would otherwise outlive
it until the next successful transition). The failed probe reaches that contract
through its **resolve-null** branch — `postRefreshToken` returns `null` on a
non-ok status and on a thrown fetch and never rejects (`src/api/client.ts`), so
a `.catch` alone is dead code; the catch is kept for a probe that throws. A drop
that leaves the SAME identity in place keeps the user's own choice: the default
is a default, not a lock.

The one default is the **demo tenant** (`isReadOnlySession(user)`): a public
storefront boots masked via `applyDemoMaskDefault()`. It is a *default*, not a
lock — the first tap un-masks it, and any explicit user choice wins.

### D2 — What masks

Absolute CAD/USD quantities, and anything derived from them, on every surface
the shell owns: balances, market value, gain, prices, deltas, totals, chart
**axes** and chart **tooltips**.

A masked chart keeps the **shape** of the line and loses the scale: ticks render
`•••••`, so the series stays readable as a trend.

**Percentages are shown, not masked.** A percentage is proportional — it says
nothing about scale, so it leaks nothing the mask was hiding. The ruled-visible
set:

- budget **% remaining**
- **category-slice percent labels** on the donut
- **spending-change percentages** (M103's honest 2dp)
- **growth / period delta percentages**
- **TWR / MWR percent**

**One carve-out: book-cost-derived percentages follow the mask.** The
position-level **"Your gain %"** on `Investments`, and SecurityDetail's
book-vs-market gain %, are ratios *against a masked absolute*. Printing
`%+4.63` beside `••••••` hands a determined shoulder-surfer back the very scale
the mask removed.

These render through **`fmtSignedPctBookCost`**, a separate function rather
than `fmtSignedPct(v, true)`. The difference between the two is semantic — "is
this ratio derived from a masked absolute?" — and a boolean at the call site
says nothing about which one it is; a named function does. It *follows* the
session like an amount (`isMasked()`), so an unmasked user still sees the
figure; per-surface flips require UX testing that shows zero de-mask risk
first. Its null/NaN check runs **before** the mask check: a missing value is
not a hidden one, so absence stays an em dash either way (M65 honesty).

**A chart's TEXT ALTERNATIVE is a money surface too (F-9).** D2 says the
shell's money hides on "every surface", and a chart's `aria-label` is not a
comment on the chart — for assistive tech it *is* the chart, read on every
load with no hover needed. Two shapes were printing raw figures on a masked
`/investments` (security re-verification of `6d4e445`, F-9): the
currency-exposure donut's label ended `CAD 77%; total 211004.` and its hover
tooltip read `47743.27 USD`, while every other money surface in the same
viewport rendered •••••. Both were money built INSIDE a formatter callback —
the exact class `scripts/sweep_money.mjs` cannot see (F-7 was the same shape),
which is why the pin is behavioural (`tests/f9ExposureDonutMask.test.tsx`).

The defect was structural, so the fix is too. The shared `Donut` builds its
money fact as `total ${centerFormat(total)}` — correct for `CategoriesDonut`,
whose centre IS the money total and whose default `centerFormat` is the
mask-aware `formatCadCompact`. `ExposureDonut` overrides `centerFormat` with
its **holdings-count** formatter (its centre is a count: the slices are native
values in different currencies, so one summed "total" would mix denominations),
and that count formatter rode into the label as a bare `String(Math.round(n))`
— a cross-currency sum printed raw. So:

- `Donut` grew **`totalFact`** (string | null): the caller owns the label's
  money fact when its centre is not the money total. Omitted → the mask-aware
  default, byte-identical for every existing caller.
- `ExposureDonut` passes one fact **per currency** through `fmtNative` — the
  same mask-aware helper the table beside it uses — and the same for its
  tooltip, and it calls `useMasked()` itself (D6: the tooltip formatter is a
  closure built in that component, so a flip must re-render *it*, not just the
  `Donut` it hands the prop to).

Verified: masked label reads `… CAD 77%; •••••; •••••.` — the share survives
(D2's percentage ruling), the scale does not; unmasked reads
`… 163,260.73 CAD; 47,743.27 USD.`; both directions on one mount, same node.

**Raw JSX interpolation is invisible to the sweep — the trades log (F-10).**
The sweep tracks calls to the mask-aware helpers, so two cells that printed
`{trade.price_native}` and `{trade.commission}` outright were never in its
scope: the `/investments` **Trades log** rendered **25 rows / 42 raw money
cells / 0 masked cells** (`58.34000000`, `136.29000000`, `21000.00000000`,
`0.00`) while the four sibling tables in the same viewport rendered ••••• —
and neither cell re-rendered on a flip (security final signature of
`164a33b`). D2 already covers it: **prices** are on the list, and D4's
working-surface exemptions (import wizard, reconciliation, Settings, receipts,
export previews) never extend to a read-only log table. So:

- both cells route through **`fmtNative`**, the mask-aware native-amount helper
  the positions table beside them uses, resolving the currency exactly the way
  the server does — the row's own `trades.currency`, else its **ACCOUNT's**
  currency (docs/02 §trades, M132; the `sankeyEngine` is the other consumer of
  that chain). An account outside the 200-row lookup prints the bare figure
  rather than a fabricated ISO code;
- `TradesLog` subscribes with **`useMasked()`**, so a flip repaints the cells
  (D6);
- a zero commission masks to bullets like any other amount, in BOTH of the
  shapes the cell can hold: the live `commission` wire value is
  `Numeric(18,2)` so it arrives `"0.00"` (unmasked it prints `0.00 USD`), and
  the cell's em-dash guard is the literal `commission === "0"` — unmasked that
  one still prints its `"—"`, masked it is bullets. A masked row therefore
  never says "no fee" out loud.

Verified: masked, every one of the six money cells (3 price/amount + 3
commission) reads ••••• and no `\d{1,3}(,\d{3})*\.\d{2}` survives anywhere in
the table region; unmasked, the same rows print `58.34 CAD` / `136.29 USD` /
`21,000.00 CAD` (the NULL-currency row taking its account's code), `9.99 CAD`
and the zero shapes described above; both directions on ONE mount repaint the
same `<tr>` nodes with an unchanged API-call fingerprint and zero `fetch`
calls (`tests/f10TradesLogMask.test.tsx`, 3 of 3 red on the pre-fix tree).

Verified live (AGENTS §4 step 5, no docker on this host): the worktree's own
stamped `dist` served by `vite preview` with `/api` proxied to the deployed
backend, headless Chrome, demo tenant — **25 rows / 175 cells**: masked, 50
bullet cells and **0 money-shaped cells** with the chip `aria-pressed=true`;
after clicking the shipped chip, 50 money cells (`58.79 CAD`, `0.00 CAD`,
`135.02 CAD`, `2,000.00 USD`, `21,000.00 CAD`, …), 0 bullets, chip
`aria-pressed=false`, **0 new `/api/` requests across the flip and 0 console
errors**. Screenshots: `f10-trades-log-masked.png` /
`f10-trades-log-unmasked.png`.

**A table under a masked axis is not exempt (F-11).** `/forecast` carried
three surfaces that printed raw absolutes while everything around them hid —
all of them strings the SERVER composed, which is the class the D6 sweep can
never see (it tracks calls to the shared helpers):

- the **"End value per series" table** printed the response's own strings
  (`Baseline 372566.98 0.00`) directly beneath a y-axis reading `•••••`; F-7
  fixed the axis, and the table — a separate surface — was left behind;
- the **Assumptions list** (`lib/forecastCopy.ts`) built `Net worth (CAD):
  231305.38`, `Cash … 482.12`, `Positions … 230823.26`, `Income … = recurring
  … + residual …`, `Expense … = recurring … + residual …` with **zero** mask
  references anywhere in the module;
- the **scenario event rows** (`House purchase 2030-01-01 — 600000.00 price,
  120000.00 down`) — the same class, found while fixing the other two: the
  demo tenant seeds no forecast scenarios, so the security sweep never
  rendered a row.

The fix preserves the page's own honesty rule (docs/45 §Boundary: a figure is
the response's string, never re-parsed or re-rounded) — each site *branches*
on the mask rather than running through `formatCad`:

- `Forecast` subscribes with `useMasked()` and its table cells, its
  `buildAssumptions` call and both `eventSummary` sites branch on that
  render's value;
- `buildAssumptions(inputs, params, masked?)` takes the flag exactly like the
  shared helpers do — omitted means "consult the ambient mask", so the
  builder fails closed — while percentages, the row count and the history
  dates pass through untouched (§D2's ruling: a date and a proportion are not
  a balance);
- the chart's **text alternative** lost its `useMemo`: its endpoint facts run
  through `formatCad`, so the memo froze the label at its mount state — a
  session that flipped masked after load kept `$100,000.00 to $120,000.00` in
  its `aria-label` while every other surface read bullets. Money inside a
  memo is what §D6 bans, and this instance sits behind `fromToFact` in
  another module, so the sweep cannot see it either.

Pin: `tests/f11ForecastMask.test.tsx` mounts the real `<Forecast/>` inside the
real `<AppShell/>` and reads the DOM back — text AND `aria-label`s — with an
unmasked control that proves the figures render, and a chip flip that must
change both ways on the same nodes with the fetch log frozen. 2 of 3 red on
the pre-fix tree; re-introducing only the memoized label turns the flip test
red again on the label alone.

**A research card is a money surface (F-12).** `/securities/:id`'s stats strip
hid its figures while the **Fundamentals card directly beneath it** printed
them raw — measured live on a masked demo session:

```
365-day stats:   52W HIGH ••••• | 52W LOW ••••• | 365D CHANGE +23.67% | CACHED CLOSES 52
Fundamentals:    EPS 15.79 | P/E 17.67 | MARKET CAP 386.27B | DIV YIELD % 2.55 |
                 BETA 0.906 | 52W RANGE 201.76 – 306.38
title tooltips:  title="15.79"   title="386.27B"
```

Cause, the same class as F-9/F-10/F-11: `fmtResearch()` — the card's own
"compact display for research numbers" — never asked the mask, and
`FundamentalsCard` carried no `useMasked()` subscription, so the raw strings
were invisible to `scripts/sweep_money.mjs` (which tracks calls to the shared
helpers). Measured as **8 text hits + 6 `title` hits**, the whole-page
raw-money count while masked.

The fix, four halves:

- `fmtResearch(raw, masked?)` masks **first** — fail closed like every other
  formatter: a masked session gets `•••••` for a per-share figure, a market
  cap, the 52-week range, and for the ratio figures derived from them (P/E,
  beta), while an unmasked session keeps the exact abbreviation it had
  (`386270000000.00` → `386.27B`, `15.79000000` → `15.79`);
- `FundamentalsCard` subscribes with `useMasked()`, so a flip repaints the
  card in place (§D3);
- `fmtResearch` joins `sweep_money.mjs`'s `MONEY` set, so the D6 gate now
  structurally covers this card: the next mask-blind formatter added to a page
  fails the sweep instead of shipping;
- the `Pair` `title=` tooltip is the SAME string as its value, so the six raw
  tooltips go with the six raw values — a fix that masked only the visible
  text would have left half the finding standing.

Percentages stay exactly as §D2 rules them: `365D CHANGE +23.67%` and the
day-change percentage are readable both ways, and on a masked `/securities/1`
the ONLY decimal left anywhere on the page is that percentage. The card's
**edit inputs** (opened by an explicit Edit click) keep holding the raw
strings, the same typing-surface ruling §D4 gives the transaction editor: a
field the user is typing into is hostile to mask, and the read view around it
is masked.

Pin: `tests/f12SecurityFundamentalsMask.test.tsx` — the real
`<SecurityDetail/>` over the finding's own figures: masked, no
`\d{1,3}(,\d{3})*\.\d{2}` in any text run **or** any `title`/`aria-label`/`alt`
with percentages excluded, and with every un-excluded decimal provably inside
a `%` string; an UNMASKED CONTROL prints `15.79` / `386.27B` /
`201.76 – 306.38` (tooltips included) and the scanner finds them; a store flip
changes both ways on the SAME `<dd>` nodes with the domain mocks and raw
`fetch` frozen. 2 of 3 red on the pre-fix tree (`source swapped to cd4187d`,
same test).

### D3 — Instant, client-only, and **every money component subscribes itself**

`⌘⇧H` flips the mask in one synchronous store write (`useSyncExternalStore`).
No API round-trip, no route change, no spinner — and, per D6, **no re-fetch**.

The mechanism is worth stating because M139 got it wrong and M140 is the fix.

M139 wrote that `AppShell` could call `useMasked()` **once**, relying on that
re-render walking down through `<Outlet/>` into every mounted page. It does
not, and the reason is React Router, not React: `createBrowserRouter` builds
each route's element **once**, at module load, so `<Outlet/>` hands React the
*same element object* on every render. React's reconciliation bails out of a
subtree whose element is referentially unchanged, so the page under the outlet
never re-rendered. Everything the shell itself renders flipped instantly; the
money that came out of the routed pages — the portfolio header, the Dashboard
KPI strip, the accounts list — kept its old figure until the next query or
cache event happened to re-render it. That is the whole of Mark's M140 report:
*"hitting the mask/unmask does not happen in real time, needs a page refresh."*
A reload "fixed" it only because a reload re-renders everything.

**The rule now (D6 is the enforcement): every component that renders a money
string calls `useMasked()` itself.** The subscription is a
`useSyncExternalStore` read, so it re-renders *that* component on a flip
regardless of what its ancestors did, and the formatters — which read the
ambient mask at call time — produce the right string on the pass that follows.
No prop threading, no context, no second store.

Two consequences are load-bearing:

- **Recharts memoizes its axes, and re-rendering the chart is NOT by itself
  enough.** `XAxis`/`YAxis` are `React.memo`'d on `axisPropsAreEqual`, so the
  tick layer repaints only when a prop the memo actually COMPARES changes —
  and for a long time `chrome.moneyAxis` WAS the stable module function
  `formatAxisMoney`, which never does. What redrew the Dashboard's axis in
  M140 was therefore never the memo "missing": the chart's `data` array is
  rebuilt on every render, which churns Recharts' store and fires
  `YAxisImpl`'s own selector subscription. A chart that MEMOIZES its points
  gets neither — the investments `AccountValueChartBody` (`points` is a
  `useMemo`) kept painting the mount state through **both** directions of a
  flip, while every other money string on the page changed (QA blocker on the
  Ledger candidate `t_b29beeaa`). **The contract is reactive, not incidental:
  the axis must receive a prop that changes with the mask.**
  Since the F-7 follow-up that contract is **centralised in
  `useChartChrome()`** (charts/chartDefaults.ts): the hook calls
  `useMasked()` itself — so every component that builds its chrome re-renders
  on a flip even if nothing else in it subscribed (an independent second path
  to the same repaint; `Forecast` now subscribes in its own body too, since
  F-11) — and hands `moneyAxis` back as a FRESH closure per render,
  `(v) => (masked ? MASK_TEXT : formatAxisMoney(v))`, whose branch comes from
  `useMasked()`'s return value for THAT render. A flip therefore hands
  Recharts a new prop identity, the memo misses, and the tick layer re-runs
  the mask-aware formatter — no refetch, no remount, no formatter deciding
  what was already drawn from global state. Passing an inline
  `tickFormatter={(value) => (masked ? MASK_TEXT : formatAxisMoney(value))}`
  straight to an axis (the shape `AccountValueChart` still uses) is exactly
  equivalent; the hook is simply where the other TEN call sites get it.
  (Measured both ways:
  `tests/f7MoneyAxisMaskRepaint.test.tsx` pins all TEN non-investment call
  sites through masked → unmasked → masked on ONE mount, reading the tick
  strings back out of the SVG — same axis node, frozen fetch log — and
  records that pre-fix Forecast, DividendsPanel and UnrealizedBarChart were
  genuinely frozen while the other six of the then-nine repainted only by the `data`-churn
  accident; `tests/ledger-ui-build/b1-axis-mask-repaint.test.tsx` does the
  same for the investments body, and `tests/m140ChartAxis.test.tsx` holds
  the Dashboard chart unchanged. Note the D6 sweep bans a `useMemo`/
  `useCallback` wrapper here: the closure has to be rebuilt each pass.)
- **A `useMemo` around money formatting would defeat the whole design**, because
  a re-render does not re-run it. The codebase still carries none, and the sweep
  now *fails* on one rather than trusting a comment (D6).

Formatters are still **fail-closed**: an omitted flag means "consult the ambient
mask", so a new call site masks by default and cannot silently regress. What
changed in M140 is only *who re-renders* — the helpers stay the single source of
truth, they just need a subscriber in the component that calls them.

### D4 — Working surfaces do not mask

Masking a field the user is actively typing into is hostile, not safe. Exempt
and pinned `masked: false` at the call site:

- the **import wizard** (`Imports`) and **Reconciliation** — the user is
  matching statement lines to ledger rows;
- **Settings** — configuration, not disclosure (the LLM test panel's echo is
  the exception: it *prints* a figure back, so it **is** masked);
- **receipts** — the evidence the user is filing;
- **export confirm previews** — the one place the user must see the real
  figure before committing.

**Notification inbox bodies** run through `maskAmountsInText` instead: the
amount is embedded in prose ("Spent 123.45 of 500.00 CAD…"), so the numbers
mask while the sentence, the account name and the date stay readable.

**The same rule covers every surface that renders that prose.** The Dashboard's
critical-budget banner (M51) prints the same server-composed notification line
inside its `role="alert"`, so it runs the item's **title and body** through the
helper too (`Dashboard.tsx`). It shipped rendering `{item.title} — {item.body}`
raw (F-1, security review of `42c9ac7`): a masked session printed
"Spent 312.84 of 150.00 CAD (209% of limit; threshold 100%)" in the same
viewport whose live region said "Amounts masked. Dollar figures are hidden…",
beside KPIs rendering •••••. The title goes through the helper as well — it
leaves non-amount prose untouched ("Budget alert: Groceries" prints
unchanged), and the line is one run of text, so an amount-shaped token inside
a category name cannot bypass the mask either. The acceptance is behavioural,
not static: `scripts/sweep_money.mjs` proves component *subscription* and can
never see server-composed prose, so `tests/dashboardBudgetBannerMask.test.tsx`
mounts the real `<Dashboard/>` inside the real `<AppShell/>` and reads the DOM
back — no `\d{1,3}(,\d{3})*\.\d{2}` anywhere on a masked `/dashboard`, with an
unmasked control proving the banner rendered.

### D5 — Never on the wire

The mask is a rendering concern. `mask.ts` does not import the API client, no
request carries it, and `users` has no column for it. `docs/11`'s privacy mode
is untouched — M139 renders what privacy mode has already decided to send.

### D6 — No re-render exceptions (M140)

M139 shipped a mask that worked *most* of the time, which is the worst kind of
working: the chip flipped, the shell's own money flipped, and the routed pages
kept the old figure until a reload. M140 closes that with one rule and a gate
that enforces it.

**The rule.** Every component that renders a money string through a mask-aware
helper calls `useMasked()` once, as its first statement. No re-implementation
of the mask: the helpers stay the one source of truth, they just need a
subscriber in the component that calls them.

**The gate** — `scripts/sweep_money.mjs --check`, run as
`tests/m140MaskSweep.test.ts` so the normal `vitest run` fails on a regression.
It walks the AST of `frontend/src` and fails on four things:

| Failure | Why it matters |
| --- | --- |
| a money-rendering component with no `useMasked()` | the M140 bug, verbatim — the flip silently does nothing there |
| money built inside `useMemo` / `useCallback` / `useEffect` | a re-render does not re-run those, so the string survives the flip |
| a currency formatter that bypasses the helpers (`Intl…style:"currency"`, `` `$${n}` ``) without consulting `isMasked()` itself | the `fmtDelta` class — the helper cannot mask what the helper never sees |
| money formatting at module scope | no component owns it, so nothing can ever re-render it |

`scripts/hook_call_guard.mjs --check` covers the inverse hazard: a subscribed
component is a hook consumer, so a plain `Name(...)` call of one anywhere is an
"Invalid hook call" crash TypeScript will not catch. Both are in the same test
file, and both carry a vacuous-pass guard (a floor on the audited count) so a
broken walker fails instead of silently reporting nothing.

**What the sweep found beyond the reported bug.** The audit of every
`fmtCad` / `fmtCadCompact` / `formatWithCad` / `formatMoney` / `fmtNative` /
`fmtCadSigned` call in `pages/` and `components/` turned up **63 money-rendering
components, 61 of them without a subscription** — not just the three surfaces in
Mark's report. Two findings were of a different kind than "does not re-render":

- `pages/Dashboard.tsx`'s `fmtDelta` (every KPI delta chip) formats currency
  itself and never asked the mask, so a masked dashboard printed `$200` next to
  a masked net worth. It consults `isMasked()` now, and the sweep's third check
  makes that class fail the gate rather than relying on a reviewer to notice.
- **The SSE live-tick path** (M126/M130) re-renders on every quote, which is
  exactly why it needed the subscription: a tick on a masked session must
  re-render the recomputed headline as bullets. `tests/m140MaskFlip.test.tsx`
  drives a real `EventSource` tick against a masked header and asserts the
  recomputed figure never appears as a number, then un-masks and asserts the
  LIVE value (`$15,000`, not the pre-tick `$10,000`) shows — the tick and the
  flip are independent axes.

**Zero re-fetches.** The mask lives below the data layer (D5), so a flip fires
no request. That is asserted directly: the flip tests take a fingerprint of
every mocked API's call count before the flip and require it unchanged after —
masking and unmasking, on `/investments`, `/accounts` and the Dashboard KPI
strip. No `invalidateQueries`, no refetch, no session re-probe.

## Touch points

| Path | Change |
| --- | --- |
| `frontend/src/lib/mask.ts` | **new** — store, `useMasked`, `maskAmountsInText`, `maskForYear` |
| `frontend/src/lib/format.ts` | `formatCad` / `fmtCad` / `formatCadCompact` take optional `masked` |
| `frontend/src/components/reports/TaxAlertsPanel.tsx` | M139/M140 follow-up: the page-local `$`-shim is gone — money renders through `lib/format::formatCad`. Rendering of valid Decimal-strings is unchanged; an unparseable/blank/null value now renders the shared honest `—` instead of the shim's fabricated `$0` (`tests/taxAlertsPanel.test.tsx`) |
| `frontend/src/lib/impact.ts` | `fmtCadSigned` / `fmtSigned` mask by default; `fmtSignedPct` never masks; `fmtSignedPctBookCost` follows the mask |
| `frontend/src/lib/transactions/format.ts` | `formatWithCad` masks both halves as one unit |
| `frontend/src/components/charts/chartDefaults.ts` | `formatAxisMoney` masks ticks; `useChartChrome()` subscribes and returns `moneyAxis` as a per-render closure (F-7, §D3) |
| `frontend/src/components/charts/Donut.tsx` | **F-9:** new `totalFact?: string \| null` — the caller owns the text alternative's money fact when its centre is not the money total; omitted, the mask-aware `formatCadCompact(total)` default is unchanged |
| `frontend/src/components/charts/ExposureDonut.tsx` | **F-9:** both mask-blind formatters gone — tooltip and label fact route through `fmtNative`, and the component subscribes (the tooltip formatter is a closure it builds) |
| `frontend/src/pages/Investments.tsx` | **F-10:** `TradesLog`'s price/amount and commission cells are no longer raw `{trade.price_native}` / `{trade.commission}` interpolation — they render through `fmtNative` (row currency → ACCOUNT currency, M132) and the component subscribes with `useMasked()` |
| `frontend/src/lib/forecastCopy.ts` | **F-11:** `buildAssumptions(inputs, params, masked?)` — the five absolute figures (and their recurring/residual parts) render •••••; percentages, row count and history dates are untouched |
| `frontend/src/pages/Forecast.tsx` | **F-11:** subscribes with `useMasked()`; the end-value table's four cells, the `buildAssumptions` call and both `eventSummary` sites branch on that render's mask; the chart's text alternative is rebuilt per render (no `useMemo`, §D6) |
| `frontend/src/pages/SecurityDetail.tsx` | **F-12:** `fmtResearch(raw, masked?)` masks first (fail closed) — the EPS / P-E / market-cap / yield / beta / 52w-range figures and their `Pair` tooltips render ••••• while masked; `FundamentalsCard` subscribes with `useMasked()` |
| `scripts/sweep_money.mjs` | **F-12:** `fmtResearch` joins the `MONEY` set, so the D6 gate owns the Fundamentals card instead of never seeing it |
| `frontend/src/components/ui/AnimatedNumber.tsx` | `mask` prop; **both** the visual span and the `sr-only` twin mask |
| `frontend/src/components/privacy/MaskChip.tsx` | **new** — chip, `MaskToggleRow`, `MaskLiveRegion`, `focusMaskChip` |
| `frontend/src/components/layout/AppShell.tsx` | `useMasked()`, `⌘⇧H` in the single keydown listener, chip in the rail |
| `frontend/src/auth/AuthContext.tsx` | boot clear + demo default on identity change |
| `frontend/src/**` — **73 money-rendering components** (count is `scripts/sweep_money.mjs`'s output, not a hand-maintained figure) | M140: each calls `useMasked()` as its first statement so a flip re-renders it (D6). Swept mechanically, not by hand — see `scripts/sweep_money.mjs` |
| `frontend/src/pages/Dashboard.tsx` | M140: `fmtDelta` (the KPI delta chips' own currency builder) consults `isMasked()`; it was masking nothing. F-1: the critical-budget banner's `{item.title}` / `{item.body}` run through `maskAmountsInText` (D4) |
| `scripts/sweep_money.mjs` | **new** — the D6 gate: AST sweep of every money-formatter call; `--check` exits non-zero on a missing subscription, a memoized money string, a raw currency builder, or module-scope money |
| `scripts/hook_call_guard.mjs` | **new** — a `useMasked()` component must only ever be rendered as JSX; a plain call is an uncatchable "Invalid hook call" |

## a11y

- The chip is a **button**, not a badge: `aria-pressed`, and the indicator and
  the switch are one control. State changes ride three channels — icon
  (`Eye`/`EyeOff`), text (`Masked`/`Unmasked`) and colour — so it never depends
  on colour alone.
- One polite live region (`mask-live-region`) announces every flip.
- `⌘⇧H` is handled inside the shell's **existing** keydown listener, not a
  second one — the duplicate-listener bug class M72 already caught.
- Focus returns to the chip after a keyboard toggle.

## Verification

```bash
cd frontend && npx tsc --noEmit
cd frontend && npx vitest run tests/m139Mask.test.ts tests/m139MaskChip.test.tsx \
  tests/m140MaskFlip.test.tsx tests/m140ChartAxis.test.tsx tests/m140MaskSweep.test.ts \
  tests/m139RestoreMask.test.tsx tests/m183FailedBootMask.test.tsx tests/m167WorldMap.test.tsx \
  tests/dashboardBudgetBannerMask.test.tsx tests/f9ExposureDonutMask.test.tsx \
  tests/f10TradesLogMask.test.tsx tests/f11ForecastMask.test.tsx \
  tests/f12SecurityFundamentalsMask.test.tsx
node scripts/sweep_money.mjs --check      # D6 gate
node scripts/hook_call_guard.mjs --check  # D6 gate, inverse hazard
```

- `tests/m139Mask.test.ts` — the session contract (key, no restore, demo
  default, logout kill) and the text masker, pinned against the three
  notification-body shapes `backend/app/api/notifications.py` actually emits;
  plus the percentage ruling and the book-cost carve-out.
- `tests/m139MaskChip.test.tsx` — the real `AppShell`: chip, account-menu row,
  live region, `⌘⇧H` (and that plain `⌘H` / bare `h` / `⌘K` do **not** fire).
- `tests/m140MaskFlip.test.tsx` — the M140 bug, inverted: the real shell over
  real routes (`/investments`, `/accounts`, Dashboard), one chip flip, and
  every money callout becomes bullets **on the same DOM nodes** (a remount
  would drop state and refetch — the test pins node identity), with an
  unchanged API-call fingerprint across mask *and* unmask. Plus the SSE-tick
  masked path.
- `tests/m140ChartAxis.test.tsx` — a masked chart redraws its money ticks as
  bullets without remounting, and keeps its date labels (D2: the scale hides,
  the trend does not).
- `tests/dashboardBudgetBannerMask.test.tsx` — F-1 (D4): the critical-budget
  banner's server-composed prose masks. Real `<Dashboard/>` in the real
  `<AppShell/>`, the real `criticalBudgetItems` selection over the server's own
  notification shapes: on a masked session no `\d{1,3}(,\d{3})*\.\d{2}` text
  exists anywhere on `/dashboard` (the whole viewport, not one node) and the
  live region's "Amounts masked…" claim is on screen in the same frame; the
  unmasked control asserts the prose prints verbatim and that the scanner
  finds it; a chip flip re-masks the same DOM node with an unchanged API-call
  fingerprint. The percentage carve-out is symmetric (`12.34%` readable both
  ways — D2).
- `tests/ledger-ui-build/b1-axis-mask-repaint.test.tsx` — the OTHER shape of
  the same rule: the investments `AccountValueChartBody`, whose `points` are
  memoized (so nothing churns Recharts' store), flips masked → unmasked →
  masked on ONE mount, reads the tick strings back out of the SVG, asserts the
  axis node is the same element throughout and that `fetch` was never called.
  Red before the reactive-formatter fix, green after.
- `tests/f9ExposureDonutMask.test.tsx` — F-9 (D2): the currency-exposure
  donut's text alternative and its hover tooltip are money surfaces. The real
  `<ExposureDonut/>` over the live-measured CAD/USD pair from the finding:
  masked, no `\d{1,3}(,\d{3})*\.\d{2}` in the label or the hovered tooltip,
  with `CAD 77%` surviving (D2's percentage ruling); an UNMASKED CONTROL
  asserts the same surfaces carry `163,260.73 CAD` / `47,743.27 USD`, so a
  masked pass cannot be vacuous; both directions on ONE mount with the wrapper
  node pinned by identity — the tooltip half only flips because `ExposureDonut`
  re-renders its formatter closure. 4 of 5 red on the pre-fix tree, reproducing
  the finding's exact strings (`total 211004.` in the label).
- `tests/f10TradesLogMask.test.tsx` — F-10 (D2): the Trades log's
  price/amount and commission cells are money surfaces even though they were
  raw JSX interpolation, the class `scripts/sweep_money.mjs` structurally
  cannot see (F-9/F-1's shape). The real `<Investments/>` over three rows that
  cover every branch: CAD `58.34` + `9.99` commission, USD `136.29` with the
  LIVE `0.00` zero, a NULL-currency `21000.00` cash row that must take its
  ACCOUNT's code (and carries the literal `"0"` the em-dash guard branches
  on): masked, no `\d{1,3}(,\d{3})*\.\d{2}` in the table region and all six
  money cells read •••••; an UNMASKED CONTROL prints `58.34 CAD` /
  `136.29 USD` / `21,000.00 CAD` / `9.99 CAD` / `0.00 USD` plus the `—`, and
  the scanner finds those exact figures, so a masked pass cannot be vacuous;
  both directions on ONE mount with the `<tr>` nodes pinned by identity, an
  unchanged API-call fingerprint and zero `fetch` calls. 3 of 3 red on the
  pre-fix tree (source swapped to `b15b20f`, same test).
- `tests/f11ForecastMask.test.tsx` — F-11 (D2): the three `/forecast`
  surfaces the security sweep could not see, because none of them calls a
  shared formatter — the end-value table, the assumptions figures and the
  scenario event rows. Real `<Forecast/>` in the real `<AppShell/>`, the
  server's own run payload: masked, no `\d{1,3}(,\d{3})*\.\d{2}` anywhere
  on the page in text **or** `aria-label`s (the chart's text alternative
  included), with `Inflation: 2.00%`, the row count and the event date
  surviving; an UNMASKED CONTROL asserts the same surfaces print the
  response's own strings (`120000.00`, `12345.67`, `5000.00 = recurring …`)
  and that the scanner finds them; a chip flip changes both ways on the SAME
  nodes with `h.calls.length` frozen. 2 of 3 red pre-fix — and restoring only
  the memoized chart label turns the flip test red on the label alone, which
  is the §D6 half the sweep structurally cannot reach.
- `tests/f12SecurityFundamentalsMask.test.tsx` — F-12 (D2): the Fundamentals
  card on `/securities/:id`, the one surface whose raw figures lived inside a
  page-local helper the sweep had never heard of. The real `<SecurityDetail/>`
  over the finding's own payload (`15.79000000`, `386270000000.00` → `386.27B`,
  `201.76`–`306.38`): masked, all six figures AND their `title=` tooltips read
  •••••, no `\d{1,3}(,\d{3})*\.\d{2}` anywhere in the text runs or in
  `title`/`aria-label`/`alt` once §D2's percentages are excluded — and every
  decimal that survives un-excluded is provably inside a `%` run; an UNMASKED
  CONTROL prints the compact response figures (tooltips included) and the
  scanner finds ≥8 of them, so a masked pass cannot be vacuous; a flip changes
  both ways on the SAME `<dd>` nodes with the domain mocks and raw `fetch`
  frozen. 2 of 3 red on the pre-fix tree.
- `tests/m140MaskSweep.test.ts` — runs both D6 gates, plus a canary that writes
  an unsubscribed money component and requires the sweep to fail on it, and a
  vacuous-pass floor on the audited count.
- `tests/m139RestoreMask.test.tsx` — **the RESTORE seam**, through the real
  `AuthProvider` + real `AppShell` (so the chip is the shipped element, not a
  mock): stale key cleared on a non-demo restore, storefront default on a demo
  restore, the late-restore guard, chip ↔ key agreement both ways, plus the
  MONEY-surface half — a seeded account balance must render as bullets on the
  shell rail for a restored demo session with no `$` figure anywhere on screen,
  an ordinary-tenant control proving that surface is real, and a per-render log
  proving no render ever painted the restored demo identity unmasked.
- `tests/AuthContext.test.tsx` — the restore seam at the provider: the mask boot
  contract on both branches (successful AND failed restore), the non-demo
  invariant, logout clearing a live mask, and the cache half — a pre-seeded
  production-shaped entry survives the restore and `removeQueries` is never
  called on first load (running the mask half must not smuggle in the eviction
  half). The failed-boot case pins the shape the client actually emits
  (`restoreSession` resolving `null`), not a rejection it cannot produce.
- `tests/m183FailedBootMask.test.tsx` — the failed boot end to end through the
  REAL client: `fetch` stubbed at the boundary answering `401` (and throwing),
  the real `restoreSession` and the real `AuthProvider` on top, seeded stale key
  → cleared and unmasked, with the late-choice guard pinned on that branch too.
  It mocks no seam, which is the point: the seam-level pin went green while the
  production path stayed red (t_5900dd66).
- `tests/reportMoneyAxes.test.tsx` — a masked axis cannot regress the compact
  tick format, and a tick **index** passed as the second argument can never be
  mistaken for a mask flag.
- `scripts/a11y/m140_mask_flip_probe.py` — the deployed stack: flip the chip on
  `/investments`, `/dashboard` and `/accounts` and assert the numbers change
  **without a reload and without a single network request**.

### The Recharts trap, for whoever reads the axis formatter next

Recharts calls `tickFormatter(value, index)`. An early cut of this feature gave
`formatAxisMoney` an optional `masked?: boolean` second parameter, which
silently received the tick's **index** — truthy for every tick but the first —
so an unmasked chart drew `$0`, then `•••••`, `•••••`, `•••••`. Axis formatting
therefore takes exactly one argument. Never thread a positional mask flag
through a Recharts callback.
