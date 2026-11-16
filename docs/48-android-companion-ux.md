# 48 — Android companion UX scope (concept)

**Status: UX proposal; no platform, implementation, or release decision implied.** This document defines a mobile companion experience for fast, low-risk financial check-ins. It is an experience boundary, not a second full finance workstation. It builds on the shipped responsive/PWA surface described in `docs/09` and the Ledger navigation in `docs/47`; it does not claim that Android-specific capabilities currently exist.

## TL;DR

| V1 companion | Later, only with a demonstrated need | Web-first / web-only |
|---|---|---|
| Authenticated read-focused home; account/portfolio glance; display masking; account/holding/transaction read review; clear stale/error/empty states; manual refresh; alerts inbox and notification deep links where existing alert data supports them | Opt-in local biometric re-lock; tightly bounded offline read-only snapshot; alert actions beyond opening the app; share/import capture after a safe staged-review design | Import batch resolution/commit, ledger editing/deletion, trading, tax/ACB work, bulk operations, complex reports/forecast scenario editing, settings/account administration, reconciliation, security/credential changes |

The native shell is valuable when it removes friction around **returning securely**, **reaching a relevant read-only screen from a notification**, or **checking a previously viewed snapshot without connectivity**. Native wrappers alone are not a user benefit. Never let a cached/offline value look current or authoritative.

## Product promise and invariants

The job is: “I have a moment; tell me what changed, let me verify the relevant account/holding/transaction, and get me back out safely.” Optimize for a brief glance and one-step drill-in, not dense dashboard parity.

1. **Server is authoritative.** Display balances, portfolio value, positions, prices and activity from authenticated server responses. No client-side balance reconstruction or optimistic financial writes. Include “As of” time and, for prices, source/staleness; cached observations are not live quotes.
2. **Tenant isolation.** A signed-in identity owns every request and cache. On logout, expiry, or identity change, clear visible tenant data and invalidate private caches before another identity is shown. Never show one tenant’s alert/notification content on another tenant’s lock screen.
3. **Demo remains read-only.** The demo can browse. No mobile affordance should imply that a demo mutation can persist; wherever a write is shown for explanatory context, state read-only before action and preserve the server's `demo_account_restricted` truth. V1 companion avoids mutation workflows entirely.
4. **Price precedence.** When showing a security, a manual price override remains the displayed source over yfinance backfill (docs/07); label source and staleness rather than replacing, blending, or silently refreshing it.
5. **Privacy.** The existing session display mask is a presentation choice, not encryption or access control. Mask values by default on lock-screen surfaces; keep mask state obvious and accessible. Do not expose financial values in notifications, app-switcher previews, widgets, or offline cache previews. Respect privacy mode and E2EE degradation as currently specified; do not imply that masking hides server access or that E2EE is universal.
6. **No invented certainty.** Distinguish zero from unavailable, stale, hidden, locked, or not yet loaded. Use server timestamps and exact currency labels; never sum unlike currencies into a fabricated total.

## Existing mobile baseline and opportunity

The current product already has a responsive shell: five-tab bottom navigation below 768 px (**Home, Activity, Add, Portfolio, More**), drawer for remaining destinations, and a fixed shell-level mask control (docs/09, docs/47). The app is responsive, not a separate native interaction model. It already offers a dashboard, transaction list, portfolio and account detail, plus manual-price freshness/source cues.

The companion should therefore **not** duplicate a full mobile site. Its distinct value is a secure re-entry pattern, a glance-first information hierarchy, notification-to-relevant-record handoff, and potentially an explicitly bounded offline snapshot. Keep existing responsive web as the route for complex work and as fallback if a native capability is unavailable.

## Prioritized journeys

### P0 — Re-enter and check current state (daily)

1. User opens the companion. If session valid and app was recently foregrounded, show the glance screen; after background/lock timeout, require device unlock or app re-auth according to the approved security policy.
2. If signed out/expired, show a calm sign-in screen; explain session expiry without discarding the intended destination. Demo entry remains clearly marked as demo/read-only.
3. On home, show portfolio value (if available), account-group/account rows, one high-signal recent activity or alert, and a freshness line. A pull-to-refresh is explicit; it never presents a success state until the server answers.
4. Tap an account or holding for read-only details; back returns to the same scroll/selection context.

