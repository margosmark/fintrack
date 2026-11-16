# 53 — Android v1 read-only companion: acceptance criteria

**Status: the acceptance bar, written before implementation exists.** Mark
approved building the Android app, then the home-screen widget (Decision in
[`docs/51`](51-android-home-screen-widgets.md), 2026-10-29). This document is
the measuring stick implementation is judged against — written now so the bar
cannot be retrofitted to whatever gets built. It is owned by the QA lane.

**Precedence.** These criteria operationalize, and never relax, the boundaries
in [`docs/48`](48-android-companion-ux.md),
[`docs/49`](49-android-companion-recommendation.md) and
[`docs/51`](51-android-home-screen-widgets.md). Where this document and those
three differ, **the stricter boundary governs**. This doc adds no scope, no
authorization, and no new capability: it authorizes no production access,
production record, deployment, signing key, store account, or server contract
change.

## How to use this document

- Each criterion is `AC<n>`. It is **pass / fail**, judged on evidence (§
  Evidence format), not on a implementer's self-report.
- The v1 app (AC1–AC5, AC7) is judged when an implementation candidate exists.
  AC6 (widget) is dormant until Mark opens a widget implementation epic past
  the W-0 feasibility gate in `docs/51`; it does not become true by being
  written here.
- Probes run against a **local/dev stack with synthetic data only** — demo
  tenant plus at least one real test identity. No production access, no
  production records, ever.

## AC1 — Financial truth

Every displayed number is a value the server returned in a response the client
actually received. No local balance or price computation, no optimistic writes,
no cached value presented as current.

| # | Pass condition |
|---|---|
| AC1.1 | Every displayed amount traces to a field of a server response body. Interception test: mutate one server field in transit; the UI must show the mutated value — proving no client-side recomputation overrides it. |
| | Totals are server-provided and denominated; mixed-currency values are listed per currency and **omitted** — never summed — when the server supplies no single-currency total. |
| AC1.2 | No mutation affordance exists anywhere in v1. No create/edit/delete screen, no optimistic local state change, no write request issued by the client other than none (the only permitted non-GET traffic is auth/session bookkeeping already owned by the server). Verified by proxy/log: zero POST/PUT/PATCH/DELETE to domain routes during a full walkthrough of every v1 screen. |
| AC1.3 | The five negative states render **distinctly** — not one shared error screen, not a collapse to `$0`: |

| State | Required rendering |
|---|---|
| Loading (first load) | Skeleton/progress; no figures of any kind, not even stale ones. |
| Refreshing over last-known data | Previous figures stay visible **with their original timestamp**; refresh failure says "Couldn't update · showing data from ⟨time⟩" + Retry. |
| Stale | Value shown with the server's staleness signal and as-of time; never styled as current. |
| Offline | Explicit offline state; last-known data only if the offline snapshot feature exists (it does not in v1 — v1 shows an offline state, no cached finance figures). |
| Unavailable / server error | Section identified as unavailable; other sections keep working data; never collapsed to zero. |
| No data | "Add/manage accounts on web" style honest empty state; never a fabricated zero portfolio. |
| Locked (masked) | `•••••` placeholders, mask state obvious via label + icon, not color alone. |
| Session expired | Calm sign-in screen; the intended destination — a **filled** internal route (`account/5`, never the `account/{accountId}` pattern) — is preserved for **that same identity only** and revalidated after re-auth through the deep-link existence/tenant gate; an identity change at commit clears it, so another identity can never consume it. |
| Permission / privacy-mode withheld | Explain withheld data **in place**; never claim "no holdings" merely because withheld rows were not returned. |

**Destination-preservation lifecycle** (one coherent contract; pins AC1.3's
session-expired row, AC3.1 and AC3.2):

1. **Note** — on expiry, the client preserves the **filled** route
   (`account/5`: route pattern + resolved argument values at the moment of
   expiry, never the pattern alone). An argument that is missing or does not
   match `[A-Za-z0-9._-]+` is never preserved (a value must not be able to
   compose a different route). The preserved value is an internal same-app
   route only — never a scheme/authority (no open redirect).
2. **Consume** — on re-auth the client returns to the filled destination,
   refetches its data fresh, and the id destination renders **only after** the
   server's own verdict for the current identity: a preserved deep-link
   destination is consumed **through** the existence/tenant gate (AC3.2), and
   a detail screen shows its route-derived id (e.g. the `Account 5` subtitle)
   only once that row has been confirmed — never from the route alone.
   A gate verdict is scoped to the session that consumed it: when that
   session ends, the verdict is invalidated, and a **same-identity** re-auth
   whose preserved destination is the launch link's own route re-runs the
   existence/tenant gate (fresh verdict, fresh fetch) rather than swallowing
   the entry — the link-launched expiry interleaving (QA R3-1). A different
   identity's commit never re-validates (or re-fetches) the old identity's
   link target, and an explicit sign-out — which drops the destination —
   never resurrects the link's navigation on the next login.
