# FinTracker Android companion — visual system

**Status:** product/design concept only; no Android implementation or platform selection. See [`android-companion-concept.html`](android-companion-concept.html) for interactive light/dark concept frames. All names, balances and dates in frames are illustrative.

## Direction

An Android **Monitor** surface: answer “what is my money doing?” quickly, then support careful review. It complements FinTracker’s dense responsive web/PWA rather than miniaturizing it. Use Android Material conventions—system status/navigation insets, top app bar, destination navigation, accessible touch targets, modal confirmation, device theme, screen-lock APIs as a possible future system affordance—without prescribing UI framework. Use Roboto/system sans as native default. Keep the established FinTracker signal-blue interactive identity, but translate it into Material tonal surfaces and restrained elevation, not web glass/glow.

## V1 visual patterns

| Area | Direction | Rationale / guardrail |
|---|---|---|
| Typography | Roboto/system sans; 12sp metadata, 14sp supporting/body, 16sp section title, 20sp app title, 30–32sp key balances. Tabular lining numerals for all currency and quantities. | Distinct readable hierarchy; no tiny data labels; allow Android font scaling without truncation.
| Color | Warm paper `#F6F4EF`, white `#FFFFFF`, ink `#171A20`, muted `#5D626C`, signal blue `#245DB4`, semantic green `#146B4A`, negative `#A8322A`, warning `#765100`. Dark: `#111318` background, `#1B1E25` surface, `#EDF0F5` ink, lighter blue `#A5C4FF`, accessible semantic equivalents. | Based on existing Ledger Daylight/Night direction, adapted to native tonal planes. Accent is for controls, never gain/loss. Color never sole signal: pair gains/losses with signed amount and labels. Measure final Android surface pairs against WCAG AA.
| Elevation | Flat background; cards use tonal surface + subtle 1–2dp-like shadow; menus/dialogs elevated with scrim. Avoid glass, neon, floating-card stacks. | Hierarchy is mostly spacing and typography; elevation identifies temporary/interactive layers.
| Navigation | Top app bar with back/menu and privacy action. Four compact bottom destinations: Home, Activity, Portfolio, More. System back/edge gesture returns through detail → list → home; preserve scroll position. Safe-area/system bars follow OS. | Three primary destinations are glance/review. Import, tax, reports, settings and advanced tools remain behind More or web, not crammed into a tab strip. Android system controls remain system-owned.
| Cards/lists | Net-worth total first, one compact trend, then account and recent activity rows; grouped white/tonal surfaces with dividers, 56–64dp rows; account identity at left, name + type, right-aligned tabular value. | No decorative icon tiles; leverage alignment for scanning. Row tap opens detail; no inline write controls in read-only concept.
| Financial data | Always name the metric, currency, and timestamp/provenance: “Market value CAD · Price as of Oct 24”; distinguish cached/stale from live/stream status; holdings detail separates quantity, book cost, last price, market value, and server-computed return. Signed transaction amounts use ± and semantic label. | No client-invented totals. FX, valuation, account balance, and tax values are server-owned; prices remain subject to manual-price precedence. Never imply current market price when showing a cached close.
| Masking | Explicit eye control, stable bullet mask on totals and row values, persists by user preference; auto-mask on app background and remove sensitive app-switcher preview. Reveal requires device credential/biometric confirmation if supported; state what reveal duration means. | Privacy must not rely only on obscured screenshot; do not announce value amounts in accessibility labels while masked. Device authentication is a future capability until threat model and storage/session behavior are approved.
| Loading | Preserve section labels and row layout; neutral tonal skeleton, no animated shimmer required. Do not render `0` or empty as loading. A refresh timestamp remains visible. | Stable layout lowers cognitive load and avoids false financial inference. Respect reduced motion.
| Empty | Plain language, explain where data comes from and the correct next step (“Accounts appear after you add them in FinTracker”; web handoff). | Do not fabricate balances or imply sync succeeded.
| Error/stale | Inline status with last successful timestamp, concise issue and retry where safe. Stale prices use text/icon + amber, never green “live”; offline shows cached data age and “read-only” label. | Read-only cache is only a later feature after secure cache/expiry design. Never queue writes offline.

## Server authority, demo, and trust

- Render server-returned amounts, currencies, `as of` times and status verbatim; clients may format, not calculate balances, FX, portfolio valuation, ACB or returns.
- Show a persistent “Demo · read only” cue on every demo surface. No enabled-looking write CTA; explain that edits are unavailable and provide a web route only when applicable.
- Respect tenant scope and existing privacy rules. Never blend local/offline/demo data with authenticated tenant data.
- Treat chart as supporting evidence: direct labels/time range, accessible summary, no reliance on hue; omit chart rather than imply missing history.
- Don’t imply biometric lock, notification delivery, offline functionality or import capture exist in v1 mockup; those are concepts only.

## Dark mode and accessibility

Follow Android system light/dark preference by default; offer explicit override only if product already has one. Preserve distinct light and dark identities from the current web tokens. Check normal text ≥4.5:1, large text ≥3:1, UI boundaries/focus ≥3:1. Use 48dp Android target guidance where practical (never below project’s 44px touch floor); provide visible focus, TalkBack names and logical heading order. Charts include a concise text summary and data labels. Respect display/font scaling, bold text, reduced motion and high-contrast settings; no clipped currency, dates or nav labels. Do not use color alone for stale, gain/loss, selected, or masked states. Modal reveal is announced and dismissible; system back closes dialog before navigating.

## Scope boundary

**V1 concept:** secure entry, Home glance, account list/detail, portfolio and holding detail, activity review, privacy mask/reveal affordance, demo cue, explicit loading/empty/error/stale states, system navigation and dark mode. Focus on reading; no offline write path.

**Later, validate value and threat model first:** biometric/device credential gating, encrypted last-known read-only cache with expiry and clear age, notifications for user-chosen actionable alerts, Android share/import capture, widgets/quick glance. Each needs explicit permissions/retention/lock-screen review.

**Keep web-first/web-only:** dense tables and bulk edits, import/reconciliation workflows, tax filing/reporting, advanced analytics/configuration and full administration. Mobile can deep-link to responsive web rather than reproduce all tools.

## Material rationale

Material guidance provides familiar Android edge-to-edge/system-bar behavior, clear top-level destination navigation, touch-sized actions, tonal elevation, readable type scale, and OS-consistent dialogs. The concept uses those patterns for predictable operation while preserving FinTracker’s established semantic color rule (brand accent is not financial meaning). It intentionally avoids a brand-new ornamental visual language and avoids claiming that a concept mock grants native security properties.

## Verification notes

The HTML concept includes three 365×740-ish Android device frames: home, holding detail, and combined mask/reveal plus loading/empty/error examples. The header toggle switches the entire board between light and dark themes. Validate implementation contrast and Material component measurements against the eventual supported Android version; this artifact is a visual direction, not a tested application.
