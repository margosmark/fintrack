# 50 — Android-native capability spike: native vs PWA verdict

**Status: design/evidence verdict only. No Android implementation, app scaffold, Gradle project, backend write path, platform commitment, or release is authorized by this document.** Checked against the repository and platform documentation on **2026-10-27**. This evaluates whether Android-specific capability—not visual polish—earns a Kotlin/Compose client. UX/product boundaries remain owned by [docs/48](48-android-companion-ux.md); sequencing and the closed approval gate remain owned by [docs/49](49-android-companion-recommendation.md).

## Decision summary

| Capability | What the current PWA actually lacks | Native increment | Verdict now |
|---|---|---|---|
| Biometric/device-lock re-entry | No local app-lock gate; but WebAuthn/passkey sign-in already exists in the web client. | Lifecycle-controlled local gate; native Keystore-bound key use. | **KEEP-IN-PWA** until re-entry friction is measured; test web passkeys first. |
| Local notifications | No OS notification/push implementation. Current inbox has only budget threshold, bill due, tax loss; no price/dividend/stale-feed event contract. | OS notification channels and app lifecycle/deep-link integration; native posting is not itself reliable event delivery. | **KEEP-IN-PWA**: first test standards-based Web Push against a real supported-device matrix. |
| Offline-safe read-only snapshot | Deliberate: service worker never caches `/api`; offline launches shell and shows no stale financial data. | App-controlled encrypted store/lifecycle and explicit offline UI; still cannot know remote revocation while disconnected. | **SPIKE-IT**: only a narrow threat-model + invalidation feasibility test, not offline finance feature work. |
| Android navigation/back | Web router owns browser history; no native back stack or notification-task integration. | Android Navigation Compose provides a system back stack/deep-link handling model. | **KEEP-IN-PWA**: evaluate current installed PWA on actual devices before paying for separate stack. |
| Controlled encrypted local storage | Browser storage is origin-scoped/best-effort and evictable; product currently avoids persisting query/financial snapshot data. | Android Keystore protects key material and can require auth; app owns lifecycle and deletion controls. | **SPIKE-IT** only as a bounded storage design/feasibility probe paired with offline snapshot; do not persist data yet. |

**Verdict:** the strongest native differentiator is controlled offline storage, but offline access is not currently proven to be a user need and creates a severe freshness/revocation obligation. Fund at most a short, non-production feasibility spike around that paired question. Do not start a Compose client on the strength of biometrics, notifications, or Back behavior alone: the PWA has relevant web APIs and the largest current gaps are unimplemented product/backend paths, not uniquely native APIs.

## Evidence boundary and financial-truth rules

Observed implementation, not assumption: `frontend/src/pwa.ts` registers a worker only in production, only in secure contexts, and treats registration as an enhancement; its comment explicitly says the LAN HTTP deployment runs without a worker (`frontend/src/pwa.ts` lines 8–17, 43–67). `frontend/public/sw.js` caches the application shell and hashed assets, but returns early for every `/api` request (lines 4–15, 193–219). This is an intentional fail-safe: there is no stale financial response to mistake for current data. The shell can open offline, but its data cannot; offline dashboard values must not be implied.

The auth client keeps the access token in memory and the refresh token in an HttpOnly cookie (`frontend/src/api/client.ts` lines 18–21); identity transitions remove React Query cache (`frontend/src/auth/AuthContext.tsx` lines 136–150). Display masking is explicitly presentation-only and session-scoped, not encryption or access control (`frontend/src/lib/mask.ts` lines 1–22). The current web app already implements WebAuthn passkeys (`frontend/src/lib/webauthn.ts` lines 106–130, 164–184), so claims that browser biometric/passkey capability is wholly unavailable would be false.

Any future native client must continue to consume server-supplied financial values and provenance. No local balance reconstruction, quote generation, mutation/optimistic-write path, notification action that implies resolution, or changed manual-price precedence. Demo remains read-only. Identity changes/logout must purge local private data before a different tenant is shown; lock-screen and app-switcher surfaces remain generic. Those are inherited hard constraints from docs/48 and docs/49, not negotiable spike outcomes.

