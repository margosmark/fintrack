# FinTracker Android capability spike — Material 3 visual direction

**Status: visual mockup only.** This is not an Android client, platform commitment, notification implementation, biometric security guarantee, or local-cache specification. Experience boundaries remain in [`docs/48-android-companion-ux.md`](../../../docs/48-android-companion-ux.md); platform sequencing remains in [`docs/49-android-companion-recommendation.md`](../../../docs/49-android-companion-recommendation.md). Mockup: [`index.html`](index.html).

## Surface and visual intent

**Monitor** is the primary surface: one glance at server-stamped state, followed by a careful read-only review. Use Material 3 tonal roles, a quiet warm light canvas, a near-neutral dark canvas, and a separate high-contrast mode. Geometry and typographic hierarchy do the work; no glass, glow, or stacked floating cards. Compose is a future rendering target, not a requirement implied by these files.

All data in the mockup is illustrative. Server-returned values, currency, source, and timestamps remain authoritative; no client totals or financial math are specified. Manual price provenance is explicit. Lock-screen notification copy is generic. Offline snapshot is conspicuously read-only and dated. Biometrics are shown only as a proposed local re-entry affordance, never as server authentication.

## Token contract

Values below are normative seeds for a Compose `ColorScheme` / type and shape setup. Mockup uses dp-equivalent CSS pixels at 1×. The high-contrast palette is deliberately monochrome-first; state always includes text/icon cues as well as color. All text/surface roles must be re-measured after real Compose component overlays and dynamic color decisions.

### Color roles

| Compose role | Light | Dark | High contrast (light) |
|---|---|---|---|
| `primary` | `#245DB4` | `#A5C4FF` | `#003A7A` |
| `onPrimary` | `#FFFFFF` | `#142D50` | `#FFFFFF` |
| `primaryContainer` | `#D8E5FF` | `#29364C` | `#D8E5FF` |
| `onPrimaryContainer` | `#102D55` | `#D8E5FF` | `#001B3D` |
| `secondary` | `#4F5F77` | `#B9C7DF` | `#303F55` |
| `onSecondary` | `#FFFFFF` | `#263347` | `#FFFFFF` |
| `secondaryContainer` | `#DCE4F1` | `#3A465A` | `#E1E8F2` |
| `onSecondaryContainer` | `#18283D` | `#DCE4F1` | `#111F33` |
| `tertiary` | `#146B4A` | `#73D6A9` | `#005333` |
| `onTertiary` | `#FFFFFF` | `#12382A` | `#FFFFFF` |
| `tertiaryContainer` | `#C5F0D9` | `#184D38` | `#C5F0D9` |
| `onTertiaryContainer` | `#002114` | `#C5F0D9` | `#00190F` |
| `error` | `#A8322A` | `#FFB4AB` | `#8C1715` |
| `onError` | `#FFFFFF` | `#5A100D` | `#FFFFFF` |
| `errorContainer` | `#FFDAD5` | `#7C2D28` | `#FFDAD5` |
| `onErrorContainer` | `#410006` | `#FFDAD5` | `#410006` |
| `background` | `#F6F4EF` | `#111318` | `#FFFFFF` |
| `onBackground` | `#171A20` | `#EDF0F5` | `#000000` |
| `surface` | `#FFFFFF` | `#1B1E25` | `#FFFFFF` |
| `onSurface` | `#171A20` | `#EDF0F5` | `#000000` |
| `surfaceVariant` | `#EEEEE9` | `#252932` | `#F0F0F0` |
| `onSurfaceVariant` | `#4B515A` | `#C1C6D0` | `#202124` |
| `outline` | `#6F757E` | `#8B919C` | `#3F444B` |
| `outlineVariant` | `#C5C7C7` | `#454A54` | `#767A80` |
| `inverseSurface` | `#2C3038` | `#E2E5EC` | `#202124` |
| `inverseOnSurface` | `#F1F0F5` | `#292D35` | `#FFFFFF` |
| `inversePrimary` | `#B2CAFF` | `#315FAD` | `#A9C5FF` |
| `scrim` | `#000000` | `#000000` | `#000000` |
| `positive` (custom) | `#146B4A` | `#73D6A9` | `#005333` |
| `negative` (custom) | `#A8322A` | `#FFB4AB` | `#8C1715` |
| `warning` (custom) | `#765100` | `#F2C66D` | `#624100` |
| `onWarning` (custom) | `#FFFFFF` | `#342600` | `#FFFFFF` |
| `offlineContainer` (custom) | `#E8E7E3` | `#34363A` | `#F0F0F0` |
| `onOfflineContainer` (custom) | `#25282D` | `#F2F2F2` | `#000000` |

Do not use `positive`/`negative` to convey meaning alone: pair with signed amount and “Gain”/“Loss”; warning includes icon and words. High contrast raises the outline and text hierarchy rather than relying on saturated fills.

### Type scale

Use platform Roboto via `FontFamily.Default`, tabular numerals (`FontFeature.tabularFigures()` / `fontFeatureSettings = "tnum"`) for all monetary and quantity values. Respect system font scaling; permit wrapping before truncation.

