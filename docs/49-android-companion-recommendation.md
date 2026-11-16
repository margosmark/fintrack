# 49 — Android companion recommendation and platform decision gate

**Status: planning recommendation only. This is not a platform commitment or an implementation specification.** It is the PM synthesis of the UX lane, [`docs/48-android-companion-ux.md`](48-android-companion-ux.md), and the visual-design lane, `design/android-material/android-companion-concept.html` plus `visual-system.md` (task `t_18de3414`). The UX document owns experience boundaries; the visual artifact owns its Material-native direction. This document decides how to sequence a decision, not that an Android app should now be built.

## Decision in one page

**Recommendation: take the incremental PWA-first path now; retain Kotlin/Jetpack Compose as the conditional Android-native successor; do not choose a cross-platform shell for this Android-only discovery.**

FinTracker already has a responsive/PWA baseline, while the companion's differentiated jobs are narrow: secure re-entry, truthful read-focused glance/review, and (only if justified) notification handoff, system navigation, local re-lock, and an offline-safe snapshot. Shipping a native wrapper before measuring whether those jobs merit its security, release, and maintenance burden would be cosmetic duplication. Conversely, a future app that must deliver reliable Android notifications, system-native navigation, controlled local re-lock, and carefully governed private storage is best served by Kotlin/Compose—not a shell selected merely to look native.

The next allowed decision after Mark's approval is therefore a **bounded technical/product validation plan**, not Android application delivery. Re-evaluate the platform once evidence establishes a material native-capability gap that the PWA cannot safely or reliably close.

## Evidence used

| Lane | Verified input | Decision consequence |
| --- | --- | --- |
| UX — `t_7e29a6ae`, `docs/48` | Defines the companion as rapid, low-risk check-in and review, not a second finance workstation. It prioritizes server-authoritative values, tenant/cache clearing, display masking, read-only demo truth, price provenance and explicit stale/offline state. | A v1 must be read-focused and truth-preserving; editing, imports and tax work do not justify a native build. |
| UX — `t_7e29a6ae`, `docs/48` | Identifies system navigation as v1 behavior, while biometric re-lock and an offline snapshot are later/conditional. Push is only a privacy-safe handoff if current server alert events justify it. | Native capability value is plausible but unproven; no platform should be selected before capability feasibility and user-value evidence. |
| Visual design — `t_18de3414` | Material-native Monitor concept with three representative frames, light/dark token behavior, and verified rendering at 740px. It deliberately remains a planning/design artifact. | Material conventions are a viable future target. The artifact is not evidence that a native runtime, navigation stack, notification path, or storage model exists. |
| Existing product baseline | Responsive/PWA shell already exists; the discovery epic verified no Android, React Native, or Capacitor scaffold. | PWA-first extends a working surface and avoids a second client before differentiated benefit is demonstrated. |

## Recommended v1 product boundary

“V1” means the first user-facing companion scope **if Mark later authorizes implementation**. It does not authorize that implementation now.

### In scope: read-focused companion

1. **Secure authenticated entry and safe return** — sign-in/session expiry handling, intended-destination continuation only after authentication, and tenant-safe logout/identity changes.
2. **Masked glance** — account/portfolio summary, account rows, freshness/provenance and one high-signal recent item or alert. Amount masking is obvious, reaches all displayed values/chart labels, and resets on lock/background/logout according to an approved security policy.
3. **Read-only drill-in** — account, holding and transaction review with account/context, exact currency, server-supplied values, pending-import status, manual-price source and timestamp/staleness. No client-side balance reconstruction or optimistic money writes.
4. **Truthful refresh and states** — explicit refresh; distinct loading, unavailable, no-data, stale, locked, permission and session-expired states. Cached data, if any, is timestamped and never presented as live.
5. **Safe deep-link handoff** — only to an authenticated, authorized, extant record. A stale, deleted or cross-tenant target resolves to a generic unavailable state, never another record.
6. **Android navigation behavior** — expected Back behavior that preserves list/detail context and does not expose an unrelated prior tenant after notification or session recovery.
7. **Help and full-web handoff** — an explicit route to the web product for work outside this boundary.

### Deferred: evidence- and safety-gated

| Capability | Prerequisite before scope | Non-negotiable condition |
| --- | --- | --- |
| Local biometric re-lock | Security-policy review and demonstrated re-entry need | It is an opt-in local gate over a still-valid server session, never account recovery or a substitute for server authentication. |
| Offline-safe read-only snapshot | Threat model, encrypted/private storage decision, expiry/invalidation design and user opt-in | Minimal data only; permanently visible offline/last-updated state; no writes, calculations, alerts, quotes or cross-identity cache survival. Omit it if private invalidation cannot be proven. |
| Push notification handoff | Existing alert event/source supports a useful review journey and permission/copy policy is approved | Generic OS-surface copy without amount, merchant, security or account name; deep-link auth/revalidation; no faux notification action. |
| Share-sheet/import capture | End-to-end privacy disclosure and web-equivalent staged review/commit design | Never auto-commit, and never expose sensitive content outside reviewed upload/staging boundaries. |
| Widgets/lock-screen surface | Separate privacy and stale-data product decision | Not a follow-on by default; high shoulder-surfing risk means no financial value display without approval. |