## Capability-by-capability assessment

### 1. Biometric / device-lock secure re-entry — **KEEP-IN-PWA**

**PWA failure/evidence.** There is no app-level biometric relock on background/resume in the current client; the session-restoration path probes the refresh cookie and mask boot contract (`frontend/src/auth/AuthContext.tsx` lines 156–184). However, the claim “PWA cannot use biometrics” is unsupported: the web code already implements WebAuthn credentials through `navigator.credentials.create/get` (`frontend/src/lib/webauthn.ts` lines 106–130, 164–184), and WebAuthn is available in secure contexts on supported browsers.[7] The existing browser passkey authenticates to the server; it is not equivalent to a local gate over a still-valid session.

**What native buys.** A native host can apply a deliberate foreground/background timeout, place a local gate before rendering financial UI, and use device credentials without entering a browser credential ceremony. It can bind a cryptographic key operation to a biometric/device-credential prompt. Android documents `BiometricPrompt` and allowed authenticators including strong biometric and device credential.[1] Native does **not** extend an expired server session or replace server authentication.

**Approach if later justified.** `androidx.biometric:BiometricPrompt` around a device-local re-lock only; authenticate with `BIOMETRIC_STRONG | DEVICE_CREDENTIAL` policy where supported. Key-gated sensitive local state may use Android Keystore auth-bound keys, but never store a reusable password or treat a successful local prompt as server re-auth.[1][4]

**Security/maintenance cost.** Define lifecycle timeout, task-switch/screenshot treatment, fallback when biometrics are unavailable/enrollment changes, cancel/lock behavior, accessibility, device-credential fallback, and expired/revoked-session handling. A compromised/running process can still access unlocked content; a biometric prompt does not make the UI or server token unassailable. Adds native lifecycle/device test matrix and a sensitive auth seam.

**Escalation evidence bar.** First measure sign-in/re-entry completion, passkey availability and failure/cancel rates on representative Android browsers. Escalate only if users repeatedly cannot complete secure PWA re-entry, a tested passkey/web flow fails a defined security/usability target, and a local lock materially improves task completion without weakening server expiry/logout semantics. No such measurements were found in the evidence reviewed for this spike.

### 2. Reliable local notifications — **KEEP-IN-PWA**

**PWA failure/evidence.** There is no Notifications API, PushManager subscription, notification-click handler, or push registration in `frontend/src/pwa.ts` or `frontend/public/sw.js`; the worker currently handles only app-shell fetch and stale-chunk recovery. The web Notifications API is limited availability across browsers, requires permission, and mobile use needs persistent service-worker notifications rather than page-lifetime notifications.[8] The Push API can receive messages while the web app is not loaded when an active service worker and subscription exist, so “web push is impossible” is also false.[9]

The server contract is a stronger current blocker: `frontend/src/api/notifications.ts` defines inbox kinds only as `budget_threshold | bill_due | tax_loss` (lines 12–15), and the API inbox payload is fetched/read in foreground (lines 17–43, 57–68). There is no current price-move, dividend, or stale-feed mobile event contract evidenced here. Android native local notification APIs cannot create trustworthy events that the backend does not produce.

**What native buys.** Native can integrate Android notification channels, runtime permission, task stack and app deep links directly. Android 13+ requires the `POST_NOTIFICATIONS` runtime permission for non-exempt notifications; permission denial remains a hard opt-out, not something native can bypass.[11] Notification UI is only the last mile: timely server events still need an authenticated delivery service/token lifecycle or a scheduled worker. WorkManager is Android’s persistent-work API, but scheduling is system-managed rather than an exact-time delivery guarantee.[4]

