# Android capability spike — interactive UX mockups

**What:** bounded spike deliverable (option 2) validating whether Android-native capabilities
justify a Kotlin/Compose client — *not* a full app build. Read-only; no production write path
exists in any frame; no platform commitment (docs/49's approval gate stands).

**Open `index.html` in a browser.** It carries the provenance-state legend, the three flow
links, and the capability scorecard (each candidate capability → which flow mocks it → the pass
condition it must meet to earn itself).

| File | Flows | Frames |
|---|---|---|
| `flow-reentry.html` | Capability 1 (+5): biometric/device-lock re-gate, cancel + session-expiry fallbacks, masked re-entry default, app-switcher privacy | r1–r7 |
| `flow-notification.html` | Capability 2 (+4): generic lock-screen notification → auth → revalidated deep link → alert/holding review; stale-link, expired-session, permission-denied fallbacks | n1–n8 |
| `flow-offline.html` | Capabilities 3 + 5 (+4): fresh → stale → offline snapshot → expired → cleared-on-logout, opt-out empty state, storage facts with revocation | o1–o9 |

## The rule this directory is built to enforce

**A number without provenance is a failed deliverable.** Every frame (`.scr`) must carry at
least one visible `[data-state]` marker — `fresh`, `stale`, `offline`, `expired`, `demo`,
`masked`, `auth`, `unavailable`, `permission`, `cleared`, … Freshness (server recency) and
tenant mode (demo read-only) are orthogonal; a demo screen can be fresh, stale or offline and
shows both.

```bash
node verify-states.mjs   # exit 0 = every frame honest
```

The verifier asserts, per frame: ≥1 provenance marker; rail/title/note/probe metadata present;
every `data-go`/`data-back` target resolves; any frame containing a currency string also carries
a fresh/stale/offline marker; and the cross-frame pins (expired frame `o4` = **0** currency
strings, fresh baseline `o1` = 4).

## Interaction

- **Step rail** (right panel) is generated from the DOM — screens and rail cannot drift.
- **System-back pill** (bottom of each frame) is Android back: walks `data-back`, and at a root
  shows an honest "would leave the app" toast instead of doing nothing.
- **Spike probe** panel per step: the concrete thing to measure or assert when running the
  capability test on a real device.
- Light/dark toggle; touch targets meet docs/09's 44 px floor; no horizontal overflow at 390 px.

## Provenance copy (the strings QA should assert verbatim)

- `Fresh · server 10:42 AM`
- `Stale · last server update 10:42 AM · 2h 27m ago`
- `Couldn't update. Showing data from 10:42 AM.`
- `Offline · snapshot saved Oct 26, 9:40 PM · read-only` (sticky) + per-card `Offline snapshot · saved …`
- `Snapshot expired … Values are hidden until you reconnect.`
- `Demo · read only`
- `Values hidden` / `Values shown · re-masks on background`
- `This item is no longer available`

## Status

Concept artifact only. Pixel-level styling is @designerbot's lane; flows/states are @uxbot's;
requirements/prioritization @pmbot; implementation @devbot (none authorized). Grounded in
`docs/48-android-companion-ux.md`, `docs/49-android-companion-recommendation.md`,
`design/android-material/visual-system.md`. All names, balances, times and alert types are
illustrative — alert types in particular stay illustrative until docs/49 Q2's alert-source audit.