### Remain web-only / web-first

The companion must link out rather than imitate a partial high-consequence workflow:

- ledger transaction create/edit/delete, categorization, splits, transfers and bulk operations;
- import upload resolution, matching, batch commit/discard/revert and reconciliation;
- trades, dividends, manual price edits and other investment writes;
- tax/ACB workflows, tax reporting, exports and dense report comparison;
- account/institution management, rules, budgets/goals/debts configuration and forecast scenario editing;
- security/session/password/credential administration, backup/restore and data export;
- any flow requiring substantial table context, multi-record confirmation or a server mutation.

The responsive web product remains the durable fallback for these tasks whether or not a companion is eventually delivered.

## Platform decision matrix

Ratings are comparative for FinTracker **at this time**: Strong / Moderate / Weak. “Cross-platform shell” means a WebView/hybrid wrapper or equivalent React/web-first mobile shell; it does not assume a particular vendor. This matrix evaluates the first Android companion only, not a future iOS commitment.

| Criterion | Kotlin + Jetpack Compose | Cross-platform shell | Incremental PWA-first |
| --- | --- | --- | --- |
| Trust and security constraints | **Strong** — first-class Android lifecycle, credential/keystore and privacy controls are available, but they add native security design and release responsibility. | **Moderate** — can reach native controls through plugins, but boundary/plugin review and WebView semantics complicate the threat model. | **Strong now** — reuses mature web session/server controls; must still avoid unsafe browser caching and false offline claims. |
| Native-capability benefit | **Strong** — best path for Android Back behavior, notification channels, local re-lock and disciplined storage if later justified. | **Moderate** — can expose many capabilities, with greater bridge/reliability risk. | **Weak to Moderate** — installable web and browser notifications can help, but capability reliability/control varies and does not prove a native app need. |
| Delivery risk | **Weak initially** — new client, build/release pipeline, QA matrix and native expertise are all new. | **Moderate** — fast apparent start but plugin, shell, store and bridge failure modes arrive early. | **Strong** — smallest change surface, continuing the shipped responsive/PWA path. |
| Ongoing maintenance cost | **Weak** — independent Android code, dependency and OS compatibility lifecycle. | **Moderate** — shared UI may lower feature cost, but bridge/vendor/native maintenance persists. | **Strong** — one product UI/runtime; no second application lifecycle. |
| Design fidelity to Material concept | **Strong** — Compose naturally expresses Material behavior, dynamic system interaction and navigation patterns. | **Moderate** — can visually approximate the concept; system behavior may still reveal a shell. | **Moderate** — web can implement the information hierarchy/tokens, but cannot fully reproduce native interactions. |
| Offline-safe, read-only need | **Strong potential, high obligation** — controllable storage and invalidation are possible, not free. | **Moderate** — feasible but bridge/storage policy increases audit surface. | **Moderate** — service worker caching can support limited snapshots but is the easiest route to accidental stale-data ambiguity; not recommended until explicitly designed. |
| Notification reliability | **Strong potential** — Android notification channels and deep-link handling are controllable after backend/event readiness. | **Moderate** — typically plugin-dependent and operationally more complex. | **Weak to Moderate** — browser/device support and delivery semantics vary; acceptable only as an experiment, not as a promise. |
| System navigation | **Strong** — native back stack and notification re-entry can be designed/tested as first-class behavior. | **Moderate** — shell must reconcile web router and Android Back stacks. | **Moderate** — browser/PWA navigation is usable but has less lifecycle/back-stack control. |
| Future extensibility | **Strong for Android** — sound base if validated Android-only differentiated capability becomes strategic. | **Moderate** — potential multi-platform reuse trades against long-lived bridge constraints. | **Strong for learning, weak for native expansion** — fastest evidence path, but does not itself create an Android-native base. |
| **Current decision** | **Conditional successor** — select only after evidence proves a material native gap and a Kotlin delivery/security plan passes review. | **Do not select** — no iOS requirement or proven shared-code saving offsets shell risk today. | **Recommended now** — improve/measure the existing mobile surface and validate the native-capability case before funding a second client. |

### Why not select Kotlin now?

The UX and design lanes establish that a native experience could be desirable; they do not establish that it is necessary. At present, the P0 journey is a masked, server-backed glance and read-only review—already aligned to a responsive/PWA product. The costly differentiators (notifications, local re-lock and offline snapshots) are expressly conditional in the UX scope. Selecting Kotlin now would convert unresolved product/security questions into implementation work.

### Why not select a cross-platform shell?

FinTracker has no authorized iOS goal, no existing native scaffold, and no validated native-only workflow. A shell would introduce app-store, plugin, bridge, lifecycle and notification maintenance without delivering the security simplicity of PWA-first or the Android reliability/fidelity of Compose. Revisit only if a separately approved multi-platform strategy supplies evidence and ownership for shared native code.

