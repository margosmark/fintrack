# 56 — Android Account Detail: interaction pass (holdings nav, ledger clamp+expand, Show N more, Frame A de-dup)

**Status: implemented** (kanban `t_b236fcb8`, docs/53 AC7 gates green — see §4).
Source of the rulings: uxbot's Account Detail review + mockup
`design/android-material/android-companion-concept.html`, confirmed by PM as five
scope items (t_b236fcb8). This doc is the implementation contract for the screen
touched by that pass.

## 1 Context

Before this pass the Android Account Detail screen was read-only but inert:
`DetailRow(label = "Balance", …)` duplicated the hero figure, the mask control
lived in the scrolling header, the `TopAppBar` was a static "FinTracker" with no
back or privacy action, and **zero** `clickable` modifiers existed in
`AccountDetailScreen.kt` — every row was a dead end. The ledger footer was the
copy `"Showing N of M · full history on the web"` (a web hand-off, forbidden in
the mobile contract).

Five PM rulings define the pass; they are pinned in §2. Non-rulings that look
similar are explicitly **out of scope**: `typeLabel` keeps its
`else -> type` passthrough (no new account-type labels), and no
transaction-detail route is created (`Routes.kt` still has only `TRANSACTIONS`;
docs/52 confirms there is no `GET /transactions/{id}` handler to deep-link to).

## 2 Approach (exact shapes)

### 2.1 Holdings row → `Routes.HOLDING_DETAIL` (navigation only)

`AccountHoldingsContent` gives every `AccountPositionRow` a whole-row
`onClick = { onOpenHolding(row.accountId, row.securityId) }`, wired at the call
site as `navController.navigate(Routes.holdingDetail(...))` — the identical
shape `GlanceScreen.PositionRow` uses. The screen exposes
`onOpenHolding: (accountId: Int, securityId: Int) -> Unit`. No demo
write-boundary contact: navigation only.

- Row tap target: test tag `AccountDetailTags.holdingRow(securityId)` =
  `account_holding_row_<securityId>`; row height ≥ 64dp at 390px.
- Section wrapper tag: `AccountDetailTags.HOLDINGS` = `account_detail_holdings`.
- The positions route fires only for `INVESTMENT_ACCOUNT_TYPES`
  (`brokerage_non_registered, rrsp, tfsa, fhsa, resp, lira, rdsp` — client
  mirror of `backend/app/models/accounts.py`) so a cash account never triggers
  the market-data refresh fan-out of `GET /portfolio/positions`.

### 2.2 Ledger row: 2-line clamp + tap = IN-PLACE expand (never navigation)

One `Row` per ledger entry — date │ description │ money rail — is a single tap
target: tag `AccountDetailTags.ledgerRow(transactionId)` =
`account_ledger_row_<transactionId>`, `clickable` with the TalkBack label pair

- collapsed → `"show full description"`
- expanded → `"collapse description"`

State is `remember(row.transactionId) { mutableStateOf(false) }` (per row,
resets when the row identity changes). The description
(`AccountDetailTags.ledgerDescription(transactionId)` =
`account_ledger_description_<transactionId>`) is a single `Text`:

```kotlin
maxLines = if (expanded) Int.MAX_VALUE else 2,
overflow = TextOverflow.Ellipsis,
```

Contract details:

- The clamp is **visual only**: the semantics tree always carries the full
  description, in both states.
- Tap toggles state in place; the nav route never changes (asserted:
  `navController.currentDestination.route == Routes.ACCOUNT_DETAIL` across
  both toggles). Second tap collapses back.
- No transaction-detail route is invented (ruling; docs/52 §transaction list).

### 2.3 Running balance stays on the right rail, as mocked

Beneath the amount, each ledger row that has a server figure renders:

```
balance <running_balance>       // "balance " prefix, labelSmall, onSurfaceVariant
```

- Style: `labelSmall` (12sp) muted with `fontFeatureSettings = "tnum"`
  (tabular numerals) — `MaterialTheme.typography.labelSmall` merged with
  `TextStyle(color = onSurfaceVariant, fontFeatureSettings = "tnum")`.
