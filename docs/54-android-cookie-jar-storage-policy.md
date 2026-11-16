# 54 — Android cookie-jar auth storage policy (security review of docs/52 Option A)

**Status: security verdict + binding policy for the Android auth implementation.**
Owner: @security. Reviews the Option A recommendation in
[`docs/52`](52-android-native-auth-and-read-api.md) (dedicated Android HTTP client,
app-scoped persistent cookie jar reusing the `refresh_token` cookie flow, access JWT
in process memory, serial refresh, no backend change) and turns it into checks the
implementation must pass. **This document authorizes no code, no backend change, no
production access, no production record, no signing key, and no widget credential
path.** It adds no capability: it only constrains how the already-authorized v1
companion ([`docs/49`](49-android-companion-recommendation.md),
[`docs/51`](51-android-home-screen-widgets.md)) may hold and use the existing
credential. Precedence: where this doc, `docs/48`, `docs/49`, `docs/51`, `docs/52`
or `docs/53` differ, **the stricter boundary governs**
([`docs/53`](53-android-v1-acceptance.md) L9–L16).

**Evidence basis.** Every backend/frontend/file claim below cites `file:line` from
repository snapshot `origin/main` commit
`afa70cf8bc3c3be0aa9d1c52741c7d77685ea136` (2026-10-29). That tip contains the
`android/` toolchain skeleton only — no auth code, no cookie jar, no Keystore use
(`android/app/src/main/kotlin/com/margosmark/fintracker/MainActivity.kt` L8–L17).
Android **platform** behavior (Keystore semantics, backup mechanics, debuggable
derivation) is labelled *platform-unverified*: it is a requirement stated against
current Android guidance, not an observed fact on a device, and must be re-verified
against current Android documentation at implementation time, exactly as
`docs/52` L59 and L102 require.

## 0. Verdict summary

| # | Question | Verdict |
| --- | --- | --- |
| 1 | Is persistent app-private cookie storage acceptable for the 30-day rotating refresh credential at all? | **YES — accepted with conditions** (§1). App-private, Keystore-encrypted, backup-excluded, debuggable-fail-closed. Plaintext persistence and any shared/public storage are **rejected**. |
| 2 | Must the native client serialize refresh like the SPA? | **YES — mandatory** (§2). A concurrent loser is treated as reuse and triggers chain-wide revocation. |
| 3 | Exposure surface | 14 concrete implementer checks, each with a verification method (§3). |
| 4 | Logout / identity-change sequence | **Conditionally sufficient** (§4): the proposed order is right, but four additions are required before it can pass review. |
| 5 | Non-goals | **Confirmed** (§5): nothing here authorizes a native refresh-token header/body protocol, client-wide read-only scopes, per-device session listing/revocation, or any backend change. |

## 1. Storage policy — where the jar may live

### 1.1 Verdict on persistence

Persistent app-private cookie storage is **acceptable** for this credential, with
conditions. Rationale, grounded in the server contract the app is a client of:

- The credential is **opaque, short-lived-ish, rotating and revocable server-side**:
  32 random bytes, stored server-side only as a SHA-256 hash
  (`backend/app/core/security.py` L56–L66; `backend/app/models/refresh_tokens.py`
  L11–L19), default 30-day expiry (`backend/app/core/config.py` L20), consumed
  atomically on every refresh (`backend/app/api/auth.py` L194–L220), revocable by
  logout (`backend/app/api/auth.py` L455–L456) and chain-revoked on reuse
  (`backend/app/api/auth.py` L390–L402). A stolen *persisted* copy therefore dies by
  rotation, expiry, logout or reuse detection — it is not a static secret like a
  password.
- The persistence buys what Option C (in-memory only, `docs/52` L54) cannot: session
  survival across process death, which a companion app with background/widget
  ambitions needs. Rejecting persistence outright would force sign-in on every
  process restart and push the product toward the widget epic's harder credential
  problem prematurely.
- The honest risk statement stands (`docs/52` L37, L52): `HttpOnly` is a
  **browser-script** boundary and does not exist natively. The persisted jar is a
  bearer credential readable by the app's own process, and by anyone who can read the
  app's private directory (root, physical access with the device unlocked, forensic
  extraction). Encryption at rest (§1.2) and the debuggable gate (§1.5) narrow that
  window; they do not close it. A compromised *running* process is out of scope of
  any storage control — that residual is accepted exactly as the SPA accepts it.