**Approach if later justified.** First implement/measure standards-based Web Push using the existing service worker, generic privacy-safe copy, user gesture permission request, subscription revocation, and app-side authenticated deep-link revalidation. If a measured Android device/browser gap remains, compare an FCM-to-Android notification path; never poll markets on-device for price alerts, and never put amounts/security/account identifiers into notification payloads or previews. Android posting would use `NotificationCompat`, channels and a notification intent/deep-link into an authenticated route.[2][3]

**Security/maintenance cost.** Both paths require opt-in/opt-out and subscription/token deletion, abuse/CSRF review, delivery retries/deduplication, OS permission handling, generic copy, deep-link authorization after login, and privacy QA on lock screen. Native adds FCM/token infrastructure, Android permission/device matrix, and notification-channel support. Web push also exposes a push endpoint capability and needs endpoint secrecy and lifecycle management.[9]

**Escalation evidence bar.** Establish an event definition and service window for each alert; instrument a PWA Web Push baseline on named Android versions/browsers and permission states. Escalate only if a predefined supported-device cohort misses a pre-agreed delivery/re-entry target materially in the PWA while backend event generation is healthy, and a native-only route improves it in a controlled test. No current event contract for the requested price/dividend/stale-feed types was found; no reliability results exist in this spike.

### 3. Offline-safe read-only snapshot — **SPIKE-IT**

**PWA failure/evidence.** This is a real deliberate gap, not a hidden defect. The service worker states “the shell offline, the DATA never offline” and explicitly bypasses `/api` (`frontend/public/sw.js` lines 4–8, 193–201). A cold offline navigation may return cached `/index.html` (lines 203–219), but cannot show cached server data. The global TanStack `QueryClient` has no persistence configuration (`frontend/src/queryClient.ts` lines 1–9). The policy is aligned with docs/48’s rule that an offline finance snapshot must carry an always-visible last-updated time, be read-only, and be cleared on identity change/logout/timeout.

**What native buys.** A first-party Android application can own the local database and its lifecycle, encrypt a deliberately small snapshot with a key protected by Android Keystore, and make online/offline/last-updated status persistent in its own UI. Keystore keys can be non-exportable and restricted to authorized operations; hardware backing is device-dependent and must not be assumed universally.[5] This is greater control, not automatic safety: an offline device cannot learn that the server revoked access, the account changed, or a value became stale.

**Approach if later justified.** Store only a bounded allowlist of server-returned read DTOs (summary/account/position fields plus server timestamps, currency, price source/staleness, and identity/version); no API tokens, raw ledger dump, calculations, quotes, mutations, or notification payloads. Encrypt serialized snapshot at rest with AES-GCM; wrap the data key with an Android Keystore key, and require device credential/biometric for key use only if the chosen UX justifies it. Delete on logout, identity change, explicit clear, session revocation when next online, expiry, or schema incompatibility. Offline UI must say **Offline · last updated [server timestamp]** and may not render a live/current label. A native proof should use synthetic fixtures only and demonstrate lifecycle/invalidation; not real user data.

**Security/maintenance cost.** Highest of the five: threat model rooted/compromised devices, backups/device transfer, key invalidation/enrollment change, storage schema migrations, crash recovery, identity-switch races, logout while offline, remote revocation delays, retention/expiry, and accessibility of stale states. Keystore protects key extraction/usage policy but cannot prevent a compromised unlocked app from displaying decrypted values.[5] Browser storage is best-effort by default and eviction differs by browser; Android ownership improves control but does not solve remote invalidation or display leakage.[10]

**Escalation evidence bar.** Require (1) observed user demand from travel/connectivity cases, not a design preference; (2) explicit privacy/security approval of exact cached fields and maximum age; (3) demonstrated identity/logout purge and no-cross-tenant tests; (4) deterministic stale/offline labels and server timestamps; (5) key-loss/re-enrollment/reinstall/backup recovery tests; (6) documented mitigation for offline revocation; and (7) comparison showing PWA storage cannot meet the approved policy. If any invalidation/privacy property cannot be proven, drop offline financial data and keep the helpful offline shell/error state.