- **Currency prints once per row** — the rail line passes `currency = null` to
  `AmountText`; only the amount above it carries the currency suffix.
- The value goes through `MaskedText`/`AmountText(masked = masked)` like every
  other figure: masked when the session mask is on, revealed after the app-bar
  eye. It is **server-computed** (`running_balance` from
  `GET /accounts/{id}/ledger`, docs/47) and **not** behind a tap.
- The line is **absent** (not guessed) when `runningBalance == null`. That
  occurs for rows paged in via §2.4: `GET /transactions` has no
  `running_balance`, and the rulings allow no new API — a running balance is
  never invented client-side (docs/53 AC1.3).

Tags: `ledger_amount`, `ledger_balance`, ledger summary line `ledger_current`.

### 2.4 Ledger paging — "Show N more" (replaces the dead-end footer)

The dead-end copy `"Showing N of M · full history on the web"` is gone;
`ForbiddenCopyTest`/`showMorePagesThroughTransactionsRouteAndDropsDeadEndCopy`
pin its absence.

- Initial window: `LEDGER_PAGE_SIZE = 20` rows of the ledger page
  (`GET /accounts/{id}/ledger`), `initialWindow = min(20, items.size)`.
- While `shown < page.total`, render one `TextButton`
  (`AccountDetailTags.SHOW_MORE` = `account_ledger_show_more`) whose label is
  `"Show $nextLimit more"` with `nextLimit = min(20, page.total - shown)`.
  The button is `heightIn(min = 48.dp)` — explicit floor, because the shared
  `sm` button token (28dp) would break the 44px contract (docs/09).
- Fetch: **existing API only** — `SectionSources.accountTransactions(accountId,
  limit, offset)` → `FinanceRepository.transactions(limit, offset, accountId)` →
  `GET /transactions?limit=&offset=&account_id=` (committed rows, server
  ordering `date desc, id desc`).
- Landing a window: map DTO → `LedgerRowDisplay` using
  `tx.payeeOrDescription` (the same normalized-payee-else-description rule the
  server uses for the ledger line), `.reversed()` so the newest-first window
  appends in the ledger's ascending order, then
  `distinctBy { transactionId }` de-dup. Empty answer → `noMoreFromServer`
  (button disappears — it never lies about having more).
- In-flight: button `enabled = !loadingMore`. Failure states keep every landed
  row, show `"Couldn't load more entries."` (labelSmall, muted) and leave the
  button armed for retry of the same window.
- Appended rows carry `runningBalance = ledgerLines[tx.id]?.runningBalance`
  from the ORIGINAL ledger page only (see §2.3).

### 2.5 Frame A de-dup (hero owns the total)

`AccountBalanceHero` renders `total_value` once at `displaySmall` (32sp, tnum)
with tag `account_balance_hero` / `account_balance_hero_value`, and
`AccountDetailFields` renders **no `Balance` row at all** — the de-dup ruling
is structural:

- `Cash balance` row renders only when `cashBalance != null &&
  !sameFigure(cashBalance, totalValue)` (a cash account whose cash equals the
  hero would print the same figure twice).
- `Investment value` row renders when `type in INVESTMENT_ACCOUNT_TYPES ||
  !isZeroFigure(investmentValue)` — dropped only for cash accounts at 0.00;
  investment accounts keep the zero line (honest composition).
- `Starting balance` (and `Type`, `Currency`, `Last sync`, tax treatment for
  `brokerage_non_registered`) are unchanged: they are the composition the hero
  is made of.

### 2.6 App-bar actions: back + privacy eye (48dp each)

`FinTrackerApp`'s `Scaffold.TopAppBar` gains two icon actions, both sized
**48×48dp explicitly** (the shared `sm` token is 28dp — forbidden here):

- Back: `navigationIcon` present **only** on `Routes.ACCOUNT_DETAIL` and
  `Routes.HOLDING_DETAIL`; `Icon(Icons.AutoMirrored.Filled.ArrowBack)`,
  `contentDescription = "Back"`, `popBackStack()`, test tag
  `AppBarTags.BACK` = `app_bar_back`. Root tabs show no back icon.