### 1.2 Required storage properties (the implementation contract)

| # | Requirement | Rationale / evidence |
| --- | --- | --- |
| S1 | The jar lives **only in app-private internal storage** (`filesDir`/`cacheDir`, UID-isolated, no `INTERNET`-shared path). Never external/shared storage, never a content provider, never a shared `CookieManager`/WebView cookie store, never `SharedPreferences`, never a plain SQLite/Room column. | `SharedPreferences` and widget/launcher surfaces are already banned for raw refresh cookies by `docs/51` L104 and `docs/53` AC6.5 (L105). The WebView `CookieManager` is a separate shared store with its own sync/persistence and would add a WebView attack surface the v1 app does not have. *(platform-unverified: exact `CookieManager` persistence mechanics.)* |
| S2 | The on-disk jar is **encrypted at rest**: AES-GCM with a non-exportable Android Keystore key (fresh key per app install; per-identity key alias where §4 identity change applies). The key never leaves the Keystore; the jar file never contains the key. | `docs/50` L59, L83 prescribe exactly this shape (AEAD, wrap data key with Keystore key, key alias per account identity, idempotent purge + key destruction). Keystore non-exportability: `docs/50` footnote [5]; hardware backing is device-dependent and must not be assumed (`docs/51` L105). *(platform-unverified: StrongBox/`setUserAuthenticationRequired` selection — chosen at implementation against current guidance; the crypto **library** is deliberately not named here, same status as `docs/52` L59.)* |
| S3 | **Backup is excluded, permanently**: `android:allowBackup="false"` stays as shipped (`android/app/src/main/AndroidManifest.xml` L7); no `backupAgent`, no `fullBackupContent`, no `dataExtractionRules` carve-out that re-includes the jar file. Any future PR flipping this flag on is a security finding, not a config tweak. | With `allowBackup=false` the jar cannot ride cloud backup or device-to-device transfer. *(platform-unverified: exact adb-backup/device-transfer semantics per API level — verify at implementation; `minSdk 26`, `targetSdk 34`, `android/app/build.gradle.kts` L8–L15.)* Restore-to-new-device therefore yields a dead key + unreadable jar: **benign** — the user signs in again. Reinstall-without-restore is the same. This must be treated as normal UX, never "fixed" by exporting the key. |
| S4 | The jar file contains **only the refresh cookie** — its value, domain/host, path, expiry and secure flag. No access JWT, no user object, no financial data, no device/user identifiers beyond what cookie replay requires. | The access JWT is process-memory-only, mirroring the SPA (`frontend/src/api/client.ts` L18–L21, L56–L67). Note the *response-body* trap: the access JWT **is** in the login/refresh JSON body (`backend/app/schemas/auth.py` L31), so it is one careless disk-cache or log line away from persistence — §3 checks E1, E2. |
| S5 | Cookie attributes from `Set-Cookie` are honored by the native jar: name `refresh_token`, `Secure`, `Path=/api/v1/auth` (`backend/app/api/auth.py` L69–L70, L94–L102). The jar **sends the cookie only over HTTPS and only to `/api/v1/auth` paths**, and stores it only from responses of the auth routes. | Path scoping is a real defense: on ordinary API calls the refresh credential is never on the wire, so no interceptor, proxy log or packet capture of data traffic can contain it. The native client targets the production HTTPS endpoint only; a non-HTTPS base URL is a misconfiguration and must fail closed at startup. *(platform-unverified: this is the native analogue of the MCP trap — cookie stacks mishandling `Secure` on non-TLS hops; see the fin-tracker auth contract, and `docs/52` L48. Prove jar send/drop behavior with a test in §6.)* |
| S6 | Persistence happens at exactly one seam: the HTTP client's cookie handler, after a successful `Set-Cookie` from `POST /auth/login`, `POST /auth/demo`, `POST /auth/refresh`, the second-factor/passkey/OIDC confirmations, or their equivalents. Nothing else writes the jar. | Those are the only cookie-setting paths: `backend/app/api/auth.py` L312–L313 (login), L329–L330 (demo), L429–L430 (refresh), `backend/app/api/auth_second_factor.py` L291–L292, L321–L322, L589–L590, `backend/app/api/auth_oidc.py` L260–L261. A TOTP-capable account's password login returns a **challenge with no session and no cookie** (`backend/app/api/auth.py` L306–L311) — the native login flow must not treat HTTP 200 as a session or attempt to persist anything from a challenge response (`docs/52` L24). |