### 4. Android-native navigation and system Back — **KEEP-IN-PWA**

**PWA failure/evidence.** The app is a browser-history client using React Router’s `createBrowserRouter` and auth-guard redirects (`frontend/src/App.tsx` lines 1–3, 79–132, 134–164). It does not own an Android task stack; system Back is therefore browser/PWA history behavior, not a native app navigation controller. The current PWA already has mobile navigation and links through its shell (see docs/09 and docs/47). This difference is real, but no field evidence was found that current Android Back/deep-link behavior blocks users.

**What native buys.** Jetpack Navigation Compose supplies a `NavHost`/`NavController` graph, back stack, deep-link handling, and Back/Up behavior as Android application navigation.[6] It can make notification-to-detail, auth return, and detail-to-list scroll-context behavior an explicit native route contract. Compose is not automatically safer: it must still reject stale/foreign identifiers and re-check server authorization.

**Approach if later justified.** Use Navigation Compose with typed routes containing opaque record IDs only (no financial values), authenticated-route guard and a post-login destination continuation. Notification ingress starts a safe app task; resolve target over the authenticated API and show generic unavailable on deleted/foreign records. Preserve list position locally only within current identity and clear it at identity transitions. Avoid a WebView shell that merely wraps the same router.

**Security/maintenance cost.** Adds independently tested route graph, back-stack restoration, process death, notification task flags, auth redirects, deep-link parsing, accessibility and device/OS coverage. A second navigation implementation can drift from the browser product and create tenant-history leaks if the stack survives logout/account switch.

**Escalation evidence bar.** Reproduce a specific Android device/version failure in the installed PWA (Back exits unexpectedly, breaks list context, or notification re-entry lands incorrectly); compare against a native prototype and require zero auth/tenant leakage and materially better completion across a representative test set. Until reproduced, improve/verify the PWA’s own history and deep-link handling.

### 5. Controlled encrypted local storage — **SPIKE-IT** (as a design subquestion of #3 only)

**PWA failure/evidence.** The browser already offers IndexedDB and Web Crypto, but they are not a general secure-finance cache. The repo’s local encrypted backup stores ciphertext in IndexedDB using PBKDF2/AES-GCM and explicitly keeps no plaintext snapshot at rest (`frontend/src/lib/privacyBackup.ts` lines 1–8, 19–34, 63–91). It is a user-passphrase backup, not an offline dashboard. Ordinary web-origin storage is best-effort by default and subject to browser quota/eviction policy (`frontend/src/lib/tenantStorage.ts` lines 1–25; platform evidence [10]). Site data can be cleared by the user; browser-origin storage does not provide the app with native-style lifecycle controls.

**What native buys.** Android can store a small encrypted database/file under app-private storage and use Android Keystore for non-exportable encryption keys and auth-bound key operations.[5] The app has explicit control to clear a local snapshot at logout/identity transition and to keep it out of a web-origin cache. It does **not** make stored data revocable while offline, prevent display by a compromised unlocked process, or justify storing more data.

**Approach if later justified.** Design only: a single snapshot repository with minimum field allowlist, per-user namespace, server timestamp/expiry, AEAD encryption, key alias per account identity, no cloud backup of plaintext/key material, idempotent purge and key destruction on logout/identity change. Verify backup/restore behavior and make local encryption keys non-exportable where device support permits; clearly label hardware-backed status as variable. Never place credentials in preferences or plaintext database fields.

**Security/maintenance cost.** Encryption-at-rest alone is not access control; decrypted values enter process memory and screen surfaces. Must address Android backup exclusions, reinstall/key mismatch, device transfer, app uninstall, key invalidation, supported OS versions, migration and security review. Web storage is less operator-controlled but native encrypted storage materially increases responsibility and attack surface.