| Token | Size / line height | Weight | Use |
|---|---:|---:|---|
| `displaySmall` | 32 / 40 sp | 700 | Primary masked/unmasked summary |
| `headlineSmall` | 24 / 32 sp | 600 | Destination / detail heading |
| `titleLarge` | 20 / 28 sp | 600 | Top app bar title |
| `titleMedium` | 16 / 24 sp | 600 | Section and account title |
| `bodyLarge` | 16 / 24 sp | 400 | Primary explanatory copy |
| `bodyMedium` | 14 / 20 sp | 400 | Rows and supporting content |
| `labelLarge` | 14 / 20 sp | 600 | Buttons and selected nav label |
| `bodySmall` | 12 / 16 sp | 400 | Timestamp / source / secondary metadata; never sole financial context |
| `labelSmall` | 11 / 16 sp | 600 | Overline or compact state chip only |

### Spacing, shape, elevation

- Spacing steps: `4, 8, 12, 16, 20, 24, 32` dp. Screen horizontal inset `20dp`; section gap `24dp`; card internal inset `16dp`.
- Shape: small `8dp`; medium `12dp`; large `20dp`; extra-large `28dp`; full `50%`. Use medium for controls/chips, large for informational surfaces, extra-large for modal sheets only.
- Elevation: app canvas `0dp`; cards `0dp` tonal separation (outline only if needed); nav bar `1dp`; menus `3dp`; dialogs `6dp`; modal sheet `3dp` plus scrim. Avoid elevation stacks and shadow-as-boundary.
- Layout: list rows `64dp` minimum, account/detail row 56–64dp; target controls at least `48dp` square; never below FinTracker's 44px floor.

## Component inventory and visual behavior

- **Top app bar:** small, 64dp; back arrow on details, destination title, trailing icon buttons for mask/refresh. Status/system bars remain OS-owned and edge-to-edge insets are respected.
- **Summary card:** one primary server-supplied value with currency in the same line, visible “As of” timestamp, source/freshness badge. Mask text is a stable bullet pattern; accessible name says “Value hidden”, never the value.
- **Account / holding rows:** 64dp, text-first, title + type/source metadata, right-aligned tabular value and currency. Divider uses `outlineVariant`; no decorative icon tile.
- **Status banner:** one persistent compact tonal strip for offline/stale/demo/read-only states, with icon + explicit label; never encode status only by color.
- **Notification entry:** inbox item card includes event type, safe context and server time. OS notification is always generic (“FinTrack has an alert to review”); no personal financial values or identifiers.
- **Biometric prompt affordance:** a centered Material dialog with device-lock icon, brief local-only explanation, primary “Continue with device unlock” and text “Use sign-in instead”. This is a concept for re-locking an already valid session; do not conflate with login, identity recovery, or persistent encryption.
- **Bottom navigation:** four destinations—Home, Activity, Portfolio, More—with selected icon/container and label; no Add destination. Detail routes replace bottom navigation only if the UX spec expects full-screen drill-in. System Back closes modal first, then detail to list, preserving scroll position.
- **Offline snapshot:** monochrome offline banner, “Read-only · last updated [server timestamp]”; muted values remain visible only if opted into an approved private-cache capability. No refresh-success cue, actions, derived numbers, or notification resolution while offline.
- **Demo cue:** persistent `Demo · read only` container; no write affordance or success styling.
- **Loading/empty/error:** fixed label + neutral skeleton for loading; explanatory zero-data copy rather than `$0`; inline error preserves last timestamp and retry. Partial data failure scopes to one section.

## Motion notes

Use Compose/Material defaults as a ceiling, not a mandate: 100–180ms for selected-state/pressed feedback, 180–240ms for detail enter/return; small fade/vertical translation only, no parallax or chart draw-on. A dialog appears promptly with standard scale/fade and scrim. Loading skeleton may use a static tonal fill; no shimmer required. Respect `Settings.Global.ANIMATOR_DURATION_SCALE` / system animator scale and reduce/disable non-essential motion. Never delay content or mask state behind transitions.

## Accessibility and contrast

48dp touch targets, TalkBack labels and state descriptions, logical heading order, keyboard/switch focus indication, large-font reflow. Never announce hidden monetary values in masked state. Keep signed values and source/time available in visible context. Verify WCAG text 4.5:1, large text 3:1, and focus/boundary 3:1 on final rendered color overlays for all themes; this spec's tokens are seeds, not a substitute for runtime measurements.

## Compose seed

[`FinTrackColors.kt`](FinTrackColors.kt) is a compact Kotlin seed with the three explicit schemes plus custom semantic extension roles. It has no Android dependencies beyond Compose Material 3 and is illustrative—not a buildable client module.

## Verification

Open `index.html` locally. Use the toolbar to move through Home, Holding, Activity, Alerts, and secure-entry frames; switch Light / Dark / High contrast. The UI is static mock data and is not wired to APIs, notifications, biometric APIs, or device storage. Color ratios for the emitted theme text/surface pairs are reported in the task handoff with input values and measurement scope; the app's real component states require a later device-rendered contrast audit.