### 1.3 What the encryption does and does not protect (honesty clause, binding on UI/docs copy)

- **Protects:** offline extraction of the jar file (device theft while locked,
  forensic image, backup artifact if backup were ever re-enabled, another app
  without root). This is the threat S2 exists for.
- **Does not protect:** a compromised or running app process (it can read the jar
  through the app's own APIs and the decrypted cookie in memory); screen surfaces;
  the network; anything a rooted attacker does while the app runs. Never describe the
  jar as "device-bound", "hardware-secured", or "encrypted credential storage" in
  user-facing copy without this qualifier — `docs/52` L52 and `docs/51` L105 set the
  same honesty bar for Keystore claims.

### 1.4 Debuggable-flag policy

- A **debuggable build must never persist the cookie jar.** Debug builds are
  `run-as`-able and inspectable; persisting a live credential there converts any
  dev device into a credential store with an inspection toolchain attached. The
  implementation must check `ApplicationInfo.FLAG_DEBUGGABLE` at runtime **before the
  first persist** and fall back to a non-persistent in-memory jar (Option C behavior,
  `docs/52` L54) on debuggable builds — fail closed, not a log warning.
  *(platform-unverified: flag derivation from build type.)*
- The repo's release build type is currently a debug-only card — no release
  signing, no minification (`android/app/build.gradle.kts` L27–L31). Release
  signing/production distribution is out of scope for v1 (`docs/53` L158–L159);
  when it arrives, "release build is not debuggable and is signed" is a
  **precondition of this policy's acceptance**, reviewed with the signing design,
  not an assumption.

## 2. Serialization — single-flight refresh is mandatory, not a nicety

### 2.1 Why

`POST /auth/refresh` consumes the presented token with a conditional UPDATE and
exactly one concurrent caller can win; a loser presenting a *spent, unexpired* token
is treated as reuse and triggers **chain-wide revocation of every live refresh token
of that user — including the winner's brand-new successor** (`backend/app/api/auth.py`
L194–L220, L390–L402; expiry is deliberately excluded from the cascade
L396–L401). One lost race logs the tenant out of every device. The SPA built
single-flight refresh plus a cross-tab Web Lock specifically because of this
(`frontend/src/api/client.ts` L91, L113, L295–L331, with the defect history at
L76–L90). The native client must provide the equivalent guarantee.

### 2.2 Required mechanism (shapes, not library choices)

| # | Requirement |
| --- | --- |
| R1 | **One HTTP client, one jar owner.** A single `OkHttpClient`-equivalent instance with a single auth/refresh interceptor owns the cookie jar for the whole app. No second client instance may hold or read the jar. |
| R2 | **One in-flight refresh, process-wide.** All refresh callers — startup session probe, 401-recovery, any foreground/background trigger — await the *same* shared refresh operation (coroutine `Mutex` + shared `Deferred`, or equivalent). No polling, no second POST. Mirrors `pendingRefresh` (`frontend/src/api/client.ts` L91, L325–L331). Waiters adopt the winner's access token from the shared result rather than issuing their own refresh; mirrors `postRefreshToken` storing the winner's token (`frontend/src/api/client.ts` L334–L351). |
| R3 | **Single replay, auth routes excluded.** On a 401 from an ordinary API call: refresh once, replay the original request once, else clear tokens and land on the sign-in screen — mirroring `withAuthRetry` (`frontend/src/api/client.ts` L202–L248). 401s from login/register/demo/refresh/second-factor/passkey/OIDC routes must **propagate, never trigger a refresh** — mirroring `AUTH_401_SKIP_PATHS` (`frontend/src/api/client.ts` L152–L174); a refresh mid-sign-in both masks the real error and burns a rotation. The refresh call itself must never route through the client's own 401→refresh path. |
| R4 | **Logout takes the same lock, with a generation check.** Logout acquires the refresh lock (or awaits the in-flight refresh), bumps a session-generation counter, then clears. A refresh response that lands after the generation bump must be **discarded, not persisted** — otherwise a refresh racing logout writes a fresh cookie after the jar was cleared, and the "logged-out" device silently holds a live session that survives the identity switch (§4). |
| R5 | **One process.** No component touching the jar may run in a separate process (`android:process`); background work (WorkManager or any worker) uses the same process and the same single HTTP client/jar. A second process would silently hold a second jar copy and reproduce the race the single-flight exists to prevent. *(platform-unverified: WorkManager's default same-process model.)* |
| R6 | **Refresh budget awareness.** The endpoint is rate-limited to 30/min (`backend/app/api/auth.py` L366–L367); the client must not implement retry/backoff loops that could burn the bucket, and must surface exhaustion as a plain error state. |
| R7 | **Widget/background refresh is NOT covered by this policy.** The jar is for the foreground v1 app only. Any credential path for the widget epic needs the separately designed, revocable, least-privilege session path (`docs/51` L104–L107, `docs/53` AC6.5 L105) and its own security review. Sharing this jar with a widget provider is rejected. |
| R8 | **A background refresh failure never escapes the refresh thread.** The caller is blocked in `future.get()` — its try/catch sits on ITS thread, so anything thrown inside the pool runnable is uncaught, and Android's uncaught-exception handler kills the PROCESS (t_6781e4c5: the missing `INTERNET` permission's `SecurityException` took the app down 2.1 s after launch, before any UI rendered). Every failure — `IOException` or not — maps to `Unreachable` ("no answer", not a session verdict), the single-flight slot is always freed, and the future always completes: a future that never completes hangs every caller in `future.get()` forever. Pinned by `ApiClientTest` (non-IO + unreachable-base paths, recording executor) and `BootSmokeTest` (boot restore path). |