- Privacy: `AppBarMaskToggle` (icon-only eye) in `actions` for every route,
  tag `SectionTags.MASK_TOGGLE`, semantics label from
  `maskActionDescription(masked)` ("Hide values"/"Show values" family — same
  wording as the scrolling-header control).
- On Account Detail the scrolling header suppresses its own control
  (`ScreenHeader(…, showMask = false)`) — exactly one mask toggle per screen,
  and it can never scroll away.

`typeLabel` was NOT changed (no `chequing/savings/credit_card/line_of_credit`
branches) — the reviewed passthrough stands.

## 3 Touch points

| File | Change |
|---|---|
| `android/.../ui/screens/AccountDetailScreen.kt` | Screen rewrite: §2.1–2.5, `AccountDetailTags`, `LEDGER_PAGE_SIZE`, `landMoreRows`, Frame A fields |
| `android/.../navigation/FinTrackerApp.kt` | §2.6 back/eye wiring, `AppBarTags.BACK`, `onOpenHolding` + `ledgerMoreSource` params |
| `android/.../ui/screens/ScreenChrome.kt` | `ScreenHeader(showMask:)`, `maskActionDescription`, `AppBarMaskToggle` (48dp) |
| `android/.../ui/AppScope.kt` | `SectionSources.accountTransactions(accountId, limit, offset)` + `ServerBackedSections` impl |
| `android/.../test/UiTestSupport.kt`, `ServerTestSupport.kt` | test `SectionSources` implementations for the new member |
| `android/.../test/screens/AccountDetailInteractionTest.kt` | behaviour suite for §2 (9 tests) |

Backend: **no changes** — §2.4 reuses `GET /transactions`, §2.3 reuses
`GET /accounts/{id}/ledger`.

## 4 Verification

Local gates only (docs/53 AC7 — Actions billing ignored per standing rule), on
this branch's worktree:

| Gate | Command | Result |
|---|---|---|
| AC7.1 | `cd android && ./gradlew test` | **243 unit tests, 0 failures** (debug+release variants, 35 suites in debug) |
| AC7.2 | `cd android && ./gradlew lint` | **0 errors**; warnings enumerated: 7 pre-existing (`IntentFilterUniqueDataAttributes` ×2, `GradleDependency` ×4, `MissingApplicationIcon`) — all in manifest/`build.gradle.kts`, none in Kotlin this pass touched |
| AC7.3 | `cd android && ./gradlew assembleDebug` | clean build |
| Focused | `./gradlew testDebugUnitTest --tests "…AccountDetailInteractionTest"` | **9/9 pass** |

`AccountDetailInteractionTest` pins the five behaviours: holdings whole-row
navigation to `HOLDING_DETAIL`; ledger tap = label/state toggling IN PLACE
with the route pinned to `ACCOUNT_DETAIL` and full text preserved across
toggle; running balance on the right rail under the amount with `CAD` exactly
once per row; `Show 20 more` → `Show 5 more` paging through
`GET /transactions` with the dead-end copy absent; Frame A de-dup (hero-only
total, `Investment value` kept for investment accounts / dropped at 0.00 for
cash); tap floors ≥ 44dp at both 390px and 360px; the 48dp app-bar eye/back.

**Environment note — why the clamp is pinned at the source seam, not in
pixels:** Robolectric's font in this suite **never wraps**: a bare
`Text(5400 chars, bodyMedium)` in a 315dp box lays out **36dp** (one
unwrapped line box), so clamped and expanded heights are identical here by
construction and a pixel-growth assertion cannot be green on any string.
The test therefore pins what the environment can prove — tap toggles the
state (TalkBack label flips both ways), the full text survives in semantics,
the route never changes — plus the exact source seam
`maxLines = if (expanded) Int.MAX_VALUE else 2`. Visual clamp/expand geometry
is verified on a real device by the parallel QA acceptance card (dual
acceptance), not claimed from this environment.

**Known limitation (documented, ruling-consistent):** rows paged in by §2.4
show no running-balance line — `GET /transactions` has no
`running_balance` field and the rulings permit no new API; the value is never
fabricated client-side (§2.3, docs/53 AC1.3).
