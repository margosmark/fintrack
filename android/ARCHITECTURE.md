# FinTracker Android — architecture

Shell card: `t_166f2243`. This document describes the module layout the shell
establishes and where later cards (auth, screens) plug in.

## Module layout

Single Gradle module, `:app`, layered by package:

```
android/app/src/main/kotlin/com/margosmark/fintracker/
├── MainActivity.kt              # single activity; wires theme + app root
├── ui/theme/                    # Material 3 theme from design tokens
│   ├── Color.kt                 #   ALL color tokens (light+dark) — edit here only
│   ├── Type.kt                  #   type scale (12/14/16/20/32sp, tnum for currency)
│   └── Theme.kt                 #   FinTrackerTheme + Light/DarkColorScheme
├── navigation/
│   ├── Routes.kt                # route table (the contract: app ↔ tests ↔ deep links)
│   ├── DeepLinks.kt             # parse + GATE (auth/extant/tenant) — pure JVM, fully tested
│   └── FinTrackerApp.kt         # NavHost; folds deep-link resolution into startDestination
└── state/
    ├── ScreenState.kt           # the 7 truthful states (+ Ready) as a sealed hierarchy
    ├── ServerCheckedAt.kt       # the ONE server_checked_at formatter
    ├── StatePane.kt             # renders each state distinctly (icon+heading+tint+tag)
    └── MaskedText.kt            # bullet mask for values AND labels
```

Tests live in `android/app/src/test/kotlin/com/margosmark/fintracker/` and run
on the JVM via Robolectric (`@Config(sdk = [34])`) — no emulator exists on this
host and none is required, **including QA screenshots** (see
[QA screenshots](#qa-screenshots) below).

## QA screenshots

A QA test class CAN produce real PNGs of a screen on this host, no emulator:
`screens/QaScreenshotSupport.kt::qaScreenshot` software-draws the app's own
ComposeView into an ARGB bitmap and compresses it to PNG (`QA_SHOT_DIR` env
selects the output dir, default `/tmp/qa-shots`). Two conditions make it work:

1. **`@GraphicsMode(GraphicsMode.Mode.NATIVE)` on the test class** (plus the
   usual `@Config(sdk = [34], qualifiers = "w320dp")`). Robolectric 4.14
   defaults to LEGACY graphics mode, where `Bitmap.createBitmap` bottoms out in
   a null native bitmap and compose's vector `DrawCache` dies with
   `NullPointerException … "bm" is null` on the first icon; under NATIVE the
   robolectric nativeruntime provides real Skia bitmaps and the same draw
   succeeds.
2. **`captureToImage()` is NOT a viable path here** — it needs a window
   compositor Robolectric does not have and fails with
   `ComposeTimeoutException` at `WindowCapture.forceRedraw`. The helper tries
   it only as a fallback and records the failure instead of throwing.

Reference implementation: `screens/QaScreenshotTest.kt` (320dp Glance,
synthetic fixtures, masked/unmasked/revealed states) — it fails its own
assertions unless the written file is a real multi-colour PNG with valid magic
bytes. A screenshot helper must never let a capture failure mask a behavioural
assertion: record it, assert on semantics/text, and gate the shot separately.

## Where things plug in

### Auth + session client (t_6eddd208, owns `data/` + `net/`)

The shell never parses tokens. Two seams:

1. **`SessionSnapshot`** (`navigation/DeepLinks.kt`) — the only session view the
   shell needs (`isAuthenticated`, `tenantId`). The auth card maps its real
   session into this and passes it to `FinTrackerApp(session = …)` from
   `MainActivity`. `MainActivity` currently hard-codes
   `SessionSnapshot.UNAUTHENTICATED` with a comment marking the swap point.
2. **`targetExists: (DeepLinkTarget) -> Boolean`** — the deep-link extant+
   tenant check. The auth/data layer MUST scope this lookup to the current
   session's tenant, so a foreign id answers `false` exactly like a missing
   one (a foreign target can never resolve — that is the isolation rule).
   The shell currently passes `{ false }` (no data layer ⇒ nothing is extant),
   which makes every id-carrying deep link truthfully land on the unavailable
   destination.

Dependency additions (Retrofit/OkHttp/etc.) go through `app/build.gradle.kts`
by comment request — that file is owned by the shell card while it runs.

### Screens

`FinTrackerApp`'s `composable(...)` blocks currently render a tagged
placeholder (`Destination(ScreenTags.X, label)`). A screen card replaces the
placeholder body with the real screen, keeps the route and the `ScreenTags`
value so the nav tests keep their teeth, and renders its data through
`StatePane(state, content)` — every screen MUST surface loading/no-data/
unavailable through those primitives rather than bespoke empty views.

### Widget (later, docs/51)

The widget reads the same server-owned data through Glance; nothing in this
shell is widget-facing yet.

## Hard rules encoded here (docs/48/49/51/53)

- **No client-side computation.** Screens use server values, without client-side totals or conversion. The
  presentation layer formats decimal strings for display only — Home via
  `HomeNumberFormat`, account detail via `AccountValueFormat` (docs/52 §3):
  locale grouping, two decimal places for amounts, two to four for prices,
  HALF_UP rounding; nonnumeric inputs and nonzero values that would round to
  zero pass through. Currency labels remain server-provided, and the mask
  applies to formatted values.
- **Timestamp truthfulness.** The only timestamp renderer is
  `formatServerCheckedAt(instant)`; `ScreenState` types carry
  `server_checked_at` from responses and cannot be constructed with a local
  clock reading. `Loading`/`Unavailable` render NO timestamp when the state
  has none (tests pin the omission).
- **Masking covers labels as well as values** — `MaskedText` is the single
  primitive; masked branch never composes the raw string into semantics
  (test walks the semantics tree for digit leakage).
- **Deep links resolve only to authenticated, extant, same-tenant targets**;
  everything else renders the generic `unavailable` destination. Gate logic is
  pure (`resolveDeepLink`) and separately tested from the UI wiring.
- **Debug signing only**; no keystores, no credentials in the repo.

## Build & test

```bash
android/scripts/verify_toolchain.sh          # exit 0 = toolchain ready
cd android && ./gradlew testDebugUnitTest    # JVM unit tests (Robolectric)
cd android && ./gradlew assembleDebug        # app/build/outputs/apk/debug/
# QA screenshots (see §QA screenshots): PNGs land in $QA_SHOT_DIR
QA_SHOT_DIR=/tmp/qa-shots ./gradlew :app:testDebugUnitTest \
    --tests 'com.margosmark.fintracker.screens.QaScreenshotTest'
```

Robolectric downloads its `android-all` instrumented jar from Maven on first
run; after that the suite is offline-capable and takes seconds.