## 3. Exposure surface — checks the implementer must pass

Cookie values (and the access JWT, which shares most of these surfaces) must never
reach: UI, logs, analytics, clipboard, deep links, widget state, ordinary
preferences. Concrete checks, each with a verification method; all are pass/fail and
belong in the implementation's security review alongside `docs/53`'s ACs:

| # | Check | Verification method |
| --- | --- | --- |
| E1 | **No body/headers logging, ever, in any build.** Any HTTP logging interceptor is debug-only, excluded from release via build-type wiring, and redacts `Authorization`, `Cookie`, `Set-Cookie` headers *and* the `access_token` JSON field by default. | Code review of the interceptor wiring + a debug-run transcript showing redacted markers, never values. |
| E2 | **No disk cache of auth responses.** `POST /auth/*` responses carry the access JWT in the body (`backend/app/schemas/auth.py` L31) — an OkHttp disk cache (or any cache directory) must exclude `/auth/*`, and response bodies must never be serialized into app storage for debugging. | Code review + inspect app cache dir after a full login→refresh→logout walkthrough. |
| E3 | **No token in any log line**, including exception messages, coroutine crash context, and strict-mode reports. Auth failures log the error **code** (`{detail, code}` envelope, e.g. `invalid_or_revoked_refresh_token`, `backend/app/api/auth.py` L404–L408) — never the cookie. | Grep the logging seam for cookie/token variables; walkthrough log capture. |
| E4 | **No crash/analytics/ads SDK** in v1 — a crash reporter that captures request context or storage dumps is a credential exfiltration channel. Adding one requires a separate privacy decision (`docs/53` L173–L174, `docs/49` L122). | Dependency review of `android/app/build.gradle.kts`. |
| E5 | **Clipboard: never.** No code path copies a token, cookie value, or auth response to the clipboard. | Code review. |
| E6 | **Deep links never carry credentials.** No token in any URI, and deep links resolve through auth + server authorization + existence check before rendering, landing on one generic unavailable state (`docs/53` AC3.2 L73). A stale link must not trigger a refresh by itself. | Interrogate intent filters + walkthrough with foreign/expired targets. |
| E7 | **No URL-embedded access tokens.** The SSE price stream accepts an access token as a query parameter (`backend/app/api/deps.py` L203–L214); v1 does not use it (`docs/51` L62), and if it ever does, request URLs must never be logged — a query token lands in every intermediary's access log. | v1: confirm no SSE usage; pin any future use to a review. |
| E8 | **Widget/RemoteViews state carries zero credential material** — already binding via `docs/51` L104 and `docs/53` AC6.5 (L105); restated here because the jar makes the violation one import away. | Widget epic review (§2 R7). |
| E9 | **Ordinary preferences hold no auth state.** `SharedPreferences`/DataStore contain UI settings only (mask defaults, widget toggles) — never tokens, never the user's email tied to a credential. | Inspect pref files after login/logout. |
| E10 | **Screenshots / app-switcher / notifications show no credential surface.** No debug screen, session-info screen, or "about" screen renders token material; masking rules for *values* are `docs/53` AC2.1–AC2.3 (L62–L64). | Screenshot walkthrough of every screen incl. auth states. |
| E11 | **Accessibility services get no extra surface**: TalkBack speaks what is on screen, never a token (values side is `docs/53` AC2.4 L65). | Accessibility traversal. |
| E12 | **TLS is platform-default.** HTTPS only; no custom `TrustManager`, no certificate pinning bypass hooks, no cleartext permitted (add a network-security config with cleartext disabled; `targetSdk 34` default is already deny — *platform-unverified*). The jar never sends the cookie over plain HTTP (S5). | Code review + proxy attempt on http base URL must fail at startup. |
| E13 | **Backup artifacts**: the jar file is excluded by §1.3/S3; verify with a backup extraction attempt on a debug build (*platform-unverified* mechanics). | Backup probe when a debuggable test build exists. |
| E14 | **No token in WorkManager input/output data, notifications, or widget XML** for any background job, including the future freshness worker. | Worker review at implementation. |