3. **Clear** — `pendingDestination` is cleared on explicit logout and,
   **at identity-change commit**, before any of the new identity's data can
   render: an identity must never consume or render a route preserved by a
   different identity.

## AC2 — Privacy and masking

Display masking (`docs/46`, M139) is a **render-time presentation** fact, not
encryption or access control — the Android client must not claim otherwise.

| # | Pass condition |
|---|---|
| AC2.1 | Masking covers **every** displayed absolute monetary value **and** every place a value leaks: chart axis labels, chart tooltips/value labels, accessibility (TalkBack) content descriptions, notification content, app-switcher snapshot, launcher/widget preview. Percentages stay unmasked per `docs/46` D2. |
| AC2.2 | Mask state is session-scoped with **restore-on-boot OFF**: cold start is always the tenant default (masked; demo boots masked), a user reveal is never resurrected across a relaunch, and a silent same-identity session drop keeps the user's own in-session choice. |
| AC2.3 | Backgrounding, device lock, logout, identity change and session expiry **re-mask immediately** — including OS surfaces: the app-switcher snapshot and any notification must show the masked/generic state, not the last on-screen frame. |
| AC2.4 | TalkBack/accessibility traversal over a masked screen speaks no unmasked amount; over an unmasked screen it speaks what is on screen — nothing more (no hidden row values, no cached off-screen items). |
| AC2.5 | Screenshot and screen-recording of any v1 screen reflects the same mask state as the glass. |

## AC3 — Tenant safety

| # | Pass condition |
|---|---|
| AC3.1 | On logout, expiry, or identity change, all local private data (cache, databases, prefs holding tenant data, in-memory models) is purged **before** any new identity's data can render. Verified by: sign in as A → browse → log out → sign in as B; zero of A's identifiers, names, or amounts appear in B's UI, in local storage inspection, or in any B-screen network response. |
| AC3.2 | Deep links (notification, external, stale) resolve through authentication + server authorization + existence check **before** rendering. This ordering binds **every** entry path: cold-start link composition, and re-auth consumption of a preserved destination — while the existence/tenant gate is in flight, the id destination must not render; the gate's verdict decides the outcome. Deleted, foreign-tenant and stale targets all land on one **generic** unavailable state that does not reveal whether the foreign record exists. |
| AC3.3 | Android Back from a deep-linked destination cannot land on another identity's screen under any interleaving of expiry + re-auth + Back. |
| AC3.4 | Process death (dev options → "Don't keep activities", and `adb shell am kill`) between any two steps of AC3.1 leaves no tenant residue on next launch. |

## AC4 — Freshness honesty

| # | Pass condition |
|---|---|
| AC4.1 | Any timestamp shown is `server_checked_at` (or the server field it is explicitly labelled as) from a real received response. The client never computes a displayed time from render time, clock math, or a scheduled-job time. |
| AC4.2 | A delivered-but-unfetched timestamp (e.g. the `Price.created_at` upsert row — provenance work, `t_222ecc24`/`t_90bd955c`) is **omitted**, never labelled "fetched", "checked", or "updated". Market-date age and provider-fetch age stay distinct. |
| AC4.3 | Manual price override renders as the displayed source with its as-of date and is never silently replaced by a backfill. |
| AC4.4 | Forbidden words anywhere in v1 UI or widget copy: "live", "current", "real time", "continuous", or any exact next-refresh promise — absent an explicit server attestation of real-time delivery, which does not exist. |

## AC5 — Demo read-only

| # | Pass condition |
|---|---|
| AC5.1 | Demo tenant boots with an unmistakable, persistent "Demo · read-only" marker in the shell and any drill-in header. |
| AC5.2 | The demo path contains no write affordance. If a write-shaped control is ever shown for explanatory context, the read-only state is stated before the action and the server's `demo_account_restricted` (403) truth is preserved — the client never shows a local success state. |
| AC5.3 | AC1.2's zero-write network log holds on the demo tenant too. |

## AC6 — Widget (dormant until its epic is opened)

Applies only after Mark opens a widget implementation epic past W-0 (`docs/51`).
Not pre-authorized by building the app.

| # | Pass condition |
|---|---|
| AC6.1 | Values are hidden unless the **per-widget visibility toggle** is on; default on install and after every identity change is **hidden**; the toggle is per widget, not global, and its settings surface exposes no account names or raw tenant identifiers. |
| AC6.2 | Switching the toggle **off** applies immediately across launcher preview, lock screen, recents/app-switcher, screenshots and notification shade — not on the next render, not after the next refresh. |
| AC6.3 | Re-lock is **unconditional** after logout, session expiry, account switch or tenant change, regardless of toggle state; the widget never renders another identity's cached content. |
| AC6.4 | Freshness copy is *last checked ⟨server time⟩* — computed from the successful server response, never from widget render time; a delayed/skipped OS refresh never makes an old value look newly checked. |
| AC6.5 | Widget traffic uses a designed, revocable, least-privilege session path; no passwords, TOTP secrets, recovery codes, raw refresh cookies or long-lived bearer tokens in SharedPreferences, widget XML, launcher state, notifications, logs or backups. |
| AC6.6 | Widget content is a bounded allowlist from the `WidgetState`-shaped contract in `docs/51`; no client-computed totals/prices; demo widgets keep the demo marker and read-only stance. |