**Why first:** it serves the companion’s recurring job with the least risk and keeps the financial “source of truth” in the existing service.

### P0 — Mask, then selectively reveal for this session

1. Home launches masked when privacy preferences or a fresh app session call for it; mask control is in the persistent shell, never hidden in Settings.
2. User taps “Show values”; visible figures and chart value labels update together. State is communicated with label and icon/pressed state, not color alone.
3. Backgrounding, lock, logout, or session expiry re-masks. Re-entry does not silently preserve a visible value state across a different identity.
4. Notification/recents previews remain masked regardless of in-app reveal.

**Why first:** shoulder-surfing is a real mobile context; the existing mask is already a deliberate app contract. Make its scope honest: it masks display, not the network or stored data.

### P1 — Review one transaction or holding safely

1. Open Activity or tap an alert/deep link; show identity/context first: account name, date, payee/security, currency, and status/source.
2. Open a compact detail sheet/page with full relevant read fields. Show “Pending review” for staged imports; staged rows are visible to review but are not committed financial activity (docs/03).
3. Provide an explicit “Open full review on web” for anything requiring edit, categorization, import commit/discard, or a large table. Do not present a mobile action that cannot finish safely.
4. If the linked record was removed or is outside the user’s tenant, show not found/access unavailable, not another record or stale cached content.

**Why P1:** the key trust action is confirming what an amount belongs to. Editing and batch review have high consequence and dense context, so remain web-first.

### P1 — Act on an actionable alert

1. Notification copy is generic and privacy-safe (“Fintrack has an alert to review”), with no amount, merchant, security symbol, or account name on lock screen by default.
2. Tap opens the authenticated in-app Alerts/Activity destination; after sign-in, continue to the intended item only if authorization and current existence are confirmed.
3. In app, show trigger, relevant amount/currency, account scope, and “as of” time; dismiss/read action only if supported by the existing server contract. Otherwise it is an informational deep link, not a faux action.
4. Permission denied, notifications disabled, stale link, expired session, no connection, or revoked tenant access each have a clear in-app fallback. Never mark an alert resolved merely because the notification was opened.

**Why P1:** alerts are useful only when they lead to an explainable review, not when they leak private details or imply unsupported controls.

### P2 — Offline glance (conditional)

When the user has deliberately opted in and data has previously been fetched, allow a **read-only, time-bounded snapshot** of a small set of account/portfolio summaries. Every offline screen carries a persistent “Offline · last updated [time]” banner; stale data must never be styled or worded as current. No mutations, quotes, notification actions, or derived calculations offline. Expire/clear it on identity change, logout, or policy timeout. If reliable invalidation and privacy-safe storage cannot be guaranteed, omit offline finance data and show a helpful offline state instead.

**Why later:** it has value during travel/connectivity gaps, but stale balances create material trust risk. It is not a v1 prerequisite.

## Information architecture

Persistent shell (signed-in):

- **Home** — portfolio/account snapshot, freshness, a few actionable alerts/recent items.
- **Activity** — recent transactions and status filters; detail is read-only in v1.
- **Portfolio** — portfolio summary, positions list, holding detail and price provenance.
- **Alerts** — actionable/informational alerts with status only where server-backed.
- **More** — full account list/detail, help, privacy/session controls, “Open full web app”.

Auth routes: Welcome/sign in → optional demo explore (read-only) → Home; expired session → sign in → intended destination. Do not expose Add as a primary companion destination in v1 merely to mirror responsive navigation: creation/editing belongs to full web flows. Deep links must resolve through auth and tenant checks before record display.

## Wireframes (structural, not visual design)

### 1. Secure entry

```text
┌──────────────────────────┐
│ Fintrack                 │
│ Your finances, securely  │
│                          │
│ [Email                 ] │
│ [Password              ] │
│ [ Sign in                ]│
│                          │
│ Explore demo (read-only) │
│                          │
│ Session expired? Sign in │
└──────────────────────────┘
```