## 4. Logout / identity change — verdict and required additions

### 4.1 Verdict

The proposed sequence — **stop in-flight requests → server logout with the current
jar → clear jar + access JWT → synchronously purge all account-derived
query/local/widget data before another identity renders; offline: clear locally and
disclose that server revocation is unconfirmed** — is the **correct order** and
matches the SPA's privacy outcome (`frontend/src/auth/AuthContext.tsx` L136–L154
removes the query cache on owner change; L128–L134 clears on failed boot) and the
QA bar (`docs/53` AC3.1 L72, AC3.4 L75).

Verdict: **conditionally sufficient**. It passes review only with the four additions
in §4.2; without them, two realistic races (refresh-during-logout,
late-response-after-purge) can render or persist data the sequence claims to
destroy.

### 4.2 Required additions

| # | Addition | Why |
| --- | --- | --- |
| L1 | **Refresh interlock (R4).** Logout holds the refresh lock and bumps the session generation; a refresh completing after the bump has its `Set-Cookie`/token discarded, never persisted. | Otherwise a refresh racing logout re-populates the just-cleared jar with a *live, rotated* cookie — a ghost session that survives both logout and the next identity. |
| L2 | **"Stop in-flight requests" must be concrete and awaited**: cancel the dispatcher's queued calls and running calls for the old session, await termination, and only then purge. Any response applied after the purge re-creates the old tenant's cache. | `docs/53` AC3.1 (L72) is judged by "zero of A's identifiers… appear in B's UI, in local storage inspection" — a late-arriving response fails it. |
| L3 | **Purge destroys key material, not just data**: delete the encrypted jar file **and** destroy/invalidate the Keystore data key for the outgoing identity, delete every identity-namespaced local store (DB, cache, snapshot), and push a signed-out state to any widget (`docs/51` L94). Purge is **idempotent and re-runs on boot**, because process death mid-logout must still leave no residue (AC3.4, `docs/53` L75). | Deleting ciphertext while the key survives leaves a recoverable session on disk; a purge that only runs in the happy path dies with the process. |
| L4 | **Identity change is logout.** Signing in as B while A's session exists must run the full §4 sequence for A first (server revoke attempt, then purge), then authenticate B. Never overwrite A's still-live cookie with B's login response in place — that leaves A's server session alive *and* previously satisfied the "cookie exists" check for the wrong tenant. | `docs/52` L52: "do not silently overwrite a still-live refresh cookie and retain any prior user content." Single jar (S4) makes overwriting the literal default behavior — the interlock makes it deliberate. |

### 4.3 Offline logout — verdict: disclosure is correct; do NOT persist for deferred revocation

Clearing locally and disclosing "server sign-out could not be confirmed" is the
**right** call and is sufficient as-is. The tempting alternative — queue the raw
token to replay `/auth/logout` when connectivity returns — is **rejected**: it
requires persisting the very credential the sequence exists to destroy (§1.4, S2/S4)
and creates a durable token artifact whose only purpose is a revocation nicety.
Bounded by rotation + 30-day expiry (`backend/app/core/config.py` L20) + reuse
detection, the unrevoked-but-locally-destroyed token is the smaller risk. The UI
disclosure must appear in this state (`docs/52` L60 calls this a design requirement,
not a server guarantee).