**Escalation evidence bar.** No encrypted-storage feature should advance without the offline demand and approval gates in #3, a reviewed threat model, a synthetic-data prototype that demonstrates encrypted-at-rest bytes, key invalidation, deletion/purge, backup exclusion, and identity switch/logout race tests, plus a clear retention maximum. This is a capability experiment, not permission to persist production financial data.

## Smallest worthwhile spike and exit criteria

**Worth funding only as a bounded feasibility study, conditional on approval; no code is included here.** Time-box the next authorized stage to a short design/QA experiment centered on one question: can a *minimum* read-only server snapshot be stored and cleared under an acceptable Android threat model, and is there enough real user demand to justify offline access? Do not build a home screen or duplicate web routes. Use synthetic DTO fixtures only; do not connect a prototype to production data or add a production write path.

Exit criteria before any Compose implementation proposal:

1. Mark explicitly authorizes this next stage (docs/49’s approval gate remains closed until that happens).
2. Product evidence establishes a concrete offline use case and maximum acceptable snapshot age.
3. Security review approves exact allowlisted fields, encryption/key lifecycle, backup exclusion, lock-screen behavior, and offline-revocation disclosure.
4. A prototype/design test proves the offline banner/time is persistent, financial fields are server-supplied only, all states distinguish stale/unavailable/hidden/zero, and no financial arithmetic or writes occur offline.
5. Tests prove logout, account switch, demo session, key invalidation, expiry and sign-out clear or hide all prior-identity data before rendering another identity; cross-tenant deep links resolve generically unavailable.
6. User evaluation shows useful improvement over the current PWA shell + “connect to refresh” fallback with zero critical trust/privacy failures.
7. The team names an Android owner, release/signing and dependency-update plan, OS/device matrix, crash/privacy policy and rollback owner.

If any of these fail, **stop at PWA**. Notifications and biometrics should remain in the PWA evaluation lane unless a measured web capability gap—not a preference for native appearance—passes their individual evidence bars.

## Sources

[1] https://developer.android.com/identity/sign-in/biometric-auth — Android biometric authentication
    > "One method of protecting sensitive information or premium content within your app is to request biometric authentication"
    > "The system lets you declare the following types of authentication:"
[2] https://developer.android.com/develop/ui/views/notifications — Android notifications
    > "Notifications allow your app to provide users with timely information and reminders even when the app isn't explicitly in use."
[3] https://developer.android.com/develop/ui/views/notifications/time-sensitive — Android time-sensitive notifications
    > "Create a notification channel to display notifications and let users manage notification categories in system settings."
[4] https://developer.android.com/develop/background-work/background-tasks/persistent/getting-started/define-work — WorkManager work requests
    > "This could cause a particular run of your work to be delayed, or even skipped if the conditions are not met within the run interval."
[5] https://developer.android.com/privacy-and-security/keystore — Android Keystore
    > "The Android Keystore system lets you store cryptographic keys in a container to make them more difficult to extract from the device."
[6] https://developer.android.com/guide/navigation — Android Navigation component
    > "Back and up: Handles back and up actions correctly by default."
[7] https://developer.mozilla.org/en-US/docs/Web/API/Web_Authentication_API — MDN Web Authentication API
    > "The Web Authentication API (WebAuthn) is an extension of the Credential Management API"
[8] https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API — MDN Notifications API
    > "This feature is not Baseline because it does not work in some of the most widely-used browsers."
[9] https://developer.mozilla.org/en-US/docs/Web/API/Push_API — MDN Push API
    > "The Push API gives web applications the ability to receive messages pushed to them from a server, whether or not the web app is in the foreground, or even currently loaded, on a user agent."
[10] https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria — MDN storage quotas and eviction
    > "The data stored in the browser by an origin is best-effort by default."
[11] https://developer.android.com/develop/ui/compose/notifications/notification-permission — Android notification runtime permission
    > "Android 13 (API level 33) and higher supports a runtime permission for sending non-exempt (including Foreground Services (FGS)) notifications from an app:"