- Loading: submit has progress and cannot double-submit. Network failure preserves email, never password; explain retry.
- **Password → TOTP transition:** the password request is busy only while `AuthState` remains the same state it started from. A successful password response that changes state to `TotpRequired` ends that request, immediately enables the authenticator-code field and submit button, and labels the button “Submit code” (never “Checking…”). The challenge is not an authenticated session; do not store/render a token or cookie until the second factor succeeds. TOTP submit is disabled only for its own in-flight request; an error response preserves the challenge and code field, gives generic retry copy, and re-enables the field/button. State transitions away from a challenge (expiry/cancellation/logout) release busy state AND clear the step-local error copy — the transition is the honest report, so a rejected-code message never rides along under a committed session — while failure copy that arrives with no state change (wrong code, offline, timeout) is shown and kept. Late failure is shown honestly and cannot wedge the form. Network calls have finite HTTP timeouts. Test with a local synthetic challenge: password → editable code → invalid code → retry → successful session and loaded read-only screen; also delayed/offline failure and challenge cancellation. Never log or persist submitted codes/challenge tokens.
- Invalid credentials: generic sign-in failure; do not reveal account existence.
- Expired/invalid refresh: return here with destination preserved only as an internal same-app route.
- Demo: explicit “Demo · read-only” label persists in shell and any write boundary; no implication that demo state resets.
- Biometric later: only a local re-lock to an already authenticated session after server/session policy allows it; never replace server authentication or recover expired credentials. Cancellation falls back to app sign-in.

### 2. Home glance + mask

```text
┌──────────────────────────┐
│ Home            [Masked] │
│ Updated 10:42 · Refresh  │
│                          │
│ Portfolio                │
│ ••••••• CAD   View →     │
│ Prices: mixed freshness  │
│                          │
│ Accounts                 │
│ Chequing       ••••• CAD │
│ TFSA           ••••• CAD │
│ USD account   ••••• USD  │
│                          │
│ Needs review             │
│ 1 new item        View → │
│                          │
│ Home Activity Portfolio  │
│       Alerts  More       │
└──────────────────────────┘
```

- Unmasked state uses explicit “Values shown” label and button state. Hide action immediately masks all value-bearing surfaces, including charts and accessible names.
- No accounts: helpful “Add/manage accounts on web” link; do not fabricate a zero portfolio.
- Partial/stale data: keep available data, identify unavailable section and timestamp; never collapse errors into $0.
- Refresh in progress keeps last-known figures visibly timestamped; refresh failure says “Couldn’t update. Showing data from [time]” with Retry.
- Mixed currencies: list own-currency values; any total must be a server-provided, explicitly denominated portfolio total, otherwise omit it.

### 3. Holding review

```text
┌──────────────────────────┐
│ ‹ Portfolio              │
│ ACME · Example Corp      │
│ CAD · held in TFSA       │
│                          │
│ Market value  ••••• CAD  │
│ Price $123.45 · CAD      │
│ Manual price · as of date│
│                          │
│ Position details         │
│ Quantity       12        │
│ Book cost      ••••• CAD │
│ Price status   Current*  │
│                          │
│ Recent activity          │
│ Buy · date · account     │
│                          │
│ Open full details on web │
└──────────────────────────┘
```

- “Current*” must be replaced by actual source and age; sample is illustrative. Manual override is clearly identified and never silently replaced by a backfill. Show native and CAD values only when the API provides them; label both currencies.
- If price absent/stale: show “No recent price” / “Last price [date]” and omit invented market value or clearly label server-calculated stale valuation. Explain privacy-mode/E2EE withholding in place; never claim no holdings solely because withheld rows are not returned.
- If security is watchlist-only: say “Not held” and do not show an account position.

### 4. Transaction review

```text
┌──────────────────────────┐
│ ‹ Activity               │
│ Coffee shop              │
│ −$8.45 CAD               │
│                          │
│ Date          Oct 25     │
│ Account       Chequing   │
│ Category      Dining     │
│ Status        Posted     │
│                          │
│ Imported row? Pending    │
│ Not committed to ledger  │
│                          │
│ [Open full review on web]│
└──────────────────────────┘
```

- Pending staged item: persistent pending label and no language implying committed balance impact. Mobile can inspect; batch resolution/commit is web-only.
- Missing category/locked fields: show available fields and explain what is unavailable; no silent empty-value inference.
- Record unavailable/forbidden: show “This item is no longer available” and safe back navigation; do not reveal whether a foreign-tenant ID exists.

### 5. Alert handoff