### 4.4 Server-side residuals the client cannot fix (must be disclosed in docs/UI copy, never promised away)

1. `POST /auth/logout` revokes **only the presented refresh-token row**, not the
   user's other sessions (`backend/app/api/auth.py` L434–L458) — per-device
   revocation does not exist (§5).
2. The access JWT is stateless: a copied access token remains usable until its
   15-minute expiry even after logout (`backend/app/core/security.py` L29–L38;
   `backend/app/api/deps.py` L137–L159; `backend/app/core/config.py` L19) —
   `docs/52` L31.
3. Offline, the client cannot learn of server-side revocation at all — same
   honesty rule as `docs/52` L96.

## 5. Explicit non-goals (scope-creep tripwires)

This policy authorizes **none** of the following. Each requires, in order: (a) a
**new Mark-approved `docs/NN` design spec** (the same approval gate `docs/52` L62–L63
and `docs/53` L153–L156 name), (b) an explicit **@security review** of that spec,
(c) **QA acceptance criteria** added to the `docs/53` lineage, and (d) ordinary
implementation review. Appearing in a diff or review claim without those, each is a
**finding, not a bonus**:

1. **A native refresh-token header/body protocol.** The cookie-only parser is the
   security boundary, deliberately (`backend/app/api/auth.py` L133–L151,
   L381–L385); the refresh endpoint takes no body at all.
2. **Client-wide read-only scopes / a read-only token type.** Android v1 stays
   read-only by client discipline, not by authorization (`docs/52` L61; the demo
   guard `backend/app/api/deps.py` L99–L120 covers the demo tenant only).
3. **Per-device session listing or revocation** — the "native session contract" of
   `docs/52` L63 is a proposal, not a feature; logout today is one row (§4.4.1).
4. **Any backend change** whatsoever — endpoint, schema, token semantics, cookie
   attributes.
5. **Sharing the v1 jar with the widget/background refresh** (§2 R7), and **any
   analytics/crash SDK** (`docs/53` L173–L174).

## 6. Verification / unverified items

**How this was verified (documentation-only change; no code, no production access,
no records touched):**

1. Re-fetched and pinned `origin/main` at
   `afa70cf8bc3c3be0aa9d1c52741c7d77685ea136` (`git rev-parse origin/main`; tip
   == remote). Every `file:line` citation above was read from that working tree,
   not copied from `docs/52`'s older snapshot `4f33e97a` — the cited ranges were
   re-verified line-by-line (auth cookie set/clear L83–L115; cookie-only parser
   L133–L151; `_issue_pair` L169–L185; atomic claim L194–L220; revoke-all
   L223–L232; login/demo/refresh/logout L285–L458; JWT mechanics
   `core/security.py` L29–L66; config L19–L20, L64; deps L137–L214 and demo guard
   L40–L120; SPA single-flight `client.ts` L91, L152–L174, L202–L248, L295–L351;
   `AuthContext.tsx` L128–L154; `mask.ts` L1–L22; second-factor/OIDC cookie paths;
   `AndroidManifest.xml` L4–L7; `build.gradle.kts` L8–L15, L27–L31). Where a
   range drifted from `docs/52`'s numbers, the current-tree numbers are used here.
2. Confirmed the `android/` tree at this tip is the toolchain skeleton only —
   13 tracked files, no auth, jar, Keystore, or network code; build outputs are
   gitignored (`android/.gitignore` L1–L7) and untracked.
3. Cross-checked `docs/51` (L59, L94, L104–L107), `docs/53` (AC2, AC3, AC6.5,
   non-goals L151–L176) and `docs/50` (L59, L83) so this policy never relaxes an
   existing boundary.

**Unverified / deliberately not claimed:** all Android platform behavior — Keystore
non-exportability and hardware-backing variance, `FLAG_DEBUGGABLE` derivation,
`allowBackup=false` semantics across API levels (incl. adb backup and D2D transfer),
WorkManager's process model, network-security-config defaults, and any specific
crypto/encdec library's guarantees. The persistent-jar library and exact Keystore
configuration remain **not selected** here, exactly as `docs/52` L59 leaves them;
selection is an implementation-time task to be verified against current Android
guidance and re-reviewed by @security before the auth implementation is accepted.