## AC7 — Build gates (local only)

GitHub Actions billing is deliberately ignored; **CI is not evidence either
way**. On the exact SHA under review, locally:

| # | Gate |
|---|---|
| AC7.1 | `./gradlew test` — all unit tests pass, zero failures. |
| AC7.2 | `./gradlew lint` — no lint errors (warnings enumerated). |
| AC7.3 | `./gradlew assembleDebug` (and `assembleRelease` if a release build exists at review time) — clean build. |
| AC7.4 | Evidence carries the **full 40-hex SHA** plus a clean-tree proof (`git status --porcelain` empty). If the branch is pushed, the SHA is cross-checked against `git ls-remote origin <branch>` — a worktree SHA that is not the pushed tip is not the SHA under review. |

## Evidence format

A self-report is not a verdict. Every claimed pass attaches, per criterion:

```text
AC<n> — <claim>
SHA: <full 40-hex>          # git rev-parse HEAD
Tree: clean                 # git status --porcelain → empty output
Remote tip: <40-hex or "local-only branch">   # git ls-remote origin <branch>
Device/emulator: <model>, Android <API level>, build <debug|release>
Command(s): <verbatim shell lines>
Output:  <verbatim output tail, including counts — e.g. "BUILD SUCCESSFUL",
          "N tests, 0 failed">  — failing output attached as-is, never redacted
Probe evidence: screenshots/video for OS-surface criteria (AC2.3, AC2.5,
          AC6.1–6.3), captured on the same SHA; network logs for AC1.2/AC5.3
```

Rules QA applies when judging:

1. QA re-runs the AC7 gates itself on the pinned SHA and independently executes
   the behavioural probes — implementer evidence is corroboration, not the
   verdict.
2. Missing output, hand-trimmed output, mismatched SHA, or a dirty tree =
   **unverified**, which fails the criterion.
3. Any criterion whose evidence shows a failure elsewhere fails on its own
   terms; a green summary over a red detail is treated as a finding against the
   submitter, not the run.
4. Device-surface claims (launcher, lock screen, recents, shade) require
   screenshots on a named device/API level; a layout description is not
   evidence.

## Non-goals (scope-creep tripwires)

Out of scope for v1 and the widget epic unless a **new** Mark-approved doc says
otherwise. Each item below, if it appears in a diff or a review claim, is a
finding, not a bonus:

- iOS, cross-platform shells (Capacitor/RN/Flutter), or any second platform.
- Any production access, production record, deployment, feature-flag
  activation, signing key, Play Console account, or store submission.
- Mutations of any kind: ledger editing, categorization, splits, transfers,
  import batch commit/discard/revert, trades, dividend capture, manual price
  edits, account/institution management, rules, budgets, reconciliation, tax
  or ACB work, exports. These remain web-first/web-only per `docs/49`.
- Client-side balance/price/return computation of any kind, including
  "harmless" summing, currency conversion, or cached-snapshot expiry math that
  is presented as financial truth.
- Offline read-only snapshot (still evidence-gated in `docs/49`); biometric
  re-lock (evidence-gated); share/import capture (gated); push notification
  delivery beyond what existing alert sources already justify.
- A new alert/event system, a new server contract change, or any weakening of
  server-owned tenant isolation, privacy mode, E2EE degradation behavior, or
  masking semantics — each needs its own approved doc first.
- Analytics, crash-reporting or any third-party SDK added without a separate
  privacy decision.
- Web-product changes: this is a companion; the responsive/PWA product stays
  the product of record and is not modified as a side effect.

## Verification

Documentation-only change, executed on the QA scratch workspace:

1. Read `docs/48-android-companion-ux.md`, `docs/49-android-companion-recommendation.md`,
   `docs/50-android-native-capability-spike.md` and
   `docs/51-android-home-screen-widgets.md` from `origin/main` at
   `4f33e97a9e83c36873a466b2c49a2e108d7a900e` (tip confirmed == remote via
   `git ls-remote origin refs/heads/main`) and carried their boundaries
   forward verbatim — masking scope, tenant purge ordering, generic
   unavailable deep-link state, demo read-only, widget visibility-toggle and
   *last checked* requirements.
2. Confirmed `docs/52-*` does not exist at that tip and no `docs/53-*` exists
   there; 53 is the next free number (note: `docs/48` is double-numbered —
   `48-android-companion-ux.md` and `48-cve-2026-111497-build-base-pinning.md`
   — hence the explicit filenames used above).
3. Grounded AC4.2 in the shipped provenance findings (`t_222ecc24` FAIL,
   `t_90bd955c` PASS): `created_at` is an upsert row, not fetch evidence.
4. This change adds no code, no endpoint, no Android scaffold, and no
   production contact of any kind.