```text
┌──────────────────────────┐
│ Alerts                   │
│                          │
│ Review a low-balance alert│
│ Chequing · updated 10:42 │
│ [Review account]         │
│                          │
│ Price alert              │
│ Manual price · 10:30     │
│ [Review holding]         │
│                          │
│ Notification previews    │
│ show no financial values │
└──────────────────────────┘
```

The example alert types are illustrative, not a commitment that every type is currently generated or actionable. Show only alert types backed by an existing server source. If none, honest empty state: “No alerts to review.” Open notification after session expiry routes through sign-in and then revalidates the target.

## Native capability value and boundary

| Capability | Decision | User value / safety condition |
|---|---|---|
| System back/navigation | V1 behavior | Expected Android muscle memory; preserve list position and handle nested detail/auth/deep-link back safely. Back from an external notification destination should not accidentally exit to an unrelated tenant screen. |
| Biometric app lock | Later, opt-in | Convenient privacy re-lock in public; only a local gate over an existing valid session. Must have timeout/background policy, fallback, clear disable path, and server auth remains authoritative. Not a substitute for login or account recovery. |
| Offline-safe read cache | Later, conditional | Helps a traveler check last-known balances in a dead zone. Only if explicitly opted in, bounded, identity-cleared and clearly timestamped; read-only and no financial actions. Otherwise no cached finance display. |
| Push notifications | V1 only as privacy-safe handoff, if existing alert events justify it | Real value is timely return to a relevant in-app review. Generic lock-screen copy, user controls, deep-link re-auth/revalidation, no financial payload in OS surface. No duplicate alert system or unsupported “resolve” action. |
| Share/import capture | Later; web remains batch review | Android share sheet/file picker can reduce capture friction for a statement or receipt, but uploaded content is sensitive and parser uncertainty demands staged preview, account confirmation and commit review. Never auto-commit; only pursue after full on-device-to-server privacy disclosure and web-equivalent review path. |
| App widgets / lock screen | Not v1 | High shoulder-surfing and stale-data risk; little added value over generic notification without an explicit privacy model. |
| Native charts/biometric-specific data access | Not v1 | No clear job advantage over existing API-backed views; avoid privileged OS data access. |

## Mobile-vs-web feature boundary

**V1 mobile:** sign-in/session re-entry; masked glance; account and portfolio/holding read views; recent transaction read views; alert-to-screen navigation; help and safe handoff to web. Manual refresh; clear server timestamp, source, currency, pending status and error state.

**Later mobile (evidence-gated):** local biometric re-lock; offline read-only snapshot; richer alert preferences/actions only if backed by current server contracts; share/import capture only with staged review and explicit privacy consent.

**Web-first / web-only:** create/edit/delete ledger rows, categorize, transaction bulk actions, imports and batch commit/discard/revert, trades and investment writes, manual price edits, account creation/admin, reconciliation, taxes/ACB and exports, rules, budgets/goals/debts configuration, forecast scenario editing, reports requiring dense comparison, security/session administration and data export/backup restore. Mobile may deep-link to these destinations but should not make a partial or unsafe substitute.

## Acceptance / design validation questions

- In every identity transition (sign-in, silent restore, logout/expiry), no prior tenant’s financial record is visible after the transition; private cache is cleared before new content renders.
- A mask toggle changes every visible amount and chart value without changing underlying data; lock-screen and app-switcher previews remain generic.
- Every displayed total has one currency and server provenance; manual prices remain visibly manual and take precedence over fetched prices.
- Pending import records are labelled pending and never described as committed activity; demo writes remain unavailable/read-only.
- Refresh, offline, stale price, no data, permission denied, hidden/locked data, expired session and revoked deep link all have distinct, actionable states.
- Test at narrow phone widths with large text, screen reader labels for mask state, logical Android back behavior, and touch targets meeting the existing 44px floor (docs/09).

## Rationale and open product decisions

This is a UX scope recommendation, not a backend capability inventory or a promise that an Android application will ship. Existing responsive pages cover many tasks; native-only work is justified only where it materially improves secure re-entry, notification handoff, or a carefully bounded offline glance. Product must decide whether native notifications and offline snapshots are worth their privacy, permission and stale-data costs before those enter scope. No implementation or platform choice is made here.
