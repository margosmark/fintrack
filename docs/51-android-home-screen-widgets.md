# 51 — Android home-screen widgets: product and technical brief

**Status: approved product boundary, not an implementation plan.** Widget concepts, refresh limitations, and security boundaries in this document are binding requirements for the work Mark authorized on 2026-10-29 (see Decision). Existing boundaries remain in [`docs/48`](48-android-companion-ux.md), [`docs/49`](49-android-companion-recommendation.md), and [`docs/50`](50-android-native-capability-spike.md). The repository still contains no Android client, widget provider, Gradle project, widget-specific API, or release pipeline. **This document authorizes no production access, production write, or deployment.**

## Decision

**Approved by Mark (2026-10-29): build the FinTracker Android app, then add the home-screen widget.** This lifts the discovery-only stance carried by docs 48–50, which remain valid as evidence and as the v1 product boundary. The widget requirement is the capability that earns native delivery: Android home-screen widgets require a native `AppWidgetProvider`, so a PWA cannot satisfy it.

Sequencing:

1. **Build the Android app** — read-focused companion per the v1 boundary in [`docs/49`](49-android-companion-recommendation.md): authenticated entry, masked glance, read-only drill-in, truthful refresh/stale states, safe deep-link handoff, Android Back behavior, help/full-web handoff. No mutations, imports, trading, tax work, or client-side financial computation.
2. **Then the widget** — under the boundaries in this document, including the [visibility toggle](#os-surface-privacy-proposal) and *last checked* freshness labelling.

Not authorized by this approval: production writes, client-side balance/price reconstruction, changes to server-owned financial truth, tenant-boundary or masking changes, removal of demo read-only behavior, or any claim of real-time freshness the server does not supply.

Remaining prerequisites are engineering, not product: a build toolchain (JDK/Android SDK) does not yet exist on the build host, and native session handling must be reconciled with the current access-token-in-memory plus HttpOnly refresh-cookie design before an authenticated client can be shipped.

The build must still answer whether a useful widget can show only server-authorized, privacy-approved information at a schedule that users understand as *last checked*, not live.

## Product proposition and boundaries

A home-screen widget is useful only for a brief, low-risk glance: “Is there something I need to review, and when did FinTracker last check it?” It is not a second dashboard. It must not offer partial imports, ledger edits, trading, reconciliation, tax work, settings changes, or financial mutations. The full responsive web product remains the route for complex work.

The existing companion documents classify widgets/lock-screen surfaces as **not a follow-on by default** because of shoulder-surfing and stale-data risk. This brief narrows the question to a feasibility review; it does not reverse that decision.

### Proposed widget concepts

| Concept | Candidate content | Intended user value | Current fit |
| --- | --- | --- | --- |
| **Review queue** | Generic count or label such as “1 alert to review”; tap opens the app to authenticated review | Returns to an existing work queue without showing private details | Best first candidate only if the existing inbox/alert source supports the journey |
| **Single-account glance** | One selected account’s name and **explicitly approved** value, always with server timestamp and masked-by-default state | Saves one app opening for a routine check | Requires a separate privacy decision; not permitted by current docs without approval |
| **Holding/watchlist glance** | One selected holding or watched symbol, source, market date, and staleness; no client-computed total | Surfaces an approved market-data follow-up | Useful only when the server supplies trustworthy provenance and the content policy permits it |
| **Unlock/privacy placeholder** | “Values hidden”, or a generic signed-out/private state, with an app launch action | Gives the launcher a useful entry point without displaying financial data | Safest baseline; recommended for the feasibility review |
| **Deep-link status** | Generic “FinTracker: open to review” action, no financial values in the launcher surface | Hands the user to an authenticated, revalidated destination | Useful only when the app and target authorization path exists |

**Default for the next milestone:** design and test the **generic review/deep-link concept plus locked/private placeholder**. A financial value on the home screen is a separate proposed variant, requiring explicit privacy approval and a named maximum age; it is not implied by selecting the review concept.

A widget must not:

- display an amount, balance, account name, ticker, payee, merchant, alert detail, or private queue identity in a default or lock-screen surface;
- display one tenant’s cached content after logout, expiry, identity change, or account switch;
- imply that a widget action resolved, approved, imported, paid, or changed anything;
- derive or blend balances, currencies, prices, or tax/return calculations client-side;
- use a notification or widget action as a substitute for authentication and server authorization;
- expose financial content in launcher previews, screenshots, app-switcher surfaces, or OS accessibility output without an approved policy;
- be labelled “live”, “current”, or “real time” merely because the widget refreshed or a background process was scheduled.

## Existing API and privacy boundaries

These are observed repository contracts at the time of this brief, not promises that a future widget may alter them.

| Boundary | Observed behavior | Consequence for a widget |
| --- | --- | --- |
| Financial truth | The server computes and serves account, portfolio, position, price, transaction, and notification data. Server responses contain Decimal strings, dates/timestamps, currency, provenance, and staleness fields where applicable. | The widget may render a bounded allowlist of server-returned fields. It must not recompute totals or prices. |
| Tenant isolation | Domain routes scope queries to the authenticated `user_id`; foreign/deleted records return generic not-found behavior where specified. | A widget cache and every request must be namespaced by identity and purged on identity transitions. |
| Demo mode | The shared demo tenant is readable but mutating routes answer `403 demo_account_restricted`. | Demo widgets are read-only and must retain an unmistakable demo/read-only marker; no write affordance. |
| Display masking | M139 is a client-side render-time mask, not encryption or access control. The web session mask defaults appropriately for privacy contexts and is session-scoped. | Widget masking is an independent launcher-surface policy. Do not claim the web mask protects stored or network data. |
| Privacy mode | Server-side privacy mode binds to external-fetch seams; cached/manual data remains usable where specified. | Widget refresh must not trigger a provider fetch or bypass the server’s privacy lock. A cache-only read does not make stale data fresh. |
| Auth | Access JWTs are short-lived; refresh tokens are delivered only in an HttpOnly, path-scoped cookie and rotated server-side. Login can require a second-factor challenge; passkeys exist. | A widget must not place refresh credentials in launcher storage, app preferences, widget state, or logs. Background refresh must use an explicitly designed, revocable, least-privilege session path—not copy the web cookie contract blindly. |
| Read authorization | Authenticated routes are tenant-scoped; widget deep links must authenticate and revalidate authorization, existence, and current tenant before displaying a target. | A tap from the launcher is only an ingress route. No target content may render before the server check succeeds. |
| Price provenance | Position/watchlist responses expose market date, source, provider-fetch timestamp where available, and staleness; absent provider-fetch evidence must be omitted rather than invented. | Never label a cached quote “live”; preserve source and distinguish market-date age from provider-fetch age. |
| Existing price stream | SSE price updates are cache-only, poll the database every five seconds while connected, and are not a background delivery mechanism. | A widget must not hold an SSE stream open or imply widget-level five-second refresh. |
| Notifications | The current inbox union supports `budget_threshold`, `bill_due`, and `tax_loss`; outbound channels are email/webhook. No widget/push event contract exists. | Do not invent alert events for widgets. The review-queue concept first requires an existing-source audit. |

## Proposed widget data contract

The future implementation must use a narrow, versioned read contract. This section is a requirement sketch, not a current endpoint and not authorization to add one.

```text
WidgetState
  schema_version
  state: locked | signed_out | refreshing | fresh | stale | unavailable
  server_checked_at: ISO-8601 UTC | null
  expires_after_seconds: integer | null
  tenant_scope: opaque non-reversible scope identifier | null
  content: approved minimal DTO | null
  content_version: opaque revision | null
  privacy: masked | approved-visible
  demo_read_only: boolean
  error_kind: generic enum | null
```

`content` may contain only fields explicitly approved for the chosen concept. If approval covers only a generic queue, `content` is a count/label. For a value-bearing variant, allowlist fields such as one server-supplied amount, currency, account label, source, market date, and fetch timestamp—never a full account list, raw ledger, query results, tokens, or secrets.

The client must not fabricate `server_checked_at`. If no server response is available, the widget shows the last successful timestamp only as **last checked** and changes state to stale/unavailable according to the approved maximum age.

## Update and freshness model

| Trigger | Proposed behavior | What may be claimed |
| --- | --- | --- |
| App foreground or user pull-to-refresh | Make one authenticated read request; update state only after the server responds. | “Last checked [server time]” or “Updated [server time]” |
| Launcher widget refresh (best effort) | Android may delay, batch, skip, or deny background work for battery, data, or process-lifetime reasons. Use a conservative minimum interval as a floor, not a schedule promise. | “Last checked [server time]”; never “live”, “real time”, “continuous”, or an exact next-refresh time |
| Device offline or API unavailable | Keep an approved last-known read only if it is explicitly permitted; otherwise render unavailable/locked. Never convert absence to zero. | “Offline · last checked [server time]” or “Couldn’t check · last checked [server time]” |
| Session expired or identity changed | Immediately remove or replace the prior tenant’s state; require authentication and server revalidation. | Generic signed-out/expired state; no financial values |
| Server returned stale price or unavailable price | Preserve source/date/fetch evidence and show the server’s stale/unavailable state. | “Price as of [market date] · source [source]”; never “current” when stale |
| Demo tenant | Read-only state with persistent demo marker. | Demo content only; no mutation language |

**Proposed default freshness floor for the feasibility review:** at most one foreground/user-initiated read plus best-effort background checks no more often than **15 minutes**, subject to Android’s actual constraints, API rate limits, battery policy, and security review. This is a proposed test bound, not a shipped refresh guarantee or a claim that Android will honor it.

A widget’s freshness label must be computed from the successful server response (or an explicitly approved server timestamp), not from widget render time. A delayed OS refresh must not make an old value look newly checked.

## Authentication and local-security proposal

1. Review whether a native widget host can use a short-lived access credential plus a protected refresh mechanism. Do not store passwords, TOTP secrets, recovery codes, raw refresh cookies, or long-lived bearer tokens in SharedPreferences, widget XML, launcher state, notifications, logs, or backups.
2. Prefer hardware-backed/Keystore-protected key material where supported, but state honestly that it does not protect a compromised, unlocked app from displaying decrypted content.
3. Design a revocable, user-controlled credential lifecycle: revoke on logout, identity change, widget removal, app uninstall, security-policy timeout, and session invalidation when next online.
4. Treat local biometric/device unlock as a **local privacy gate over a still-valid server session**, never as authentication, recovery, or proof that the widget’s cached value is current. Existing docs keep that capability evidence-gated.
5. Preserve tenant isolation under process death and rapid account switching. A widget update must be atomic with respect to identity: render only the identity that owns the payload.
6. Never use a widget deep link as an authorization decision. Resolve the target through an authenticated route and show generic unavailable when it is deleted, foreign-tenant, stale, or no longer permitted.

## OS-surface privacy proposal

The feasibility review must test actual launcher previews, lock-screen surfaces, screenshots, accessibility traversal, notification shade interactions, and app-switcher previews—not merely the widget’s normal visible layout.

Baseline proposal:

- default widget state is **masked/generic**;
- no financial value or private label appears in the launcher preview by default;
- values are hidden unless the user has explicitly turned a **visibility toggle** on for that widget;
- the toggle is user-facing (widget settings or app settings), defaults to **hidden** on install and after every identity change, and is per widget rather than global;
- switching it off takes effect immediately across every OS surface — launcher preview, lock screen, recents/app-switcher, screenshots, notification shade — not only on the next widget render;
- turning visibility on still expires the values on background/lock/logout according to policy, and re-locking is unconditional after logout, expiry, account switch, or tenant change;
- the toggle controls rendered visibility only; it is not authorization, encryption, or a change to server-side masking and access control;
- widget settings do not expose account names or raw tenant identifiers;
- the widget never renders another identity’s cached content;
- accessibility labels do not leak masked values;
- demo state is visibly distinct from a real tenant’s state.

A launcher is not an encrypted display surface. Display masking reduces shoulder-surfing only for the rendered copy; it does not hide data from the server, process memory, backups, or a compromised device.

## Feasibility milestone (explicit next stage)

**Name:** W-0 — Android widget feasibility and privacy review

**Status:** proposed, not implemented. Mark’s approval gate from [`docs/49`](49-android-companion-recommendation.md) remains closed for implementation until he approves this stage.

**Scope:**

- Select the generic review/deep-link and locked/private placeholder concepts only.
- Audit the existing notification/inbox sources for one safe, generic review journey; do not create a new alert system.
- Define the proposed `WidgetState` contract and a synthetic fixture pack; use no production or real financial data.
- Build a throwaway, non-production Android host/launcher prototype only if Mark approves the stage, with no store account, signing key, production endpoint credentials, backend write, or deployment.
- Test refresh timing, delayed/skipped background checks, offline behavior, expired sessions, logout/account switch, demo mode, stale/missing price provenance, and launcher/lock-screen preview leakage on the named Android version/device matrix.
- Produce a user-value comparison against the existing responsive/PWA path and a security review of the proposed credential, cache, purge, and deep-link paths.

**Exit criteria before a widget implementation epic:**

1. Mark explicitly approves the stage and names its owner, device matrix, and test endpoint.
2. The existing alert-source audit finds a useful, server-backed generic review journey—or the review concludes the concept has no justified value.
3. Security/QA approve the exact widget fields, local credential storage, maximum age, identity purge, offline behavior, and OS-surface privacy policy.
4. The prototype demonstrates no financial values in default launcher previews, no cross-tenant carryover, and honest last-checked/stale/unavailable states.
5. Refresh tests show Android may delay or skip work; the product copy does not promise exact or real-time refresh.
6. User evidence shows the widget improves a real task without creating a stale-data or shoulder-surfing failure.
7. A future implementation proposal names Android ownership, dependency/update process, device/OS coverage, signing/release plan, rollback owner, and security/QA gates.

**Stop condition:** if any credential, privacy, invalidation, or freshness property cannot be demonstrated, stop with the generic launcher placeholder or no widget. The Android app is approved, but a value-bearing widget is not pre-authorized by that approval: it must still earn its place by demonstrating the properties below.

## Verification and scope of authorization

This document is grounded in the repository's current Android discovery and mockup artifacts, API/auth/privacy contracts, and design-state spike at the time of review. Mark's approval covers building the Android app and then the widget; it does not alter any API endpoint, schema, credential model, tenant boundary, masking behavior, or demo read-only guarantee, and no endpoint, schema, Android module, Gradle file, widget provider, credential, test device, production data, or deployment has been created or changed by this document.

Relevant source artifacts: `docs/48-android-companion-ux.md`, `docs/49-android-companion-recommendation.md`, `docs/50-android-native-capability-spike.md`, `design/android-spike/`, `backend/app/api/auth.py`, `backend/app/api/deps.py`, `backend/app/api/portfolio.py`, `backend/app/api/watchlist.py`, `backend/app/api/notifications.py`, `backend/app/api/_price_stream.py`, `backend/app/schemas/portfolio.py`, `backend/app/schemas/watchlist.py`, and `frontend/src/lib/mask.ts`.

## Toolchain status (updated 2026-10-29, card t_a86ada99)

The build toolchain now exists: `android/` holds the pinned toolchain spec (`android/toolchain.md`: Temurin JDK 17.0.21, Android SDK 34 with build-tools 34.0.0, Gradle 8.9 wrapper, AGP 8.5.2, Kotlin 2.0.21), the non-interactive gate (`android/scripts/verify_toolchain.sh`, exit 0 on a fresh shell), and a minimal Kotlin project skeleton that compiles to a debug-signed APK. No app logic, auth, screens, or widget provider exists yet — those are follow-up cards. No credentials, keystores, or release signing exist anywhere in the repo.