## Assumptions

1. The web responsive/PWA product remains the primary workstation and continues to own complex financial workflows.
2. Financial values, calculations, permissions and record authorization remain server-authoritative; a companion consumes validated server responses.
3. The existing alert/inbox service may support a useful notification handoff, but this has not been capability-audited for mobile delivery.
4. FinTracker's initial native target, if any, is Android only; no iOS parity date or cross-platform business case is approved.
5. Material design direction is accepted as a concept, not as an implementation library or design-system commitment.
6. The user population and baseline mobile completion/retention metrics needed to quantify native benefit are not yet available in this discovery record.

## Unresolved questions that must be answered before any native selection

1. **User value:** Which mobile jobs currently fail or take materially longer in the responsive/PWA product: re-entry, alert handoff, offline viewing, navigation, or something else?
2. **Notification feasibility:** Which existing server events can safely produce user-controlled, generic notifications; what delivery service, token lifecycle, opt-out/delete behavior and deep-link authorization model would be required?
3. **Security policy:** What background timeout, device-unlock rule, local biometric behavior, screenshot/app-switcher protection and device-compromise stance are acceptable?
4. **Offline policy:** What exact fields could ever be stored locally; encryption/key ownership, expiration, tenant switch, logout, remote invalidation and recovery behavior; and can each be tested and audited?
5. **Operational ownership:** Who owns Android signing, dependency/security updates, device/OS coverage, crash monitoring, accessibility and release rollback?
6. **Economics:** Is Android-only value sufficient to fund a separate client, or is an iOS/multi-platform strategy likely enough to change the cross-platform calculation?

## Measurable success criteria for the next decision stage

No production rollout metric is implied. The following are approval criteria for a future bounded validation plan:

| Dimension | Measure | Threshold / evidence required |
| --- | --- | --- |
| P0 job value | Instrumented, consented research or moderated usability sessions compare the existing mobile web path with the proposed companion flow for sign-in → masked glance → holding/transaction review. | At least 5 representative users complete each P0 scenario without financial-state misunderstanding; no critical trust failure (wrong currency, stale value perceived current, cross-tenant exposure, hidden pending status). |
| Readability/accessibility | Narrow-phone and large-text/screen-reader validation of mask state, freshness, source, currency, errors and Back behavior. | 100% of predefined acceptance scenarios pass; all primary touch targets meet the existing 44px floor; no information conveyed only by color. |
| Truth and privacy | Security/QA test matrix exercises login/logout/account switch, expiry, lock/background, demo, stale/deleted deep links and withheld/unavailable data. | Zero private-data carryover between identities; zero unmasked financial content in OS notification/app-switcher surfaces; every cached/offline display visibly identifies its timestamp/state. |
| Notification justification | Prototype or technical spike against a non-production test path confirms an existing alert source can drive a generic notification and safe revalidated deep link. | At least 95% of test deliveries on the defined supported Android/device set reach the intended in-app state within the agreed service window; unsupported states have a user-visible fallback. This is a gate to scope push, not a requirement to ship push. |
| Native gap evidence | PWA-first measurement establishes that a differentiating job cannot meet the agreed reliability or system-integration target through the web runtime. | Written comparison shows the gap, affected users, workaround and why it is not a product/design issue; only then open a Kotlin option review. |
| Delivery readiness | A future implementation proposal identifies owner, threat model, test/device matrix, signing/release plan, dependency update process and rollback strategy. | Security and QA sign off on the proposal before an implementation epic is decomposed. |

## Explicit non-authorization and Mark approval gate

This planning epic authorizes **only** the recommendation recorded here. It authorizes **none** of the following:

- application code, Android project/scaffold, Kotlin/Compose, cross-platform wrapper, PWA code changes or backend/mobile API changes;
- a platform commitment, vendor/library selection, Android signing key, Play Console/Play Store account or store submission;
- notification permission request, push service, device-token storage, local biometric flow, offline cache/storage or analytics/crash SDK;
- production deployment, production-data collection, feature flag activation or release.

**MARK-APPROVAL GATE — CLOSED:** No implementation, spike, security-review, QA, Play Store, notification, analytics or deployment card may be created or dispatched from this synthesis until Mark explicitly approves this document and names the permitted next stage. Until then, the only valid result is this planning record; the responsive/PWA product remains the product of record.

## Verification

This is a documentation-only planning decision. Verification performed for this synthesis:

1. Read the completed UX lane at `t_7e29a6ae` / `docs/48-android-companion-ux.md` and retained its v1/later/web-only boundaries and financial-trust invariants.
2. Read the completed visual-system artifact at `t_18de3414/design/android-material/visual-system.md`; its verified rendering/dark-theme concept is cited as direction, not implementation evidence.
3. Confirmed this worktree contains no Android-native source addition and this document makes no production/platform authorization.
